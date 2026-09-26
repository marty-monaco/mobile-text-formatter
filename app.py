"""
Clean Reader — mobile-friendly text/recipe/article reader with Supabase persistence,
Internet Archive recovery, and dedicated Medium/Substack extraction.
"""

import hashlib
import html
import io
import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, urlparse

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
from recipe_scrapers import scrape_me
from striprtf.striprtf import rtf_to_text
from supabase import Client, create_client

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
    cleaned = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Force-harden any links bleach let through: untrusted anchors that keep
    # target="_blank" without rel="noopener noreferrer" enable reverse-tabnabbing.
    soup = BeautifulSoup(cleaned, "html.parser")
    for a in soup.find_all("a"):
        a["rel"] = "noopener noreferrer nofollow"
        if a.get("target") is None:
            a["target"] = "_blank"
    return str(soup)


@dataclass
class ExtractResult:
    ok: bool
    content: str = ""
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Supabase Client & Database Persistence
# ---------------------------------------------------------------------------

@st.cache_resource
def get_supabase_client() -> Optional[Client]:
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


sb = get_supabase_client()


def load_history() -> list:
    if sb:
        try:
            resp = sb.table("read_history").select("url").order("accessed_at", desc=True).limit(10).execute()
            urls = []
            for row in resp.data:
                if row["url"] not in urls:
                    urls.append(row["url"])
            return urls
        except Exception:
            pass
    return st.session_state.get("url_history", [])


def save_url_to_history(url: str):
    if sb:
        try:
            sb.table("read_history").insert({"url": url}).execute()
        except Exception:
            pass
    history = st.session_state.get("url_history", [])
    if url in history:
        history.remove(url)
    history.insert(0, url)
    st.session_state["url_history"] = history[:10]


def clear_history():
    if sb:
        try:
            sb.table("read_history").delete().neq("url", "").execute()
        except Exception:
            pass
    st.session_state["url_history"] = []


def load_library() -> list:
    if sb:
        try:
            resp = sb.table("saved_articles").select("*").order("saved_at", desc=True).execute()
            return resp.data or []
        except Exception:
            return []
    return []


def save_to_library(url: str, title: str, content: str, word_count: int) -> bool:
    if sb:
        try:
            sb.table("saved_articles").upsert({
                "url": url,
                "title": title or url,
                "content": content,
                "word_count": word_count,
            }, on_conflict="url").execute()
            return True
        except Exception:
            return False
    return False


def delete_from_library(article_id: str):
    if sb:
        try:
            sb.table("saved_articles").delete().eq("id", article_id).execute()
        except Exception:
            pass


def guess_title(sanitized_content: str, fallback: str) -> str:
    soup = BeautifulSoup(sanitized_content, "html.parser")
    heading = soup.find(["h1", "h2", "h3"])
    if heading and heading.get_text(strip=True):
        return heading.get_text(strip=True)[:120]
    return fallback


def to_plain_text_for_export(sanitized_content: str) -> str:
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
# Mobile Scaffolding / PWA Metas
# ---------------------------------------------------------------------------
st.markdown(
    """
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="theme-color" content="#1a1a1a">
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


# ---------------------------------------------------------------------------
# Recipe Extraction
# ---------------------------------------------------------------------------

def extract_recipe_via_scraper(url: str, html_str: Optional[str] = None) -> Optional[str]:
    try:
        scraper = scrape_me(url, html=html_str) if html_str else scrape_me(url)
        title = scraper.title()
        ingredients = scraper.ingredients()
        instructions = scraper.instructions().split("\n")
        yields = scraper.yields()
        total_time = scraper.total_time()

        out = [f"<h1>{html.escape(title)}</h1>"]
        meta = []
        if yields:
            meta.append(f"Yield: {html.escape(yields)}")
        if total_time:
            meta.append(f"Total Time: {total_time} mins")
        if meta:
            out.append(f"<p><em>{' | '.join(meta)}</em></p>")

        if ingredients:
            out.append("<h2>Ingredients</h2><ul>")
            for item in ingredients:
                out.append(f"<li>{html.escape(item)}</li>")
            out.append("</ul>")

        if instructions:
            out.append("<h2>Directions</h2><ol>")
            for step in instructions:
                if step.strip():
                    out.append(f"<li>{html.escape(step.strip())}</li>")
            out.append("</ol>")

        return "".join(out)
    except Exception:
        return None


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

    out = [f"<h1>{html.escape(title)}</h1>"]
    if description:
        out.append(f"<p><em>{html.escape(description)}</em></p>")

    if ingredients:
        out.append("<h2>Ingredients</h2><ul>")
        for item in ingredients:
            out.append(f"<li>{html.escape(item)}</li>")
        out.append("</ul>")

    if steps:
        out.append("<h2>Directions</h2><ol>")
        for step in steps:
            out.append(f"<li>{html.escape(step)}</li>")
        out.append("</ol>")

    return "".join(out)


def find_recipe_section_by_anchor(raw_html: str) -> Optional[str]:
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
                any(m in c.lower() for m in ("recipe-card", "tasty-recipe", "wprm-recipe"))
                for c in tag.get("class", [])
            )
        )

    if not target or len(target.find_all("li")) < 2:
        return None

    return str(target)


# ---------------------------------------------------------------------------
# Specialized Substack & Medium Resolvers
# ---------------------------------------------------------------------------

def is_substack_url(url: str, html_text: str = "") -> bool:
    parsed = urlparse(url)
    if "substack.com" in parsed.netloc:
        return True
    if any(marker in html_text for marker in ("substackcdn.com", "substack-custom-domains", "Substack")):
        return True
    return False


def extract_substack(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if "p" in path_parts:
            slug = path_parts[path_parts.index("p") + 1]
            api_endpoint = f"{parsed.scheme}://{parsed.netloc}/api/v1/posts/{slug}"
            resp = requests.get(
                api_endpoint,
                impersonate="chrome124",
                timeout=12,
                headers={"Accept": "application/json"},
            )
            if resp.status_code == 200:
                data = resp.json()
                body_html = data.get("body_html", "")
                title = data.get("title", "")
                subtitle = data.get("subtitle", "")

                header_html = f"<h1>{html.escape(title)}</h1>"
                if subtitle:
                    header_html += f"<p><em>{html.escape(subtitle)}</em></p>"

                if body_html:
                    return header_html + sanitize_html(body_html)
    except Exception:
        pass
    return None


def extract_medium(url: str) -> Optional[str]:
    freedium_url = f"https://freedium.cfd/{url}"
    try:
        resp = requests.get(freedium_url, impersonate="chrome124", timeout=15)
        if resp.status_code == 200 and len(resp.text) > 500:
            soup = BeautifulSoup(resp.text, "html.parser")
            article = soup.find("article") or soup.find("main")
            if article:
                return sanitize_html(str(article))
            body = trafilatura.extract(resp.text, favor_recall=True)
            if body:
                return format_plain_text(body)
    except Exception:
        pass

    proxy_jina = fetch_jina_proxy(url)
    if proxy_jina and len(proxy_jina) > 300:
        return proxy_jina

    return None


# ---------------------------------------------------------------------------
# Fallback Gateways
# ---------------------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_archive_org_snapshot(target_url: str) -> Optional[Tuple[str, str]]:
    try:
        api_url = f"https://archive.org/wayback/available?url={target_url}"
        resp = requests.get(api_url, timeout=10)
        if resp.status_code != 200:
            return None

        data = resp.json()
        closest = data.get("archived_snapshots", {}).get("closest")
        if not closest or not closest.get("available"):
            return None

        raw_url = closest.get("url", "")
        timestamp = closest.get("timestamp", "")
        clean_snapshot_url = re.sub(r"/web/(\d{14})/", r"/web/\1id_/", raw_url)
        if "id_/" not in clean_snapshot_url:
            clean_snapshot_url = raw_url.replace(f"/web/{timestamp}/", f"/web/{timestamp}id_/")

        archive_resp = requests.get(
            clean_snapshot_url,
            impersonate="chrome124",
            timeout=15,
            allow_redirects=True,
        )
        if archive_resp.status_code == 200 and len(archive_resp.text.strip()) > 300:
            date_formatted = (
                f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
                if len(timestamp) >= 8 else "Archived"
            )
            return archive_resp.text, date_formatted
    except Exception:
        return None
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_jina_proxy(target_url: str) -> str:
    proxy_url = f"https://r.jina.ai/{target_url}"
    try:
        resp = requests.get(proxy_url, impersonate="chrome124", timeout=20)
        if resp.status_code == 200 and resp.text.strip():
            return sanitize_html(markdown.markdown(resp.text))
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Core URL Extractor
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def extract_from_url(raw_input: str, prefer_wayback: bool = False) -> ExtractResult:
    match = re.search(r"(https?://[^\s]+)", raw_input.strip())
    if not match:
        return ExtractResult(ok=False, message="Please enter a valid URL starting with http:// or https://")

    clean_url = match.group(1)

    if is_substack_url(clean_url):
        substack_content = extract_substack(clean_url)
        if substack_content:
            return ExtractResult(ok=True, content=substack_content)

    if "medium.com" in clean_url or any(d in clean_url for d in ("towardsdatascience.com", "betterprogramming.pub")):
        medium_content = extract_medium(clean_url)
        if medium_content:
            return ExtractResult(ok=True, content=medium_content)

    parts = urlsplit(clean_url)
    preserve_query_domains = ("share.google", "bit.ly", "onedrive.live.com", "1drv.ms")
    if not any(domain in parts.netloc for domain in preserve_query_domains):
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    if any(d in parts.netloc for d in ("onedrive.live.com", "1drv.ms")):
        if "download=1" not in clean_url:
            clean_url += ("&" if "?" in clean_url else "?") + "download=1"

    if prefer_wayback:
        archive_res = fetch_archive_org_snapshot(clean_url)
        if archive_res:
            arch_html, arch_date = archive_res
            recipe = extract_recipe_via_scraper(clean_url, html_str=arch_html)
            if recipe:
                return ExtractResult(ok=True, content=f"<p><em>🏛️ Snapshot ({arch_date})</em></p>{recipe}")
            body = trafilatura.extract(arch_html, favor_recall=True)
            if body:
                return ExtractResult(ok=True, content=f"<p><em>🏛️ Snapshot ({arch_date})</em></p>{format_plain_text(body)}")

    try:
        resp = requests.get(clean_url, impersonate="chrome124", timeout=15, allow_redirects=True)
        status = resp.status_code
        body_text = resp.text
    except SSLError:
        return ExtractResult(ok=False, message="Site TLS error. Use the manual paste box below.")
    except Exception:
        status = 500
        body_text = ""

    anti_bot = ["icanhazip.com", "contentlicensing@people.inc", "captcha-delivery.com", "access denied"]
    is_blocked = status in (403, 429, 500) or any(pat in body_text.lower() for pat in anti_bot)

    if not is_blocked and body_text:
        if is_substack_url(clean_url, body_text):
            substack_content = extract_substack(clean_url)
            if substack_content:
                return ExtractResult(ok=True, content=substack_content)

        c_type = resp.headers.get("content-type", "").lower()
        c_disp = resp.headers.get("content-disposition", "").lower()
        f_url = resp.url.lower()

        if "application/pdf" in c_type or f_url.endswith(".pdf") or ".pdf" in c_disp:
            return ExtractResult(ok=True, content=extract_pdf(resp.content))
        if "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in c_type or f_url.endswith(".docx") or ".docx" in c_disp:
            return ExtractResult(ok=True, content=extract_docx(resp.content))
        if "application/epub+zip" in c_type or f_url.endswith(".epub") or ".epub" in c_disp:
            return ExtractResult(ok=True, content=extract_epub(resp.content))
        if "application/rtf" in c_type or "text/rtf" in c_type or f_url.endswith(".rtf") or ".rtf" in c_disp:
            return ExtractResult(ok=True, content=extract_rtf(resp.content))
        if "text/plain" in c_type or f_url.endswith(".txt") or ".txt" in c_disp:
            return ExtractResult(ok=True, content=format_plain_text(resp.text))

        recipe_scraped = extract_recipe_via_scraper(clean_url, html_str=body_text)
        if recipe_scraped:
            return ExtractResult(ok=True, content=recipe_scraped)

        recipe_data = extract_recipe_schema(body_text)
        if recipe_data:
            return ExtractResult(ok=True, content=format_recipe_output(recipe_data))

        recipe_section = find_recipe_section_by_anchor(body_text)
        if recipe_section:
            return ExtractResult(ok=True, content=sanitize_html(recipe_section))

        body = trafilatura.extract(body_text, include_comments=False) or trafilatura.extract(body_text, favor_recall=True)
        if body:
            return ExtractResult(ok=True, content=format_plain_text(body))

    archive_res = fetch_archive_org_snapshot(clean_url)
    if archive_res:
        arch_html, arch_date = archive_res
        recipe = extract_recipe_via_scraper(clean_url, html_str=arch_html)
        if recipe:
            return ExtractResult(ok=True, content=f"<p><em>🏛️ Snapshot ({arch_date})</em></p>{recipe}")
        recipe_data = extract_recipe_schema(arch_html)
        if recipe_data:
            return ExtractResult(ok=True, content=f"<p><em>🏛️ Snapshot ({arch_date})</em></p>{format_recipe_output(recipe_data)}")
        body = trafilatura.extract(arch_html, favor_recall=True)
        if body:
            return ExtractResult(ok=True, content=f"<p><em>🏛️ Snapshot ({arch_date})</em></p>{format_plain_text(body)}")

    proxy_content = fetch_jina_proxy(clean_url)
    if proxy_content:
        return ExtractResult(ok=True, content=proxy_content)

    return ExtractResult(ok=False, message="Firewalls blocked access and no archive found. Use manual paste below.")


# ---------------------------------------------------------------------------
# Reader Rendering
# ---------------------------------------------------------------------------

def render_reader(
    html_content: str,
    plain_text_for_tts: str,
    font_family: str,
    font_size: int,
    theme_style: str,
    pane_height: int = 600,
):
    # Resolved relative to this script's own location, not the process's
    # current working directory, so it doesn't break under a different
    # launch WORKDIR (Docker, systemd, a process manager, etc.).
    template_path = Path(__file__).parent / "reader_view.html"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        # Fallback inline if the template file is missing.
        template = (
            "<div style='{{ theme_style }} font-family:{{ font_family }}; "
            "font-size:{{ font_size }}px; padding:16px; min-height:{{ pane_height }}px;'>"
            "{{ content }}"
            "<script>window.__TTS_TEXT__ = {{ tts_text_json }};</script>"
            "</div>"
        )

    doc = (
        template
        .replace("{{ pane_height }}", str(pane_height))
        .replace("{{ font_family }}", font_family)
        .replace("{{ font_size }}", str(font_size))
        .replace("{{ theme_style }}", theme_style)
        .replace("{{ content }}", html_content)
        .replace("{{ tts_text_json }}", json.dumps(plain_text_for_tts))
    )
    components.html(doc, height=pane_height + 70, scrolling=False)


# ---------------------------------------------------------------------------
# UI Entrypoint
# ---------------------------------------------------------------------------

st.title("📖 Clean 9:16 Reader")

if not sb:
    st.info("💡 Supabase not connected. Operating in session memory mode.")

history_list = load_history()
selected_history_url = None

if history_list:
    with st.expander("🕒 Recent URLs", expanded=False):
        chosen = st.selectbox("Select a previously accessed page:", ["-- Select from history --"] + history_list, index=0)
        if chosen != "-- Select from history --":
            selected_history_url = chosen
        if st.button("🗑️ Clear History"):
            clear_history()
            st.rerun()

library_items = load_library()
loaded_from_library = None

if library_items:
    with st.expander(f"📚 Saved Articles ({len(library_items)})", expanded=False):
        for entry in library_items:
            art_id = entry["id"]
            col_title, col_open, col_txt, col_html, col_del = st.columns([4, 1.3, 1, 1, 1])
            col_title.write(entry.get("title", entry.get("url", "Untitled")))
            if col_open.button("Open", key=f"open_{art_id}"):
                loaded_from_library = entry["content"]
            col_txt.download_button(
                "⬇️ .txt",
                data=to_plain_text_for_export(entry["content"]),
                file_name=f"{entry.get('title', 'article')[:60]}.txt",
                mime="text/plain",
                key=f"dl_txt_{art_id}",
            )
            col_html.download_button(
                "⬇️ .html",
                data=entry["content"],
                file_name=f"{entry.get('title', 'article')[:60]}.html",
                mime="text/html",
                key=f"dl_html_{art_id}",
            )
            if col_del.button("🗑️", key=f"del_{art_id}"):
                delete_from_library(art_id)
                st.rerun()

col_input, col_ia_check = st.columns([3, 1])
with col_input:
    url_input = st.text_input(
        "Paste URL (Article, Recipe, OneDrive, PDF, or text):",
        value=selected_history_url if selected_history_url else "",
        placeholder="https://...",
    )
with col_ia_check:
    st.write("")
    st.write("")
    use_wayback = st.checkbox("🏛️ Wayback", help="Force retrieval from Internet Archive snapshot")

uploaded_file = st.file_uploader("Or upload document:", type=["txt", "pdf", "docx", "epub", "rtf", "md"])

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
        result = extract_from_url(url_input, prefer_wayback=use_wayback)
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
            f"<p style='opacity:0.7'>⏱️ ~{reading_time_min} min read  •  {word_count:,} words</p>",
            unsafe_allow_html=True,
        )
    with col_copy:
        with st.popover("📋 Copy Text"):
            st.code(raw_plain_text, language=None)

    save_key = active_source_url or f"upload_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"
    if st.button("💾 Save to Supabase Library (read offline later)"):
        sanitized = sanitize_html(content)
        title = guess_title(sanitized, fallback=active_source_url or "Uploaded Document")
        if save_to_library(save_key, title, sanitized, word_count):
            st.success("Saved to Supabase.")
            st.rerun()
        else:
            st.error("Could not save to Supabase. Verify database connections.")

    with st.expander("⚙️ Reader Controls", expanded=False):
        font_family_opt = st.selectbox("Typeface", ["Sans-Serif", "Serif", "Monospace"], key="reader_font")
        font_size_val = st.slider("Font Size (px)", min_value=14, max_value=32, value=18, step=1, key="reader_size")
        theme = st.selectbox("Color Theme", ["Light", "Sepia", "Dark"], key="reader_theme")
        pane_height_val = st.slider(
            "Reading Pane Height (px)", min_value=400, max_value=1000, value=600, step=50, key="reader_pane_height"
        )
        st.caption("Scroll and read-aloud controls are located in the top toolbar of the reader below.")

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
