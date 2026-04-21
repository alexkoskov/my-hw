#!/usr/bin/env python3
"""Autoevolution news source.

Autoevolution article pages are behind Cloudflare, which blocks stock
``requests``. We use ``curl_cffi`` to impersonate a Chrome TLS fingerprint
and scrape the full article body (`div.newstext`) plus article-specific
gallery images. If the scrape fails (network error, layout change, or a
future CF update), the RSS-only ``enrich_entry`` path is used as fallback
— better truncated text than nothing.
"""

import html
import logging
import re
from typing import Dict, List, Optional

try:
    from curl_cffi import requests as curl_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ARTICLE_ID_RE = re.compile(r"-(\d+)\.html")
REQUEST_TIMEOUT = 20
MAX_IMAGES = 10

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


def _scrape_article_page(link: str, fetcher=None) -> Optional[Dict]:
    """Fetch the article page via curl_cffi (Cloudflare bypass) and parse it.

    Returns ``None`` on any failure so the caller can fall back to RSS.
    ``fetcher`` is injectable for tests; otherwise curl_cffi is used.
    """
    if fetcher is None:
        if not _CURL_CFFI_AVAILABLE:
            return None
        fetcher = lambda url: curl_requests.get(
            url, impersonate="chrome", timeout=REQUEST_TIMEOUT
        )

    try:
        response = fetcher(link)
    except Exception as exc:
        logger.warning("Autoevolution scrape failed for %s: %s", link, exc)
        return None
    if response.status_code != 200:
        logger.warning("Autoevolution scrape got HTTP %s for %s", response.status_code, link)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    body = soup.find("div", class_="newstext")
    if body is None:
        logger.warning("Autoevolution article has no .newstext body: %s", link)
        return None

    paragraphs: List[str] = []
    for tag in body.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
        text = tag.get_text(" ", strip=True)
        if text and text != title:
            paragraphs.append(text)
    if not paragraphs:
        return None

    # Only keep gallery images whose filename contains this article's ID —
    # everything else is editor avatars or links to sibling articles.
    m = ARTICLE_ID_RE.search(link)
    article_id = m.group(1) if m else None
    images: List[str] = []
    if article_id:
        id_re = re.compile(rf"-{article_id}[-_]?\d*\.(?:jpe?g|png|webp)", re.IGNORECASE)
        seen = set()
        for img in soup.find_all(["img", "a"]):
            for attr in ("src", "data-src", "href"):
                url = (img.get(attr) or "").strip()
                if not url.startswith("http"):
                    continue
                if not id_re.search(url):
                    continue
                base = url.split("?", 1)[0]
                if base in seen:
                    continue
                seen.add(base)
                images.append(url)
                break
            if len(images) >= MAX_IMAGES:
                break

    return {"title": title, "paragraphs": paragraphs, "images": images}


def fetch_autoevolution_article(entry: dict, fetcher=None) -> Optional[Dict]:
    """Get the best available article payload for an autoevolution entry.

    Tries Cloudflare-bypass scraping first; falls back to RSS-only enrichment
    so the pipeline still posts something even if scraping breaks.
    """
    link = entry.get("link") or ""
    if link:
        scraped = _scrape_article_page(link, fetcher=fetcher)
        if scraped:
            # Prefer RSS media for images if the page didn't yield any
            if not scraped["images"]:
                scraped["images"] = _collect_rss_images(entry)
            return scraped
    return enrich_entry(entry)


def _collect_rss_images(entry: dict) -> List[str]:
    images: List[str] = []
    for field in ("media_content", "media_thumbnail"):
        for m in entry.get(field) or []:
            url = m.get("url") if isinstance(m, dict) else None
            if url and url not in images:
                images.append(url)
    return images


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

    return {
        "title": title,
        "paragraphs": paragraphs,
        "images": _collect_rss_images(entry),
    }
