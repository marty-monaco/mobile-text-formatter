import html
import io
import re
import requests
import streamlit as st
import trafilatura
from pypdf import PdfReader

st.set_page_config(page_title="Mobile Reader", page_icon="📖", layout="centered")

# Mobile 9:16 optimized typography
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2.5rem;
        padding-left: 1rem;
        padding-right: 1rem;
        max-width: 650px;
    }
    .reader-body {
        font-size: 1.15rem;
        line-height: 1.75;
        letter-spacing: 0.01em;
        word-break: break-word;
        color: inherit;
    }
    .reader-body p {
        margin-bottom: 1.35em;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def format_plain_text(raw_text: str) -> str:
    """Reflows hard-wrapped text into proper paragraphs for mobile screens."""
    # Standardize line breaks
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    # Split into blocks separated by blank lines
    blocks = re.split(r"\n\s*\n+", text)
    clean_paragraphs = []

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        # Check if block looks like pre-formatted poetry/indented lines
        is_verse = any(line.startswith("    ") or len(line) < 40 for line in lines)

        if is_verse and len(lines) > 1:
            joined = "<br>".join(html.escape(l) for l in lines)
        else:
            # Unwrap standard hard-wrapped lines into a flowing paragraph
            joined = html.escape(" ".join(lines))

        clean_paragraphs.append(f"<p>{joined}</p>")

    return "".join(clean_paragraphs)


def extract_from_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return format_plain_text("\n\n".join(pages))


def decode_bytes(data: bytes) -> str:
    """Attempts common encodings used in legacy and archive text files."""
    for enc in ("utf-8", "latin-1", "iso-8859-1", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_from_url(url: str) -> str:
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=15,
        verify=False,
    )
    response.encoding = response.apparent_encoding

    content_type = response.headers.get("Content-Type", "").lower()
    url_lower = url.lower()

    # 1. Handle PDF
    if "application/pdf" in content_type or url_lower.endswith(".pdf"):
        return extract_from_pdf(response.content)

    # 2. Handle Plain Text files (.txt)
    if "text/plain" in content_type or url_lower.endswith(".txt"):
        return format_plain_text(response.text)

    # 3. Clean HTML articles
    text = trafilatura.extract(response.text, include_comments=False)
    if not text:
        text = trafilatura.extract(response.text, favor_recall=True)

    if text:
        return format_plain_text(text)
    return "<p>Could not extract readable text from this page.</p>"


# UI Layout
st.title("📖 Clean Reader")

url_input = st.text_input(
    "Paste URL (Article, .txt, or PDF):",
    placeholder="https://example.com/story or .txt link",
)

uploaded_file = st.file_uploader(
    "Or upload a file (.txt, .pdf):",
    type=["txt", "pdf"],
)

rendered_html = ""

if url_input:
    with st.spinner("Fetching and formatting..."):
        try:
            rendered_html = extract_from_url(url_input)
        except Exception as e:
            st.error(f"Error loading URL: {e}")
elif uploaded_file:
    with st.spinner("Formatting file..."):
        try:
            file_bytes = uploaded_file.read()
            if uploaded_file.name.lower().endswith(".pdf"):
                rendered_html = extract_from_pdf(file_bytes)
            else:
                raw_text = decode_bytes(file_bytes)
                rendered_html = format_plain_text(raw_text)
        except Exception as e:
            st.error(f"Error reading file: {e}")

# Presentation Controls & Render
if rendered_html:
    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        font_choice = st.radio(
            "Font",
            ["Serif", "Sans-Serif"],
            horizontal=True,
            label_visibility="collapsed",
        )
    with col2:
        font_size = st.select_slider(
            "Size",
            options=["1rem", "1.15rem", "1.3rem"],
            value="1.15rem",
            label_visibility="collapsed",
        )

    font_family = "Georgia, 'Times New Roman', serif" if font_choice == "Serif" else "-apple-system, BlinkMacSystemFont, sans-serif"

    st.markdown(
        f'<div class="reader-body" style="font-family: {font_family}; font-size: {font_size};">'
        f"{rendered_html}"
        f"</div>",
        unsafe_allow_html=True,
    )
