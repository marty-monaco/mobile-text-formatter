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


def format_recipe_output(recipe: dict) -> str:
    title = recipe.get("name", "Recipe")
    description = recipe.get("description", "")
    ingredients = recipe.get("recipeIngredient", [])

    raw_steps = recipe.get("recipeInstructions", [])
    steps = []
    for step in raw_steps:
        if isinstance(step, str):
            steps.append(step)
        elif isinstance(step, dict) and "text" in step:
            steps.append(step["text"])

    out = [f"<h2>{html.escape(title)}</h2>"]
    if description:
        out.append(f"<p><em>{html.escape(description)}</em></p>")

    if ingredients:
        out.append("<h3>Ingredients</h3><ul>")
        for item in ingredients:
            out.append(f"<li>{html.escape(item)}</li>")
        out.append("</ul>")

    if steps:
        out.append("<h3>Directions</h3><ol>")
        for step in steps:
            out.append(f"<li>{html.escape(step)}</li>")
        out.append("</ol>")

    return "".join(out)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_jina_proxy(target_url: str) -> str:
    proxy_url = f"https://r.jina.ai/{target_url}"
    resp = requests.get(proxy_url, impersonate="chrome124", timeout=20)
    if resp.status_code == 200 and resp.text.strip():
        # Third-party proxy output is untrusted — sanitize before returning.
        return sanitize_html(markdown.markdown(resp.text))
    return ""


@st.cache_data(ttl=3600, show_spinner=False)
def extract_from_url(raw_input: str) -> ExtractResult:
    match = re.search(r"(https?://[^\s]+)", raw_input.strip())
    if not match:
        return ExtractResult(ok=False, message="Please enter a valid URL starting with http:// or https://")

    clean_url = match.group(1)

    # 1. Preserve query parameters for shorteners and cloud drives
    parts = urlsplit(clean_url)
    preserve_query_domains = ("share.google", "bit.ly", "onedrive.live.com", "1drv.ms")
    if not any(domain in parts.netloc for domain in preserve_query_domains):
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    # 2. Force OneDrive links into direct binary download mode
    if any(d in parts.netloc for d in ("onedrive.live.com", "1drv.ms")):
        if "download=1" not in clean_url:
            clean_url += ("&" if "?" in clean_url else "?") + "download=1"

    # 3. Perform network request with redirection and real TLS verification.
    # NOTE: we deliberately do NOT set verify=False here. A blanket bypass
    # makes every fetch vulnerable to a man-in-the-middle silently swapping
    # in different content. If a specific site has a known-broken cert
    # chain, handle that as a narrow, visible exception rather than
    # disabling verification for every request.
    try:
        resp = requests.get(
            clean_url,
            impersonate="chrome124",
            timeout=15,
            allow_redirects=True,
        )
    except SSLError:
        return ExtractResult(
            ok=False,
            message=(
                "This site's TLS certificate could not be verified, so the page "
                "was not fetched for your safety. Try the manual paste box below instead."
            ),
        )
    except Exception:
        proxy_content = fetch_jina_proxy(clean_url)
        if proxy_content:
            return ExtractResult(ok=True, content=proxy_content)
        return ExtractResult(ok=False, message="Failed to fetch this URL.")

    anti_bot_patterns = ["icanhazip.com", "contentlicensing@people.inc", "captcha-delivery.com"]
    is_blocked = (
        resp.status_code in (403, 429)
        or any(pat in resp.text for pat in anti_bot_patterns)
    )

    if is_blocked:
        proxy_content = fetch_jina_proxy(clean_url)
        if proxy_content:
            return ExtractResult(ok=True, content=proxy_content)
        return ExtractResult(
            ok=False,
            message="This site blocked direct access. Please paste the article/recipe text into the manual box below.",
        )

    content_type = resp.headers.get("content-type", "").lower()
    content_disp = resp.headers.get("content-disposition", "").lower()
    final_url = resp.url.lower()

    # Match format by MIME type, Content-Disposition header, or URL extension
    if "application/pdf" in content_type or final_url.endswith(".pdf") or ".pdf" in content_disp:
        return ExtractResult(ok=True, content=extract_pdf(resp.content))

    if (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in content_type
        or final_url.endswith(".docx")
        or ".docx" in content_disp
    ):
        return ExtractResult(ok=True, content=extract_docx(resp.content))

    if "application/epub+zip" in content_type or final_url.endswith(".epub") or ".epub" in content_disp:
        return ExtractResult(ok=True, content=extract_epub(resp.content))

    if (
        "application/rtf" in content_type
        or "text/rtf" in content_type
        or final_url.endswith(".rtf")
        or ".rtf" in content_disp
    ):
        return ExtractResult(ok=True, content=extract_rtf(resp.content))

    if "text/plain" in content_type or final_url.endswith(".txt") or ".txt" in content_disp:
        return ExtractResult(ok=True, content=format_plain_text(resp.text))

    # Recipe Schema Parser
    recipe_data = extract_recipe_schema(resp.text)
    if recipe_data:
        return ExtractResult(ok=True, content=format_recipe_output(recipe_data))

    # Article text extraction via Trafilatura
    body = trafilatura.extract(resp.text, include_comments=False)
    if not body:
        body = trafilatura.extract(resp.text, favor_recall=True)

    if body:
        return ExtractResult(ok=True, content=format_plain_text(body))

    proxy_content = fetch_jina_proxy(clean_url)
    if proxy_content:
        return ExtractResult(ok=True, content=proxy_content)

    return ExtractResult(ok=False, message="Unable to extract readable content from this page.")


# ---------------------------------------------------------------------------
# UI Layout
# ---------------------------------------------------------------------------
st.title("📖 Clean 9:16 Reader")

# History Dropdown
history_list = load_history()
selected_history_url = None

if history_list:
    with st.expander("🕒 Recent URLs (Last 10)", expanded=False):
        chosen = st.selectbox(
            "Select a previously accessed page:",
            options=["-- Select from history --"] + history_list,
            index=0,
        )
        if chosen != "-- Select from history --":
            selected_history_url = chosen

        if st.button("🗑️ Clear URL History"):
            clear_history()
            st.rerun()

url_input = st.text_input(
    "Paste URL (Article, Recipe, OneDrive, PDF, or text):",
    value=selected_history_url if selected_history_url else "",
    placeholder="https://...",
)

uploaded_file = st.file_uploader(
    "Or upload document:",
    type=["txt", "pdf", "docx", "epub", "rtf", "md"],
)

with st.expander("📋 Manual Text / Recipe Paste (Fallback)"):
    manual_text = st.text_area("Paste raw text or recipe directions here:", height=150)

content = ""
active_source_url = None

if manual_text.strip():
    content = format_plain_text(manual_text)
elif url_input:
    with st.spinner("Extracting & formatting..."):
        result = extract_from_url(url_input)
        if result.ok:
            content = result.content
            active_source_url = url_input
        else:
            st.error(result.message)
elif uploaded_file:
    with st.spinner("Formatting file..."):
        try:
            b = uploaded_file.read()
            ext = uploaded_file.name.split(".")[-1].lower()
            if ext == "pdf":
                content = extract_pdf(b)
            elif ext == "docx":
                content = extract_docx(b)
            elif ext == "epub":
                content = extract_epub(b)
            elif ext == "rtf":
                content = extract_rtf(b)
            elif ext == "md":
                content = extract_markdown(b)
            else:
                content = format_plain_text(decode_bytes(b))
        except Exception as e:
            st.error(f"Error parsing file: {e}")

# Presentation Controls & Reader Display
if content:
    if active_source_url:
        save_url_to_history(active_source_url)

    raw_plain_text = BeautifulSoup(content, "html.parser").get_text(separator=" ")
    word_count = len(raw_plain_text.split())
    reading_time_min = max(1, round(word_count / 200)) if word_count > 0 else 0

    st.divider()

    col_meta, col_copy = st.columns([3, 2])
    with col_meta:
        st.markdown(
            f"<div class='meta-chip'>⏱️ ~{reading_time_min} min read &nbsp;•&nbsp; {word_count:,} words</div>",
            unsafe_allow_html=True,
        )
    with col_copy:
        with st.popover("📋 Copy Text"):
            st.code(raw_plain_text, language=None)

    with st.expander("⚙️ Reader Controls & Auto-Scroll", expanded=False):
        font_family_opt = st.selectbox(
            "Typeface",
            ["Sans-Serif", "Serif", "Monospace"],
            key="reader_font",
        )
        font_size_val = st.slider(
            "Font Size (px)",
            min_value=14,
            max_value=32,
            value=18,
            step=1,
            key="reader_size",
        )
        theme = st.selectbox(
            "Color Theme",
            ["Light", "Sepia", "Dark"],
            key="reader_theme",
        )

        st.markdown("**Hands-Free Auto-Scroll**")
        scroll_speed = st.select_slider(
            "Scroll Speed",
            options=["Off", "Slow", "Medium", "Fast"],
            value="Off",
            key="auto_scroll_speed",
        )

    speed_ms = {"Off": 0, "Slow": 70, "Medium": 40, "Fast": 20}[scroll_speed]

    # Auto-scroll must run inside a real iframe (components.html) for its
    # <script> to execute at all, and it reaches into window.parent so it
    # scrolls the actual page rather than the (invisible) iframe itself.
    components.html(
        f"""
        <script>
        (function() {{
            const parentWin = window.parent;
            if (parentWin.__cleanReaderScrollTimer) {{
                clearInterval(parentWin.__cleanReaderScrollTimer);
                parentWin.__cleanReaderScrollTimer = null;
            }}
            const speedMs = {speed_ms};
            if (speedMs > 0) {{
                parentWin.__cleanReaderScrollTimer = setInterval(() => {{
                    parentWin.document.documentElement.scrollBy({{ top: 1, behavior: 'smooth' }});
                }}, speedMs);
            }}
        }})();
        </script>
        """,
        height=0,
    )

    font_map = {
        "Sans-Serif": "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
        "Serif": "Georgia, Cambria, 'Times New Roman', serif",
        "Monospace": "Menlo, Consolas, Monaco, monospace",
    }
    theme_map = {
        "Light": "background-color: #ffffff; color: #111111;",
        "Sepia": "background-color: #fbf0d9; color: #433422;",
        "Dark": "background-color: #1a1a1a; color: #e0e0e0;",
    }

    # Sanitize once more right before render — belt-and-suspenders, since
    # this is the single place all content paths converge before hitting
    # unsafe_allow_html=True.
    safe_content = sanitize_html(content)

    st.markdown(
        f"""
        <div class="reader-frame" style="
            font-family: {font_map[font_family_opt]};
            font-size: {font_size_val}px;
            {theme_map[theme]}
        ">
            {safe_content}
        </div>
        """,
        unsafe_allow_html=True,
    )
