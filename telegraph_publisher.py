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
import re
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


#: Render-path resource bounds. ``_llm_common`` holds its own copy of this
#: pair for the REQUEST path (Decision 8) — deliberately duplicated, not
#: imported, same reason as ``_BOLD_MARKER_RE`` below: neither layer may
#: depend on the other. Change one, change both: if the values drift, a
#: block can go to the LLM carrying markers the renderer then discards,
#: and the markers surface as visible litter instead of formatting.
_MAX_TEXT_FOR_RUNS = 100_000
_MAX_RUNS_PER_BLOCK = 100

#: Inline-format → Telegraph tag mapping. Wrapping order (outer → inner)
#: when multiple formats apply to the same span: bold > italic >
#: underline > strikethrough.  ``"<a>"`` is handled separately as the
#: outermost wrapper around any formats.
_FORMAT_TAGS = (
    ("bold", "strong"),
    ("italic", "i"),
    ("underline", "u"),
    ("strikethrough", "s"),
)


def _wrap_with_formats(child_text, formats):
    """Wrap a plain text segment in nested format nodes per ``formats`` list.

    The order in :data:`_FORMAT_TAGS` defines outer → inner nesting. A
    string is returned unchanged if ``formats`` is empty.
    """
    if not formats:
        return child_text
    node_text = child_text
    # Build inside-out so the outermost format from _FORMAT_TAGS ends up at
    # the top. Iterate REVERSE order so we wrap inner formats first.
    for fmt_name, tag in reversed(_FORMAT_TAGS):
        if fmt_name in formats:
            node_text = {"tag": tag, "children": [node_text]}
    return node_text


#: Paired ``**bold**`` markers in LLM output. Non-greedy body so two adjacent
#: spans on one line don't merge; no newline in the body so a stray unbalanced
#: ``**`` can't swallow the rest of the paragraph. Mirrors
#: ``_llm_common._BOLD_MARKER_RE`` — deliberately duplicated rather than
#: imported: the publisher must not depend on the LLM-engine layer, and this
#: is the LAST line of defence before text reaches a reader.
_BOLD_MARKER_RE = re.compile(r"\*\*([^*\n]+?)\*\*")

#: Any leftover asterisk pair after paired decoding (i.e. an UNBALANCED marker).
_STRAY_MARKER_RE = re.compile(r"\*\*")

#: Scheme allowlist for media URLs (``img src`` / ``iframe src``). The ``^``
#: anchor rejects leading whitespace (``  javascript:…``) and IGNORECASE
#: accepts ``HTTPS://`` without letting ``JavaScript:`` through — that scheme
#: simply is not in the allowlist. Bounded, no free quantifiers (ReDoS
#: contract).
#:
#: MIRRORS ``preview_renderer._SAFE_URL_RE`` — deliberately duplicated rather
#: than imported, the same convention as ``_BOLD_MARKER_RE`` above: the
#: preview renderer is an operator-side CLI layer and the publisher is the
#: production runtime path, so the dependency would point the wrong way.
#: Change one, change both; a test pins that both verdicts agree on a shared
#: URL set.
#:
#: The publisher is where this check belongs because every source and every
#: path funnels through it. The per-source pickers trust
#: ``startswith("http")`` (t_hunted, lamley, autoevolution), which lets
#: ``httpx://evil/x.jpg`` through, and autoevolution's gallery branch checks
#: no scheme at all.
_SAFE_MEDIA_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _is_safe_media_url(src) -> bool:
    """True iff ``src`` is an http(s) URL fit to emit into a Telegraph node.

    Rejects non-strings, blanks, unsafe schemes (``javascript:``, ``data:``,
    ``file:``), look-alike schemes (``httpx://``), protocol-relative
    ``//cdn/x.jpg`` and relative paths.
    """
    if not isinstance(src, str):
        return False
    return bool(_SAFE_MEDIA_URL_RE.match(src.strip()))


def _decode_bold_markers(text, runs=None):
    """Turn ``**bold**`` markers in *text* into real formatting.

    Returns ``(clean_text, effective_runs)``.

    Why this lives in the publisher rather than in the translation layer: the
    LLM is primed to emit these markers on EVERY article (the system prompt
    explains them unconditionally), but the decode step that existed before
    2026-07-28 ran only on the variant-B block-patch path. Every other route —
    a flat-``paragraphs`` source, a model that returned its own ``blocks``, the
    page title, an image caption — carried the raw markers all the way onto the
    page, where readers saw literal ``**Все ли старые Hot Wheels ценны?**``.
    Rendering is the one place every route converges, so the guarantee "no
    literal ``**`` ever reaches a reader" can only be made here.

    Behaviour:

    * Paired markers become ``{'text': …, 'formats': ['bold']}`` runs, honouring
      bold the model added on its own (operator decision 2026-07-28).
    * When *runs* are already present they WIN and are returned unchanged — the
      source's own formatting outranks the model's guesses. The markers are
      still stripped from the text. Existing runs stay locatable afterwards:
      ``_render_paragraph_with_runs`` finds them with ``str.find`` and marker
      removal never alters a run's own substring.
    * An UNBALANCED marker matches nothing, so its asterisks are removed by
      ``_STRAY_MARKER_RE`` rather than published. Losing an emphasis we cannot
      place beats showing punctuation the author never wrote.
    """
    if not isinstance(text, str) or "**" not in text:
        return text, runs
    decoded = []
    parts = []
    cursor = 0
    for match in _BOLD_MARKER_RE.finditer(text):
        parts.append(text[cursor:match.start()])
        body = match.group(1)
        parts.append(body)
        decoded.append({"text": body, "formats": ["bold"]})
        cursor = match.end()
    parts.append(text[cursor:])
    clean = "".join(parts)
    if _STRAY_MARKER_RE.search(clean):
        logger.warning(
            "[markers] unbalanced ** stripped from rendered text (%d chars)",
            len(clean),
        )
        clean = _STRAY_MARKER_RE.sub("", clean)
    return clean, (runs if runs else (decoded or None))


def _render_paragraph_with_runs(text, runs, source_url):
    """Return list of children for the given block text and runs metadata.

    Each run can carry:
    * ``href`` — if same-site (per :func:`_is_same_site`), the matched
      substring is wrapped in an ``<a>`` node.
    * ``formats`` — a list of inline format markers (``"bold"``,
      ``"italic"``, ``"underline"``, ``"strikethrough"``) that wrap the
      matched substring in corresponding Telegraph-supported tags.
      Color-class spans are mapped upstream to ``"bold"`` (Telegraph
      rejects color attributes).

    When BOTH ``href`` and ``formats`` apply, the ``<a>`` is the outermost
    wrapper (Telegraph allows ``<a><strong>...</strong></a>`` but not the
    reverse — anchors must dominate).

    The list is suitable for unpacking into ``p(*children)`` or
    ``heading(level, *children)``.

    Behaviour:
    * Empty/None ``runs`` → returns ``[text]``.
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
    # `source_url` is accepted for signature stability + future cross-
    # article linking; not consumed in the current link-disabled render.
    _ = source_url
    # Find spans for runs with formats. Inline links are intentionally
    # NOT rendered (product decision 2026-05-13): subscribers reading the
    # Russian translation should not be hyperlinked to English source
    # pages mid-prose. The source-footer at the bottom of the page
    # still carries «Источник: …» for readers who want the original.
    # `_is_same_site` is preserved (dormant) for the planned cross-article
    # linking feature where same-site hrefs would be re-mapped to our
    # own Telegraph URLs — at that point flip `href_val` back on.
    spans = []  # list of (start, end, href|None, formats|None)
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_text = run.get("text")
        if not run_text or not run_text.strip():
            continue  # Decision 9 — empty/whitespace skip BEFORE str.find
        href_val = None  # 2026-05-13: inline links disabled by product decision
        formats = run.get("formats")
        # Filter formats to known mapping; ignore unknown values defensively.
        if formats:
            known = {name for name, _ in _FORMAT_TAGS}
            formats = [f for f in formats if f in known]
        if not formats and not href_val:
            continue  # nothing to render for this run
        pos = text.find(run_text)
        if pos < 0:
            continue
        spans.append((pos, pos + len(run_text), href_val, formats or None))
    # Decision 5 — first-wrap-wins (sort by start, drop overlapping with already-accepted)
    spans.sort(key=lambda s: s[0])
    accepted = []
    last_end = -1
    for start, end, href, fmts in spans:
        if start >= last_end:
            accepted.append((start, end, href, fmts))
            last_end = end
        # else: overlap with earlier span → skip; the substring still appears
        # as plain text in the rebuilt segment.
    if not accepted:
        return [text]
    # Build children: alternating text segments and formatted nodes
    children = []
    cursor = 0
    for start, end, href, fmts in accepted:
        if start > cursor:
            children.append(text[cursor:start])
        span_text = text[start:end]
        # Inner: apply format wrapping (strong > i > u > s).
        node = _wrap_with_formats(span_text, fmts) if fmts else span_text
        # Outer: anchor wraps the formatted node so Telegraph accepts the
        # nesting order (a > strong, NEVER strong > a).
        if href:
            inner = node if isinstance(node, dict) else node
            children.append({
                "tag": "a",
                "attrs": {"href": href},
                "children": [inner],
            })
        else:
            children.append(node)
        cursor = end
    if cursor < len(text):
        children.append(text[cursor:])
    return children


def _build_content_from_blocks(
    subtitle: str,
    blocks: List[dict],
    source_url: Optional[str],
    auto_marker: bool = False,
    image_limit: Optional[int] = None,
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

    ``image_limit`` caps how many ``image`` blocks are emitted. ``None``
    (the default) means no cap, which keeps every existing caller working
    unchanged. The HERO COUNTS toward the limit, extra images are dropped
    FROM THE TAIL, and ``video`` blocks are not counted — the cap is about
    images. ``0`` and ``1`` are honoured literally (no images / hero only);
    ``0`` must never be read as "unlimited", that would silently discard the
    setting.

    Why the cap lives here at all: the per-source limits only ever sliced the
    derived flat ``images`` list, which this function ignores entirely — the
    moment ``blocks`` is non-empty the flat list is unused. Measured on 14
    real articles, ALL FOUR lamley posts exceed their limit of 10 (14, 41, 48
    and 50 images).

    Images whose ``src`` fails :func:`_is_safe_media_url` are dropped BEFORE
    the cap is applied, so discarded junk cannot eat a live image's slot; if
    the hero was the invalid one, the next valid image takes its place. A
    block with no ``src`` at all is skipped rather than raising.
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

    # Order matters: drop unusable src FIRST, then apply the cap. The other
    # way round, a junk image would consume a slot and a good one would fall
    # off the tail.
    all_image_idx = [
        i for i, b in enumerate(blocks) if b.get("type") == "image"
    ]
    valid_image_idx = [
        i for i in all_image_idx if _is_safe_media_url(blocks[i].get("src"))
    ]
    for i in all_image_idx:
        if i not in valid_image_idx:
            logger.warning(
                "[telegraph] dropping image block with unusable src=%.100r",
                blocks[i].get("src"),
            )

    if image_limit is None:
        kept_image_idx = list(valid_image_idx)
    else:
        kept_image_idx = valid_image_idx[:image_limit]
        if len(valid_image_idx) > len(kept_image_idx):
            logger.info(
                "[telegraph] image cap applied: had=%d kept=%d limit=%d",
                len(valid_image_idx),
                len(kept_image_idx),
                image_limit,
            )
    kept_image_set = set(kept_image_idx)
    first_image_idx = kept_image_idx[0] if kept_image_idx else None

    nodes: list = []
    if first_image_idx is not None:
        hero = blocks[first_image_idx]
        nodes.append(figure_img(
            hero["src"], _decode_bold_markers(hero.get("caption", ""))[0],
        ))

    if subtitle:
        nodes.append(p(i_(f"💬 «{_decode_bold_markers(subtitle)[0]}»")))
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
            text, runs = _decode_bold_markers(block["text"], block.get("runs"))
            nodes.append(p(*_render_paragraph_with_runs(
                text, runs, source_url,
            )))
        elif t == "lead":
            nodes.append(p(b_(_decode_bold_markers(block["text"])[0])))
        elif t == "heading":
            text, runs = _decode_bold_markers(block["text"], block.get("runs"))
            nodes.append(heading(
                block.get("level", 3),
                *_render_paragraph_with_runs(text, runs, source_url),
            ))
        elif t == "list_item":
            text = (block.get("text") or "").lstrip(" •\t\n")  # Decision 10 — strip leading bullet/whitespace before prepending
            text, runs = _decode_bold_markers(text, block.get("runs"))
            nodes.append(p("• ", *_render_paragraph_with_runs(
                text, runs, source_url,
            )))
        elif t == "image":
            # Dropped by scheme or by the cap — the caption goes with it: a
            # caption without its image is litter in the text.
            if i not in kept_image_set:
                continue
            nodes.append(figure_img(
                block["src"], _decode_bold_markers(block.get("caption", ""))[0],
            ))
        elif t == "video":
            if not _is_safe_media_url(block.get("src")):
                logger.warning(
                    "[telegraph] dropping video block with unusable src=%.100r",
                    block.get("src"),
                )
                continue
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
    # Same scheme policy as the block path. No CAP here on purpose: the flat
    # list was already sliced by the parser (orangetrack, lamley, t-hunted,
    # autoevolution each apply their own limit), so a second slice would only
    # confuse. The VALIDATION is still needed — the flat lists are built with
    # the same trusting ``startswith("http")`` checks.
    remaining = []
    for src in (images or []):
        if _is_safe_media_url(src):
            remaining.append(src)
        else:
            logger.warning(
                "[telegraph] dropping flat image with unusable src=%.100r", src
            )
    if remaining:
        nodes.append(figure_img(remaining.pop(0)))

    if subtitle:
        nodes.append(p(i_(f"💬 «{_decode_bold_markers(subtitle)[0]}»")))
        nodes.append({"tag": "hr"})

    for i, para in enumerate(paragraphs):
        # Flat sources have no runs container, so the model's own `**` markers
        # are the ONLY formatting signal here — decode them into real bold
        # instead of publishing the asterisks (prod complaint 2026-07-28).
        para_text, para_runs = _decode_bold_markers(para)
        nodes.append(p(*_render_paragraph_with_runs(
            para_text, para_runs, source_url,
        )))
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
    image_limit: Optional[int] = None,
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
            image_limit=image_limit,
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
    image_limit: Optional[int] = None,
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

    # The page title is plain text on Telegraph — it cannot carry formatting,
    # and it is ALSO what the Telegram link preview shows, so a stray `**` here
    # is the most visible leak of all. Strip the markers without trying to
    # render them (2026-07-28).
    title = _decode_bold_markers(title)[0]

    content = preview_nodes(
        title=title,
        paragraphs=paragraphs,
        images=images,
        source_url=source_url,
        subtitle=subtitle,
        blocks=blocks,
        auto_marker=auto_marker,
        image_limit=image_limit,
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
