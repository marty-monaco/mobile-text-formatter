"""
Clean Reader — mobile-friendly reader for articles, recipes and documents.

- Supabase email/password sign-in, per-user library and history (row-level security)
- One Supabase client per browser session; optional "keep me signed in" cookie
- Safe fetching: no internal addresses, pinned DNS, checked redirects, size limits
- Live page first, then Internet Archive, then optional third-party fallbacks
- Recipe serving scaler (thirds, ranges, links preserved; saves the original quantities)
- Reader pane with auto-scroll and read-aloud
"""

import hashlib
import html
import io
import ipaddress
import json
import math
import re
import socket
import time
import zipfile
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import bleach
import ebooklib
import extruct
import markdown
import streamlit as st
import streamlit.components.v1 as components
import trafilatura
from bs4 import BeautifulSoup
from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import RequestException, SSLError, Timeout
from docx import Document
from ebooklib import epub
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text
from supabase import Client, create_client

import recipe_scrapers

try:
    from curl_cffi import CurlOpt
except ImportError:  # older curl_cffi
    from curl_cffi.const import CurlOpt

try:
    from supabase.client import ClientOptions
except ImportError:
    ClientOptions = None

try:
    from streamlit_cookies_controller import CookieController
except ImportError:
    CookieController = None

st.set_page_config(page_title="Clean Reader", page_icon="📖", layout="centered")


def secret(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


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
# Removed together with their contents so script/style text can't show up as page text.
DROP_WHOLE = [
    "script", "style", "noscript", "template", "iframe", "object", "embed", "svg",
    "form", "button", "select", "textarea", "input", "head", "link", "meta",
]


def sanitize_html(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(DROP_WHOLE):
        tag.decompose()
    cleaned = bleach.clean(
        str(soup),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    out = BeautifulSoup(cleaned, "html.parser")
    for a in out.find_all("a"):
        a["rel"] = "noopener noreferrer nofollow"
        if a.get("target") is None:
            a["target"] = "_blank"
    return str(out)


def json_for_script(value) -> str:
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


@dataclass
class ExtractResult:
    ok: bool
    content: str = ""
    message: Optional[str] = None
    method_used: str = ""


class DocumentError(Exception):
    """A document that can't be turned into readable text (scanned, locked, too big...)."""


# ---------------------------------------------------------------------------
# Safe fetching. The server fetches whatever link a visitor pastes, so it must
# never be tricked into reaching internal services (localhost, private networks,
# cloud metadata). Every request: only http/https on ports 80/443, every address
# the host resolves to must be public, the connection is pinned to the address we
# checked, redirects are followed by hand and re-checked, and bodies are capped.
# ---------------------------------------------------------------------------

ALLOWED_PORTS = {80, 443}
MAX_REDIRECTS = 5
MAX_URL_LENGTH = 2048
IMPERSONATE = "chrome124"
MAX_DOWNLOAD_BYTES = int(secret("MAX_DOWNLOAD_BYTES", 15_000_000))
MAX_PDF_PAGES = int(secret("MAX_PDF_PAGES", 300))
THIRD_PARTY_FALLBACKS = str(secret("ENABLE_THIRD_PARTY_FALLBACKS", True)).lower() != "false"

_EXTRA_BLOCKED = [
    ipaddress.ip_network(n)
    for n in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32", "100.64.0.0/10")
]


class BlockedURL(Exception):
    pass


class FetchError(Exception):
    pass


class TLSError(FetchError):
    pass


class TooLarge(FetchError):
    pass


@dataclass
class Fetched:
    status_code: int
    url: str
    headers: dict
    content: bytes

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "").lower()
        charset = ctype.split("charset=")[-1].split(";")[0].strip(" \"'") if "charset=" in ctype else None
        for enc in (charset, "utf-8", "cp1252"):
            if not enc:
                continue
            try:
                return self.content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


def _is_public_ip(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED if ip.version == net.version)


def check_syntax(url: str):
    """Network-free checks. Returns (host, port) or raises BlockedURL."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise BlockedURL("That link is empty or too long.")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise BlockedURL("That link isn't a valid web address.")
    if parts.scheme.lower() not in ("http", "https"):
        raise BlockedURL("Only http and https links are supported.")
    if parts.username or parts.password:
        raise BlockedURL("Links with a username or password aren't supported.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("That link has no website address.")
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    if ALLOWED_PORTS is not None and port not in ALLOWED_PORTS:
        raise BlockedURL("Only standard web ports (80 and 443) are allowed.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise BlockedURL("That address isn't reachable from the public internet.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not _is_public_ip(literal):
        raise BlockedURL("That address isn't reachable from the public internet.")
    return host, port


def validate_url(url: str):
    host, port = check_syntax(url)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError("Couldn't find that website.")
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not _is_public_ip(ip):
            raise BlockedURL("That address isn't reachable from the public internet.")
        if str(ip) not in ips:
            ips.append(str(ip))
    if not ips:
        raise FetchError("Couldn't find that website.")
    ips.sort(key=lambda s: ":" in s)  # prefer IPv4
    return host, port, ips


def safe_get(url: str, *, headers: Optional[dict] = None, timeout: int = 15, max_bytes: Optional[int] = None) -> Fetched:
    limit = max_bytes or MAX_DOWNLOAD_BYTES
    current = url.strip()
    for _ in range(MAX_REDIRECTS + 1):
        host, port, ips = validate_url(current)
        addr = f"[{ips[0]}]" if ":" in ips[0] else ips[0]
        session = curl.Session(curl_options={CurlOpt.RESOLVE: [f"{host}:{port}:{addr}"]})
        response = None
        try:
            response = session.get(
                current, headers=headers, timeout=timeout, allow_redirects=False,
                impersonate=IMPERSONATE, stream=True,
            )
            lowered = {k.lower(): v for k, v in response.headers.items()}
            declared = lowered.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise TooLarge("That page is too large to process.")
            body = bytearray()
            for chunk in response.iter_content():
                body.extend(chunk)
                if len(body) > limit:
                    raise TooLarge("That page is too large to process.")
            status = response.status_code
        except (TooLarge, BlockedURL):
            raise
        except SSLError:
            raise TLSError("That site's security certificate couldn't be verified.")
        except Timeout:
            raise FetchError("The site took too long to respond.")
        except RequestException as e:
            raise FetchError(f"Couldn't fetch that page ({type(e).__name__}).")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            try:
                session.close()
            except Exception:
                pass
        location = lowered.get("location")
        if status in (301, 302, 303, 307, 308) and location:
            current = urljoin(current, location)
            continue
        return Fetched(status_code=status, url=current, headers=lowered, content=bytes(body))
    raise FetchError("That link redirects too many times.")


def soft_get(url: str, **kw) -> Optional[Fetched]:
    """For helper services: any failure just means 'not available'."""
    try:
        return safe_get(url, **kw)
    except FetchError:
        return None
    except BlockedURL:
        return None


# ---------------------------------------------------------------------------
# Supabase: per-session client + sign-in (with "keep me signed in" cookie)
# ---------------------------------------------------------------------------

COOKIE_NAME = "clean_reader_rt"
COOKIE_DAYS = 30


def get_session_client() -> Optional[Client]:
    """One client per browser session. Never cache this with cache_resource: a shared
    client would share one person's login with every other visitor.

    Background auto-refresh is off on purpose. Supabase refresh tokens are single-use, so
    an old session refreshing on its own timer would burn the token held in the cookie."""
    url = secret("SUPABASE_URL")
    key = secret("SUPABASE_KEY")
    if not url or not key:
        return None
    if "sb_client" not in st.session_state:
        try:
            if ClientOptions is not None:
                st.session_state["sb_client"] = create_client(url, key, options=ClientOptions(auto_refresh_token=False))
            else:
                st.session_state["sb_client"] = create_client(url, key)
        except Exception:
            return None
    return st.session_state["sb_client"]


def current_user() -> Optional[dict]:
    return st.session_state.get("auth_user")


def get_db() -> Optional[Client]:
    """The authenticated client (refreshing the token if needed), or None."""
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
    st.session_state["auth_user"] = {"id": res.user.id, "email": res.user.email}
    st.session_state["remember_me"] = remember
    st.session_state.pop("_logged_out", None)
    st.session_state.pop("_db_fail", None)


def sync_cookie(client: Client, cookies):
    """Keep the cookie holding the newest refresh token (Supabase rotates them)."""
    if cookies is None or not st.session_state.get("remember_me"):
        return
    try:
        session = client.auth.get_session()
    except Exception:
        return
    if session is None or not session.refresh_token:
        return
    if st.session_state.get("_cookie_rt") != session.refresh_token:
        try:
            cookies.set(COOKIE_NAME, session.refresh_token, max_age=COOKIE_DAYS * 86400, secure=True, same_site="lax")
            st.session_state["_cookie_rt"] = session.refresh_token
        except Exception:
            pass


def try_restore_from_cookie(client: Client, cookies) -> str:
    """'restored', 'none' (no cookie yet), 'failed' (token rejected) or 'error' (temporary)."""
    try:
        token = cookies.get(COOKIE_NAME)
    except Exception:  # component hasn't reported back yet
        return "none"
    if not token:
        return "none"
    try:
        res = client.auth.refresh_session(token)
    except Exception as e:
        status = getattr(e, "status", None)
        return "failed" if status in (400, 401, 403, 422) else "error"
    if res and res.session and res.user:
        start_session(res, remember=True)
        return "restored"
    return "failed"


def request_sign_out():
    st.session_state["_do_sign_out"] = True


def perform_sign_out(client: Client, cookies):
    try:
        client.auth.sign_out({"scope": "local"})
    except Exception:
        try:
            client.auth.sign_out()
        except Exception:
            pass
    if cookies is not None:
        try:
            cookies.remove(COOKIE_NAME)
        except Exception:
            pass
    for key in list(st.session_state.keys()):
        if key != "sb_client":
            del st.session_state[key]
    st.session_state["_logged_out"] = True


def try_auto_login(client: Client) -> bool:
    """If AUTO_LOGIN is on in secrets, sign in with the stored account."""
    if str(secret("AUTO_LOGIN", False)).lower() != "true":
        return False
    email, password = secret("APP_EMAIL"), secret("APP_PASSWORD")
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
        remember = st.checkbox("Keep me signed in on this device", value=True, disabled=CookieController is None)
        if allow_signup:
            c1, c2 = st.columns(2)
            do_sign_in = c1.form_submit_button("Sign in", use_container_width=True)
            do_sign_up = c2.form_submit_button("Create account", use_container_width=True)
        else:
            do_sign_in = st.form_submit_button("Sign in", use_container_width=True)
            do_sign_up = False
    if CookieController is None:
        st.caption("Install streamlit-cookies-controller to stay signed in between visits.")

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
        start_session(res, remember and CookieController is not None)
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
                db.table("read_history").select("url")
                .eq("user_id", current_user()["id"])
                .order("accessed_at", desc=True).limit(50).execute()
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
            db.table("read_history").insert({"url": url, "user_id": current_user()["id"]}).execute()
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
            db.table("saved_articles").select("*")
            .eq("user_id", current_user()["id"]).order("saved_at", desc=True).execute()
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
            {"user_id": current_user()["id"], "url": url, "title": title or url, "content": content, "word_count": word_count},
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
        db.table("saved_articles").delete().eq("id", article_id).eq("user_id", current_user()["id"]).execute()
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


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def format_plain_text(raw_text: str) -> str:
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    for block in re.split(r"\n\s*\n+", text):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if any(len(l) < 35 for l in lines) and len(lines) > 1:
            joined = "<br>".join(html.escape(l) for l in lines)
        else:
            joined = html.escape(" ".join(lines))
        paragraphs.append(f"<p>{joined}</p>")
    return "".join(paragraphs)


# ---------------------------------------------------------------------------
# Document formats (with limits so a hostile file can't exhaust the server)
# ---------------------------------------------------------------------------

def _check_zip(data: bytes, limit: int = 100_000_000):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > limit:
                raise DocumentError("That document expands to too much data.")
    except zipfile.BadZipFile:
        raise DocumentError("That document couldn't be read.")


def extract_pdf(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("That PDF is password protected.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentError(f"That PDF has more than {MAX_PDF_PAGES} pages.")
        text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("That PDF couldn't be read.")
    if not text.strip():
        raise DocumentError("This PDF looks like a scanned image, so there's no readable text to extract.")
    return format_plain_text(text)


def extract_docx(file_bytes: bytes) -> str:
    _check_zip(file_bytes)
    try:
        doc = Document(io.BytesIO(file_bytes))
        return format_plain_text("\n\n".join(p.text for p in doc.paragraphs if p.text.strip()))
    except Exception:
        raise DocumentError("That Word document couldn't be read.")


def extract_rtf(file_bytes: bytes) -> str:
    try:
        return format_plain_text(rtf_to_text(decode_bytes(file_bytes)))
    except Exception:
        raise DocumentError("That RTF file couldn't be read.")


def extract_epub(file_bytes: bytes) -> str:
    _check_zip(file_bytes)
    try:
        book = epub.read_epub(io.BytesIO(file_bytes))
        parts = []
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                text = BeautifulSoup(item.get_content(), "html.parser").get_text("\n")
                if text.strip():
                    parts.append(text)
        return format_plain_text("\n\n".join(parts))
    except Exception:
        raise DocumentError("That e-book couldn't be read.")


def extract_markdown(file_bytes: bytes) -> str:
    return sanitize_html(markdown.markdown(decode_bytes(file_bytes)))


# ---------------------------------------------------------------------------
# Recipe scaling
# ---------------------------------------------------------------------------

UNICODE_FRACTIONS = {
    "½": 1 / 2, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 1 / 4, "¾": 3 / 4,
    "⅕": 1 / 5, "⅖": 2 / 5, "⅗": 3 / 5, "⅘": 4 / 5,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 1 / 8, "⅜": 3 / 8, "⅝": 5 / 8, "⅞": 7 / 8,
}
_FR = "".join(UNICODE_FRACTIONS)
_NUM = rf"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?\s*[{_FR}]|\d+(?:\.\d+)?|[{_FR}])"
# A leading quantity or range ("1 1/2", "½", "2-3", "2 to 3"). The trailing lookahead
# leaves "1-inch piece" and "1/2" fragments alone.
_QTY_RE = re.compile(rf"^\s*({_NUM})(?:\s*(?:-|–|—|to)\s*({_NUM}))?(?![\d/]|[-–]\s*[A-Za-z])")
_FRAC_POINTS = [0.0, 1 / 8, 1 / 6, 1 / 4, 1 / 3, 3 / 8, 1 / 2, 5 / 8, 2 / 3, 3 / 4, 5 / 6, 7 / 8, 1.0]
_FRAC_GLYPH = {1 / 8: "⅛", 1 / 6: "⅙", 5 / 6: "⅚", 1 / 4: "¼", 1 / 3: "⅓", 3 / 8: "⅜", 1 / 2: "½", 5 / 8: "⅝", 2 / 3: "⅔", 3 / 4: "¾", 7 / 8: "⅞"}
SCALE_OPTIONS = {"0.5x (Half)": 0.5, "1x (Normal)": 1.0, "2x (Double)": 2.0, "3x (Triple)": 3.0}
_RECIPE_HEADS = ["h1", "h2", "h3", "h4", "h5", "h6"]
_ING_RE = re.compile(r"^\s*ingredients?\b", re.I)
_ING_LABEL_RE = re.compile(r"^\s*ingredients?\s*:?\s*$", re.I)
_STOP_RE = re.compile(r"^\s*(directions?|instructions?|method|steps?|preparation|notes?|nutrition|equipment|tips?)\b", re.I)
_STOP_LABEL_RE = re.compile(r"^\s*(directions?|instructions?|method|steps?|preparation|notes?|nutrition|equipment|tips?)\s*:?\s*$", re.I)


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
    nearest = min(_FRAC_POINTS, key=lambda f: abs(f - (x - whole)))
    if nearest == 1.0:
        whole, nearest = whole + 1, 0.0
    if whole == 0 and nearest == 0.0:
        return "1/16" if x >= 0.03 else f"{x:.2f}"
    glyph = _FRAC_GLYPH.get(nearest, "")
    if whole and glyph:
        return f"{whole} {glyph}"
    return glyph or str(whole)


def scale_ingredient_text(text: str, factor: float) -> Optional[str]:
    """The scaled text, or None if the line doesn't start with a quantity."""
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
    """Scales the lists under an 'Ingredients' heading (until Directions/Method/Notes).
    Returns (new_html, number_of_lines_scaled). Links and formatting inside lines are kept."""
    soup = BeautifulSoup(content_html, "html.parser")
    count = 0
    done = set()
    scan = _RECIPE_HEADS + ["ul", "ol", "p", "strong", "b"]
    for anchor in soup.find_all(_RECIPE_HEADS + ["p", "strong", "b"]):
        text = anchor.get_text(" ", strip=True)
        is_ingredients = bool(_ING_RE.match(text)) if anchor.name in _RECIPE_HEADS else bool(_ING_LABEL_RE.match(text))
        if not is_ingredients:
            continue
        lists_seen = 0
        for el in anchor.find_all_next(scan):
            el_text = el.get_text(" ", strip=True) if el.name not in ("ul", "ol") else ""
            if el.name in _RECIPE_HEADS and _STOP_RE.match(el_text):
                break
            if el.name in ("p", "strong", "b") and _STOP_LABEL_RE.match(el_text):
                break
            if el.name not in ("ul", "ol"):
                continue
            if id(el) in done or any(id(p) in done for p in el.parents):
                continue
            done.add(id(el))
            lists_seen += 1
            if lists_seen > 6:
                break
            for li in el.find_all("li"):
                node = next((s for s in li.find_all(string=True) if s.strip()), None)
                if node is None:
                    continue
                updated = scale_ingredient_text(str(node), factor)
                if updated is not None:
                    node.replace_with(updated)
                    count += 1
    return str(soup), count


# ---------------------------------------------------------------------------
# Recipe extraction
# ---------------------------------------------------------------------------

def extract_recipe_via_scraper(url: str, html_str: Optional[str] = None) -> Optional[str]:
    """Parses recipe HTML we already fetched. Never fetches anything itself."""
    if not html_str:
        return None
    try:
        try:
            scraper = recipe_scrapers.scrape_html(html_str, org_url=url, supported_only=False)
        except TypeError:  # older recipe_scrapers
            scraper = recipe_scrapers.scrape_html(html=html_str, org_url=url)
    except Exception:
        return None

    def safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    title = safe(scraper.title, "") or "Recipe"
    ingredients = safe(scraper.ingredients, []) or []
    steps = [s.strip() for s in (safe(scraper.instructions, "") or "").split("\n") if s.strip()]
    if not ingredients and not steps:
        return None
    yields, total_time = safe(scraper.yields), safe(scraper.total_time)

    out = [f"<h2>{html.escape(title)}</h2>"]
    meta = []
    if yields:
        meta.append(f"Yield: {html.escape(str(yields))}")
    if total_time:
        meta.append(f"Total Time: {html.escape(str(total_time))} mins")
    if meta:
        out.append(f"<p class='meta-chip'><em>{' | '.join(meta)}</em></p>")
    if ingredients:
        out.append("<h3>Ingredients</h3><ul>" + "".join(f"<li>{html.escape(str(i))}</li>" for i in ingredients) + "</ul>")
    if steps:
        out.append("<h3>Directions</h3><ol>" + "".join(f"<li>{html.escape(s)}</li>" for s in steps) + "</ol>")
    return "".join(out)


def extract_recipe_schema(html_content: str):
    try:
        data = extruct.extract(html_content, syntaxes=["json-ld"])
    except Exception:
        return None

    def walk(node):
        if isinstance(node, dict):
            types = node.get("@type", [])
            types = [types] if isinstance(types, str) else [t for t in types if isinstance(t, str)]
            if any(t.lower() == "recipe" for t in types):
                return node
            for v in node.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = walk(v)
                if found:
                    return found
        return None

    return walk(data.get("json-ld", []))


def format_recipe_output(recipe: dict) -> str:
    title = str(recipe.get("name", "Recipe"))
    ingredients = recipe.get("recipeIngredient", []) or []
    steps = []
    for step in recipe.get("recipeInstructions", []) or []:
        if isinstance(step, str):
            steps.append(step)
        elif isinstance(step, dict):
            if "text" in step:
                steps.append(str(step["text"]))
            for sub in step.get("itemListElement", []) or []:  # HowToSection
                if isinstance(sub, dict) and "text" in sub:
                    steps.append(str(sub["text"]))
    out = [f"<h2>{html.escape(title)}</h2>"]
    if recipe.get("description"):
        out.append(f"<p><em>{html.escape(str(recipe['description']))}</em></p>")
    if ingredients:
        out.append("<h3>Ingredients</h3><ul>" + "".join(f"<li>{html.escape(str(i))}</li>" for i in ingredients) + "</ul>")
    if steps:
        out.append("<h3>Directions</h3><ol>" + "".join(f"<li>{html.escape(s)}</li>" for s in steps) + "</ol>")
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
        target = soup.find(lambda tag: tag.get("id") and "recipe" in tag.get("id", "").lower()) or soup.find(
            lambda tag: tag.get("class")
            and any(any(m in c.lower() for m in ("recipe-card", "tasty-recipe", "wprm-recipe")) for c in tag.get("class", []))
        )
    if not target or len(target.find_all("li")) < 2:
        return None
    return str(target)


# ---------------------------------------------------------------------------
# Substack / Medium
# ---------------------------------------------------------------------------

def is_substack_url(url: str, html_text: str = "") -> bool:
    if (urlparse(url).hostname or "").lower().endswith("substack.com"):
        return True
    return any(m in html_text for m in ("substackcdn.com", "substack-custom-domains"))


def extract_substack(url: str) -> Optional[str]:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if "p" not in parts or parts.index("p") + 1 >= len(parts):
        return None
    slug = parts[parts.index("p") + 1]
    resp = soft_get(
        f"{parsed.scheme}://{parsed.netloc}/api/v1/posts/{slug}", headers={"Accept": "application/json"}, timeout=12
    )
    if not resp or resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    body_html = data.get("body_html", "")
    if not body_html:
        return None
    header = f"<h2>{html.escape(str(data.get('title', '')))}</h2>"
    if data.get("subtitle"):
        header += f"<p><em>{html.escape(str(data['subtitle']))}</em></p>"
    return header + sanitize_html(body_html)


def extract_medium(url: str) -> Optional[str]:
    resp = soft_get(f"https://freedium.cfd/{url}", timeout=15)
    if resp and resp.status_code == 200 and len(resp.text) > 500:
        soup = BeautifulSoup(resp.text, "html.parser")
        article = soup.find("article") or soup.find("main")
        if article:
            return sanitize_html(str(article))
        body = trafilatura.extract(resp.text, favor_recall=True)
        if body:
            return format_plain_text(body)
    proxy = fetch_jina_proxy(url)
    return proxy if proxy and len(proxy) > 300 else None


# ---------------------------------------------------------------------------
# Fallback gateways
# ---------------------------------------------------------------------------

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_archive_org_snapshot(target_url: str) -> Optional[Tuple[str, str]]:
    meta = soft_get(f"https://archive.org/wayback/available?url={target_url}", timeout=10)
    if not meta or meta.status_code != 200:
        return None
    try:
        closest = meta.json().get("archived_snapshots", {}).get("closest")
    except ValueError:
        return None
    if not closest or not closest.get("available"):
        return None
    raw_url, timestamp = str(closest.get("url", "")), str(closest.get("timestamp", ""))
    host = (urlparse(raw_url).hostname or "").lower()
    if host != "web.archive.org" and not host.endswith(".archive.org"):
        return None
    snapshot_url = re.sub(r"/web/(\d{14})/", r"/web/\1id_/", raw_url)
    if "id_/" not in snapshot_url:
        snapshot_url = raw_url.replace(f"/web/{timestamp}/", f"/web/{timestamp}id_/")
    resp = soft_get(snapshot_url, timeout=15)
    if resp and resp.status_code == 200 and len(resp.text.strip()) > 300:
        date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}" if len(timestamp) >= 8 else "Archived"
        return resp.text, date
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_jina_proxy(target_url: str) -> str:
    if not THIRD_PARTY_FALLBACKS:
        return ""
    resp = soft_get(f"https://r.jina.ai/{target_url}", timeout=20)
    if resp and resp.status_code == 200 and resp.text.strip():
        return sanitize_html(markdown.markdown(resp.text))
    return ""


# ---------------------------------------------------------------------------
# Core URL extractor
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    if not any(d in parts.netloc for d in ("share.google", "bit.ly", "onedrive.live.com", "1drv.ms")):
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if any(d in parts.netloc for d in ("onedrive.live.com", "1drv.ms")) and "download=1" not in url:
        url += ("&" if "?" in url else "?") + "download=1"
    return url


def archive_content(clean_url: str, arch_html: str, arch_date: str) -> Optional[str]:
    stamp = f"<p class='meta-chip'>🏛️ Snapshot ({arch_date})</p>"
    recipe = extract_recipe_via_scraper(clean_url, html_str=arch_html)
    if recipe:
        return stamp + recipe
    schema = extract_recipe_schema(arch_html)
    if schema:
        return stamp + format_recipe_output(schema)
    body = trafilatura.extract(arch_html, favor_recall=True)
    return stamp + format_plain_text(body) if body else None


@st.cache_data(ttl=3600, show_spinner=False)
def extract_from_url(raw_input: str, prefer_wayback: bool = False) -> ExtractResult:
    match = re.search(r"(https?://[^\s]+)", raw_input.strip())
    if not match:
        return ExtractResult(ok=False, message="Please enter a valid URL starting with http:// or https://")
    original_url = match.group(1)
    try:
        check_syntax(original_url)
    except BlockedURL as e:
        return ExtractResult(ok=False, message=str(e))
    clean_url = normalize_url(original_url)
    host = (urlparse(original_url).hostname or "").lower()

    if is_substack_url(original_url):
        content = extract_substack(original_url)
        if content:
            return ExtractResult(ok=True, content=content, method_used="Substack API")

    if THIRD_PARTY_FALLBACKS and (host.endswith("medium.com") or host in ("towardsdatascience.com", "betterprogramming.pub")):
        content = extract_medium(original_url)
        if content:
            return ExtractResult(ok=True, content=content, method_used="Medium mirror")

    if prefer_wayback:
        snap = fetch_archive_org_snapshot(clean_url)
        if snap:
            content = archive_content(clean_url, *snap)
            if content:
                return ExtractResult(ok=True, content=content, method_used="Internet Archive snapshot")

    # Live page first: it is the best source and the only one that sees recipes properly.
    resp = None
    try:
        resp = safe_get(clean_url, timeout=15)
    except BlockedURL as e:
        return ExtractResult(ok=False, message=str(e))
    except TooLarge as e:
        return ExtractResult(ok=False, message=str(e))
    except TLSError:
        return ExtractResult(ok=False, message="Site TLS error. Use the manual paste box below.")
    except FetchError:
        resp = None

    if resp is not None:
        ctype = resp.headers.get("content-type", "").lower()
        disp = resp.headers.get("content-disposition", "").lower()
        f_url = resp.url.lower()
        is_pdf = "application/pdf" in ctype or f_url.endswith(".pdf") or ".pdf" in disp
        is_docx = "wordprocessingml.document" in ctype or f_url.endswith(".docx") or ".docx" in disp
        is_epub = "application/epub+zip" in ctype or f_url.endswith(".epub") or ".epub" in disp
        binary = is_pdf or is_docx or is_epub
        body_text = "" if binary else resp.text
        anti_bot = ["icanhazip.com", "contentlicensing@people.inc", "captcha-delivery.com", "access denied"]
        blocked = resp.status_code in (403, 429, 500) or any(p in body_text.lower() for p in anti_bot)

        if not blocked and resp.content:
            try:
                if is_pdf:
                    return ExtractResult(ok=True, content=extract_pdf(resp.content), method_used="PDF text")
                if is_docx:
                    return ExtractResult(ok=True, content=extract_docx(resp.content), method_used="Word document")
                if is_epub:
                    return ExtractResult(ok=True, content=extract_epub(resp.content), method_used="EPUB")
                if "rtf" in ctype or f_url.endswith(".rtf") or ".rtf" in disp:
                    return ExtractResult(ok=True, content=extract_rtf(resp.content), method_used="RTF")
            except DocumentError as e:
                return ExtractResult(ok=False, message=str(e))
            if "text/plain" in ctype or f_url.endswith(".txt") or ".txt" in disp:
                return ExtractResult(ok=True, content=format_plain_text(resp.text), method_used="Plain text")

            if is_substack_url(clean_url, body_text):
                content = extract_substack(clean_url)
                if content:
                    return ExtractResult(ok=True, content=content, method_used="Substack API")

            recipe = extract_recipe_via_scraper(clean_url, html_str=body_text)
            if recipe:
                return ExtractResult(ok=True, content=recipe, method_used="Recipe scraper")
            schema = extract_recipe_schema(body_text)
            if schema:
                return ExtractResult(ok=True, content=format_recipe_output(schema), method_used="Recipe schema (JSON-LD)")
            section = find_recipe_section_by_anchor(body_text)
            if section:
                return ExtractResult(ok=True, content=sanitize_html(section), method_used="Jump-to-recipe section")
            body = trafilatura.extract(body_text, include_comments=False) or trafilatura.extract(body_text, favor_recall=True)
            if body:
                return ExtractResult(ok=True, content=format_plain_text(body), method_used="Article text")

    snap = fetch_archive_org_snapshot(clean_url)
    if snap:
        content = archive_content(clean_url, *snap)
        if content:
            return ExtractResult(ok=True, content=content, method_used="Internet Archive snapshot")

    proxy = fetch_jina_proxy(clean_url)
    if proxy:
        return ExtractResult(ok=True, content=proxy, method_used="Jina reader proxy")

    return ExtractResult(ok=False, message="This site blocked access and no archive copy was found. Use the manual paste box below.")


# ---------------------------------------------------------------------------
# Reader rendering (auto-scroll + read aloud)
# ---------------------------------------------------------------------------

READER_TEMPLATE = """
<style>
  *, *::before, *::after { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system, sans-serif; }
  #progress-container { position: fixed; top: 0; left: 0; width: 100%; height: 5px; background: rgba(128,128,128,0.2); z-index: 999; }
  #progress-bar { width: 0%; height: 100%; background-color: #ff4b4b; transition: width 0.1s ease-out; }
  #toolbar { display: flex; flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch; align-items: center; gap: 6px; padding: 8px 8px 6px 8px; background: #f2f2f2; border-bottom: 1px solid #ddd; margin-top: 5px; }
  .ctrl-btn, .speed-btn { flex-shrink: 0; border: 1px solid #ccc; background: #fff; color: #222; border-radius: 999px; padding: 6px 12px; font-size: 13px; cursor: pointer; white-space: nowrap; }
  .speed-btn.active { background: #ff4b4b; color: #fff; border-color: #ff4b4b; }
  .speed-group { display: flex; flex-shrink: 0; gap: 4px; }
  #reader-scroll { height: __PANE_HEIGHT__px; overflow-y: auto; -webkit-overflow-scrolling: touch; }
  .reader-frame { font-family: __FONT_FAMILY__; font-size: __FONT_SIZE__px; __THEME_STYLE__ padding: 1.25rem 1rem 3rem 1rem; word-break: break-word; }
  .reader-frame p { margin-bottom: 1.35em; line-height: 1.8; }
  .reader-frame img { max-width: 100%; height: auto; }
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
<div id="reader-scroll"><div class="reader-frame">__HTML_CONTENT__</div></div>
<script>
  (function() {
    const scrollEl = document.getElementById('reader-scroll');
    const bar = document.getElementById('progress-bar');
    const scrollToggleBtn = document.getElementById('scrollToggleBtn');
    const ttsBtn = document.getElementById('ttsBtn');
    const speedBtns = document.querySelectorAll('.speed-btn');

    function updateProgress() {
      const total = scrollEl.scrollHeight - scrollEl.clientHeight;
      if (total > 0) { bar.style.width = (scrollEl.scrollTop / total * 100) + '%'; }
    }
    scrollEl.addEventListener('scroll', updateProgress);
    updateProgress();

    let scrollTimer = null, scrollSpeedMs = 40, scrollActive = false;
    function startScrolling() {
      if (scrollTimer) clearInterval(scrollTimer);
      scrollTimer = setInterval(function() {
        if (scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 2) { setScrollActive(false); return; }
        scrollEl.scrollTop += 1;
      }, scrollSpeedMs);
    }
    function stopScrolling() { if (scrollTimer) { clearInterval(scrollTimer); scrollTimer = null; } }
    function setScrollActive(active) {
      scrollActive = active;
      if (active) { startScrolling(); scrollToggleBtn.textContent = '⏸ Pause Scroll'; }
      else { stopScrolling(); scrollToggleBtn.textContent = '▶️ Start Scroll'; }
    }
    scrollToggleBtn.addEventListener('click', function() { setScrollActive(!scrollActive); });
    speedBtns.forEach(function(btn) {
      btn.addEventListener('click', function() {
        speedBtns.forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        scrollSpeedMs = parseInt(btn.dataset.ms, 10);
        if (scrollActive) startScrolling();
      });
    });

    const synth = ('speechSynthesis' in window) ? window.speechSynthesis : null;
    const ttsFullText = __TTS_TEXT_JSON__;
    let ttsChunks = [], ttsIndex = 0, ttsSpeaking = false;
    function chunkText(text) {
      if (!text) return [];
      const sentences = text.match(/[^.!?]+[.!?]*/g) || [text];
      const chunks = []; let current = '';
      for (let i = 0; i < sentences.length; i++) {
        const s = sentences[i];
        if ((current + s).length > 180 && current) { chunks.push(current.trim()); current = s; }
        else { current += s; }
      }
      if (current.trim()) chunks.push(current.trim());
      return chunks;
    }
    function speakNext() {
      if (!synth || ttsIndex >= ttsChunks.length) { ttsSpeaking = false; ttsBtn.textContent = '🔊 Read Aloud'; return; }
      const utter = new SpeechSynthesisUtterance(ttsChunks[ttsIndex]);
      utter.onend = function() { ttsIndex += 1; speakNext(); };
      utter.onerror = function() { ttsSpeaking = false; ttsBtn.textContent = '🔊 Read Aloud'; };
      synth.speak(utter);
    }
    if (!synth) { ttsBtn.disabled = true; ttsBtn.textContent = '🔇 Audio Unavailable'; }
    else {
      ttsBtn.addEventListener('click', function() {
        if (!ttsSpeaking) {
          if (synth.paused) { synth.resume(); }
          else { ttsChunks = chunkText(ttsFullText); ttsIndex = 0; synth.cancel(); speakNext(); }
          ttsSpeaking = true; ttsBtn.textContent = '⏸ Pause Reading';
        } else { synth.pause(); ttsSpeaking = false; ttsBtn.textContent = '▶️ Resume Reading'; }
      });
    }
  })();
</script>
"""


def render_reader(html_content: str, plain_text_for_tts: str, font_family: str, font_size: int, theme_style: str, pane_height: int = 600):
    values = {
        "PANE_HEIGHT": str(pane_height),
        "FONT_FAMILY": font_family,
        "FONT_SIZE": str(font_size),
        "THEME_STYLE": theme_style,
        "HTML_CONTENT": html_content,
        "TTS_TEXT_JSON": json_for_script(plain_text_for_tts),
    }
    # One pass: substituted text is never scanned again, so article text that happens to
    # contain a placeholder can't be injected into the script or another slot.
    doc = re.sub(r"__([A-Z_]+)__", lambda m: values.get(m.group(1), m.group(0)), READER_TEMPLATE)
    # st.iframe replaces components.html (deprecated, removal date has passed); older Streamlit lacks it.
    if hasattr(st, "iframe"):
        st.iframe(doc, height=pane_height + 70)
    else:
        components.html(doc, height=pane_height + 70, scrolling=False)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

HISTORY_PLACEHOLDER = "-- Select from history --"


def _clear_library_view():
    st.session_state.pop("library_view", None)


def _apply_history_choice():
    choice = st.session_state.get("history_choice")
    if choice and choice != HISTORY_PLACEHOLDER:
        st.session_state["url_input"] = choice
        st.session_state.pop("library_view", None)
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
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <style>
    .block-container { padding-top: 1rem; padding-bottom: 3rem; padding-left: 0.85rem; padding-right: 0.85rem; max-width: 620px; }
    .meta-chip { font-size: 0.82rem; color: #888888; margin-bottom: 0.75rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

client = get_session_client()
cookies = CookieController() if (client is not None and CookieController is not None) else None

if client is not None:
    if st.session_state.pop("_do_sign_out", False):
        perform_sign_out(client, cookies)

    if current_user() is None:
        signed_in = try_auto_login(client)

        if not signed_in and not st.session_state.get("_logged_out") and cookies is not None:
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
                    "Couldn't reach the sign-in service. Reload to try again; your saved sign-in has been kept."
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
            base = safe_filename(entry_title)
            col_title, col_open, col_txt, col_html, col_del = st.columns([4, 1.3, 1, 1, 1])
            col_title.write(entry_title)
            col_open.button("Open", key=f"open_{art_id}", on_click=_open_library_item, args=(entry,))
            col_txt.download_button(
                "⬇️ .txt", data=to_plain_text_for_export(entry["content"]),
                file_name=f"{base}.txt", mime="text/plain", key=f"dl_txt_{art_id}",
            )
            col_html.download_button(
                "⬇️ .html", data=entry["content"],
                file_name=f"{base}.html", mime="text/html", key=f"dl_html_{art_id}",
            )
            if col_del.button("🗑️", key=f"del_{art_id}"):
                delete_from_library(art_id)
                st.rerun()

col_input, col_ia_check, col_clear = st.columns([3, 1, 0.8])
with col_input:
    url_input = st.text_input(
        "Paste URL (Article, Recipe, OneDrive, PDF, or text):",
        key="url_input",
        placeholder="https://...",
        on_change=_clear_library_view,
    )
with col_ia_check:
    st.write("")
    st.write("")
    use_wayback = st.checkbox("🏛️ Wayback", help="Force retrieval from Internet Archive snapshot")
with col_clear:
    st.write("")
    st.write("")
    if st.button("🔄 Cache", help="Flush cached pages and re-fetch"):
        st.cache_data.clear()
        st.toast("Cache flushed!", icon="🧹")
        st.rerun()

uploaded_file = st.file_uploader(
    "Or upload document:", type=["txt", "pdf", "docx", "epub", "rtf", "md"], on_change=_clear_library_view
)

with st.expander("📋 Manual Text / Recipe Paste (Fallback)"):
    manual_text = st.text_area("Paste raw text or recipe directions here:", height=150, on_change=_clear_library_view)

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
        result = extract_from_url(url_input.strip(), prefer_wayback=use_wayback)
    if result.ok:
        content = result.content
        active_source_url = url_input.strip()
        method_used = result.method_used
    else:
        st.error(result.message)
elif uploaded_file:
    with st.spinner("Formatting file..."):
        try:
            data = uploaded_file.read()
            ext = uploaded_file.name.split(".")[-1].lower()
            if ext == "pdf":
                content = extract_pdf(data)
            elif ext == "docx":
                content = extract_docx(data)
            elif ext == "epub":
                content = extract_epub(data)
            elif ext == "rtf":
                content = extract_rtf(data)
            elif ext == "md":
                content = extract_markdown(data)
            else:
                content = format_plain_text(decode_bytes(data))
            method_used = f"Uploaded .{ext}"
            content_title = uploaded_file.name
        except DocumentError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Error parsing file: {e}")

if content:
    # Log history only when the URL changes, not on every widget rerun.
    if active_source_url and st.session_state.get("_last_history_url") != active_source_url:
        save_url_to_history(active_source_url)
        st.session_state["_last_history_url"] = active_source_url

    safe_content = sanitize_html(content)  # the original quantities: this is what gets saved
    content_id = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()[:12]
    base_title = content_title or guess_title(safe_content, fallback=active_source_url or "Article")

    st.divider()
    if method_used:
        st.caption(f"Extracted via: {method_used}")
    if library_view:
        st.button("✖ Close saved article", on_click=_clear_library_view)

    # Recipe scaler: shown only when an Ingredients list with quantities is found.
    display_content = safe_content
    _, scalable_lines = scale_recipe_html(safe_content, 2.0)
    if scalable_lines:
        col_scale_lbl, col_scale_btn = st.columns([2, 3])
        with col_scale_lbl:
            st.markdown("🍳 **Scale Servings:**")
        with col_scale_btn:
            scale_choice = st.radio(
                "Scale", options=list(SCALE_OPTIONS), index=1, horizontal=True,
                label_visibility="collapsed", key=f"servings_{content_id}",
            )
        factor = SCALE_OPTIONS[scale_choice]
        if factor != 1.0:
            display_content, _ = scale_recipe_html(safe_content, factor)
            st.caption("Quantities at the start of each ingredient line are scaled. Check unusual lines. Saving keeps the original amounts.")

    plain_for_stats = BeautifulSoup(display_content, "html.parser").get_text(separator=" ")
    word_count = len(plain_for_stats.split())
    reading_time_min = max(1, round(word_count / 200)) if word_count > 0 else 0

    col_meta, col_copy = st.columns([3, 2])
    with col_meta:
        st.markdown(
            f"<div class='meta-chip'>⏱️ ~{reading_time_min} min read &nbsp;•&nbsp; {word_count:,} words</div>",
            unsafe_allow_html=True,
        )
    with col_copy:
        with st.popover("📋 Copy Text"):
            st.code(to_plain_text_for_export(display_content), language=None)

    if client is not None and not library_view:
        if st.button("💾 Save to my library"):
            save_key = active_source_url or f"upload_{content_id}"
            if save_to_library(save_key, base_title, safe_content, len(BeautifulSoup(safe_content, 'html.parser').get_text(' ').split())):
                st.session_state["_flash"] = "Saved to your library."
                st.rerun()
            else:
                st.error("Could not save. Check your Supabase connection.")

    with st.expander("⚙️ Reader Controls", expanded=False):
        font_family_opt = st.selectbox("Typeface", ["Sans-Serif", "Serif", "Monospace"], key="reader_font")
        font_size_val = st.slider("Font Size (px)", min_value=14, max_value=32, value=18, step=1, key="reader_size")
        theme = st.selectbox("Color Theme", ["Light", "Sepia", "Dark"], key="reader_theme")
        pane_height_val = st.slider(
            "Reading Pane Height (px)", min_value=400, max_value=1000, value=600, step=50, key="reader_pane_height"
        )
        st.caption("Scroll and read-aloud controls are in the toolbar above the article.")

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
