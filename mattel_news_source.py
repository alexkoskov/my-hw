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
from bs4 import BeautifulSoup

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


def fetch_mattel_article(
    link: str,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
    """Fetch a single Mattel article page and return {title, paragraphs, images}.

    Parses the article page's ``__NEXT_DATA__`` to get the HTML body (from
    ``contentArticle.result.body``), converts it to plain-text paragraphs,
    and collects images from ``contentArticle.thumbnail`` plus the
    ``download_media`` attachments.

    Returns ``None`` on any failure and notifies the admin via ``notifier``.
    """
    http = session or requests
    try:
        response = http.get(
            link,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_SIZE:
            raise MattelNewsError(
                f"Article too large: {len(response.content)} > {MAX_RESPONSE_SIZE}"
            )
        match = _NEXT_DATA_RE.search(response.text)
        if not match:
            raise MattelNewsError("__NEXT_DATA__ not found on article page")
        data = json.loads(match.group(1))
        content_article = data["props"]["pageProps"]["contentArticle"]
        article = content_article["result"]
    except (requests.RequestException, json.JSONDecodeError, KeyError, TypeError,
            MattelNewsError) as exc:
        _notify(notifier, f"Mattel article fetch error ({link}): {exc}")
        return None

    body_html = article.get("body") or ""
    paragraphs: List[str] = []
    if body_html:
        soup = BeautifulSoup(body_html, "html.parser")
        for tag in soup.find_all(["p", "li", "h1", "h2", "h3", "h4"]):
            text = tag.get_text(" ", strip=True)
            if text:
                paragraphs.append(text)

    images: List[str] = []
    thumb_url = (content_article.get("thumbnail") or {}).get("url")
    if thumb_url:
        images.append(thumb_url)
    for media in article.get("download_media") or []:
        url = media.get("url")
        if url and url not in images:
            images.append(url)

    excerpt = article.get("excerpt") or ""
    if isinstance(excerpt, dict):
        excerpt = excerpt.get("text") or ""
    subtitle = str(excerpt).strip()

    return {
        "title": article.get("title", ""),
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
    }
