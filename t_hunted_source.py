#!/usr/bin/env python3
"""t-hunted.blogspot.com news source.

Fourth source-parser for the Hot Wheels news bot. Scrapes Portuguese-
language articles from the Blogger-hosted blog ``t-hunted.blogspot.com``;
the RSS feed (Blogger Atom) carries only title + ~150-char excerpt.

Trimmed copy of ``lamley_source.py``: same contract
``fetch_t_hunted_article(link, session, notifier) -> dict | None``, same
SSRF-allowlist pattern, same flat-walk HTML extraction. Drops the WAF/
throttle/curl_cffi apparatus — Blogger is not Cloudflare-fronted. Adds
a Blogger-aware image-dedup step (size token lives in the URL path,
not query params) and ``<h3 class="post-title">`` / ``<div class="post-body">``
as the canonical title / body selectors.

SSRF guard: ``_is_allowed_t_hunted_url`` is an exact match against the
single-element allowlist ``('t-hunted.blogspot.com',)``. The upstream
dispatcher in ``news_bot.fetch_full_article`` uses a permissive
substring check (``'blogspot.com' in domain``) — this function is the
hard gate closing that hole.

See ``work/completed/t-hunted-pt-source/`` tech-spec Task 1 and code-research §B
for the line-precise design.
"""

import logging
import re
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import admin_alerts
import dom_blocks
import feature_flags
from boilerplate_filter import filter_blocks

logger = logging.getLogger(__name__)

#: Single-host SSRF allowlist. Exact-match only — NOT a glob, endswith,
#: or regex. Adding a new Blogger source means appending an exact hostname
#: here; no pattern syntax supported by design.
_ALLOWED_HOSTS = ('t-hunted.blogspot.com',)

_TIMEOUT_SECONDS = 15
#: Higher than lamley's 10 because t-hunted's new-arrival photo-gallery
#: posts routinely carry 15-25 product photos (e.g. a full Car Culture
#: set unboxing). Capped to avoid pathological pages with embedded
#: ad / reaction-emoji <img> tags blowing out the Telegraph upload.
_IMAGE_LIMIT = 30
#: Defensive hard cap. Blogger posts are tiny (<200KB observed); 2MB
#: drops obvious DOS / error pages before BeautifulSoup runs.
_MAX_BYTES = 2_000_000
#: Plain UA — Blogger doesn't Chrome-fingerprint. Avoids the
#: ``python-requests/*`` default which some CDN edges auto-block.
_USER_AGENT = 'Mozilla/5.0 (compatible; HotWheelsNewsBot/1.0)'

#: Blogger size-suffix stripper: ``=s1600`` / ``=s640`` / ``=s320-c`` —
#: at end-of-string (``.../=s1600``) or followed by ``/``
#: (``.../=s1600/photo.jpg``). Tech-spec AC literally specifies
#: ``r'=s\d+(-c)?$'`` (trailing only); the ``(?:/|$)`` alternation
#: covers Blogger's actual mid-path shape too (see test
#: ``test_image_dedup_strips_blogger_size_suffix``; deviation noted in
#: feature decisions.md). Bounded quantifiers — ReDoS-safe.
_BLOGGER_SIZE_SUFFIX_RE = re.compile(r'=s\d+(-c)?(?:/|$)')


#: Canonical Blogger image-CDN hosts. ``blogger.googleusercontent.com``
#: is the modern shared CDN; ``*.bp.blogspot.com`` (1.bp / 2.bp / 3.bp / 4.bp)
#: is the legacy Picasa-era host still present on older posts. Membership
#: is by exact hostname suffix to keep ``_is_blogger_image_url`` ReDoS-safe
#: and to reject same-substring decoy URLs (e.g. off-site trackers that
#: pass the canonical host in a query parameter).
_BLOGGER_IMAGE_HOSTS = ('blogger.googleusercontent.com', 'bp.blogspot.com')


def _is_blogger_image_url(url: str) -> bool:
    """Return True iff *url*'s hostname ends with a canonical Blogger CDN host.

    Used to guard the lightbox-anchor lift in image extraction: lift the
    parent ``<a href>`` only when it points at a sibling Blogger image
    variant, not at an off-site click-tracker that happens to mention
    ``blogger`` in its path or query.
    """
    try:
        host = (urlparse(url).hostname or '').lower()
    except (ValueError, AttributeError):
        return False
    return any(host == h or host.endswith('.' + h) for h in _BLOGGER_IMAGE_HOSTS)


#: Block types whose ``text`` the LLM layer patches positionally
#: (``_llm_common._PATCHED_TEXT_BLOCK_TYPES``). The flat ``paragraphs`` list
#: MUST have exactly one entry per block of these types — see the alignment
#: note in ``fetch_t_hunted_article``.
_PATCHABLE_BLOCK_TYPES = ("lead", "paragraph", "heading", "list_item")


def _pick_img_src(img) -> Optional[str]:
    """t-hunted's image-src policy — the Blogger lightbox sandwich.

    ``<a href="…/s1200/photo.jpg"><img src="…/w200-h200/photo.jpg"></a>``:
    ``img.src`` is the grid THUMBNAIL and the full-resolution variant lives in
    the wrapping anchor. Telegraph embeds the URL verbatim, so without this
    lift subscribers get 200×200 minis instead of photographs.

    The anchor is only trusted when it points at a sibling Blogger image
    variant, never at an off-site click-tracker that merely mentions
    ``blogger`` somewhere. Scheme checking goes through the shared
    ``safe_img_src`` rather than ``startswith("http")``, which used to let
    ``httpx://evil/x.jpg`` through.
    """
    src = dom_blocks.safe_img_src(img.get("src"))
    parent = img.parent
    if parent is not None and getattr(parent, "name", None) == "a":
        href = parent.get("href") or ""
        if _is_blogger_image_url(href):
            full = dom_blocks.safe_img_src(href)
            if full:
                return full
    return src


def _image_dedup_key(src: str) -> str:
    """t-hunted's image-dedup policy.

    lamley's ``split("?")`` assumes the size sits in the query string; Blogger
    puts it in the PATH (``=s1600`` / ``=s640`` / ``=s320-c``), so two URLs for
    the same photo differ by a path segment. Strip the query AND the size
    suffix; first-seen URL wins.
    """
    return _BLOGGER_SIZE_SUFFIX_RE.sub('', src.split("?", 1)[0])


def _norm_for_title_compare(text: str) -> str:
    """Normalise for the title-dedup predicate by dropping ALL whitespace.

    The two sides come from DIFFERENT flatteners and must be made comparable
    before they are compared. ``title`` is built with
    ``get_text(" ", strip=True)`` — ``<p>a<b>b</b>c</p>`` becomes ``'a b c'``
    — while a block's text comes from ``dom_blocks.text_from_runs``, which
    joins runs with no separator and yields ``'abc'``. Comparing them raw
    would make the predicate quietly stop matching, and a paragraph repeating
    the title would survive in ``blocks`` while never having been in
    ``paragraphs``: the SECOND source of list desync (code-research § II-3.2).
    """
    return "".join((text or "").split())


def _notify(notifier: Optional[Callable[[str], None]], message: str) -> None:
    """Log alert at ERROR + best-effort call ``notifier``.

    Verbatim shape from ``lamley_source._notify`` — guarantees the alert
    is in journalctl even when the notifier callback itself raises.
    """
    logger.error(message)
    if notifier is None:
        return
    try:
        notifier(message)
    except Exception:
        logger.exception("Failed to send admin notification")


def _is_allowed_t_hunted_url(link: str) -> bool:
    """Return True iff *link* is an http(s) URL on an allowlisted host.

    SSRF gate inside the parser. Rejects: foreign hosts, loopback /
    link-local IPs, other Blogger subdomains, userinfo-attack URLs
    (``http://t-hunted.blogspot.com@evil.com/`` — ``urlparse(...).hostname``
    correctly yields the post-@ host), suffix-host attacks
    (``t-hunted.blogspot.com.attacker.example``), subdomain variants
    (``www.t-hunted.blogspot.com``), and non-http schemes.
    """
    try:
        parsed = urlparse(link)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    return host in _ALLOWED_HOSTS


def fetch_t_hunted_article(
    link: str,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
    """Fetch and parse a single t-hunted Blogger article.

    Returns ``{'title', 'subtitle', 'paragraphs', 'images'}`` on success
    (always string / list values, never inner ``None``), or ``None`` on
    SSRF rejection, HTTP error, oversize body, or missing body wrapper.

    On host-rejected / fetch-error / no-body, ``notifier`` is invoked
    once with an admin-alert string from ``admin_alerts.alert_t_hunted_*``
    (E031-E033 — built in Task 2). Caller binds ``notifier`` to
    ``send_admin_notification`` in ``news_bot.fetch_full_article``.
    """
    if not _is_allowed_t_hunted_url(link):
        _notify(notifier, admin_alerts.alert_t_hunted_host_rejected(link))
        return None

    session = session or requests.Session()
    try:
        response = session.get(
            link,
            timeout=_TIMEOUT_SECONDS,
            headers={'User-Agent': _USER_AGENT},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _notify(
            notifier,
            admin_alerts.alert_t_hunted_fetch_error(link, str(exc)),
        )
        return None

    if len(response.content) > _MAX_BYTES:
        # Defensive WARN (no admin alert by design — Blogger doesn't
        # serve oversize bodies; this is a DOS / DOM-exploit gate).
        logger.warning(
            "t-hunted: response body too large (%d bytes) — dropping %s",
            len(response.content), link,
        )
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    # Strip inline scripts/styles so their JS doesn't end up in body text.
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    # Title: <h3 class="post-title"> canonical for Blogger; fall back
    # to <h1 class="entry-title">, then bare <h1>.
    title_tag = (
        soup.find("h3", class_="post-title")
        or soup.find("h1", class_="entry-title")
        or soup.find("h1")
    )
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    # Body wrapper: <div class="post-body"> canonical; fall back to
    # lamley-style selectors so atypical themes still parse.
    body = (
        soup.find("div", class_="post-body")
        or soup.find("div", class_="entry-content")
        or soup.find("article")
    )
    if body is None:
        _notify(notifier, admin_alerts.alert_t_hunted_no_body(link))
        return None

    # ONE list is the source of truth — the blocks. The flat `paragraphs` is
    # DERIVED from it after every removal, never maintained in parallel.
    #
    # Why that matters more than it looks: the subtitle lift moves one entry
    # out, and if the two lists were filtered separately they would end up
    # differing by one. `_llm_common` pairs them POSITIONALLY when encoding
    # and consumes them sequentially when decoding, and both sides swallow a
    # shortfall silently (`except StopIteration`, no log) — so the tail block
    # would ship to the channel IN PORTUGUESE. That is the 2026-05-06 outage,
    # after which orangetrack answered by hardcoding `subtitle = ""`. The
    # operator wants the lead, so the lift has to be a single operation over a
    # single list. Measured: the lift desyncs 9 of these 10 articles if done
    # the naive way (code-research § II-3.2).
    #
    # The heading heuristic is switched ON here because t-hunted has ZERO real
    # <h2>/<h3>/<h4> inside `div.post-body` across all 10 corpus articles — a
    # whole-bold paragraph is its only section marker (§ II-2.2).
    builder = dom_blocks.BlockBuilder(
        pick_img_src=_pick_img_src,
        image_dedup_key=_image_dedup_key,
        video_hosts=dom_blocks.YOUTUBE_HOSTS,
        video_provider="youtube",
        headings_from_bold=True,
    )
    builder.walk(body)
    blocks = builder.blocks

    # Title-dedup. Blogger themes repeat the post title as the first
    # paragraph of the body; dropping it is parser-local policy the shared
    # walker has no business knowing about. Applied to the BLOCKS, so it
    # cannot desync the two lists.
    title_key = _norm_for_title_compare(title)
    if title_key:
        blocks = [
            b for b in blocks
            if not (
                b.get("type") in _PATCHABLE_BLOCK_TYPES
                and _norm_for_title_compare(b.get("text")) == title_key
            )
        ]

    # CRITICAL ORDER: boilerplate filter BEFORE subtitle lift, otherwise
    # a Blogger footer ("Marcadores: ...") at the top of a title-only
    # post would float into ``subtitle``.
    blocks = filter_blocks(blocks)

    # Lift the first surviving text block as subtitle (the editorial lead on
    # the Telegraph page) and REMOVE it from blocks in the same operation, so
    # it neither publishes twice nor unbalances the lists.
    #
    # Photo-gallery posts (single intro paragraph + many product images) are
    # the dominant t-hunted format for new-arrival announcements. For those we
    # keep the one paragraph in the body and ship an empty subtitle, so
    # news_bot.fetch_full_article does NOT drop the post on its
    # ``not article.get('paragraphs')`` guard — that is Hotfix 1 of
    # work/completed/t-hunted-pt-source. The >= 2 count is taken over the SAME
    # list the lift removes from; counting one list and lifting from another
    # is exactly the desync this function is built to avoid.
    patchable_idx = [
        i for i, b in enumerate(blocks)
        if b.get("type") in _PATCHABLE_BLOCK_TYPES
    ]
    if len(patchable_idx) >= 2:
        lead = blocks.pop(patchable_idx[0])
        subtitle = lead.get("text") or ""
    else:
        subtitle = ""

    # Flat lists DERIVED from the surviving blocks. `heading` and `list_item`
    # belong here alongside `paragraph`: they are in
    # `_llm_common._PATCHED_TEXT_BLOCK_TYPES`, and leaving them out would
    # shift the pairing by one at every heading.
    paragraphs: List[str] = [
        b["text"] for b in blocks
        if b.get("type") in ("paragraph", "heading", "list_item")
    ]
    images: List[str] = [
        b["src"] for b in blocks if b.get("type") == "image"
    ][:_IMAGE_LIMIT]

    article = {
        "title": title,
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
    }
    # Decision 6: the kill switch gates block EMISSION here, in the parser —
    # never inside `dom_blocks`, which orangetrack also consumes and where a
    # gate would strip ITS blocks too. The walk runs either way, so the flat
    # text is identical in both flag states; "off" means plain text, NOT a
    # byte-for-byte return to the pre-feature output (measured drift for
    # t-hunted: −0.10 %, only collapsed \xa0 and a space before punctuation).
    if feature_flags.source_formatting_enabled():
        article["blocks"] = blocks
    return article
