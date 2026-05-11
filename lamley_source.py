#!/usr/bin/env python3
"""Lamley Group news source.

lamleygroup.com is a WordPress blog whose RSS carries only a short excerpt
(~100 chars). `fetch_lamley_article` scrapes the individual article page,
extracts the full body paragraphs and images (capped to keep Telegraph
pages reasonable).

Rate limiting: lamleygroup.com's WordPress install rate-limits fast bursts
(observed: ~10 requests/sec triggers HTTP 429). Two-layer mitigation:
  * Module-level throttle (`_MIN_REQUEST_INTERVAL_S`) — a soft client-side
    floor between consecutive fetches in the same process.
  * On 429 — sleep `Retry-After` (or a fallback) and retry ONCE before
    giving up; subsequent failures still surface to the operator.
"""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from boilerplate_filter import filter_boilerplate
import admin_alerts

logger = logging.getLogger(__name__)

#: ``curl_cffi`` impersonates a real Chrome's TLS/JA3 fingerprint, which
#: Cloudflare-style bot management explicitly checks for. Plain ``requests``
#: ships a Python TLS handshake that the WAF flags before any header is
#: even read. We fall back to ``requests`` if the optional dep is missing
#: so tests / dev environments without the wheel keep working.
try:
    from curl_cffi import requests as _cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover — only triggers in stripped envs
    _cffi_requests = None
    _CFFI_AVAILABLE = False

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Full browser-style header set. Lamley sits behind a Cloudflare-like
#: WAF that scores requests on header completeness — sending only
#: ``User-Agent`` flags the request as scripted (the bot was getting 429
#: in field tests despite a plausible UA). These headers mirror what a
#: real Chrome 131 sends on a top-level navigation: Accept-Language and
#: Accept-Encoding most of all, plus the Sec-Fetch-* and Sec-Ch-Ua-*
#: client hints that Cloudflare bot-management explicitly checks for.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "DNT": "1",
    "Connection": "keep-alive",
}
REQUEST_TIMEOUT = 15
MAX_RESPONSE_SIZE = 5 * 1024 * 1024
IMAGE_LIMIT = 10

#: Minimum gap between two consecutive Lamley fetches in the same process.
#: Field-tested: 2 s still triggered 429 from Lamley's WAF for back-to-back
#: bursts. 20 s gives lamleygroup.com room to breathe between requests.
#: Cost: 10 articles → +200 s (≈ 3.5 min) added to the daily 12:00 МСК
#: cron prep phase — still leaves a wide margin before the 13:00 МСК
#: publish window opens.
_MIN_REQUEST_INTERVAL_S = 20.0

#: How long to wait when a 429 response arrives without a usable
#: ``Retry-After`` header.
_DEFAULT_RETRY_AFTER_S = 30.0

#: After this many consecutive 429s in the current process, declare a
#: WAF-level lockout and pause ALL Lamley fetches for ``_COOLDOWN_S``.
#: Operator-tuned: 5 strikes is enough to detect "we're in the
#: penalty box" without overreacting to a couple of unlucky responses.
_429_THRESHOLD = 5

#: How long to pause Lamley fetches after the consecutive-429 threshold
#: trips. 1 hour gives Lamley's WAF time to forget about us.
_COOLDOWN_S = 3600.0  # 1 hour

#: Per-URL blacklist TTL — a URL that hit 429 (after the per-request
#: retry already failed) is skipped for this long even after the
#: process-wide cool-down expires. Most Lamley articles are timeless
#: enough that 24 h of staleness is acceptable; this stops the bot
#: from re-hitting known-bad URLs every cron tick.
_URL_BLACKLIST_S = 86400.0  # 24 hours

#: Throttle state. ``threading.Lock`` is sufficient because the cron-side
#: bot is single-threaded; the lock is defensive only.
_throttle_lock = threading.Lock()
_last_request_time: float = 0.0

#: WAF-protection state. Reset on process restart — that's intentional;
#: a fresh process should re-probe Lamley and find out itself whether
#: the lockout cleared.
_consecutive_429_count: int = 0
_cooldown_until: float = 0.0
#: ``{url: time.monotonic() expiry}``. Stale entries are cleaned up
#: lazily on each fetch — a 24-hour TTL × ~10 articles/day caps the
#: dict at low double digits without a sweeper job.
_url_blacklist: dict[str, float] = {}


def _is_in_cooldown() -> bool:
    """True if the process is currently locked out of Lamley fetches."""
    return time.monotonic() < _cooldown_until


def _is_url_blacklisted(url: str) -> bool:
    """True if this specific URL hit 429 within the last
    ``_URL_BLACKLIST_S`` and should be skipped on this attempt."""
    expiry = _url_blacklist.get(url)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        # Lazy cleanup — expired entry, drop it.
        _url_blacklist.pop(url, None)
        return False
    return True


def _record_429(url: str) -> None:
    """Per-URL blacklist + consecutive-429 counter. May trip the
    process-wide cool-down if the threshold is reached."""
    global _consecutive_429_count, _cooldown_until
    _url_blacklist[url] = time.monotonic() + _URL_BLACKLIST_S
    _consecutive_429_count += 1
    if _consecutive_429_count >= _429_THRESHOLD:
        _cooldown_until = time.monotonic() + _COOLDOWN_S
        logger.warning(
            "Lamley consecutive 429 threshold hit (%d/%d) — entering "
            "process-wide cool-down for %.0f minutes",
            _consecutive_429_count, _429_THRESHOLD, _COOLDOWN_S / 60,
        )


def _record_success() -> None:
    """Reset the consecutive-429 counter on a successful fetch."""
    global _consecutive_429_count
    if _consecutive_429_count > 0:
        logger.info(
            "Lamley fetch succeeded — resetting 429 counter (was %d)",
            _consecutive_429_count,
        )
    _consecutive_429_count = 0


def _throttle_wait() -> None:
    """Block until ``_MIN_REQUEST_INTERVAL_S`` has elapsed since the
    previous fetch. Updates the timestamp to "now" on return."""
    global _last_request_time
    with _throttle_lock:
        elapsed = time.monotonic() - _last_request_time
        gap = _MIN_REQUEST_INTERVAL_S - elapsed
        if gap > 0:
            logger.debug("lamley throttle: sleeping %.1fs", gap)
            time.sleep(gap)
        _last_request_time = time.monotonic()


def _parse_retry_after(value: Optional[str]) -> float:
    """Best-effort ``Retry-After`` header parser.

    Server may return either delta-seconds (``"30"``) or HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``). We support delta-seconds and
    fall back to ``_DEFAULT_RETRY_AFTER_S`` for anything else.
    """
    if not value:
        return _DEFAULT_RETRY_AFTER_S
    try:
        secs = float(value.strip())
    except ValueError:
        return _DEFAULT_RETRY_AFTER_S
    # Cap upper bound so a server-side typo cannot lock the bot for hours.
    return max(1.0, min(secs, 120.0))


def _notify(notifier, message: str) -> None:
    logger.error(message)
    if notifier is None:
        return
    try:
        notifier(message)
    except Exception:
        logger.exception("Failed to send admin notification")


#: Allowlist of host suffixes that ``fetch_lamley_article`` will GET.
#: Defends against SSRF when an upstream caller (RSS, news_bot's
#: substring host-dispatch) passes a hostile URL whose hostname merely
#: *contains* "lamleygroup.com" — e.g. ``lamleygroup.com.attacker.example``.
_ALLOWED_HOSTS = ('lamleygroup.com', 'www.lamleygroup.com')


def _is_allowed_lamley_url(link: str) -> bool:
    """Return True iff ``link`` is an https:// URL whose host is in
    ``_ALLOWED_HOSTS``. Anything else is rejected silently."""
    try:
        parsed = urlparse(link)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    return host in _ALLOWED_HOSTS


def fetch_lamley_article(
    link: str,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
    """Scrape a lamleygroup.com article page.

    Returns ``{'title', 'paragraphs', 'images'}`` or ``None`` on failure.

    SSRF guard: ``link``'s hostname must exactly equal ``lamleygroup.com``
    or ``www.lamleygroup.com``. The upstream news_bot dispatcher does a
    substring host check (``'lamleygroup.com' in domain``) which would
    accept ``lamleygroup.com.attacker.example`` — this allowlist is the
    defence-in-depth guard that closes that hole.

    When no ``session`` is injected (the production path), we route
    through ``curl_cffi.requests`` with ``impersonate="chrome131"`` so
    the TLS handshake matches a real Chrome — Cloudflare's bot manager
    fingerprints the JA3 hash before headers are read. Tests inject a
    ``MagicMock`` session and bypass this entirely.
    """
    if not _is_allowed_lamley_url(link):
        logger.warning(
            "Lamley fetch rejected (hostname not in allowlist): %r", link,
        )
        _notify(notifier, admin_alerts.alert_lamley_host_rejected(link))
        return None

    if session is not None:
        http = session
    elif _CFFI_AVAILABLE:
        http = _cffi_requests
    else:
        http = requests

    # WAF-protection short-circuits — log at INFO so the operator sees
    # the bot is intentionally skipping rather than silently dropping.
    if _is_in_cooldown():
        remaining = int(_cooldown_until - time.monotonic())
        logger.info(
            "Lamley cool-down active (%ds left) — skipping %s",
            remaining, link,
        )
        return None
    if _is_url_blacklisted(link):
        remaining = int(_url_blacklist[link] - time.monotonic())
        logger.info(
            "Lamley URL blacklisted (%ds left) — skipping %s",
            remaining, link,
        )
        return None

    def _do_fetch():
        kwargs = {"headers": BROWSER_HEADERS, "timeout": REQUEST_TIMEOUT}
        # ``impersonate`` is only meaningful for curl_cffi. Inject it only
        # when we routed there, so MagicMock test sessions don't see an
        # unexpected kwarg.
        if http is _cffi_requests:
            kwargs["impersonate"] = "chrome131"
        return http.get(link, **kwargs)

    # Soft client-side throttle BEFORE the first attempt so a tight call
    # loop doesn't hammer Lamley's WordPress.
    _throttle_wait()
    try:
        response = _do_fetch()
        # On 429, honour Retry-After (or fall back) and try ONE more time.
        if response.status_code == 429:
            retry_s = _parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "Lamley 429 for %s — sleeping %.1fs before retry",
                link, retry_s,
            )
            time.sleep(retry_s)
            response = _do_fetch()
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_SIZE:
            _notify(notifier, admin_alerts.alert_lamley_article_too_large(
                len(response.content)
            ))
            return None
    except requests.HTTPError as exc:
        # 429 after retry → record both the per-URL blacklist and the
        # consecutive-strike counter (which may trip cool-down).
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code == 429:
            _record_429(link)
        _notify(notifier, admin_alerts.alert_lamley_fetch_error(link, str(exc)))
        return None
    except requests.RequestException as exc:
        # Other transport-level errors (timeout, connection refused,
        # DNS) — don't blacklist the URL or trip the counter; they're
        # not WAF-shaped.
        _notify(notifier, admin_alerts.alert_lamley_fetch_error(link, str(exc)))
        return None

    # Successful response — reset the consecutive-429 counter.
    _record_success()

    soup = BeautifulSoup(response.text, "html.parser")
    # Strip inline scripts/styles so their JS doesn't end up in body text.
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    title_tag = soup.find("h1", class_="entry-title") or soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    body = (
        soup.find("div", class_="entry-content")
        or soup.find("article")
    )
    if body is None:
        _notify(notifier, admin_alerts.alert_lamley_no_body(link))
        return None

    paragraphs: List[str] = []
    for tag in body.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
        text = tag.get_text(" ", strip=True)
        if text and text != title:
            paragraphs.append(text)

    # Strip UI-boilerplate (social-share, "Subscribe", "Read more", etc.)
    # BEFORE picking the subtitle so the editorial lead is real prose.
    paragraphs = filter_boilerplate(paragraphs)

    # Lamley RSS has no subtitle field; use the first body paragraph as the
    # editorial lead on the Telegraph page. Drop it from the body so it
    # doesn't repeat below the decorated lead.
    subtitle = paragraphs[0] if paragraphs else ""
    paragraphs = paragraphs[1:]

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
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
    }
