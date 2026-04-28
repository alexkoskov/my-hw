#!/usr/bin/env python3
"""Lamley Group news source.

lamleygroup.com is a WordPress blog whose RSS carries only a short excerpt
(~100 chars). `fetch_lamley_article` scrapes the individual article page,
extracts the full body paragraphs and images (capped to keep Telegraph
pages reasonable).

Rate limiting: lamleygroup.com's WordPress install rate-limits fast bursts
(observed: ~10 requests/sec triggers HTTP 429). Two-layer mitigation:
  * Module-level throttle (`_MIN_REQUEST_INTERVAL_S`) — a soft client-side
    floor between consecutive fetches in the same process.
  * On 429 — sleep `Retry-After` (or a fallback) and retry ONCE before
    giving up; subsequent failures still surface to the operator.
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

from boilerplate_filter import filter_boilerplate

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
MAX_RESPONSE_SIZE = 5 * 1024 * 1024
IMAGE_LIMIT = 10

#: Minimum gap between two consecutive Lamley fetches in the same process.
#: 2 s keeps us well under any typical WAF rate-limit while not noticeably
#: slowing the daily 12:00 МСК cron tick (10 articles → +20 s, deep within
#: the publish window).
_MIN_REQUEST_INTERVAL_S = 2.0

#: How long to wait when a 429 response arrives without a usable
#: ``Retry-After`` header.
_DEFAULT_RETRY_AFTER_S = 30.0

#: Throttle state. ``threading.Lock`` is sufficient because the cron-side
#: bot is single-threaded; the lock is defensive only.
_throttle_lock = threading.Lock()
_last_request_time: float = 0.0


def _throttle_wait() -> None:
    """Block until ``_MIN_REQUEST_INTERVAL_S`` has elapsed since the
    previous fetch. Updates the timestamp to "now" on return."""
    global _last_request_time
    with _throttle_lock:
        elapsed = time.monotonic() - _last_request_time
        gap = _MIN_REQUEST_INTERVAL_S - elapsed
        if gap > 0:
            logger.debug("lamley throttle: sleeping %.1fs", gap)
            time.sleep(gap)
        _last_request_time = time.monotonic()


def _parse_retry_after(value: Optional[str]) -> float:
    """Best-effort ``Retry-After`` header parser.

    Server may return either delta-seconds (``"30"``) or HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``). We support delta-seconds and
    fall back to ``_DEFAULT_RETRY_AFTER_S`` for anything else.
    """
    if not value:
        return _DEFAULT_RETRY_AFTER_S
    try:
        secs = float(value.strip())
    except ValueError:
        return _DEFAULT_RETRY_AFTER_S
    # Cap upper bound so a server-side typo cannot lock the bot for hours.
    return max(1.0, min(secs, 120.0))


def _notify(notifier, message: str) -> None:
    logger.error(message)
    if notifier is None:
        return
    try:
        notifier(message)
    except Exception:
        logger.exception("Failed to send admin notification")


def fetch_lamley_article(
    link: str,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
    """Scrape a lamleygroup.com article page.

    Returns ``{'title', 'paragraphs', 'images'}`` or ``None`` on failure.
    """
    http = session or requests

    def _do_fetch():
        return http.get(
            link,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )

    # Soft client-side throttle BEFORE the first attempt so a tight call
    # loop doesn't hammer Lamley's WordPress.
    _throttle_wait()
    try:
        response = _do_fetch()
        # On 429, honour Retry-After (or fall back) and try ONE more time.
        if response.status_code == 429:
            retry_s = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "Lamley 429 for %s — sleeping %.1fs before retry",
                link, retry_s,
            )
            time.sleep(retry_s)
            response = _do_fetch()
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_SIZE:
            _notify(notifier, f"Lamley article too large: {len(response.content)}")
            return None
    except requests.RequestException as exc:
        _notify(notifier, f"Lamley fetch error ({link}): {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    # Strip inline scripts/styles so their JS doesn't end up in body text.
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    title_tag = soup.find("h1", class_="entry-title") or soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    body = (
        soup.find("div", class_="entry-content")
        or soup.find("article")
    )
    if body is None:
        _notify(notifier, f"Lamley article has no recognizable body: {link}")
        return None

    paragraphs: List[str] = []
    for tag in body.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
        text = tag.get_text(" ", strip=True)
        if text and text != title:
            paragraphs.append(text)

    # Strip UI-boilerplate (social-share, "Subscribe", "Read more", etc.)
    # BEFORE picking the subtitle so the editorial lead is real prose.
    paragraphs = filter_boilerplate(paragraphs)

    # Lamley RSS has no subtitle field; use the first body paragraph as the
    # editorial lead on the Telegraph page. Drop it from the body so it
    # doesn't repeat below the decorated lead.
    subtitle = paragraphs[0] if paragraphs else ""
    paragraphs = paragraphs[1:]

    images: List[str] = []
    seen_bases = set()
    for img in body.find_all("img"):
        src = img.get("src") or ""
        if not src.startswith("http"):
            continue
        base = src.split("?", 1)[0]
        if base in seen_bases:
            continue
        seen_bases.add(base)
        images.append(src)
        if len(images) >= IMAGE_LIMIT:
            break

    return {
        "title": title,
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
    }
