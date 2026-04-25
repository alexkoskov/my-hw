#!/usr/bin/env python3
"""
Mattel Corporate News source for Hot Wheels-related press releases.

Fetches https://corporate.mattel.com/news, extracts the embedded Next.js
RSC streaming-payload (``self.__next_f.push([1, "..."])``), filters entries
mentioning Hot Wheels, and returns them in a feedparser-compatible format
for use with the news_bot.py pipeline.

Migration note (2026-04): Mattel moved to the Next.js App Router; the legacy
``<script id="__NEXT_DATA__">`` JSON island disappeared. The same listing
data (``article2.entries`` with ``handle``, ``title``, ``date``, ``excerpt``,
``seo_description``, ``thumbnail`` fields) lives inside the streaming
payload now. Article bodies are referenced via ``body: "$<row-id>"`` and
resolved by reading a separate text-row marker ``<row-id>:T<hex-len>,<content>``
that may span multiple chunks of the streaming payload.

Public surface (preserved from pre-migration contract):
- ``NEWS_URL``, ``ARTICLE_URL_PREFIX``, ``MAX_RESPONSE_SIZE`` constants
- ``MattelNewsError`` exception class
- ``fetch_mattel_news(url, session, notifier) -> List[dict]``
- ``fetch_mattel_article(link, session, notifier) -> Optional[dict]``
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple

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

# Module-level compiled regex (Decision 8 control 2). Character-class form
# (?:[^"\\]|\\.)* matches a JS-string literal in linear time and correctly
# handles escaped quotes (\") inside the body. Avoids the catastrophic
# backtracking risk of the lazy `.+?` form. re.DOTALL is unnecessary with
# the explicit character class but kept for symmetry with how Next.js
# emits literals containing real newlines via \n escapes (no raw newlines
# inside the literal — the class permits anything except an unescaped ").
_FLIGHT_PUSH_RE = re.compile(
    r'self\.__next_f\.push\(\[\s*1\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\)'
)

_ARTICLE2_ANCHOR = '"article2":{"entries":['


class MattelNewsError(Exception):
    """Raised when the Mattel news source cannot be processed."""


# ---------------------------------------------------------------------------
# Helper: Hot Wheels filter and entry-dict assembly (preserved 1:1)
# ---------------------------------------------------------------------------


def _is_hotwheels(entry: dict) -> bool:
    title = (entry.get("title") or "").lower()
    handle = (entry.get("handle") or "").lower()
    return "hot wheels" in title or "hot-wheels" in handle


def _excerpt_to_str(excerpt) -> str:
    """Normalise the ``excerpt`` field — supports both string and the
    ``{"text": "..."}`` dict-form some Contentstack records emit."""
    if isinstance(excerpt, dict):
        return str(excerpt.get("text") or "").strip()
    if excerpt is None:
        return ""
    return str(excerpt).strip()


def _build_entry(raw: dict) -> Optional[dict]:
    handle = raw.get("handle")
    title = raw.get("title")
    if not handle or not title:
        return None

    excerpt_str = _excerpt_to_str(raw.get("excerpt"))
    summary = excerpt_str or title

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


# ---------------------------------------------------------------------------
# Flight-payload pipeline (RSC streaming)
# ---------------------------------------------------------------------------


def _iter_flight_payloads(html: str) -> Iterator[str]:
    """Yield each ``self.__next_f.push([1, "..."])`` body, JS-string-unescaped.

    Decode failures (a malformed JS string literal) are swallowed — the
    parser's job is to find the largest well-formed payload, not to validate
    every push.
    """
    for raw in _FLIGHT_PUSH_RE.findall(html):
        try:
            # json.loads handles \n, \", \\, \uXXXX exactly as JS would.
            # Wrapped in (json.JSONDecodeError, RecursionError, ValueError)
            # per Decision 8 control 3.
            yield json.loads('"' + raw + '"')
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue


def _concat_flight(html: str) -> str:
    """Concatenate all unescaped flight-push bodies in document order.

    Required for body-row resolution (AC8) — a ``<row>:T<hex>,<content>``
    marker may begin in one push and continue in the next.
    """
    return "".join(_iter_flight_payloads(html))


def _find_entries_slice(unescaped: str) -> Tuple[int, int]:
    """Locate the ``article2.entries`` array in ``unescaped`` and return
    ``(start, end)`` byte offsets where ``unescaped[start:end]`` is the
    JSON array literal (including the outer brackets).

    The bracket-match is depth-aware AND string-literal-aware so embedded
    ``[``/``]`` inside string values (e.g., URLs containing ``[``) don't
    confuse depth tracking. Decision 8 control 4.

    Raises ``MattelNewsError("article2.entries not found")`` if the anchor
    is missing or the array is unterminated.
    """
    anchor_pos = unescaped.find(_ARTICLE2_ANCHOR)
    if anchor_pos < 0:
        raise MattelNewsError("article2.entries not found")

    # The bracket of the array starts at the last char of the anchor.
    arr_start = anchor_pos + len(_ARTICLE2_ANCHOR) - 1
    if arr_start >= len(unescaped) or unescaped[arr_start] != "[":
        raise MattelNewsError("article2.entries not found")

    depth = 0
    in_string = False
    escape = False
    n = len(unescaped)
    i = arr_start
    # Capped at len(unescaped) — bracket-match safety, Decision 8 ctl 4.
    while i < n:
        c = unescaped[i]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return arr_start, i + 1
        i += 1

    raise MattelNewsError("article2.entries not found")


def _decode_entries(unescaped: str) -> List[dict]:
    """Slice + JSON-decode the ``article2.entries`` array."""
    arr_start, arr_end = _find_entries_slice(unescaped)
    slice_text = unescaped[arr_start:arr_end]
    try:
        decoded = json.loads(slice_text)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise MattelNewsError(
            f"invalid JSON in entries array: {type(exc).__name__}"
        ) from exc
    if not isinstance(decoded, list):
        raise MattelNewsError("article2.entries not found")
    return decoded


def _extract_listing_entries(html: str) -> List[dict]:
    """Listing-page pipeline: scan all pushes, decode the one carrying the
    ``article2.entries`` anchor, return the raw entry list.

    Raises:
        - ``MattelNewsError("flight payload not found")`` if zero pushes.
        - ``MattelNewsError("article2.entries not found")`` if no push
          carries the anchor or the array is malformed.
        - ``MattelNewsError("invalid JSON in entries array: <type>")`` if
          the slice fails to decode.
    """
    payloads = list(_iter_flight_payloads(html))
    if not payloads:
        raise MattelNewsError("flight payload not found")

    # Anchor on a semantic marker, not the largest chunk (Decision 2).
    last_error: Optional[MattelNewsError] = None
    for payload in payloads:
        if _ARTICLE2_ANCHOR not in payload:
            continue
        try:
            return _decode_entries(payload)
        except MattelNewsError as exc:
            # Remember the most informative error and keep scanning — most
            # listing pages have exactly one push with the anchor, but if
            # an empty/malformed one appears earlier we'd rather report the
            # decode failure from the rich one than a structural miss.
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise MattelNewsError("article2.entries not found")


def _extract_entries(html: str) -> List[dict]:
    """Public-by-test thin wrapper around ``_extract_listing_entries``.

    Kept under its old name + signature so test_mattel_news_source.py's
    direct import (Decision 3) doesn't break.
    """
    return _extract_listing_entries(html)


def _slug_from_link(link: str) -> str:
    return link.rsplit("/", 1)[-1].split("?", 1)[0]


def _find_entry_by_handle_or_url(entries: List[dict], link: str) -> Optional[dict]:
    """Locate the matching entry by ``handle`` (URL slug) with ``url``-field
    fallback (AC7).
    """
    slug = _slug_from_link(link)
    for entry in entries:
        if entry.get("handle") == slug:
            return entry
    for entry in entries:
        url = entry.get("url") or ""
        if url == link or (url and url.endswith("/" + slug)):
            return entry
    return None


def _extract_article_entry(html: str, link: str) -> Tuple[dict, str]:
    """Article-page pipeline: concatenate all flight pushes, locate the
    article2.entries array, find the entry matching the link, and return
    ``(entry, concatenated_unescaped_payload)``.

    The concat is returned alongside the entry so the caller can resolve
    the entry's ``body: "$<row-id>"`` reference against the same stream
    without re-parsing.
    """
    concat = _concat_flight(html)
    if not concat:
        raise MattelNewsError("flight payload not found")
    entries = _decode_entries(concat)
    entry = _find_entry_by_handle_or_url(entries, link)
    if entry is None:
        slug = _slug_from_link(link)
        raise MattelNewsError(
            f"article entry not found for handle: {slug}"
        )
    return entry, concat


# ---------------------------------------------------------------------------
# Body-row resolution (RSC text-rows)
# ---------------------------------------------------------------------------


def _resolve_body_html(concat: str, body_ref: str) -> str:
    """Resolve a ``"$<row-id>"`` body reference into the row's literal HTML.

    Returns ``""`` (content-empty path, ES9c, AC9) on every failure mode:
        - ``body_ref`` not in the expected ``$<row-id>`` shape;
        - row-id marker not present in the concatenated stream;
        - advertised hex-length exceeds available content (truncated);
        - advertised hex-length above ``MAX_RESPONSE_SIZE`` (Decision 8
          control 4 — guards against attacker-supplied huge slice).
    """
    if not isinstance(body_ref, str) or not body_ref.startswith("$"):
        return ""
    row_id = body_ref[1:]
    if not row_id.isdigit():
        return ""

    # Find "<row_id>:T<hex>," — match at start of concat or after any
    # non-digit so we don't accidentally hit "53" inside "153".
    pattern = re.compile(
        r'(?:^|[^0-9])' + re.escape(row_id) + r':T([0-9a-fA-F]+),'
    )
    match = pattern.search(concat)
    if not match:
        return ""

    hex_len = match.group(1)
    try:
        length = int(hex_len, 16)
    except ValueError:
        return ""

    # Decision 8 control 4: cap advertised length at MAX_RESPONSE_SIZE so
    # an adversarial "ffffffff" (~4 GB) cannot trigger a huge slice.
    if length < 0 or length > MAX_RESPONSE_SIZE:
        return ""

    body_start = match.end()
    body_end = body_start + length
    if body_end > len(concat):
        # Truncated: advertised length exceeds available content. AC9 path.
        return ""

    return concat[body_start:body_end]


def _paragraphs_from_body(body_html: str) -> List[str]:
    """Walk the BS4 tree and emit paragraph-like text (p, li, h1-h4)."""
    if not body_html:
        return []
    soup = BeautifulSoup(body_html, "html.parser")
    out: List[str] = []
    for tag in soup.find_all(["p", "li", "h1", "h2", "h3", "h4"]):
        text = tag.get_text(" ", strip=True)
        if text:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# Notifier (sanitised — Decision 8 control 5)
# ---------------------------------------------------------------------------


def _notify(notifier, message: str) -> None:
    logger.error(message)
    if notifier is None:
        return
    try:
        notifier(message)
    except Exception:
        logger.exception("Failed to send admin notification")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
            allow_redirects=False,  # Decision 8 control 6
        )
        response.raise_for_status()
        # Enforce size guard BEFORE any regex/JSON parsing (Decision 8).
        if len(response.content) > MAX_RESPONSE_SIZE:
            raise MattelNewsError(
                f"response too large: {len(response.content)} bytes"
            )
        raw_entries = _extract_listing_entries(response.text)
    except requests.RequestException as exc:
        # Sanitised: type only, no str(exc) (Decision 8 control 5).
        _notify(notifier, f"Mattel news HTTP error: {type(exc).__name__}")
        return []
    except MattelNewsError as exc:
        msg = str(exc)
        if msg.startswith("response too large:"):
            _notify(notifier, f"Mattel news {msg}")
        else:
            _notify(notifier, f"Mattel news parsing error: {msg}")
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


def fetch_mattel_article(
    link: str,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
    """Fetch a single Mattel article page and return ``{title, subtitle,
    paragraphs, images}``.

    Reads the entry's listing-shaped metadata from the article page's flight
    payload (article pages render the listing block in addition to the
    article-specific entry), locates the entry by URL slug (with ``url``-field
    fallback), resolves the entry's ``body`` reference into a text-row,
    walks the body HTML for paragraphs, and applies the thumbnail-only
    image policy.

    Returns ``None`` on any structural failure and notifies the admin via
    ``notifier``. The body-row resolution failure modes (missing marker,
    truncated content, advertised length > cap) are NOT considered errors —
    they yield ``paragraphs=[]`` per AC9.
    """
    # SSRF guard (Decision 8 control 1, ES10) — runs BEFORE any HTTP call.
    # Note: link is intentionally NOT echoed in the notifier message to avoid
    # amplifying a malicious URL into the admin chat.
    if not isinstance(link, str) or not link.startswith(ARTICLE_URL_PREFIX):
        _notify(notifier, "Mattel article fetch error: invalid article link prefix")
        return None

    http = session or requests
    try:
        response = http.get(
            link,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,  # Decision 8 control 6
        )
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_SIZE:
            raise MattelNewsError(
                f"response too large: {len(response.content)} bytes"
            )
        entry, concat = _extract_article_entry(response.text, link)
    except requests.RequestException as exc:
        _notify(
            notifier,
            f"Mattel article fetch error ({link}): {type(exc).__name__}",
        )
        return None
    except MattelNewsError as exc:
        msg = str(exc)
        if msg.startswith("response too large:"):
            _notify(notifier, f"Mattel article {msg}")
        else:
            _notify(notifier, f"Mattel article fetch error ({link}): {msg}")
        return None

    # Body resolution — content-empty failures are NOT notified (AC9 / ES9c).
    body_ref = entry.get("body")
    if isinstance(body_ref, str) and body_ref.startswith("$"):
        body_html = _resolve_body_html(concat, body_ref)
    else:
        body_html = ""
    paragraphs = _paragraphs_from_body(body_html)

    # Image policy: thumbnail only. ``download_media`` is a press-kit field
    # (logos in multiple formats + hi-res press photos); surfacing those on
    # the Telegraph page would inflate the article with figures absent from
    # the source. If a future Mattel article uses inline imagery, the right
    # path is parsing <img> tags out of body_html — never download_media.
    images: List[str] = []
    thumb = entry.get("thumbnail") or {}
    if isinstance(thumb, dict):
        url = thumb.get("url")
        if url:
            images.append(url)

    subtitle = _excerpt_to_str(entry.get("excerpt"))

    return {
        "title": entry.get("title", "") or "",
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
    }
