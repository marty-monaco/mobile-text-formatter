"""
Clean Reader — mobile-friendly text/recipe/article reader.

Changes in this pass (see chat for full rationale):
1. Removed `verify=False` blanket TLS bypass; SSL errors now surface a clear
   message instead of silently disabling certificate checks.
2. All HTML that gets rendered with unsafe_allow_html=True is now passed
   through `sanitize_html()` (bleach) — closes an XSS hole in the markdown
   and Jina-proxy paths.
3. URL history moved to `st.session_state` instead of a shared JSON file on
   disk, so one user's browsing history isn't visible/clearable by another
   visitor if this is ever deployed for more than one person.
4. Fetch + extraction wrapped in `st.cache_data` so moving a slider (font
   size, theme, scroll speed) doesn't silently re-fetch the URL.
5. Replaced "magic string" error signaling (checking `.startswith(...)` on
   HTML content) with a small `ExtractResult` type.
6. Fixed auto-scroll and reading-progress bar: <script> tags inside HTML
   passed to st.markdown() never execute (browsers ignore injected
   <script> in innerHTML). Both now run via st.components.v1.html(), which
   renders in a real iframe where scripts do execute, and reach into
   window.parent to scroll the actual page.

Extra dependency introduced: bleach (pip install bleach)
"""

import html
import io
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import bleach
import ebooklib
import extruct
import markdown
import streamlit as st
import streamlit.components.v1 as components
import trafilatura
from bs4 import BeautifulSoup
from curl_cffi import requests
from curl_cffi.requests.exceptions import SSLError
from docx import Document
from ebooklib import epub
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text

st.set_page_config(page_title="Clean Reader", page_icon="📖", layout="centered")

# Allowlist for anything we render with unsafe_allow_html=True.
# Deliberately excludes <script>, event-handler attrs, javascript: URLs, etc.
ALLOWED_TAGS = [
    "p", "br", "hr", "h1", "h2", "h3", "h4",
    "ul", "ol", "li", "em", "strong", "i", "b", "u",
    "a", "img", "blockquote", "code", "pre", "span", "div", "table",
    "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def sanitize_html(raw_html: str) -> str:
    """Strip anything that isn't on the allowlist before we ever render it
    with unsafe_allow_html=True. Treat every extracted/converted document
    as untrusted input, since it ultimately comes from the open web."""
    return bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )


@dataclass
class ExtractResult:
    ok: bool
    content: str = ""
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# History (session-scoped — not shared across users/visitors)
# ---------------------------------------------------------------------------

def load_history() -> list:
    return st.session_state.get("url_history", [])


def save_url_to_history(url: str):
    history = load_history()
    if url in history:
        history.remove(url)
    history.insert(0, url)
    st.session_state["url_history"] = history[:10]


def clear_history():
    st.session_state["url_history"] = []


# ---------------------------------------------------------------------------
# Mobile scaffolding / PWA metas (no <script> here anymore — see components.html below)
# ---------------------------------------------------------------------------
st.markdown(
    """
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">

    <style>
    .block-container {
        padding-top: 1rem;
        padding-bottom: 3rem;
        padding-left: 0.85rem;
        padding-right: 0.85rem;
        max-width: 620px;
    }

    #progress-container {
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 4px;
        background-color: transparent;
        z-index: 99999;
    }
    #progress-bar {
        width: 0%;
        height: 100%;
        background-color: #ff4b4b;
        transition: width 0.1s ease-out;
    }

    .reader-frame {
        padding: 1.25rem 1rem;
        border-radius: 8px;
        word-break: break-word;
    }
    .reader-frame p {
        margin-bottom: 1.35em;
        line-height: 1.8;
    }
    .meta-chip {
        font-size: 0.82rem;
        color: #888888;
        margin-bottom: 0.75rem;
    }
    </style>

    <div id="progress-container">
        <div id="progress-bar"></div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Reading-progress bar: must live in a real iframe (components.html) so the
# <script> tag actually executes. It reaches into window.parent because the
# progress bar / page content it measures lives in the parent document, not
# inside this invisible iframe.
components.html(
    """
    <script>
    (function() {
        const parentWin = window.parent;
        const doc = parentWin.document;

        function updateProgress() {
            const el = doc.documentElement;
            const totalHeight = el.scrollHeight - el.clientHeight;
            const bar = doc.getElementById('progress-bar');
            if (bar && totalHeight > 0) {
                const progress = (parentWin.scrollY / totalHeight) * 100;
                bar.style.width = progress + '%';
            }
        }

        // Avoid stacking duplicate listeners across Streamlit reruns.
        if (parentWin.__cleanReaderProgressBound) {
            parentWin.removeEventListener('scroll', parentWin.__cleanReaderProgressHandler);
        }
        parentWin.__cleanReaderProgressHandler = updateProgress;
        parentWin.__cleanReaderProgressBound = true;
        parentWin.addEventListener('scroll', updateProgress);
    })();
    </script>
    """,
    height=0,
)


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "latin-1", "iso-8859-1", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def format_plain_text(raw_text: str) -> str:
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n+", text)
    clean_paragraphs = []

    for block in blocks:
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue

        is_short_form = any(len(l) < 35 for l in lines)
        if is_short_form and len(lines) > 1:
            joined = "<br>".join(html.escape(l) for l in lines)
        else:
            joined = html.escape(" ".join(lines))

        clean_paragraphs.append(f"<p>{joined}</p>")

    return "".join(clean_paragraphs)


def extract_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    return format_plain_text("\n\n".join(p.extract_text() or "" for p in reader.pages))


def extract_docx(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))
    return format_plain_text("\n\n".join(p.text for p in doc.paragraphs if p.text.strip()))


def extract_rtf(file_bytes: bytes) -> str:
    return format_plain_text(rtf_to_text(decode_bytes(file_bytes)))


def extract_epub(file_bytes: bytes) -> str:
    book = epub.read_epub(io.BytesIO(file_bytes))
    full_text = []
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), "html.parser")
            text = soup.get_text(separator="\n")
            if text.strip():
                full_text.append(text)
    return format_plain_text("\n\n".join(full_text))


def extract_markdown(file_bytes: bytes) -> str:
    # markdown.markdown() does not sanitize its output, so run it through
    # our allowlist before it's ever rendered.
    return sanitize_html(markdown.markdown(decode_bytes(file_bytes)))


def extract_recipe_schema(html_content: str):
    try:
        data = extruct.extract(html_content, syntaxes=["json-ld"])
        for node in data.get("json-ld", []):
            types = node.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if any(t.lower() == "recipe" for t in types):
                return node
    except Exception:
        return None
    return None


def
