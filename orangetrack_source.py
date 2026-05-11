#!/usr/bin/env python3
"""Orange Track Diecast news source.

orangetrackdiecast.com is Brad Bannach's solo Hot Wheels blog hosted on
WordPress.com. RSS carries the full article body inside ``<content:encoded>``
so the primary path parses the feed entry directly. When ``content:encoded``
is missing or empty, the parser falls back to a bounded-streaming HTTP GET
of the article page.

Hard-won security defaults (per project recipe / tech-spec Decisions):
  * ``_is_allowed_orangetrack_url`` exact-host allowlist before any HTTP fetch.
  * ``allow_redirects=False`` on fallback GET (no redirect-bypass SSRF).
  * Bounded streaming via ``iter_content`` + 5 MB cap (lying-server safe).
  * YouTube wrapper gated by hostname allowlist before the ID regex.
  * Anchor href / image src scheme filter (``http``/``https``/``mailto``).
  * Walk DOM by HTML tag, not by Gutenberg ``wp-block-*`` classes.

The aggregator collects ``(code, link)`` events during one cron-tick lifetime
and emits a single admin-ping message at the end via ``emit(send_fn)``.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from boilerplate_filter import filter_blocks, filter_boilerplate
import admin_alerts

logger = logging.getLogger(__name__)

#: Canonical RSS feed URL for orangetrackdiecast.com. Lives as a module
#: constant (not in feeds.json) so the shared ``_fetch_rss_entries``
#: iteration path stays untouched. Mirrors mattel_news_source.NEWS_URL.
_FEED_URL = "https://orangetrackdiecast.com/feed/"

#: Exact-host allowlist for both entry-level (poisoned-link) and
#: fallback-HTTP guards. Subdomain attacks like
#: ``orangetrackdiecast.com.attacker.example`` would pass the
#: dispatcher's substring ``in`` check; this allowlist closes that hole.
_ALLOWED_HOSTS = ("orangetrackdiecast.com", "www.orangetrackdiecast.com")

#: Hostname allowlist for the YouTube embed wrapper. Without this, an
#: attacker URL containing the substring ``youtube.com/embed/abc`` would
#: be falsely wrapped to a Telegra.ph YouTube proxy URL.
_YOUTUBE_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
    "youtu.be",
)

REQUEST_TIMEOUT = 15
MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5 MB
IMAGE_LIMIT = 10
_CHUNK_SIZE = 8 * 1024  # 8 KB chunks for iter_content

#: Schemes that survive the href filter. Anything else (``javascript:``,
#: ``data:``, ``file:``, scheme-relative ``//evil/x``) drops the href and
#: the anchor degenerates to plain text.
_ALLOWED_HREF_SCHEMES = frozenset(("http", "https", "mailto"))

#: Schemes accepted on ``<img src>``. ``data:`` SVGs and ``file:`` paths
#: are dropped (defense-in-depth — Telegraph filters too, but the parser
#: must not emit them in the first place).
_ALLOWED_IMG_SCHEMES = frozenset(("http", "https"))

#: YouTube video ID regex (after the hostname allowlist gate has accepted
#: the iframe src). Captures the 11-char ID from common URL shapes.
_YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|/embed/|/watch\?v=|/v/|/shorts/)"
    r"([A-Za-z0-9_-]{6,})"
)

#: Per-aggregator caps. 50 links/code keeps the per-bullet line readable;
#: 500 total events caps memory in the pathological case of a feed bug
#: producing 10000 errors; 3500-char output stays well under Telegram's
#: 4096 limit with margin for the [INSTANCE_LABEL] prefix.
_MAX_LINKS_PER_CODE = 50
_MAX_TOTAL_EVENTS = 500
_MAX_SUMMARY_CHARS = 3500

#: Sanitization output bound for the (code, link) strings rendered in
#: the admin ping. Keeps a long crafted link from filling the bullet line.
_SAFE_FOR_PING_MAX = 200


# ---------------------------------------------------------------------------
# Helper: SSRF allowlist guard
# ---------------------------------------------------------------------------


def _is_allowed_orangetrack_url(link: str) -> bool:
    """Return True iff ``link`` is an http(s) URL with an exact-allowlist host.

    Mirrors lamley_source._is_allowed_lamley_url. Rejects:
      * non-string / None input,
      * non-http(s) schemes (``javascript:``, ``data:``, ``file:``),
      * scheme-relative URLs (``//evil/x``),
      * malformed URLs that urlparse can't parse,
      * any host outside ``_ALLOWED_HOSTS`` (subdomain attacks closed).
    """
    if not isinstance(link, str) or not link:
        return False
    try:
        parsed = urlparse(link)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    return host in _ALLOWED_HOSTS


# ---------------------------------------------------------------------------
# Helper: YouTube embed wrapper (with hostname allowlist gate)
# ---------------------------------------------------------------------------


def _video_embed_url(youtube_url: str) -> Optional[str]:
    """Wrap a YouTube URL into Telegra.ph's iframe-embed proxy form.

    The hostname allowlist gate runs BEFORE the ID regex — without it,
    a non-YouTube URL containing the substring ``youtube.com/embed/abc``
    would be falsely wrapped (autoevolution's regex-only path has this
    gap; we close it here per Decision 8).

    Returns ``None`` when the URL is not a YouTube link or has no
    extractable video ID. Telegra.ph validates ``iframe.src`` at
    create-page time and accepts ONLY ``/embed/<provider>?url=…`` proxy
    URLs, so raw YouTube URLs would be silently stripped to an empty
    ``/embed/`` (breaking Instant View).
    """
    if not isinstance(youtube_url, str) or not youtube_url:
        return None
    try:
        parsed = urlparse(youtube_url)
    except (ValueError, AttributeError):
        return None
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        return None
    m = _YOUTUBE_ID_RE.search(youtube_url)
    if not m:
        return None
    video_id = m.group(1)
    watch = f"https://www.youtube.com/watch?v={video_id}"
    return (
        "https://telegra.ph/embed/youtube?url="
        + urllib.parse.quote(watch, safe="")
    )


# ---------------------------------------------------------------------------
# Helper: sanitization for admin-ping strings
# ---------------------------------------------------------------------------

#: Strip ASCII control chars (\r \n \t \x00-\x1f). Order: control-strip
#: BEFORE truncate so a malicious link that's just under 200 chars can't
#: smuggle a control byte by being trimmed after.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_for_ping(s: str) -> str:
    """Sanitize a string for inclusion in an admin-ping message.

    Steps in order (Decision 5):
      1. Strip ASCII control chars (\\r, \\n, \\t, \\x00-\\x1f, \\x7f).
      2. Replace any remaining non-printable byte with '?'.
      3. Truncate to ``_SAFE_FOR_PING_MAX`` chars (with '…' suffix).

    Prevents log/admin-ping spoofing via crafted feed link strings.
    """
    if not isinstance(s, str):
        s = str(s) if s is not None else ""
    s = _CONTROL_CHAR_RE.sub("", s)
    # Replace remaining non-printable (e.g. unpaired surrogates would be
    # caught here if any survived). ``str.isprintable`` is the test.
    s = "".join(c if c.isprintable() else "?" for c in s)
    if len(s) > _SAFE_FOR_PING_MAX:
        s = s[: _SAFE_FOR_PING_MAX - 1] + "…"
    return s


# ---------------------------------------------------------------------------
# Helper: anchor href filter
# ---------------------------------------------------------------------------


def _safe_href(href: Optional[str]) -> Optional[str]:
    """Return ``href`` iff it's a safe scheme; else None.

    Allowed: http, https, mailto (for editorial mailto links).
    Dropped: javascript:, data:, file:, scheme-relative ``//evil/x``,
    relative paths, malformed strings.
    """
    if not href or not isinstance(href, str):
        return None
    href = href.strip()
    if not href:
        return None
    # Scheme-relative URLs (``//evil/x``) have no parsed scheme; reject.
    if href.startswith("//"):
        return None
    try:
        parsed = urlparse(href)
    except (ValueError, AttributeError):
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme in _ALLOWED_HREF_SCHEMES:
        return href
    return None


def _safe_img_src(src: Optional[str]) -> Optional[str]:
    """Return ``src`` iff it's an http(s) URL; else None."""
    if not src or not isinstance(src, str):
        return None
    src = src.strip()
    if not src:
        return None
    if src.startswith("//"):
        return None
    try:
        parsed = urlparse(src)
    except (ValueError, AttributeError):
        return None
    if (parsed.scheme or "").lower() not in _ALLOWED_IMG_SCHEMES:
        return None
    return src


# ---------------------------------------------------------------------------
# Internal: text-runs walker (paragraph children → ordered text+href runs)
# ---------------------------------------------------------------------------


#: Inline-formatting tags that map to Telegraph-supported nodes. Order in
#: each value is ALSO the nesting order when multiple formats apply to the
#: same span (outer → inner): bold > italic > underline > strikethrough.
_INLINE_FORMAT_TAGS = {
    "strong": "bold",
    "b": "bold",
    "em": "italic",
    "i": "italic",
    "u": "underline",
    "s": "strikethrough",
    "del": "strikethrough",
}


def _has_color_class(node) -> bool:
    """True if this BS4 element carries any WordPress-Gutenberg color class.

    Telegraph API does not support text color, so we map colored text to
    bold (visual emphasis preserved) — see ``_render_paragraph_with_runs``
    in ``telegraph_publisher.py``. We accept ANY class containing the
    substring ``-color`` (e.g. ``has-vivid-red-color``,
    ``has-vivid-cyan-blue-color``, ``has-text-color``) as a signal of
    "this span is visually emphasised".
    """
    classes = node.get("class") or []
    return any("-color" in (c or "") for c in classes)


def _runs_from_tag(tag) -> List[Dict]:
    """Walk a tag's contents and return ordered runs.

    Run shape: ``{'text': str, ['href': str], ['formats': list[str]]}``.

    * Anchors (``<a>``) with safe schemes preserve the ``href``;
      anchors with dropped/unsafe href degenerate to plain text.
    * Inline formatting tags (``<strong>``, ``<b>``, ``<em>``, ``<i>``,
      ``<u>``, ``<s>``, ``<del>``) and any element with a WordPress
      ``has-*-color`` class accumulate format markers in the ``formats``
      list (cumulative across nested elements). Color classes are mapped
      to ``"bold"`` since Telegraph rejects color attributes.
    * Sibling runs with the same ``href``/``formats`` are NOT merged —
      they stay separate (downstream renderer picks first occurrence per
      run, so adjacent identical runs are harmless).
    """
    runs: List[Dict] = []
    buf: List[str] = []
    fmt_stack: List[str] = []

    def current_formats():
        # Preserve order of accumulation, dedup while keeping first-seen order.
        seen: List[str] = []
        for f in fmt_stack:
            if f not in seen:
                seen.append(f)
        return seen

    def flush(href=None):
        if not buf:
            return
        combined = "".join(buf)
        if combined:
            run: Dict = {"text": combined}
            if href:
                run["href"] = href
            fmts = current_formats()
            if fmts:
                run["formats"] = list(fmts)
            runs.append(run)
        buf.clear()

    def walk(element):
        for child in element.children:
            if isinstance(child, str):
                buf.append(str(child))
                continue
            name = getattr(child, "name", None)
            if name == "a":
                href = _safe_href(child.get("href"))
                # Recurse into anchor children to capture nested <strong>/etc.
                # and emit a single run per anchor; if an inner format applies,
                # we attach it to the run alongside the href.
                inner_buf: List[str] = []
                inner_fmts: List[str] = []

                def collect(el):
                    inner_name = getattr(el, "name", None)
                    if isinstance(el, str):
                        inner_buf.append(str(el))
                        return
                    fmt = _INLINE_FORMAT_TAGS.get(inner_name)
                    color_fmt = "bold" if _has_color_class(el) else None
                    pushed = []
                    if fmt:
                        inner_fmts.append(fmt)
                        pushed.append(fmt)
                    if color_fmt and color_fmt not in inner_fmts:
                        inner_fmts.append(color_fmt)
                        pushed.append(color_fmt)
                    for sub in getattr(el, "children", []):
                        collect(sub)
                    for _ in pushed:
                        inner_fmts.pop()

                for sub in child.children:
                    collect(sub)
                link_text = "".join(inner_buf).strip()
                # Re-derive a deduped format list (collect could push the same
                # format twice across siblings — keep first occurrence order).
                seen_inner: List[str] = []
                for f in current_formats():
                    seen_inner.append(f)
                # Note: anchor inner formats only matter if anchor itself or
                # ancestors are formatted. We use the OUTER fmt_stack to attach
                # ambient formatting to anchors (e.g., paragraph-wide <strong>).
                if href and link_text:
                    flush()
                    run: Dict = {"text": link_text, "href": href}
                    fmts = current_formats()
                    if fmts:
                        run["formats"] = list(fmts)
                    runs.append(run)
                elif link_text:
                    # Drop unsafe href, keep plain text inline.
                    buf.append(link_text)
                continue
            # Inline-format tag handling: push format marker(s) onto stack,
            # walk children, pop. Color class is treated as "bold" emphasis.
            fmt = _INLINE_FORMAT_TAGS.get(name)
            color_fmt = "bold" if _has_color_class(child) else None
            pushed: List[str] = []
            if fmt:
                fmt_stack.append(fmt)
                pushed.append(fmt)
            if color_fmt and color_fmt not in fmt_stack:
                fmt_stack.append(color_fmt)
                pushed.append(color_fmt)
            if pushed:
                # Flush any pending plain-text buffer BEFORE we descend into
                # the formatted span — otherwise the unformatted prefix would
                # incorrectly get this format attached when flushed later.
                flush()
                walk(child)
                flush()
                for _ in pushed:
                    fmt_stack.pop()
            else:
                walk(child)

    walk(tag)
    flush()
    # Normalize whitespace inside each run; trim leading/trailing on edges.
    for r in runs:
        r["text"] = re.sub(r"\s+", " ", r["text"])
    if runs:
        runs[0]["text"] = runs[0]["text"].lstrip()
        runs[-1]["text"] = runs[-1]["text"].rstrip()
    return [r for r in runs if r["text"]]


# ---------------------------------------------------------------------------
# Internal: extract images from a <figure> / <img> element
# ---------------------------------------------------------------------------


def _best_img_src(img) -> Optional[str]:
    """Pick the best ``src`` from an ``<img>`` element.

    Prefers ``srcset`` ``?w=1024`` if present; else ``src``. Returns
    None if neither yields a safe http(s) URL.
    """
    # srcset format: "url1 300w, url2 600w, url3 1024w" — pick a 1024 if
    # available (Brad's WordPress.com default lays out 300/600/1024).
    srcset = img.get("srcset") or ""
    if srcset:
        candidates: List[Tuple[str, int]] = []
        for part in srcset.split(","):
            part = part.strip()
            if not part:
                continue
            tokens = part.split()
            url = tokens[0]
            width = 0
            for tok in tokens[1:]:
                if tok.endswith("w"):
                    try:
                        width = int(tok[:-1])
                    except ValueError:
                        width = 0
            candidates.append((url, width))
        # Pick the largest <= 1024, else the largest overall.
        if candidates:
            preferred = [c for c in candidates if c[1] and c[1] <= 1024]
            picks = preferred if preferred else candidates
            picks.sort(key=lambda c: c[1] or 0, reverse=True)
            url = _safe_img_src(picks[0][0])
            if url:
                return url
    src = _safe_img_src(img.get("src"))
    if src:
        return src
    return None


# ---------------------------------------------------------------------------
# Internal: parse <content:encoded> HTML body → canonical dict
# ---------------------------------------------------------------------------


def _has_empty_embed_wrapper(html_str: str) -> bool:
    """Detect WordPress empty `wp-block-embed` wrapper.

    WordPress.com RSS sometimes strips the actual ``<iframe>``/``<video>``
    element from ``content:encoded`` but leaves the surrounding
    ``<figure class="wp-block-embed ...">`` / ``<div class="wp-block-embed__wrapper">``.
    The live HTML page DOES carry the iframe — the RSS export is the lossy
    layer. When we see this empty-wrapper marker we treat the feed body as
    incomplete and trigger the HTTP-scrape fallback, which fetches the live
    page and recovers the embedded video.

    Discovered 2026-05-06 on
    `https://orangetrackdiecast.com/2026/04/23/unboxing-hot-wheels-2026-red-line-club-1988-porsche-911-targa-turbo-will-i-get-the-chase/`
    where the article's centerpiece YouTube unboxing was missing on Telegraph.
    """
    if not html_str:
        return False
    try:
        soup = BeautifulSoup(html_str, "html.parser")
    except Exception:
        return False
    # Look for the wrapper element specifically.
    for wrapper in soup.find_all(
        class_=lambda c: c and "wp-block-embed__wrapper" in c
    ):
        # If the wrapper has no iframe / video child, it's empty —
        # WordPress stripped the actual embed during RSS export.
        if not wrapper.find("iframe") and not wrapper.find("video"):
            return True
    return False


def _parse_content_encoded(html_str: str, link: str) -> Optional[Dict]:
    """Parse a content:encoded HTML body into the canonical contract dict.

    Returns ``{'title', 'subtitle', 'paragraphs', 'images', 'blocks'}``
    or None if no extractable content survives the filters.

    ``title`` comes from the first ``<h1>`` if present (else empty —
    callers usually have ``entry.title`` to fall back to).

    ``blocks`` preserves DOM order: paragraph / image / video / heading
    entries. h5 headings go to blocks-only (not flat ``paragraphs``).
    """
    if not html_str or not isinstance(html_str, str):
        return None

    soup = BeautifulSoup(html_str, "html.parser")

    # Strip script/style/noscript so JS doesn't leak into paragraph text.
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    # Title: take the first <h1> if any (content:encoded usually doesn't
    # carry one — title comes from RSS entry).
    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    blocks: List[Dict] = []
    seen_image_bases = set()

    # Walk top-level descendants. We use ``find_all(recursive=True)`` for
    # primary tag types, then traverse them in document order via
    # ``soup.descendants`` filter — but to stay simple and predictable,
    # iterate over the direct children of the root and recurse manually
    # when we hit a wrapper (<div>, <article>, <section>).
    def _emit_paragraph(p_tag):
        runs = _runs_from_tag(p_tag)
        if not runs:
            return
        text = " ".join(r["text"] for r in runs).strip()
        if not text:
            return
        blocks.append({"type": "paragraph", "text": text, "runs": runs})

    def _emit_heading(h_tag, level):
        # Emit h-tags with level-aware dispatch (Decisions 2 + 3 of
        # orangetrack-rendering-fixes):
        #   - h2 / h3 / h4 → ``type: "heading", level: 3``. orangetrack uses
        #     these as full section headers (model name = section), so a
        #     prominent Telegraph heading conveys the right hierarchy.
        #     All three are normalised to ``level=3`` (single visual
        #     treatment) — orangetrack typically has one section level.
        #   - h5 → ``type: "paragraph"`` (carve-out preserves commit
        #     ``babc67c`` from SESSION-2026-05-06.md break 3). On
        #     orangetrack ``<h5>`` is used as in-paragraph section marker
        #     with long descriptive text; rendering it as a Telegraph
        #     heading looked uneven. Keep paragraph typography here.
        #   - h1 / h6 are dropped earlier in ``_walk`` and never reach
        #     this helper.
        runs = _runs_from_tag(h_tag)
        if not runs:
            return
        text = " ".join(r["text"] for r in runs).strip()
        if not text:
            return
        if level in (2, 3, 4):
            blocks.append({
                "type": "heading",
                "level": 3,
                "text": text,
                "runs": runs,
            })
            return
        # level == 5 (and any other unexpected level): paragraph.
        blocks.append({
            "type": "paragraph",
            "text": text,
            "runs": runs,
        })

    def _emit_image(img_tag, caption: str = ""):
        src = _best_img_src(img_tag)
        if not src:
            return
        base = src.split("?", 1)[0]
        if base in seen_image_bases:
            return
        seen_image_bases.add(base)
        block: Dict = {"type": "image", "src": src}
        if caption:
            block["caption"] = caption
        blocks.append(block)

    def _emit_iframe(iframe_tag):
        raw_src = iframe_tag.get("src") or ""
        embed = _video_embed_url(raw_src)
        if not embed:
            return
        blocks.append({"type": "video", "src": embed})

    # Walk: BS4's ``descendants`` yields ALL nodes in DOM order, which
    # would double-count <p> nested under <figure>. Instead, walk top
    # children and recurse selectively — known content tags get emitted
    # once. We process by tag name (Decision 3 — no Gutenberg classes).
    handled_tags = {
        "p", "h1", "h2", "h3", "h4", "h5", "h6",
        "figure", "img", "iframe",
        # ``ul`` / ``ol`` stay out of ``handled_tags`` so the wrapper
        # fallback recurses into them and reaches their ``<li>`` children
        # — the explicit ``li`` branch below handles emission. Including
        # ``"li"`` here is documentation: it pins that ``<li>`` is
        # processed by its own branch and does NOT fall through to
        # generic recursion.
        "li",
    }

    # WordPress chrome class markers — when we encounter a div/section
    # with one of these classes, skip it entirely (don't recurse into
    # it). These are the "Share this:" buttons (sharedaddy), the
    # tag/category list (taxonomies), JetPack related posts (jp-related),
    # comment forms, etc. Discovered 2026-05-06 on the Porsche Targa
    # Turbo republish — test channel showed these blocks bleeding into
    # the Telegraph article. Genuine content like the "Original listing
    # on Mattel Creations: …" paragraph stays (operator wants it).
    _CHROME_CLASS_MARKERS = (
        "sharedaddy", "sd-", "taxonomies", "jp-related",
        "post-comments", "comment-form",
    )

    def _has_chrome_class(child) -> bool:
        classes = child.get("class") or []
        for c in classes:
            cl = c.lower()
            for marker in _CHROME_CLASS_MARKERS:
                if marker in cl:
                    return True
        return False

    def _walk(node):
        for child in list(node.children):
            name = getattr(child, "name", None)
            if not name:
                continue  # NavigableString — skip; <p> walker handles text.
            if _has_chrome_class(child):
                continue  # WordPress footer chrome — drop.
            if name == "p":
                # Check if the paragraph wraps an iframe / img only — those
                # take precedence so we don't get a run with an empty
                # ``text`` from BS4's get_text on the iframe.
                inner_iframes = child.find_all("iframe")
                inner_imgs = child.find_all("img")
                if inner_iframes and not child.get_text(strip=True):
                    for iframe in inner_iframes:
                        _emit_iframe(iframe)
                    continue
                # Mixed paragraph: emit text first, then nested media.
                _emit_paragraph(child)
                for iframe in inner_iframes:
                    _emit_iframe(iframe)
                for img in inner_imgs:
                    if img.find_parent("figure"):
                        # Will be picked up by the figure walker.
                        continue
                    _emit_image(img)
                continue
            if name in ("h2", "h3", "h4"):
                _emit_heading(child, int(name[1]))
                continue
            if name == "h5":
                # h5 keeps `type: paragraph` (babc67c carve-out from SESSION-2026-05-06.md
                # break 3 — h5 used as in-paragraph markers, big-bold styling looked uneven).
                # Now also flows into paragraphs_flat alongside paragraph/heading/list_item
                # (orangetrack-rendering-fixes Decision 2 keeps h5 as paragraph-typed block).
                _emit_heading(child, 5)
                continue
            if name in ("h1", "h6"):
                # h1 already used for title; h6 is rare/decorative.
                continue
            if name == "figure":
                img = child.find("img")
                if img:
                    cap_tag = child.find("figcaption")
                    caption = cap_tag.get_text(" ", strip=True) if cap_tag else ""
                    _emit_image(img, caption=caption)
                # Video embed wrapped in <figure> (typical WP output:
                # <figure class="wp-block-embed"><div><iframe>...). Pick up
                # iframes nested anywhere inside the figure — the figure
                # handler runs ONCE per figure, so we look for iframe
                # children that the top-level walk wouldn't reach
                # (figure isn't a wrapper-tag in the recurse-list).
                for inner_iframe in child.find_all("iframe"):
                    _emit_iframe(inner_iframe)
                # Nested figures (carousel / gallery) — recurse to grab
                # additional <img> inside.
                for nested in child.find_all("figure"):
                    if nested is child:
                        continue
                    nested_img = nested.find("img")
                    if not nested_img:
                        continue
                    cap = nested.find("figcaption")
                    cap_text = cap.get_text(" ", strip=True) if cap else ""
                    _emit_image(nested_img, caption=cap_text)
                continue
            if name == "img":
                _emit_image(child)
                continue
            if name == "iframe":
                _emit_iframe(child)
                continue
            if name == "li":
                # <li> children of <ul>/<ol> emit a dedicated
                # ``list_item`` block (Decisions 1, 8 of
                # orangetrack-rendering-fixes). Bullet "• " is NOT
                # inserted here — it is prepended in
                # ``telegraph_publisher`` after LLM translation, so the
                # bullet survives any LLM stripping/translation (AC2).
                # ``<ul>`` and ``<ol>`` are treated identically per
                # Decision 8. Empty / whitespace-only ``<li>`` is
                # dropped (no block emitted).
                li_runs = _runs_from_tag(child)
                if not li_runs:
                    continue
                li_text = " ".join(r["text"] for r in li_runs).strip()
                if not li_text:
                    continue
                blocks.append({
                    "type": "list_item",
                    "text": li_text,
                    "runs": li_runs,
                })
                continue
            # Wrapper tag (div / section / article / ul / ol / etc.):
            # recurse so the inner <p>/<figure>/<iframe>/<li> get walked.
            if name not in handled_tags:
                _walk(child)

    _walk(soup)

    # ----------------------------------------------------------------
    # Post-process: filter, dedup, derive flat fields, synthesize
    # paragraphs from title for video-only posts.
    # ----------------------------------------------------------------
    blocks = filter_blocks(blocks)

    # Include paragraph, heading AND list_item text in the flat list.
    # Reason: ``_llm_common._patch_text_with_ru_paragraphs`` consumes
    # ru_paragraphs sequentially for any block whose type is in
    # ``_PATCHED_TEXT_BLOCK_TYPES`` (lead/paragraph/heading/list_item —
    # see Task 2 of orangetrack-rendering-fixes which extends that
    # tuple), and ``_translate_block_strings`` (variant B+) explicitly
    # skips text fields of patchable types (relying on _patch to fill
    # them). If list_item / heading were excluded here, ru_paragraphs
    # would be shorter than the count of patchable blocks, and trailing
    # blocks would stay in English. See SESSION-2026-05-06.md (overrides
    # tech-spec Decision 15 which originally said "h5 heading goes to
    # blocks-only").
    paragraphs_flat: List[str] = [
        b["text"] for b in blocks
        if b["type"] in ("paragraph", "heading", "list_item")
    ]
    paragraphs_flat = filter_boilerplate(paragraphs_flat)

    # Subtitle stays empty — orangetrack RSS doesn't ship a separate
    # subtitle field, and extracting the first paragraph as subtitle
    # caused an off-by-one mismatch between `paragraphs` and `blocks`:
    # body_paragraphs had K-1 entries, blocks had K paragraph-type
    # entries, so `_llm_common._patch_text_with_ru_paragraphs` consumed
    # ru translations sequentially and left the trailing block(s) in
    # English. The first paragraph stays in body_paragraphs and is
    # translated as part of the body — Telegraph just doesn't render
    # the italic `💬 «…»` lead decoration, which is acceptable.
    # See SESSION-2026-05-06.md for the incident.
    subtitle = ""
    body_paragraphs = paragraphs_flat

    # If the post is video-only (no usable paragraphs) but has a title,
    # synthesize paragraphs from the title (gating field at news_bot.py:1510).
    has_video = any(b["type"] == "video" for b in blocks)
    if not body_paragraphs and has_video and title:
        body_paragraphs = [title]

    images_flat: List[str] = [
        b["src"] for b in blocks if b["type"] == "image"
    ][:IMAGE_LIMIT]

    if not blocks and not body_paragraphs and not subtitle and not images_flat:
        return None

    return {
        "title": title,
        "subtitle": subtitle,
        "paragraphs": body_paragraphs,
        "images": images_flat,
        "blocks": blocks,
    }


# ---------------------------------------------------------------------------
# Internal: bounded-stream HTTP fallback fetch
# ---------------------------------------------------------------------------


def _fetch_article_html(
    url: str,
    notifier: Optional[Callable[[str, str], None]],
    link: str,
) -> Optional[str]:
    """Bounded-stream HTTP GET of the article URL. Returns body str or None.

    On any failure mode emits one notifier event with the appropriate
    code (``ART_FALLBACK_HTTP_<status>``, ``ART_FALLBACK_REDIRECT_<status>``,
    ``ART_FALLBACK_TIMEOUT``, ``ART_FALLBACK_TOO_LARGE``) and returns None.

    Mandatory: ``allow_redirects=False`` (no redirect-bypass SSRF) and
    streamed body via ``iter_content`` capped at ``MAX_RESPONSE_SIZE``.
    """
    def _ping(code: str) -> None:
        if notifier is not None:
            try:
                notifier(code, link)
            except Exception:
                logger.exception("orangetrack notifier raised in fallback fetch")

    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=False,
        )
    except requests.Timeout:
        _ping("ART_FALLBACK_TIMEOUT")
        return None
    except requests.RequestException as exc:
        logger.warning("orangetrack fallback transport error for %s: %s", url, exc)
        _ping("ART_FALLBACK_TIMEOUT")
        return None

    status = response.status_code
    # 3xx — reject redirects (SSRF hardening).
    if 300 <= status < 400:
        try:
            response.close()
        except Exception:
            pass
        _ping(f"ART_FALLBACK_REDIRECT_{status}")
        return None
    # 4xx / 5xx — surface status.
    if status >= 400:
        try:
            response.close()
        except Exception:
            pass
        _ping(f"ART_FALLBACK_HTTP_{status}")
        return None

    # 200 — stream body with cap.
    buf = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > MAX_RESPONSE_SIZE:
                _ping("ART_FALLBACK_TOO_LARGE")
                try:
                    response.close()
                except Exception:
                    pass
                return None
    except requests.Timeout:
        _ping("ART_FALLBACK_TIMEOUT")
        return None
    except requests.RequestException as exc:
        logger.warning(
            "orangetrack fallback streaming error for %s: %s", url, exc,
        )
        _ping("ART_FALLBACK_TIMEOUT")
        return None
    finally:
        try:
            response.close()
        except Exception:
            pass

    # Decode response. Use response.encoding if set, else utf-8 fallback.
    encoding = response.encoding or "utf-8"
    try:
        full_html = bytes(buf).decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        full_html = bytes(buf).decode("utf-8", errors="replace")

    # Scope to article body — full page contains site chrome (header,
    # nav, sidebar, footer, comment form, related posts, affiliate
    # banners) which the parser would otherwise drag into Telegraph
    # output. orangetrack uses HTML5 `<article>` element; selector chain
    # below is defensive in case of theme drift.
    try:
        page = BeautifulSoup(full_html, "html.parser")
    except Exception:
        return full_html  # last-resort: return full page if BS4 chokes
    article_node = (
        page.find("article")
        or page.find("div", class_="entry-content")
        or page.find("main")
    )
    if article_node is not None:
        return str(article_node)
    # No recognizable article container — log and return full page so
    # at least *something* gets parsed (downstream will likely produce
    # noisy output but it won't crash). Operator sees noisy fallback as
    # signal that the theme changed.
    logger.warning(
        "orangetrack fallback: no <article>/entry-content/<main> found "
        "for %s; passing full page to parser",
        url,
    )
    return full_html


# ---------------------------------------------------------------------------
# Public: parse one feed entry
# ---------------------------------------------------------------------------


def fetch_orangetrack_article(
    entry: dict,
    notifier: Optional[Callable[[str, str], None]] = None,
) -> Optional[Dict]:
    """Get the article body for an orangetrackdiecast feed entry.

    Primary path: parse ``content:encoded`` if present and non-empty (silent
    on success). Fallback: ``_is_allowed_orangetrack_url`` → bounded-stream
    HTTP GET → parse.

    ``notifier`` signature is ``Callable[[str, str], None]`` — pair
    ``(code, link)``. Wired in ``_fetch_orangetrack_entries`` to
    ``OrangetrackPingAggregator.add``.
    """
    link = entry.get("link") or ""

    # Primary: content:encoded carries the full body.
    raw_html = ""
    # feedparser often exposes content:encoded as ``entry.content`` (a
    # list of dicts with 'value'); also check ``content_encoded`` as a
    # plain string fallback.
    content_field = entry.get("content")
    if content_field:
        if isinstance(content_field, list):
            for c in content_field:
                if isinstance(c, dict) and c.get("value"):
                    raw_html = c["value"]
                    break
        elif isinstance(content_field, str):
            raw_html = content_field
    if not raw_html:
        raw_html = entry.get("content_encoded") or ""
    if not raw_html:
        # feedparser sometimes provides via ``summary`` if config differs;
        # for orangetrack the canonical field is ``content``.
        raw_html = ""

    if raw_html:
        # WordPress.com RSS sometimes strips iframe/video from content:encoded
        # but leaves the empty wrapper. Detect this BEFORE accepting parse
        # results, so we trigger the HTTP fallback which pulls the live page
        # (where the iframe is intact) instead of returning a video-less
        # parse from feed-only data. See `_has_empty_embed_wrapper` docstring.
        feed_incomplete = _has_empty_embed_wrapper(raw_html)
        try:
            parsed = _parse_content_encoded(raw_html, link)
        except Exception:
            logger.exception(
                "orangetrack content:encoded parse failed for %s", link,
            )
            if notifier is not None:
                try:
                    notifier("ART_PARSE_EXCEPTION", link)
                except Exception:
                    logger.exception("orangetrack notifier raised after parse exc")
            return None
        if parsed and not feed_incomplete:
            # Successful primary path — silent.
            # Fall back to RSS title when content:encoded didn't carry h1.
            if not parsed.get("title"):
                parsed["title"] = entry.get("title") or ""
            # Re-run the video-only synthesis using the now-filled title:
            # _parse_content_encoded couldn't synthesize when its h1 was
            # empty (content:encoded rarely carries one).
            has_video = any(
                b.get("type") == "video" for b in (parsed.get("blocks") or [])
            )
            if (
                not parsed.get("paragraphs")
                and not parsed.get("subtitle")
                and has_video
                and parsed.get("title")
            ):
                parsed["paragraphs"] = [parsed["title"]]
            return parsed
        if feed_incomplete:
            logger.info(
                "orangetrack feed has empty wp-block-embed wrapper for %s; "
                "falling back to HTTP scrape to recover stripped iframe",
                link,
            )
        # Empty parse OR incomplete feed → fall through to HTTP fallback
        # (notifier called below only if HTTP fallback also fails).

    # Fallback: HTTP GET. Allowlist guard FIRST.
    if not link:
        return None
    if not _is_allowed_orangetrack_url(link):
        if notifier is not None:
            try:
                notifier("ART_FALLBACK_HOST_REJECTED", link)
            except Exception:
                logger.exception("orangetrack notifier raised on host-rejected")
        return None

    body = _fetch_article_html(link, notifier, link)
    if body is None:
        return None

    try:
        parsed = _parse_content_encoded(body, link)
    except Exception:
        logger.exception(
            "orangetrack fallback parse exception for %s", link,
        )
        if notifier is not None:
            try:
                notifier("ART_PARSE_EXCEPTION", link)
            except Exception:
                logger.exception("orangetrack notifier raised after parse exc")
        return None

    if not parsed:
        if notifier is not None:
            try:
                notifier("ART_FALLBACK_PARSE_EMPTY", link)
            except Exception:
                logger.exception("orangetrack notifier raised on parse-empty")
        return None

    if not parsed.get("title"):
        parsed["title"] = entry.get("title") or ""
    return parsed


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _code_sort_key(code: str) -> Tuple[int, str]:
    """Group codes: FEED_* < ENTRY_* < ART_* — alphabetical within."""
    if code.startswith("FEED_"):
        return (0, code)
    if code.startswith("ENTRY_"):
        return (1, code)
    if code.startswith("ART_"):
        return (2, code)
    return (3, code)


class OrangetrackPingAggregator:
    """Collects (code, link) tuples during one cron tick and emits a single
    aggregated admin-ping at end via ``emit(send_fn)``.

    Bounds (Decision 5):
      * per-code link list capped at ``_MAX_LINKS_PER_CODE`` (50) entries;
      * total ``add()`` calls capped at ``_MAX_TOTAL_EVENTS`` (500) — past
        that, calls are silent no-ops (NOT raises);
      * ``format_summary()`` truncates rendered output to
        ``_MAX_SUMMARY_CHARS`` (3500).

    Each ``add()`` runs ``code`` and ``link`` through ``_safe_for_ping``
    so attacker-controlled control chars can't smuggle fake summary
    lines into the admin-ping.
    """

    def __init__(self, instance_label: Optional[str] = None) -> None:
        self.instance_label = (instance_label or "").strip()
        # {code: {link: count}} preserving insertion order via dict semantics.
        self._events: Dict[str, Dict[str, int]] = {}
        self._total_calls = 0
        # Total count of add() invocations that landed AFTER the
        # _MAX_TOTAL_EVENTS guard but BEFORE any per-code-link cap dropped
        # them. Used by format_summary to reflect real event volume in the
        # header even when the per-code cap (50 links) trips. Distinct from
        # _total_calls, which counts ALL calls (including ones the
        # _MAX_TOTAL_EVENTS guard rejects); _total_added counts events that
        # passed the global guard regardless of the per-code cap.
        self._total_added = 0
        # Per-code link-list-truncated flag — for the "… N more truncated"
        # marker. Stored separately so we don't grow the dict past cap.
        self._truncated_count: Dict[str, int] = {}

    def add(self, code: str, link: str) -> None:
        # Count every add() invocation up-front so format_summary() can
        # reflect true event volume in the header — independent of either
        # the global 500-cap (which silent-drops further calls) or the
        # per-code link cap (which truncates the bullet list).
        self._total_added += 1
        if self._total_calls >= _MAX_TOTAL_EVENTS:
            # Silent no-op for storage — pathological flood guard. The
            # header (via _total_added) still reflects the true call
            # volume, which is what the operator needs for severity.
            return
        self._total_calls += 1
        safe_code = _safe_for_ping(code)
        safe_link = _safe_for_ping(link)
        bucket = self._events.setdefault(safe_code, {})
        if safe_link in bucket:
            bucket[safe_link] += 1
            return
        if len(bucket) >= _MAX_LINKS_PER_CODE:
            self._truncated_count[safe_code] = (
                self._truncated_count.get(safe_code, 0) + 1
            )
            return
        bucket[safe_link] = 1

    def is_empty(self) -> bool:
        return not self._events

    def format_summary(self) -> str:
        if self.is_empty():
            return ""
        # Header reflects the TRUE volume of events that fired during the
        # tick (every add() call that passed the global 500-cap), so an
        # operator triaging a flood sees the real severity even when the
        # per-code link cap (50) drops some entries from the bullet list.
        total_events = self._total_added
        prefix = f"[{self.instance_label}] " if self.instance_label else ""
        header = f"{prefix}{admin_alerts.alert_orangetrack_summary_header(total_events)}"
        lines = [header]
        for code in sorted(self._events.keys(), key=_code_sort_key):
            bucket = self._events[code]
            # count_total includes the per-code truncated overflow so the
            # bullet count matches the header semantic (events fired, not
            # links stored).
            extra_truncated = self._truncated_count.get(code, 0)
            count_total = sum(bucket.values()) + extra_truncated
            link_list = list(bucket.keys())
            link_str = ", ".join(link_list)
            if extra_truncated:
                link_str += f" … {extra_truncated} more truncated"
            lines.append(
                f"  • {code} ({count_total}×) — {link_str}"
            )
        out = "\n".join(lines)
        if len(out) > _MAX_SUMMARY_CHARS:
            # Truncate with marker so operator sees it was clipped.
            ellipsis = "\n… [truncated]"
            cut = _MAX_SUMMARY_CHARS - len(ellipsis)
            out = out[:cut] + ellipsis
        return out

    def emit(self, send_fn: Callable[[str], None]) -> None:
        """Send the formatted summary via ``send_fn``. Swallows + logs any
        exception so admin-ping delivery failure doesn't break the cron tick.
        """
        if self.is_empty():
            return
        text = self.format_summary()
        if not text:
            return
        try:
            send_fn(text)
        except Exception:
            logger.exception(
                "OrangetrackPingAggregator.emit: send_fn raised; swallowing"
            )
