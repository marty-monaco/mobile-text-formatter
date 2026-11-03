"""
Clean Reader — mobile-friendly reader for articles, recipes, and documents.
- Per-user Supabase persistence (History & Library) with Row Level Security (RLS)
- Multi-format document extraction (.pdf, .docx, .epub, .rtf, .txt, .md)
- Dedicated Substack, Medium, Recipe, and Archive extraction
- Hands-free auto-scroll, read-aloud TTS, and offline subway cache
"""

import html
import io
import json
import os
import re
from dataclasses import dataclass
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
    soup = BeautifulSoup(cleaned, "html.parser")
    for a in soup.find_all("a"):
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


@dataclass
class ExtractResult:
    ok: bool
    content: str = ""
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Supabase Authentication & User-Isolated Client
# ---------------------------------------------------------------------------

def get_session_client() -> Optional[Client]:
    """Returns an unshared client instance unique to this browser session."""
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_KEY")
    if not url or not key:
        return None
    if "sb_client" not in st.session_state:
        try:
            st.session_state["sb_client"] = create_client(url, key)
        except Exception:
            return None
    return st.session_state["sb_client"]


def get_current_user() -> Optional[dict]:
    """Retrieves current authenticated user from session state."""
    return st.session_state.get("auth_user")


def get_db() -> Optional[Client]:
    """Returns the session client with the active JWT attached for Supabase RLS."""
    client = get_session_client()
    user = get_current_user()
    if client is None:
        return None
    if user is not None:
        try:
            session = client.auth.get_session()
            if session and session.access_token:
                client.postgrest.auth(session.access_token)
        except Exception:
            pass
    return client


# ---------------------------------------------------------------------------
# User-Scoped Persistence Layer (read_history & saved_articles)
# ---------------------------------------------------------------------------

def load_history() -> list:
    db = get_db()
    user = get_current_user()
    if db and user:
        try:
            resp = (
                db.table("read_history")
                .select("url")
                .eq("user_id", user["id"])
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
    user = get_current_user()
    if db and user:
        try:
            db.table("read_history").insert({
                "url": url,
                "user_id": user["id"],
            }).execute()
        except Exception:
            pass
    history = st.session_state.get("url_history", [])
    if url in history:
        history.remove(url)
    history.insert(0, url)
    st.session_state["url_history"] = history[:10]


def clear_history():
    db = get_db()
    user = get_current_user()
    if db and user:
        try:
            db.table("read_history").delete().eq("user_id", user["id"]).execute()
        except Exception:
            pass
    st.session_state["url_history"] = []


def load_library() -> list:
    db = get_db()
    user = get_current_user()
    if not db or not user:
        return []
    try:
        resp = (
            db.table("saved_articles")
            .select("*")
            .eq("user_id", user["id"])
            .order("saved_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception:
        return []


def save_to_library(url: str, title: str, content: str, word_count: int) -> bool:
    db = get_db()
    user = get_current_user()
    if not db or not user:
        return False
    try:
        db.table("saved_articles").upsert(
            {
                "user_id": user["id"],
                "url": url,
                "title": title or url,
                "content": content,
                "word_count": word_count,
            },
            on_conflict="user_id,url",
        ).execute()
        return True
    except Exception:
        return False


def delete_from_library(article_id: str):
    db = get_db()
    user = get_current_user()
    if not db or not user:
        return
    try:
        db.table("saved_articles").delete().eq("id", article_id).eq("user_id", user["id"]).execute()
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
        if el.name in
