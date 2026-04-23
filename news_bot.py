#!/usr/bin/env python3
"""
Automated news collector and Telegram poster.
Fetches RSS feed, parses articles, translates, summarizes, and posts to Telegram.
"""

import sqlite3
import logging
import feedparser
import requests
from deep_translator import GoogleTranslator
import schedule
import asyncio
import time
from collections import Counter
from datetime import datetime
from telegram import Bot, LinkPreviewOptions
from telegram.error import TelegramError
import os
import json
from urllib.parse import urlparse

from mattel_news_source import fetch_mattel_news, fetch_mattel_article
import autoevolution_source
import lamley_source
import telegraph_publisher
from telegraph_publisher import TelegraphError

# Late-binding DAO for the manual-review-workflow queue tables
# (``pending_articles``, ``published_articles``, ``failed_articles``).
# Imported under a short alias so the prep-phase ``job()`` body reads
# cleanly and matches the vocabulary used in tech-spec §Architecture.
# ``pending_articles_repo`` itself imports ``news_bot`` at module level
# for ``DB_FILE`` access — the cycle resolves because this import runs
# after all our module-level names have been bound.
import pending_articles_repo as pending_repo

# Configuration - set via environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TELEGRAM_ADMIN_ID = os.getenv('TELEGRAM_ADMIN_ID', '@sunny413x')
TRANSLATOR_SERVICE = 'google'  # or 'libre'
RSS_URL = "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml"
DB_FILE = "news.db"
LOG_LEVEL = logging.INFO

# Manual-review-workflow knobs (Decision 5). Env-overridable so operators
# can tune timing without a code change. Parsed at import-time — tests that
# need different values reload the module or patch the constant directly.
IDLE_TIMEOUT_HOURS = int(os.getenv('IDLE_TIMEOUT_HOURS', '48'))
GRACE_WINDOW_HOURS = int(os.getenv('GRACE_WINDOW_HOURS', '2'))
QUEUE_CAP = int(os.getenv('QUEUE_CAP', '10'))

# Env-var names whose values must never leak into stored error strings or
# admin-chat messages (Decision 11). Kept as a module-level tuple so new
# secrets can be added without touching the function body.
_SECRET_ENV_NAMES = (
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_CHANNEL_ID',
    'TELEGRAM_ADMIN_ID',
    'TELEGRAPH_ACCESS_TOKEN',
)


def sanitize_error_message(exc):
    """Return str(exc) with every known env-secret value replaced by
    ``[REDACTED]``. Decision 11 of manual-review-workflow tech-spec:
    protects ``pending_articles.last_error`` / ``failed_articles.last_error``
    and admin-chat messages from accidentally leaking ``TELEGRAM_BOT_TOKEN``
    / ``TELEGRAM_CHANNEL_ID`` / ``TELEGRAM_ADMIN_ID`` /
    ``TELEGRAPH_ACCESS_TOKEN`` via ``requests`` / ``_api_call`` network-error
    strings.

    Empty / None / whitespace-only env values are skipped: ``str.replace('',
    '[REDACTED]')`` would interleave the marker between every character.
    Any internal error is swallowed — the sanitiser must never break the
    caller's error-reporting path.
    """
    try:
        message = str(exc)
    except Exception:
        # Exception's __str__ itself raised — fall back to a safe
        # placeholder so the caller's error path keeps working.
        return '<unrepresentable exception>'

    for name in _SECRET_ENV_NAMES:
        value = os.getenv(name)
        if not value or not value.strip():
            continue
        try:
            message = message.replace(value, '[REDACTED]')
        except Exception:
            # Defensive: never let sanitisation break the caller.
            continue
    return message


def send_admin_notification(message):
    """Send a notification message to the admin."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        logging.error("Telegram credentials or admin ID not set.")
        return False
    async def _send():
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        try:
            await bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=message, parse_mode='Markdown')
            logging.info(f"Admin notification sent: {message[:50]}...")
            return True
        except TelegramError as e:
            logging.error(f"Failed to send admin notification: {e}")
            return False
    return asyncio.run(_send())


def load_feeds():
    """Load RSS feed URLs from feeds.json. If missing or invalid, send admin notification and return empty list."""
    try:
        with open('feeds.json', 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.warning(f"feeds.json missing or invalid: {e}. Falling back to default RSS URL.")
        try:
            send_admin_notification(f"⚠️ feeds.json missing or invalid: {e}. Bot has no RSS feed to process.")
        except Exception as notify_err:
            logging.error(f"Failed to send admin notification: {notify_err}")
        return [RSS_URL]

    if not isinstance(data, list):
        logging.warning("feeds.json does not contain a list. Falling back to default RSS URL.")
        try:
            send_admin_notification("⚠️ feeds.json does not contain a list. Bot has no RSS feed to process.")
        except Exception as notify_err:
            logging.error(f"Failed to send admin notification: {notify_err}")
        return [RSS_URL]

    valid_urls = []
    for item in data[:5]:  # limit to first 5
        if not isinstance(item, str):
            logging.warning("feeds.json contains non‑string item. Falling back to default RSS URL.")
            try:
                send_admin_notification("⚠️ feeds.json contains non‑string item. Bot has no RSS feed to process.")
            except Exception as notify_err:
                logging.error(f"Failed to send admin notification: {notify_err}")
            return [RSS_URL]
        parsed = urlparse(item)
        if not (parsed.scheme and parsed.netloc) or parsed.scheme not in ('http', 'https'):
            logging.warning(f"Invalid URL in feeds.json: {item}. Falling back to default RSS URL.")
            try:
                send_admin_notification(f"⚠️ Invalid URL in feeds.json: {item}. Bot has no RSS feed to process.")
            except Exception as notify_err:
                logging.error(f"Failed to send admin notification: {notify_err}")
            return [RSS_URL]
        valid_urls.append(item)

    if not valid_urls:
        logging.warning("feeds.json contains no valid URLs. Falling back to default RSS URL.")
        try:
            send_admin_notification("⚠️ feeds.json contains no valid URLs. Bot has no RSS feed to process.")
        except Exception as notify_err:
            logging.error(f"Failed to send admin notification: {notify_err}")
        return [RSS_URL]
    return valid_urls


# Setup logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database functions
def init_db():
    """Create all four SQLite tables used by the bot if missing.

    Owns the DDL for ``processed_news`` directly and delegates the three
    manual-review-workflow tables (``pending_articles`` /
    ``published_articles`` / ``failed_articles``) to
    ``pending_articles_repo.init_schema``. One ``conn.commit`` covers both
    steps — DDL is idempotent (``CREATE TABLE IF NOT EXISTS``), so
    repeated calls on a populated DB are a no-op.
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS processed_news
                 (link TEXT PRIMARY KEY, title TEXT, pub_date TEXT, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # Delegate the three new tables to the repo — single source of truth
    # for their DDL (Task 1). ``init_schema`` is idempotent and commits
    # internally, but we commit once more below so the caller sees a single
    # transactional boundary at ``init_db`` granularity.
    pending_repo.init_schema(conn)
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def is_processed(link):
    """Check if a news item has already been processed."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_news WHERE link = ?", (link,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_processed(link, title, pub_date):
    """Mark a news item as processed."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO processed_news (link, title, pub_date) VALUES (?, ?, ?)",
              (link, title, pub_date))
    conn.commit()
    conn.close()
    logger.debug(f"Marked as processed: {link}")

# RSS functions
def fetch_rss(url):
    """Fetch and parse RSS feed, return list of entries."""
    try:
        feed = feedparser.parse(url)
        if feed.bozo:
            logger.warning(f"RSS feed parse warning for {url}: {feed.bozo_exception}")
        return feed.entries
    except Exception as e:
        logger.error(f"Failed to fetch RSS from {url}: {e}")
        return []

def filter_new_entries(entries):
    """Filter entries that are not already processed."""
    new_entries = []
    seen = set()
    for entry in entries:
        link = entry.get('link')
        if link and not is_processed(link) and link not in seen:
            new_entries.append(entry)
            seen.add(link)
    logger.info(f"Found {len(new_entries)} new entries.")
    return new_entries

# Translation
def translate_text(text, source='auto', target='ru'):
    """Translate text using Google Translate."""
    try:
        translator = GoogleTranslator(source=source, target=target)
        translated = translator.translate(text)
        return translated
    except Exception as e:
        logger.error(f"Translation failed: {e}")
        return text  # fallback to original

def transcreate_text(text, source='auto', target='ru', is_title=False):
    """
    Translate and adapt text for a lively Russian Telegram channel.

    Google Translate + post-processing:
    - replaces bureaucratic phrasing with plain Russian
    - fixes common Hot Wheels mistranslations (brand names, jargon)
    - flips a few passive constructions to active
    - prepends a single content-aware emoji to titles (deterministic)
    """
    import re

    try:
        translated = GoogleTranslator(source=source, target=target).translate(text)
    except Exception as e:
        logger.error(f"Translation failed in transcreation: {e}")
        translated = text

    if not translated or not translated.strip():
        return text

    result = translated

    # Bureaucratic → plain Russian
    bureaucratic = {
        r'является': 'это',
        r'осуществляется': 'происходит',
        r'представляет собой': 'это',
        r'в рамках': 'в',
        r'в процессе': 'во время',
        r'в ходе': 'во время',
        r'на сегодняшний день': 'сейчас',
        r'в настоящее время': 'сейчас',
        r'на данный момент': 'сейчас',
        r'как правило': 'обычно',
        r'в связи с тем,?\s+что': 'так как',
        r'в целях': 'чтобы',
        r'с целью': 'чтобы',
        r'в случае,?\s+если': 'если',
        r'по итогам': 'после',
        r'имеет возможность': 'может',
        r'получат возможность': 'смогут',
        r'тем не менее': 'но',
        r'при этом': 'и',
    }
    for pattern, repl in bureaucratic.items():
        result = re.sub(pattern, repl, result, flags=re.IGNORECASE)

    # Passive → active
    result = re.sub(r'был выполн[еён]', 'сделали', result, flags=re.IGNORECASE)
    result = re.sub(r'был представлен', 'представили', result, flags=re.IGNORECASE)
    result = re.sub(r'было объявлено', 'объявили', result, flags=re.IGNORECASE)
    result = re.sub(r'был запущен', 'запустили', result, flags=re.IGNORECASE)

    # Hot Wheels domain glossary — fix recurring Google Translate mistakes.
    # Keeps brand names in English (fandom convention) and fixes terms Google
    # mangles ("garage build" → "гаражный проект", not "сборка гаража").
    hw_glossary = {
        r'\bХот[-\s]?[УВв]илс\b': 'Hot Wheels',
        r'\bхот[-\s]?колёс\b': 'Hot Wheels',
        r'\bсборка гаража\b': 'гаражный проект',
        r'\bсборки гаража\b': 'гаражного проекта',
        r'\bсборке гаража\b': 'гаражному проекту',
        r'\bсборкой гаража\b': 'гаражным проектом',
        r'\bлитой автомобиль\b': 'дайкаст-модель',
        r'\bлитого автомобиля\b': 'дайкаст-модели',
        r'\bлитому автомобилю\b': 'дайкаст-модели',
        r'\bлитым автомобилем\b': 'дайкаст-моделью',
        r'\b[Тт]ур легенд\b': 'Legends Tour',
        r'\bлегендарный тур\b': 'Legends Tour',
        r'\bтур\s+(Hot Wheels[™®]?\s+Legends Tour)': r'\1',
        r'\bтеперь принимает заявки\b': 'открывает приём заявок',
        r'\bтеперь принимает заявления\b': 'открывает приём заявок',
    }
    for pattern, repl in hw_glossary.items():
        result = re.sub(pattern, repl, result, flags=re.IGNORECASE)

    # Titles get a single emoji prefix chosen by content (deterministic).
    if is_title:
        t = result.lower()
        if re.search(r'легенд|legends|tour|чемпион|приз|победител', t):
            emoji = '🏆'
        elif re.search(r'гонк|скорост|race|ралли', t):
            emoji = '🏎️'
        elif re.search(r'релиз|выпуск|launch|запуск|вышел|выходит|дебют', t):
            emoji = '🚀'
        elif re.search(r'коллекц|серия|series|collection', t):
            emoji = '💎'
        elif re.search(r'сотруднич|партнёр|collab|partner', t):
            emoji = '🤝'
        elif re.search(r'анонс|объявл|представля|announce', t):
            emoji = '📢'
        elif re.search(r'машин|автомобил|модел|\bcar\b', t):
            emoji = '🚗'
        else:
            emoji = '🔥'
        return f"{emoji} {result}"

    # Body: truncate to 4000 chars on a sentence boundary.
    if len(result) > 4000:
        window = result[:4000]
        match = re.search(r'[.!?][\s\n]', window[::-1])
        if match:
            cut_pos = 4000 - match.start() - 1
            result = result[:cut_pos]
        else:
            last_space = window.rfind(' ')
            if last_space != -1:
                result = result[:last_space]
            else:
                result = result[:4000]

    return result

def _source_hashtag(source_url):
    """Return a Telegram hashtag for the source: `#{brand}` from the URL's
    netloc, stripping `www.` and the TLD. Example: `corporate.mattel.com`
    → `#mattel`, `autoevolution.com` → `#autoevolution`."""
    netloc = urlparse(source_url).netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    parts = netloc.split('.')
    label = parts[-2] if len(parts) >= 2 else netloc
    return f"#{label}"


# Explicit netloc → internal source_name map (Decision 4 of
# manual-review-workflow tech-spec). Used by the SOURCES registry to stamp
# every fetched entry with a brand label that drives admin-ping counting
# (`SOURCE_LABEL` / `SOURCE_EMOJI`) and `pending_articles.source_name`.
#
# Why an explicit map instead of bare-netloc inference: `urlparse(...).netloc`
# of `https://lamleygroup.com/...` is `'lamleygroup.com'`, which would label
# Lamley entries as `'lamleygroup'` — mismatching the `'lamley'` vocabulary
# the admin-ping dicts expect. Kept intentionally separate from
# `_source_hashtag` (Decision 14): the channel-post hashtag keeps the
# TLD-stripped form (`#lamleygroup`, not `#lamley`) for continuity with the
# existing channel format.
NETLOC_TO_SOURCE = {
    'www.autoevolution.com': 'autoevolution',
    'autoevolution.com':     'autoevolution',
    'lamleygroup.com':       'lamley',
    'www.lamleygroup.com':   'lamley',
    'corporate.mattel.com':  'mattel',
}


def _resolve_source_name(link):
    """Map a URL to its internal `source_name` via `NETLOC_TO_SOURCE`.

    Reads the URL's netloc, lowercases it (so `WWW.Autoevolution.COM` and
    `www.autoevolution.com` collapse to the same key), and looks it up in
    `NETLOC_TO_SOURCE`. Returns `'other'` on miss — never raises, even for
    empty or malformed URLs (`urlparse('')` yields an empty netloc, which
    misses the map and falls through to `'other'`). The caller
    (`_fetch_rss_entries`) is responsible for logging a WARNING on `'other'`.
    """
    try:
        netloc = urlparse(link or '').netloc.lower()
    except Exception:
        # urlparse is very forgiving — this branch is defensive only.
        return 'other'
    return NETLOC_TO_SOURCE.get(netloc, 'other')


# Source vocabulary for the manual-review-workflow admin-ping (Decision 4).
# Keys match `pending_articles.source_name` exactly — no 'rss' key, because
# lamley also arrives via RSS and the two outlets must remain distinguishable
# in the ping. `'other'` is a netloc-fallback (Task 5) and intentionally has
# no emoji/label: those entries simply don't appear in the ping.
SOURCE_EMOJI = {
    'autoevolution': '\U0001F7E0',  # orange circle
    'mattel':        '\U0001F7E3',  # purple circle
    'lamley':        '\U0001F7E2',  # green circle
}
SOURCE_LABEL = {
    'autoevolution': 'autoevolution',
    'mattel':        'mattel',
    'lamley':        'lamley',
}

# Canonical iteration order for the admin-ping fragments. Literal, not
# derived from dict insertion order or sort — pinned so future refactors
# don't silently change the operator-visible format.
_ADMIN_PING_ORDER = ('autoevolution', 'mattel', 'lamley')


def build_admin_ping(rows):
    """Compose the consolidated admin-ping line for the pending-review
    queue. Decision 12 (single ping per tick) + user-spec L25/L57.

    Format (byte-for-byte): ``"N ждут review: 🟠 autoevolution ×K, 🟣 mattel
    ×M, 🟢 lamley ×L"``. Sources with a zero count are omitted; ``N`` is the
    total row count including entries whose ``source_name`` is outside the
    known vocabulary (e.g. ``'other'``).

    Returns ``None`` on an empty ``rows`` list — user-spec AC L57 forbids
    pinging about an empty queue.
    """
    if not rows:
        return None

    counts = Counter(r['source_name'] for r in rows)
    parts = []
    for key in _ADMIN_PING_ORDER:
        count = counts.get(key, 0)
        if count == 0:
            continue
        parts.append(f"{SOURCE_EMOJI[key]} {SOURCE_LABEL[key]} ×{count}")

    return f"{len(rows)} ждут review: " + ", ".join(parts)


def send_telegraph_teaser(telegraph_url, source_url):
    """Publish the locked-format channel post: a single source hashtag +
    Telegraph preview card above (via LinkPreviewOptions). See
    work/telegraph-pipeline/post-format.md for the spec. The preview card
    carries all visible content (domain, title, excerpt, image, ⚡ INSTANT
    VIEW button) — the message body is just the source attribution."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.error("Telegram credentials not set.")
        return False

    text = _source_hashtag(source_url)

    async def _send():
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=text,
                parse_mode='Markdown',
                link_preview_options=LinkPreviewOptions(
                    url=telegraph_url,
                    show_above_text=True,
                ),
            )
            logger.info(f"Posted to Telegram: {telegraph_url}")
            return True
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
            return False

    return asyncio.run(_send())

# Processing pipeline
def fetch_full_article(entry):
    """Dispatch to the source-specific fetcher based on the article domain.

    Returns ``{'title', 'paragraphs', 'images'}`` or ``None`` if no handler
    matches or the fetch fails. Supported sources: corporate.mattel.com
    (parses __NEXT_DATA__), lamleygroup.com (HTML scrape),
    autoevolution.com (RSS-only — Cloudflare blocks scraping).
    """
    link = entry.get('link') or ''
    domain = urlparse(link).netloc.lower()
    try:
        if 'corporate.mattel.com' in domain:
            return fetch_mattel_article(link, notifier=send_admin_notification)
        if 'lamleygroup.com' in domain:
            return lamley_source.fetch_lamley_article(link, notifier=send_admin_notification)
        if 'autoevolution.com' in domain:
            return autoevolution_source.fetch_autoevolution_article(entry)
    except Exception as exc:
        logger.exception(f"Source fetcher failed for {link}: {exc}")
        return None
    logger.warning(f"No source handler for domain: {domain}")
    return None


# ---------------------------------------------------------------------------
# SOURCES registry (Decision 4 of manual-review-workflow tech-spec).
# Task 5 lays the groundwork; Task 6 will refactor `job()` to iterate this.
# Each fetcher accepts a ``notifier`` callable for uniform error-surfacing
# (passed through to per-source fetchers that support admin notifications).
# Every returned entry must carry a ``source_name`` string — one of
# ``'autoevolution'`` / ``'lamley'`` / ``'mattel'`` / ``'other'`` — so the
# prep phase can count them per `SOURCE_LABEL` / `SOURCE_EMOJI`.
# ---------------------------------------------------------------------------


def _fetch_rss_entries(notifier=None):
    """Fetch all RSS feeds and return a `list[dict]` with `source_name` set.

    Iterates `load_feeds()` (falls back to `[RSS_URL]` when the JSON config
    yields an empty list), calls `fetch_rss(url)` per feed with per-URL
    try/except so one broken feed doesn't abort the rest, and normalises each
    `feedparser.FeedParserDict` entry into a plain `dict` with only the
    fields we actually consume (`link`, `title`, `published`, `summary`).
    Every item is stamped with `feed_url` (the originating feed URL) and
    `source_name` (via `_resolve_source_name`, a WARNING log is emitted on
    `'other'`).

    `notifier` is accepted for API parity with `_fetch_mattel_entries`; RSS
    errors currently go to the local logger only.
    """
    entries = []
    feed_urls = load_feeds() or [RSS_URL]
    for url in feed_urls:
        try:
            raw = fetch_rss(url)
        except Exception as exc:
            logger.error(f"Failed to fetch feed {url}: {exc}")
            continue
        for entry in raw or []:
            # `entry` is typically a FeedParserDict. Build the output as a
            # plain dict with explicit field selection — `dict(entry)` would
            # leak feedparser internals (`summary_detail`, `title_detail`,
            # `links`, ...) which are not JSON-serialisable for the Task 6
            # `pending_articles.paragraphs` column. `entry.get(...)` works
            # on both plain dicts and FeedParserDicts.
            link = entry.get('link')
            item = {
                'link': link,
                'title': entry.get('title'),
                'published': entry.get('published', ''),
                'summary': entry.get('summary', ''),
                'feed_url': url,
            }
            # Fall back to the feed URL's netloc when an entry lacks a link
            # — for RSS, the feed netloc equals the entry netloc.
            item['source_name'] = _resolve_source_name(link or url)
            if item['source_name'] == 'other':
                logger.warning(f"Unknown netloc for RSS entry: link={link!r} feed={url!r}")
            entries.append(item)
    return entries


def _fetch_mattel_entries(notifier=None):
    """Fetch Mattel corporate-news entries and stamp `source_name='mattel'`.

    Thin wrapper around `fetch_mattel_news(notifier=...)` that guarantees a
    non-None list and tags every entry. `fetch_mattel_news` already sets
    `feed_url=NEWS_URL`, so that field is left untouched.
    """
    items = fetch_mattel_news(notifier=notifier) or []
    for item in items:
        item['source_name'] = 'mattel'
    return items


# Module-level registry the prep phase (Task 6) will iterate. Order matters
# only for log readability — RSS first (fastest, cheapest), Mattel second
# (single HTTP fetch + HTML parse).
SOURCES = [
    _fetch_rss_entries,
    _fetch_mattel_entries,
]


# Scheduler
def job():
    """Prep-phase cron tick (manual-review-workflow Decision 10).

    The tick now STAGES articles into ``pending_articles`` and pings the
    admin — it no longer publishes to Telegraph or Telegram. Publishing
    is driven by the operator via ``hw_review publish`` (Task 8) or the
    idle-fallback / overflow-fast-track helpers (Tasks 9 & 10) which own
    steps (1) and (4) below.

    Pass layout mirrors tech-spec §How-it-works "Prep phase":
      (1a) idle heads-up        — Task 9 placeholder (no-op).
      (1b) overdue auto-publish — Task 9 placeholder (no-op).
      (2)  fetch all sources    — iterates ``SOURCES`` registry.
      (3)  filter against processed_news + pending_articles.
      (4)  overflow fast-track  — Task 10 placeholder (accepts all).
      (5)  INSERT into pending  — per-entry fetch_full_article + insert.
      (6)  admin ping           — single consolidated notification.
    """
    logger.info("Starting prep-phase tick...")
    init_db()  # idempotent — guard against missing tables on first run.

    # ------------------------------------------------------------------
    # Step (1a): idle heads-up — staged for Task 9.
    # The repo call is real (returns [] when nothing is stale) so the
    # scaffolding stays exercised; Task 9 will add the admin-ping loop.
    # ------------------------------------------------------------------
    try:
        _stale = pending_repo.list_pending_stale(IDLE_TIMEOUT_HOURS)
    except Exception as exc:
        logger.error(f"list_pending_stale failed: {exc}")
        _stale = []
    if _stale:
        # TODO(Task 9): send one consolidated heads-up ping and
        # `mark_notified` each row (Decision 12).
        logger.debug(f"[Task 9 placeholder] {len(_stale)} stale rows awaiting heads-up.")

    # ------------------------------------------------------------------
    # Step (1b): overdue auto-publish — staged for Task 9.
    # ------------------------------------------------------------------
    try:
        _overdue = pending_repo.list_notified_overdue(GRACE_WINDOW_HOURS)
    except Exception as exc:
        logger.error(f"list_notified_overdue failed: {exc}")
        _overdue = []
    if _overdue:
        # TODO(Task 9): call `_fallback_publish(row, via_review=False)` for
        # each overdue row; on success `move_to_published(via_review=False)`,
        # on 3 strikes `move_to_failed` (Decision 13).
        logger.debug(f"[Task 9 placeholder] {len(_overdue)} overdue rows awaiting fallback publish.")

    # ------------------------------------------------------------------
    # Step (2): fetch all sources via the SOURCES registry.
    # One source failing must not abort the tick — sanitise the error
    # string and surface it to the admin, then carry on.
    # ------------------------------------------------------------------
    all_entries = []
    for fetcher in SOURCES:
        fetcher_name = getattr(fetcher, '__name__', repr(fetcher))
        try:
            items = fetcher(notifier=send_admin_notification) or []
        except Exception as exc:
            safe = sanitize_error_message(exc)
            logger.error(f"Source fetcher {fetcher_name} failed: {safe}")
            try:
                send_admin_notification(
                    f"⚠️ Source {fetcher_name} failed: {safe}"
                )
            except Exception as notify_err:
                logger.error(f"Failed to send admin notification: {notify_err}")
            continue
        all_entries.extend(items)
    logger.info(f"Fetched {len(all_entries)} entries across {len(SOURCES)} sources.")

    # ------------------------------------------------------------------
    # Step (3): filter against processed_news (existing helper) AND
    # pending_articles (inline guard — we check each candidate against
    # the repo). ``filter_new_entries_extended`` is deliberately NOT
    # introduced here; the inline form keeps the existing unit tests
    # that exercise ``filter_new_entries`` in isolation valid.
    # ------------------------------------------------------------------
    new_entries = filter_new_entries(all_entries)
    before_pending_filter = len(new_entries)
    new_entries = [
        e for e in new_entries
        if pending_repo.get_pending(e.get('link')) is None
    ]
    if before_pending_filter != len(new_entries):
        logger.info(
            f"Filtered out {before_pending_filter - len(new_entries)} "
            f"entries already in pending_articles."
        )

    # ------------------------------------------------------------------
    # Step (4): overflow fast-track — staged for Task 10.
    # Current behaviour: accept every new entry; Task 10 will implement
    # the QUEUE_CAP / eviction logic (Decision 7).
    # ------------------------------------------------------------------
    # TODO(Task 10): if `pending_repo.count_pending() + len(new_entries) >
    # QUEUE_CAP`, evict ru-NULL pending rows via `_fallback_publish`
    # before inserting the overflow; drop leftover new entries with a
    # consolidated admin ping.
    accepted = new_entries

    # ------------------------------------------------------------------
    # Step (5): stage each accepted entry into pending_articles.
    # ``fetch_full_article`` network-failure → skip (existing behaviour
    # from the former ``process_new_articles`` guard). The repo owns all
    # JSON serialisation — we pass Python lists/dicts verbatim.
    # ------------------------------------------------------------------
    inserted = 0
    for entry in accepted:
        link = entry.get('link')
        if not link:
            logger.warning("Entry has no link, skipping.")
            continue

        article = fetch_full_article(entry)
        if not article or not article.get('paragraphs'):
            logger.warning(f"No article data for {link}, skipping")
            continue

        row = {
            'link': link,
            'source_name': entry.get('source_name') or _resolve_source_name(link),
            'feed_url': entry.get('feed_url'),
            'title': article.get('title') or entry.get('title') or '',
            'subtitle': article.get('subtitle') or '',
            'paragraphs': article.get('paragraphs') or [],
            'images': article.get('images') or [],
            'blocks': article.get('blocks'),
            'pub_date': entry.get('published') or entry.get('pub_date') or '',
        }
        try:
            if pending_repo.insert_pending(row):
                inserted += 1
            else:
                # UNIQUE conflict — another prep tick raced us; expected.
                logger.info(f"Pending row already exists for {link} — skipped.")
        except Exception as exc:
            safe = sanitize_error_message(exc)
            logger.error(f"insert_pending failed for {link}: {safe}")

    # ------------------------------------------------------------------
    # Step (6): admin-ping — exactly one consolidated notification,
    # suppressed on an empty queue (user-spec AC L57).
    # ------------------------------------------------------------------
    try:
        rows = pending_repo.list_pending()
    except Exception as exc:
        logger.error(f"list_pending failed: {exc}")
        rows = []

    ping = build_admin_ping(rows)
    if ping:
        try:
            send_admin_notification(ping)
        except Exception as notify_err:
            # Admin chat being unreachable must not fail the tick.
            logger.error(f"Failed to send admin ping: {notify_err}")

    logger.info(
        f"Prep-phase done. Inserted {inserted}, queue size {len(rows)}."
    )

def main():
    """Entry point."""
    init_db()
    telegraph_publisher.ensure_access_token()
    logger.info("News bot started.")

    # Hourly cron (manual-review-workflow Decision 5). The 2h grace
    # window after an idle-timeout heads-up is only meaningful if the
    # scheduler ticks at least once within that window.
    schedule.every().hour.do(job)

    # Run immediately for testing / first-boot population. The cron loop
    # below starts the hourly cadence after this first tick completes.
    job()

    # Keep the script alive
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()