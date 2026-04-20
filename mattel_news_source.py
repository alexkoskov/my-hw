#!/usr/bin/env python3
"""
Mattel Corporate News source for Hot Wheels-related press releases.

Fetches https://corporate.mattel.com/news, extracts the embedded Next.js JSON
(__NEXT_DATA__), filters entries mentioning Hot Wheels, and returns them in
a feedparser-compatible format for use with news_bot.py pipeline.
"""

import json
import logging
import re
import time
from typing import Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

NEWS_URL = "https://corporate.mattel.com/news"
ARTICLE_URL_PREFIX = "https://corporate.mattel.com/news/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5 MB — guard against oversized responses

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
    re.DOTALL,
)


class MattelNewsError(Exception):
    """Raised when the Mattel news source cannot be processed."""


def _is_hotwheels(entry: dict) -> bool:
    title = (entry.get("title") or "").lower()
    handle = (entry.get("handle") or "").lower()
    return "hot wheels" in title or "hot-wheels" in handle


def _build_entry(raw: dict) -> Optional[dict]:
    handle = raw.get("handle")
    title = raw.get("title")
    if not handle or not title:
        return None

    excerpt = raw.get("excerpt") or ""
    if isinstance(excerpt, dict):
        excerpt = excerpt.get("text") or ""
    summary = str(excerpt).strip() or title

    published_parsed = None
    date_str = raw.get("date")
    if date_str:
        try:
            published_parsed = time.strptime(date_str, "%Y-%m-%d")
        except (TypeError, ValueError):
            logger.warning("Could not parse Mattel date: %s", date_str)

    return {
        "link": ARTICLE_URL_PREFIX + handle,
        "title": title,
        "summary": summary,
        "published_parsed": published_parsed,
        "feed_url": NEWS_URL,
    }


def _extract_entries(html: str) -> List[dict]:
    match = _NEXT_DATA_RE.search(html)
    if not match:
        raise MattelNewsError("__NEXT_DATA__ script tag not found in HTML")

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise MattelNewsError(f"Invalid __NEXT_DATA__ JSON: {exc}") from exc

    try:
        return data["props"]["pageProps"]["page"]["data"]["state"]["article2"]["entries"]
    except (KeyError, TypeError) as exc:
        raise MattelNewsError(
            "Expected path props.pageProps.page.data.state.article2.entries not found"
        ) from exc


def fetch_mattel_news(
    url: str = NEWS_URL,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    """Return Hot Wheels-related entries from Mattel corporate news page.

    Any HTTP, parsing, or structural error is reported via ``notifier`` (if
    provided) and results in an empty list, so the caller's job keeps running.
    """
    http = session or requests
    try:
        response = http.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_SIZE:
            raise MattelNewsError(
                f"Response too large: {len(response.content)} > {MAX_RESPONSE_SIZE}"
            )
        raw_entries = _extract_entries(response.text)
    except requests.RequestException as exc:
        _notify(notifier, f"Mattel news HTTP error: {exc}")
        return []
    except MattelNewsError as exc:
        _notify(notifier, f"Mattel news parsing error: {exc}")
        return []

    entries = []
    for raw in raw_entries:
        if not _is_hotwheels(raw):
            continue
        built = _build_entry(raw)
        if built is not None:
            entries.append(built)

    logger.info("Mattel news: %d Hot Wheels entries found", len(entries))
    return entries


def _notify(notifier, message: str) -> None:
    logger.error(message)
    if notifier is None:
        return
    try:
        notifier(message)
    except Exception:
        logger.exception("Failed to send admin notification")
