#!/usr/bin/env python3
"""Publish articles to Telegra.ph.

Exposes `publish_article(title, paragraphs, images, source_url)` that returns
a Telegra.ph URL. Access token is created on first call via
`ensure_access_token()` and cached in the `.env` file so subsequent runs
reuse it.
"""

import json
import logging
import os
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegra.ph"
DEFAULT_SHORT_NAME = "hwnews"
DEFAULT_AUTHOR_NAME = "Hot Wheels News"
REQUEST_TIMEOUT = 15
ENV_TOKEN_KEY = "TELEGRAPH_ACCESS_TOKEN"


class TelegraphError(Exception):
    """Raised when a Telegra.ph API call fails."""


def _api_call(method: str, data: dict, session: Optional[requests.Session] = None) -> dict:
    http = session or requests
    resp = http.post(f"{API_BASE}/{method}", data=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise TelegraphError(f"{method} failed: {body.get('error')}")
    return body["result"]


def create_account(
    short_name: str = DEFAULT_SHORT_NAME,
    author_name: str = DEFAULT_AUTHOR_NAME,
    session: Optional[requests.Session] = None,
) -> str:
    """Register a new anonymous Telegra.ph account; return its access_token."""
    result = _api_call(
        "createAccount",
        {"short_name": short_name, "author_name": author_name},
        session=session,
    )
    return result["access_token"]


def _save_token_to_env(env_path: str, token: str) -> None:
    """Append or update TELEGRAPH_ACCESS_TOKEN in the .env file."""
    lines: List[str] = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()
    updated = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{ENV_TOKEN_KEY}="):
            lines[i] = f"{ENV_TOKEN_KEY}={token}\n"
            updated = True
            break
    if not updated:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{ENV_TOKEN_KEY}={token}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)


def ensure_access_token(
    env_path: str = ".env",
    session: Optional[requests.Session] = None,
) -> str:
    """Return a Telegra.ph access token, creating and persisting one if needed."""
    token = os.environ.get(ENV_TOKEN_KEY)
    if token:
        return token
    token = create_account(session=session)
    os.environ[ENV_TOKEN_KEY] = token
    try:
        _save_token_to_env(env_path, token)
    except OSError as exc:
        logger.warning("Could not persist Telegraph token to %s: %s", env_path, exc)
    logger.info("Created new Telegraph account; token stored in %s", env_path)
    return token


def _build_content_from_blocks(
    subtitle: str,
    blocks: List[dict],
    source_url: Optional[str],
) -> list:
    """Render ordered content blocks into Telegra.ph nodes, preserving
    image/video positions from the source article.

    Block shapes:
        {'type': 'paragraph', 'text': str}
        {'type': 'lead', 'text': str}          # bold intro
        {'type': 'heading', 'text': str, 'level': 3|4}
        {'type': 'image', 'src': str}
        {'type': 'video', 'src': str}          # embed URL

    The first ``image`` block becomes the hero figure so it drives the
    Telegram preview thumbnail; the rest appear in their original positions.
    """
    def p(*children): return {"tag": "p", "children": list(children)}

    def figure_img(src, caption=""):
        children = [{"tag": "img", "attrs": {"src": src}}]
        if caption:
            children.append({"tag": "figcaption", "children": [caption]})
        return {"tag": "figure", "children": children}

    def iframe(src): return {"tag": "iframe", "attrs": {"src": src}}
    def a(href, text): return {"tag": "a", "attrs": {"href": href}, "children": [text]}
    def i_(text): return {"tag": "i", "children": [text]}
    def b_(text): return {"tag": "b", "children": [text]}
    def heading(level, text):
        lvl = level if level in (3, 4) else 3
        return {"tag": f"h{lvl}", "children": [text]}

    first_image_idx = next(
        (i for i, b in enumerate(blocks) if b.get("type") == "image"),
        None,
    )

    nodes: list = []
    if first_image_idx is not None:
        hero = blocks[first_image_idx]
        nodes.append(figure_img(hero["src"], hero.get("caption", "")))

    if subtitle:
        nodes.append(p(i_(f"💬 «{subtitle}»")))
        nodes.append({"tag": "hr"})

    for i, block in enumerate(blocks):
        if i == first_image_idx:
            continue
        t = block.get("type")
        if t == "paragraph":
            nodes.append(p(block["text"]))
        elif t == "lead":
            nodes.append(p(b_(block["text"])))
        elif t == "heading":
            nodes.append(heading(block.get("level", 3), block["text"]))
        elif t == "image":
            nodes.append(figure_img(block["src"], block.get("caption", "")))
        elif t == "video":
            nodes.append(iframe(block["src"]))

    if source_url:
        nodes.append(p(i_("Источник: "), a(source_url, source_url)))
    return nodes


def _build_content(
    subtitle: str,
    paragraphs: List[str],
    images: List[str],
    source_url: Optional[str],
) -> list:
    """Compose the Telegra.ph node tree for the locked post format:

    1. hero figure (first image),
    2. decorated subtitle `p(italic("💬 «{subtitle}»"))` — editorial lead,
    3. `hr` separator between lead and body,
    4. body paragraphs with images interleaved every 3rd paragraph,
    5. any trailing images,
    6. italic "Источник: <link>" footer.

    If ``subtitle`` is empty, steps 2 and 3 are skipped.
    """
    def p(*children):
        return {"tag": "p", "children": list(children)}

    def figure_img(src):
        return {"tag": "figure", "children": [{"tag": "img", "attrs": {"src": src}}]}

    def a(href, text):
        return {"tag": "a", "attrs": {"href": href}, "children": [text]}

    def i_(text):
        return {"tag": "i", "children": [text]}

    nodes: list = []
    remaining = list(images or [])
    if remaining:
        nodes.append(figure_img(remaining.pop(0)))

    if subtitle:
        nodes.append(p(i_(f"💬 «{subtitle}»")))
        nodes.append({"tag": "hr"})

    for i, para in enumerate(paragraphs):
        nodes.append(p(para))
        if remaining and (i + 1) % 3 == 0:
            nodes.append(figure_img(remaining.pop(0)))

    for img in remaining:
        nodes.append(figure_img(img))

    if source_url:
        nodes.append(p(i_("Источник: "), a(source_url, source_url)))
    return nodes


def publish_article(
    title: str,
    paragraphs: Optional[List[str]] = None,
    images: Optional[List[str]] = None,
    source_url: Optional[str] = None,
    subtitle: str = "",
    blocks: Optional[List[dict]] = None,
    access_token: Optional[str] = None,
    author_name: str = DEFAULT_AUTHOR_NAME,
    session: Optional[requests.Session] = None,
) -> str:
    """Publish a Russian translated article to Telegra.ph; return the page URL.

    ``subtitle`` is the editorial lead from the source site. When non-empty it
    is rendered as a decorated italic paragraph (💬 «…») followed by `<hr>`
    before the body — this is the visual convention the bot's posts follow.

    ``blocks`` (optional) is the preferred input — an ordered list preserving
    image/video positions in the source article. When provided, ``paragraphs``
    and ``images`` are ignored. Sources that don't expose block structure
    (Mattel, Lamley, RSS fallback) still pass the flat lists.
    """
    token = access_token or os.environ.get(ENV_TOKEN_KEY)
    if not token:
        raise TelegraphError(f"{ENV_TOKEN_KEY} is not set; call ensure_access_token first")
    if blocks:
        content = _build_content_from_blocks(subtitle, blocks, source_url)
    else:
        content = _build_content(subtitle, paragraphs or [], images or [], source_url)
    result = _api_call(
        "createPage",
        {
            "access_token": token,
            "title": title,
            "author_name": author_name,
            "content": json.dumps(content, ensure_ascii=False),
            "return_content": "false",
        },
        session=session,
    )
    return result["url"]
