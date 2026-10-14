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
  content_note    why retrieval failed (only set when it did); shown as a count in the email
"""

from __future__ import annotations

import html
import os
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


_gd_lock = threading.Lock()
_gd_ok = 0
_gd_consec_fail = 0
GD_GIVE_UP_AFTER = 20  # if the first 20 articles all fail to decode, stop trying this run


def decode_google_news(url: str) -> tuple[str | None, str]:
    """Turn a news.google.com/rss/articles/... link into the publisher's link.
    Returns (url, note). note says why it failed."""
    global _gd_ok, _gd_consec_fail
    with _gd_lock:
        if _gd_ok == 0 and _gd_consec_fail >= GD_GIVE_UP_AFTER:
            return None, "google decode skipped (first 20 all failed)"
    try:
        from googlenewsdecoder import gnewsdecoder
    except Exception as e:
        return None, f"google decoder import failed: {type(e).__name__}"
    last = "unknown"
    for attempt in range(3):
        _pace("news.google.com", GOOGLE_DECODE_DELAY_S)
        try:
            res = gnewsdecoder(url, interval=None)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
        else:
            # googlenewsdecoder 0.2.x reports "success", older versions "status"
            if isinstance(res, dict) and (res.get("success") or res.get("status")) and res.get("decoded_url"):
                with _gd_lock:
                    _gd_ok += 1
                    _gd_consec_fail = 0
                return res["decoded_url"], ""
            last = str((res or {}).get("message") or res)[:100]
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    with _gd_lock:
        _gd_consec_fail += 1
    return None, f"google decode failed: {last}"


def extract_via_jina(url: str) -> tuple[str, str]:
    """Second try for pages that block direct requests from GitHub: the Jina
    Reader service (r.jina.ai) fetches the page and returns plain text. Works
    without a key at a low rate limit. Set JINA_API_KEY for a higher limit, or
    USE_JINA=0 to switch it off."""
    if os.environ.get("USE_JINA", "1") == "0":
        return "", "jina off"
    key = os.environ.get("JINA_API_KEY")
    _pace("r.jina.ai", 0.6 if key else 3.2)
    headers = {"Accept": "text/plain", "X-Return-Format": "text", "User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        resp = requests.get("https://r.jina.ai/" + url, headers=headers, timeout=45)
    except Exception as e:
        return "", f"jina {type(e).__name__}"
    if resp.status_code != 200:
        return "", f"jina http {resp.status_code}"
    text = resp.text
    m = re.search(r"Markdown Content:\s*", text)
    if m:
        text = text[m.end():]
    head = text[:400]
    if "Target URL returned error" in head or "CAPTCHA" in head:
        return "", "jina: target blocked"
    return text.strip(), ""


def extract_from_page(url: str) -> tuple[str, str]:
    """Return (text, note). text is empty when extraction failed."""
    import trafilatura

    host = urlparse(url).netloc
    _pace(host, HOST_DELAY_S)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=PAGE_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as e:
        return "", f"page http {getattr(getattr(e, 'response', None), 'status_code', None) or type(e).__name__}"
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype.lower() and "xml" not in ctype.lower():
        return "", "page not html"
    body = resp.content[:MAX_PAGE_BYTES]
    if b"Just a moment" in body[:5000] or b"challenge-platform" in body:
        return "", "page bot challenge"
    try:
        text = trafilatura.extract(
            body,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
    except Exception as e:
        return "", "page extract error"
    text = (text or "").strip()
    return text, ("" if len(text) >= PAGE_TEXT_MIN_CHARS else "page text too short")


def _finish(a: dict, text: str, status: str) -> None:
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS].rstrip() + " [TRUNCATED]"
    a["content"] = text
    a["content_status"] = status


def looks_like_article(text: str, title: str) -> bool:
    """True when the text plausibly belongs to the article with this title.
    Used to reject consent pages and unrelated pages that Jina can return for
    a Google News link."""
    head = (text or "")[:4000].lower()
    core = re.sub(r"\s+-\s+[^-]{2,40}$", "", title or "").strip().lower()
    if len(core) < 6:
        return len(text) >= PAGE_TEXT_MIN_CHARS
    if re.search(r"[\u4e00-\u9fff]", core):  # Chinese titles: match a run of characters
        return core[:8] in head or core[2:10] in head
    words = [w for w in re.findall(r"[a-z0-9']{3,}", core)]
    if not words:
        return False
    hits = sum(1 for w in words if w in head)
    return hits / len(words) >= 0.6


def enrich_one(a: dict) -> None:
    feed_text = (a.get("feed_content") or "").strip()
    teaser = (a.get("summary") or "").strip()

    if len(feed_text) >= FEED_TEXT_MIN_CHARS:
        _finish(a, feed_text, "full_feed")
        return

    url = a.get("url") or ""
    if "news.google.com" in url:
        real, note = decode_google_news(url)
        if real:
            a["resolved_url"] = real
            url = real
        else:
            # Decoding failed. Last resort: let Jina open the Google link, which
            # redirects to the publisher. Accept only if the text matches the headline.
            jtext, jnote = extract_via_jina(url)
            if len(jtext) >= PAGE_TEXT_MIN_CHARS and looks_like_article(jtext, a.get("title", "")):
                _finish(a, jtext, "full_page")
            else:
                a["content_note"] = f"{note}; {jnote or 'jina text did not match headline'}"
                _finish(a, FALLBACK_TAG + (feed_text or teaser), "fallback")
            return

    text, note = extract_from_page(url)
    if len(text) < PAGE_TEXT_MIN_CHARS and len(feed_text) < PAGE_TEXT_MIN_CHARS:
        jtext, jnote = extract_via_jina(url)
        if len(jtext) >= PAGE_TEXT_MIN_CHARS:
            text, note = jtext, ""
        else:
            note = f"{note}; {jnote}" if note else jnote

    if len(text) >= PAGE_TEXT_MIN_CHARS and len(text) >= len(feed_text):
        _finish(a, text, "full_page")
    elif len(feed_text) >= PAGE_TEXT_MIN_CHARS:
        _finish(a, feed_text, "full_feed")
    else:
        a["content_note"] = note or "no text found"
        _finish(a, FALLBACK_TAG + (text or feed_text or teaser), "fallback")


def enrich_articles(articles: list[dict]) -> dict[str, int]:
    """Fill content for every article in place. Never raises on one bad article."""

    def safe(a: dict) -> None:
        try:
            enrich_one(a)
        except Exception as e:  # keep the run alive
            best = a.get("feed_content") or a.get("summary") or ""
            a["content_note"] = f"error {type(e).__name__}"
            _finish(a, FALLBACK_TAG + best, "fallback")
            print(f"  ! content error for {a.get('url')}: {e}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(safe, articles))

    stats = {"full_feed": 0, "full_page": 0, "fallback": 0}
    reasons: dict[str, int] = {}
    for a in articles:
        stats[a.get("content_status", "fallback")] += 1
        if a.get("content_status") == "fallback":
            r = a.get("content_note") or "unknown"
            reasons[r] = reasons.get(r, 0) + 1
    for r, c in sorted(reasons.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  [content] {c} x {r}")  # run log only, not the email
    return stats
