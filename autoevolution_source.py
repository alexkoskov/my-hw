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
ARTICLE_SLUG_RE = re.compile(r"/news/([^/?#]+?)-\d+\.html")
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/watch\?v=|youtube\.com/embed/)"
    r"([A-Za-z0-9_-]+)"
)
VIMEO_ID_RE = re.compile(r"vimeo\.com/(\d+)")
REQUEST_TIMEOUT = 20
MAX_IMAGES = 10


def _video_embed_url(href: str) -> Optional[str]:
    """Translate a YouTube/Vimeo link into a Telegraph-compatible embed URL."""
    m = YOUTUBE_ID_RE.search(href)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    m = VIMEO_ID_RE.search(href)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    return None

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

    subtitle = ""
    for cand in soup.find_all("div"):
        cls = set(cand.get("class") or [])
        if {"mgtop_10", "mgbot_10", "fsz19"}.issubset(cls):
            subtitle = cand.get_text(" ", strip=True)
            break

    body = soup.find("div", class_="newstext")
    if body is None:
        logger.warning("Autoevolution article has no .newstext body: %s", link)
        return None

    # Article slug (everything in the URL path after /news/ but before the
    # numeric ID). Used to filter inline gallery images to this article only.
    slug_m = ARTICLE_SLUG_RE.search(link)
    slug = slug_m.group(1) if slug_m else ""

    # Hero image — sits outside .newstext in `div.ch_pic.mainpic > a > img`.
    hero_src = None
    hero_container = soup.find("div", class_="ch_pic")
    if hero_container:
        img = hero_container.find("img")
        if img and (img.get("src") or "").startswith("http"):
            hero_src = img.get("src")

    blocks: List[Dict] = []
    seen_media = set()

    # Walk direct children of .newstext in DOM order so images/videos land at
    # their original positions between paragraphs.
    for child in body.children:
        if not getattr(child, "name", None):
            continue
        cls = " ".join(child.get("class") or [])
        if ("ad" in cls and ("intext" in cls or "ad300" in cls)) or "clearfix" in cls:
            continue

        # Bold lead intro — autoevolution uses `div.sanscond.fsz22.bold`
        if child.name == "div" and ("fsz22" in cls or "sanscond" in cls):
            text = child.get_text(" ", strip=True)
            if text:
                blocks.append({"type": "lead", "text": text})
            continue

        # Media inside this child: <a> wrappers carry the authoritative URL
        # (full-size gallery link or YouTube/Vimeo).
        for anchor in child.find_all("a", href=True):
            href = anchor["href"]
            embed = _video_embed_url(href)
            if embed:
                if embed not in seen_media:
                    seen_media.add(embed)
                    blocks.append({"type": "video", "src": embed})
                continue
            if IMAGE_EXT_RE.search(href) and slug and slug in href:
                base = href.split("?", 1)[0]
                if base in seen_media:
                    continue
                seen_media.add(base)
                blocks.append({"type": "image", "src": href})

        # Direct <img> not wrapped in <a> — rare; skip thumbnails / avatars.
        for img in child.find_all("img"):
            if img.find_parent("a"):
                continue
            src = img.get("src") or img.get("data-src") or ""
            if not src.startswith("http"):
                continue
            if "editors/" in src or "_img/" in src or "130x" in src:
                continue
            if slug and slug in src:
                base = src.split("?", 1)[0]
                if base in seen_media:
                    continue
                seen_media.add(base)
                blocks.append({"type": "image", "src": src})

        # Paragraph / heading text
        if child.name == "p":
            text = child.get_text(" ", strip=True)
            if text:
                blocks.append({"type": "paragraph", "text": text})
        elif child.name in ("h2", "h3", "h4"):
            text = child.get_text(" ", strip=True)
            if text:
                blocks.append({
                    "type": "heading",
                    "text": text,
                    "level": int(child.name[1]),
                })

    # Prepend the hero image so it becomes the Telegraph preview thumbnail.
    if hero_src and hero_src.split("?", 1)[0] not in seen_media:
        blocks.insert(0, {"type": "image", "src": hero_src})

    if not blocks:
        return None

    # Back-compat flat lists (some callers still expect paragraphs/images).
    paragraphs = [
        b["text"] for b in blocks
        if b["type"] in ("lead", "paragraph", "heading")
    ]
    images = [b["src"] for b in blocks if b["type"] == "image"][:MAX_IMAGES]

    return {
        "title": title,
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
        "blocks": blocks,
    }


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

    # RSS doesn't carry a dedicated subtitle; leave it empty so the Telegraph
    # page skips the decorated lead paragraph + hr for RSS-only articles.
    return {
        "title": title,
        "subtitle": "",
        "paragraphs": paragraphs,
        "images": _collect_rss_images(entry),
    }
