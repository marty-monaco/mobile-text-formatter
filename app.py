"""
Clean Reader - mobile-friendly reader for articles, recipes and documents.

Phase-1 stabilisation build (v1 multi-tenant base + v2 features):
- Persistent login: Supabase refresh token in a cookie, rotated on every use,
  restored on cold start, proactively refreshed before the access token expires.
- Per-user isolation (RLS + explicit user_id filters). No shared DB client.
- SSRF-hardened fetching (scheme/port allowlist, public-IP check on every
  redirect hop, response size cap).
- Active document lives in session_state, so widget clicks never lose it.
- Reader UI (theme, typeface, size, scroll, TTS, TOC) runs entirely in the
  iframe and persists per-article scroll position, so tweaking it never
  triggers a Streamlit rerun.
- Recipe scaler (ranges, fractions, markup-safe) and chapter TOC.

Required secrets (.streamlit/secrets.toml):
    SUPABASE_URL = "https://xxxx.supabase.co"
    SUPABASE_KEY = "<anon public key>"          # never the service-role key
    ENABLE_THIRD_PARTY_FALLBACKS = true         # freedium / r.jina.ai (optional)

Supabase schema (run once in the SQL editor):

    create table if not exists read_history (
      id bigserial primary key,
      user_id uuid not null default auth.uid() references auth.users on delete cascade,
      url text not null,
      accessed_at timestamptz not null default now()
    );
    create table if not exists saved_articles (
      id uuid primary key default gen_random_uuid(),
      user_id uuid not null default auth.uid() references auth.users on delete cascade,
      url text not null,
      title text,
      content text,
      word_count int,
      saved_at timestamptz not null default now(),
      unique (user_id, url)
    );
    alter table read_history enable row level security;
    alter table saved_articles enable row level security;
    create policy "own history" on read_history for all
      using (auth.uid() = user_id) with check (auth.uid() = user_id);
    create policy "own articles" on saved_articles for all
      using (auth.uid() = user_id) with check (auth.uid() = user_id);
"""

import hashlib
import html
import io
import ipaddress
import json
import logging
import math
import re
import socket
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit

import bleach
import ebooklib
import extruct
import markdown
import streamlit as st
import streamlit.components.v1 as components
import trafilatura
from bs4 import BeautifulSoup, NavigableString
from curl_cffi import requests
from curl_cffi.requests.exceptions import SSLError
from docx import Document
from ebooklib import epub
from pypdf import PdfReader
from striprtf.striprtf import rtf_to_text
from supabase import Client, create_client

try:
    from recipe_scrapers import scrape_html
except ImportError:  # older recipe-scrapers
    scrape_html = None
    from recipe_scrapers import scrape_me

try:
    from streamlit_cookies_controller import CookieController
except ImportError:
    CookieController = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clean_reader")

st.set_page_config(page_title="Clean Reader", page_icon="📖", layout="centered")

# ---------------------------------------------------------------------------
# Constants & helpers
# ---------------------------------------------------------------------------

COOKIE_NAME = "cr_refresh"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
MAX_FETCH_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
ALLOWED_PORTS = (None, 80, 443, 8080, 8443)
HISTORY_LIMIT = 10
PLACEHOLDER = "-- Select from history --"

ALLOWED_TAGS = [
    "p", "br", "hr", "h1", "h2", "h3", "h4",
    "ul", "ol", "li", "em", "strong", "i", "b", "u",
    "a", "img", "blockquote", "code", "pre", "span", "div", "table",
    "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "rel", "target"],
    "h1": ["id"], "h2": ["id"], "h3": ["id"], "h4": ["id"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def secret(name: str, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def third_party_enabled() -> bool:
    return str(secret("ENABLE_THIRD_PARTY_FALLBACKS", True)).lower() not in ("0", "false", "no")


def sanitize_html(raw_html: str) -> str:
    cleaned = bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    soup = BeautifulSoup(cleaned, "html.parser")
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href.startswith("#"):
            a["target"] = "_self"
            continue
        a["rel"] = "noopener noreferrer nofollow"
        if a.get("target") is None:
            a["target"] = "_blank"
    return str(soup)


def json_for_script(value) -> str:
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class ExtractResult:
    ok: bool
    content: str = ""
    message: Optional[str] = None


class ExtractError(Exception):
    """Raised inside the cached extractor so failures are never cached."""


class UnsafeURL(Exception):
    pass


class FetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# SSRF-hardened fetching
# ---------------------------------------------------------------------------

def validate_url(url: str) -> None:
    """Reject non-http(s), odd ports, and hosts resolving to non-public IPs.

    Note: resolution happens here and again inside curl, so a DNS-rebinding
    race is still theoretically possible. For public hosting, also run behind
    an egress proxy / firewall that blocks private ranges.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURL("Only http(s) URLs are allowed.")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeURL("Invalid port.")
    if port not in ALLOWED_PORTS:
        raise UnsafeURL("That port is not allowed.")
    try:
        infos = socket.getaddrinfo(
            parts.hostname, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP
        )
    except socket.gaierror:
        raise UnsafeURL("Could not resolve that host.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeURL("That address is not allowed.")


@dataclass
class Fetched:
    status: int
    headers: dict
    content: bytes
    url: str

    @property
    def text(self) -> str:
        ctype = self.headers.get("content-type", "")
        m = re.search(r"charset=([\w-]+)", ctype, re.I)
        if m:
            try:
                return self.content.decode(m.group(1), errors="replace")
            except LookupError:
                pass
        return decode_bytes(self.content)


def safe_get(url: str, timeout: int = 15, headers: Optional[dict] = None) -> Fetched:
    """GET with per-hop IP validation, manual redirects and a size cap."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_url(current)
        try:
            resp = requests.get(
                current,
                impersonate="chrome124",
                timeout=timeout,
                headers=headers,
                allow_redirects=False,
                stream=True,
            )
        except SSLError:
            raise FetchError("tls")
        except Exception as e:
            raise FetchError(f"request failed: {e}")
        try:
            loc = resp.headers.get("location")
            if resp.status_code in (301, 302, 303, 307, 308) and loc:
                current = urljoin(current, loc)
                continue
            try:
                declared = int(resp.headers.get("content-length") or 0)
            except ValueError:
                declared = 0
            if declared > MAX_FETCH_BYTES:
                raise FetchError("response too large")
            buf = bytearray()
            for chunk in resp.iter_content():
                buf.extend(chunk)
                if len(buf) > MAX_FETCH_BYTES:
                    raise FetchError("response too large")
            hdrs = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            return Fetched(resp.status_code, hdrs, bytes(buf), current)
        finally:
            try:
                resp.close()
            except Exception:
                pass
    raise FetchError("too many redirects")


# ---------------------------------------------------------------------------
# Cookie-backed session persistence
# ---------------------------------------------------------------------------

cookies = CookieController(key="cr_cookies") if CookieController else None


def queue_cookie(op: str, value: str = "") -> None:
    """Cookie writes happen at the top of the NEXT run so the component renders."""
    st.session_state["cookie_op"] = (op, value)


def flush_cookie_ops() -> None:
    op = st.session_state.pop("cookie_op", None)
    if not op or cookies is None:
        return
    try:
        if op[0] == "set":
            try:
                cookies.set(COOKIE_NAME, op[1], max_age=COOKIE_MAX_AGE, same_site="Lax")
            except TypeError:
                cookies.set(COOKIE_NAME, op[1], max_age=COOKIE_MAX_AGE)
        else:
            cookies.remove(COOKIE_NAME)
    except Exception:
        log.exception("cookie operation failed")


# ---------------------------------------------------------------------------
# Supabase auth (one client per browser session)
# ---------------------------------------------------------------------------

def get_session_client() -> Optional[Client]:
    url, key = secret("SUPABASE_URL"), secret("SUPABASE_KEY")
    if not url or not key:
        return None
    if "sb_client" not in st.session_state:
        try:
            st.session_state["sb_client"] = create_client(url, key)
        except Exception:
            log.exception("could not create Supabase client")
            return None
    return st.session_state["sb_client"]


def get_current_user() -> Optional[dict]:
    return st.session_state.get("auth_user")


def invalidate_caches() -> None:
    st.session_state.pop("history_cache", None)
    st.session_state.pop("library_cache", None)


def adopt_session(res) -> bool:
    """Store a successful auth response and queue its refresh token for the cookie."""
    if res and getattr(res, "user", None) and getattr(res, "session", None):
        st.session_state["auth_user"] = {"id": res.user.id, "email": res.user.email}
        queue_cookie("set", res.session.refresh_token)
        invalidate_caches()
        return True
    return False


def restore_session() -> None:
    """Cold-start login from the refresh-token cookie (single use, rotated)."""
    client = get_session_client()
    if client is None or cookies is None or get_current_user():
        return
    rt = cookies.get(COOKIE_NAME)
    if not rt or rt == st.session_state.get("bad_rt"):
        return
    try:
        res = client.auth.refresh_session(rt)
        if not adopt_session(res):
            raise ValueError("refresh returned no session")
    except Exception as e:
        log.warning("session restore failed: %s", e)
        st.session_state["bad_rt"] = rt
        queue_cookie("clear")


def ensure_fresh_session() -> None:
    """Refresh the access token shortly before expiry; rotate the cookie."""
    client = get_session_client()
    user = get_current_user()
    if client is None or user is None:
        return
    try:
        sess = client.auth.get_session()
        if sess is None:
            raise ValueError("no active session")
        if sess.expires_at and sess.expires_at - time.time() < 120:
            res = client.auth.refresh_session()
            if res and res.session:
                queue_cookie("set", res.session.refresh_token)
            else:
                raise ValueError("refresh failed")
    except Exception as e:
        log.warning("session expired, signing out locally: %s", e)
        st.session_state.pop("auth_user", None)
        queue_cookie("clear")
        invalidate_caches()


def get_db() -> Optional[Client]:
    client = get_session_client()
    if client is None or get_current_user() is None:
        return None
    try:
        sess = client.auth.get_session()
        if sess and sess.access_token:
            client.postgrest.auth(sess.access_token)
    except Exception:
        log.exception("could not attach JWT")
    return client


def db_fail(op: str, err: Exception) -> None:
    log.exception("db %s failed", op)
    st.session_state["db_error"] = f"Could not {op}. If this persists, sign out and back in."


# ---------------------------------------------------------------------------
# User-scoped persistence (history & library)
# ---------------------------------------------------------------------------

def get_history(force: bool = False) -> List[str]:
    if not force and "history_cache" in st.session_state:
        return st.session_state["history_cache"]
    db, user = get_db(), get_current_user()
    urls: List[str] = []
    if db and user:
        try:
            resp = (
                db.table("read_history").select("url")
                .eq("user_id", user["id"]).order("accessed_at", desc=True).limit(50).execute()
            )
            for row in resp.data or []:
                if row["url"] not in urls:
                    urls.append(row["url"])
            urls = urls[:HISTORY_LIMIT]
        except Exception as e:
            db_fail("load history", e)
            urls = st.session_state.get("url_history", [])
    else:
        urls = st.session_state.get("url_history", [])
    st.session_state["history_cache"] = urls
    return urls


def save_url_to_history(url: str) -> None:
    db, user = get_db(), get_current_user()
    if db and user:
        try:
            db.table("read_history").insert({"url": url, "user_id": user["id"]}).execute()
        except Exception as e:
            db_fail("save history", e)
    local = st.session_state.get("url_history", [])
    if url in local:
        local.remove(url)
    local.insert(0, url)
    st.session_state["url_history"] = local[:HISTORY_LIMIT]
    st.session_state.pop("history_cache", None)


def clear_history() -> None:
    db, user = get_db(), get_current_user()
    if db and user:
        try:
            db.table("read_history").delete().eq("user_id", user["id"]).execute()
        except Exception as e:
            db_fail("clear history", e)
    st.session_state["url_history"] = []
    st.session_state.pop("history_cache", None)


def get_library(force: bool = False) -> list:
    """Metadata only; content is fetched when an article is opened."""
    if not force and "library_cache" in st.session_state:
        return st.session_state["library_cache"]
    db, user = get_db(), get_current_user()
    items: list = []
    if db and user:
        try:
            resp = (
                db.table("saved_articles").select("id,url,title,word_count,saved_at")
                .eq("user_id", user["id"]).order("saved_at", desc=True).execute()
            )
            items = resp.data or []
        except Exception as e:
            db_fail("load your library", e)
    st.session_state["library_cache"] = items
    return items


def fetch_article(article_id: str) -> Optional[dict]:
    db, user = get_db(), get_current_user()
    if not db or not user:
        return None
    try:
        resp = (
            db.table("saved_articles").select("*")
            .eq("id", article_id).eq("user_id", user["id"]).limit(1).execute()
        )
        return (resp.data or [None])[0]
    except Exception as e:
        db_fail("open that article", e)
        return None


def save_to_library(url: str, title: str, content: str, word_count: int) -> bool:
    db, user = get_db(), get_current_user()
    if not db or not user:
        return False
    try:
        db.table("saved_articles").upsert(
            {"user_id": user["id"], "url": url, "title": title or url,
             "content": content, "word_count": word_count},
            on_conflict="user_id,url",
        ).execute()
        st.session_state.pop("library_cache", None)
        return True
    except Exception as e:
        db_fail("save the article", e)
        return False


def delete_from_library(article_id: str) -> None:
    db, user = get_db(), get_current_user()
    if not db or not user:
        return
    try:
        db.table("saved_articles").delete().eq("id", article_id).eq("user_id", user["id"]).execute()
    except Exception as e:
        db_fail("delete the article", e)
    st.session_state.pop("library_cache", None)


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
# Text / document parsers
# ---------------------------------------------------------------------------

st.markdown(
    """
    <style>
    .block-container { padding-top: 1rem; padding-bottom: 3rem;
        padding-left: 0.85rem; padding-right: 0.85rem; max-width: 620px; }
    .meta-chip { font-size: 0.82rem; color: #888888; margin-bottom: 0.75rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def format_plain_text(raw_text: str) -> str:
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for block in re.split(r"\n\s*\n+", text):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if len(lines) > 1 and any(len(l) < 35 for l in lines):
            joined = "<br>".join(html.escape(l) for l in lines)
        else:
            joined = html.escape(" ".join(lines))
        out.append(f"<p>{joined}</p>")
    return "".join(out)


def extract_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    extracted = "\n\n".join(p.extract_text() or "" for p in reader.pages)
    if not extracted.strip():
        return "<p><em>⚠️ This PDF looks like a scanned image or is empty; no readable text was found.</em></p>"
    return format_plain_text(extracted)


def extract_docx(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))
    return format_plain_text("\n\n".join(p.text for p in doc.paragraphs if p.text.strip()))


def extract_rtf(file_bytes: bytes) -> str:
    return format_plain_text(rtf_to_text(decode_bytes(file_bytes)))


def extract_epub(file_bytes: bytes) -> str:
    book = epub.read_epub(io.BytesIO(file_bytes))
    parts = []
    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            text = BeautifulSoup(item.get_content(), "html.parser").get_text(separator="\n")
            if text.strip():
                parts.append(text)
    return format_plain_text("\n\n".join(parts))


def extract_markdown(file_bytes: bytes) -> str:
    return sanitize_html(markdown.markdown(decode_bytes(file_bytes)))


def parse_upload(name: str, data: bytes) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        return extract_pdf(data)
    if ext == "docx":
        return extract_docx(data)
    if ext == "epub":
        return extract_epub(data)
    if ext == "rtf":
        return extract_rtf(data)
    if ext == "md":
        return extract_markdown(data)
    return format_plain_text(decode_bytes(data))


def article_body(html_text: str) -> Optional[str]:
    """Generic article extraction. Markdown output keeps headings so the TOC works."""
    for kw in ({}, {"favor_recall": True}):
        try:
            md = trafilatura.extract(
                html_text, output_format="markdown", include_comments=False, **kw
            )
        except Exception:
            md = None
        if md and md.strip():
            return sanitize_html(markdown.markdown(md))
    try:
        plain = trafilatura.extract(html_text, favor_recall=True)
    except Exception:
        plain = None
    return format_plain_text(plain) if plain else None


# ---------------------------------------------------------------------------
# Recipe scaling engine
# ---------------------------------------------------------------------------

UNICODE_FRACTIONS = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25, "¾": 0.75,
    "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}
_FR = "".join(UNICODE_FRACTIONS)
_QTY = rf"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?\s*[{_FR}]|\d+(?:\.\d+)?|[{_FR}])"
_LINE_RE = re.compile(rf"^\s*({_QTY})(?:(\s*(?:-|–|to)\s*)({_QTY}))?\s*(.*)$", re.S)
_FRAC_STEPS = [
    (0, ""), (1 / 8, "⅛"), (1 / 4, "¼"), (1 / 3, "⅓"), (3 / 8, "⅜"), (1 / 2, "½"),
    (5 / 8, "⅝"), (2 / 3, "⅔"), (3 / 4, "¾"), (7 / 8, "⅞"), (1, ""),
]


def parse_amount(val: str) -> float:
    val = val.strip()
    for char, num in UNICODE_FRACTIONS.items():
        if char in val:
            prefix = val.replace(char, "").strip()
            return (float(prefix) if prefix else 0.0) + num
    if "/" in val:
        parts = val.split()
        if len(parts) == 2:
            return float(parts[0]) + float(Fraction(parts[1]))
        return float(Fraction(val))
    return float(val)


def format_amount(amount: float) -> str:
    whole = int(math.floor(amount))
    frac = amount - whole
    value, symbol = min(_FRAC_STEPS, key=lambda t: abs(t[0] - frac))
    if value == 1:
        whole += 1
    if whole == 0 and not symbol:
        return f"{amount:.2f}".rstrip("0").rstrip(".") or "0"
    if whole and symbol:
        return f"{whole} {symbol}"
    return symbol or str(whole)


def scale_ingredient_line(text: str, multiplier: float) -> str:
    m = _LINE_RE.match(text)
    if not m:
        return text
    q1, sep, q2, rest = m.groups()
    try:
        out = format_amount(parse_amount(q1) * multiplier)
        if q2:
            joiner = " to " if "to" in sep else "–"
            out += joiner + format_amount(parse_amount(q2) * multiplier)
        return f"{out} {rest}".strip()
    except Exception:
        return text


def scale_recipe_html(content_html: str, multiplier: float) -> str:
    if multiplier == 1.0:
        return content_html
    soup = BeautifulSoup(content_html, "html.parser")
    for lst in soup.find_all(["ul", "ol"]):
        prev = lst.find_previous(["h1", "h2", "h3", "h4"])
        by_heading = prev is not None and "ingredient" in prev.get_text().lower()
        by_class = any("ingredient" in c.lower() for c in lst.get("class", []))
        if not (by_heading or by_class):
            continue
        for li in lst.find_all("li"):
            for node in li.find_all(string=True):
                if node.strip():
                    node.replace_with(NavigableString(scale_ingredient_line(str(node), multiplier)))
                    break
    return str(soup)


# ---------------------------------------------------------------------------
# Chapter navigation (articles only)
# ---------------------------------------------------------------------------

EXCLUDE_HEADING_RE = re.compile(
    r"^(step\s*\d+|ingredients?|directions?|instructions?|notes?|yield|prep\s*time|"
    r"cook\s*time|nutrition|share|comments?)$",
    re.IGNORECASE,
)


def inject_chapter_anchors(content_html: str, is_recipe: bool) -> Tuple[str, List[Tuple[str, str, int]]]:
    if is_recipe:
        return content_html, []
    soup = BeautifulSoup(content_html, "html.parser")
    toc: List[Tuple[str, str, int]] = []
    for i, h in enumerate(soup.find_all(["h1", "h2"]), 1):
        text = h.get_text(strip=True)
        if len(text) < 4 or EXCLUDE_HEADING_RE.match(text):
            continue
        anchor = f"section-{i}"
        h["id"] = anchor
        toc.append((anchor, text, int(h.name[1])))
    if len(toc) < 3:
        return content_html, []
    return str(soup), toc


def build_toc_html(toc: List[Tuple[str, str, int]]) -> str:
    if not toc:
        return ""
    items = "".join(
        f"<li class='lvl{lvl}'><a href='#{aid}'>{html.escape(title)}</a></li>"
        for aid, title, lvl in toc
    )
    return (
        f"<details class='toc'><summary>📑 Table of Contents ({len(toc)})</summary>"
        f"<ul>{items}</ul></details>"
    )


# ---------------------------------------------------------------------------
# Recipe extraction
# ---------------------------------------------------------------------------

def wrap_recipe(inner: str) -> str:
    return f"<div class='recipe-doc'>{inner}</div>"


def extract_recipe_via_scraper(url: str, html_str: str) -> Optional[str]:
    try:
        if scrape_html is not None:
            scraper = scrape_html(html=html_str, org_url=url)
        else:
            scraper = scrape_me(url, html=html_str)
        title = scraper.title()
        ingredients = scraper.ingredients()
        instructions = [s for s in scraper.instructions().split("\n") if s.strip()]
        if not ingredients or not instructions:
            return None
        try:
            yields = scraper.yields()
        except Exception:
            yields = ""
        try:
            total_time = scraper.total_time()
        except Exception:
            total_time = None

        out = [f"<h2>{html.escape(title)}</h2>"]
        meta = []
        if yields:
            meta.append(f"Yield: {html.escape(str(yields))}")
        if total_time:
            meta.append(f"Total Time: {total_time} mins")
        if meta:
            out.append(f"<p class='meta-chip'><em>{' | '.join(meta)}</em></p>")
        out.append("<h3>Ingredients</h3><ul>")
        out.extend(f"<li>{html.escape(i)}</li>" for i in ingredients)
        out.append("</ul><h3>Directions</h3><ol>")
        out.extend(f"<li>{html.escape(s.strip())}</li>" for s in instructions)
        out.append("</ol>")
        return wrap_recipe("".join(out))
    except Exception:
        return None


def extract_recipe_schema(html_content: str) -> Optional[dict]:
    try:
        data = extruct.extract(html_content, syntaxes=["json-ld"])
        for node in data.get("json-ld", []):
            nodes = node.get("@graph", [node]) if isinstance(node, dict) else []
            for n in nodes:
                types = n.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if any(str(t).lower() == "recipe" for t in types):
                    return n
    except Exception:
        return None
    return None


def format_recipe_output(recipe: dict) -> str:
    title = recipe.get("name", "Recipe")
    description = recipe.get("description", "")
    ingredients = recipe.get("recipeIngredient", [])

    steps: List[str] = []

    def collect(step):
        if isinstance(step, str):
            steps.append(step)
        elif isinstance(step, dict):
            if "text" in step:
                steps.append(step["text"])
            for sub in step.get("itemListElement", []) or []:
                collect(sub)
        elif isinstance(step, list):
            for s in step:
                collect(s)

    collect(recipe.get("recipeInstructions", []))

    out = [f"<h2>{html.escape(str(title))}</h2>"]
    if description:
        out.append(f"<p><em>{html.escape(str(description))}</em></p>")
    if ingredients:
        out.append("<h3>Ingredients</h3><ul>")
        out.extend(f"<li>{html.escape(str(i))}</li>" for i in ingredients)
        out.append("</ul>")
    if steps:
        out.append("<h3>Directions</h3><ol>")
        out.extend(f"<li>{html.escape(s)}</li>" for s in steps)
        out.append("</ol>")
    return wrap_recipe("".join(out))


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
        target = soup.find(lambda t: t.get("id") and "recipe" in t.get("id", "").lower()) or soup.find(
            lambda t: t.get("class") and any(
                any(m in c.lower() for m in ("recipe-card", "tasty-recipe", "wprm-recipe"))
                for c in t.get("class", [])
            )
        )
    if not target or len(target.find_all("li")) < 2:
        return None
    return wrap_recipe(sanitize_html(str(target)))


def html_to_content(html_text: str, url: str) -> Optional[str]:
    """Recipe scraper -> JSON-LD -> anchor section -> generic article body."""
    recipe = extract_recipe_via_scraper(url, html_text)
    if recipe:
        return recipe
    data = extract_recipe_schema(html_text)
    if data:
        out = format_recipe_output(data)
        if "<li>" in out:
            return out
    section = find_recipe_section_by_anchor(html_text)
    if section:
        return section
    return article_body(html_text)


# ---------------------------------------------------------------------------
# Specialised resolvers & fallbacks
# ---------------------------------------------------------------------------

def is_substack_url(url: str, html_text: str = "") -> bool:
    if "substack.com" in urlparse(url).netloc:
        return True
    return any(m in html_text for m in ("substackcdn.com", "substack-custom-domains"))


def extract_substack(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if "p" not in parts or parts.index("p") + 1 >= len(parts):
            return None
        slug = parts[parts.index("p") + 1]
        resp = safe_get(
            f"{parsed.scheme}://{parsed.netloc}/api/v1/posts/{slug}",
            timeout=12, headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        data = json.loads(resp.text)
        body_html = data.get("body_html", "")
        if not body_html:
            return None
        header = f"<h2>{html.escape(data.get('title', ''))}</h2>"
        if data.get("subtitle"):
            header += f"<p><em>{html.escape(data['subtitle'])}</em></p>"
        return header + sanitize_html(body_html)
    except Exception as e:
        log.info("substack extractor failed: %s", e)
        return None


def fetch_jina_proxy(target_url: str) -> str:
    if not third_party_enabled():
        return ""
    try:
        resp = safe_get(f"https://r.jina.ai/{target_url}", timeout=20)
        if resp.status_code == 200 and resp.text.strip():
            return sanitize_html(markdown.markdown(resp.text))
    except Exception as e:
        log.info("jina proxy failed: %s", e)
    return ""


def extract_medium(url: str) -> Optional[str]:
    if third_party_enabled():
        try:
            resp = safe_get(f"https://freedium.cfd/{url}", timeout=15)
            if resp.status_code == 200 and len(resp.content) > 500:
                soup = BeautifulSoup(resp.text, "html.parser")
                article = soup.find("article") or soup.find("main")
                if article:
                    return sanitize_html(str(article))
                body = article_body(resp.text)
                if body:
                    return body
        except Exception as e:
            log.info("freedium failed: %s", e)
    proxied = fetch_jina_proxy(url)
    return proxied if len(proxied) > 300 else None


def snapshot_chip(date: str) -> str:
    return f"<p class='meta-chip'>🏛️ Snapshot ({html.escape(date)})</p>"


def fetch_archive_org_snapshot(target_url: str) -> Optional[Tuple[str, str]]:
    try:
        resp = safe_get(f"https://archive.org/wayback/available?url={quote(target_url, safe='')}", timeout=10)
        if resp.status_code != 200:
            return None
        closest = json.loads(resp.text).get("archived_snapshots", {}).get("closest")
        if not closest or not closest.get("available"):
            return None
        raw_url = closest.get("url", "")
        ts = closest.get("timestamp", "")
        snap = re.sub(r"/web/(\d{14})/", r"/web/\1id_/", raw_url)
        arch = safe_get(snap, timeout=15)
        if arch.status_code == 200 and len(arch.text.strip()) > 300:
            date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else "Archived"
            return arch.text, date
    except Exception as e:
        log.info("archive.org failed: %s", e)
    return None


def archive_content(url: str) -> Optional[str]:
    snap = fetch_archive_org_snapshot(url)
    if not snap:
        return None
    arch_html, date = snap
    body = html_to_content(arch_html, url)
    return snapshot_chip(date) + body if body else None


# ---------------------------------------------------------------------------
# Core URL extractor
# ---------------------------------------------------------------------------

def _extract_uncached(raw_input: str, prefer_wayback: bool) -> ExtractResult:
    match = re.search(r"(https?://[^\s]+)", raw_input.strip())
    if not match:
        return ExtractResult(False, message="Please enter a valid URL starting with http:// or https://")
    clean_url = match.group(1)

    try:
        validate_url(clean_url)
    except UnsafeURL as e:
        return ExtractResult(False, message=str(e))

    if is_substack_url(clean_url):
        c = extract_substack(clean_url)
        if c:
            return ExtractResult(True, c)

    if "medium.com" in clean_url or any(d in clean_url for d in ("towardsdatascience.com", "betterprogramming.pub")):
        c = extract_medium(clean_url)
        if c:
            return ExtractResult(True, c)

    parts = urlsplit(clean_url)
    keep_query = ("share.google", "bit.ly", "onedrive.live.com", "1drv.ms")
    if not any(d in parts.netloc for d in keep_query):
        clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    if any(d in parts.netloc for d in ("onedrive.live.com", "1drv.ms")) and "download=1" not in clean_url:
        clean_url += ("&" if "?" in clean_url else "?") + "download=1"

    if prefer_wayback:
        c = archive_content(clean_url)
        if c:
            return ExtractResult(True, c)

    status, fetched = 500, None
    try:
        fetched = safe_get(clean_url)
        status = fetched.status
    except UnsafeURL as e:
        return ExtractResult(False, message=str(e))
    except FetchError as e:
        if str(e) == "tls":
            return ExtractResult(False, message="Site TLS error. Use the manual paste box below.")
        log.info("fetch failed for %s: %s", clean_url, e)

    ctype = fetched.headers.get("content-type", "").lower() if fetched else ""
    cdisp = fetched.headers.get("content-disposition", "").lower() if fetched else ""
    furl = fetched.url.lower() if fetched else ""
    is_textual = ("text/" in ctype or "html" in ctype or "xml" in ctype or not ctype)
    body_text = fetched.text if (fetched and is_textual) else ""

    anti_bot = ("icanhazip.com", "contentlicensing@people.inc", "captcha-delivery.com", "access denied")
    blocked = status >= 400 or any(p in body_text.lower() for p in anti_bot)

    if fetched and fetched.content and not blocked:
        try:
            if "application/pdf" in ctype or furl.endswith(".pdf") or ".pdf" in cdisp:
                return ExtractResult(True, extract_pdf(fetched.content))
            if "wordprocessingml.document" in ctype or furl.endswith(".docx") or ".docx" in cdisp:
                return ExtractResult(True, extract_docx(fetched.content))
            if "application/epub+zip" in ctype or furl.endswith(".epub") or ".epub" in cdisp:
                return ExtractResult(True, extract_epub(fetched.content))
            if "rtf" in ctype or furl.endswith(".rtf") or ".rtf" in cdisp:
                return ExtractResult(True, extract_rtf(fetched.content))
            if "text/plain" in ctype or furl.endswith(".txt") or ".txt" in cdisp:
                return ExtractResult(True, format_plain_text(fetched.text))
        except Exception:
            log.exception("document parsing failed for %s", clean_url)
            return ExtractResult(False, message="That file could not be parsed.")

        if is_substack_url(clean_url, body_text):
            c = extract_substack(clean_url)
            if c:
                return ExtractResult(True, c)

        c = html_to_content(body_text, clean_url)
        if c:
            return ExtractResult(True, c)

    c = archive_content(clean_url)
    if c:
        return ExtractResult(True, c)

    proxied = fetch_jina_proxy(clean_url)
    if proxied:
        return ExtractResult(True, proxied)

    return ExtractResult(False, message="Firewalls blocked access and no archive found. Use manual paste below.")


@st.cache_data(ttl=3600, show_spinner=False, max_entries=64)
def _extract_cached(raw_input: str, prefer_wayback: bool, scope: str) -> ExtractResult:
    # `scope` (user id or "anon") is part of the cache key so private links
    # never leak across users. Raising keeps failures out of the cache.
    res = _extract_uncached(raw_input, prefer_wayback)
    if not res.ok:
        raise ExtractError(res.message or "Extraction failed")
    return res


def extract_from_url(raw_input: str, prefer_wayback: bool = False) -> ExtractResult:
    user = get_current_user()
    try:
        return _extract_cached(raw_input, prefer_wayback, user["id"] if user else "anon")
    except ExtractError as e:
        return ExtractResult(False, message=str(e))
    except Exception:
        log.exception("unexpected extraction error")
        return ExtractResult(False, message="Unexpected error while extracting. Try manual paste below.")


# ---------------------------------------------------------------------------
# Reader (all interactive behaviour lives client-side, in the iframe)
# ---------------------------------------------------------------------------

READER_TEMPLATE = r"""
<style>
  :root { --bg:#ffffff; --fg:#111111; --bar:#f2f2f2; --bd:#dddddd; --btn:#ffffff; --accent:#ff4b4b; --muted:#888; }
  :root[data-theme=sepia] { --bg:#fbf0d9; --fg:#433422; --bar:#efe3c6; --bd:#d9c9a3; --btn:#fbf0d9; }
  :root[data-theme=dark]  { --bg:#1a1a1a; --fg:#e0e0e0; --bar:#262626; --bd:#3a3a3a; --btn:#2c2c2c; }
  html, body { margin:0; padding:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:var(--bg); }
  #progress { position:fixed; top:0; left:0; width:100%; height:5px; background:rgba(128,128,128,.2); z-index:9; }
  #bar { width:0; height:100%; background:var(--accent); }
  #toolbar { display:flex; gap:6px; align-items:center; overflow-x:auto; -webkit-overflow-scrolling:touch;
             padding:10px 8px 6px; background:var(--bar); border-bottom:1px solid var(--bd); }
  .btn { flex-shrink:0; border:1px solid var(--bd); background:var(--btn); color:var(--fg);
         border-radius:999px; padding:6px 12px; font-size:13px; cursor:pointer; white-space:nowrap; }
  .btn.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  #reader-scroll { height:__PANE_HEIGHT__px; overflow-y:auto; -webkit-overflow-scrolling:touch; box-sizing:border-box; background:var(--bg); }
  #frame { color:var(--fg); padding:1.25rem 1rem; line-height:1.8; overflow-wrap:anywhere; }
  #frame p, #frame li { line-height:1.8; }
  #frame p { margin:0 0 1.35em; }
  #frame img { max-width:100%; height:auto; }
  #frame a { color:inherit; }
  #frame pre { overflow-x:auto; }
  #frame table { display:block; overflow-x:auto; }
  .meta-chip { font-size:.82rem; color:var(--muted); }
  details.toc { margin-bottom:1.2rem; padding:8px 12px; background:rgba(128,128,128,.1); border-radius:8px; font-size:.92rem; }
  details.toc summary { cursor:pointer; font-weight:600; }
  details.toc ul { list-style:none; padding-left:4px; margin:10px 0 0; }
  details.toc li { margin-bottom:6px; } details.toc li.lvl2 { margin-left:14px; }
  details.toc a { text-decoration:none; opacity:.85; }
</style>
<div id="progress"><div id="bar"></div></div>
<div id="toolbar">
  <button id="scrollBtn" class="btn">▶️ Scroll</button>
  <button id="speedBtn" class="btn">Speed: Medium</button>
  <button id="smaller" class="btn">A−</button>
  <button id="larger" class="btn">A+</button>
  <button id="fontBtn" class="btn">Sans</button>
  <button id="themeBtn" class="btn">Light</button>
  <button id="ttsBtn" class="btn">🔊 Read Aloud</button>
</div>
<div id="reader-scroll"><div id="frame">__TOC_HTML____HTML_CONTENT__</div></div>
<script>
(function () {
  const KEY = '__DOC_KEY__';
  const SCROLL_KEY = 'cr:scroll:' + KEY, PREF_KEY = 'cr:prefs', INDEX_KEY = 'cr:index';
  const $ = id => document.getElementById(id);
  const root = document.documentElement, scrollEl = $('reader-scroll'), frame = $('frame'), bar = $('bar');
  const FONTS = {
    Sans: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
    Serif: "Georgia, Cambria, 'Times New Roman', serif",
    Mono: "Menlo, Consolas, Monaco, monospace"
  };
  const FONT_NAMES = Object.keys(FONTS), THEMES = ['light', 'sepia', 'dark'];
  const SPEEDS = [{n: 'Slow', pps: 20}, {n: 'Medium', pps: 40}, {n: 'Fast', pps: 80}];
  let prefs = {theme: 'light', font: 'Sans', size: 18, speed: 1};

  try { Object.assign(prefs, JSON.parse(localStorage.getItem(PREF_KEY) || '{}')); } catch (e) {}
  function savePrefs() { try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (e) {} }
  function applyPrefs() {
    root.dataset.theme = prefs.theme;
    frame.style.fontFamily = FONTS[prefs.font] || FONTS.Sans;
    frame.style.fontSize = prefs.size + 'px';
    $('fontBtn').textContent = prefs.font;
    $('themeBtn').textContent = prefs.theme[0].toUpperCase() + prefs.theme.slice(1);
    $('speedBtn').textContent = 'Speed: ' + SPEEDS[prefs.speed].n;
  }
  applyPrefs();

  $('themeBtn').onclick = () => { prefs.theme = THEMES[(THEMES.indexOf(prefs.theme) + 1) % THEMES.length]; applyPrefs(); savePrefs(); };
  $('fontBtn').onclick = () => { prefs.font = FONT_NAMES[(FONT_NAMES.indexOf(prefs.font) + 1) % FONT_NAMES.length]; applyPrefs(); savePrefs(); };
  $('larger').onclick = () => { prefs.size = Math.min(32, prefs.size + 1); applyPrefs(); savePrefs(); };
  $('smaller').onclick = () => { prefs.size = Math.max(14, prefs.size - 1); applyPrefs(); savePrefs(); };

  /* ---- per-article scroll position (keeps only the 30 most recent) ---- */
  try {
    const saved = parseFloat(localStorage.getItem(SCROLL_KEY));
    if (!isNaN(saved)) setTimeout(() => { scrollEl.scrollTop = saved; updateProgress(); }, 0);
    let idx = JSON.parse(localStorage.getItem(INDEX_KEY) || '[]').filter(k => k !== KEY);
    idx.push(KEY);
    while (idx.length > 30) localStorage.removeItem('cr:scroll:' + idx.shift());
    localStorage.setItem(INDEX_KEY, JSON.stringify(idx));
  } catch (e) {}

  let saveTimer = null;
  function updateProgress() {
    const total = scrollEl.scrollHeight - scrollEl.clientHeight;
    bar.style.width = total > 0 ? (scrollEl.scrollTop / total * 100) + '%' : '0%';
  }
  scrollEl.addEventListener('scroll', () => {
    updateProgress();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { try { localStorage.setItem(SCROLL_KEY, scrollEl.scrollTop); } catch (e) {} }, 400);
  });
  updateProgress();

  /* ---- auto-scroll (rAF, px/sec; own float position so slow speeds work on Safari) ---- */
  let active = false, last = 0, pos = 0;
  function tick(ts) {
    if (!active) return;
    pos += SPEEDS[prefs.speed].pps * ((ts - last) / 1000);
    last = ts;
    scrollEl.scrollTop = pos;
    if (scrollEl.scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 2) { setActive(false); return; }
    requestAnimationFrame(tick);
  }
  function setActive(on) {
    active = on;
    $('scrollBtn').textContent = on ? '⏸ Pause' : '▶️ Scroll';
    $('scrollBtn').classList.toggle('on', on);
    if (on) { pos = scrollEl.scrollTop; last = performance.now(); requestAnimationFrame(tick); }
  }
  $('scrollBtn').onclick = () => setActive(!active);
  $('speedBtn').onclick = () => { prefs.speed = (prefs.speed + 1) % SPEEDS.length; applyPrefs(); savePrefs(); };
  ['wheel', 'touchstart', 'keydown'].forEach(ev => scrollEl.addEventListener(ev, () => { if (active) setActive(false); }, {passive: true}));

  /* ---- in-page anchors (TOC, footnotes) scroll the inner pane ---- */
  frame.addEventListener('click', e => {
    const a = e.target.closest('a[href^="#"]');
    if (!a) return;
    e.preventDefault();
    const target = document.getElementById(a.getAttribute('href').slice(1));
    if (target) {
      const top = target.getBoundingClientRect().top - scrollEl.getBoundingClientRect().top + scrollEl.scrollTop - 8;
      scrollEl.scrollTo({top: top, behavior: 'smooth'});
    }
  });

  /* ---- read aloud ---- */
  const ttsText = __TTS_TEXT_JSON__;
  let chunks = [], ci = 0, speaking = false;
  function chunkText(text) {
    const sentences = text.match(/[^.!?]+[.!?]*/g) || [text];
    const out = []; let cur = '';
    for (const s of sentences) {
      if ((cur + s).length > 200 && cur) { out.push(cur.trim()); cur = s; } else { cur += s; }
    }
    if (cur.trim()) out.push(cur.trim());
    return out;
  }
  function speakNext() {
    if (ci >= chunks.length) { speaking = false; $('ttsBtn').textContent = '🔊 Read Aloud'; return; }
    const u = new SpeechSynthesisUtterance(chunks[ci]);
    u.onend = () => { ci += 1; if (speaking) speakNext(); };
    u.onerror = () => { speaking = false; $('ttsBtn').textContent = '🔊 Read Aloud'; };
    window.speechSynthesis.speak(u);
  }
  $('ttsBtn').onclick = () => {
    if (!('speechSynthesis' in window)) return;
    if (!speaking) {
      if (window.speechSynthesis.paused) { window.speechSynthesis.resume(); }
      else { chunks = chunkText(ttsText); ci = 0; window.speechSynthesis.cancel(); speaking = true; speakNext(); }
      speaking = true;
      $('ttsBtn').textContent = '⏸ Pause Reading';
    } else {
      window.speechSynthesis.pause(); speaking = false;
      $('ttsBtn').textContent = '▶️ Resume Reading';
    }
  };
  window.addEventListener('pagehide', () => { try { window.speechSynthesis.cancel(); } catch (e) {} });
})();
</script>
"""


def render_reader(content_html: str, toc_html: str, plain_text: str, doc_key: str, pane_height: int) -> None:
    values = {
        "PANE_HEIGHT": str(int(pane_height)),
        "DOC_KEY": re.sub(r"[^0-9a-f]", "", doc_key),
        "TOC_HTML": toc_html,
        "HTML_CONTENT": content_html,
        "TTS_TEXT_JSON": json_for_script(plain_text),
    }
    # Single-pass substitution: inserted content is never re-scanned for placeholders.
    doc = re.sub(r"__([A-Z_]+?)__", lambda m: values.get(m.group(1), m.group(0)), READER_TEMPLATE)
    components.html(doc, height=int(pane_height) + 62, scrolling=False)


# ---------------------------------------------------------------------------
# Session-state helpers & callbacks
# ---------------------------------------------------------------------------

def set_active(content: str, source_url: Optional[str]) -> None:
    st.session_state["active_doc"] = {
        "content": content,
        "source_url": source_url,
        "key": short_hash(source_url or content[:5000]),
    }


def open_library_item(article_id: str) -> None:
    row = fetch_article(article_id)
    if row:
        url = row.get("url") or ""
        set_active(row["content"], url if url.startswith("http") else None)


def delete_library_item(article_id: str) -> None:
    delete_from_library(article_id)


def pick_history() -> None:
    chosen = st.session_state.get("history_pick")
    if chosen and chosen != PLACEHOLDER:
        st.session_state["url_input"] = chosen


def use_manual_text() -> None:
    text = st.session_state.get("manual_text", "")
    if text.strip():
        set_active(format_plain_text(text), None)


# ---------------------------------------------------------------------------
# App entrypoint
# ---------------------------------------------------------------------------

restore_session()
ensure_fresh_session()
flush_cookie_ops()

st.title("📖 Clean 9:16 Reader")

client = get_session_client()
user = get_current_user()

with st.sidebar:
    st.header("👤 Account")
    if client is None:
        st.warning("Supabase credentials not detected in secrets.")
    elif user:
        st.success(f"Signed in as **{user.get('email')}**")
        if st.button("🚪 Log Out"):
            try:
                client.auth.sign_out()
            except Exception:
                log.exception("sign_out failed")
            if cookies is not None:
                st.session_state["bad_rt"] = cookies.get(COOKIE_NAME)
            queue_cookie("clear")
            for k in ("auth_user", "url_history", "active_doc"):
                st.session_state.pop(k, None)
            invalidate_caches()
            st.rerun()
    else:
        if cookies is None:
            st.caption("Install `streamlit-cookies-controller` to stay signed in across refreshes.")
        action = st.radio("Choose action", ["Sign In", "Sign Up"], horizontal=True)
        email_in = st.text_input("Email", key="auth_email")
        pass_in = st.text_input("Password", type="password", key="auth_password")
        if action == "Sign In":
            if st.button("Log In"):
                try:
                    res = client.auth.sign_in_with_password({"email": email_in, "password": pass_in})
                    if adopt_session(res):
                        st.session_state.pop("bad_rt", None)
                        st.rerun()
                    else:
                        st.error("Login failed. Check your email and password.")
                except Exception as e:
                    log.warning("login failed: %s", e)
                    st.error("Login failed. Check your email and password.")
        else:
            if st.button("Register"):
                try:
                    res = client.auth.sign_up({"email": email_in, "password": pass_in})
                    if res and res.user:
                        st.info("Account created. Confirm via email if required, then sign in.")
                except Exception as e:
                    log.warning("sign up failed: %s", e)
                    st.error("Sign up failed. Use a valid email and a stronger password.")

if not user:
    st.info("💡 Sign in via the sidebar to persist your Recent URLs and Saved Articles.")

if st.session_state.get("db_error"):
    st.warning(st.session_state.pop("db_error"))

# ---- History ---------------------------------------------------------------
history_list = get_history()
if history_list:
    with st.expander("🕒 Recent URLs", expanded=False):
        st.selectbox(
            "Select a previously accessed page:", [PLACEHOLDER] + history_list,
            key="history_pick", on_change=pick_history,
        )
        if st.button("🗑️ Clear History"):
            clear_history()
            st.rerun()

# ---- Library (metadata only) ----------------------------------------------
library_items = get_library()
if library_items:
    with st.expander(f"📚 Saved Articles ({len(library_items)})", expanded=False):
        for entry in library_items:
            art_id = str(entry["id"])
            c_title, c_open, c_del = st.columns([5, 1.5, 1])
            c_title.write(entry.get("title") or entry.get("url") or "Untitled")
            c_open.button("Open", key=f"open_{art_id}", on_click=open_library_item, args=(art_id,))
            c_del.button("🗑️", key=f"del_{art_id}", on_click=delete_library_item, args=(art_id,))

# ---- Inputs ----------------------------------------------------------------
col_input, col_ia, col_cache = st.columns([3, 1, 0.8])
with col_input:
    url_input = st.text_input(
        "Paste URL (Article, Recipe, OneDrive, PDF, or text):",
        key="url_input", placeholder="https://...",
    )
with col_ia:
    st.write("")
    st.write("")
    use_wayback = st.checkbox("🏛️ Wayback", help="Force retrieval from Internet Archive snapshot")
with col_cache:
    st.write("")
    st.write("")
    if st.button("🔄 Cache", help="Flush cached pages and re-fetch"):
        st.cache_data.clear()
        st.session_state.pop("loaded_sig", None)
        st.toast("Cache flushed!", icon="🧹")
        st.rerun()

uploaded_file = st.file_uploader("Or upload document:", type=["txt", "pdf", "docx", "epub", "rtf", "md"])

with st.expander("📋 Manual Text / Recipe Paste (Fallback)"):
    st.text_area("Paste raw text or recipe directions here:", height=150, key="manual_text")
    st.button("Show pasted text", on_click=use_manual_text)

# ---- Load triggers (each fires once per distinct input) --------------------
url_clean = (url_input or "").strip()
sig = (url_clean, use_wayback)
if url_clean and sig != st.session_state.get("loaded_sig"):
    st.session_state["loaded_sig"] = sig
    with st.spinner("Extracting & formatting..."):
        result = extract_from_url(url_clean, prefer_wayback=use_wayback)
    if result.ok:
        set_active(result.content, url_clean)
        save_url_to_history(url_clean)
    else:
        st.error(result.message)

if uploaded_file is not None:
    up_sig = (uploaded_file.name, uploaded_file.size)
    if up_sig != st.session_state.get("loaded_upload"):
        st.session_state["loaded_upload"] = up_sig
        if uploaded_file.size > MAX_FETCH_BYTES:
            st.error("File too large (15 MB max).")
        else:
            with st.spinner("Formatting file..."):
                try:
                    set_active(parse_upload(uploaded_file.name, uploaded_file.read()), None)
                except Exception:
                    log.exception("upload parse failed")
                    st.error("That file could not be parsed.")

# ---- Reader ----------------------------------------------------------------
doc = st.session_state.get("active_doc")
if doc:
    original = doc["content"]
    is_recipe = BeautifulSoup(original, "html.parser").select_one(".recipe-doc") is not None

    view_html = original
    if is_recipe:
        c1, c2 = st.columns([2, 3])
        c1.markdown("🍳 **Scale Servings:**")
        mult_map = {"0.5x": 0.5, "1x": 1.0, "2x": 2.0, "3x": 3.0}
        choice = c2.radio(
            "Scale", list(mult_map), index=1, horizontal=True,
            label_visibility="collapsed", key=f"scale_{doc['key']}",
        )
        view_html = scale_recipe_html(original, mult_map[choice])

    anchored, toc_items = inject_chapter_anchors(view_html, is_recipe)
    safe_content = sanitize_html(anchored)
    toc_markup = build_toc_html(toc_items)

    plain_text = BeautifulSoup(safe_content, "html.parser").get_text(separator=" ")
    word_count = len(plain_text.split())
    reading_time = max(1, round(word_count / 200)) if word_count else 0

    st.divider()
    col_meta, col_copy = st.columns([3, 2])
    col_meta.markdown(
        f"<div class='meta-chip'>⏱️ ~{reading_time} min read &nbsp;•&nbsp; {word_count:,} words</div>",
        unsafe_allow_html=True,
    )
    with col_copy:
        with st.popover("📋 Copy Text"):
            st.code(plain_text, language=None)

    if st.button("💾 Save to Library"):
        if not get_current_user():
            st.error("Please sign in from the sidebar to save articles.")
        else:
            sanitized = sanitize_html(original)
            fallback = doc["source_url"] or "Uploaded Document"
            save_key = doc["source_url"] or f"upload:{short_hash(original)}"
            if save_to_library(save_key, guess_title(sanitized, fallback), sanitized, word_count):
                st.success("Saved to your library.")
                st.rerun()

    if library_items and doc["source_url"] is None:
        pass  # uploads/manual text are saved under a content-hash key

    with st.expander("⚙️ Layout", expanded=False):
        pane_height = st.slider("Reading Pane Height (px)", 400, 1000, 600, 50, key="reader_pane_height")
        st.caption("Typeface, size, theme, scroll speed and read-aloud are in the reader toolbar.")

    render_reader(safe_content, toc_markup, plain_text, doc["key"], pane_height)

    exp_col1, exp_col2 = st.columns(2)
    title_for_file = re.sub(r"[^\w\- ]", "", guess_title(safe_content, "article"))[:60] or "article"
    exp_col1.download_button("⬇️ .txt", to_plain_text_for_export(safe_content),
                             file_name=f"{title_for_file}.txt", mime="text/plain")
    exp_col2.download_button("⬇️ .html", safe_content,
                             file_name=f"{title_for_file}.html", mime="text/html")
