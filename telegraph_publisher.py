#!/usr/bin/env python3
"""Publish articles to Telegra.ph.

Public surface:

* ``publish_article(title, paragraphs, images, source_url, ...)`` — builds the
  Telegra.ph node tree and uploads it via ``createPage``; returns the page URL.
* ``ensure_access_token()`` — returns a cached Telegra.ph access token,
  creating and persisting one on first call.
* ``preview_nodes(title, paragraphs, images, source_url, subtitle, blocks)`` —
  offline mirror of the node tree ``publish_article`` would upload. No network
  calls, no ``TELEGRAPH_ACCESS_TOKEN`` required. Used by the local HTML preview
  renderer in ``hw_review preview N`` so the preview and the real publication
  share a single source of truth for node-tree construction.
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


# Auto-fallback differentiator. Rendered as a plain ``<p>`` paragraph
# node immediately before the ``Источник:`` footer when ``auto_marker``
# is True. The arrow is U+21B3 ('↳ DOWNWARDS ARROW WITH TIP RIGHTWARDS')
# — pinned byte-for-byte by the test suite to guard against editor-
# driven character drift. Style: plain text, no italic/bold wrap —
# unobtrusive but visible to anyone who opens the article.
AUTO_MARKER_TEXT = "↳ автоперевод"


def _auto_marker_node() -> dict:
    """Return the ``<p>`` node for the auto-fallback marker. Plain text
    child, no decoration."""
    return {"tag": "p", "children": [AUTO_MARKER_TEXT]}


def _build_content_from_blocks(
    subtitle: str,
    blocks: List[dict],
    source_url: Optional[str],
    auto_marker: bool = False,
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

    # Block-level rendering uses only the flat `text` field for now.
    # External `<a>` hrefs live in block["runs"] as metadata so Phase 2
    # (cross-article linking to our own Telegraph pages) can consume them —
    # we do NOT emit them here, because rendering raw source links ruined
    # the reading flow in early attempts (commit a984505).
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

    if auto_marker:
        nodes.append(_auto_marker_node())
    nodes.extend(_footer_nodes(source_url))
    return nodes


def _footer_nodes(source_url):
    """Render the "Источник" footer. Plain `<p>` so the link is interactive
    in both Instant View and the web rendering."""
    if not source_url:
        return []
    return [{"tag": "p", "children": [
        "Источник: ",
        {"tag": "a", "attrs": {"href": source_url}, "children": [source_url]},
    ]}]


def _build_content(
    subtitle: str,
    paragraphs: List[str],
    images: List[str],
    source_url: Optional[str],
    auto_marker: bool = False,
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

    if auto_marker:
        nodes.append(_auto_marker_node())
    nodes.extend(_footer_nodes(source_url))
    return nodes


def preview_nodes(
    title: str,
    paragraphs: Optional[List[str]] = None,
    images: Optional[List[str]] = None,
    source_url: Optional[str] = None,
    subtitle: str = "",
    blocks: Optional[List[dict]] = None,
    auto_marker: bool = False,
) -> list:
    """Return the Telegra.ph node tree that ``publish_article`` would upload,
    without making any network call.

    This is the offline mirror consumed by ``preview_renderer.render_html``
    for the local HTML preview in ``hw_review preview N``. The branching
    (``_build_content_from_blocks`` when ``blocks`` is non-empty, otherwise
    ``_build_content``) mirrors ``publish_article`` exactly so the preview
    matches what will actually be sent to ``createPage``.

    The function is pure and deterministic:

    * no HTTP calls (``_api_call`` / ``requests`` are never invoked),
    * no ``TELEGRAPH_ACCESS_TOKEN`` lookup,
    * no reads from ``os.environ``.

    ``title`` is accepted for symmetry with ``publish_article`` (and so the
    caller's code reads naturally) but is **not** included in the returned
    tree — Telegra.ph's ``createPage`` passes ``title`` as a separate field
    and the ``content`` array holds only body nodes.

    ``auto_marker`` (default False) injects a plain ``<p>`` node carrying
    ``↳ автоперевод`` immediately before the ``Источник:`` footer.
    Used by the auto-fallback path (``via_review=False``) to flag
    Gemini-translated posts to operators and curious readers without
    polluting the channel teaser.
    """
    if blocks:
        return _build_content_from_blocks(
            subtitle, blocks, source_url, auto_marker=auto_marker,
        )
    return _build_content(
        subtitle, paragraphs or [], images or [], source_url,
        auto_marker=auto_marker,
    )


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
    auto_marker: bool = False,
) -> str:
    """Publish a Russian translated article to Telegra.ph; return the page URL.

    ``subtitle`` is the editorial lead from the source site. When non-empty it
    is rendered as a decorated italic paragraph (💬 «…») followed by `<hr>`
    before the body — this is the visual convention the bot's posts follow.

    ``blocks`` (optional) is the preferred input — an ordered list preserving
    image/video positions in the source article. When provided, ``paragraphs``
    and ``images`` are ignored. Sources that don't expose block structure
    (Mattel, Lamley, RSS fallback) still pass the flat lists.

    ``auto_marker`` (default False) — when True, a plain ``<p>`` paragraph
    carrying ``↳ автоперевод`` is inserted immediately before the
    ``Источник:`` footer. Set by the auto-fallback path
    (``news_bot._fallback_publish`` with ``via_review=False``) so
    operators and curious readers can tell Gemini-translated posts apart
    from operator-curated ones. Manual ``hw_review.cmd_publish`` does
    NOT pass this flag → no marker on operator-curated posts. The
    channel teaser is byte-identical for both paths (Decision 14).
    """
    token = access_token or os.environ.get(ENV_TOKEN_KEY)
    if not token:
        raise TelegraphError(f"{ENV_TOKEN_KEY} is not set; call ensure_access_token first")

    content = preview_nodes(
        title=title,
        paragraphs=paragraphs,
        images=images,
        source_url=source_url,
        subtitle=subtitle,
        blocks=blocks,
        auto_marker=auto_marker,
    )
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
