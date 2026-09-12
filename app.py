"""
Clean Reader — mobile-friendly text/recipe/article reader.
"""

import html
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
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
# History (session-scoped)
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
# Library (persisted parsed content)
# NOTE: this writes to local disk, which is wiped on redeploy on Streamlit
# Community Cloud. Tracked as a known limitation — migrating this to
# Supabase is the planned fix (see roadmap discussion).
# ---------------------------------------------------------------------------

LIBRARY_FILE = Path("saved_articles.json")


def load_library() -> dict:
    if LIBRARY_FILE.exists():
        try:
            return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_to_library(key_url: str, title: str, sanitized_content: str):
    library = load_library()
    library[key_url] = {
        "title": title or key_url,
        "content": sanitized_content,
        "saved_at": time.time(),
    }
    try:
        LIBRARY_FILE.write_text(json.dumps(library), encoding="utf-8")
    except Exception:
        pass


def delete_from_library(key_url: str):
    library = load_library()
    library.pop(key_url, None)
    try:
        LIBRARY_FILE.write_text(json.dumps(library), encoding="utf-8")
    except Exception:
        pass


def guess_title(sanitized_content: str, fallback: str) -> str:
    soup = BeautifulSoup(sanitized_content, "html.parser")
    heading = soup.find(["h1", "h2", "h3"])
    if heading and heading.get_text(strip=True):
        return heading.get_text(strip=True)[:120]
    return fallback


def to_plain_text_for_export(sanitized_content: str) -> str:
    """Simple, shareable plain-text rendering — good for pasting a recipe
    into an email or text message. Headings are upper-cased, list items
    get a leading dash, everything else stays as plain paragraphs."""
    soup = BeautifulSoup(sanitized_content, "html.parser")
    lines = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        text = el.get_text(strip=True)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            lines.append(f"\n{text.upper()}\n")
        elif el.name == "li":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mobile scaffolding / PWA metas
# ---------------------------------------------------------------------------
st.markdown(
    """
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <link rel="manifest" href="app/static/manifest.json">

    <style>
    .block-container {
        padding-top: 1rem;
        padding-bottom: 3rem;
        padding-left: 0.85rem;
        padding-right: 0.85rem;
        max-width: 620px;
    }
    .meta-chip {
        font-size: 0.82rem;
        color: #888888;
        margin-bottom: 0.75rem;
    }
    </style>
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


def find_recipe_section_by_anchor(raw_html: str) -> Optional[str]:
    """Look for a 'Jump to Recipe' style link and, if found, return only the
    HTML it points to — trimming the preamble story most recipe blogs put
    before the actual recipe. Returns None if nothing convincing is found,
    so the caller keeps using normal article extraction."""
    soup = BeautifulSoup(raw_html, "html.parser")

    anchor = soup.find(
        lambda tag: tag.name in ("a", "button")
        and tag.get_text(strip=True)
        and re.search(r"jump\s*to\s*recipe|go\s*to\s*recipe|print\s*recipe", tag.get_text(strip=True), re.I)
    )
    if not anchor:
        return None

    target_id = None
    href = anchor.get("href", "")
    if href.startswith("#"):
        target_id = href[1:]
    target_id = target_id or anchor.get("data-target") or anchor.get("aria-controls")

    target = soup.find(id=target_id) if target_id else None

    if not target:
        target = soup.find(
            lambda tag: tag.get("id") and "recipe" in tag.get("id", "").lower()
        ) or soup.find(
            lambda tag: tag.get("class")
            and any(
                any(marker in c.lower() for marker in ("recipe-card", "tasty-recipe", "wprm-recipe"))
                for c in tag.get("class", [])
            )
        )

    if not target:
        return None

    if len(target.find_all("li")) < 2:
        return None

    return str(target)


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
        return sanitize_html(markdown.markdown(resp.text))
    return ""


@st.cache_data(ttl=3600, show_spinner=False)
def extract_from_url(raw_input: str) -> ExtractResult:
    match = re.search(r"(https?://[^\s]+)", raw_input.strip())
    if not match:
        return ExtractResult(ok=False, message="Please enter a valid URL starting with http:// or https://")

    clean_url = match.group(1)

    parts = urlsplit(clean_url)
    preserve_query_domains = ("share.google", "bit.ly", "onedrive.live.com", "1drv.ms")
    if not any(domain in parts.netloc for domain in preserve_query_domains):
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    if any(d in parts.netloc for d in ("onedrive.live.com", "1drv.ms")):
        if "download=1" not in clean_url:
            clean_url += ("&" if "?" in clean_url else "?") + "download=1"

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

    recipe_data = extract_recipe_schema(resp.text)
    if recipe_data:
        return ExtractResult(ok=True, content=format_recipe_output(recipe_data))

    recipe_section_html = find_recipe_section_by_anchor(resp.text)
    if recipe_section_html:
        return ExtractResult(ok=True, content=sanitize_html(recipe_section_html))

    body = trafilatura.extract(resp.text, include_comments=False)
    if not body:
        body = trafilatura.extract(resp.text, favor_recall=True)

    if body:
        return ExtractResult(ok=True, content=format_plain_text(body))

    proxy_content = fetch_jina_proxy(clean_url)
    if proxy_content:
        return ExtractResult(ok=True, content=proxy_content)

    return ExtractResult(ok=False, message="Unable to extract readable content from this page.")


def render_reader(
    html_content: str,
    plain_text_for_tts: str,
    font_family: str,
    font_size: int,
    theme_style: str,
    pane_height: int = 600,
):
    """Renders the article in a self-contained iframe with its own toolbar:
    a scroll start/pause toggle, three speed presets, and a read-aloud
    toggle using the browser's built-in SpeechSynthesis API. All of this
    lives in plain JS inside the iframe (not Streamlit widgets), so using
    it does NOT trigger a Streamlit rerun and won't interrupt playback.
    Changing font/theme/pane-height (real Streamlit widgets) WILL rebuild
    the iframe and reset playback — that's an inherent Streamlit tradeoff,
    not a bug."""
    tts_text_json = json.dumps(plain_text_for_tts)

    doc = f"""
    <style>
      html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, sans-serif; }}
      #progress-container {{
          position: fixed; top: 0; left: 0; width: 100%; height: 6px;
          background: rgba(128,128,128,0.2); z-index: 999;
      }}
      #progress-bar {{ width: 0%; height: 100%; background-color: #ff4b4b; transition: width 0.1s ease-out; }}
      #toolbar {{
          display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
          padding: 8px 8px 6px 8px; background: #f2f2f2; border-bottom: 1px solid #ddd;
          margin-top: 6px;
      }}
      .ctrl-btn, .speed-btn {{
          border: 1px solid #ccc; background: #fff; color: #222;
          border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer;
      }}
      .speed-btn.active {{ background: #ff4b4b; color: #fff; border-color: #ff4b4b; }}
      .speed-group {{ display: flex; gap: 4px; }}
      #reader-scroll {{
          height: {pane_height}px; overflow-y: auto; -webkit-overflow-scrolling: touch;
          box-sizing: border-box;
      }}
      .reader-frame {{
          font-family: {font_family}; font-size: {font_size}px; {theme_style}
          padding: 1.25rem 1rem;
      }}
      .reader-frame p {{ margin-bottom: 1.35em; line-height: 1.8; }}
    </style>
    <div id="progress-container"><div id="progress-bar"></div></div>
    <div id="toolbar">
      <button id="scrollToggleBtn" class="ctrl-btn">▶️ Start Scroll</button>
      <span class="speed-group">
        <button class="speed-btn" data-ms="70">Slow</button>
        <button class="speed-btn active" data-ms="40">Medium</button>
        <button class="speed-btn" data-ms="20">Fast</button>
      </span>
      <button id="ttsBtn" class="ctrl-btn">🔊 Read Aloud</button>
    </div>
    <div id="reader-scroll">
      <div class="reader-frame">{html_content}</div>
    </div>
    <script>
      (function() {{
        const scrollEl = document.getElementById('reader-scroll');
        const bar = document.getElementById('progress-bar');
        const scrollToggleBtn = document.getElementById('scrollToggleBtn');
        const ttsBtn = document.getElementById('ttsBtn');
        const speedBtns = document.querySelectorAll('.speed-btn');

        function updateProgress() {{
          const total = scrollEl.scrollHeight - scrollEl.clientHeight;
          if (total > 0) {{ bar.style.width = (scrollEl.scrollTop / total * 100) + '%'; }}
        }}
        scrollEl.addEventListener('scroll', updateProgress);
        updateProgress();

        // --- Auto-scroll: start/pause + speed ---
        let scrollTimer = null;
        let scrollSpeedMs = 40;
        let scrollActive = false;

        function startScrolling() {{
          if (scrollTimer) clearInterval(scrollTimer);
          scrollTimer = setInterval(() => {{
            scrollEl.scrollBy({{ top: 1, behavior: 'auto' }});
          }}, scrollSpeedMs);
        }}
        function stopScrolling() {{
          if (scrollTimer) {{ clearInterval(scrollTimer); scrollTimer = null; }}
        }}

        scrollToggleBtn.addEventListener('click', () => {{
          scrollActive = !scrollActive;
          if (scrollActive) {{
            startScrolling();
            scrollToggleBtn.textContent = '⏸ Pause Scroll';
          }} else {{
            stopScrolling();
            scrollToggleBtn.textContent = '▶️ Start Scroll';
          }}
        }});

        speedBtns.forEach(btn => {{
          btn.addEventListener('click', () => {{
            speedBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            scrollSpeedMs = parseInt(btn.dataset.ms, 10);
            if (scrollActive) startScrolling();
          }});
        }});

        // --- Read Aloud (SpeechSynthesis) ---
        const ttsFullText = {tts_text_json};
        let ttsChunks = [];
        let ttsIndex = 0;
        let ttsSpeaking = false;

        function chunkText(text) {{
          const sentences = text.match(/[^.!?]+[.!?]*/g) || [text];
          const chunks = [];
          let current = "";
          for (const s of sentences) {{
            if ((current + s).length > 200 && current) {{
              chunks.push(current.trim());
              current = s;
            }} else {{
              current += s;
            }}
          }}
          if (current.trim()) chunks.push(current.trim());
          return chunks;
        }}

        function speakNext() {{
          if (ttsIndex >= ttsChunks.length) {{
            ttsSpeaking = false;
            ttsBtn.textContent = '🔊 Read Aloud';
            return;
          }}
          const utter = new SpeechSynthesisUtterance(ttsChunks[ttsIndex]);
          utter.onend = () => {{ ttsIndex += 1; speakNext(); }};
          window.speechSynthesis.speak(utter);
        }}

        ttsBtn.addEventListener('click', () => {{
          if (!ttsSpeaking) {{
            if (window.speechSynthesis.paused) {{
              window.speechSynthesis.resume();
            }} else {{
              ttsChunks = chunkText(ttsFullText);
              ttsIndex = 0;
              window.speechSynthesis.cancel();
              speakNext();
            }}
            ttsSpeaking = true;
            ttsBtn.textContent = '⏸ Pause Reading';
          }} else {{
            window.speechSynthesis.pause();
            ttsSpeaking = false;
            ttsBtn.textContent = '▶️ Resume Reading';
          }}
        }});
      }})();
    </script>
    """
    components.html(doc, height=pane_height + 70, scrolling=False)


# ---------------------------------------------------------------------------
# UI Layout
# ---------------------------------------------------------------------------
st.title("📖 Clean 9:16 Reader")

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

library = load_library()
loaded_from_library = None

if library:
    with st.expander("📚 Saved Articles (offline)", expanded=False):
        for key, entry in sorted(library.items(), key=lambda kv: kv[1]["saved_at"], reverse=True):
            col_title, col_open, col_txt, col_html, col_del = st.columns([4, 1.3, 1, 1, 1])
            col_title.write(entry["title"])
            if col_open.button("Open", key=f"open_{key}"):
                loaded_from_library = entry["content"]
            col_txt.download_button(
                "⬇️ .txt",
                data=to_plain_text_for_export(entry["content"]),
                file_name=f"{entry['title'][:60]}.txt",
                mime="text/plain",
                key=f"dl_txt_{key}",
            )
            col_html.download_button(
                "⬇️ .html",
                data=entry["content"],
                file_name=f"{entry['title'][:60]}.html",
                mime="text/html",
                key=f"dl_html_{key}",
            )
            if col_del.button("🗑️", key=f"del_{key}"):
                delete_from_library(key)
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

if loaded_from_library:
    content = loaded_from_library
elif manual_text.strip():
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

    if active_source_url:
        if st.button("💾 Save to Library (read offline later)"):
            sanitized = sanitize_html(content)
            title = guess_title(sanitized, fallback=active_source_url)
            save_to_library(active_source_url, title, sanitized)
            st.success("Saved for offline reading.")

    with st.expander("⚙️ Reader Controls", expanded=False):
        font_family_opt = st.selectbox("Typeface", ["Sans-Serif", "Serif", "Monospace"], key="reader_font")
        font_size_val = st.slider("Font Size (px)", min_value=14, max_value=32, value=18, step=1, key="reader_size")
        theme = st.selectbox("Color Theme", ["Light", "Sepia", "Dark"], key="reader_theme")
        pane_height_val = st.slider(
            "Reading Pane Height (px)", min_value=400, max_value=1000, value=600, step=50, key="reader_pane_height"
        )
        st.caption("Scroll and read-aloud controls are in the toolbar above the article itself.")

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

    safe_content = sanitize_html(content)

    render_reader(
        html_content=safe_content,
        plain_text_for_tts=raw_plain_text,
        font_family=font_map[font_family_opt],
        font_size=font_size_val,
        theme_style=theme_map[theme],
        pane_height=pane_height_val,
    )
