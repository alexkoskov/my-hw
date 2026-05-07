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
from urllib.parse import urlparse

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


def _strip_www(netloc: str) -> str:
    """Return ``netloc`` lower-cased and with a leading ``www.`` removed.

    Uses ``str.removeprefix`` (Python 3.9+) — NOT ``str.lstrip("www.")``,
    which is a character-set strip and would also match ``wwwfake-…``
    lookalike domains (security audit critical finding).
    """
    n = (netloc or "").lower()
    return n.removeprefix("www.")


def _is_same_site(href, source_netloc):
    """Return True iff ``href`` points to the same site as ``source_netloc``.

    Contract: ``source_netloc`` is the already-parsed netloc string from
    ``urlparse(source_url).netloc`` — caller computes once per render to
    avoid re-parsing on every run.

    Returns False for: empty/falsy ``href`` or ``source_netloc``,
    non-http(s) schemes (drops ``mailto:``, ``javascript:``, ``data:``),
    empty parsed netloc, ``urlparse`` exceptions (logged), or netloc
    mismatch after ``www.`` prefix normalisation. See Decision 4.
    """
    if not href or not source_netloc:
        return False
    try:
        u = urlparse(href)
    except Exception as exc:
        logger.warning(
            "[orangetrack-render] urlparse failed for %s...: %s",
            str(href)[:50],
            type(exc).__name__,
        )
        return False
    if u.scheme.lower() not in ("http", "https"):
        return False
    if not u.netloc:
        return False
    return _strip_www(u.netloc) == _strip_www(source_netloc)


_MAX_TEXT_FOR_RUNS = 100_000
_MAX_RUNS_PER_BLOCK = 100


def _render_paragraph_with_runs(text, runs, source_url):
    """Return list of children (string + a-nodes interleaved) for the given
    block text and runs metadata.

    Same-site runs (per :func:`_is_same_site`) are wrapped in ``<a>`` nodes
    inside the resulting children list. The list is suitable for unpacking
    into ``p(*children)`` or ``heading(level, *children)``.

    Behaviour:
    * Empty/None ``runs`` or empty/None ``source_url`` → returns ``[text]``.
    * DoS bounds (Decision 10): ``len(text) > 100000`` or ``len(runs) > 100``
      → falls through to ``[text]`` with a single WARNING.
    * Each run's ``text`` field is located via case-sensitive ``str.find``;
      runs whose text is missing or whitespace-only are skipped BEFORE the
      ``find`` call (Decision 9 zero-width guard).
    * Overlapping spans: first-wrap-wins (Decision 5) — later overlapping
      runs render as plain text within the rebuilt segment.
    """
    if not runs:
        return [text]
    # DoS bounds (Decision 10)
    if len(text) > _MAX_TEXT_FOR_RUNS or len(runs) > _MAX_RUNS_PER_BLOCK:
        logger.warning(
            "[orangetrack-render] DoS bound: text=%d runs=%d — falling through to plain text",
            len(text),
            len(runs),
        )
        return [text]
    # Compute source netloc once (Decision 7 — symmetric helper invocation)
    try:
        source_netloc = urlparse(source_url).netloc if source_url else ""
    except Exception:
        source_netloc = ""
    # If source_netloc is empty, _is_same_site will return False for every
    # run → no spans collected → fall through to plain text.
    # Find spans for same-site runs
    spans = []  # list of (start, end, href)
    for run in runs:
        run_text = run.get("text") if isinstance(run, dict) else None
        if not run_text or not run_text.strip():
            continue  # Decision 9 — empty/whitespace skip BEFORE str.find
        href = run.get("href")
        if not _is_same_site(href, source_netloc):
            continue
        pos = text.find(run_text)
        if pos < 0:
            continue
        spans.append((pos, pos + len(run_text), href))
    # Decision 5 — first-wrap-wins (sort by start, drop overlapping with already-accepted)
    spans.sort(key=lambda s: s[0])
    accepted = []
    last_end = -1
    for start, end, href in spans:
        if start >= last_end:
            accepted.append((start, end, href))
            last_end = end
        # else: overlap with earlier span → skip; the substring still appears
        # as plain text in the rebuilt segment.
    if not accepted:
        return [text]
    # Build children: alternating text segments and <a> nodes
    children = []
    cursor = 0
    for start, end, href in accepted:
        if start > cursor:
            children.append(text[cursor:start])
        children.append({"tag": "a", "attrs": {"href": href}, "children": [text[start:end]]})
        cursor = end
    if cursor < len(text):
        children.append(text[cursor:])
    return children


def _build_content_from_blocks(
    subtitle: str,
    blocks: List[dict],
    source_url: Optional[str],
    auto_marker: bool = False,
) -> list:
    """Render ordered content blocks into Telegra.ph nodes, preserving
    image/video positions from the source article.

    Block shapes:
        {'type': 'paragraph', 'text': str, 'runs': list}
        {'type': 'lead', 'text': str}                  # bold intro
        {'type': 'heading', 'text': str, 'level': 3|4, 'runs': list}
        {'type': 'list_item', 'text': str, 'runs': list}  # rendered as <p>• …</p>
        {'type': 'image', 'src': str}
        {'type': 'video', 'src': str}                  # embed URL

    The first ``image`` block becomes the hero figure so it drives the
    Telegram preview thumbnail; the rest appear in their original positions.

    Paragraph, heading, and list_item blocks flow through
    :func:`_render_paragraph_with_runs` so same-site ``<a>`` runs from
    ``block["runs"]`` are rendered inline. List_item blocks have their
    leading bullet/whitespace stripped before the publisher prepends
    ``"• "`` (Decision 10 bullet-doubling guard).
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
    def heading(level, *children):
        lvl = level if level in (3, 4) else 3
        return {"tag": f"h{lvl}", "children": list(children)}

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

    # Block-level rendering: paragraph/heading/list_item flow through the
    # same-site link helper so `<a href>` runs from block["runs"] become
    # inline ``<a>`` nodes when they point back to ``source_url``'s netloc.
    # Off-domain hrefs are still dropped by design (rendering raw external
    # links ruined the reading flow in early attempts, commit a984505).
    for i, block in enumerate(blocks):
        if i == first_image_idx:
            continue
        t = block.get("type")
        if t == "paragraph":
            nodes.append(p(*_render_paragraph_with_runs(
                block["text"], block.get("runs"), source_url,
            )))
        elif t == "lead":
            nodes.append(p(b_(block["text"])))
        elif t == "heading":
            nodes.append(heading(
                block.get("level", 3),
                *_render_paragraph_with_runs(
                    block["text"], block.get("runs"), source_url,
                ),
            ))
        elif t == "list_item":
            text = (block.get("text") or "").lstrip(" •\t\n")  # Decision 10 — strip leading bullet/whitespace before prepending
            nodes.append(p("• ", *_render_paragraph_with_runs(
                text, block.get("runs"), source_url,
            )))
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
