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
from boilerplate_filter import filter_boilerplate

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

    paragraphs: List[str] = []
    for tag in body.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
        text = tag.get_text(" ", strip=True)
        if text and text != title:
            paragraphs.append(text)

    # CRITICAL ORDER: boilerplate filter BEFORE subtitle lift, otherwise
    # a Blogger footer ("Marcadores: ...") at the top of a title-only
    # post would float into ``subtitle``. PT patterns arrive in Task 4
    # without touching this call site; today only EN labels strip.
    paragraphs = filter_boilerplate(paragraphs)

    # Lift first surviving paragraph as subtitle (editorial lead on the
    # Telegraph page); drop it from body so it doesn't repeat below.
    # Photo-gallery posts (single intro paragraph + many product images)
    # are the dominant t-hunted format for new-arrival announcements — for
    # those we keep the one paragraph in body and ship an empty subtitle,
    # so news_bot.fetch_full_article does NOT drop the post on its
    # ``not article.get('paragraphs')`` guard. Lamley keeps the
    # unconditional lift because lamley posts are review-style and always
    # carry 2+ paragraphs.
    if len(paragraphs) >= 2:
        subtitle = paragraphs[0]
        paragraphs = paragraphs[1:]
    else:
        subtitle = ""

    # Blogger-aware image dedup: lamley's ``split("?")`` assumes size
    # in query params; Blogger encodes size in the path. Strip query +
    # trailing ``=sNNN(-c)?`` segment. First-seen URL wins.
    images: List[str] = []
    seen_bases = set()
    for img in body.find_all("img"):
        src = img.get("src") or ""
        if not src.startswith("http"):
            continue
        # Blogger lightbox sandwich:
        #   <a href="https://.../s1200/photo.jpg">      ← FULL-SIZE
        #     <img src="https://.../w200-h200/photo.jpg" />  ← 200×200 thumb
        #   </a>
        # ``img.src`` is the grid thumbnail; the full-resolution variant
        # lives in the wrapping ``<a href>``. Telegraph embeds the src URL
        # verbatim (no re-hosting), so without this lift subscribers see
        # 200×200 minis instead of full photos.
        parent = img.parent
        if parent is not None and parent.name == "a":
            href = parent.get("href") or ""
            if href.startswith("http") and _is_blogger_image_url(href):
                src = href
        base = _BLOGGER_SIZE_SUFFIX_RE.sub('', src.split("?", 1)[0])
        if base in seen_bases:
            continue
        seen_bases.add(base)
        images.append(src)
        if len(images) >= _IMAGE_LIMIT:
            break

    return {
        "title": title,
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
    }
