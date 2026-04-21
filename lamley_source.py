#!/usr/bin/env python3
"""Lamley Group news source.

lamleygroup.com is a WordPress blog whose RSS carries only a short excerpt
(~100 chars). `fetch_lamley_article` scrapes the individual article page,
extracts the full body paragraphs and images (capped to keep Telegraph
pages reasonable).
"""

import logging
from typing import Callable, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
MAX_RESPONSE_SIZE = 5 * 1024 * 1024
IMAGE_LIMIT = 10


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
    try:
        response = http.get(
            link,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_SIZE:
            _notify(notifier, f"Lamley article too large: {len(response.content)}")
            return None
    except requests.RequestException as exc:
        _notify(notifier, f"Lamley fetch error ({link}): {exc}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
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
        "paragraphs": paragraphs,
        "images": images,
    }
