"""
Clean Reader — mobile-friendly reader for articles, recipes and documents.

- Supabase email/password login, per-user library + history (row-level security)
- One Supabase client PER browser session (never shared across users)
- "Keep me signed in" cookie with refresh-token rotation handling
- Selectable extraction method, Internet Archive / Archive.today / Substack / Medium recovery
- Recipe serving scaler
- Export: .txt, .md, .html, .epub
- Share-sheet launch via  ?url=<encoded link>
- Reader iframe (reader_view.html): auto-scroll, read-aloud, voice, speed, sleep timer
"""

import hashlib
import html
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, urlsplit, urlunsplit

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
from markdownify import markdownify as html_to_markdown
from pypdf import PdfReader
from recipe_scrapers import scrape_me
from striprtf.striprtf import rtf_to_text
from streamlit_cookies_controller import CookieController
from supabase import Client, create_client

try:
    from supabase.client import ClientOptions
except ImportError:  # older/newer supabase versions
    ClientOptions = None

st.set_page_config(page_title="Clean Reader", page_icon="📖", layout="centered")

# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

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
    # Harden links: target=_blank without rel=noopener enables reverse-tabnabbing.
    soup = BeautifulSoup(cleaned, "html.parser")
    for a in soup.find_all("a"):
        a["rel"] = "noopener noreferrer nofollow"
        if a.get("target") is None:
            a["target"] = "_blank"
    return str(soup)


def json_for_script(value) -> str:
    """json.dumps output that is safe to drop inside an inline <script>.
    ensure_ascii (the default) already escapes U+2028/2029; this also neutralises
    '</script>' and '<!--' by escaping < > & as unicode escapes."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


AUTO_METHOD = "Auto (best guess)"
EXTRACTION_METHODS = [
    AUTO_METHOD,
    "Recipe scraper",
    "Recipe schema (JSON-LD)",
    "Jump-to-recipe section",
    "Article text",
    "Internet Archive snapshot",
    "Archive.today mirror",
    "Wikipedia clean article",
    "Jina reader proxy",
    "Substack API",
    "Medium mirror",
]


@dataclass
class ExtractResult:
    ok: bool
    content: str = ""
    message: Optional[str] = None
    method_used: str = ""


# ---------------------------------------------------------------------------
# Supabase: per-session client + auth (with "keep me signed in" cookie)
# ---------------------------------------------------------------------------

COOKIE_NAME = "clean_reader_rt"
COOKIE_DAYS = 30


def secret(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def get_session_client() -> Optional[Client]:
    """One client per browser session. Do NOT cache this with cache_resource:
    a shared client would share one user's login with every other visitor.

    Background auto-refresh is disabled on purpose. Supabase refresh tokens are
    single-use, so a leftover client from an earlier browser session refreshing
    on its own timer would burn the token held in the cookie and sign you out.
    Refreshing only happens in get_db() during a live script run, and
    sync_cookie() then stores the new token."""
    url = secret("SUPABASE_URL")
    key = secret("SUPABASE_KEY")
    if not url or not key:
        return None
    if "sb_client" not in st.session_state:
        try:
            if ClientOptions is not None:
                st.session_state["sb_client"] = create_client(
                    url, key, options=ClientOptions(auto_refresh_token=False)
                )
            else:
                st.session_state["sb_client"] = create_client(url, key)
        except Exception:
            return None
    return st.session_state["sb_client"]


def current_user() -> Optional[dict]:
    return st.session_state.get("auth_user")


def get_db() -> Optional[Client]:
    """Returns the authenticated client (refreshing the token if needed), or None."""
    client = st.session_state.get("sb_client")
    if client is None or current_user() is None:
        return None
    try:
        session = client.auth.get_session()
        if session is None:
            return None
        client.postgrest.auth(session.access_token)
    except Exception:
        return None
    return client


def start_session(res, remember: bool):
    """Record a successful sign-in."""
    st.session_state["auth_user"] = {"id": res.user.id, "email": res.user.email}
    st.session_state["remember_me"] = remember
    st.session_state.pop("_logged_out", None)
    st.session_state.pop("_db_fail", None)


def sync_cookie(client: Client, cookies):
    """Keep the cookie holding the newest refresh token. Supabase rotates
    refresh tokens, so a stale cookie would stop working."""
    if not st.session_state.get("remember_me"):
        return
    try:
        session = client.auth.get_session()
    except Exception:
        return
    if session is None or not session.refresh_token:
        return
    if st.session_state.get("_cookie_rt") != session.refresh_token:
        try:
            cookies.set(
                COOKIE_NAME,
                session.refresh_token,
                max_age=COOKIE_DAYS * 86400,
                secure=True,
                same_site="lax",
            )
            st.session_state["_cookie_rt"] = session.refresh_token
        except Exception:
            pass


def try_restore_from_cookie(client: Client, cookies) -> str:
    """Returns:
    'restored' - signed in from the cookie
    'none'     - no cookie (yet)
    'failed'   - Supabase rejected the token (really expired or already used)
    'error'    - temporary problem (network, 5xx, rate limit); keep the cookie
    """
    token = cookies.get(COOKIE_NAME)
    if not token:
        return "none"
    try:
        res = client.auth.refresh_session(token)
    except Exception as e:
        status = getattr(e, "status", None)
        # A 4xx from Supabase means the token itself was rejected.
        return "failed" if status in (400, 401, 403, 422) else "error"
    if res and res.session and res.user:
        start_session(res, remember=True)
        return "restored"
    return "failed"


def request_sign_out():
    st.session_state["_do_sign_out"] = True


def perform_sign_out(client: Client, cookies):
    """Signs out this device only and forgets the cookie."""
    try:
        client.auth.sign_out({"scope": "local"})
    except Exception:
        try:
            client.auth.sign_out()
        except Exception:
            pass
    try:
        cookies.remove(COOKIE_NAME)
    except Exception:
        pass
    for key in list(st.session_state.keys()):
        if key != "sb_client":
            del st.session_state[key]
    st.session_state["_logged_out"] = True


def try_auto_login(client: Client) -> bool:
    """If AUTO_LOGIN is on in secrets, sign in with the stored account so the
    login screen is skipped. Returns True on success."""
    if str(secret("AUTO_LOGIN", False)).lower() != "true":
        return False
    email = secret("APP_EMAIL")
    password = secret("APP_PASSWORD")
    if not email or not password:
        return False
    try:
        res = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        st.session_state["login_notice"] = f"Automatic sign-in failed: {e}"
        return False
    if res.session and res.user:
        st.session_state["auth_user"] = {"id": res.user.id, "email": res.user.email}
        return True
    return False


def render_login(client: Client):
    st.title("📖 Clean Reader")
    notice = st.session_state.pop("login_notice", None)
    if notice:
        st.warning(notice)
    st.caption("Sign in to open your library.")

    allow_signup = str(secret("ALLOW_SIGNUP", True)).lower() != "false"

    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        remember = st.checkbox("Keep me signed in on this device", value=True)
        if allow_signup:
            c1, c2 = st.columns(2)
            do_sign_in = c1.form_submit_button("Sign in", use_container_width=True)
            do_sign_up = c2.form_submit_button("Create account", use_container_width=True)
        else:
            do_sign_in = st.form_submit_button("Sign in", use_container_width=True)
            do_sign_up = False

    if not (do_sign_in or do_sign_up):
        return
    if not email or not password:
        st.error("Enter your email and password.")
        return

    try:
        if do_sign_in:
            res = client.auth.sign_in_with_password({"email": email, "password": password})
        else:
            res = client.auth.sign_up({"email": email, "password": password})
    except Exception as e:
        st.error(str(e))
        return

    if res.session and res.user:
        start_session(res, remember)
        st.rerun()
    else:
        st.info("Account created. Check your email to confirm it, then sign in.")


# ---------------------------------------------------------------------------
# History + library (always scoped to the signed-in user)
# ---------------------------------------------------------------------------

def load_history() -> list:
    db = get_db()
    if db:
        try:
            resp = (
                db.table("read_history")
                .select("url")
                .eq("user_id", current_user()["id"])
                .order("accessed_at", desc=True)
                .limit(50)
                .execute()
            )
            urls = []
            for row in resp.data or []:
                if row["url"] not in urls:
                    urls.append(row["url"])
            return urls[:10]
        except Exception:
            pass
    return st.session_state.get("url_history", [])


def save_url_to_history(url: str):
    db = get_db()
    if db:
        try:
            db.table("read_history").insert(
                {"url": url, "user_id": current_user()["id"]}
            ).execute()
        except Exception:
            pass
    history = st.session_state.get("url_history", [])
    if url in history:
        history.remove(url)
    history.insert(0, url)
    st.session_state["url_history"] = history[:10]


def clear_history():
    db = get_db()
    if db:
        try:
            db.table("read_history").delete().eq("user_id", current_user()["id"]).execute()
        except Exception:
            pass
    st.session_state["url_history"] = []


def load_library() -> list:
    db = get_db()
    if not db:
        return []
    try:
        resp = (
            db.table("saved_articles")
            .select("*")
            .eq("user_id", current_user()["id"])
            .order("saved_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception:
        return []


def save_to_library(url: str, title: str, content: str, word_count: int) -> bool:
    db = get_db()
    if not db:
        return False
    try:
        db.table("saved_articles").upsert(
            {
                "user_id": current_user()["id"],
                "url": url,
                "title": title or url,
                "content": content,
                "word_count": word_count,
            },
            on_conflict="user_id,url",
        ).execute()
        return True
    except Exception as e:
        st.error(f"Database error: {e}")
        return False


def delete_from_library(article_id):
    db = get_db()
    if not db:
        return
    try:
        db.table("saved_articles").delete().eq("id", article_id).eq(
            "user_id", current_user()["id"]
        ).execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

BLOCK_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "blockquote", "pre", "td", "th"]
HEADING_TAGS = ("h1", "h2", "h3", "h4")


def guess_title(sanitized_content: str, fallback: str) -> str:
    soup = BeautifulSoup(sanitized_content, "html.parser")
    heading = soup.find(["h1", "h2", "h3"])
    if heading and heading.get_text(strip=True):
        return heading.get_text(strip=True)[:120]
    return fallback


def safe_filename(title: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "", title or "").strip()
    return (cleaned[:60] or "article").replace(" ", "_")


def leaf_blocks(soup: BeautifulSoup) -> list:
    """Block elements that don't contain other blocks (avoids double-reading)."""
    return [el for el in soup.find_all(BLOCK_TAGS) if not el.find(BLOCK_TAGS)]


def to_plain_text_for_export(content_html: str) -> str:
    soup = BeautifulSoup(content_html, "html.parser")
    blocks = leaf_blocks(soup)
    if not blocks:
        return soup.get_text("\n", strip=True)

    lines = []
    for el in blocks:
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in HEADING_TAGS:
            lines.append(f"\n{text.upper()}\n")
        elif el.name == "li":
            parent = el.parent
            if parent is not None and parent.name == "ol":
                items = parent.find_all("li", recursive=False)
                number = next((i for i, s in enumerate(items, 1) if s is el), 1)
                lines.append(f"{number}. {text}")
            else:
                lines.append(f"- {text}")
        else:
            lines.append(text)
    return "\n".join(lines).strip()


def html_to_tts_text(content_html: str) -> str:
    """One line per block, each ending in punctuation so the voice pauses."""
    soup = BeautifulSoup(content_html, "html.parser")
    blocks = leaf_blocks(soup)
    if not blocks:
        return soup.get_text(" ", strip=True)
    lines = []
    for el in blocks:
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if not text:
            continue
        if text[-1] not in ".!?:;":
            text += "."
        lines.append(text)
    return "\n".join(lines)


def to_markdown_export(content_html: str, title: str) -> str:
    body = html_to_markdown(content_html, heading_style="ATX")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not re.search(r"<h1", content_html, re.I):
        body = f"# {title}\n\n{body}"
    return body + "\n"


def build_standalone_html(content_html: str, title: str) -> str:
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:Georgia,serif;max-width:640px;margin:2rem auto;"
        "padding:0 1rem;line-height:1.7;font-size:18px;color:#111}"
        "img{max-width:100%}</style></head><body>"
        f"{content_html}</body></html>"
    )


@st.cache_data(show_spinner=False)
def build_epub(title: str, content_html: str) -> bytes:
    book = epub.EpubBook()
    book.set_identifier(hashlib.sha256((title + content_html).encode("utf-8")).hexdigest()[:16])
    book.set_title(title)
    book.set_language("en")

    chapter = epub.EpubHtml(title=title, file_name="article.xhtml", lang="en")
    chapter.content = f"<h1>{html.escape(title)}</h1>{content_html}"
    book.add_item(chapter)
    book.toc = (chapter,)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]

    buffer = io.BytesIO()
    epub.write_epub(buffer, book)
    return buffer.getvalue()


def render_export_buttons(content_html: str, title: str, key_prefix: str):
    base = safe_filename(title)
    st.download_button(
        "📄 Plain text (.txt)", to_plain_text_for_export(content_html),
        file_name=f"{base}.txt", mime="text/plain",
        key=f"{key_prefix}_txt", use_container_width=True,
    )
    st.download_button(
        "📝 Markdown (.md)", to_markdown_export(content_html, title),
        file_name=f"{base}.md", mime="text/markdown",
        key=f"{key_prefix}_md", use_container_width=True,
    )
    st.download_button(
        "🌐 Web page (.html)", build_standalone_html(content_html, title),
        file_name=f"{base}.html", mime="text/html",
        key=f"{key_prefix}_html", use_container_width=True,
    )
    st.download_button(
        "📚 E-book (.epub)", build_epub(title, content_html),
        file_name=f"{base}.epub", mime="application/epub+zip",
        key=f"{key_prefix}_epub", use_container_width=True,
    )


# ---------------------------------------------------------------------------
# Recipe serving scaler
# ---------------------------------------------------------------------------

UNICODE_FRACTIONS = {
    "½": 1 / 2, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 1 / 4, "¾": 3 / 4,
    "⅕": 1 / 5, "⅖": 2 / 5, "⅗": 3 / 5, "⅘": 4 / 5,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 1 / 8, "⅜": 3 / 8, "⅝": 5 / 8, "⅞": 7 / 8,
}
_FR = "".join(UNICODE_FRACTIONS)
_NUM = rf"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?\s*[{_FR}]|\d+(?:\.\d+)?|[{_FR}])"
# Leading quantity or range ("1 1/2", "½", "2-3", "2 to 3"). The trailing
# lookahead stops "1-inch piece" or "1/2" fragments from being misread.
_QTY_RE = re.compile(rf"^\s*({_NUM})(?:\s*(?:-|–|—|to)\s*({_NUM}))?(?![\d/]|[-–]\s*[A-Za-z])")
_FRACS = [
    (0.0, ""), (1 / 8, "1/8"), (1 / 4, "1/4"), (1 / 3, "1/3"), (3 / 8, "3/8"),
    (1 / 2, "1/2"), (5 / 8, "5/8"), (2 / 3, "2/3"), (3 / 4, "3/4"), (7 / 8, "7/8"),
    (1.0, ""),
]
SCALE_OPTIONS = {"½×": 0.5, "1×": 1.0, "2×": 2.0, "3×": 3.0}


def parse_quantity(token: str) -> Optional[float]:
    token = token.strip()
    try:
        if token in UNICODE_FRACTIONS:
            return UNICODE_FRACTIONS[token]
        m = re.fullmatch(rf"(\d+(?:\.\d+)?)\s*([{_FR}])", token)
        if m:
            return float(m.group(1)) + UNICODE_FRACTIONS[m.group(2)]
        m = re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", token)
        if m:
            return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
        m = re.fullmatch(r"(\d+)/(\d+)", token)
        if m:
            return int(m.group(1)) / int(m.group(2))
        return float(token)
    except (ValueError, ZeroDivisionError):
        return None


def format_quantity(x: float) -> str:
    if x >= 10:
        rounded = round(x, 1)
        return str(int(rounded)) if rounded == int(rounded) else str(rounded)
    whole = int(x)
    frac = x - whole
    frac_value, label = min(_FRACS, key=lambda f: abs(f[0] - frac))
    if frac_value == 1.0:
        whole += 1
        label = ""
    if whole == 0 and not label:
        return f"{x:.2f}".rstrip("0").rstrip(".") or "0"
    if whole and label:
        return f"{whole} {label}"
    return label or str(whole)


def scale_ingredient_text(text: str, factor: float) -> Optional[str]:
    """Returns the scaled text, or None if the line has no leading quantity."""
    m = _QTY_RE.match(text)
    if not m:
        return None
    low = parse_quantity(m.group(1))
    if low is None:
        return None
    scaled = format_quantity(low * factor)
    if m.group(2):
        high = parse_quantity(m.group(2))
        if high is None:
            return None
        scaled += "–" + format_quantity(high * factor)
    return scaled + text[m.end():]


def scale_recipe_html(content_html: str, factor: float) -> Tuple[str, int]:
    """Scales the list that follows an 'Ingredients' heading.
    Returns (new_html, number_of_lines_scaled)."""
    soup = BeautifulSoup(content_html, "html.parser")
    scaled_count = 0
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        if not re.match(r"\s*ingredients?\b", heading.get_text(" ", strip=True), re.I):
            continue
        nxt = heading.find_next(["ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"])
        if nxt is None or nxt.name not in ("ul", "ol"):
            continue
        for li in nxt.find_all("li"):
            node = next((s for s in li.find_all(string=True) if s.strip()), None)
            if node is None:
                continue
            updated = scale_ingredient_text(str(node), factor)
            if updated is not None:
                node.replace_with(updated)
                scaled_count += 1
    return str(soup), scaled_count


# ---------------------------------------------------------------------------
# Format extractors
# ---------------------------------------------------------------------------

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
# Recipe extraction
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


def extract_article_text(page_html: str) -> Optional[str]:
    body = trafilatura.extract(page_html, include_comments=False) or trafilatura.extract(
        page_html, favor_recall=True
    )
    return format_plain_text(body) if body else None


def extract_from_html(clean_url: str, page_html: str) -> Optional[Tuple[str, str]]:
    """Runs the automatic chain. Returns (content_html, method_label) or None."""
    recipe = extract_recipe_via_scraper(clean_url, html_str=page_html)
    if recipe:
        return recipe, "Recipe scraper"
    schema = extract_recipe_schema(page_html)
    if schema:
        return format_recipe_output(schema), "Recipe schema (JSON-LD)"
    section = find_recipe_section_by_anchor(page_html)
    if section:
        return sanitize_html(section), "Jump-to-recipe section"
    article = extract_article_text(page_html)
    if article:
        return article, "Article text"
    return None


# ---------------------------------------------------------------------------
# Substack / Medium
# ---------------------------------------------------------------------------

def is_substack_url(url: str, html_text: str = "") -> bool:
    parsed = urlparse(url)
    if "substack.com" in parsed.netloc:
        return True
    return any(m in html_text for m in ("substackcdn.com", "substack-custom-domains", "Substack"))


def extract_substack(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if "p" in path_parts:
            slug = path_parts[path_parts.index("p") + 1]
            api_endpoint = f"{parsed.scheme}://{parsed.netloc}/api/v1/posts/{slug}"
            resp = requests.get(
                api_endpoint, impersonate="chrome124", timeout=12,
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

    proxy = fetch_jina_proxy(url)
    if proxy and len(proxy) > 300:
        return proxy
    return None


# ---------------------------------------------------------------------------
# Wikipedia REST API extractor
# ---------------------------------------------------------------------------

def is_wikipedia_url(url: str) -> bool:
    parsed = urlparse(url)
    return "wikipedia.org" in parsed.netloc and "/wiki/" in parsed.path


def extract_wikipedia(url: str) -> Optional[str]:
    """Fetches clean mobile-first HTML directly from the official Wikipedia REST API."""
    try:
        parsed = urlparse(url)
        lang = parsed.netloc.split(".")[0] if "." in parsed.netloc else "en"
        title = parsed.path.split("/wiki/")[-1]
        if not title:
            return None

        api_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/html/{title}"
        resp = requests.get(
            api_url,
            impersonate="chrome124",
            timeout=12,
            headers={"User-Agent": "CleanReader/1.0 (cleanreader@example.com)"},
        )
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Strip Wikipedia-specific navigation and citation clutter
        for selector in [
            "table.sidebar", "table.infobox", ".mw-ref", ".navbox",
            ".noprint", "link", "style", ".mw-empty-elt",
        ]:
            for tag in soup.select(selector):
                tag.decompose()

        # Fix relative links to full Wikipedia links
        for a in soup.find_all("a", href=True):
            if a["href"].startswith("./"):
                a["href"] = f"https://{lang}.wikipedia.org/wiki/{a['href'][2:]}"

        return sanitize_html(str(soup))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Archive.today extractor
# ---------------------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_archive_today(target_url: str) -> Optional[str]:
    """Queries Archive.today/Archive.is for paywalled or hard-blocked articles."""
    gateways = ["https://archive.is/latest/", "https://archive.ph/latest/"]
    for base in gateways:
        try:
            resp = requests.get(
                f"{base}{target_url}",
                impersonate="chrome124",
                timeout=15,
                allow_redirects=True,
            )
            if resp.status_code == 200 and len(resp.text.strip()) > 1000:
                # Discard Archive.is banner/toolbar if present
                soup = BeautifulSoup(resp.text, "html.parser")
                for bar in soup.find_all(id=re.compile(r"header|toolbar|wm-ipp", re.I)):
                    bar.decompose()

                body = trafilatura.extract(str(soup), favor_recall=True)
                if body:
                    return f"<p><em>🏛️ Archive.today Snapshot</em></p>{format_plain_text(body)}"
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Fallback gateways
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
            clean_snapshot_url, impersonate="chrome124", timeout=15, allow_redirects=True
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


def fetch_live_html(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, impersonate="chrome124", timeout=15, allow_redirects=True)
        if resp.status_code == 200 and resp.text.strip():
            return resp.text
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Core URL extractor
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    preserve_query_domains = ("share.google", "bit.ly", "onedrive.live.com", "1drv.ms")
    if not any(d in parts.netloc for d in preserve_query_domains):
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if any(d in parts.netloc for d in ("onedrive.live.com", "1drv.ms")):
        if "download=1" not in url:
            url += ("&" if "?" in url else "?") + "download=1"
    return url


def run_forced_method(method: str, original_url: str, clean_url: str) -> ExtractResult:
    content: Optional[str] = None

    if method == "Substack API":
        content = extract_substack(original_url)
    elif method == "Medium mirror":
        content = extract_medium(original_url)
    elif method == "Wikipedia clean article":
        content = extract_wikipedia(original_url)
    elif method == "Archive.today mirror":
        content = fetch_archive_today(clean_url)
    elif method == "Jina reader proxy":
        content = fetch_jina_proxy(clean_url) or None
    elif method == "Internet Archive snapshot":
        snapshot = fetch_archive_org_snapshot(clean_url)
        if snapshot:
            arch_html, arch_date = snapshot
            found = extract_from_html(clean_url, arch_html)
            if found:
                content = f"<p><em>🏛️ Snapshot ({arch_date})</em></p>{found[0]}"
    else:
        # Methods that work from the live page (recipes, article text)
        page_html = fetch_live_html(clean_url)
        if not page_html:
            return ExtractResult(
                ok=False,
                message="Couldn't fetch the live page for this method. Try the Internet Archive or Jina options.",
            )
        if method == "Recipe scraper":
            content = extract_recipe_via_scraper(clean_url, html_str=page_html)
        elif method == "Recipe schema (JSON-LD)":
            schema = extract_recipe_schema(page_html)
            content = format_recipe_output(schema) if schema else None
        elif method == "Jump-to-recipe section":
            section = find_recipe_section_by_anchor(page_html)
            content = sanitize_html(section) if section else None
        elif method == "Article text":
            content = extract_article_text(page_html)

    if content:
        return ExtractResult(ok=True, content=content, method_used=method)
    return ExtractResult(
        ok=False,
        message=f"“{method}” couldn't extract anything from this page. Try another method.",
    )


@st.cache_data(ttl=3600, show_spinner=False)
def extract_from_url(raw_input: str, method: str = AUTO_METHOD) -> ExtractResult:
    match = re.search(r"(https?://[^\s]+)", raw_input.strip())
    if not match:
        return ExtractResult(ok=False, message="Please enter a valid URL starting with http:// or https://")

    original_url = match.group(1)
    clean_url = normalize_url(original_url)

    if method != AUTO_METHOD:
        return run_forced_method(method, original_url, clean_url)

    # ---- Automatic chain ----
    # 1. Site-specific handlers
    if is_wikipedia_url(original_url):
        content = extract_wikipedia(original_url)
        if content:
            return ExtractResult(ok=True, content=content, method_used="Wikipedia REST API")

    if is_substack_url(original_url):
        content = extract_substack(original_url)
        if content:
            return ExtractResult(ok=True, content=content, method_used="Substack API")

    if "medium.com" in original_url or any(
        d in original_url for d in ("towardsdatascience.com", "betterprogramming.pub")
    ):
        content = extract_medium(original_url)
        if content:
            return ExtractResult(ok=True, content=content, method_used="Medium mirror")

    # 2. Live page
    status, body_text, resp = 500, "", None
    try:
        resp = requests.get(clean_url, impersonate="chrome124", timeout=15, allow_redirects=True)
        status, body_text = resp.status_code, resp.text
    except SSLError:
        return ExtractResult(ok=False, message="Site TLS error. Use the manual paste box below.")
    except Exception:
        pass

    anti_bot = ["icanhazip.com", "contentlicensing@people.inc", "captcha-delivery.com", "access denied"]
    is_blocked = status in (403, 429, 500) or any(p in body_text.lower() for p in anti_bot)

    if resp is not None and not is_blocked and body_text:
        if is_substack_url(clean_url, body_text):
            content = extract_substack(clean_url)
            if content:
                return ExtractResult(ok=True, content=content, method_used="Substack API")

        c_type = resp.headers.get("content-type", "").lower()
        c_disp = resp.headers.get("content-disposition", "").lower()
        f_url = resp.url.lower()

        if "application/pdf" in c_type or f_url.endswith(".pdf") or ".pdf" in c_disp:
            return ExtractResult(ok=True, content=extract_pdf(resp.content), method_used="PDF text")
        if (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in c_type
            or f_url.endswith(".docx") or ".docx" in c_disp
        ):
            return ExtractResult(ok=True, content=extract_docx(resp.content), method_used="Word document")
        if "application/epub+zip" in c_type or f_url.endswith(".epub") or ".epub" in c_disp:
            return ExtractResult(ok=True, content=extract_epub(resp.content), method_used="EPUB")
        if "application/rtf" in c_type or "text/rtf" in c_type or f_url.endswith(".rtf") or ".rtf" in c_disp:
            return ExtractResult(ok=True, content=extract_rtf(resp.content), method_used="RTF")
        if "text/plain" in c_type or f_url.endswith(".txt") or ".txt" in c_disp:
            return ExtractResult(ok=True, content=format_plain_text(resp.text), method_used="Plain text")

        found = extract_from_html(clean_url, body_text)
        if found:
            return ExtractResult(ok=True, content=found[0], method_used=found[1])

    # 3. Fallbacks: only reached if the live page failed, was blocked, or gave nothing usable
    snapshot = fetch_archive_org_snapshot(clean_url)
    if snapshot:
        arch_html, arch_date = snapshot
        found = extract_from_html(clean_url, arch_html)
        if found:
            return ExtractResult(
                ok=True,
                content=f"<p><em>🏛️ Snapshot ({arch_date})</em></p>{found[0]}",
                method_used=f"Internet Archive snapshot → {found[1]}",
            )

    archive_today_content = fetch_archive_today(clean_url)
    if archive_today_content:
        return ExtractResult(ok=True, content=archive_today_content, method_used="Archive.today snapshot")

    proxy_content = fetch_jina_proxy(clean_url)
    if proxy_content:
        return ExtractResult(ok=True, content=proxy_content, method_used="Jina reader proxy")

    return ExtractResult(
        ok=False,
        message="This site blocked access and no archive copy was found. Try another method below, or use manual paste.",
    )


# ---------------------------------------------------------------------------
# Reader rendering
# ---------------------------------------------------------------------------

def render_reader(
    html_content: str,
    plain_text_for_tts: str,
    font_family: str,
    font_size: int,
    theme_style: str,
    pane_height: int = 600,
):
    template_path = Path(__file__).parent / "reader_view.html"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        template = (
            "<div style='{{ theme_style }} font-family:{{ font_family }}; "
            "font-size:{{ font_size }}px; padding:16px; min-height:{{ pane_height }}px;'>"
            "{{ content }}</div>"
        )

    values = {
        "pane_height": str(pane_height),
        "font_family": font_family,
        "font_size": str(font_size),
        "theme_style": theme_style,
        "content": html_content,
        "tts_text_json": json_for_script(plain_text_for_tts),
    }
    # Single pass: substituted text is never re-scanned, so an article that
    # happens to contain a placeholder can't inject into later substitutions.
    doc = re.sub(r"\{\{ (\w+) \}\}", lambda m: values.get(m.group(1), m.group(0)), template)
    components.html(doc, height=pane_height + 70, scrolling=False)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

HISTORY_PLACEHOLDER = "-- Select from history --"


def _clear_library_view():
    st.session_state.pop("library_view", None)


def _on_new_source():
    st.session_state.pop("library_view", None)
    st.session_state["extract_method"] = AUTO_METHOD


def _apply_history_choice():
    choice = st.session_state.get("history_choice")
    if choice and choice != HISTORY_PLACEHOLDER:
        st.session_state["url_input"] = choice
        _on_new_source()
    st.session_state["history_choice"] = HISTORY_PLACEHOLDER


def _open_library_item(entry: dict):
    st.session_state["library_view"] = {
        "title": entry.get("title") or entry.get("url") or "Saved article",
        "content": entry["content"],
    }


# ---------------------------------------------------------------------------
# App
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

client = get_session_client()
cookies = CookieController() if client is not None else None

if client is not None:
    if st.session_state.pop("_do_sign_out", False):
        perform_sign_out(client, cookies)

    if current_user() is None:
        signed_in = try_auto_login(client)

        if not signed_in and not st.session_state.get("_logged_out"):
            outcome = try_restore_from_cookie(client, cookies)
            if outcome == "restored":
                signed_in = True
            elif outcome == "failed":
                try:
                    cookies.remove(COOKIE_NAME)
                except Exception:
                    pass
                st.session_state["login_notice"] = "Your saved sign-in expired. Please sign in again."
            elif outcome == "error":
                st.session_state["login_notice"] = (
                    "Couldn't reach the sign-in service. Reload to try again; "
                    "your saved sign-in has been kept."
                )
            else:
                # The cookie component loads a moment after the page does.
                waits = st.session_state.get("_cookie_waits", 0)
                if waits < 4:
                    st.session_state["_cookie_waits"] = waits + 1
                    st.caption("Loading…")
                    time.sleep(0.6)
                    st.rerun()

        if not signed_in:
            render_login(client)
            st.stop()

    if get_db() is None:
        fails = st.session_state.get("_db_fail", 0) + 1
        st.session_state.pop("auth_user", None)
        st.session_state["_db_fail"] = fails
        if fails > 2:
            st.session_state["_logged_out"] = True
        st.session_state.setdefault("login_notice", "Your session expired. Please sign in again.")
        st.rerun()

    sync_cookie(client, cookies)

st.title("📖 Clean 9:16 Reader")

flash = st.session_state.pop("_flash", None)
if flash:
    st.success(flash)

if client is None:
    st.info("💡 Supabase isn't configured, so there is no saved library. History lives in this session only.")
else:
    c_user, c_out = st.columns([4, 1])
    c_user.caption(f"Signed in as {current_user()['email']}")
    if str(secret("AUTO_LOGIN", False)).lower() != "true":
        c_out.button("Sign out", on_click=request_sign_out)

# Share-sheet / bookmarklet launch:  https://YOUR-APP/?url=<URL-ENCODED LINK>
incoming_url = st.query_params.get("url")
if incoming_url and st.session_state.get("_consumed_url_param") != incoming_url:
    st.session_state["url_input"] = incoming_url
    st.session_state["extract_method"] = AUTO_METHOD
    st.session_state.pop("library_view", None)
    st.session_state["_consumed_url_param"] = incoming_url

history_list = load_history()
if history_list:
    with st.expander("🕒 Recent URLs", expanded=False):
        st.selectbox(
            "Select a previously accessed page:",
            [HISTORY_PLACEHOLDER] + history_list,
            key="history_choice",
            on_change=_apply_history_choice,
        )
        if st.button("🗑️ Clear History"):
            clear_history()
            st.rerun()

library_items = load_library()
if library_items:
    with st.expander(f"📚 Saved Articles ({len(library_items)})", expanded=False):
        for entry in library_items:
            art_id = entry["id"]
            entry_title = entry.get("title") or entry.get("url") or "Untitled"
            c_title, c_open, c_export, c_del = st.columns([4, 1.4, 1.2, 1])
            c_title.write(entry_title)
            c_open.button("Open", key=f"open_{art_id}", on_click=_open_library_item, args=(entry,))
            with c_export.popover("⬇️"):
                render_export_buttons(entry["content"], entry_title, key_prefix=f"lib_{art_id}")
            if c_del.button("🗑️", key=f"del_{art_id}"):
                delete_from_library(art_id)
                st.rerun()

url_input = st.text_input(
    "Paste URL (Article, Recipe, OneDrive, PDF, or text):",
    key="url_input",
    placeholder="https://...",
    on_change=_on_new_source,
)

with st.expander("🛠️ Not looking right? Try another method"):
    method = st.selectbox("Extraction method", EXTRACTION_METHODS, key="extract_method")
    if st.button("🔄 Re-fetch (ignore cache)"):
        extract_from_url.clear()
        fetch_jina_proxy.clear()
        fetch_archive_org_snapshot.clear()
        fetch_archive_today.clear()
        st.rerun()

uploaded_file = st.file_uploader(
    "Or upload document:",
    type=["txt", "pdf", "docx", "epub", "rtf", "md"],
    on_change=_clear_library_view,
)

with st.expander("📋 Manual Text / Recipe Paste (Fallback)"):
    manual_text = st.text_area(
        "Paste raw text or recipe directions here:", height=150, on_change=_clear_library_view
    )

content = ""
content_title: Optional[str] = None
active_source_url: Optional[str] = None
method_used = ""
library_view = st.session_state.get("library_view")

if library_view:
    content = library_view["content"]
    content_title = library_view["title"]
    method_used = "Saved library copy"
elif manual_text.strip():
    content = format_plain_text(manual_text)
    method_used = "Manual paste"
elif url_input.strip():
    with st.spinner("Extracting & formatting..."):
        result = extract_from_url(url_input.strip(), method=method)
    if result.ok:
        content = result.content
        active_source_url = url_input.strip()
        method_used = result.method_used
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
            method_used = f"Uploaded .{ext}"
            content_title = uploaded_file.name
        except Exception as e:
            st.error(f"Error parsing file: {e}")

if content:
    # Only log history when the URL actually changes (not on every widget rerun).
    if active_source_url and st.session_state.get("_last_history_url") != active_source_url:
        save_url_to_history(active_source_url)
        st.session_state["_last_history_url"] = active_source_url

    safe_content = sanitize_html(content)
    content_id = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()[:12]
    base_title = content_title or guess_title(safe_content, fallback=active_source_url or "Article")

    st.divider()
    if method_used:
        st.caption(f"Extracted via: {method_used}")
    if library_view:
        st.button("✖ Close saved article", on_click=_clear_library_view)

    # Recipe scaler: only shown when an Ingredients list with quantities is found.
    display_content = safe_content
    _, scalable_lines = scale_recipe_html(safe_content, 2.0)
    if scalable_lines:
        factor_label = st.radio(
            "Scale recipe", list(SCALE_OPTIONS), index=1, horizontal=True, key=f"servings_{content_id}"
        )
        factor = SCALE_OPTIONS[factor_label]
        if factor != 1.0:
            display_content, _ = scale_recipe_html(safe_content, factor)
            st.caption("Quantities at the start of each ingredient line are scaled; check unusual lines.")

    word_count = len(BeautifulSoup(display_content, "html.parser").get_text(separator=" ").split())
    reading_time_min = max(1, round(word_count / 200)) if word_count > 0 else 0

    col_meta, col_copy, col_export = st.columns([3, 2, 2])
    col_meta.markdown(
        f"<p style='opacity:0.7'>⏱️ ~{reading_time_min} min • {word_count:,} words</p>",
        unsafe_allow_html=True,
    )
    with col_copy:
        with st.popover("📋 Copy"):
            st.code(to_plain_text_for_export(display_content), language=None)
    with col_export:
        with st.popover("⬇️ Export"):
            render_export_buttons(display_content, base_title, key_prefix="current")

    if client is not None and not library_view:
        if st.button("💾 Save to my library"):
            save_key = active_source_url or f"upload_{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"
            if save_to_library(save_key, base_title, safe_content, word_count):
                st.session_state["_flash"] = "Saved to your library."
                st.rerun()

    with st.expander("⚙️ Reader Controls", expanded=False):
        font_family_opt = st.selectbox("Typeface", ["Sans-Serif", "Serif", "Monospace"], key="reader_font")
        font_size_val = st.slider("Font Size (px)", min_value=14, max_value=32, value=18, step=1, key="reader_size")
        theme = st.selectbox("Color Theme", ["Light", "Sepia", "Dark"], key="reader_theme")
        pane_height_val = st.slider(
            "Reading Pane Height (px)", min_value=400, max_value=1000, value=600, step=50, key="reader_pane_height"
        )
        st.caption("Scroll, read-aloud, voice and sleep-timer controls are in the toolbar above the article.")

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

    render_reader(
        html_content=display_content,
        plain_text_for_tts=html_to_tts_text(display_content),
        font_family=font_map[font_family_opt],
        font_size=font_size_val,
        theme_style=theme_map[theme],
        pane_height=pane_height_val,
    )
