#!/usr/bin/env python3
"""
Full-text retrieval for new articles.

For each new article, in order:
  1. Use the full text the feed itself carries (content:encoded, Reddit posts,
     Substack), if it is long enough to be a real article.
  2. Otherwise open the article page and extract the main text with trafilatura.
     Google News links are first decoded to the publisher's real address.
  3. If nothing usable comes back, keep the feed description and mark the row
     so it is obvious in the CSV that the full text was not retrieved.

Sets on each article:
  content         the text that goes in the CSV
  content_status  full_feed | full_page | fallback
"""

from __future__ import annotations

import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}

PAGE_TIMEOUT_S = 20
WORKERS = 8
FEED_TEXT_MIN_CHARS = 1500   # feed text shorter than this is treated as a teaser
PAGE_TEXT_MIN_CHARS = 300    # page text shorter than this is treated as a failed extract
MAX_CONTENT_CHARS = 32000    # Excel and Sheets cap a cell at 32,767 characters
MAX_PAGE_BYTES = 3_000_000
FALLBACK_TAG = "[FULL TEXT NOT RETRIEVED] "

# Seconds between requests to the same host, so one busy publisher is not hammered.
HOST_DELAY_S = 0.5
GOOGLE_DECODE_DELAY_S = 1.0

_host_locks: dict[str, threading.Lock] = {}
_host_last: dict[str, float] = {}
_guard = threading.Lock()


def _pace(host: str, delay: float) -> None:
    with _guard:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        wait = _host_last.get(host, 0.0) + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        _host_last[host] = time.time()


def html_to_text(s: str) -> str:
    """HTML fragment to plain text, keeping paragraph breaks."""
    s = s or ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</blockquote>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def feed_full_text(entry) -> str:
    """Longest body text a feed entry carries. Called from fetch_feeds."""
    parts = []
    for c in entry.get("content") or []:
        parts.append(c.get("value") or "")
    parts.append(entry.get("summary") or "")
    parts.append(entry.get("description") or "")
    texts = [html_to_text(p) for p in parts if p]
    return max(texts, key=len) if texts else ""


def decode_google_news(url: str) -> str | None:
    """Turn a news.google.com/rss/articles/... link into the publisher's link."""
    try:
        from googlenewsdecoder import gnewsdecoder
    except Exception:
        return None
    _pace("news.google.com", GOOGLE_DECODE_DELAY_S)
    try:
        res = gnewsdecoder(url, interval=None)
    except Exception:
        return None
    if isinstance(res, dict) and res.get("status") and res.get("decoded_url"):
        return res["decoded_url"]
    return None


def extract_from_page(url: str) -> tuple[str, str]:
    """Return (text, note). text is empty when extraction failed."""
    import trafilatura

    host = urlparse(url).netloc
    _pace(host, HOST_DELAY_S)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=PAGE_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as e:
        return "", f"page fetch failed: {e}"
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype.lower() and "xml" not in ctype.lower():
        return "", f"not html ({ctype})"
    body = resp.content[:MAX_PAGE_BYTES]
    if b"Just a moment" in body[:5000] or b"challenge-platform" in body:
        return "", "blocked by bot challenge"
    try:
        text = trafilatura.extract(
            body,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception as e:
        return "", f"extract failed: {e}"
    return (text or "").strip(), ""


def _finish(a: dict, text: str, status: str) -> None:
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS].rstrip() + " [TRUNCATED]"
    a["content"] = text
    a["content_status"] = status


def enrich_one(a: dict) -> None:
    feed_text = (a.get("feed_content") or "").strip()
    teaser = (a.get("summary") or "").strip()

    if len(feed_text) >= FEED_TEXT_MIN_CHARS:
        _finish(a, feed_text, "full_feed")
        return

    url = a.get("url") or ""
    if "news.google.com" in url:
        real = decode_google_news(url)
        if real:
            a["resolved_url"] = real
            url = real
        else:
            best = feed_text or teaser
            _finish(a, FALLBACK_TAG + best, "fallback")
            return

    text, _note = extract_from_page(url)
    if len(text) >= PAGE_TEXT_MIN_CHARS and len(text) >= len(feed_text):
        _finish(a, text, "full_page")
    elif len(feed_text) >= PAGE_TEXT_MIN_CHARS:
        _finish(a, feed_text, "full_feed")
    else:
        best = text or feed_text or teaser
        _finish(a, FALLBACK_TAG + best, "fallback")


def enrich_articles(articles: list[dict]) -> dict[str, int]:
    """Fill content for every article in place. Never raises on one bad article."""

    def safe(a: dict) -> None:
        try:
            enrich_one(a)
        except Exception as e:  # keep the run alive
            best = a.get("feed_content") or a.get("summary") or ""
            _finish(a, FALLBACK_TAG + best, "fallback")
            print(f"  ! content error for {a.get('url')}: {e}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(safe, articles))

    stats = {"full_feed": 0, "full_page": 0, "fallback": 0}
    for a in articles:
        stats[a.get("content_status", "fallback")] += 1
    return stats
