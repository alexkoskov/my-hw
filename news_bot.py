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
import llm_transcreation as claude_transcreation  # alias preserves bound name
from llm_transcreation import (
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
# Optional label distinguishing this bot instance in admin pings
# (e.g. "prod" / "test"). Empty / unset → no prefix (backward compat).
INSTANCE_LABEL = os.getenv('INSTANCE_LABEL', '').strip()
TRANSLATOR_SERVICE = 'google'  # or 'libre'
RSS_URL = "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml"
DB_FILE = "news.db"
LOG_LEVEL = logging.INFO

# Distributed-publish constants (llm-transcreation-and-distributed-publishing
# Decisions 2, 4, 9, 14, 15). The 10:00–20:00 МСК window + 40-minute minimum
# interval are the same numbers ``compute_publish_slots`` uses by default —
# kept here for explicit reference in ``job()`` (window-end guard) and the
# crash-loop guard (Decision 9). ``BACKLOG_WARNING_THRESHOLD`` seeds the
# AC20 queue-pressure admin ping.
MIN_INTERVAL_MINUTES = 90
WINDOW_START_TIME = datetime.strptime("10:00", "%H:%M").time()
WINDOW_END_TIME = datetime.strptime("20:00", "%H:%M").time()
BACKLOG_WARNING_THRESHOLD = 50
MSK_TZ = pytz.timezone("Europe/Moscow")

#: Per-article LLM retry tuning — applies to ClaudeTranscreationError
#: only (refusal / malformed JSON / too-short / etc). True outages
#: (ClaudeOutageError) still route through the existing 2-ping + 2h
#: grace state machine in outage_state. Operator-tuned (variant X'):
#: Per-article LLM failure handling: a single attempt per slot. The
#: slot loop's standard 3-strike attempt counter handles transient
#: LLM hiccups across slots (≥ MIN_INTERVAL_MINUTES apart). Inline
#: retry-with-sleep was removed 2026-04-30 — it blocked the slot for
#: up to 10 min synchronously and left the publish loop's pacing
#: unable to absorb a misbehaving article without lag.

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
    'GEMINI_API_KEY',
    'OPENAI_API_KEY',
    'OPENROUTER_API_KEY',
    'OPEN_ROUTER_API_KEY',  # alias accepted by openrouter_transcreation
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


def _feeds_fallback(reason):
    """Log + admin-ping a feeds.json failure and return the default list."""
    logging.warning(f"{reason}. Falling back to default RSS URL.")
    try:
        send_admin_notification(f"⚠️ {reason}. Bot has no RSS feed to process.")
    except Exception as notify_err:
        logging.error(f"Failed to send admin notification: {notify_err}")
    return [RSS_URL]


def load_feeds():
    """Load RSS feed URLs from feeds.json. If missing or invalid, send admin notification and return [RSS_URL]."""
    try:
        with open('feeds.json', 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return _feeds_fallback(f"feeds.json missing or invalid: {e}")

    if not isinstance(data, list):
        return _feeds_fallback("feeds.json does not contain a list")

    valid_urls = []
    for item in data[:5]:  # limit to first 5
        if not isinstance(item, str):
            return _feeds_fallback("feeds.json contains non‑string item")
        parsed = urlparse(item)
        if not (parsed.scheme and parsed.netloc) or parsed.scheme not in ('http', 'https'):
            return _feeds_fallback(f"Invalid URL in feeds.json: {item}")
        valid_urls.append(item)

    if not valid_urls:
        return _feeds_fallback("feeds.json contains no valid URLs")
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

# OpenRouter keys: ``sk-or-v1-<60+ chars>``. Matched FIRST so the more
# generic OpenAI ``sk-...`` pattern below cannot win on the same substring.
_OPENROUTER_KEY_RE = re.compile(r"sk-or-(?:v\d+-)?[A-Za-z0-9_-]{20,}")

# Anthropic API keys take the shape ``sk-ant-<env_or_kind>-<secret>`` and
# may include ``=`` and ``.`` in sandbox/admin variants.  The character
# class below is intentionally broad enough to catch sandbox-shape keys
# (per Decision 12 of llm-transcreation-and-distributed-publishing); the
# ``{16,}`` floor keeps the regex from matching short benign suffixes.
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_=.-]{16,}")

# OpenAI keys come in three shapes:
#   * Project keys  : ``sk-proj-<long-id>``  (50+ chars, includes hyphens)
#   * Service acct  : ``sk-svcacct-<long-id>``
#   * Legacy classic: ``sk-<48-char-base62>`` (no hyphens after the prefix)
# The legacy form must be a separate alternation so it doesn't clash with
# the prefix-based shapes; the contiguous ``[A-Za-z0-9]{32,}`` body
# additionally ensures we do NOT match ``sk-or-v1-...`` or ``sk-ant-...``
# (those contain ``-`` which breaks the contiguous run).
_OPENAI_KEY_RE = re.compile(
    r"sk-(?:proj|svcacct)-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}"
)

# Google API keys (Gemini, Maps, etc.) follow ``AIza<35 chars>`` per
# https://cloud.google.com/docs/authentication/api-keys-best-practices.
# Anchoring to the ``AIza`` prefix makes false positives unlikely; we use
# ``{35,}`` (open-ended) so any future format that grows the body still
# scrubs cleanly without trimming the tail.
_GEMINI_KEY_RE = re.compile(r"AIza[A-Za-z0-9_-]{35,}")


def _redact_text(text):
    """Scrub Telegram-bot-token, Anthropic, OpenAI, OpenRouter, and Gemini
    API-key shapes from a string.

    Single source of truth for redaction — used by both the logging filter
    and ``send_admin_notification`` (the admin-notify path lives outside
    the logging pipeline so the filter alone does not cover it; per
    Decision 12).

    Pattern order matters: OpenRouter (``sk-or-...``) and Anthropic
    (``sk-ant-...``) are scrubbed BEFORE the broader OpenAI ``sk-...``
    pattern so the latter cannot win on substrings already replaced.

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
        text = _OPENROUTER_KEY_RE.sub("***", text)
        text = _ANTHROPIC_KEY_RE.sub("***", text)
        text = _OPENAI_KEY_RE.sub("***", text)
        text = _GEMINI_KEY_RE.sub("***", text)
        return text
    except Exception:
        # Defensive: never let redaction break the caller.
        try:
            return text if isinstance(text, str) else str(text)
        except Exception:
            return ""


class _TokenRedactingFilter(logging.Filter):
    """Defence-in-depth: scrub Telegram-bot-token, Anthropic, OpenAI,
    OpenRouter, and Gemini API-key shapes from any LogRecord before it
    reaches a handler. Installed on the root logger plus the noisy HTTP
    loggers and every LLM SDK logger so it covers every library we import,
    including ones we haven't audited.

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
# LLM SDK families — each uses its own logger hierarchy and may emit
# ``logger.exception(...)`` lines whose text embeds the API key in
# request URLs / headers.  We only ``addFilter`` (no ``setLevel`` bump):
# these loggers are not "noisy" — their default level is fine.
for _llm_logger_name in (
    "anthropic", "anthropic._client", "anthropic._base_client",
    "openai", "openai._base_client",
    "google_genai", "google_genai.models",
    # OpenRouter goes through the openai SDK above.
):
    logging.getLogger(_llm_logger_name).addFilter(_TOKEN_FILTER)


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
    # Prepend [INSTANCE_LABEL] when set so operator can distinguish
    # admin pings from prod vs test bot in the same admin chat.
    if INSTANCE_LABEL:
        safe_message = f"[{INSTANCE_LABEL}] {safe_message}"
    async def _send():
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        try:
            # parse_mode=None (plain text). Markdown was rejected
            # 2026-04-30: a stray ``*`` / ``_`` / ``[`` in a sanitised
            # exception message caused ``can't parse entities`` and the
            # admin ping was silently dropped. Operational alerts have no
            # use for inline formatting, and plain text removes the
            # spoofing risk where article-derived text could rewrite the
            # visible message via crafted ``[label](url)`` syntax.
            await bot.send_message(
                chat_id=TELEGRAM_ADMIN_ID,
                text=safe_message,
            )
            logging.info(f"Admin notification sent: {safe_message[:50]}...")
            return True
        except TelegramError as e:
            logging.error(f"Failed to send admin notification: {e}")
            return False
    return asyncio.run(_send())


logger = logging.getLogger(__name__)


class GoogleTranslationError(Exception):
    """Raised by the Google-fallback path when a translation came back
    mostly-English — typically a 403/blocked Google Translate call that
    returned the source verbatim. Caught by the slot loop; treated as a
    standard publish failure (attempt_count++, eventually move_to_failed).
    """


# ---------------------------------------------------------------------------
# Author social-media plug stripper (post-translation, RU-side).
#
# Variant B of the author-plug-filter feature: surgically removes inline
# plugs left over by the LLM (or Google Translate fallback) — phrases like
# "(подписывайтесь на меня в Instagram @diecast215)" or "follow me on
# Instagram @x" that were embedded in a longer paragraph and so did not
# fit the boilerplate filter's length-bound or whole-string-match shape.
#
# Patterns are anchored on (a) cue verbs paired with a target/platform OR
# (b) a parenthesised phrase containing a platform name + @handle. Bare
# platform mentions like «коллекционер написал в Instagram» do NOT match.
# Corporate plugs like "Follow Mattel on Instagram" do NOT match either —
# A1 and the cue patterns require me/us, not a brand name.
# ---------------------------------------------------------------------------

_PLUG_PLATFORMS_NB = (
    'instagram|twitter|x|tiktok|youtube|facebook|reddit|patreon|discord|linktree'
)

_PLUG_PATTERNS = [
    # Parenthesised plug with mandatory @handle, regardless of verb.
    # Catches the canonical leak shape and Google-Translate variants:
    #   "(подписывайтесь на меня в Instagram @diecast215)"
    #   "(посмотрите меня в Twitter @x)"
    #   "(follow me on Instagram for the latest reveals @diecast215)"
    re.compile(
        r'\s*\(\s*[^()]*?(' + _PLUG_PLATFORMS_NB + r')\s*@\w{2,30}[^()]*?\)\s*',
        re.I,
    ),
    # RU cue-verb sentence: "подписывайтесь / следите за / мой Instagram"
    # paired with target (на меня/нас) OR platform name within the same
    # sentence. Sentence boundary = previous .!? ... next .!? or end.
    re.compile(
        r'(?:(?<=[\.\!\?])\s*|^)'                                       # start
        r'[^\.\!\?]*?'
        r'(?:'
            r'подпиш[иу]тесь|подпис[ыа]вайтесь|подписаться|'
            r'следите\s+за\s+(?:нами|мной)|'
            r'(?:мой|наш|моего|нашего)\s+(?:' + _PLUG_PLATFORMS_NB + r')'
        r')'
        r'[^\.\!\?]*?'
        r'(?:на\s+(?:меня|нас)|за\s+(?:нами|мной)|(?:' + _PLUG_PLATFORMS_NB + r'))'
        r'[^\.\!\?]*?'
        r'(?:[\.\!\?]+|$)\s*',
        re.I,
    ),
    # EN cue-verb sentence: "follow|check|subscribe to me/us on <platform>"
    # paired with platform mention. Sentence-bounded same as RU.
    re.compile(
        r'(?:(?<=[\.\!\?])\s*|^)'
        r'[^\.\!\?]*?'
        r'(?:follow|check|subscribe\s+to)\s+(?:me|us)\s+on\s+'
        r'(?:' + _PLUG_PLATFORMS_NB + r')'
        r'[^\.\!\?]*?'
        r'(?:[\.\!\?]+|$)\s*',
        re.I,
    ),
]


def _strip_plugs(text):
    """Remove inline author-social-media plug sentences from a string.

    Returns cleaned text. Non-string input passes through unchanged. Safe
    on None. Caller is responsible for logging which fragments were
    removed (this helper does not log).

    Idempotent: applying twice yields the same result as applying once.
    """
    if not isinstance(text, str) or not text:
        return text
    cleaned = text
    for pat in _PLUG_PATTERNS:
        cleaned = pat.sub(' ', cleaned)
    # Collapse stray whitespace introduced at sentence boundaries.
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def _strip_plugs_in_blocks(blocks):
    """Apply ``_strip_plugs`` to ``text`` and ``caption`` fields of every
    block. Drops blocks of type paragraph/lead/heading whose ``text``
    became empty after strip. Image/video blocks are kept regardless of
    caption emptiness. Returns a new list; never mutates input."""
    if not isinstance(blocks, list):
        return blocks
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            out.append(b)
            continue
        new_b = dict(b)
        text = new_b.get('text')
        if isinstance(text, str):
            new_b['text'] = _strip_plugs(text)
        cap = new_b.get('caption')
        if isinstance(cap, str):
            new_b['caption'] = _strip_plugs(cap)
        # Drop pure-text block whose text became empty.
        if (
            new_b.get('type') in ('paragraph', 'lead', 'heading')
            and isinstance(new_b.get('text'), str)
            and not new_b['text'].strip()
        ):
            continue
        out.append(new_b)
    return out


def _llm_translation_is_russian(paragraphs, threshold=0.30):
    """Heuristic: total Cyrillic letter share across all paragraphs.

    Mirrors ``_is_mostly_russian`` in the LLM transcreation modules so
    the Google fallback path applies the same EN-leak guard. 30%
    accommodates brand-name density (Hot Wheels, Nissan GT-R, Bugatti)
    while still flagging an entirely-English response. Empty / no-letter
    paragraphs return True (other validators handle empty content).
    """
    total = 0
    cyr = 0
    for p in paragraphs or ():
        if not isinstance(p, str):
            continue
        for ch in p:
            if ch.isalpha():
                total += 1
                if 0x0400 <= ord(ch) <= 0x04FF:
                    cyr += 1
    if total == 0:
        return True
    return (cyr / total) >= threshold


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

def _is_hot_wheels_relevant(entry):
    """Reject articles that came through the Hot Wheels RSS feed by
    cross-tagging but are actually about a sibling Mattel brand.

    autoevolution.com tags Matchbox / Mega Bloks / Hot Wheels articles
    with overlapping topic tags, so the ``tag-Hot+Wheels+News`` feed
    occasionally yields Matchbox-only stories. The channel is Hot
    Wheels-focused — anything where the title names a sibling brand
    *without also* naming Hot Wheels is filtered out at fetch time so
    it never enters ``pending_articles``.
    """
    title = (entry.get('title') or '').lower()
    if not title:
        return True  # nothing to inspect; default include
    if 'hot wheels' in title:
        return True  # explicit HW mention — keep
    # Sibling brands observed in production. Add more conservatively —
    # broad keyword bans risk dropping legitimate cross-over articles.
    sibling_brands = ('matchbox',)
    if any(brand in title for brand in sibling_brands):
        return False
    return True


def filter_new_entries(entries):
    """Filter entries that are not already processed AND that look
    Hot-Wheels-relevant (see ``_is_hot_wheels_relevant`` for the
    sibling-brand exclusion)."""
    new_entries = []
    seen = set()
    skipped_offtopic = 0
    for entry in entries:
        link = entry.get('link')
        if not link or is_processed(link) or link in seen:
            continue
        if not _is_hot_wheels_relevant(entry):
            skipped_offtopic += 1
            logger.info(
                "Skipping off-topic entry (sibling brand): %r",
                entry.get('title'),
            )
            continue
        new_entries.append(entry)
        seen.add(link)
    if skipped_offtopic:
        logger.info(
            "Filtered out %d off-topic entries (sibling brands)",
            skipped_offtopic,
        )
    logger.info(f"Found {len(new_entries)} new entries.")
    return new_entries

# Translation
def transcreate_text(text, source='auto', target='ru', is_title=False):
    """
    Translate and adapt text for a lively Russian Telegram channel.

    Google Translate + minimal post-processing:
    - fixes common Hot Wheels mistranslations (brand names, jargon)
    - prepends a single content-aware emoji to titles (deterministic)

    Per Decision 11 (llm-transcreation-and-distributed-publishing) the
    bureaucratic-regex cleanup and 4000-char body truncation were removed:
    Claude is now the primary transcreator (idiomatic by design) and
    Telegraph has no caption-style length limit. This function survives
    only as a Google-Translate fallback for per-article Claude failures
    and global Anthropic outages, so the HW glossary safety net stays.
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

def send_telegraph_teaser(telegraph_url, source_url):
    """Publish a single-message channel teaser:

    One ``send_message`` whose visible body is the hashtag line
    (e.g. ``#autoevolution #news``). The Telegraph URL travels via
    ``LinkPreviewOptions.url`` with ``show_above_text=True`` and
    ``prefer_large_media=True``, which renders the INSTANT VIEW
    preview card with a full-width image ABOVE the tags. The raw
    URL stays hidden inside the options object.

    Final stack subscribers see:
        [Telegraph IV preview card with full-width image + INSTANT VIEW]
        #source #news

    Spec: work/telegraph-pipeline/post-format.md.
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
                    prefer_large_media=True,
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


def _maybe_record_recovery():
    """Idempotent: if the outage state machine is currently active,
    clear it and emit the recovery admin ping. No-op (cheap read-only
    probe) when the bot is in steady-state healthy mode.

    Called from every successful Claude transcreation in
    ``_fallback_publish`` AND from a successful startup health probe
    in ``main()``. Without it, a single transient outage in the past
    leaves ``fallback_active=1`` set forever — the bot silently
    routes every future article through Google Translate with the
    ``↳ автоперевод`` marker, and the channel is permanently
    degraded until an operator hand-edits ``bot_state``.
    """
    try:
        event = outage_state.record_recovery_event(
            datetime.now(timezone.utc),
        )
    except Exception as state_err:  # noqa: BLE001 — health probe, never raise
        logger.error(
            f"[recovery] outage_state.record_recovery_event failed: "
            f"{sanitize_error_message(state_err)}"
        )
        return
    for ping_text in event.get('pings_to_send') or []:
        try:
            send_admin_notification(ping_text)
        except Exception as notify_err:  # noqa: BLE001
            logger.error(
                f"[recovery] admin-ping send failed: "
                f"{sanitize_error_message(notify_err)}"
            )


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
                # Defensive isinstance() guards: a malformed upstream
                # block dict (e.g. ``text=None`` or a nested object)
                # would have crashed inside ``GoogleTranslator.translate``.
                text_val = new_block.get('text')
                if isinstance(text_val, str) and text_val:
                    new_block['text'] = transcreate_text(text_val)
                cap_val = new_block.get('caption')
                if isinstance(cap_val, str) and cap_val:
                    new_block['caption'] = transcreate_text(cap_val)
                ru_blocks.append(new_block)
        # GoogleTranslator returns the input verbatim on a 403 / blocked
        # call (``transcreate_text`` swallows the exception and falls
        # back to the original string). Under double-engine outage
        # (Claude AND Google both down) this would surface in the
        # channel as RU title + EN body. Reject those — the slot loop
        # bumps attempt_count, the article retries on a later slot.
        if not _llm_translation_is_russian(ru_paragraphs):
            raise GoogleTranslationError(
                "Google fallback returned mostly-English paragraphs — "
                "likely 403/blocked translate call returning source verbatim",
            )
        return ru_title, ru_subtitle, ru_paragraphs, ru_blocks

    # Track whether THIS article was translated by Google fallback (legacy
    # path, lower quality) vs. by an LLM (Claude / OpenRouter / etc, current
    # quality bar). Only the Google-fallback path warrants the
    # ``↳ автоперевод`` marker on the Telegra.ph page (operator decision —
    # users see the marker as a quality warning; LLM output is
    # indistinguishable from manual review and should not carry it).
    used_google_fallback = False

    # Already-in-fallback shortcut (Decision 5 / tech-spec "Publish loop"):
    # when the state machine says Claude is down + 2h grace elapsed, route
    # straight to Google without trying Claude.
    if outage_state.is_fallback_active():
        logger.info(
            f"[fallback] is_fallback_active=True — routing {link} via Google"
        )
        ru_title, ru_subtitle, ru_paragraphs, ru_blocks = _google_translate()
        used_google_fallback = True
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
            # Healthy Claude call → close any active outage and emit
            # the recovery ping. Idempotent: if no outage was active
            # this is a cheap read-only probe inside outage_state.
            _maybe_record_recovery()
        except ClaudeTranscreationError as exc:
            # Operator decision: GPT translates EVERYTHING. Per-article
            # LLM hiccups (refusal, malformed JSON, schema drift) raise
            # straight up — the slot loop bumps attempt_count, and after
            # 3 slot strikes (= 3 cron slots ≈ 4.5h with the 90-min floor)
            # the article auto-moves to failed_articles. No inline retry
            # with time.sleep — that would block the slot for 10+ min
            # synchronously and stall publish-loop pacing.
            logger.warning(
                f"[fallback] Claude per-article failure for {link}: "
                f"{type(exc).__name__}: {sanitize_error_message(exc)} "
                f"— slot strike (next slot retries this row)"
            )
            raise
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
            # NB: ``outage_signal`` is intentionally function-local. If
            # Steps 2-5 below raise a non-outage exception (Telegraph
            # 503, Telegram error, …) the signal is "lost" — but
            # ``record_outage_event`` already persisted the state in
            # bot_state, so cross-call communication still works via
            # the DB. The protocol explicitly probes Claude again on
            # subsequent slots between ping #1 and ping #3 (1-2h) on
            # the off chance the outage was transient; the global
            # ``is_fallback_active`` flag flips at ping #3, and only
            # then does the slot loop short-circuit Claude.
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
            used_google_fallback = True

    # Step 1b: strip author social-media plugs from RU output.
    # Single call site — covers Claude-success, is_fallback_active shortcut,
    # ClaudeOutageError degraded mode (every path that reaches this point).
    # Wrapped in try/except: regex bug must not block publish (publish-
    # something > publish-nothing). Per-fragment INFO log so operator can
    # spot false positives via journalctl.
    try:
        original_pieces = (
            [ru_title or '', ru_subtitle or '']
            + list(ru_paragraphs or [])
            + [
                str(b.get('text') or '') + '|' + str(b.get('caption') or '')
                for b in (ru_blocks or [])
                if isinstance(b, dict)
            ]
        )
        ru_title = _strip_plugs(ru_title)
        ru_subtitle = _strip_plugs(ru_subtitle)
        if ru_paragraphs:
            ru_paragraphs = [
                p for p in (_strip_plugs(p) for p in ru_paragraphs)
                if isinstance(p, str) and p.strip()
            ]
        ru_blocks = _strip_plugs_in_blocks(ru_blocks)
        cleaned_pieces = (
            [ru_title or '', ru_subtitle or '']
            + list(ru_paragraphs or [])
            + [
                str(b.get('text') or '') + '|' + str(b.get('caption') or '')
                for b in (ru_blocks or [])
                if isinstance(b, dict)
            ]
        )
        if original_pieces != cleaned_pieces:
            logger.info(
                f"[author_plug] stripped from {link!r} "
                f"(via_review={via_review}, used_google={used_google_fallback})"
            )
    except Exception as plug_err:
        logger.error(
            f"[author_plug] strip failed for {link}: "
            f"{sanitize_error_message(plug_err)} — using original RU"
        )

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
        # ``↳ автоперевод`` marker is injected ONLY when this article was
        # translated via Google fallback (legacy lower-quality path). LLM
        # output (Claude / OpenRouter / etc) is treated as production
        # quality and carries no marker, matching the manual-review path's
        # node-tree shape. Manual review (via_review=True) never gets the
        # marker either.
        telegraph_url = telegraph_publisher.publish_article(
            title=ru_title,
            paragraphs=ru_paragraphs,
            images=row.get('images') or [],
            source_url=link,
            subtitle=ru_subtitle,
            blocks=ru_blocks,
            auto_marker=used_google_fallback and not via_review,
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
      (d) admin ping             — plan-of-day; always fires (heartbeat on
                                  quiet days); + backlog warning when N > 50.
      (e) distributed-publish    — sleep-until-slot, publish via
                                  ``_fallback_publish`` (Claude primary +
                                  Google per-article fallback). When the
                                  outage state machine has fallback active,
                                  ``_fallback_publish`` short-circuits to
                                  Google internally. ``ClaudeOutageError``
                                  re-raises are absorbed and the loop
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
    slots, carry_over = compute_publish_slots(
        queue_size, now_msk,
        window_start=WINDOW_START_TIME,
        window_end=WINDOW_END_TIME,
    )

    # ------------------------------------------------------------------
    # Step (d): admin ping with plan-of-day. Always sent — operator wants a
    # heartbeat that confirms the cron tick fired, even on quiet days when
    # there are no new articles and the queue is empty.
    # Quiet day: «🟢 Бот сработал, новых статей нет.»
    # Busy day:  «Зафетчил N новых, в очереди M, расписание сегодня: …; carry-over: K»
    # Backlog warning fires as a separate ping at queue_size > 50 (AC20).
    # ------------------------------------------------------------------
    if queue_size == 0 and inserted == 0:
        plan_msg = "🟢 Бот сработал, новых статей нет."
    else:
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
    #     Google fallback). ``_fallback_publish`` short-circuits to Google
    #     internally when the state machine has the global Google fallback
    #     active.
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
    a single daily fixed-time cron at 10:00 МСК via ``schedule.every().
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
    else:
        # Probe healthy — close any stale outage state from a prior run.
        # Without this, a transient outage that happened yesterday and
        # was never recovered (because nothing called record_recovery_event
        # on success) would persist across restarts and force every slot
        # through Google forever.
        _maybe_record_recovery()

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

    # Daily fixed-time cron at 10:00 МСК (Decisions 2 + 4). pytz is the
    # only timezone API ``schedule==1.2.1`` accepts — see Decision 4.
    schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)

    # Run immediately for first-boot population. Subsequent runs go via
    # the cron loop below, firing at 10:00 МСК daily.
    job()

    # Keep the script alive
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
