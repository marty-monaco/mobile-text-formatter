import io
import requests
import streamlit as st
import trafilatura
from pypdf import PdfReader

# Configure for mobile readability
st.set_page_config(page_title="Mobile Reader", page_icon="📖", layout="centered")

# Custom CSS for optimal 9:16 mobile reading typography
st.markdown(
    """
    <style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
        padding-left: 1rem;
        padding-right: 1rem;
        max-width: 650px;
    }
    .reader-body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Georgia, serif;
        font-size: 1.15rem;
        line-height: 1.75;
        letter-spacing: 0.01em;
        word-break: break-word;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def extract_from_pdf(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_from_url(url):
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
        verify=False,
    )
    content_type = response.headers.get("Content-Type", "").lower()

    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        return extract_from_pdf(response.content)

    # Clean web extraction (strips ads, navigation, boilerplate)
    text = trafilatura.extract(response.text, include_comments=False)
    return text if text else "Could not extract readable text from this page."


st.title("📖 Clean Reader")

# Input Section
url_input = st.text_input("Paste URL (Article or PDF link):", placeholder="https://example.com/story")
uploaded_file = st.file_uploader("Or upload a PDF file:", type=["pdf"])

extracted_content = ""

if url_input:
    with st.spinner("Fetching and reformatting..."):
        try:
            extracted_content = extract_from_url(url_input)
        except Exception as e:
            st.error(f"Error loading URL: {e}")
elif uploaded_file:
    with st.spinner("Processing PDF..."):
        try:
            extracted_content = extract_from_pdf(uploaded_file.read())
        except Exception as e:
            st.error(f"Error reading file: {e}")

# Display Section
if extracted_content:
    st.divider()
    # Reader controls
    font_mode = st.radio("Style", ["Serif", "Sans-Serif"], horizontal=True, label_visibility="collapsed")
    font_family = "Georgia, serif" if font_mode == "Serif" else "-apple-system, sans-serif"

    st.markdown(
        f'<div class="reader-body" style="font-family: {font_family};">'
        f"{extracted_content.replace(chr(10), '<br><br>')}"
        f"</div>",
        unsafe_allow_html=True,
    )
