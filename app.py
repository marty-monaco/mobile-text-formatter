import html
import io
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

import ebooklib
import extruct
import markdown
import streamlit as st
import trafilatura
from bs4 import BeautifulSoup
from curl_cffi import requests
from docx import Document
from ebooklib import epub
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text

st.set_page_config(page_title="Clean Reader", page_icon="📖", layout="centered")

HISTORY_FILE = "url_history.json"


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_url_to_history(url: str):
    history = load_history()
    if url in history:
        history.remove(url)
    history.insert(0, url)
    history = history[:10]
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f)
    except Exception:
        pass


def clear_history():
    if os.path.exists(HISTORY_FILE):
        try:
            os.remove(HISTORY_FILE)
        except Exception:
            pass


# Mobile Scaffolding, PWA Metas, and Progress Bar
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

    <script>
    window.addEventListener('scroll', () => {
        const doc = document.documentElement;
        const totalHeight = doc.scrollHeight - doc.clientHeight;
        if (totalHeight > 0) {
            const progress = (window.scrollY / totalHeight) * 100;
            const bar = document.getElementById('progress-bar');
            if (bar) bar.style.width = progress + '%';
        }
    });
    </script>
    """,
    unsafe_allow_html=True,
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
    return markdown.markdown(decode_bytes(file_bytes))


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


def fetch_jina_proxy(target_url: str) -> str:
    proxy_url = f"https://r.jina.ai/{target_url}"
    resp = requests.get(proxy_url, impersonate="chrome124", timeout=20)
    if resp.status_code == 200 and resp.text.strip():
        return markdown.markdown(resp.text)
    return ""


def extract_from_url(raw_input: str) -> str:
    match = re.search(r"(https?://[^\s]+)", raw_input.strip())
    if not match:
        return "<p>Please enter a valid URL starting with http:// or https://</p>"

    clean_url = match.group(1)

    parts = urlsplit(clean_url)
    if not ("share.google" in parts.netloc or "bit.ly" in parts.netloc):
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    try:
        resp = requests.get(
            clean_url,
            impersonate="chrome124",
            timeout=15,
            verify=False,
            allow_redirects=True,
        )
    except Exception:
        proxy_content = fetch_jina_proxy(clean_url)
        if proxy_content:
            return proxy_content
        raise

    anti_bot_patterns = ["icanhazip.com", "contentlicensing@people.inc", "captcha-delivery.com"]
    is_blocked = (
        resp.status_code in (403, 429)
        or any(pat in resp.text for pat in anti_bot_patterns)
    )

    if is_blocked:
        proxy_content = fetch_jina_proxy(clean_url)
        if proxy_content:
            return proxy_content
        return (
            "<p>This site blocked direct access. "
            "Please paste the article/recipe text into the manual box below.</p>"
        )

    content_type = resp.headers.get("content-type", "").lower()
    final_url = resp.url.lower()

    if "application/pdf" in content_type or final_url.endswith(".pdf"):
        return extract_pdf(resp.content)
    if "text/plain" in content_type or final_url.endswith(".txt"):
        return format_plain_text(resp.text)
    if final_url.endswith(".docx"):
        return extract_docx(resp.content)
    if final_url.endswith(".epub"):
        return extract_epub(resp.content)
    if final_url.endswith(".rtf"):
        return extract_rtf(resp.content)

    recipe_data = extract_recipe_schema(resp.text)
    if recipe_data:
        return format_recipe_output(recipe_data)

    body = trafilatura.extract(resp.text, include_comments=False)
    if not body:
        body = trafilatura.extract(resp.text, favor_recall=True)

    if body:
        return format_plain_text(body)

    proxy_content = fetch_jina_proxy(clean_url)
    if proxy_content:
        return proxy_content

    return "<p>Unable to extract readable content.</p>"


# UI Layout
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
    "Paste URL (Article, Recipe, PDF, or text):",
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
        try:
            content = extract_from_url(url_input)
            active_source_url = url_input
        except Exception as e:
            st.error(f"Failed to fetch content: {e}")
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
    if active_source_url and not content.startswith("<p>Unable to extract") and not content.startswith("<p>Please enter"):
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
    auto_scroll_script = ""
    if speed_ms > 0:
        auto_scroll_script = f"""
        <script>
        if (window.autoScrollTimer) clearInterval(window.autoScrollTimer);
        window.autoScrollTimer = setInterval(() => {{
            window.scrollBy({{ top: 1, behavior: 'smooth' }});
        }}, {speed_ms});
        </script>
        """

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

    st.markdown(
        f"""
        {auto_scroll_script}
        <div class="reader-frame" style="
            font-family: {font_map[font_family_opt]};
            font-size: {font_size_val}px;
            {theme_map[theme]}
        ">
            {content}
        </div>
        """,
        unsafe_allow_html=True,
    )
