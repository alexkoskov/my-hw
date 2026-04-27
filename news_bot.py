#!/usr/bin/env python3
"""
Automated news collector and Telegram poster.
Fetches RSS feed, parses articles, translates, summarizes, and posts to Telegram.
"""

import sqlite3
import logging
import re
import os
import json
import asyncio
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytz

from dotenv import load_dotenv

# Load .env at import-time so any code path that reaches news_bot — cron
# job(), overflow auto-publish, manual CLI invocation — sees TELEGRAM_*/
# TELEGRAPH_* credentials. Other entrypoints (hw_review.py, send_post.py,
# ensure_access_token) call load_dotenv() independently; this is the
# news_bot-specific guarantee, not a global one.
load_dotenv()

import feedparser
import requests
from deep_translator import GoogleTranslator
import schedule
from telegram import Bot, LinkPreviewOptions
from telegram.error import TelegramError

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

# Claude transcreation (Wave 2 task 3) + outage state machine (Wave 2
# task 5) — pulled in at import time so ``_fallback_publish`` can pivot
# between Claude (primary) and Google Translate (per-article fallback /
# degraded-mode global fallback) without conditional imports inside the
# hot path. Tests patch the bound names ``news_bot.transcreate_via_claude``
# / ``news_bot.outage_state.is_fallback_active`` on the module surface.
import claude_transcreation
from claude_transcreation import (
    transcreate_via_claude,
    ClaudeTranscreationError,
    ClaudeOutageError,
)
import outage_state

# Pure scheduling helper (Task 02) — produces today's publish slots from a
# pending count + tz-aware ``now``. Imported at module level so ``job()``
# stays free of conditional imports inside the hot path; tests patch
# ``news_bot.compute_publish_slots`` to inject synthetic slot lists.
from compute_publish_slots import compute_publish_slots

# Configuration - set via environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TELEGRAM_ADMIN_ID = os.getenv('TELEGRAM_ADMIN_ID', '@sunny413x')
TRANSLATOR_SERVICE = 'google'  # or 'libre'
RSS_URL = "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml"
DB_FILE = "news.db"
LOG_LEVEL = logging.INFO

# Distributed-publish constants (llm-transcreation-and-distributed-publishing
# Decisions 2, 4, 9, 14, 15). The 13:00–20:00 МСК window + 40-minute minimum
# interval are the same numbers ``compute_publish_slots`` uses by default —
# kept here for explicit reference in ``job()`` (window-end guard) and the
# crash-loop guard (Decision 9). ``BACKLOG_WARNING_THRESHOLD`` seeds the
# AC20 queue-pressure admin ping.
MIN_INTERVAL_MINUTES = 40
WINDOW_START_TIME = datetime.strptime("13:00", "%H:%M").time()
WINDOW_END_TIME = datetime.strptime("20:00", "%H:%M").time()
BACKLOG_WARNING_THRESHOLD = 50
MSK_TZ = pytz.timezone("Europe/Moscow")

# Env-var names whose values must never leak into stored error strings or
# admin-chat messages (Decision 11; ANTHROPIC_API_KEY added per Decision 12
# of llm-transcreation-and-distributed-publishing). Kept as a module-level
# tuple so new secrets can be added without touching the function body.
_SECRET_ENV_NAMES = (
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_CHANNEL_ID',
    'TELEGRAM_ADMIN_ID',
    'TELEGRAPH_ACCESS_TOKEN',
    'ANTHROPIC_API_KEY',
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

# ---------------------------------------------------------------------------
# Security hardening — suppress third-party HTTP INFO logs + token scrubber
# ---------------------------------------------------------------------------
# The Telegram Bot API embeds the bot token in the URL path
# (``https://api.telegram.org/bot<TOKEN>/<method>``).  ``python-telegram-bot``
# uses ``httpx``/``httpcore`` under the hood, both of which log every outbound
# request at INFO level.  With our root logger at INFO that meant the bot
# token was being written to stdout / the systemd journal on every HTTP
# round-trip — a HIGH-severity secret leak discovered during live-publish QA
# of manual-review-workflow.  The triple defence below (explicit level bumps
# on the noisy loggers + a root-level regex filter) ensures no log record —
# ours or a dependency's — emits a raw bot token.
for _noisy in ("httpx", "httpcore", "urllib3", "requests"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# Telegram bot tokens use the shape ``<bot_id>:<35-char-secret>`` where
# ``bot_id`` is 8-10 digits and the secret is drawn from [A-Za-z0-9_-]{35}.
# We keep the pattern slightly loose (``{30,}`` on the secret tail) so a
# future token-format tweak from BotFather still gets caught.
_BOT_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")

# Anthropic API keys take the shape ``sk-ant-<env_or_kind>-<secret>`` and
# may include ``=`` and ``.`` in sandbox/admin variants.  The character
# class below is intentionally broad enough to catch sandbox-shape keys
# (per Decision 12 of llm-transcreation-and-distributed-publishing); the
# ``{16,}`` floor keeps the regex from matching short benign suffixes.
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_=.-]{16,}")


def _redact_text(text):
    """Scrub Telegram-bot-token AND Anthropic-API-key shapes from a string.

    Single source of truth for redaction — used by both the logging filter
    and ``send_admin_notification`` (the admin-notify path lives outside
    the logging pipeline so the filter alone does not cover it; per
    Decision 12).

    Contract:
      * Always returns a string.  Non-string input is coerced (``None`` →
        ``""``, other → ``str(...)``) so the helper never raises on edge
        input.  Matches the silent-fallback invariant of
        ``_TokenRedactingFilter.filter`` which returns ``True`` even on
        internal error.
      * Never raises.  Any internal failure returns the input as-is
        (best-effort coerced to string).
    """
    if text is None:
        return ""
    try:
        if not isinstance(text, str):
            text = str(text)
        text = _BOT_TOKEN_RE.sub("***", text)
        text = _ANTHROPIC_KEY_RE.sub("***", text)
        return text
    except Exception:
        # Defensive: never let redaction break the caller.
        try:
            return text if isinstance(text, str) else str(text)
        except Exception:
            return ""


class _TokenRedactingFilter(logging.Filter):
    """Defence-in-depth: scrub Telegram-bot-token-shaped AND Anthropic-API-
    key-shaped substrings from any LogRecord before it reaches a handler.
    Installed on the root logger plus the noisy HTTP loggers and the
    anthropic SDK loggers so it covers every library we import, including
    ones we haven't audited.

    Rewriting ``record.msg`` / ``record.args`` is safe because ``filter`` is
    invoked after ``getMessage`` caching — handlers re-render from the
    scrubbed fields.  We always return ``True`` (the record is kept, just
    sanitised).  Any internal error is swallowed so a broken filter can
    never break the caller's logging path.
    """

    def filter(self, record):  # noqa: D401 — stdlib signature
        try:
            # Pre-render, scrub, then drop args so the handler doesn't
            # re-format and reintroduce a raw token from ``record.args``.
            rendered = record.getMessage()
            scrubbed = _redact_text(rendered)
            if scrubbed != rendered:
                record.msg = scrubbed
                record.args = None
        except Exception:
            # Never let the filter break logging.
            pass
        return True


_TOKEN_FILTER = _TokenRedactingFilter()
# Attach to the root logger so records routed to root's handlers get
# scrubbed, and to the named noisy loggers so any handler attached
# directly to one of them (e.g. pytest ``caplog``, third-party test
# harnesses, operator-added per-library handlers) also benefits.
logging.getLogger().addFilter(_TOKEN_FILTER)
# Also attach the filter to every handler currently on the root logger.
# Logger-level filters in Python's logging module only run for records
# ORIGINATING on that logger — propagated records from child loggers
# (e.g. ``logging.getLogger("foo").info(...)``) reach root's handlers
# WITHOUT root's logger-level filter being consulted.  Handler-level
# filters DO run on every record dispatched to the handler, including
# propagated ones, so this closes the gap for arbitrary child loggers we
# haven't named explicitly below (Decision 12 hardening).
for _root_handler in logging.getLogger().handlers:
    _root_handler.addFilter(_TOKEN_FILTER)
for _noisy in ("httpx", "httpcore", "urllib3", "requests"):
    logging.getLogger(_noisy).addFilter(_TOKEN_FILTER)
# Anthropic SDK family — the SDK uses its own logger hierarchy and may
# emit ``logger.exception(...)`` lines whose text embeds the API key in
# request URLs / headers.  We only ``addFilter`` (no ``setLevel`` bump):
# anthropic loggers are not "noisy" — their default level is fine.
for _anthropic_logger_name in ("anthropic", "anthropic._client", "anthropic._base_client"):
    logging.getLogger(_anthropic_logger_name).addFilter(_TOKEN_FILTER)


# Admin-notify invariant (Decision 12, llm-transcreation-and-distributed-
# publishing): for outage / SDK-error admin pings, the user-visible text
# MUST use ``type(exc).__name__`` only — never ``str(exc)``.  Full
# ``str(exc)`` may go to logs (which are now redacted by the filter
# above).  ``send_admin_notification`` itself routes its message through
# ``_redact_text`` as a belt-and-suspenders measure, but callers should
# still avoid embedding raw exception text in the user-visible payload.
def send_admin_notification(message):
    """Send a notification message to the admin.

    The ``message`` is passed through ``_redact_text`` BEFORE the Telegram
    payload is built so that any caller that accidentally embeds a secret
    (Telegram bot token, Anthropic API key) sees ``***`` in the chat
    rather than the raw value.  Per Decision 12.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        logging.error("Telegram credentials or admin ID not set.")
        return False
    safe_message = _redact_text(message)
    async def _send():
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        try:
            await bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=safe_message, parse_mode='Markdown')
            logging.info(f"Admin notification sent: {safe_message[:50]}...")
            return True
        except TelegramError as e:
            logging.error(f"Failed to send admin notification: {e}")
            return False
    return asyncio.run(_send())


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
    VIEW button) — the message body is just the source attribution.

    The teaser body is byte-identical for the manual-review path
    (``hw_review publish``) and the auto-fallback path
    (``_fallback_publish``) — Decision 14 of the manual-review-workflow
    tech-spec. Path differentiation lives INSIDE the Telegra.ph article
    body as a ``↳ автоперевод`` paragraph node before the ``Источник:``
    footer (auto-fallback only) — see ``telegraph_publisher.publish_article``
    ``auto_marker`` parameter.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.error("Telegram credentials not set.")
        return False

    source_hashtag = _source_hashtag(source_url)
    # Append the static `#news` tag alongside the source hashtag. Skip the
    # append when `_source_hashtag` produced a bare `#` (unknown / malformed
    # source_url) — emitting a lone `#news` would lose source attribution
    # without giving the subscriber anything in return.
    if source_hashtag and source_hashtag != "#":
        text = f"{source_hashtag} #news"
    else:
        text = source_hashtag

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


# ---------------------------------------------------------------------------
# Preview-file cleanup (shared with ``hw_review.cmd_publish``).
#
# ``hw_review.py`` re-exports its own ``_cleanup_preview_html`` — we keep a
# local implementation here (rather than importing from ``hw_review``) so
# ``news_bot`` has no runtime dependency on the CLI module. The two helpers
# share the same contract: delete the file at ``preview_path`` if it still
# exists, swallow ``FileNotFoundError`` silently, log + swallow any other
# ``OSError``. Never raises.
# ---------------------------------------------------------------------------
def _cleanup_preview_html(preview_path):
    """Remove the cached HTML preview file (best-effort). Parity with
    ``hw_review._cleanup_preview_html``."""
    if not preview_path:
        return
    try:
        os.unlink(preview_path)
    except FileNotFoundError:
        logger.debug(f"preview file already gone: {preview_path}")
    except OSError as exc:
        logger.warning(
            f"could not delete preview file {preview_path}: {exc}"
        )


# ---------------------------------------------------------------------------
# Auto-fallback publisher (manual-review Task 9 + llm-transcreation Task 7).
#
# Called from ``job()`` step (1b) for rows whose grace window has elapsed
# without operator review, and from the overflow fast-track. Translates
# the article EN→RU and runs the canonical Telegraph → Telegram → DB
# pipeline shared with ``hw_review.cmd_publish``:
#
#   Step 1 (translate). Two-tier engine contract per llm-transcreation
#       Decisions 1, 5, 9:
#       * Primary: ``transcreate_via_claude(row)`` if
#         ``outage_state.is_fallback_active() == False``. Returns a dict
#         with ``title`` (emoji prefix), ``subtitle``, ``paragraphs``
#         (and ``blocks`` for autoevolution). Hot Wheels-glossary +
#         emoji safety net are applied inside the Claude module.
#       * Per-article fallback (``ClaudeTranscreationError`` —
#         refusal / malformed JSON / 4xx): drop THIS article to the
#         legacy ``transcreate_text`` Google path. Outage state is NOT
#         advanced (Decision 5 — per-article problems must not take
#         the channel offline).
#       * API-level outage (``ClaudeOutageError`` — 429 / 5xx / auth /
#         network): the state machine and admin-pings already fired
#         inside ``claude_transcreation``. We translate THIS article via
#         Google (degraded mode — never leave a slot unpublished),
#         finish Steps 2–5 normally, and then **re-raise the
#         ClaudeOutageError** so the upstream ``job()`` loop (Task 8)
#         can advance its slot-counter without a strike and route
#         subsequent slots through Google directly.
#       * Already-in-fallback shortcut: when
#         ``is_fallback_active() == True`` on entry, Claude is NOT
#         called at all — the row goes straight to Google. Recovery
#         is owned by ``job()`` (probes Claude on the first slot of
#         the next cron tick), not here.
#   Step 2 (Telegraph). Reuse stored ``telegraph_url`` per Decision 9
#       idempotency; else ``telegraph_publisher.publish_article`` →
#       ``mark_telegraph_published`` (dedicated txn, survives Telegram
#       teaser failure). The ``↳ автоперевод`` marker (user-spec AC18)
#       is injected by ``publish_article(auto_marker=not via_review)``
#       regardless of which engine produced the RU body — uniform
#       across both Claude and Google paths.
#   Step 3 (persist RU). ``pending_repo.update_staged`` writes RU
#       title / subtitle / paragraphs / blocks into the pending row;
#       this is the NOT NULL anchor read by ``move_to_published``.
#   Step 4 (Telegram teaser). ``send_telegraph_teaser`` returns False
#       on soft failure → raise so the caller bumps ``attempt_count``.
#   Step 5 (DB move + preview cleanup). Atomic
#       ``move_to_published(via_review=False)`` then best-effort
#       ``_cleanup_preview_html``.
#
# Contract: returns ``True`` on full success; raises ``ClaudeOutageError``
# AFTER successful Steps 2–5 on the API-level-outage degraded path; raises
# any other exception (Telegraph / Telegram / repo failure) up to the
# caller so ``attempt_count`` can be bumped via
# ``pending_repo.increment_attempt``.
# ---------------------------------------------------------------------------
def _fallback_publish(row, via_review=False):
    """Auto-publish a pending row through Claude (primary) or Google
    (per-article + global fallback). Used by ``job()`` step (e) — the
    distributed-publish loop — and ``hw_review`` operator-driven
    publishes (``via_review=True``).

    Parameters
    ----------
    row : dict
        A pending-articles row as returned by
        ``pending_articles_repo.get_pending`` / ``list_pending``.
    via_review : bool, default False
        Marker persisted into ``published_articles.via_review`` — False
        for auto-publish paths, True reserved for operator-driven
        publishes.

    Returns
    -------
    bool
        ``True`` on success. Re-raises ``ClaudeOutageError`` after a
        successful degraded-mode publish so ``job()`` can advance its
        outage-aware slot loop. Other exceptions propagate to the
        caller so ``attempt_count`` can be bumped.
    """
    link = row['link']

    # Step 1: EN → RU. Two-tier translation engine — see comment header.
    en_title = row.get('title') or ''
    en_subtitle = row.get('subtitle') or ''
    en_paragraphs = row.get('paragraphs') or []
    en_blocks = row.get('blocks')

    # ``outage_signal`` carries a ``ClaudeOutageError`` to re-raise after
    # Steps 2–5 complete. It MUST stay None on the happy / per-article
    # branches so the function's normal True return path is preserved.
    outage_signal = None

    # Pure-Google translation path, factored so both the per-article
    # fallback and the API-level-outage degraded path share one body.
    def _google_translate():
        ru_title = transcreate_text(en_title, is_title=True) if en_title else ''
        ru_subtitle = transcreate_text(en_subtitle) if en_subtitle else ''
        ru_paragraphs = [transcreate_text(p) for p in en_paragraphs]
        ru_blocks = None
        if en_blocks:
            ru_blocks = []
            for block in en_blocks:
                if not isinstance(block, dict):
                    ru_blocks.append(block)
                    continue
                new_block = dict(block)
                if 'text' in new_block and new_block.get('text'):
                    new_block['text'] = transcreate_text(new_block['text'])
                if 'caption' in new_block and new_block.get('caption'):
                    new_block['caption'] = transcreate_text(new_block['caption'])
                ru_blocks.append(new_block)
        return ru_title, ru_subtitle, ru_paragraphs, ru_blocks

    # Already-in-fallback shortcut (Decision 5 / tech-spec "Publish loop"):
    # when the state machine says Claude is down + 2h grace elapsed, route
    # straight to Google without trying Claude. Recovery is owned by
    # ``job()``'s probe on the first slot of the next cron tick.
    if outage_state.is_fallback_active():
        logger.info(
            f"[fallback] is_fallback_active=True — routing {link} via Google"
        )
        ru_title, ru_subtitle, ru_paragraphs, ru_blocks = _google_translate()
    else:
        # Try Claude. The classifier inside ``claude_transcreation``
        # turns SDK exceptions into either ``ClaudeTranscreationError``
        # (per-article) or ``ClaudeOutageError`` (API-level).
        try:
            claude_result = transcreate_via_claude(row)
            ru_title = claude_result.get('title') or ''
            ru_subtitle = claude_result.get('subtitle') or ''
            ru_paragraphs = list(claude_result.get('paragraphs') or [])
            ru_blocks = claude_result.get('blocks')
        except ClaudeTranscreationError as exc:
            # Per-article failure — Google for THIS row only; do NOT
            # advance the outage state machine (Decision 5).
            logger.warning(
                f"[fallback] Claude per-article failure for {link}: "
                f"{type(exc).__name__} — falling back to Google for this article"
            )
            ru_title, ru_subtitle, ru_paragraphs, ru_blocks = _google_translate()
        except ClaudeOutageError as exc:
            # API-level outage — advance the state machine and dispatch
            # admin pings per the outage protocol (2 pings + 2h grace
            # before global Google fallback). Then translate THIS article
            # via Google so the slot doesn't stay unpublished (degraded
            # mode), finish Steps 2–5, and re-raise after the publish so
            # ``job()`` can advance its slot loop without a strike.
            logger.warning(
                f"[fallback] Claude API outage for {link}: "
                f"{type(exc).__name__} — advancing outage state, "
                f"degraded-mode Google publish + re-raise"
            )
            outage_signal = exc
            try:
                # Use a tz-aware UTC datetime — outage_state rejects
                # naive datetimes (timestamps must be unambiguous across
                # the 1h/2h thresholds in the state machine).
                event = outage_state.record_outage_event(
                    datetime.now(timezone.utc),
                )
                for ping_text in event.get('pings_to_send') or []:
                    try:
                        send_admin_notification(ping_text)
                    except Exception as notify_err:
                        logger.error(
                            f"[fallback] outage admin-ping send failed: "
                            f"{sanitize_error_message(notify_err)}"
                        )
            except Exception as state_err:
                # State-machine update failure must not block the
                # degraded-mode publish — log + continue. The re-raise
                # below still informs ``job()`` so it can route
                # subsequent slots through Google.
                logger.error(
                    f"[fallback] outage_state.record_outage_event failed "
                    f"for {link}: {sanitize_error_message(state_err)}"
                )
            ru_title, ru_subtitle, ru_paragraphs, ru_blocks = _google_translate()

    # Step 2: Telegraph — reuse saved URL per Decision 9 idempotency.
    # Done BEFORE persisting RU so a Telegraph failure keeps
    # ``ru_paragraphs IS NULL`` on the pending row — the next slot
    # in the distributed-publish loop will pull it again and the
    # attempt loop can retry. Once Telegraph succeeds the URL is written via
    # ``mark_telegraph_published`` (a dedicated txn) so a Telegram
    # teaser failure still preserves the URL for operator retry.
    telegraph_url = row.get('telegraph_url')
    telegraph_path = row.get('telegraph_path')
    if telegraph_url:
        logger.info(
            f"[fallback] reusing stored telegraph_url for {link}: {telegraph_url}"
        )
    else:
        # Auto-fallback path injects the ``↳ автоперевод`` paragraph
        # inside the Telegra.ph article body (immediately before the
        # Источник footer) so operators and curious readers can tell
        # auto-fallback posts apart from operator-curated ones. The
        # marker mirrors ``via_review`` inverted: ``via_review=False`` →
        # ``auto_marker=True``. Manual hw_review.cmd_publish never sets
        # the flag, so its node tree carries no marker.
        telegraph_url = telegraph_publisher.publish_article(
            title=ru_title,
            paragraphs=ru_paragraphs,
            images=row.get('images') or [],
            source_url=link,
            subtitle=ru_subtitle,
            blocks=ru_blocks,
            auto_marker=not via_review,
        )
        telegraph_path = urlparse(telegraph_url).path.lstrip('/')
        if not telegraph_path:
            logger.warning(
                f"[fallback] telegraph URL yielded empty path: {telegraph_url!r}"
            )
        # Persist BEFORE Telegram so a teaser failure leaves the row
        # retry-idempotent (Decision 9). ``move_to_published`` below
        # reads ``telegraph_url`` from its own explicit argument, not
        # from the row — so the pending-row copy is the idempotency
        # anchor, not an input to the move.
        pending_repo.mark_telegraph_published(link, telegraph_url, telegraph_path)

    # Step 3: persist RU fields. Required for the ``published_articles``
    # NOT NULL ``ru_title`` copy inside ``move_to_published``. Writes
    # here (rather than before Telegraph) keep the row retry-eligible
    # for the next slot on a pure Telegraph failure. On a Telegram-teaser
    # failure the RU fields ARE persisted — that's the correct end
    # state: the operator-driven ``hw_review publish`` retry skips
    # Telegraph (URL cached) and skips transcreation (RU cached).
    pending_repo.update_staged(
        link,
        ru_title or '',
        ru_subtitle or '',
        ru_paragraphs,
        ru_blocks,
    )

    # Step 4: Telegram teaser. False-return → raise so caller bumps
    # attempt_count. (Exceptions from ``send_telegraph_teaser`` propagate
    # naturally — the helper already catches TelegramError internally and
    # returns False, so this raise path covers that "soft" failure mode.)
    # Teaser is single-line for both paths (Decision 14 byte-equality
    # at the visible-feed level). The auto-marker lives in the
    # Telegra.ph article body — see the ``auto_marker`` kwarg passed
    # to ``publish_article`` above.
    ok = send_telegraph_teaser(telegraph_url, link)
    if not ok:
        raise RuntimeError(
            f"send_telegraph_teaser returned False for {link}"
        )

    # Step 5: atomic move (single repo txn).
    pending_repo.move_to_published(
        link, telegraph_url, telegraph_path, via_review=via_review,
    )

    # Step 5: best-effort preview cleanup (noop on None / missing file).
    _cleanup_preview_html(row.get('preview_html_path'))

    logger.info(
        f"[fallback] Published {link} via_review={via_review} url={telegraph_url}"
    )

    # API-level outage signal — re-raise AFTER successful Steps 2–5 so
    # ``job()`` (Task 8) can advance its slot-counting loop without
    # treating this slot as a strike, and so subsequent slots route
    # through Google directly until recovery. This is the contract that
    # keeps the channel publishing AND keeps the upstream loop informed
    # — without re-raise the outage signal would be silently swallowed
    # and ``job()`` would keep trying Claude on every slot (anti-pattern
    # — articles drift to ``move_to_failed`` after 3 strikes).
    if outage_signal is not None:
        raise outage_signal

    return True


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
def _parse_published_at_utc(raw):
    """Parse a SQLite ``published_at`` string into a UTC-aware datetime.

    The column default is ``CURRENT_TIMESTAMP`` (UTC, naive — format
    ``YYYY-MM-DD HH:MM:SS`` or, for direct INSERTs, the ISO-8601 form with
    ``T``). On any parse failure return ``None`` so the crash-loop guard
    fails open (skip the wait) rather than crashing the cron tick.
    """
    if raw is None:
        return None
    try:
        # SQLite CURRENT_TIMESTAMP uses space, not 'T'.
        s = raw.replace('T', ' ')
        # Truncate fractional seconds if present.
        if '.' in s:
            s = s.split('.', 1)[0]
        naive = datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
        return naive.replace(tzinfo=timezone.utc)
    except Exception as exc:
        # Include the exception class in the log so a future operator
        # triaging a corrupted SQLite timestamp sees what failed (CR-1).
        logger.warning(
            f"Could not parse published_at={raw!r} "
            f"({type(exc).__name__}); skipping crash-loop guard."
        )
        return None


def _fallback_publish_google_only(row):
    """Google-only publish path used when ``outage_state.is_fallback_active()``
    is already True at slot time.

    Today this just delegates to ``_fallback_publish``: that helper already
    short-circuits to the Google-translate body when the state machine flag
    is set (see the ``if outage_state.is_fallback_active()`` branch in
    ``_fallback_publish``). Keeping this thin wrapper gives the
    distributed-publish loop a single, intent-named entry-point — and lets
    Task 9's planned cleanup migrate the implementation off ``_fallback_publish``
    without touching ``job()``.
    """
    return _fallback_publish(row, via_review=False)


def job():
    """Daily cron tick — fetch + distributed-publish loop.

    Replaces the manual-review-workflow prep-phase tick (Decision 10) with
    the llm-transcreation-and-distributed-publishing flow (Decisions 2, 4,
    9, 14, 15 + tech-spec §Architecture How-it-works step 7).

    Pass layout:
      (a) crash-loop guard      — sleep until ``last_published + 40min``
                                  if the most recent publish is too fresh.
      (b) fetch + filter + insert — iterate ``SOURCES``, dedup, stage rows.
      (c) compute today's slots  — ``compute_publish_slots(N, now_msk)``.
      (d) admin ping             — plan-of-day; suppressed on N=0;
                                  + backlog warning when N > 50.
      (e) distributed-publish    — sleep-until-slot, publish via
                                  ``_fallback_publish`` (Claude primary +
                                  Google per-article fallback) or, if the
                                  outage state machine has fallback active,
                                  ``_fallback_publish_google_only``.
                                  ``ClaudeOutageError`` re-raises from
                                  step 7 are absorbed and the loop
                                  advances. Other unexpected errors
                                  follow the standard 3-strikes flow
                                  (``increment_attempt`` → ``move_to_failed``).
    """
    logger.info("Starting daily cron tick...")
    init_db()  # idempotent — guard against missing tables on first run.

    # ------------------------------------------------------------------
    # Step (a): crash-loop guard (Decision 9). Defends against systematic
    # container restarts producing burst-publishes by enforcing the
    # ``MIN_INTERVAL_MINUTES`` gap between consecutive publishes across
    # restarts. Reads ``MAX(published_at)`` (UTC-naive) and sleeps until
    # ``last_published + 40min`` if the gap is too small. Failure to
    # parse the timestamp logs a warning and skips the guard (fail-open).
    # ------------------------------------------------------------------
    try:
        last_raw = pending_repo.get_max_published_at()
    except Exception as exc:
        logger.error(
            f"[crash-loop-guard] get_max_published_at failed: "
            f"{sanitize_error_message(exc)}"
        )
        last_raw = None
    last_published = _parse_published_at_utc(last_raw)
    if last_published is not None:
        now_utc = datetime.now(timezone.utc)
        gap = now_utc - last_published
        threshold = timedelta(minutes=MIN_INTERVAL_MINUTES)
        if gap < threshold:
            wait_seconds = (threshold - gap).total_seconds()
            if wait_seconds > 0:
                logger.warning(
                    f"[crash-loop-guard] last publish was {gap.total_seconds():.0f}s "
                    f"ago (< {MIN_INTERVAL_MINUTES}min); sleeping "
                    f"{wait_seconds:.0f}s before continuing."
                )
                time.sleep(wait_seconds)

    # ------------------------------------------------------------------
    # Step (b1): fetch all sources via the SOURCES registry.
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
    # Step (b2): filter against processed_news (existing helper) AND
    # pending_articles (inline guard — we check each candidate against
    # the repo).
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
    # Step (b3): stage each accepted entry into pending_articles.
    # ``fetch_full_article`` network-failure → skip. The repo owns all
    # JSON serialisation — we pass Python lists/dicts verbatim.
    # ------------------------------------------------------------------
    inserted = 0
    for entry in new_entries:
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
    # Step (c): compute today's publish slots.
    # ``compute_publish_slots`` returns (slots, carry_over). N=0 → empty
    # list, no admin ping (Step (d) guard), no loop (Step (e) guard).
    # ------------------------------------------------------------------
    now_msk = datetime.now(MSK_TZ)
    queue_size = pending_repo.count_pending()
    slots, carry_over = compute_publish_slots(queue_size, now_msk)

    # ------------------------------------------------------------------
    # Step (d): admin ping with plan-of-day.
    # Suppressed on N=0 per AC. ``count_pending() > BACKLOG_WARNING_THRESHOLD``
    # adds a separate warning ping (AC20).
    # ------------------------------------------------------------------
    if queue_size > 0:
        slot_strs = ", ".join(s.strftime("%H:%M") for s in slots)
        plan_msg = (
            f"Зафетчил {inserted} новых, в очереди {queue_size}, "
            f"расписание сегодня: {slot_strs or '—'}; "
            f"carry-over: {carry_over}"
        )
        try:
            send_admin_notification(plan_msg)
        except Exception as notify_err:
            logger.error(
                f"Failed to send plan-of-day ping: "
                f"{sanitize_error_message(notify_err)}"
            )
        if queue_size > BACKLOG_WARNING_THRESHOLD:
            backlog_msg = (
                f"⚠️ Backlog warning: queue size {queue_size} "
                f"exceeds threshold {BACKLOG_WARNING_THRESHOLD}; "
                f"carry-over today: {carry_over}"
            )
            try:
                send_admin_notification(backlog_msg)
            except Exception as notify_err:
                logger.error(
                    f"Failed to send backlog warning ping: "
                    f"{sanitize_error_message(notify_err)}"
                )

    # ------------------------------------------------------------------
    # Step (e): distributed-publish loop.
    #
    # For each slot:
    #   * Window-end guard (Decision 15): break if slot beyond 20:00
    #     (insurance against a publish overrunning its slot interval).
    #   * Sleep until slot time (max(0, ...) so a slot in the past fires
    #     immediately).
    #   * Pull oldest pending row; if list_pending is empty (manual review
    #     preempted between cron tick and slot), break.
    #   * Publish via ``_fallback_publish`` (Claude primary + per-article
    #     Google fallback) OR ``_fallback_publish_google_only`` if the
    #     state machine has the global Google fallback active.
    #   * ClaudeOutageError → already published in degraded mode by
    #     ``_fallback_publish``; advance to the next slot without strike.
    #   * Other Exception → 3-strikes flow.
    # ------------------------------------------------------------------
    window_end_dt = datetime.combine(
        now_msk.date(), WINDOW_END_TIME, tzinfo=MSK_TZ,
    )
    published_count = 0
    for idx, slot in enumerate(slots, start=1):
        # Window-end insurance.
        if slot > window_end_dt:
            logger.info(
                f"[slot {idx}/{len(slots)}] {slot.strftime('%H:%M')} > "
                f"window_end {WINDOW_END_TIME.strftime('%H:%M')} — break."
            )
            break

        wait_seconds = max(0.0, (slot - datetime.now(MSK_TZ)).total_seconds())
        if wait_seconds > 0:
            time.sleep(wait_seconds)

        rows = pending_repo.list_pending()
        if not rows:
            logger.info(
                f"[slot {idx}/{len(slots)}] queue empty (manual-review "
                f"preempted) — break."
            )
            break
        row = rows[0]
        link = row.get('link')

        logger.info(
            f"[slot {idx}/{len(slots)}] publishing row {link}"
        )
        try:
            if outage_state.is_fallback_active():
                _fallback_publish_google_only(row)
            else:
                _fallback_publish(row, via_review=False)
            published_count += 1
        except ClaudeOutageError:
            # ``_fallback_publish`` already published in degraded mode and
            # advanced the state machine. We do NOT count this as a strike;
            # the loop continues to the next slot, where the now-active
            # fallback flag routes us through the Google-only path.
            logger.warning(
                f"[slot {idx}/{len(slots)}] ClaudeOutageError surfaced; "
                f"degraded-mode publish completed, continuing loop."
            )
            # Degraded-mode publishes are still real publishes for the
            # end-of-loop summary — folding them into ``published_count``
            # matches what the channel actually saw (CR-2).
            published_count += 1
        except Exception as exc:
            safe = sanitize_error_message(exc)
            logger.error(
                f"[slot {idx}/{len(slots)}] publish failed for {link}: {safe}"
            )
            try:
                new_count = pending_repo.increment_attempt(link, safe)
            except Exception as repo_err:
                logger.error(
                    f"increment_attempt failed for {link}: "
                    f"{sanitize_error_message(repo_err)}"
                )
                continue
            if new_count >= 3:
                try:
                    pending_repo.move_to_failed(link, safe)
                    logger.warning(
                        f"[slot {idx}/{len(slots)}] moved {link} to failed "
                        f"after {new_count} strikes"
                    )
                except Exception as repo_err:
                    logger.error(
                        f"move_to_failed failed for {link}: "
                        f"{sanitize_error_message(repo_err)}"
                    )

    final_queue_size = pending_repo.count_pending()
    logger.info(
        f"[job] done. Published {published_count}, "
        f"carry-over {carry_over}, queue size now {final_queue_size}."
    )


def main():
    """Entry point — startup health checks + daily cron registration.

    Decision 14 startup health checks (BEFORE cron registration so any
    diagnostic ping reaches the operator before the first ``job()`` call):

      1. ``claude_transcreation.health_check()`` — non-raising bool probe.
         On ``False`` we ping the admin and flip the outage state machine
         to ``fallback_active=True`` so the day's first ``job()`` runs
         straight through the Google-only path.
      2. ``os.getenv('TZ') == 'Europe/Moscow'`` — on mismatch we ping the
         admin (warning, not blocking — explicit pytz makes the cron line
         correct regardless of the container's wall-clock TZ).

    Cron change (Decision 2 + 4): the legacy 12-hour cron is replaced by
    a single daily fixed-time cron at 12:00 МСК via ``schedule.every().
    day.at("12:00", tz=pytz.timezone("Europe/Moscow"))``. ``schedule==
    1.2.1`` accepts ``pytz.BaseTzInfo`` or an IANA name string but NOT
    ``zoneinfo.ZoneInfo`` — see TDD anchor in Task 8.
    """
    init_db()
    telegraph_publisher.ensure_access_token()
    logger.info("News bot started.")

    # Health check #1: Claude API + ux-guidelines.md probe.
    if not claude_transcreation.health_check():
        logger.warning(
            "[startup] claude_transcreation.health_check() returned False — "
            "switching to Google-only for the day."
        )
        try:
            send_admin_notification(
                "Claude probe failed at startup, switching to "
                "Google-only for the day"
            )
        except Exception as notify_err:
            logger.error(
                f"[startup] failed to send Claude-probe ping: "
                f"{sanitize_error_message(notify_err)}"
            )
        try:
            outage_state.set_fallback_active(True)
        except Exception as exc:
            logger.error(
                f"[startup] outage_state.set_fallback_active(True) failed: "
                f"{sanitize_error_message(exc)}"
            )

    # Health check #2: container TZ.
    tz_env = os.getenv('TZ')
    if tz_env != 'Europe/Moscow':
        logger.warning(
            f"[startup] TZ env var = {tz_env!r}, expected 'Europe/Moscow'. "
            f"Cron is still correct via explicit pytz, but operator should "
            f"review the container deploy config."
        )
        try:
            send_admin_notification(
                f"⚠️ TZ env var = {tz_env!r}, expected 'Europe/Moscow'. "
                f"Cron is correct via explicit pytz; please review container "
                f"timezone config."
            )
        except Exception as notify_err:
            logger.error(
                f"[startup] failed to send TZ-warning ping: "
                f"{sanitize_error_message(notify_err)}"
            )

    # Daily fixed-time cron at 12:00 МСК (Decisions 2 + 4). pytz is the
    # only timezone API ``schedule==1.2.1`` accepts — see Decision 4.
    schedule.every().day.at("12:00", tz=pytz.timezone("Europe/Moscow")).do(job)

    # Run immediately for first-boot population. Subsequent runs go via
    # the cron loop below, firing at 12:00 МСК daily.
    job()

    # Keep the script alive
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()