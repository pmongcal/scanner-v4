#!/usr/bin/env python3
"""
Email the daily digest as a CSV attachment via Gmail SMTP.

CSV columns:
  Date retrieved | Feed name | Article name | Short description | Content

Date retrieved is in Philippine time (UTC+8). The file is UTF-8 with a BOM so
Excel opens the Chinese text correctly. Google Sheets reads it as is.

The CSV holds every article first seen since the last successful email (read
from pending_digest.json). pending_digest.json is cleared only after the send
succeeds, so a failed send goes out with the next run.

Secrets from env (GitHub Actions secrets):
  SMTP_HOST           default: smtp.gmail.com
  SMTP_PORT           default: 587
  SMTP_USER           (required) Gmail address
  SMTP_PASSWORD       (required) Gmail app password
  SMTP_TO             (required) recipient
  SMTP_FROM_NAME      default: Scanner V4
"""

from __future__ import annotations

import csv
import io
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from extract_content import truncate_bytes

ROOT = Path(__file__).resolve().parent
ARTICLES_PATH = ROOT / "articles.json"
PENDING_PATH = ROOT / "pending_digest.json"

PHT = timezone(timedelta(hours=8))
CSV_HEADER = ["Date retrieved", "Feed name", "Article name", "Short description", "Content"]
FALLBACK_TAG = "[FULL TEXT NOT RETRIEVED]"
MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024  # Gmail's limit is 25 MB


def to_pht(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PHT).strftime("%Y-%m-%d %H:%M")


def feed_state(st: dict) -> str:
    return st.get("state") or ("working" if st.get("ok") else "failed")


def build_csv(articles: list[dict], fallback_time: str | None) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for a in articles:
        content = a.get("content")
        if content is None:  # article stored before full text existed
            content = a.get("summary", "")
        content = truncate_bytes(content)  # safety net: 8 KB per cell
        w.writerow([
            to_pht(a.get("retrieved_at") or fallback_time),
            a.get("source", ""),
            a.get("title", ""),
            a.get("summary", ""),
            content,
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def build_body(articles: list[dict], feed_status: dict, csv_name: str, csv_size: int) -> str:
    full = sum(1 for a in articles if a.get("content_status") in ("full_feed", "full_page"))
    fallback = sum(1 for a in articles if a.get("content_status") == "fallback")
    other = len(articles) - full - fallback

    lines = [
        f"Attached: {csv_name} ({csv_size / 1024:.0f} KB)",
        f"Articles: {len(articles)}",
        f"Full text retrieved: {full}",
        f"Description only (rows start with {FALLBACK_TAG}): {fallback}",
    ]
    if other:
        lines.append(f"Stored before full text existed: {other}")

    working = sum(1 for s in feed_status.values() if feed_state(s) == "working")
    lines.append(f"Feeds working: {working}/{len(feed_status)}")
    for state in ("failed", "empty", "stale"):
        names = sorted(n for n, s in feed_status.items() if feed_state(s) == state)
        if names:
            lines.append(f"{state.capitalize()} feeds: " + ", ".join(names))

    if fallback:
        by_feed: dict[str, int] = {}
        for a in articles:
            if a.get("content_status") == "fallback":
                by_feed[a.get("source", "?")] = by_feed.get(a.get("source", "?"), 0) + 1
        top = sorted(by_feed.items(), key=lambda kv: -kv[1])[:8]
        lines.append("Most description-only rows by feed: " + ", ".join(f"{n} ({c})" for n, c in top))
    return "\n".join(lines)


def main() -> int:
    if not ARTICLES_PATH.exists():
        print(f"[send_email] FATAL: {ARTICLES_PATH} not found", file=sys.stderr)
        return 2

    with ARTICLES_PATH.open() as f:
        meta = json.load(f)

    articles: list[dict] = []
    if PENDING_PATH.exists():
        try:
            with PENDING_PATH.open() as f:
                articles = json.load(f).get("articles", [])
        except Exception:
            articles = []

    feed_status = meta.get("feed_status", {})
    csv_bytes = build_csv(articles, meta.get("generated_at"))
    if len(csv_bytes) > MAX_ATTACHMENT_BYTES:
        print(f"[send_email] FATAL: CSV is {len(csv_bytes)} bytes, over the Gmail limit. "
              "Nothing was sent and the digest stays pending.", file=sys.stderr)
        return 2

    today = datetime.now(PHT).strftime("%Y-%m-%d")
    csv_name = f"scanner-{today}.csv"
    subject = f"[scanner-v4] {today} | {len(articles)} articles"
    body = build_body(articles, feed_status, csv_name, len(csv_bytes))

    smtp_host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    smtp_to = os.environ.get("SMTP_TO")
    smtp_from_name = os.environ.get("SMTP_FROM_NAME") or "Scanner V4"

    missing = [k for k, v in {
        "SMTP_USER": smtp_user,
        "SMTP_PASSWORD": smtp_password,
        "SMTP_TO": smtp_to,
    }.items() if not v]
    if missing:
        print(f"[send_email] FATAL: missing env vars: {missing}", file=sys.stderr)
        return 2

    msg = MIMEMultipart("mixed")
    msg["From"] = f"{smtp_from_name} <{smtp_user}>"
    msg["To"] = smtp_to
    msg["Subject"] = subject
    msg["X-Scanner-V4"] = "1"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    part = MIMEApplication(csv_bytes, _subtype="csv", Name=csv_name)
    part.add_header("Content-Disposition", "attachment", filename=csv_name)
    msg.attach(part)

    print(f"[send_email] connecting {smtp_host}:{smtp_port} as {smtp_user}")
    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as s:
        s.starttls()
        s.login(smtp_user, smtp_password)
        s.send_message(msg)
    print(f"[send_email] sent \"{subject}\" to {smtp_to} with {csv_name}")

    # Clear the pending digest only after a successful send.
    with PENDING_PATH.open("w") as f:
        json.dump({"articles": [], "runs": []}, f, indent=2)
    print("[send_email] cleared pending_digest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
