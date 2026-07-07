#!/usr/bin/env python3
"""
Automated news collector and Telegram poster.
Fetches RSS feed, parses articles, translates, summarizes, and posts to Telegram.
"""

import sqlite3
import logging
import re
import os
import sys
import json
import asyncio
import fcntl
import socket
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

# Global socket default timeout. feedparser.parse() uses urllib.urlopen()
# under the hood and offers no `timeout` kwarg (6.0.12 sig confirmed); a
# slow RSS server (e.g. Cloudflare-fronted autoevolution.com sending TCP
# ACK but no data) would otherwise block job() until kernel tcp_keepalive
# fires hours later. Prod incident 2026-06-08: job() hung 2.5h+ after
# Database initialized, missing the daily admin ping and any publishes.
# 20s caps the worst-case wait per socket op; legitimate fast paths
# (autoevolution curl_cffi, requests.get in source parsers, Telegraph API
# client) all pass their own explicit timeout already, so this is a
# defense-in-depth floor for any socket that forgot to.
socket.setdefaulttimeout(20)

import feedparser
from deep_translator import GoogleTranslator
import schedule
from telegram import Bot, LinkPreviewOptions
from telegram.error import TelegramError

from mattel_news_source import fetch_mattel_news, fetch_mattel_article
import autoevolution_source
import lamley_source
import orangetrack_source
import t_hunted_source
import telegraph_publisher
from telegraph_publisher import TelegraphError

from boilerplate_filter import is_boilerplate

# Late-binding DAO for the manual-review-workflow queue tables
# (``pending_articles``, ``published_articles``, ``failed_articles``).
# Imported under a short alias so the prep-phase ``job()`` body reads
# cleanly and matches the vocabulary used in tech-spec §Architecture.
# ``pending_articles_repo`` itself imports ``news_bot`` at module level
# for ``DB_FILE`` access — the cycle resolves because this import runs
# after all our module-level names have been bound.
import pending_articles_repo as pending_repo

# Claude transcreation (Wave 2 task 3) + outage state machine (Wave 2
# task 5) — pulled in at import time so ``_fallback_publish`` translates via
# the LLM only and HOLDS articles on an outage (no Google fallback since the
# 2026-06-11 hold-and-wait change). Tests patch the bound name
# ``news_bot.transcreate_via_claude`` on the module surface.
import llm_transcreation as claude_transcreation  # alias preserves bound name
from llm_transcreation import (
    transcreate_via_claude,
    ClaudeTranscreationError,
    ClaudeOutageError,
)
import outage_state
import admin_alerts

# Cross-source dedup (Wave 2, cross-source-dedup feature). Pure module —
# extract_fingerprint scans the article body against the brand lexicon,
# similarity computes guarded two-level Jaccard. Used by the new gate in
# ``job()`` between ``_is_text_only_checklist`` and ``insert_pending``.
# Imported at module load so tests can patch ``news_bot.model_extractor.*``
# (the degraded-mode test stubs ``extract_fingerprint`` to raise).
import model_extractor

# Pure scheduling helper — produces today's publish slots from a pending count
# + tz-aware ``now``. Imported at module level so ``job()`` stays free of
# conditional imports inside the hot path; tests patch
# ``news_bot.compute_fixed_slots`` to inject synthetic slot lists.
# Fixed 3-slots-per-day scheduler (operator pacing 2026-06-13: 10:00/15:00/19:30
# МСК). The old dynamic even-spread ``compute_publish_slots`` is DORMANT and no
# longer imported here.
from compute_publish_slots import compute_fixed_slots

# Configuration - set via environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TELEGRAM_ADMIN_ID = os.getenv('TELEGRAM_ADMIN_ID', '@sunny413x')
# Optional label distinguishing this bot instance in admin pings
# (e.g. "prod" / "test"). Empty / unset → no prefix (backward compat).
INSTANCE_LABEL = os.getenv('INSTANCE_LABEL', '').strip()
TRANSLATOR_SERVICE = 'google'  # or 'libre'
RSS_URL = "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml"
# Env-overridable so the Docker+VPN container can point at its mounted volume
# (DB_FILE=/data/news.db) instead of an ephemeral /app/news.db. Default keeps the
# NL/systemd + test behaviour. Read once at import; callers read news_bot.DB_FILE
# at call time (see pending_articles_repo / outage_state module docstrings).
# ``.strip() or "news.db"``: a blank/whitespace-only DB_FILE= in .env would
# otherwise resolve to "" — and sqlite3.connect("") opens a throwaway temp DB
# (empty state → channel re-flood), the exact failure this guards against. A
# stray surrounding space (`DB_FILE=/data/news.db `) is likewise a different path.
DB_FILE = os.getenv("DB_FILE", "news.db").strip() or "news.db"
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

#: Hard cap on publications per day. With the fixed-slot scheduler
#: (``compute_fixed_slots``, operator pacing 2026-06-13) the cap is a natural
#: consequence of the three fixed times (10:00/15:00/19:30 МСК), so this
#: constant is now redundant for trimming — kept for reference / back-compat
#: (other code/tests reference ``news_bot.MAX_DAILY_POSTS``). Surplus pending
#: articles go into carry_over and wait for the next cron tick.
MAX_DAILY_POSTS = 3

#: In-slot publish retry (operator decision 2026-06-17). A transient network
#: failure on the publish side — e.g. a Telegra.ph / Telegram read timeout —
#: should not cost the article its slot until the next day. On such a failure
#: the slot loop retries the SAME row up to ``PUBLISH_RETRY_ATTEMPTS`` more
#: times, ``PUBLISH_RETRY_DELAY_SECONDS`` apart, before falling through to the
#: cross-day 3-strike path (``increment_attempt`` → ``move_to_failed``). The
#: retry re-runs the full publish (incl. re-translation) — acceptable for a
#: rare transient blip. An LLM outage (``ClaudeOutageError``) is NOT retried
#: here: it HOLDS immediately (see ``_publish_with_retries``).
#: Slot spacing (10:00/15:00/19:30 МСК, ≥90 min apart) means the max
#: ATTEMPTS*DELAY (~40 min) delay cannot push one slot's publish into the
#: next slot. The only edge: retries on the LAST slot (19:30) can finish past
#: the 20:00 window-end — harmless (it only delays the day's final post;
#: revisit if a 4th, non-last slot is ever added near the window edge).
PUBLISH_RETRY_ATTEMPTS = 4
PUBLISH_RETRY_DELAY_SECONDS = 600  # 10 minutes

#: Channel-silence alert (2026-06-23). If nothing has been published for this
#: many days, job() sends a LOUDER [E017] admin warning at the end of the tick
#: — a stronger signal than the daily [E009] "нет новостей" — to catch a
#: prolonged dry spell (over-strict filter dropping everything, a dead source,
#: or server-network trouble). One ping per tick while the dry spell persists.
DRY_SPELL_ALERT_DAYS = 3

#: Seconds to sleep between Telegra.ph page creation and the Telegram
#: teaser send. Lets Telegra.ph's edge cache populate OG tags before the
#: Telegram IV worker fetches the URL — without this gap Telegram has been
#: observed to negative-cache "no preview" for the freshly-created page
#: (incident 2026-05-16: prod post lost hero image while test-instance
#: post for the same article kept it). Tests filter sleep calls by this
#: exact value to ignore the warmup pause when counting slot-wait sleeps.
TELEGRAPH_CACHE_WARMUP_SECONDS = 3

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
        send_admin_notification(admin_alerts.alert_no_rss_feeds(reason))
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
#: Max attempts for send_admin_notification before giving up. Added
#: 2026-06-09 after a single ``TelegramError: Timed out`` on the prod
#: morning tick (10:00:23 МСК) silently dropped that day's ``[E008]``
#: plan-of-day ping. The article-publish HTTP call later in the same
#: tick succeeded, confirming the failure was a transient Telegram
#: edge slowness rather than a credential / network outage. Three
#: attempts with 1s + 2s backoff costs at most ~3s on the bad path
#: and zero on the happy path.
ADMIN_NOTIFICATION_MAX_ATTEMPTS = 3


def send_admin_notification(message, *, max_attempts=ADMIN_NOTIFICATION_MAX_ATTEMPTS):
    """Send a notification message to the admin with bounded retry.

    Retries on ``TelegramError`` only (timeouts, transient network) —
    auth / chat-id errors aren't worth re-sending. Exponential backoff
    between attempts: 1s, 2s. Default ``max_attempts=3``; callers can
    pass ``max_attempts=1`` to opt out (e.g. fire-and-forget paths
    where the operator can tolerate a single missed ping).

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

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            asyncio.run(_send())
            logging.info(f"Admin notification sent: {safe_message[:50]}...")
            return True
        except TelegramError as e:
            last_err = e
            if attempt < max_attempts:
                backoff_seconds = 2 ** (attempt - 1)  # 1, 2, 4 (last unused)
                logging.warning(
                    f"Admin notification attempt {attempt}/{max_attempts} "
                    f"failed ({type(e).__name__}: {e}); retrying in "
                    f"{backoff_seconds}s."
                )
                time.sleep(backoff_seconds)
    logging.error(
        f"Failed to send admin notification after {max_attempts} attempts: "
        f"{last_err}"
    )
    return False


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


def _count_processed_news():
    """Number of rows in ``processed_news`` — the anti-repost ledger. A prod
    instance whose ledger is empty at startup is almost certainly running on an
    empty/ephemeral DB (see ``_prod_db_guard``)."""
    conn = sqlite3.connect(DB_FILE)
    try:
        return conn.execute("SELECT COUNT(*) FROM processed_news").fetchone()[0]
    finally:
        conn.close()


def _prod_db_guard():
    """Return a list of prod-DB integrity warnings (empty if OK or not a prod
    instance). B2 re-flood guard.

    The Moscow container keeps state on a mounted volume via
    ``DB_FILE=/data/news.db``. If that ``.env`` line is dropped, ``DB_FILE``
    falls back to the relative default ``"news.db"`` on the EPHEMERAL /app
    layer → ``init_db()`` creates an empty DB → ``processed_news`` is empty →
    the bot re-floods the channel with months of RSS backlog. Gated on
    ``INSTANCE_LABEL == 'prod'`` so test/local/CI (relative default DB, empty
    test DBs) never false-alarm.
    """
    if INSTANCE_LABEL != 'prod':
        return []
    warnings = []
    if not os.path.isabs(DB_FILE):
        warnings.append(
            f"DB_FILE={DB_FILE!r} — не абсолютный путь; прод-состояние может "
            f"оказаться на временном слое контейнера, а не на смонтированном /data"
        )
    try:
        if _count_processed_news() == 0:
            warnings.append(
                "таблица processed_news ПУСТАЯ на старте — если это не самый "
                "первый деплой, база не персистентная и канал будет перезалит"
            )
    except Exception as db_err:
        logger.error(
            f"[startup] processed_news count failed: "
            f"{sanitize_error_message(db_err)}"
        )
    return warnings


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

#: Sources whose RSS feeds are *broad diecast collectors* — they cover
#: many brands (Hot Wheels, Matchbox, M2 Machines, Auto World, Mini GT,
#: Topper Toys / Johnny Lightning, …) and most posts are NOT about
#: Hot Wheels. For these sources we flip the default: an entry without
#: an explicit Hot Wheels mention in the title is rejected, not kept.
#: Added 2026-06-09 after two non-HW t-hunted posts leaked through to
#: the test channel ("Topper Toys 1970: Johnny Lightning…", "Mundo
#: Premium 64 #241: Porsche, Mustang и Diablo…"). Other sources
#: (autoevolution / lamley / orangetrack) retain the default-include
#: policy that operator-confirmed works for HW-focused sources.
_BROAD_DIECAST_NETLOCS = ('t-hunted.blogspot.com',)

#: Authoritative brand labels from the source's own taxonomy (Blogger
#: "Labels:" / "Marcadores:", carried in the RSS feed as <category> and
#: surfaced on the entry dict as ``labels``). t-hunted tags every post with
#: the brand/series it covers — a sibling-brand label means it is NOT Hot
#: Wheels (2026-06-24: a Matchbox "Moving Parts" post slipped through the
#: title-only filter because "Moving Parts" is also a HW-sounding name).
#: Lowercased exact-match against the entry's labels. Add brands seen on the
#: feed + well-known diecast siblings.
_SIBLING_BRAND_LABELS = frozenset({
    'matchbox', 'maisto', 'tomica', 'johnny lightning', 'tarmac works',
    'm2 machines', 'mini gt', 'auto world', 'majorette', 'greenlight',
    'kaido house',
})

#: Hot Wheels series/line labels — a positive HW signal from the source's own
#: taxonomy. Keeps genuine HW posts whose title names neither "Hot Wheels" nor
#: the series (e.g. the Pop Culture Porsche, titled only in Portuguese).
_HW_SERIES_LABELS = frozenset({
    'silver series', 'pop culture', 'car culture', 'team transport',
    'boulevard', 'neon speeder', 'neon speeders', 'rlc', 'red line club',
    'trackin trucks', 'track fleet',
})

#: Hot Wheels line / series names matched as a SUBSTRING of the title — the
#: fallback when the source carries no usable label. HW-specific, multi-word
#: names only (single common words risk false includes). NB "moving parts" is
#: deliberately absent — it is a MATCHBOX line, not Hot Wheels.
_HW_SERIES_SIGNALS = (
    'silver series', 'pop culture', 'neon speeder',
    'car culture', 'team transport', 'boulevard', 'red line club',
)


def _is_hot_wheels_relevant(entry):
    """Reject articles that reach a feed by cross-tagging but are actually
    about a sibling diecast brand. The channel is Hot Wheels-only.

    Decision order (most authoritative first):
      1. Explicit "hot wheels" in the title → keep (cross-over round-ups).
      2. Sibling-brand LABEL (source's own taxonomy) → reject.
      3. Hot Wheels series LABEL → keep.
      4. "matchbox" in the title → reject (autoevolution cross-tag; no labels).
      5. HW series name in the title → keep (label-less t-hunted posts).
      6. Broad-diecast source (``_BROAD_DIECAST_NETLOCS``) with no HW signal →
         reject; everyone else defaults to keep.
    """
    title = (entry.get('title') or '').lower()
    labels = {str(lbl).strip().lower() for lbl in (entry.get('labels') or [])}
    if not title and not labels:
        return True  # nothing to inspect; default include
    if 'hot wheels' in title or 'hotwheels' in title:
        return True  # explicit HW mention — keep regardless of source/label
    # Authoritative brand label from the source. A sibling-brand tag rejects
    # even when the title looks HW-ish (e.g. "Moving Parts" = a Matchbox line).
    if labels & _SIBLING_BRAND_LABELS:
        return False
    # Authoritative HW series label — keep even if the title has no HW signal.
    if labels & _HW_SERIES_LABELS:
        return True
    # Title-only fallbacks (sources without usable labels, e.g. autoevolution).
    if 'matchbox' in title:
        return False
    if any(sig in title for sig in _HW_SERIES_SIGNALS):
        return True
    # Broad-diecast source guard. Reject entries from these feeds when the
    # title has no Hot Wheels signal; they default to "not HW".
    link = (entry.get('link') or '').lower()
    if any(netloc in link for netloc in _BROAD_DIECAST_NETLOCS):
        return False
    return True


#: Word-bounded match for "checklist", "check list", "check-list" — used
#: by ``_is_text_only_checklist`` to detect the title side of the
#: two-condition rule. Word-boundaries prevent false matches like
#: "checklister" or substring hits on unrelated tokens.
_CHECKLIST_TITLE_RE = re.compile(r'\bcheck[\s-]?list\b', re.IGNORECASE)

#: Threshold (in characters) below which a "checklist"-titled article's
#: paragraph body counts as "no real text" — i.e. a bare list with no
#: editorial content. A real review article on orangetrack /
#: autoevolution typically has 2000-10000 chars of paragraph text;
#: 500 sits well below that floor and well above any sensible
#: list-only post (which usually has a 50-200 char header + bullets
#: that don't end up in ``paragraphs``).
_CHECKLIST_BODY_TEXT_FLOOR = 500

#: URL-slug match (case-insensitive) for orangetrack's recurring
#: "case-contents-checklist" posts (e.g. ``/hot-wheels-basics-2026-j-
#: case-contents-checklist-for-mainline/``). These are always bare
#: lists of car names — orangetrack pads them with per-car blurbs so
#: the body floor (_CHECKLIST_BODY_TEXT_FLOOR) doesn't trip, but the
#: prose is ~80% proper nouns, which makes the LLM produce English-
#: leaking output and triggers the EN-leak guard. Verified prod
#: incident 2026-05-12: J Case Contents Checklist failed translation,
#: held up the only publish slot of the day, and left the channel
#: silent. URL-slug trigger is independent of title/body length.
_CHECKLIST_URL_RE = re.compile(r'case-contents-checklist', re.IGNORECASE)


def _is_text_only_checklist(entry, article):
    """True iff the article looks like a bare checklist post with no
    real editorial body. Two independent triggers, either is enough:

      A. ``entry['link']`` URL slug matches ``_CHECKLIST_URL_RE``
         (orangetrack's "case-contents-checklist" pattern — always a
         list of model names regardless of body length).
      B. Title matches ``_CHECKLIST_TITLE_RE`` (whole-word "checklist")
         AND total paragraph text length < ``_CHECKLIST_BODY_TEXT_FLOOR``.

    Source-agnostic on trigger B; trigger A targets a specific
    orangetrack URL pattern. Articles with "checklist" in the title
    AND a real review body (≥ 500 chars) and no matching URL slug are
    kept (it's a review *of* a checklist, not a bare list).

    Called from ``job()`` step (b3) AFTER ``fetch_full_article`` so the
    body content is available; on a True return the row never enters
    ``pending_articles``, saving the LLM-translation API call too.
    """
    link = entry.get('link') or ''
    if _CHECKLIST_URL_RE.search(link):
        return True
    title = entry.get('title') or (article or {}).get('title') or ''
    if not _CHECKLIST_TITLE_RE.search(title):
        return False
    paragraphs = (article or {}).get('paragraphs') or []
    total_text = sum(len(p) for p in paragraphs if isinstance(p, str))
    return total_text < _CHECKLIST_BODY_TEXT_FLOOR


#: Hard-block threshold for cross-source dedup (tech-spec Decision 7,
#: user-spec AC3). Articles whose ``similarity`` against any candidate in
#: the 7-day window meets or exceeds this value are dropped before
#: ``insert_pending`` and pinned in ``processed_news`` so the same URL is
#: not re-fetched on subsequent ticks (Decision 8).
_DEDUP_BLOCK_THRESHOLD = 0.50

#: Soft-flag threshold for cross-source dedup (Decision 7, user-spec AC4).
#: Articles in ``[0.30, 0.50)`` pass through to ``insert_pending`` but
#: trigger a per-pair-rate-limited E014 ping so the operator can review.
_DEDUP_FLAG_THRESHOLD = 0.30


def _check_cross_source_dedup(article: dict, fingerprint: dict,
                              conn: sqlite3.Connection, new_source=None):
    """Compare ``fingerprint`` against the last 7 days of pending +
    published rows and decide block / flag / pass.

    Returns one of:
      * ``('block', match_dict)`` — similarity ≥ ``_DEDUP_BLOCK_THRESHOLD``.
      * ``('flag', match_dict)``  — ``[_DEDUP_FLAG_THRESHOLD,
        _DEDUP_BLOCK_THRESHOLD)``.
      * ``('pass', None)``        — below the flag threshold OR empty
        ``fingerprint['strict']`` (AC6 — skip the SQL round-trip on
        articles with no recognised brands).

    ``match_dict`` carries the keys the gate caller needs to render an
    admin ping:
      * ``link``         — the matched article URL.
      * ``source_name``  — the matched row's source (for E014 ``existing_source``).
      * ``models``       — sorted list of shared strict tokens (intersection
        of the two ``strict`` sets). Empty list when the match is brand-
        only (strict Jaccard zero but the AC10 brand-fallback fired).
      * ``overlap_pct``  — ``int(round(sim * 100))``, used by E014 / E015.
      * ``n_matches``    — count of shared strict tokens.
      * ``n_total``      — count of distinct strict tokens across both
        fingerprints (``len(strict_new | strict_match)``); denominator
        the operator sees in ``"3/8"``.

    Implementation notes:
      * Iterates pending FIRST, then published — both windowed at 7 days
        per tech-spec Architecture. CROSS-SOURCE ONLY (Decision 9 reversed
        2026-06-14): candidates whose ``source_name`` equals ``new_source``
        are skipped — one source republishing the same model within 7 days
        is implausible, and comparing within-source only produces false
        positives (e.g. autoevolution Ford F-100 vs Porsche Team Transport
        sharing a brand token). When ``new_source`` is None/unknown, no
        candidate is skipped (fail-open: never miss a real cross-source dup).
      * Picks the best (highest sim) match across the entire window so
        the strongest signal wins even if a weaker pending match would
        also cross the soft-flag threshold.
      * Compare-side ``model_fingerprint`` rows can be missing (NULL,
        pre-feature rows or backfill in progress) — those candidates are
        skipped silently rather than treated as zero-similarity matches.
    """
    strict = fingerprint.get('strict') or [] if isinstance(fingerprint, dict) else []
    if not strict:
        # AC6 short-circuit — empty fingerprint cannot match anything, and
        # the dominant production-path "industry news with no brands"
        # shouldn't pay for two SELECTs.
        return ('pass', None)

    candidates = (
        pending_repo.list_recent_pending_fingerprints(conn, 7)
        + pending_repo.list_recent_published_fingerprints(conn, 7)
    )

    best_sim = 0.0
    best_row = None
    for row in candidates:
        if new_source and row.get('source_name') == new_source:
            # Same-source candidate — never deduped (Decision 9 reversed
            # 2026-06-14: within-source republishes don't happen; comparing
            # them only yields false positives).
            continue
        cand_fp = row.get('model_fingerprint')
        if not isinstance(cand_fp, dict):
            # NULL (pre-feature) or malformed row — skip silently. The
            # backfill script (Task 5) populates these going forward.
            continue
        sim = model_extractor.similarity(fingerprint, cand_fp)
        if sim > best_sim:
            best_sim = sim
            best_row = row

    if best_row is None or best_sim < _DEDUP_FLAG_THRESHOLD:
        return ('pass', None)

    cand_fp = best_row.get('model_fingerprint') or {}
    cand_strict = set(cand_fp.get('strict') or [])
    new_strict = set(strict)
    shared = sorted(new_strict & cand_strict)
    union = new_strict | cand_strict

    match = {
        'link': best_row.get('link'),
        'source_name': best_row.get('source_name') or 'other',
        'models': shared,
        'overlap_pct': int(round(best_sim * 100)),
        'n_matches': len(shared),
        'n_total': len(union),
    }

    if best_sim >= _DEDUP_BLOCK_THRESHOLD:
        return ('block', match)
    return ('flag', match)


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
    Telegraph has no caption-style length limit.

    DORMANT (since 2026-06-11 hold-and-wait change): the publish pipeline no
    longer calls this — an LLM outage now HOLDS articles instead of machine-
    translating them. Kept (with its tests) as a self-contained utility for
    possible reuse; ``GoogleTranslator``, ``_llm_translation_is_russian`` and
    ``GoogleTranslationError`` support it. Safe to delete in a follow-up if
    no revival is planned.
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
    """Return a Telegram hashtag for the source.

    Lookup order:
      1. ``SOURCE_HASHTAG_OVERRIDE`` (keyed on the normalised, ``www.``-
         stripped, lowercased netloc) — handles outliers where the default
         TLD-strip would emit the hoster (e.g. ``blogspot``) instead of
         the brand. Returns the override value as-is.
      2. Fallback: lift `#{parts[-2]}` from the netloc, stripping `www.`
         and the TLD. Example: `corporate.mattel.com` → `#mattel`,
         `autoevolution.com` → `#autoevolution`.
    """
    netloc = urlparse(source_url).netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    override = SOURCE_HASHTAG_OVERRIDE.get(netloc)
    if override is not None:
        return override
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
    'www.autoevolution.com':         'autoevolution',
    'autoevolution.com':             'autoevolution',
    'lamleygroup.com':               'lamley',
    'www.lamleygroup.com':           'lamley',
    'corporate.mattel.com':          'mattel',
    't-hunted.blogspot.com':         't-hunted',
    'orangetrackdiecast.com':        'orangetrack',
    'www.orangetrackdiecast.com':    'orangetrack',
}


# Override map for the channel-post hashtag (Decision 2 of t-hunted-pt-source
# tech-spec). Default in ``_source_hashtag`` lifts the TLD-stripped second-
# level label from the netloc (e.g. ``corporate.mattel.com`` → ``#mattel``),
# which works for outlets where the brand IS the second-level domain. The
# override map handles outliers where ``parts[-2]`` is the hoster instead of
# the brand — for ``t-hunted.blogspot.com`` the default would emit
# ``#blogspot`` (the platform, not the source). Keyed on the normalised
# (``www.``-stripped, lowercased) netloc; value is the full hashtag string
# WITH the leading ``#`` and dash-stripped at definition time.
#
# Why dash-stripped in the value: Telegram hashtag rules accept only
# ``[a-zA-Z0-9_]``; a literal ``#t-hunted`` would render as ``#t`` followed
# by ``-hunted`` plain text. Storing ``#thunted`` here keeps the value
# WYSIWYG against what subscribers see in the channel and avoids a runtime
# regex / replace.
SOURCE_HASHTAG_OVERRIDE = {
    't-hunted.blogspot.com': '#thunted',
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
    'orangetrack':   '\U0001F535',  # blue circle
    't-hunted':      '\U0001F7E4',  # brown circle
}
SOURCE_LABEL = {
    'autoevolution': 'autoevolution',
    'mattel':        'mattel',
    'lamley':        'lamley',
    'orangetrack':   'orangetrack',
    't-hunted':      'T-Hunted',
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
#   Step 1 (translate). Single-engine, hold-on-outage contract
#       (hold-and-wait, operator decision 2026-06-11):
#       * Primary + only engine: ``transcreate_via_claude(row)``. Returns
#         a dict with ``title`` (emoji prefix), ``subtitle``, ``paragraphs``
#         (and ``blocks`` for autoevolution). Hot Wheels-glossary + emoji
#         safety net are applied inside the Claude module. On success,
#         ``_maybe_record_recovery`` closes any active outage.
#       * Per-article failure (``ClaudeTranscreationError`` — refusal /
#         malformed JSON / 4xx): the LLM is up but choked on THIS article.
#         Re-raise so the slot loop strikes it (3 strikes → failed_articles).
#         Outage state is NOT advanced. No Google fallback.
#       * API-level outage (``ClaudeOutageError`` — 429 / 5xx / auth /
#         network): advance the 2-ping/2h notification state machine, then
#         re-raise WITHOUT publishing — the article is HELD in pending and
#         retried on the next slot/day until the LLM recovers. We never
#         ship a Google machine translation.
#   Step 2 (Telegraph). Reuse stored ``telegraph_url`` per Decision 9
#       idempotency; else ``telegraph_publisher.publish_article`` →
#       ``mark_telegraph_published`` (dedicated txn, survives Telegram
#       teaser failure). ``auto_marker`` is always False — the ``↳
#       автоперевод`` marker only ever flagged the removed Google path.
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
# from Step 1 (article HELD, nothing published) on an API-level outage so
# the slot loop can skip the slot without a strike; raises any other
# exception (per-article LLM failure, Telegraph / Telegram / repo failure)
# up to the caller so ``attempt_count`` can be bumped via
# ``pending_repo.increment_attempt``.
# ---------------------------------------------------------------------------


def _maybe_record_recovery():
    """Idempotent: if the outage state machine is currently active,
    clear it and emit the recovery admin ping. No-op (cheap read-only
    probe) when the bot is in steady-state healthy mode.

    Called from every successful Claude transcreation in
    ``_fallback_publish`` AND from a successful startup health probe
    in ``main()``. Without it, a transient outage in the past leaves the
    outage state machine stuck active (stale ``outage_started_at`` /
    ``ping_count``), so recovery pings never fire on the next success.
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
    """Auto-publish a pending row, translating via the LLM (Claude) only.
    On an API-level LLM outage the article is HELD (re-raises
    ``ClaudeOutageError`` before any publish side-effect) — no Google
    machine-translation fallback. Used by ``job()`` step (e) — the
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

    # Idempotency guard (publish-idempotency-fix, Decisions 1, 2, 3, 4, 8).
    # If this link is already in ``published_articles``, the pending row is a
    # zombie — short-circuit before any Telegraph/Telegram side-effect, ping
    # the admin, clean the zombie via ``skip_pending``, and return True so the
    # slot loop does not strike the row toward ``failed_articles``.
    existing = pending_repo.get_published(link)
    if existing is not None:
        logger.info(
            f"[idempotency-guard] {link} already in published_articles — "
            f"skipping re-publish of stale pending row"
        )
        ping_text = admin_alerts.alert_duplicate_publish_skipped(link)
        ping_ok = send_admin_notification(ping_text)
        if not ping_ok:
            logger.warning(
                f"admin ping for [idempotency-guard] skip of {link} failed "
                f"(Telegram down or credentials missing) — continuing cleanup"
            )
        try:
            pending_repo.skip_pending(link)
        except Exception as cleanup_err:
            logger.error(
                f"[idempotency-guard] skip_pending failed for {link}: "
                f"{cleanup_err!r} — leaving row in pending; next slot's guard "
                f"will retry cleanup"
            )
            send_admin_notification(
                admin_alerts.alert_zombie_cleanup_failed(
                    link, type(cleanup_err).__name__
                )
            )
        return True

    # Step 1: EN → RU translation. The LLM (Claude via
    # ``claude_transcreation``) is the ONLY translation path. When it is
    # unavailable we HOLD the article rather than auto-publish a low-quality
    # Google machine translation (operator decision 2026-06-11): the row
    # stays in ``pending_articles`` and the next slot/day retries the LLM
    # until it recovers. Recovery is auto-detected on the first success.
    #
    # The classifier inside ``claude_transcreation`` turns SDK exceptions
    # into ``ClaudeTranscreationError`` (per-article hiccup) or
    # ``ClaudeOutageError`` (API-level outage) — handled distinctly below.
    try:
        claude_result = transcreate_via_claude(row)
        ru_title = claude_result.get('title') or ''
        ru_subtitle = claude_result.get('subtitle') or ''
        ru_paragraphs = list(claude_result.get('paragraphs') or [])
        ru_blocks = claude_result.get('blocks')
        # Healthy LLM call → close any active outage and emit the recovery
        # ping. Idempotent: a cheap read-only probe when no outage is active.
        _maybe_record_recovery()
    except ClaudeTranscreationError as exc:
        # Per-article hiccup (refusal, malformed JSON, schema drift): the
        # LLM is UP, it just choked on THIS article. Re-raise so the slot
        # loop bumps attempt_count; after 3 strikes the row moves to
        # ``failed_articles`` so one bad article cannot wedge the queue
        # head forever. No Google fallback.
        logger.warning(
            f"[fallback] Claude per-article failure for {link}: "
            f"{type(exc).__name__}: {sanitize_error_message(exc)} "
            f"— slot strike (next slot retries this row)"
        )
        raise
    except ClaudeOutageError:
        # API-level outage (429 / 5xx / auth / network). Advance the
        # 2-ping/2h notification state machine so the operator is kept
        # informed, then HOLD: re-raise WITHOUT publishing. The row stays
        # in pending; the slot loop (``job()``) catches this, does NOT
        # strike, and the next slot retries the LLM. No Google fallback —
        # we wait for the LLM rather than ship a machine translation.
        logger.warning(
            f"[hold] LLM outage for {link} — holding article, will retry "
            f"when the LLM recovers (no Google fallback)."
        )
        try:
            # tz-aware UTC: outage_state rejects naive datetimes (the
            # 1h/2h thresholds must be unambiguous).
            event = outage_state.record_outage_event(
                datetime.now(timezone.utc),
            )
            for ping_text in event.get('pings_to_send') or []:
                try:
                    send_admin_notification(ping_text)
                except Exception as notify_err:
                    logger.error(
                        f"[hold] outage admin-ping send failed: "
                        f"{sanitize_error_message(notify_err)}"
                    )
        except Exception as state_err:
            # A state-machine update failure must not change the outcome —
            # we still re-raise to hold the article. Log and continue.
            logger.error(
                f"[hold] outage_state.record_outage_event failed for "
                f"{link}: {sanitize_error_message(state_err)}"
            )
        raise

    # Step 1b: strip author social-media plugs from RU output. Reaching
    # here means the LLM translated successfully (outage/per-article paths
    # already re-raised above). Wrapped in try/except: a regex bug must not
    # block publish (publish-something > publish-nothing). Per-fragment INFO
    # log so the operator can spot false positives via journalctl.
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
            # RU-side boilerplate filter (defense-in-depth, 2026-05-08).
            # Catches affiliate / promo lines that the EN-side parser
            # filter (orangetrack_source.py → boilerplate_filter) missed
            # because the EN variant didn't match a known pattern but the
            # RU translation matches an explicit RU pattern. Drop empty
            # paragraphs after the filter; if all RU paragraphs were
            # boilerplate the article still has title + RU subtitle, so
            # downstream rendering does not crash on empty list.
            ru_paragraphs = [
                p for p in ru_paragraphs if not is_boilerplate(p)
            ]
        ru_blocks = _strip_plugs_in_blocks(ru_blocks)
        if isinstance(ru_blocks, list):
            ru_blocks = [
                b for b in ru_blocks
                if not (
                    isinstance(b, dict)
                    and b.get('type') in ('paragraph', 'lead', 'heading', 'list_item')
                    and isinstance(b.get('text'), str)
                    and is_boilerplate(b['text'])
                )
            ]
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
                f"(via_review={via_review})"
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
        # ``auto_marker`` is always False: the ``↳ автоперевод`` marker
        # only ever flagged the legacy Google-fallback path, which has been
        # removed (LLM outages now HOLD instead of machine-translating). All
        # published articles are LLM-translated, production quality, no marker.
        telegraph_url = telegraph_publisher.publish_article(
            title=ru_title,
            paragraphs=ru_paragraphs,
            images=row.get('images') or [],
            source_url=link,
            subtitle=ru_subtitle,
            blocks=ru_blocks,
            auto_marker=False,
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
        # Mirror the persisted URL back onto the in-memory row so an in-slot
        # RETRY (``_publish_with_retries`` reuses this same dict) reuses this
        # Telegraph page instead of publishing a fresh ORPHAN one (B4).
        row['telegraph_url'] = telegraph_url
        row['telegraph_path'] = telegraph_path

        # Let Telegra.ph's edge cache settle before Telegram's IV worker
        # fetches the page (see ``TELEGRAPH_CACHE_WARMUP_SECONDS`` docstring).
        time.sleep(TELEGRAPH_CACHE_WARMUP_SECONDS)

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
    # Guard against an in-slot RETRY re-sending the teaser (B4):
    # ``_publish_with_retries`` reuses this same ``row`` dict, so if the teaser
    # already went out on a prior attempt and a LATER step failed (e.g.
    # ``move_to_published`` threw ``database is locked``), a retry must NOT post
    # a duplicate. The marker is in-memory only (per publish call); it is set
    # ONLY after a successful send, so a teaser that FAILED is still re-tried.
    if not row.get('_teaser_sent'):
        ok = send_telegraph_teaser(telegraph_url, link)
        if not ok:
            raise RuntimeError(
                f"send_telegraph_teaser returned False for {link}"
            )
        row['_teaser_sent'] = True

    # Step 5: atomic move (single repo txn).
    pending_repo.move_to_published(
        link, telegraph_url, telegraph_path, via_review=via_review,
    )

    # Step 5: best-effort preview cleanup (noop on None / missing file).
    _cleanup_preview_html(row.get('preview_html_path'))

    logger.info(
        f"[fallback] Published {link} via_review={via_review} url={telegraph_url}"
    )
    # Reaching here means the LLM translated and the article published.
    # An API-level outage never gets this far — it re-raises ClaudeOutageError
    # from Step 1 (hold) before any publish side-effect.

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
    # Match on hostname (not netloc): urlparse().hostname yields the real,
    # post-@ host, so a userinfo-attack URL like
    # ``http://autoevolution.com@169.254.169.254/…`` cannot route by its
    # pre-@ label. Downstream parsers additionally enforce exact-host
    # allowlists (defence in depth, incl. the .attacker.example suffix case).
    domain = (urlparse(link).hostname or '').lower()
    try:
        if 'orangetrackdiecast.com' in domain:
            # Pass-through: body fields already populated by
            # ``_fetch_orangetrack_entries`` (Decision 4 — full parse cycle
            # runs at fetch-time so the per-tick aggregator can see both
            # FEED_* and ART_* events). No second HTTP fetch.
            paragraphs = entry.get('paragraphs')
            if not paragraphs:
                logger.warning(
                    f"Orangetrack entry without pre-populated paragraphs: {link}"
                )
                return None
            return {
                'title': entry.get('title') or '',
                'subtitle': entry.get('subtitle') or '',
                'paragraphs': paragraphs,
                'images': entry.get('images') or [],
                'blocks': entry.get('blocks'),
            }
        if 'corporate.mattel.com' in domain:
            return fetch_mattel_article(link, notifier=send_admin_notification)
        if 'lamleygroup.com' in domain:
            return lamley_source.fetch_lamley_article(link, notifier=send_admin_notification)
        if 'autoevolution.com' in domain:
            return autoevolution_source.fetch_autoevolution_article(entry)
        if 'blogspot.com' in domain:
            return t_hunted_source.fetch_t_hunted_article(link, notifier=send_admin_notification)
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
                # Blogger "Labels:" → RSS <category> → feedparser ``tags``.
                # Carries the source's own brand/series taxonomy so the
                # relevance filter can reject by label (e.g. Matchbox), which
                # is more reliable than guessing the brand from the title.
                'labels': [
                    t.get('term') for t in (entry.get('tags') or [])
                    if t.get('term')
                ],
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


def _fetch_orangetrack_entries(notifier=None):
    """Fetch orangetrackdiecast.com feed and parse every entry's body in-place.

    Returns a list of plain dicts with body fields (title / subtitle /
    paragraphs / images / blocks) already populated — ``fetch_full_article``
    is then a pass-through (Decision 4). One ``OrangetrackPingAggregator``
    instance lives only inside this function's stack frame, collects feed-
    and article-level events via ``aggregator.add`` (passed as ``notifier``
    callback into ``fetch_orangetrack_article``), and emits a single
    aggregated admin-ping at end of the function via try/finally.

    SSRF guard runs at TWO call sites (Decision 13):
      1. Entry-level: ``_is_allowed_orangetrack_url(entry.link)`` BEFORE the
         entry enters parsing — closes content-spoofing via poisoned link.
      2. Fallback-HTTP: inside ``fetch_orangetrack_article`` before
         ``requests.get`` (defense-in-depth).
    """
    import socket
    from urllib.error import URLError

    aggregator = orangetrack_source.OrangetrackPingAggregator(
        instance_label=os.getenv('INSTANCE_LABEL'),
    )
    results = []
    feed_url = orangetrack_source._FEED_URL
    try:
        parsed = feedparser.parse(feed_url)
        status = parsed.get('status', 200) if hasattr(parsed, 'get') else getattr(parsed, 'status', 200)
        if status and status >= 400:
            aggregator.add(f'FEED_HTTP_{status}', feed_url)
            return []
        if getattr(parsed, 'bozo', 0):
            bozo_exc = getattr(parsed, 'bozo_exception', None)
            if isinstance(bozo_exc, (URLError, socket.timeout, ConnectionError, TimeoutError)):
                aggregator.add('FEED_TIMEOUT', feed_url)
            else:
                # Anything not URLError/socket-timeout/ConnectionError → treat
                # as XML parse. (feedparser surfaces SAX/Expat parse errors
                # via bozo_exception too.)
                aggregator.add('FEED_XML_PARSE', feed_url)
            # bozo doesn't always mean fatal — feedparser still parses what
            # it can. If we got entries despite bozo, fall through and
            # process them. If empty, return [].
            if not parsed.entries:
                return []

        for entry in parsed.entries:
            link = entry.get('link') if hasattr(entry, 'get') else getattr(entry, 'link', None)
            if not orangetrack_source._is_allowed_orangetrack_url(link):
                aggregator.add('ENTRY_HOST_REJECTED', link or '')
                continue
            try:
                article = orangetrack_source.fetch_orangetrack_article(
                    entry, notifier=aggregator.add,
                )
            except Exception as exc:
                logger.exception(
                    f"orangetrack article fetch raised for {link}: {exc}"
                )
                aggregator.add('ART_PARSE_EXCEPTION', link or '')
                continue
            if article is None:
                continue
            item = {
                'link': link,
                'title': entry.get('title') if hasattr(entry, 'get') else getattr(entry, 'title', '') or '',
                'published': (entry.get('published', '') if hasattr(entry, 'get') else getattr(entry, 'published', '')) or '',
                'summary': (entry.get('summary', '') if hasattr(entry, 'get') else getattr(entry, 'summary', '')) or '',
                'feed_url': feed_url,
                'source_name': 'orangetrack',
                'subtitle': article.get('subtitle') or '',
                'paragraphs': article.get('paragraphs') or [],
                'images': article.get('images') or [],
                'blocks': article.get('blocks'),
            }
            # Prefer parsed title only if non-empty (content:encoded usually
            # has no h1; RSS entry.title is the canonical title).
            parsed_title = article.get('title') or ''
            if parsed_title:
                item['title'] = parsed_title
            results.append(item)
    finally:
        if not aggregator.is_empty():
            aggregator.emit(send_admin_notification)
    return results


# Module-level registry the prep phase (Task 6) will iterate. Order matters
# only for log readability — RSS first (fastest, cheapest), orangetrack
# last (per-entry parse).
#
# Mattel отключён 2026-05-24: corporate.mattel.com переехал на Astro/Netlify,
# тело статьи рендерится client-side из JS-bundle и недоступно через любой
# JSON endpoint. Listing (``/api/news/articles``) выдаёт только handle/title/
# date/thumbnail. Полное восстановление потребует headless-браузера; за всю
# историю Mattel выдал ноль Hot Wheels статей, поэтому source отключён без
# замены. ``mattel_news_source.py`` и его тесты оставлены на месте — если
# Mattel вернёт publishable Hot Wheels контент, источник можно восстановить
# раскомментировав строку ниже и переписав парсер под новый API.
SOURCES = [
    _fetch_rss_entries,
    # _fetch_mattel_entries,  # disabled — see comment above
    _fetch_orangetrack_entries,
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


def _publish_with_retries(row, idx, n_slots):
    """Publish ``row`` via ``_fallback_publish``, retrying transient failures
    within the same slot (operator decision 2026-06-17).

    Returns ``(outcome, last_error)`` where ``outcome`` is:

    * ``'published'`` — published OK (possibly after a retry). ``last_error`` None.
    * ``'held'``      — ``ClaudeOutageError``: LLM unavailable, article HELD.
                        Caller must NOT strike. Not retried (see below).
    * ``'failed'``    — exhausted ``PUBLISH_RETRY_ATTEMPTS`` retries on a
                        transient publish error. ``last_error`` carries the
                        last exception; caller runs the 3-strike path.

    An LLM outage is deliberately NOT retried here: holding is the desired
    behaviour, and a retry would re-translate and waste tokens. Only
    publish-side failures (Telegra.ph / Telegram / repo errors) are retried.
    """
    link = row.get('link')
    attempts_left = PUBLISH_RETRY_ATTEMPTS
    while True:
        try:
            _fallback_publish(row, via_review=False)
            return 'published', None
        except ClaudeOutageError:
            return 'held', None
        except ClaudeTranscreationError as exc:
            # Per-article LLM problem (refusal / malformed JSON / schema
            # drift) — deterministic: an immediate retry re-translates to the
            # same bad result and burns tokens. Strike right away; the
            # cross-day 3-strike path still gives the row later attempts.
            return 'failed', exc
        except Exception as exc:
            # Transient publish-side failure (Telegra.ph / Telegram / repo
            # network timeout). Retry the same row in-slot before striking.
            if attempts_left <= 0:
                return 'failed', exc
            attempts_left -= 1
            logger.warning(
                f"[slot {idx}/{n_slots}] publish failed for {link}: "
                f"{sanitize_error_message(exc)} — retrying in "
                f"{PUBLISH_RETRY_DELAY_SECONDS // 60} min "
                f"(then {attempts_left} more)"
            )
            time.sleep(PUBLISH_RETRY_DELAY_SECONDS)


def job():
    """Daily cron tick — fetch + distributed-publish loop.

    Replaces the manual-review-workflow prep-phase tick (Decision 10) with
    the llm-transcreation-and-distributed-publishing flow (Decisions 2, 4,
    9, 14, 15 + tech-spec §Architecture How-it-works step 7).

    Pass layout:
      (a) crash-loop guard      — sleep until ``last_published + 40min``
                                  if the most recent publish is too fresh.
      (b) fetch + filter + insert — iterate ``SOURCES``, dedup, stage rows.
      (c) compute today's slots  — ``compute_fixed_slots(N, now_msk)``.
      (d) admin ping             — plan-of-day; always fires (heartbeat on
                                  quiet days); + backlog warning when N > 50.
      (e) distributed-publish    — sleep-until-slot, publish via
                                  ``_fallback_publish`` (LLM/Claude only).
                                  On ``ClaudeOutageError`` the article is
                                  HELD (nothing published) and the loop
                                  advances WITHOUT a strike so it retries the
                                  LLM next slot/day — no Google fallback.
                                  Other unexpected errors follow the standard
                                  3-strikes flow (``increment_attempt`` →
                                  ``move_to_failed``).
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
                    admin_alerts.alert_source_fetch_failed(fetcher_name, safe)
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

        # Reject bare "checklist" posts (title says checklist + body is
        # near-empty). Orangetrack publishes these often; subscribers
        # don't want a translated bullet list. Reviews that mention a
        # checklist in the title but have substantive body text pass.
        if _is_text_only_checklist(entry, article):
            logger.info(
                "Skipping checklist-only article (no editorial body): %s",
                link,
            )
            continue

        # --------------------------------------------------------------
        # Cross-source dedup gate (cross-source-dedup feature, Wave 2).
        # Position: post-fetch, post-checklist, pre-row-assembly per
        # Decision 14 (cheapest filters earlier; this is the most
        # expensive post-fetch gate — extract + N similarity calls +
        # 2 SQL reads).
        #
        # Three terminal branches:
        #   * block  — hard duplicate (≥50% overlap). Drop, write to
        #              processed_news (Decision 8), fire E015, continue.
        #   * flag   — soft duplicate (30-49% overlap). Pass through,
        #              fire E014 (per-pair 7-day rate-limited, AC5),
        #              keep fingerprint on the row.
        #   * pass   — no match (or empty fp). Fall through with fp.
        #
        # Wrapped in try/except Exception per Decision 12 / user-spec
        # AC9 — any sub-call failure (extractor regression, SQL fault,
        # malformed historical row) MUST NOT block publishing. On
        # exception the article publishes with model_fingerprint=NULL
        # and the operator gets one rate-limited [E016] ping per hour.
        # --------------------------------------------------------------
        fp = None
        new_source = entry.get('source_name') or _resolve_source_name(link)
        try:
            dedup_conn = pending_repo._connect()
            try:
                fp = model_extractor.extract_fingerprint(article)
                decision, match = _check_cross_source_dedup(
                    article, fp, dedup_conn, new_source,
                )

                if decision == 'block':
                    logger.info(
                        "Skipping cross-source duplicate %s; matched %s "
                        "(overlap %d%%)",
                        link, match['link'], match['overlap_pct'],
                    )
                    mark_processed(
                        link,
                        article.get('title') or entry.get('title') or '',
                        entry.get('published') or entry.get('pub_date') or '',
                    )
                    try:
                        send_admin_notification(
                            admin_alerts.alert_cross_source_blocked(
                                link, match['link'], match['overlap_pct'],
                            )
                        )
                    except Exception as notify_err:
                        logger.error(
                            "Failed to send E015 notification: %s", notify_err,
                        )
                    continue

                if decision == 'flag':
                    if not pending_repo.is_pair_rate_limited(
                        dedup_conn, link, match['link'],
                    ):
                        try:
                            send_admin_notification(
                                admin_alerts.alert_cross_source_dupe(
                                    new_link=link,
                                    existing_link=match['link'],
                                    new_source=new_source,
                                    existing_source=match['source_name'],
                                    overlap_pct=match['overlap_pct'],
                                    n_matches=match['n_matches'],
                                    n_total=match['n_total'],
                                    models=match['models'],
                                )
                            )
                        except Exception as notify_err:
                            logger.error(
                                "Failed to send E014 notification: %s",
                                notify_err,
                            )
                        pending_repo.mark_pair_pinged(
                            dedup_conn, link, match['link'],
                        )
                        dedup_conn.commit()
                # 'pass' → nothing to do; fp falls through into row.
            finally:
                dedup_conn.close()
        except Exception as exc:
            # Decision 12 / AC9 — broad handler is intentional. We don't
            # know upfront which sub-call can throw (regex compile bug,
            # repo SQL fault, exotic article shape, malformed historical
            # fingerprint JSON). The contract is "dedup never blocks
            # publishing" — a single broad handler enforces it.
            logger.exception("dedup gate failed, degraded mode active")
            try:
                rl_conn = pending_repo._connect()
                try:
                    if not pending_repo.is_dedup_degraded_rate_limited(
                        rl_conn,
                    ):
                        try:
                            send_admin_notification(
                                admin_alerts.alert_dedup_degraded(
                                    type(exc).__name__,
                                )
                            )
                        except Exception as notify_err:
                            logger.error(
                                "Failed to send E016 notification: %s",
                                notify_err,
                            )
                        pending_repo.mark_dedup_degraded_pinged(rl_conn)
                        rl_conn.commit()
                finally:
                    rl_conn.close()
            except Exception:
                # Rate-limit bookkeeping itself failed — log only. The
                # article still publishes (degraded-mode contract).
                logger.exception(
                    "dedup degraded-mode rate-limit bookkeeping failed",
                )
            fp = None

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
            'model_fingerprint': fp,
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
    # ``compute_fixed_slots`` returns (slots, carry_over) for the three
    # fixed daily times (10:00/15:00/19:30 МСК, operator pacing 2026-06-13).
    # N=0 → empty list, no admin ping (Step (d) guard), no loop (Step (e)
    # guard). The function already bounds the result to ≤3 slots, so the old
    # MAX_DAILY_POSTS trim is no longer needed.
    # ------------------------------------------------------------------
    now_msk = datetime.now(MSK_TZ)
    queue_size = pending_repo.count_pending()
    slots, carry_over = compute_fixed_slots(queue_size, now_msk)

    # ------------------------------------------------------------------
    # Step (d): admin ping with plan-of-day. Always sent — operator wants a
    # heartbeat that confirms the cron tick fired, even on quiet days when
    # there are no new articles and the queue is empty.
    # Quiet day: single-line «🟢 Бот сработал, новых статей нет.»
    # Busy day:  multi-line columnar «🟢 План на сегодня — …»
    # Backlog warning fires as a separate ping at queue_size > 50 (AC20).
    # ------------------------------------------------------------------
    if queue_size == 0 and inserted == 0:
        plan_msg = admin_alerts.alert_quiet_day()
    else:
        plan_msg = admin_alerts.alert_plan_of_day(
            inserted, queue_size, slots, carry_over
        )
    try:
        send_admin_notification(plan_msg)
    except Exception as notify_err:
        logger.error(
            f"Failed to send plan-of-day ping: "
            f"{sanitize_error_message(notify_err)}"
        )

    if queue_size > BACKLOG_WARNING_THRESHOLD:
        backlog_msg = admin_alerts.alert_backlog_warning(
            queue_size, BACKLOG_WARNING_THRESHOLD, carry_over
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
    #   * Publish via ``_fallback_publish`` (LLM/Claude only — no Google).
    #   * ClaudeOutageError → ``_fallback_publish`` HELD the article (nothing
    #     published); advance to the next slot WITHOUT a strike so it retries
    #     the LLM until recovery.
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
        outcome, err = _publish_with_retries(row, idx, len(slots))
        if outcome == 'published':
            published_count += 1
        elif outcome == 'held':
            # LLM outage — ``_fallback_publish`` HELD this article (nothing
            # was published) and advanced the operator-notification state
            # machine. Do NOT count a publish and do NOT strike the row: it
            # stays at the queue head and the next slot retries the LLM. We
            # keep iterating slots so a same-day recovery still publishes;
            # if the outage persists, the article simply carries over to the
            # next daily tick. No Google fallback.
            logger.warning(
                f"[slot {idx}/{len(slots)}] LLM outage — article held, "
                f"will retry on the next slot/day."
            )
        else:  # 'failed' — per-article problem, or transient error that
               # survived the in-slot retries (each retry logged above).
            safe = sanitize_error_message(err)
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

    # Channel-silence guard (2026-06-23): warn the operator if the channel has
    # gone quiet for DRY_SPELL_ALERT_DAYS+ days. Reads the same last-publish
    # timestamp as the crash-loop guard. A publish this tick resets the gap, so
    # this only fires during a real dry spell. Skipped when nothing was ever
    # published (fresh DB) so a brand-new bot doesn't false-alarm. Never raises
    # — monitoring must not break the tick.
    try:
        last_pub = _parse_published_at_utc(pending_repo.get_max_published_at())
        if last_pub is not None:
            silent_days = (datetime.now(timezone.utc) - last_pub).days
            if silent_days >= DRY_SPELL_ALERT_DAYS:
                logger.warning(
                    f"[dry-spell] channel silent for {silent_days} days — "
                    f"sending [E017] admin warning."
                )
                send_admin_notification(
                    admin_alerts.alert_channel_silent(silent_days)
                )
    except Exception as exc:
        logger.error(
            f"[dry-spell] channel-silence check failed: "
            f"{sanitize_error_message(exc)}"
        )

    _record_heartbeat()


#: Heartbeat marker path. Env-overridable (``HEARTBEAT_FILE``) so the Docker
#: container points it at the mounted ``/data`` volume (``HEARTBEAT_FILE=
#: /data/last_tick.ts``) — persistent across container restarts and readable by
#: ``watchdog.sh`` run via ``docker exec`` (B5). Default keeps the NL/systemd/
#: local ``~/.cache`` location.
_HEARTBEAT_PATH = (
    os.getenv("HEARTBEAT_FILE", "").strip()
    or os.path.expanduser("~/.cache/news_bot/last_tick.ts")
)


def _record_heartbeat(path=None):
    """Write a Unix-timestamp marker that proves ``job()`` completed.

    External watchdog (cron'd ``watchdog.sh``) reads this file's mtime
    and alerts the operator if it's older than the daily-tick interval.
    Designed for the alive-but-stuck class of incident (prod 2026-06-08
    feedparser hang) — ``Restart=on-failure`` already covers hard
    crashes, this catches the silent ones.

    Failure to write the heartbeat is logged but never raised — the
    heartbeat is for monitoring, not for correctness of the cron tick.
    """
    target = _HEARTBEAT_PATH if path is None else path
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write(f"{int(time.time())}\n")
    except OSError as exc:
        logger.warning(
            f"[heartbeat] failed to write {target!r}: "
            f"{sanitize_error_message(exc)}"
        )


def main():
    """Entry point — startup health checks + daily cron registration.

    Decision 14 startup health checks (BEFORE cron registration so any
    diagnostic ping reaches the operator before the first ``job()`` call):

      1. ``claude_transcreation.health_check()`` — non-raising bool probe.
         On ``False`` we ping the admin (E004). ``job()`` then attempts the
         LLM each slot and HOLDS articles until it recovers (no Google).
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
        # LLM unavailable at startup — just warn the operator. We do NOT
        # switch to Google (that path was removed); ``job()`` will attempt
        # the LLM on each slot and HOLD articles until it recovers.
        logger.warning(
            "[startup] claude_transcreation.health_check() returned False — "
            "articles will be held until the LLM recovers."
        )
        try:
            send_admin_notification(
                admin_alerts.alert_claude_probe_failed_at_startup()
            )
        except Exception as notify_err:
            logger.error(
                f"[startup] failed to send Claude-probe ping: "
                f"{sanitize_error_message(notify_err)}"
            )
    else:
        # Probe healthy — close any stale outage state from a prior run.
        # Without this, a transient outage that happened yesterday and was
        # never recovered (because nothing called record_recovery_event on
        # success) would leave the outage state machine stuck active.
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
            send_admin_notification(admin_alerts.alert_tz_mismatch(tz_env))
        except Exception as notify_err:
            logger.error(
                f"[startup] failed to send TZ-warning ping: "
                f"{sanitize_error_message(notify_err)}"
            )

    # Health check #3 (B2): prod DB integrity — catch the empty/ephemeral-DB
    # re-flood risk (a dropped ``DB_FILE=/data/news.db`` .env line). Prod only.
    db_warnings = _prod_db_guard()
    if db_warnings:
        logger.warning("[startup] prod DB guard: %s", "; ".join(db_warnings))
        try:
            send_admin_notification(admin_alerts.alert_prod_db_guard(db_warnings))
        except Exception as notify_err:
            logger.error(
                f"[startup] failed to send prod-DB-guard ping: "
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

_LOCK_FILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    ".news_bot.lock",
)
# Module-level reference keeps the file descriptor alive for the process
# lifetime. The kernel releases the flock on fd close (clean exit, SIGTERM,
# SIGKILL, OOM-kill — all paths), so no explicit cleanup is needed.
_singleton_lock_fd = None


def _acquire_singleton_lock(lock_path=_LOCK_FILE_PATH):
    """Hold an exclusive flock to refuse a second concurrent start.

    Prod incident 2026-06-08: a manual restart sequence (nohup, then
    setsid, then tmux — each launched silently because pgrep regex was
    wrong and operator believed no process was running) produced four
    parallel prod bots fetching the same RSS, racing on inserts and
    publishes. Kernel-held ``flock`` makes a second start fail-fast
    with a clear log line instead of silently doubling up.

    Per-deploy isolation: lock file is colocated with ``news_bot.py``,
    so ``/home/hwbot/bot/.news_bot.lock`` and
    ``/home/hwbot/bot_test/.news_bot.lock`` are distinct files — prod
    and test never conflict.

    On lock-conflict: log ERROR and exit with code 1. Operator sees the
    log line; no admin ping (the existing instance is already alive and
    will fire its own ping at the next scheduled tick).
    """
    global _singleton_lock_fd
    _singleton_lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(_singleton_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        logger.error(
            f"[singleton-lock] cannot acquire {lock_path!r}: another news_bot "
            f"instance is already running in this deploy directory. Refusing "
            f"to start to prevent duplicate publishes ({type(exc).__name__})."
        )
        _singleton_lock_fd.close()
        _singleton_lock_fd = None
        sys.exit(1)
    _singleton_lock_fd.write(f"{os.getpid()}\n")
    _singleton_lock_fd.flush()
    logger.info(f"[singleton-lock] acquired {lock_path!r}")


if __name__ == "__main__":
    _acquire_singleton_lock()
    main()
