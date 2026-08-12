#!/usr/bin/env python3
"""Autoevolution news source.

Autoevolution article pages are behind Cloudflare, which blocks stock
``requests``. We use ``curl_cffi`` to impersonate a browser TLS fingerprint
(``_IMPERSONATE_PROFILE``) on a session warmed by a home-page hit, and scrape
the full article body (`div.newstext`) plus article-specific gallery images.
If the scrape fails (network error, layout change, or a future CF update),
the RSS-only ``enrich_entry`` path is used as fallback — better truncated
text than nothing.

The profile and the warm-up are BOTH load-bearing; 2026-08-11 proved it the
expensive way, with the source silently down for three days and the channel
publishing nothing. Read the constants' comments before changing either.
"""

import html
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

try:
    from curl_cffi import requests as curl_requests
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

from bs4 import BeautifulSoup

from boilerplate_filter import filter_blocks, filter_boilerplate, is_boilerplate

logger = logging.getLogger(__name__)

ARTICLE_ID_RE = re.compile(r"-(\d+)\.html")
ARTICLE_SLUG_RE = re.compile(r"/news/([^/?#]+?)-\d+\.html")
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.IGNORECASE)
YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/watch\?v=|youtube\.com/embed/)"
    r"([A-Za-z0-9_-]+)"
)
VIMEO_ID_RE = re.compile(r"vimeo\.com/(\d+)")
REQUEST_TIMEOUT = 20
MAX_IMAGES = 10

#: Browser profile ``curl_cffi`` impersonates. Autoevolution's Cloudflare rules
#: challenge every Chrome fingerprint — measured live 2026-08-11 against a
#: blocked article: 403 on ``chrome`` / ``chrome136`` / ``chrome142`` /
#: ``chrome146`` / ``safari260``, 200 on ``firefox147`` and ``safari184_ios``.
#: This constant is the lever to pull when the block comes back; changing it is
#: cheaper than anything else in this module, so try it first.
_IMPERSONATE_PROFILE = "firefox147"

#: Warm-up target. A reader reaches an article from somewhere on the site, and
#: Cloudflare hands out its clearance on that first hit. Going straight at
#: article URLs with a cold session is challenged roughly half the time even on
#: a good profile (measured: 3 of 6 articles); after a home-page hit, 6 of 6.
_WARMUP_URL = "https://www.autoevolution.com/"

#: Status Cloudflare answers a challenged request with. Such responses also
#: carry ``cf-mitigated: challenge``, but that header is not guaranteed, so the
#: retry keys on the status alone.
_CHALLENGE_STATUS = 403

#: Shared by every article of one tick — see ``_get_session``. Module-level
#: because ``news_bot.fetch_full_article`` dispatches per article and has no
#: place to hold a per-tick object.
_session = None

#: SSRF guard (CWE-918). The upstream news_bot dispatcher routes here on a
#: host match, and this fetcher — unlike lamley/t_hunted/orangetrack/mattel —
#: had NO downstream host check, so a hostile ``entry['link']`` (feed
#: compromise, malicious <link>, or an open-redirect) of the form
#: ``http://autoevolution.com@169.254.169.254/…`` (userinfo attack) or
#: ``http://autoevolution.com.attacker.example/…`` (suffix attack) would cause
#: a server-side GET to an arbitrary internal/metadata host. Exact-hostname
#: allowlist — ``urlparse(...).hostname`` yields the post-@ host, closing both.
_ALLOWED_HOSTS = ('autoevolution.com', 'www.autoevolution.com')


def _is_allowed_autoevolution_url(link: str) -> bool:
    """Return True iff ``link`` is an http(s) URL whose host is exactly in
    ``_ALLOWED_HOSTS``. Anything else is rejected (no fetch)."""
    try:
        parsed = urlparse(link)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    return (parsed.hostname or '').lower() in _ALLOWED_HOSTS


def _runs_from_tag(tag) -> List[Dict]:
    """Walk a tag's contents and return an ordered list of text+link runs.

    Each run is ``{'text': str}`` or ``{'text': str, 'href': str}``. Nested
    formatting (``<strong>``, ``<em>``) is flattened to plain text, but
    ``<a href="…">`` anchors are preserved so external links survive
    translation and reach Telegraph as proper ``<a>`` nodes.
    """
    runs: List[Dict] = []
    buf: List[str] = []

    def flush():
        if not buf:
            return
        combined = "".join(buf)
        if combined:
            runs.append({"text": combined})
        buf.clear()

    def walk(element):
        for child in element.children:
            if isinstance(child, str):
                buf.append(str(child))
            elif getattr(child, "name", None) == "a" and child.get("href"):
                flush()
                link_text = child.get_text(" ", strip=False)
                if link_text:
                    runs.append({"text": link_text, "href": child["href"]})
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


def _video_embed_url(href: str) -> Optional[str]:
    """Translate a YouTube/Vimeo link into a Telegra.ph-compatible iframe src.

    Telegra.ph validates iframe ``src`` at create-page time and accepts only
    URLs under its own ``/embed/<provider>?url=…`` proxy. Raw YouTube URLs
    (watch or embed form) are silently stripped to an empty ``/embed/``,
    which makes the page fail Instant View. We wrap the source URL into the
    proxy form Telegra.ph actually serves.
    """
    import urllib.parse
    m = YOUTUBE_ID_RE.search(href)
    if m:
        watch = f"https://www.youtube.com/watch?v={m.group(1)}"
        return f"https://telegra.ph/embed/youtube?url={urllib.parse.quote(watch, safe='')}"
    m = VIMEO_ID_RE.search(href)
    if m:
        page = f"https://vimeo.com/{m.group(1)}"
        return f"https://telegra.ph/embed/vimeo?url={urllib.parse.quote(page, safe='')}"
    return None

_CONTINUE_READING_RE = re.compile(
    r"\s*\(<a[^>]*>\s*continue reading[^<]*</a>\)\s*$",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def _clean_summary(summary: str) -> str:
    text = _CONTINUE_READING_RE.sub("", summary)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return text.strip()


def _get_session(refresh: bool = False):
    """Return the shared warmed ``curl_cffi`` session, building it if needed.

    ``refresh=True`` throws the current one away first — used by the retry in
    ``_fetch_through_session`` when Cloudflare challenges a request, which
    means the clearance cookie this session carries is no longer good.
    """
    global _session
    if _session is None or refresh:
        if _session is not None:
            # The process runs for weeks and this is now the second place a
            # session gets discarded; dropping the reference alone would leak
            # the socket until GC felt like it.
            try:
                _session.close()
            except Exception:
                pass
        _session = curl_requests.Session(impersonate=_IMPERSONATE_PROFILE)
        try:
            _session.get(_WARMUP_URL, timeout=REQUEST_TIMEOUT,
                         allow_redirects=False)
        except Exception as exc:
            # Warm-up is an optimisation, not a precondition. An unreachable
            # home page must not cost the tick its articles — the session is
            # still usable, it just starts without clearance.
            logger.warning("Autoevolution warm-up failed: %s", exc)
    return _session


def reset_session() -> None:
    """Drop the shared session so the next fetch builds and warms a new one."""
    global _session
    if _session is not None:
        try:
            _session.close()
        except Exception:
            pass
    _session = None


def _fetch_through_session(link: str):
    """GET ``link`` on the shared session, re-warming once if challenged.

    Returns the 200 response, or ``None`` so the caller falls back to RSS.

    The retry is not belt-and-braces: the process outlives the daily tick, so
    by the next one the clearance cookie is ~24 h old and the first article of
    that tick is the one that gets challenged. Re-warming costs a single extra
    request and puts the rest of the tick back on the fast path. It is capped
    at one retry — a source that challenges a freshly warmed session is
    blocking us for a reason no amount of retrying will fix.
    """
    if not _CURL_CFFI_AVAILABLE:
        return None
    for attempt in (1, 2):
        session = _get_session(refresh=(attempt == 2))
        try:
            # `allow_redirects=False` is mandatory, not tuning: the allowlist
            # above validates ONE url, so following a 30x would let a hostile
            # link reach an arbitrary host through a guard that already passed
            # (CWE-918). orangetrack and mattel already set it; this fetcher
            # was the last one that did not. Verified 2026-08-12 that neither
            # the home page nor article pages redirect, so nothing is lost.
            response = session.get(link, timeout=REQUEST_TIMEOUT,
                                   allow_redirects=False)
        except Exception as exc:
            # Drop the session before giving up. Sharing one connection across
            # a tick means a transport-level failure (reset peer, dead socket)
            # would otherwise be inherited by every remaining article — the old
            # per-request code could not have this problem, so the sharing must
            # not introduce it. The next article rebuilds and re-warms.
            logger.warning("Autoevolution scrape failed for %r: %s", link, exc)
            reset_session()
            return None
        if response.status_code == 200:
            return response
        if attempt == 1 and response.status_code == _CHALLENGE_STATUS:
            logger.info(
                "Autoevolution challenged %r — re-warming the session and retrying",
                link,
            )
            continue
        logger.warning(
            "Autoevolution scrape got HTTP %s for %r", response.status_code, link,
        )
        return None
    return None


def _fetch_through(fetcher, link: str):
    """Run an injected ``fetcher`` and apply the same success contract.

    Kept separate from the session path so tests exercise parsing without
    curl_cffi, sessions or network — the contract they rely on is unchanged:
    any exception or non-200 yields ``None``.
    """
    try:
        response = fetcher(link)
    except Exception as exc:
        logger.warning("Autoevolution scrape failed for %r: %s", link, exc)
        return None
    if response.status_code != 200:
        logger.warning(
            "Autoevolution scrape got HTTP %s for %r", response.status_code, link,
        )
        return None
    return response


def _scrape_article_page(link: str, fetcher=None) -> Optional[Dict]:
    """Fetch the article page via curl_cffi (Cloudflare bypass) and parse it.

    Returns ``None`` on any failure so the caller can fall back to RSS.
    ``fetcher`` is injectable for tests; otherwise curl_cffi is used.
    """
    # SSRF guard — reject before any fetch (real or injected). See _ALLOWED_HOSTS.
    if not _is_allowed_autoevolution_url(link):
        logger.warning(
            "Autoevolution fetch rejected (hostname not in allowlist): %r", link,
        )
        return None

    if fetcher is None:
        response = _fetch_through_session(link)
    else:
        response = _fetch_through(fetcher, link)
    if response is None:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    # Strip script/style/noscript so their content doesn't leak into
    # paragraph text (autoevolution embeds `googletag.display(...)` calls
    # inside article body <script> tags).
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    subtitle = ""
    for cand in soup.find_all("div"):
        cls = set(cand.get("class") or [])
        if {"mgtop_10", "mgbot_10", "fsz19"}.issubset(cls):
            subtitle = cand.get_text(" ", strip=True)
            break

    body = soup.find("div", class_="newstext")
    if body is None:
        logger.warning("Autoevolution article has no .newstext body: %s", link)
        return None

    # Article slug (everything in the URL path after /news/ but before the
    # numeric ID). Used to filter inline gallery images to this article only.
    slug_m = ARTICLE_SLUG_RE.search(link)
    slug = slug_m.group(1) if slug_m else ""

    def _image_from_ch_pic(ch_pic) -> Optional[Dict]:
        """Extract {src, caption} from a `div.ch_pic` container — or None."""
        img = ch_pic.find("img")
        anchor = ch_pic.find("a", class_="fullimg")
        src = ""
        if anchor and (anchor.get("href") or "").startswith("http"):
            href = anchor["href"]
            if IMAGE_EXT_RE.search(href):
                src = href
        if not src and img:
            candidate = img.get("src") or img.get("data-src") or ""
            if candidate.startswith("http"):
                src = candidate
        if not src:
            return None
        cap_div = ch_pic.find("div", class_="ch_pic_crd")
        caption = cap_div.get_text(" ", strip=True) if cap_div else ""
        return {"type": "image", "src": src, "caption": caption}

    # Hero image — sits outside .newstext in `div.ch_pic.mainpic`.
    hero_block = None
    hero_container = soup.find("div", class_="mainpic")
    if hero_container:
        hero_block = _image_from_ch_pic(hero_container)

    blocks: List[Dict] = []
    seen_media = set()
    # Reserve the hero URL up front so the body/gallery walks don't add a
    # duplicate copy (gallery's item 1 is frequently the same file as hero).
    if hero_block:
        seen_media.add(hero_block["src"].split("?", 1)[0])

    # Walk direct children of .newstext in DOM order so images/videos land at
    # their original positions between paragraphs.
    for child in body.children:
        if not getattr(child, "name", None):
            continue
        cls = " ".join(child.get("class") or [])
        if ("ad" in cls and ("intext" in cls or "ad300" in cls)) or "clearfix" in cls:
            continue

        # Bold lead intro — autoevolution uses `div.sanscond.fsz22.bold`
        if child.name == "div" and ("fsz22" in cls or "sanscond" in cls):
            runs = _runs_from_tag(child)
            if runs:
                text = " ".join(r["text"] for r in runs).strip()
                blocks.append({"type": "lead", "text": text, "runs": runs})
            continue

        # Inline gallery images — `<div class="ch_pic">` often nested inside
        # `<p>` tags; each one carries its own `<div class="ch_pic_crd">`
        # caption (e.g. "Photo: Lamley Group"). We extract them (and detach
        # from the DOM) so the paragraph's own text reader below doesn't
        # re-emit the caption as a separate paragraph block.
        for ch_pic in child.find_all("div", class_="ch_pic"):
            block = _image_from_ch_pic(ch_pic)
            ch_pic.extract()
            if not block:
                continue
            base = block["src"].split("?", 1)[0]
            if base in seen_media:
                continue
            seen_media.add(base)
            blocks.append(block)

        # Videos: any `<a>` with a YouTube/Vimeo href (usually wrapped around a
        # preview thumbnail). Detach the anchor too so its link text doesn't
        # leak into the paragraph below.
        for anchor in list(child.find_all("a", href=True)):
            embed = _video_embed_url(anchor["href"])
            if not embed:
                continue
            anchor.extract()
            if embed not in seen_media:
                seen_media.add(embed)
                blocks.append({"type": "video", "src": embed})

        # Paragraph / heading text — extract runs so inline <a href> external
        # links survive translation and land on Telegraph as real <a> nodes.
        if child.name == "p":
            # Autoevolution often emits *invalid* HTML where section titles
            # are wrapped in <h2> (sometimes <h3>/<h4>) NESTED inside <p>
            # (e.g. `<p><h2 class="bold dispblock">BMW M1 Procar</h2>
            # …image…ad…body text…</p>`). Without this detach pass the
            # heading text would leak into the paragraph block via
            # `_runs_from_tag`, producing `"BMW M1 Procar Here's a tough
            # question…"` as a single paragraph and losing the structural
            # heading entirely. Emit each nested heading as its own block
            # in DOM order, then strip them from the tree before reading
            # the paragraph's residual text. Verified live 2026-05-13:
            # autoevolution article 269773 produced 9 inline-h2 sections
            # ("BMW M1 Procar", "Porsche 914 Safari", etc.) which all
            # appeared as paragraph-prefix text on the Telegraph page.
            for nested_h in list(child.find_all(["h2", "h3", "h4"])):
                h_runs = _runs_from_tag(nested_h)
                if h_runs:
                    h_text = " ".join(r["text"] for r in h_runs).strip()
                    blocks.append({
                        "type": "heading",
                        "text": h_text,
                        "level": int(nested_h.name[1]),
                        "runs": h_runs,
                    })
                nested_h.extract()
            runs = _runs_from_tag(child)
            if runs:
                text = " ".join(r["text"] for r in runs).strip()
                blocks.append({"type": "paragraph", "text": text, "runs": runs})
        elif child.name in ("h2", "h3", "h4"):
            runs = _runs_from_tag(child)
            if runs:
                text = " ".join(r["text"] for r in runs).strip()
                blocks.append({
                    "type": "heading",
                    "text": text,
                    "level": int(child.name[1]),
                    "runs": runs,
                })

    # Gallery at the end of the page (`<div class="newsgal2">`) holds the
    # full photo set — thumbnails link to `/images/news/gallery/<slug>_N.jpg`
    # and the caption lives in the thumbnail's `data-description` attribute.
    # Appended after the body so readers see the text first, then the
    # image set. `seen_media` dedupes against hero/inline images.
    gallery = soup.find("div", class_="newsgal2")
    if gallery and slug:
        gallery_href_re = re.compile(r"/gallery/[^\"']*" + re.escape(slug))
        for anchor in gallery.find_all("a", href=True):
            href = anchor["href"]
            if not gallery_href_re.search(href):
                continue
            base = href.split("?", 1)[0]
            if base in seen_media:
                continue
            seen_media.add(base)
            img = anchor.find("img")
            caption = (img.get("data-description") or "").strip() if img else ""
            blocks.append({"type": "image", "src": href, "caption": caption})

    # Prepend the hero image so it becomes the Telegraph preview thumbnail.
    if hero_block:
        blocks.insert(0, hero_block)

    # Strip UI-boilerplate (social-share, "Subscribe", "Read more", ads
    # with placeholder images, etc.) from blocks. ``filter_blocks`` is
    # smarter than the previous inline check: it also drops media blocks
    # whose caption is pure boilerplate AND has no other text (rare, but
    # protects the ``article['blocks']`` fallback used by
    # ``transcreate_via_claude`` when the LLM returns null/short blocks
    # — we don't want to leak ad slots into the Telegraph page).
    blocks = filter_blocks(blocks)

    if not blocks:
        return None

    # Back-compat flat lists (some callers still expect paragraphs/images).
    paragraphs = [
        b["text"] for b in blocks
        if b["type"] in ("lead", "paragraph", "heading")
    ]
    images = [b["src"] for b in blocks if b["type"] == "image"][:MAX_IMAGES]

    return {
        "title": title,
        "subtitle": subtitle,
        "paragraphs": paragraphs,
        "images": images,
        "blocks": blocks,
    }


def fetch_autoevolution_article(entry: dict, fetcher=None) -> Optional[Dict]:
    """Get the best available article payload for an autoevolution entry.

    Tries Cloudflare-bypass scraping first; falls back to RSS-only enrichment
    so the pipeline still posts something even if scraping breaks.
    """
    link = entry.get("link") or ""
    if link:
        scraped = _scrape_article_page(link, fetcher=fetcher)
        if scraped:
            # Prefer RSS media for images if the page didn't yield any
            if not scraped["images"]:
                scraped["images"] = _collect_rss_images(entry)
            return scraped
        # Scrape failed (403, parser-no-body, network). RSS-only fallback
        # is acceptable for prose but only if RSS carries a hero image —
        # otherwise the Telegraph page lands without <figure>, the Telegram
        # teaser has no og:image, and subscribers see a text-only preview
        # (incident 2026-05-31 with autoevolution Boulevard Mix). Returning
        # None here defers staging — news_bot.py skips the entry without
        # marking it processed, so the next cron tick retries from scratch
        # (autoevolution 403's are typically single-tick transients).
        if not _collect_rss_images(entry):
            logger.info(
                "autoevolution: deferring %s — scrape failed and no RSS "
                "hero image; will retry next tick", link,
            )
            return None
    return enrich_entry(entry)


def _collect_rss_images(entry: dict) -> List[str]:
    images: List[str] = []
    for field in ("media_content", "media_thumbnail"):
        for m in entry.get(field) or []:
            url = m.get("url") if isinstance(m, dict) else None
            if url and url not in images:
                images.append(url)
    return images


def enrich_entry(entry: dict) -> Optional[Dict]:
    """Build {title, paragraphs, images} from an autoevolution RSS entry."""
    title = entry.get("title") or ""
    summary = entry.get("summary") or entry.get("description") or ""
    if not title and not summary:
        return None

    body = _clean_summary(summary)
    paragraphs: List[str] = [p.strip() for p in _PARAGRAPH_SPLIT_RE.split(body) if p.strip()]
    if not paragraphs and body:
        paragraphs = [body]
    if not paragraphs:
        paragraphs = [title]
    # Strip UI-boilerplate before returning. RSS-only fallback rarely contains
    # share widgets, but keeping the filter consistent across all parser exits.
    paragraphs = filter_boilerplate(paragraphs)
    if not paragraphs:
        paragraphs = [title]

    # RSS doesn't carry a dedicated subtitle; leave it empty so the Telegraph
    # page skips the decorated lead paragraph + hr for RSS-only articles.
    return {
        "title": title,
        "subtitle": "",
        "paragraphs": paragraphs,
        "images": _collect_rss_images(entry),
    }
