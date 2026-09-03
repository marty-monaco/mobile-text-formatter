import html
import io
import json
import re
import ebooklib
import extruct
import markdown
import requests
import streamlit as st
import trafilatura
from bs4 import BeautifulSoup
from docx import Document
from ebooklib import epub
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text

st.set_page_config(page_title="Mobile Reader", page_icon="📖", layout="centered")

# CSS Scaffolding for mobile layout
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1rem;
        padding-bottom: 3rem;
        padding-left: 0.85rem;
        padding-right: 0.85rem;
        max-width: 620px;
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
    .recipe-meta {
        background: rgba(125, 125, 125, 0.1);
        padding: 10px;
        border-radius: 6px;
        margin-bottom: 1rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def decode_bytes(data: bytes) -> str:
    """Attempts common encodings used in legacy and archive text files."""
    for enc in ("utf-8", "latin-1", "iso-8859-1", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def format_plain_text(raw_text: str) -> str:
    """Reflows hard-wrapped lines into unified mobile paragraphs."""
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


# Extractors by Format
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
    raw_md = decode_bytes(file_bytes)
    return markdown.markdown(raw_md)


def extract_recipe_schema(html_content: str):
    """Bypasses life-story blogs and ads by reading JSON-LD recipe markup."""
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

    # Normalize instruction lists
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


def extract_from_url(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"},
        timeout=15,
        verify=False,
    )
    resp.encoding = resp.apparent_encoding
    ctype = resp.headers.get("Content-Type", "").lower()
    url_l = url.lower()

    if "application/pdf" in ctype or url_l.endswith(".pdf"):
        return extract_pdf(resp.content)
    if "text/plain" in ctype or url_l.endswith(".txt"):
        return format_plain_text(resp.text)
    if url_l.endswith(".docx"):
        return extract_docx(resp.content)
    if url_l.endswith(".epub"):
        return extract_epub(resp.content)
    if url_l.endswith(".rtf"):
        return extract_rtf(resp.content)

    # 1. Try Recipe Schema Parsing first (cleanest ad/fluff bypass)
    recipe_data = extract_recipe_schema(resp.text)
    if recipe_data:
        return format_recipe_output(recipe_data)

    # 2. Fall back to standard ad-free web page extraction
    body = trafilatura.extract(resp.text, include_comments=False)
    if not body:
        body = trafilatura.extract(resp.text, favor_recall=True)

    if body:
        return format_plain_text(body)

    return "<p>Unable to extract readable content.</p>"


# User Interface
st.title("📖 Clean 9:16 Reader")

url_input = st.text_input("Paste URL (Article, Recipe, PDF, or raw text):", placeholder="https://...")
uploaded_file = st.file_uploader(
    "Or upload document:",
    type=["txt", "pdf", "docx", "epub", "rtf", "md"],
)

content = ""

if url_input:
    with st.spinner("Extracting & removing ads..."):
        try:
            content = extract_from_url(url_input)
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

# Display Controls & Reader
if content:
    st.divider()

    c1, c2, c3 = st.columns(3)
    with c1:
        font_family_opt = st.selectbox("Typeface", ["Sans-Serif", "Serif", "Monospace"])
    with c2:
        font_size_val = st.slider("Font Size", min_value=14, max_value=28, value=18, step=1)
    with c3:
        theme = st.selectbox("Background", ["Light", "Sepia", "Dark"])

    # Map settings to inline styles
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
