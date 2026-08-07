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
import dom_blocks

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

# Per-socket-op timeout for the fallback HTTP scrape via ``requests.get``.
# Bumped 2026-06-09 from 15s → 30s after two consecutive ticks emitted
# ``ART_FALLBACK_TIMEOUT`` (E030) for the same heavy case-report pages:
#   /hot-wheels-2026-fast-furious-premium-q-case-report/
#   /hot-wheels-2026-pop-culture-r-case-report/
# Case-reports are image-heavy (20-30 hot-linked photos) and streamed
# until ``MAX_RESPONSE_SIZE`` (5 MB) — 15s was tight when Cloudflare's
# edge was slow. 30s remains safe — the gate stays cheap on quick pages
# (most return in <2s) and pings only when something is genuinely wrong.
REQUEST_TIMEOUT = 30
MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5 MB
IMAGE_LIMIT = 10
_CHUNK_SIZE = 8 * 1024  # 8 KB chunks for iter_content

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

    Thin adapter over :func:`dom_blocks.video_embed_url` with orangetrack's
    provider DATA applied. The name and the single-argument signature are
    part of the module's surface — ``tests/test_orangetrack_source.py``
    imports and calls it directly in six tests.

    Vimeo stays unwrapped here on purpose (``test_vimeo_not_wrapped``):
    Vimeo hosts exist in ``dom_blocks`` as ANOTHER provider's data and must
    not reach orangetrack through a default or a "all known hosts" list.
    """
    return dom_blocks.video_embed_url(
        youtube_url, hosts=_YOUTUBE_HOSTS, provider="youtube"
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
# Internal: orangetrack's colour-class policy
# ---------------------------------------------------------------------------


#: WordPress chrome classes whose subtree is skipped entirely. orangetrack's
#: per-site junk-class policy, injected into ``dom_blocks.BlockBuilder``.
_CHROME_CLASS_MARKERS = (
    "sharedaddy", "sd-", "taxonomies", "jp-related",
    "post-comments", "comment-form",
)



def _has_color_class(node) -> bool:
    """True if this BS4 element carries any WordPress-Gutenberg color class.

    Telegraph API does not support text color, so we map colored text to
    bold (visual emphasis preserved) — see ``_render_paragraph_with_runs``
    in ``telegraph_publisher.py``. We accept ANY class containing the
    substring ``-color`` (e.g. ``has-vivid-red-color``,
    ``has-vivid-cyan-blue-color``, ``has-text-color``) as a signal of
    "this span is visually emphasised".

    This is the ONE genuine per-site hook of ``dom_blocks.runs_from_tag``;
    the walker itself is shared.
    """
    classes = node.get("class") or []
    return any("-color" in (c or "") for c in classes)


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
            url = dom_blocks.safe_img_src(picks[0][0])
            if url:
                return url
    src = dom_blocks.safe_img_src(img.get("src"))
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

    #: WordPress chrome class markers — a div/section carrying one of these
    #: is skipped entirely (no recursion into it). These are the "Share this:"
    #: buttons (sharedaddy), the tag/category list (taxonomies), JetPack
    #: related posts (jp-related), comment forms. Discovered 2026-05-06 on the
    #: Porsche Targa Turbo republish, where they bled into the Telegraph
    #: article. Genuine content like the "Original listing on Mattel
    #: Creations: …" paragraph stays — the operator wants it.
    def _has_chrome_class(child) -> bool:
        classes = child.get("class") or []
        for c in classes:
            cl = c.lower()
            for marker in _CHROME_CLASS_MARKERS:
                if marker in cl:
                    return True
        return False

    # One builder per article — never a module-level singleton, or blocks and
    # the image-dedup set would leak between articles. The four per-site seams
    # below are orangetrack's entire configuration of the shared walker; the
    # structural policy (h2/h3/h4 → level 3, h5 → paragraph, h1/h6 dropped,
    # <p> split at <br>, <li> without a bullet) is the shared default.
    #
    # ``headings_from_bold`` stays FALSE: orangetrack authors real <h2>/<h3>
    # section headers, and inferring extra headings from whole-bold paragraphs
    # would change its published output — an AC10 violation.
    builder = dom_blocks.BlockBuilder(
        has_color_class=_has_color_class,
        is_chrome_class=_has_chrome_class,
        pick_img_src=_best_img_src,
        image_dedup_key=lambda src: src.split("?", 1)[0],
        video_hosts=_YOUTUBE_HOSTS,
        video_provider="youtube",
        headings_from_bold=False,
    )
    builder.walk(soup)
    blocks = builder.blocks

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
        # ``instance_label`` is accepted for backward-compat with existing
        # callers (notably ``news_bot._fetch_orangetrack_entries``) but is
        # NO LONGER USED for prefixing. Prior behaviour prepended
        # ``[label] `` to ``format_summary()`` output; that combined with
        # ``send_admin_notification`` (which ALSO prepends INSTANCE_LABEL
        # for every admin-bound message) produced the ``[test] [test]
        # [E030] …`` double-prefix observed in prod 2026-06-08. The single
        # source of truth for prefixing is now ``send_admin_notification``.
        # The parameter remains in the signature so external test code
        # / docs that pass it don't break — silently dropped here.
        del instance_label  # explicit "unused" signal for linters
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
        # NO inline [instance_label] prefix here — send_admin_notification
        # prepends it once for every admin-bound message (news_bot.py:412).
        # Adding it here too produced the [test] [test] [E030] double-prefix
        # incident 2026-06-08.
        header = admin_alerts.alert_orangetrack_summary_header(total_events)
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
