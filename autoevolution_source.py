#!/usr/bin/env python3
"""Autoevolution news source.

The autoevolution article pages are behind Cloudflare, so we can't scrape
them from a Python bot. What we get is the RSS feed (``rss/tag-Hot+Wheels``)
which provides a *truncated* summary ending with ``...(continue reading...)``
plus ``media_content`` and ``media_thumbnail`` images.

`enrich_entry` turns an RSS entry into the ``{title, paragraphs, images}``
shape the bot's pipeline expects — stripping the "continue reading" tail,
decoding entities, and splitting into paragraphs.
"""

import html
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CONTINUE_READING_RE = re.compile(
    r"\s*\(<a[^>]*>\s*continue reading[^<]*</a>\)\s*$",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _clean_summary(summary: str) -> str:
    text = _CONTINUE_READING_RE.sub("", summary)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return text.strip()


def enrich_entry(entry: dict) -> Optional[Dict]:
    """Build {title, paragraphs, images} from an autoevolution RSS entry."""
    title = entry.get("title") or ""
    summary = entry.get("summary") or entry.get("description") or ""
    if not title and not summary:
        return None

    body = _clean_summary(summary)
    paragraphs: List[str] = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]
    if not paragraphs and body:
        paragraphs = [body]
    if not paragraphs:
        paragraphs = [title]

    images: List[str] = []
    for media_field in ("media_content", "media_thumbnail"):
        for m in entry.get(media_field) or []:
            url = m.get("url") if isinstance(m, dict) else None
            if url and url not in images:
                images.append(url)

    return {
        "title": title,
        "paragraphs": paragraphs,
        "images": images,
    }
