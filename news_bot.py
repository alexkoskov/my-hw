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
import secrets
import socket
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional
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
from telegram.error import Conflict, TelegramError

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
import _llm_common  # noqa: F401 — _blocks_if_aligned reads its patched-type tuple
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

# Feature toggle for the tiered series/theme pair-rule layer of the
# cross-source dedup gate (dedup-model-series, Task 4 / user-spec AC6).
# Import-time constant read once from env, same pattern as ``DB_FILE``/``TZ``.
# CRITICAL: the env-var name is IDENTICAL to the constant name
# (``DEDUP_SERIES_ENABLED``) — the deploy runbook / operator set exactly this
# key in the prod ``.env``; a const↔env name drift would make a "dark" deploy
# silently no-op. Default ON: unset OR blank → enabled; only the explicit
# off-words ``0/false/no/off`` (case-insensitive) disable the pair rule, after
# which the gate runs only the legacy set-overlap backstop. The gate reads the
# MODULE attribute ``news_bot.DEDUP_SERIES_ENABLED`` (a bare name inside this
# module resolves to the module global at call time) so tests can monkeypatch
# it and an operator can flip it via env + restart without touching code.
DEDUP_SERIES_ENABLED = os.getenv(
    "DEDUP_SERIES_ENABLED", "1"
).strip().lower() not in ("0", "false", "no", "off")

# Feature toggle for the inline review keyboard on the [E014] soft-dupe
# admin ping (dedup-review-buttons, tech-spec Decision 6). Same const↔env
# name contract as ``DEDUP_SERIES_ENABLED`` above (a const↔env drift would
# make the deploy silently no-op), and the same module-attribute read
# pattern — the send site reads the bare name so tests can monkeypatch
# ``news_bot.REVIEW_BUTTONS_ENABLED`` and the operator can flip it via
# env + restart. CRITICAL, deliberately INVERTED default vs
# DEDUP_SERIES_ENABLED: this flag is OFF unless the env var is an explicit
# on-word (``1/true/yes/on``, case-insensitive); unset / blank / anything
# else → disabled. Prod-only opt-in: buttons appear only where the
# callback listener actually runs.
REVIEW_BUTTONS_ENABLED = os.getenv(
    "REVIEW_BUTTONS_ENABLED", ""
).strip().lower() in ("1", "true", "yes", "on")
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

#: Hold cap — how many CONSECUTIVE slot-loop holds a single row may cost the
#: queue before it steps aside (``pending_repo.defer_publish``, i.e.
#: ``publish_after``) and lets the article behind it publish.
#:
#: Why a cap is needed at all: a hold never strikes (that is the point of
#: hold-and-wait) and ``job()`` re-reads ``list_pending()[0]`` at every slot, so
#: a row that always fails blocks the whole channel — and does it QUIETLY,
#: because ``outage_state`` stops pinging once ``ping_count >= 3``. The 402
#: classification added 2026-08-04 made that reachable: OpenRouter's 402 can be
#: per-REQUEST ("requires more credits, or fewer max_tokens"), so a long
#: round-up can fail forever while shorter articles behind it would succeed.
#:
#: 6 = two full days of the three fixed slots. Chosen so a GENUINE LLM outage
#: never trips it — the 2-ping protocol declares a sustained outage after 2 h
#: (well inside one day), so anything reaching six holds across two days is
#: article-specific, not global. Lower values start deferring healthy articles
#: during ordinary outages and spread the queue across days for no reason.
HOLD_CAP = 6

#: How long a row that hit ``HOLD_CAP`` stays out of the queue. One day: long
#: enough for the operator to top up a balance or for a gateway to recover,
#: short enough that a false positive costs one day of that article's lead.
#: The row returns to the head afterwards (carry-over is drained oldest-first),
#: so it retries once per window — and yields again on its FIRST hold, because
#: ``defer_publish`` deliberately does not reset the counter.
HOLD_DEFER_HOURS = 24

#: Minimum gap between two [E038] pings, GLOBALLY (not per article). In a
#: sustained stall rows cross ``HOLD_CAP`` one after another; a ping each would
#: recreate the noise the outage machine's ``ping_count >= 3`` cutoff exists to
#: prevent. 6 h bounds it to 4/day at worst. Nothing is hidden: the daily
#: [E008]/[E009] «Отложено (уступили очередь): N» line carries the running
#: total, so [E038] only has to deliver the first representative cause.
HOLD_CAP_PING_WINDOW_HOURS = 6

#: End-of-tick PUBLISH RECAP (companion to the E008/E009 intake funnel). Cap on
#: distinct (link, reason) failure entries collected for the [E034] recap ping —
#: keeps the ping compact; admin_alerts also re-caps defensively when rendering.
#: Derived from ``admin_alerts.RECAP_MAX_FAILURES`` (the single source of truth)
#: so the collect-side cap here and the render-side cap there can never drift.
PUBLISH_RECAP_MAX_FAILURES = admin_alerts.RECAP_MAX_FAILURES

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
    # Proxy URLs carry credentials inline (``scheme://user:pass@host``) and
    # match none of the key regexes — the one secret shape the vocabulary
    # missed. Both cases: libraries read the lowercase form, operators
    # typically set the uppercase one. Defence in depth alongside
    # ``_PROXY_CRED_RE``: this replaces the exact configured value, the regex
    # catches any proxy URL including ones we never configured.
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy',
)


#: Hard cap on a sanitised error string. Sibling of ``admin_alerts``'
#: ``_RECAP_REASON_MAXLEN`` / ``_PROMO_TITLE_MAXLEN`` (both 200) — larger
#: because this text is the operator's primary diagnostic, not a chat line,
#: and a full LLM-gateway error body measures ~220 chars.
#:
#: The bound is not cosmetic. An SDK error message is the upstream response
#: body VERBATIM when it isn't JSON (``openai/_base_client.py``: ``err_msg =
#: err_text or ...``), so a captive portal or an intercepting proxy answering
#: 4xx with a multi-KB HTML page lands whole in the journal. On the hold path
#: that repeats every slot for as long as the outage lasts, and the journal is
#: capped at 10 MB × 3 (docker-compose.yml) — the flood would evict the very
#: diagnostics it is made of. Relevant since the egress runs through a VPN
#: gateway from an RU host.
_ERROR_MESSAGE_MAXLEN = 500


def sanitize_error_message(exc):
    """Return str(exc) with every known env-secret value replaced by
    ``[REDACTED]``, truncated to ``_ERROR_MESSAGE_MAXLEN``.

    Decision 11 of manual-review-workflow tech-spec:
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

    # Truncate LAST, after redaction: cutting first could split a secret and
    # leave a prefix of it in the tail that the replace loop would then miss.
    if len(message) > _ERROR_MESSAGE_MAXLEN:
        return message[:_ERROR_MESSAGE_MAXLEN] + '…'
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

# Proxy credentials embedded in a URL: ``scheme://user:pass@host``. The one
# secret shape none of the five key patterns above can match, and the one the
# prod topology makes plausible — egress runs through a non-RU VPN gateway, and
# ``_llm_common._ACCOUNT_LEVEL_STATUS_CODES`` now classifies 407 (proxy auth)
# explicitly, i.e. a proxy failure is on a path that reaches the journal.
#
# Only the userinfo is replaced; the host:port survives because that half is
# the diagnostic. Requires BOTH a scheme and a ``user:pass@``, so an ordinary
# article URL — including one with a port or a colon in the path — is untouched
# (the ``[^/\s:@]`` classes cannot cross a ``/``, so a path colon never
# qualifies). Order-independent of the key patterns: no ``sk-…``/``AIza…``
# shape can sit inside a userinfo field without a ``/`` or ``@`` boundary.
_PROXY_CRED_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+:[^/\s:@]+@"
)


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
        text = _PROXY_CRED_RE.sub(r"\1***:***@", text)
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


def send_admin_notification(
    message, *, max_attempts=ADMIN_NOTIFICATION_MAX_ATTEMPTS, reply_markup=None,
):
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

    ``reply_markup`` (keyword-only, dedup-review-buttons Task 2) is an
    optional ready-made telegram keyboard object forwarded verbatim to
    ``bot.send_message`` — NOT text, so it deliberately bypasses
    ``_redact_text``. Default ``None`` keeps the call identical to the
    pre-keyboard behaviour.
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
            reply_markup=reply_markup,
        )

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            asyncio.run(_send())
            # Full logging: log the ENTIRE (already-redacted) alert text, not a
            # 50-char prefix — so alert content (e.g. the flagged article link +
            # match in an [E014]/[E015] dedup ping) is diagnosable from the logs
            # alone. safe_message has already passed through _redact_text.
            logging.info("Admin notification sent: %s", safe_message)
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


def _is_admin_press(from_user_id):
    """Fail-closed admin check — the single source of truth for "is this
    press from the operator?" (tech-spec Decision 5).

    Used by BOTH ``resolve_dedup_callback`` (its auth gate) and the
    listener's ``_handle_review_update`` (security review round 1,
    SEC-T5-1: the handler must gate BEFORE any DB read so arbitrary
    users can't trigger token lookups). A non-numeric
    ``TELEGRAM_ADMIN_ID`` (default ``@sunny413x``) or a non-int
    ``from_user_id`` means nobody is authorized. Module attribute read
    at call time so tests can patch it.
    """
    try:
        admin_id = int(TELEGRAM_ADMIN_ID)
    except (TypeError, ValueError):
        # Non-numeric admin id (default '@sunny413x') — nobody matches.
        return False
    try:
        return int(from_user_id) == admin_id
    except (TypeError, ValueError):
        return False


def resolve_dedup_callback(action, token, from_user_id):
    """Decide the outcome of a dedup-review button press (pure, no I/O).

    The "brain" behind the ``[E014]`` review keyboard: Task 5's listener
    parses ``callback_data`` (grammar ``dd:<c|k>:<token>``), maps the
    letter to a full word (``c → 'cancel'``, ``k → 'keep'``), and calls
    this function with the already-parsed fields. This function does NO
    Telegram I/O — only config reads, ``pending_repo`` calls, and string
    returns; the listener applies the result via ``edit_message_text`` +
    ``answer_callback_query`` and owns the operator-decision INFO log.

    Return contract (fixed with Task 5):
        ``(status_text, answer_text)``
        * terminal outcome — ``status_text`` is the line the listener
          appends to the alert (``edit_message_text(original + "\\n\\n" +
          status_text)``); ``answer_text`` is the short callback answer
          (same string here);
        * ignored press (non-admin, or non-numeric ``TELEGRAM_ADMIN_ID``)
          — ``(None, "")``: the listener must NOT edit the message and
          answers with an empty text.

    Order of checks (security reviewer, wave 1): the admin gate runs
    FIRST, before any token lookup — the repo token helpers accept any
    string, so this function is the sole auth gate. Fail-closed
    (Decision 5): a non-numeric ``TELEGRAM_ADMIN_ID`` (e.g. the default
    ``@sunny413x``) means nobody is authorized. ``TELEGRAM_ADMIN_ID`` is
    read from the module at call time so tests can patch it.

    Terminal outcomes (Decisions 2/9/10):
        * unknown token → «⚠️ Кнопка устарела» (bot restarted / token
          already consumed) — nothing to delete, no DB writes;
        * ``keep`` + link still pending → ``clear_deferral(link)`` lifts the
          soft flag's timed deferral → «👍 Оставлено — выйдет в ближайший
          слот». (Before 2026-08-11 this branch wrote nothing and the
          article sat out the full 24 h regardless — the alert text has
          always promised otherwise.)
        * ``keep`` + link already published / gone → «⚠️ Уже опубликовано»
          / «⚠️ Статья уже недоступна», no writes;
        * ``cancel`` + link still pending → ``skip_pending(link)`` (the
          only DB write; idempotent, never touches published_articles) →
          «✅ Отменено оператором»;
        * ``cancel`` + link already published → «⚠️ Уже опубликовано,
          отменить нельзя» (channel post untouched);
        * ``cancel`` + link nowhere → «⚠️ Статья уже недоступна».
    The token is deleted exactly on terminal outcomes (keep + the three
    cancel branches) — never on an ignored press, and an unknown action
    (listener grammar should make this impossible) is a safe no-op that
    keeps the token. Idempotent: a second press of a consumed button
    resolves to the stale branch, never raises.
    """
    # 1. Admin gate — FIRST, before any token lookup (fail-closed).
    # Shared with the listener's pre-DB gate — see _is_admin_press.
    if not _is_admin_press(from_user_id):
        return (None, "")

    # 2. Token resolve + KIND check (audit SEC-CG-2). A token minted by
    # the [E036] hold keyboard must never be redeemed here: 'keep' would
    # consume it with no state change and orphan the held article
    # forever (still frozen, no live button, no re-mint path). Treated as
    # a stale button and — critically — the token is NOT consumed, so the
    # real [E036] button still works.
    entry = pending_repo.get_review_token(token)
    if entry is None or entry[0] != pending_repo.REVIEW_TOKEN_KIND_DEDUP:
        return ("⚠️ Кнопка устарела", "⚠️ Кнопка устарела")
    link = entry[1]

    # 3/4. Action branches.
    if action == 'keep':
        # The alert promises «выпустит её в ближайший слот», so the press has
        # to LIFT the soft flag's 24 h deferral. Until 2026-08-11 this branch
        # only set a status string: the button was indistinguishable from not
        # pressing it at all, and the operator had no way to see that. Same
        # queue-state checks as 'cancel' below — the row can leave between the
        # alert and the press, and reporting a release that did not happen is
        # the failure this whole fix is about.
        released = pending_repo.clear_deferral(link)
        row = pending_repo.get_pending(link)
        if row is not None and row.get('hold_reason'):
            # A row can carry BOTH marks: `publish_after` from the dedup soft
            # flag and `hold_reason` from a content gate are written side by
            # side in the same insert (see the row dict in `job()`), and the
            # flag branch does not consult `hold_markers`. Lifting the
            # deferral does not unpark it — `list_pending` still demands
            # `hold_reason IS NULL` — so promising the nearest slot here would
            # be this fix's own bug, one release later.
            status = "👍 Оставлено — но статья ещё на утверждении [E036]"
        elif released or row is not None:
            # Either the deferral was lifted, or the row is queued and was
            # never deferred (the soft flag defers only below the auto-block
            # threshold). Both mean the same thing to the operator. Writing
            # first and reading after also closes the gap a check-then-write
            # would leave: `clear_deferral` is an UPDATE, so a row that left
            # the queue meanwhile is a no-op, never a resurrection.
            status = "👍 Оставлено — выйдет в ближайший слот"
        elif pending_repo.get_published(link) is not None:
            status = "⚠️ Уже опубликовано"
        else:
            status = "⚠️ Статья уже недоступна"
    elif action == 'cancel':
        if pending_repo.get_pending(link) is not None:
            pending_repo.skip_pending(link)
            # Slot-boundary race (security review round 1): between the
            # get_pending check above and skip_pending the slot loop may
            # have published the row — skip_pending then no-ops silently
            # (it never touches published_articles). Re-read state after
            # the skip rather than trying to be transactional: a
            # published row at this point means the publish won, so
            # answer the honest «уже опубликовано», not «отменено».
            if pending_repo.get_published(link) is not None:
                status = "⚠️ Уже опубликовано, отменить нельзя"
            else:
                status = "✅ Отменено оператором"
        elif pending_repo.get_published(link) is not None:
            status = "⚠️ Уже опубликовано, отменить нельзя"
        else:
            status = "⚠️ Статья уже недоступна"
    else:
        # Unknown action — listener grammar filters these out; don't burn
        # a still-valid token on a malformed callback.
        return ("⚠️ Кнопка устарела", "⚠️ Кнопка устарела")

    # 5. Terminal outcome — consume the token (idempotent delete).
    pending_repo.delete_review_token(token)
    return (status, status)


def resolve_hold_callback(action, token, from_user_id):
    """Decide the outcome of a content-gate HOLD button press ([E036]).

    Pure in the same sense as ``resolve_dedup_callback``: no Telegram I/O,
    only config reads, ``pending_repo`` calls and string returns; the
    listener applies the result (edit + answer) and owns the decision log.
    Same ``(status_text, answer_text)`` contract, same ``(None, "")``
    signal for an ignored press.

    Auth: the SAME fail-closed numeric-admin gate (``_is_admin_press``),
    checked FIRST, before any token lookup — the repo token helpers accept
    any string, so this is the sole auth gate on this path too.

    Terminal outcomes:
        * unknown token → «⚠️ Кнопка устарела» (bot restarted / already
          consumed) — no DB writes;
        * ``approve`` + row still held → ``clear_hold(link)`` → «✅ Одобрено
          — выйдет в ближайший слот»: the row is already staged, so
          clearing ``hold_reason`` puts it straight into the publishable
          queue;
        * ``reject`` + row still pending → ``skip_pending(link)`` (DELETE +
          processed_news pin, never touches published_articles) →
          «🚫 Не будет опубликовано»;
        * either action with the row gone (already approved and published,
          rejected earlier, evicted) → «⚠️ Статья уже недоступна».

    Doing NOTHING is a supported outcome and needs no branch here: an
    unpressed hold simply stays out of ``list_pending`` forever. That is
    the operator's explicit rule — silence means do not publish; there is
    no timeout, no auto-publish and no auto-drop anywhere in this path.

    The token is deleted exactly on terminal outcomes, so a second press
    resolves to the stale branch instead of re-reporting a decision.
    """
    # 1. Admin gate — FIRST, before any token lookup (fail-closed).
    if not _is_admin_press(from_user_id):
        return (None, "")

    # 2. Token resolve + KIND check (audit SEC-CG-2). A token minted by
    # the [E014] dedup keyboard must never be redeemed here: 'reject'
    # would silently skip_pending an article that was never held. Stale
    # answer, and the token is NOT consumed so its own buttons still work.
    entry = pending_repo.get_review_token(token)
    if entry is None or entry[0] != pending_repo.REVIEW_TOKEN_KIND_HOLD:
        return ("⚠️ Кнопка устарела", "⚠️ Кнопка устарела")
    link = entry[1]

    # 3. Action branches.
    if action == 'approve':
        # clear_hold's own rowcount is the source of truth: it returns
        # False both when the row is gone and when it is no longer held,
        # so a double press can never report a second fresh approval.
        if pending_repo.clear_hold(link):
            status = "✅ Одобрено — выйдет в ближайший слот"
        else:
            status = "⚠️ Статья уже недоступна"
    elif action == 'reject':
        if pending_repo.get_pending(link) is not None:
            pending_repo.skip_pending(link)
            status = "🚫 Не будет опубликовано"
        else:
            status = "⚠️ Статья уже недоступна"
    else:
        # Unknown action — listener grammar filters these out; don't burn
        # a still-valid token on a malformed callback.
        return ("⚠️ Кнопка устарела", "⚠️ Кнопка устарела")

    # 4. Terminal outcome — consume the token (idempotent delete).
    pending_repo.delete_review_token(token)
    return (status, status)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background review listener (dedup-review-buttons Task 5) — the bot's first
# INBOUND Telegram path. A daemon thread long-polls getUpdates for
# callback_query presses under the [E014] keyboard, feeds them into the pure
# resolve_dedup_callback above, and applies the outcome (edit + answer).
# Isolation contract (tech-spec Decision 1 / Risk "listener thread crashes"):
# nothing that happens in this thread may ever affect the publish loop.
# ---------------------------------------------------------------------------

#: Long-poll timeout (seconds) passed to getUpdates — the Telegram server
#: holds the request open up to this long, so the loop is idle-cheap.
REVIEW_LISTENER_POLL_TIMEOUT_SECONDS = 30
#: Pause after a transient error (network blip, TelegramError, DB fault)
#: before re-polling — prevents a busy-loop on a repeating failure.
REVIEW_LISTENER_ERROR_BACKOFF_SECONDS = 5
#: Longer pause after a 409 Conflict (another getUpdates consumer on the
#: shared bot token) — the condition needs operator action, not fast retry.
REVIEW_LISTENER_CONFLICT_BACKOFF_SECONDS = 60

#: Exact callback_data grammar map: ``dd:<letter>:<token>``. The letters are
#: the wire format (Telegram caps callback_data at 64 bytes); the values are
#: the full-word actions ``resolve_dedup_callback`` expects (Task 4 contract
#: — it takes ``'cancel'``/``'keep'``, never the bare letters).
_REVIEW_CALLBACK_ACTIONS = {'c': 'cancel', 'k': 'keep'}

#: Content-gate HOLD grammar ([E036]): ``hd:<a|r>:<token>``.
#: ``a → 'approve'`` (publish it), ``r → 'reject'`` (drop it).
_HOLD_CALLBACK_ACTIONS = {'a': 'approve', 'r': 'reject'}

#: Every callback grammar the listener understands, keyed by wire prefix.
#: Adding a keyboard means adding a prefix here plus a resolver below.
#: The prefixes MUST stay distinct — a shared one would route a press into
#: the wrong resolver, which would answer «устарела» and lose the decision.
_REVIEW_CALLBACK_GRAMMARS = {
    'dd': _REVIEW_CALLBACK_ACTIONS,
    'hd': _HOLD_CALLBACK_ACTIONS,
}

#: Action words that belong to the HOLD grammar. The parser emits FULL
#: WORDS that are unique across grammars, so ``_handle_review_update``
#: dispatches on the word alone — no second copy of the prefix, and the
#: parser's ``(action, token)`` return shape stays unchanged (keeping the
#: [E014] round-trip contract intact). Derived from the grammar map so the
#: two can never drift.
#:
#: Dispatch names the resolver FUNCTIONS directly rather than storing them
#: in a table: a table captures the function objects at import time, which
#: would silently defeat ``patch('news_bot.resolve_dedup_callback')`` —
#: the same read-the-module-attribute-at-call-time convention the rest of
#: this file follows.
_HOLD_ACTION_WORDS = frozenset(_HOLD_CALLBACK_ACTIONS.values())

#: Telegram hard-caps callback_data at 64 BYTES; anything longer never came
#: from our keyboard, so the parser rejects it outright (wave-1 security
#: note: accept ONLY the exact grammar, ignore everything else silently).
#: Compared against the UTF-8 byte length, not the code-point count
#: (audit CA-5): a ≤64-char multibyte payload can exceed 64 bytes.
_REVIEW_CALLBACK_DATA_MAX_BYTES = 64


def _parse_review_callback_data(data):
    """Parse ``callback_data`` against the exact review-button grammars:
    ``dd:<c|k>:<token>`` ([E014] dedup) and ``hd:<a|r>:<token>`` ([E036]
    content-gate hold).

    Returns ``(action_word, token)`` — action already mapped to the full
    word (``'cancel'``/``'keep'``/``'approve'``/``'reject'``, unique across
    grammars so the caller can dispatch on it alone) — or ``None`` for
    anything that is not EXACTLY three ``:``-separated fields with a known
    prefix, a known single-letter action FOR THAT PREFIX, and a non-empty
    token. Defensive by design (wave-1 security review): garbage, foreign
    prefixes, cross-grammar letters (``dd:a:…``), full-word actions on the
    wire, empty tokens and oversized payloads are all silently rejected —
    the caller just advances the offset.
    """
    if not isinstance(data, str):
        return None
    if len(data.encode('utf-8')) > _REVIEW_CALLBACK_DATA_MAX_BYTES:
        return None
    parts = data.split(':')
    if len(parts) != 3:
        return None
    prefix, letter, token = parts
    actions = _REVIEW_CALLBACK_GRAMMARS.get(prefix)
    if actions is None or letter not in actions or not token:
        return None
    return (actions[letter], token)


async def _review_get_updates(offset):
    """One long-poll cycle: fresh ``Bot`` + fresh event loop per call.

    Matches ``send_admin_notification`` exactly (Bot constructed INSIDE
    the coroutine, one ``asyncio.run`` per Telegram call). This is not
    style pedantry: ``HTTPXRequest`` builds its ``httpx.AsyncClient`` in
    ``__init__`` and pools keep-alive connections, and a pooled
    connection is bound to the event loop that opened it — reusing one
    ``Bot`` across successive ``asyncio.run`` loops makes every second
    poll die with "Event loop is closed". A fresh Bot per call keeps
    each connection's lifetime inside its own loop.
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return await bot.get_updates(
        offset=offset,
        timeout=REVIEW_LISTENER_POLL_TIMEOUT_SECONDS,
        allowed_updates=['callback_query'],
    )


async def _review_edit_message(chat_id, message_id, text):
    """Apply a terminal outcome to the alert: append status, drop keyboard.

    Fresh Bot per call — same cross-event-loop rationale as
    ``_review_get_updates``. The text goes through ``_redact_text``
    (audit SEC-A8-3, defense-in-depth parity with the send path's
    Decision 12 belt-and-suspenders): today's inputs are Telegram's own
    copy of an already-redacted alert plus a fixed status string, but
    the edit path is an outgoing-message sink and must not silently
    lose that property if a future caller feeds it dynamic text.
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=_redact_text(text),
        reply_markup=None,
    )


async def _review_answer_callback(callback_query_id, text):
    """Answer a button press (clears the client-side spinner).

    Fresh Bot per call — same cross-event-loop rationale as
    ``_review_get_updates``.
    """
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.answer_callback_query(callback_query_id, text=text)


def _handle_review_update(update):
    """Process ONE getUpdates item: parse → resolve → edit/answer.

    Synchronous by design (the testability seam): all Telegram I/O goes
    through per-call ``asyncio.run`` on the ``_review_*`` coroutine
    helpers above, matching the project's outgoing style. May raise —
    the caller's per-update try/except owns survival (task-4 security
    note: a DB error from pending_repo must never kill the listener
    thread).

    Outcome application (fixed contract with Task 4):
      * terminal (``status_text`` is a string) → ``edit_message_text``
        with the status line appended to the original alert text and the
        keyboard removed (``reply_markup=None``), then
        ``answer_callback_query`` with ``answer_text``; the operator
        decision is logged at INFO (action + link + status);
      * ignored press (``status_text is None``) → answer with empty text
        only, NO edit, no log noise.
    """
    callback_query = getattr(update, 'callback_query', None)
    if callback_query is None:
        return  # allowed_updates should prevent this; be defensive anyway
    parsed = _parse_review_callback_data(getattr(callback_query, 'data', None))
    if parsed is None:
        return  # not our grammar — silently ignore (offset advances upstream)
    action, token = parsed
    from_user = getattr(callback_query, 'from_user', None)
    from_user_id = getattr(from_user, 'id', None)

    if not _is_admin_press(from_user_id):
        # Non-admin press (or fail-closed config): acknowledge the spinner
        # with an empty answer, change nothing, edit nothing — and do it
        # BEFORE any DB read (SEC-T5-1: arbitrary users must not be able
        # to trigger token lookups).
        asyncio.run(_review_answer_callback(callback_query.id, ""))
        return

    # Admin press — grab the link for the decision log BEFORE resolve
    # consumes the token (single source of truth — resolve deletes it on
    # a terminal outcome).
    link = pending_repo.get_review_token_link(token)

    # Dispatch on the action word — the parser guarantees it came from a
    # known grammar and the words are unique across grammars. Anything
    # else lands in the dedup resolver, whose unknown-action branch is
    # already a safe «устарела» no-op that keeps the token.
    if action in _HOLD_ACTION_WORDS:
        status_text, answer_text = resolve_hold_callback(
            action, token, from_user_id)
    else:
        status_text, answer_text = resolve_dedup_callback(
            action, token, from_user_id)

    if status_text is None:
        # resolve's own gate declined (belt-and-braces — ours already
        # passed): answer-only, no edit.
        asyncio.run(_review_answer_callback(callback_query.id, ""))
        return

    # user-spec: every operator decision lands in the log — action + article
    # link + final status. No token value here (log hygiene). Logged BEFORE
    # the Telegram edit/answer calls (audit CA-2): the state change already
    # happened inside resolve, so a transient Telegram failure below must
    # not cost the audit line.
    logger.info(
        "[review] operator decision: action=%s link=%s status=%s",
        action, link, status_text,
    )
    message = getattr(callback_query, 'message', None)
    if message is not None:
        original_text = getattr(message, 'text', None) or ''
        asyncio.run(_review_edit_message(
            message.chat_id,
            message.message_id,
            original_text + "\n\n" + status_text,
        ))
    asyncio.run(_review_answer_callback(callback_query.id, answer_text))


def _review_listener_sleep(stop_event, seconds):
    """Backoff that stays responsive to the test stop-seam."""
    if stop_event is not None:
        stop_event.wait(seconds)
    else:
        time.sleep(seconds)


def _run_review_listener(stop_event=None):
    """Daemon-thread target: long-poll getUpdates for [E014] button presses.

    Own ``Bot`` instances + own offset; each Telegram call runs a fresh
    ``Bot`` through a fresh ``asyncio.run`` (same per-call event-loop
    style as ``send_admin_notification`` — see ``_review_get_updates``
    for why the Bot must not outlive its loop; and no
    ``telegram.ext.Application``, which wants signal handlers the main
    thread owns).

    Resilience contract: NO exception ever escapes this function. Each
    update is handled inside its own try/except (one poisoned update is
    logged, acked via offset and skipped), the poll cycle has its own
    try/except with a short backoff (5s; 60s on 409 Conflict — that means
    a second getUpdates consumer on the shared bot token, which needs the
    operator to enforce the single-listener rule, not fast retries), and
    a final belt-and-braces handler catches anything else so the daemon
    thread dies quietly instead of stack-tracing over the publish loop.

    ``stop_event`` is the testability seam: production passes nothing
    (infinite loop, daemon thread dies with the process); tests set the
    event to exit after a bounded number of iterations.
    """
    try:
        offset = None
        while stop_event is None or not stop_event.is_set():
            try:
                updates = asyncio.run(_review_get_updates(offset))
            except Conflict as exc:
                logger.error(
                    "[review] getUpdates got 409 Conflict — another "
                    "process is polling with the same bot token. Review "
                    "buttons must be enabled on exactly ONE instance "
                    "(shared prod+test token); disable "
                    "REVIEW_BUTTONS_ENABLED on the other instance. "
                    "Backing off %ss. (%s)",
                    REVIEW_LISTENER_CONFLICT_BACKOFF_SECONDS,
                    sanitize_error_message(exc),
                )
                _review_listener_sleep(
                    stop_event, REVIEW_LISTENER_CONFLICT_BACKOFF_SECONDS)
                continue
            except Exception as exc:
                # Long-poll timeout, network blip, TelegramError, event-loop
                # weirdness — log, short backoff, poll again. Never busy-loop,
                # never die.
                logger.error(
                    "[review] review listener poll failed "
                    "(%s: %s); retrying in %ss.",
                    type(exc).__name__, sanitize_error_message(exc),
                    REVIEW_LISTENER_ERROR_BACKOFF_SECONDS,
                )
                _review_listener_sleep(
                    stop_event, REVIEW_LISTENER_ERROR_BACKOFF_SECONDS)
                continue

            for update in updates:
                try:
                    # Ack first: even a poisoned update is consumed exactly
                    # once — Telegram must not redeliver it forever.
                    offset = update.update_id + 1
                    _handle_review_update(update)
                except Exception:
                    # DB fault, Telegram edit/answer error, exotic update
                    # shape — log with traceback and move on. The thread
                    # (and the publish loop) must survive any single update.
                    logger.exception(
                        "[review] failed to handle update %s — skipped",
                        getattr(update, 'update_id', '?'),
                    )
    except Exception:
        # Belt and braces (e.g. Bot() constructor failure): a daemon thread
        # must never stack-trace over the publish process.
        logger.exception(
            "[review] review listener thread crashed — [E014] buttons are "
            "inactive until the next restart"
        )


def _review_listener_gate_reason():
    """Classify the review-listener gate state (audit CA-3 + CA-6).

    Returns one of:
      * ``'ok'`` — flag on, bot token present, numeric admin id;
      * ``'off'`` — flag off (the default / test-instance state);
      * ``'no_token'`` — flag on but ``TELEGRAM_BOT_TOKEN`` is empty.
        Fail-closed (audit CA-3): without this check the listener starts
        and ``Bot(token=None)`` raises ``InvalidToken`` every poll — a
        perpetual 5s ERROR loop. Mirrors the send path's credential
        guard in ``send_admin_notification``;
      * ``'bad_admin'`` — flag on but ``TELEGRAM_ADMIN_ID`` is not
        numeric (the default ``@sunny413x`` can never equal a numeric
        ``from_user.id``, so nobody could ever be authorized).

    Single source for the flag read (audit CA-6): the three startup
    shapes in ``_maybe_start_review_listener`` branch on this reason
    instead of re-checking ``REVIEW_BUTTONS_ENABLED`` separately.
    Module attributes are read at call time so tests can patch them.
    """
    if not REVIEW_BUTTONS_ENABLED:
        return 'off'
    if not TELEGRAM_BOT_TOKEN:
        return 'no_token'
    try:
        int(TELEGRAM_ADMIN_ID)
    except (TypeError, ValueError):
        return 'bad_admin'
    return 'ok'


def _review_listener_enabled():
    """Pure boolean gate (tech-spec Decisions 5 + 6) — True iff the
    listener can actually serve presses (flag on + bot token + numeric
    admin id). Shared by the listener startup AND the E014 send site
    (audit SEC-A8-1): buttons are only rendered when a listener with
    the exact same effective config would serve them — no dead buttons,
    no orphan tokens."""
    return _review_listener_gate_reason() == 'ok'


def _maybe_start_review_listener():
    """main() wiring: start the daemon listener thread iff the gate is open.

    Returns the started ``threading.Thread`` or ``None``. Three shapes,
    branching on ``_review_listener_gate_reason()`` (audit CA-6 — single
    flag read):
      * ``'off'`` (default — test instance) → silent no-op;
      * ``'no_token'`` / ``'bad_admin'`` → WARNING + best-effort admin
        ping naming the broken knob (fail-closed: the listener does not
        start, and per SEC-A8-1 the E014 send site renders no buttons
        under the same gate);
      * ``'ok'`` → daemon thread + "review listener active" log/ping.
    """
    reason = _review_listener_gate_reason()
    if reason == 'off':
        return None
    if reason != 'ok':
        if reason == 'no_token':
            warn_log = (
                "[startup] review listener disabled — TELEGRAM_BOT_TOKEN "
                "is empty (fail-closed); [E014] buttons will not work "
                "until a bot token is configured."
            )
            warn_ping = (
                "⚠️ REVIEW_BUTTONS_ENABLED включён, но TELEGRAM_BOT_TOKEN "
                "пуст — слушатель нажатий выключен (fail-closed), кнопки "
                "под [E014] работать не будут. Задайте TELEGRAM_BOT_TOKEN "
                "и перезапустите бота."
            )
        else:  # 'bad_admin'
            warn_log = (
                "[startup] review listener disabled — TELEGRAM_ADMIN_ID is "
                "not numeric (fail-closed); [E014] buttons will not work "
                "until a numeric admin id is configured."
            )
            warn_ping = (
                "⚠️ REVIEW_BUTTONS_ENABLED включён, но TELEGRAM_ADMIN_ID "
                "не числовой — слушатель нажатий выключен (fail-closed), "
                "кнопки под [E014] работать не будут. Укажите числовой "
                "TELEGRAM_ADMIN_ID и перезапустите бота."
            )
        logger.warning(warn_log)
        try:
            send_admin_notification(warn_ping)
        except Exception as notify_err:
            logger.error(
                f"[startup] failed to send review-listener warning ping: "
                f"{sanitize_error_message(notify_err)}"
            )
        return None

    listener_thread = threading.Thread(
        target=_run_review_listener,
        name='review-listener',
        daemon=True,
    )
    listener_thread.start()
    logger.info("[startup] review listener active (daemon thread started)")
    try:
        send_admin_notification(
            "✅ Review listener active — кнопки под [E014] обрабатываются "
            "этим инстансом."
        )
    except Exception as notify_err:
        logger.error(
            f"[startup] failed to send review-listener startup ping: "
            f"{sanitize_error_message(notify_err)}"
        )
    return listener_thread


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
    # --- Cross-promo / navigation sentences (incident 2026-07-29) ---------
    # t-hunted embeds "go read our other posts" CTAs INSIDE otherwise
    # legitimate paragraphs:
    #   "Вы можете посмотреть все, что мы уже публиковали о серии Pop
    #    Culture, по этой ссылке. Нажмите здесь и посмотрите, что мы уже
    #    показывали о серии Entertainment"
    # `boilerplate_filter` could not help: its click-CTA pattern is
    # ^-anchored at PARAGRAPH start, and here the CTA is the second
    # sentence. Whole-paragraph removal would also destroy the real prose
    # around it. Sentence-level is the only correct granularity, which is
    # exactly what this list already does for author social plugs.
    #
    # Kept deliberately NARROW — these must match only sentences that are
    # PURE navigation. A sentence carrying a fact ("Цена — $28, подробнее по
    # ссылке") must survive, because dropping it would lose the price; the
    # prompt's allowed-drop (d) handles that case by rewriting instead.
    # Hence: the CTA pattern anchors the imperative at sentence START, and
    # the "see everything we posted" pattern requires BOTH a viewing verb
    # and the link phrase in the same sentence.
    re.compile(
        r'(?:(?<=[\.\!\?])\s*|^)'
        r'\*{0,2}(?:нажми(?:те)?|кликни(?:те)?|жми(?:те)?)\s*\*{0,2}\s*'
        r'(?:сюда|здесь|по\s+ссылке)'
        r'[^\.\!\?]*?(?:[\.\!\?]+|$)\s*',
        re.I,
    ),
    re.compile(
        r'(?:(?<=[\.\!\?])\s*|^)'
        r'[^\.\!\?]{0,120}?'
        r'(?:посмотр|увид|прочит|показыва|публикова|постил)'
        r'[^\.\!\?]{0,120}?'
        r'по\s+\*{0,2}(?:этой\s+)?ссылке\*{0,2}'
        r'[^\.\!\?]*?(?:[\.\!\?]+|$)\s*',
        re.I,
    ),
    # Dangling pointer at OUR page layout — "подробнее в видео ниже",
    # "смотрите на фото выше". The model cannot see the page it writes for:
    # a t-hunted article carries no video blocks at all, and our re-layout
    # makes «ниже» unverifiable even when a video does survive. Same
    # narrowness rule — the viewing verb and the position word must sit in
    # one sentence together.
    re.compile(
        r'(?:(?<=[\.\!\?])\s*|^)'
        r'[^\.\!\?]{0,120}?'
        r'(?:в|на)\s+\*{0,2}(?:видео|фото|картинке|изображении)\*{0,2}\s+'
        r'(?:ниже|выше)'
        r'[^\.\!\?]*?(?:[\.\!\?]+|$)\s*',
        re.I,
    ),
    # --- Affiliate recommendation with an outside link (incident 2026-08-12) -
    # t-hunted recommends a package-forwarding company whenever a drop does not
    # ship internationally:
    #   "Рекомендуем эту компанию: www.instagram.com/minidelass/"
    # Two signals in ONE sentence — a recommending verb AND a link to an
    # outside profile — because either alone is ordinary prose: "рекомендуем
    # эту модель коллекционерам" carries no link, and "показал прототип в
    # своём Instagram: …" carries no recommendation. Both survive; the
    # negative controls in tests/test_boilerplate_filter.py pin that.
    #
    # Sentence scope is the point. The first attempt put this in
    # `boilerplate_filter._LONG_BOILERPLATE_PATTERNS`, where a match drops the
    # whole paragraph — and since the plug sits at the END, the rule could not
    # be `^`-anchored, so a paragraph of prices and model names with an ad
    # bolted on vanished entirely. Here only the plug sentence goes.
    #
    # `(?<![\w-])` before the platform list is load-bearing: `_PLUG_PLATFORMS`
    # includes the bare token `x`, so without it `matchbo|x|.com` matched —
    # and Matchbox is Mattel's sibling brand, the likeliest domain in a real
    # paragraph here. `netflix.com` and `roblox.com` collided the same way.
    re.compile(
        r'(?:(?<=[\.\!\?])\s*|^)'
        r'[^\.\!\?]{0,120}?'
        r'(?:рекомендуем|советуем|recomendamos|sugerimos|indicamos|we\s+recommend)'
        r'(?:[^.]|\.(?=\S)){0,120}?'
        r'(?<![\w-])(?:' + _PLUG_PLATFORMS_NB + r')\.com/'
        r'[^\s]*\s*',
        re.I,
    ),
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


def _maybe_alert_openrouter_balance():
    """Best-effort daily heads-up: ping the admin ([E019]) when the OpenRouter
    balance falls below ``OPENROUTER_MIN_BALANCE_USD`` (default 5). Runs once per
    tick from ``job()``. NON-BLOCKING — any failure is swallowed so a monitoring
    probe never breaks publishing. ``get_remaining_credits`` returns None (→ skip)
    when no OpenRouter key is configured or the balance can't be read."""
    try:
        import openrouter_transcreation
        remaining = openrouter_transcreation.get_remaining_credits()
        if remaining is None:
            return
        raw = os.getenv("OPENROUTER_MIN_BALANCE_USD", "").strip()
        try:
            threshold = float(raw) if raw else 5.0
        except ValueError:
            threshold = 5.0
        if remaining < threshold:
            send_admin_notification(
                admin_alerts.alert_openrouter_low_balance(remaining, threshold)
            )
    except Exception as exc:
        logger.warning(
            "OpenRouter balance check skipped: %s", sanitize_error_message(exc)
        )


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


#: Scan bounds for the promo filter ([E035]). EVERY scanned input is
#: capped: the title, the URL path and the joined body (first N
#: paragraphs) are each sliced to ``_PROMO_SCAN_MAX_CHARS`` before
#: folding. Shop pitches front-load their call-to-action language, so a
#: bounded scan is enough — and a megabyte-sized title or body can
#: never stall the intake loop (audit SEC-PROMO-2).
_PROMO_SCAN_MAX_PARAGRAPHS = 8
_PROMO_SCAN_MAX_CHARS = 2000

#: Promo markers, three tiers. Stored in canonical accented form;
#: matching is accent-stripped + lowercased on BOTH sides (see
#: ``_promo_fold``), so "nao perca" / "NÃO PERCA" hit 'não perca'.
#: Plain word-bounded substring matching — no regex on the marker side,
#: ReDoS-safe. The block rule lives in ``_is_promo_article``.
#:
#: DIRECT call-to-action — an imperative addressed to the READER. This
#: is the sharpest ad signal: ad copy tells you to act, journalism
#: (even when quoting a shopkeeper) does not.
_PROMO_CTA_DIRECT_MARKERS = (
    # PT (t-hunted and friends)
    'compre já',
    'garanta o seu',
    'garanta já',
    'não perca',
    'aproveite a oferta',
    # EN
    'shop now',
    'buy now',
    'use code',
    'order yours',
)

#: OFFER call-to-action — noun phrases naming an offer or a
#: purchase-urgency perk. Weaker than DIRECT because journalism reports
#: them factually ("the revamped shop now offers free shipping",
#: "a discount code was floating around the Discord"), so an OFFER
#: marker never blocks on its own — see the rule.
_PROMO_CTA_OFFER_MARKERS = (
    # PT
    'cupom',
    'código de desconto',
    'frete grátis',
    'promoção',
    # EN
    'coupon code',
    'discount code',
    'free shipping',
)

#: SELLER-voice markers — the publisher speaking AS the shop ("our
#: store"). Separates an ad from retail JOURNALISM, which writes about
#: somebody else's store ("Mattel relaunches its online store",
#: "beloved shop closes"). Not sufficient alone: review round 2 showed
#: interview and community pieces quote owners and fans in exactly this
#: first person, so a CTA marker is required alongside it.
_PROMO_SELLER_MARKERS = (
    'nossa loja',
    'em nossa loja',
    'our store',
    'our shop',
)

#: Every marker whose span is blanked before the WEAK pass (see
#: ``_promo_scan_markers``). Union of the three decision tiers; the
#: structural invariants (disjoint tiers, all folded) are pinned by
#: ``TestPromoMarkerSets``.
_PROMO_STRONG_MARKERS = (
    _PROMO_CTA_DIRECT_MARKERS
    + _PROMO_CTA_OFFER_MARKERS
    + _PROMO_SELLER_MARKERS
)

#: WEAK promo markers — commerce vocabulary that legit news also uses
#: ("hits stores in September", "chega às lojas"). WEAK markers NEVER
#: affect the verdict, whatever their count: they are collected purely
#: so the [E035] ping shows the operator the full picture of what the
#: article looked like.
_PROMO_WEAK_MARKERS = (
    # PT
    'loja',
    'à venda',
    'estoque',
    'desconto',
    'oferta',
    # EN
    'store',
    'on sale',
    'in stock',
    'discount',
    'offer',
)

#: URL-path tokens counted as WEAK promo markers (rendered as
#: ``url:<token>`` in the matched-marker list). Demoted from STRONG in
#: review round 1: real outlets routinely put store/shop/loja in slugs
#: for ordinary retail news ('mattel-creations-store-drop-rlc',
#: 'hot-wheels-shop-closes-after-30-years'), so a slug alone must never
#: help complete the block bar.
_PROMO_URL_TOKENS = ('loja', 'shop', 'store')


def _promo_fold(text):
    """Fold ``text`` for promo-marker matching: NFKD accent-strip +
    lowercase + collapse to single-space-separated word tokens, padded
    with one space on each side so a plain ``in`` substring check is
    word-bounded ('loja' never hits 'lojas', 'store' never hits
    'restored'). Non-str input folds to the empty token string."""
    if not isinstance(text, str):
        text = ''
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return ' ' + ' '.join(re.findall(r'[a-z0-9]+', text.lower())) + ' '


def _promo_scan_input(value):
    """Coerce one scanned input to a bounded str. Non-str values (a
    list/int/bool ``link`` from a malformed feed) degrade to '' instead
    of raising — the filter must never crash the tick (audit
    SEC-PROMO-1); the slice enforces the documented scan bound on EVERY
    input, not just the body (SEC-PROMO-2)."""
    if not isinstance(value, str):
        return ''
    return value[:_PROMO_SCAN_MAX_CHARS]


#: Markers pre-folded once at import so the per-entry scan is pure
#: substring checks.
_PROMO_STRONG_FOLDED = tuple(
    (m, _promo_fold(m)) for m in _PROMO_STRONG_MARKERS)
_PROMO_WEAK_FOLDED = tuple(
    (m, _promo_fold(m)) for m in _PROMO_WEAK_MARKERS)


def _promo_scan_markers(text):
    """Return ``(strong, weak)`` matched promo markers for ``text``.

    A WEAK marker is counted ONLY when it occurs outside every matched
    STRONG marker's span: five STRONG markers literally contain a WEAK
    one ('nossa loja' ⊃ 'loja', 'our store' ⊃ 'store', 'código de
    desconto' ⊃ 'desconto', 'discount code' ⊃ 'discount', 'aproveite a
    oferta' ⊃ 'oferta'), so naive counting would let ONE phrase supply
    both its own STRONG hit and a "corroborating" WEAK hit — collapsing
    the intended two-independent-signals bar (review round 1, major).
    Matched STRONG spans are blanked out of the folded text before the
    WEAK pass; a weak word that ALSO appears independently elsewhere
    still counts.
    """
    folded = _promo_fold(text)
    strong = [m for m, f in _PROMO_STRONG_FOLDED if f in folded]
    residual = folded
    for _m, f in _PROMO_STRONG_FOLDED:
        if f in residual:
            # Replace with a single space: the padding that made the
            # match word-bounded is preserved for neighbouring markers.
            residual = residual.replace(f, ' ')
    weak = [m for m, f in _PROMO_WEAK_FOLDED if f in residual]
    return strong, weak


def _is_promo_article(entry, article):
    """Return the list of matched promo markers when the entry+article
    look like a shop-promo/ad post (truthy → drop at intake, [E035]);
    empty list otherwise. The list is surfaced in the operator alert so
    a false positive is diagnosable at a glance.

    Prod incident 2026-07-25: t-hunted.blogspot.com published a pure
    store ad («Hot Wheels antigos e raros na loja Universo Hot Wheels»)
    and the bot translated (wasted LLM tokens) + posted it.

    Block rule, tuned across two review rounds against 15 realistic
    lamley/autoevolution/t-hunted/orangetrack snippets:

      * a SELLER-voice marker AND (>= 1 DIRECT call-to-action OR >= 2
        call-to-action markers of any tier), OR
      * >= 3 distinct call-to-action markers (a dense CTA stack — no
        seller voice needed).

    An ad TELLS THE READER TO ACT; journalism does not, even when it
    quotes a shopkeeper. Round 1 established that seller voice is
    needed (news covers somebody else's shop: "Mattel relaunches its
    online store", "beloved Hot Wheels shop closes"); round 2 showed
    seller voice is not sufficient, because interview / Q&A / community
    pieces quote owners and fans in the first person ("Our store has
    always focused on…", "our store finally got it back in stock") —
    hence the CTA requirement on top.

    Thresholds are deliberately one notch above the intuitive ones:
    a lone OFFER marker beside seller voice does not block (the
    community-roundup 'discount code' quote), and two CTA markers
    without seller voice do not block (the storefront-relaunch story
    where 'shop now' lands as an accidental bigram in "the revamped
    shop now offers free shipping").

    WEAK commerce words (including the 'loja'/'shop'/'store' URL-slug
    token, rendered ``url:loja``) never affect the verdict — they are
    reported in the marker list for the operator only, with hits inside
    a matched marker's span suppressed (see ``_promo_scan_markers``).

    Inputs scanned (source language, pre-translation): entry/article
    title, the entry link's URL path, and the first
    ``_PROMO_SCAN_MAX_PARAGRAPHS`` paragraphs of the body — each capped
    at ``_PROMO_SCAN_MAX_CHARS`` chars.

    Called from ``job()`` step (b3) right after the checklist reject —
    before the (more expensive) dedup gate, per the cheapest-filter-
    first ordering. Tolerates malformed input: non-dict ``entry`` /
    ``article`` and non-str link/title/paragraph values degrade to
    "not promo" instead of raising.
    """
    if not isinstance(entry, dict):
        entry = {}
    if not isinstance(article, dict):
        article = {}
    title = (_promo_scan_input(entry.get('title'))
             or _promo_scan_input(article.get('title')))
    paragraphs = article.get('paragraphs')
    if not isinstance(paragraphs, (list, tuple)):
        paragraphs = []
    body = ' '.join(
        p for p in paragraphs[:_PROMO_SCAN_MAX_PARAGRAPHS]
        if isinstance(p, str)
    )[:_PROMO_SCAN_MAX_CHARS]

    strong, weak = _promo_scan_markers(f'{title} {body}')

    try:
        path = urlparse(_promo_scan_input(entry.get('link'))).path
    except ValueError:
        # Malformed URL (e.g. an unterminated IPv6 literal) — the slug
        # signal is optional, the filter carries on without it.
        path = ''
    path_tokens = set(_promo_fold(path).split())
    weak += [
        f'url:{tok}' for tok in _PROMO_URL_TOKENS if tok in path_tokens
    ]

    seller = [m for m in strong if m in _PROMO_SELLER_MARKERS]
    direct = [m for m in strong if m in _PROMO_CTA_DIRECT_MARKERS]
    cta = direct + [m for m in strong if m in _PROMO_CTA_OFFER_MARKERS]

    if seller and (direct or len(cta) >= 2):
        return strong + weak
    if len(cta) >= 3:
        return strong + weak
    return []


# ---------------------------------------------------------------------------
# CONTENT GATE (2026-07-25) — three post GENRES the operator does not want
# published automatically. Prod incident: the bot published a thin t-hunted
# post that was just "here are the photos of the 2026 poster" (four
# sentences, 12 images, plus a video our parser cannot embed).
#
# Two verdicts, two mechanisms:
#   * HOLD  — poster / catalog / packaging → staged BUT parked
#             (``pending_articles.hold_reason``), invisible to the publish
#             queue, released only by «✅ Опубликовать» ([E036]).
#             NO answer = it never publishes. No timer, no auto-drop.
#   * DROP  — video review / event announcement → rejected at intake like
#             a promo post, link pinned in processed_news ([E037]).
#
# Precedence: HOLD wins. The incident post mentions «no vídeo abaixo» but
# is a poster post — the operator gets to decide, it is not silently binned.
#
# DETECTION IS SUBJECT-ANCHORED, not body keyword soup: only the title and
# the (title-derived) URL slug are scanned. An article that merely MENTIONS
# a poster, or merely EMBEDS a video, or merely happens at a convention, is
# not one of these genres. Matching reuses the promo filter's machinery —
# ``_promo_fold`` (accent-strip + lowercase + word-bounded tokens),
# ``_promo_scan_input`` (non-str → '', hard char cap) — so a marker never
# fires on a longer word ('poster' never hits 'posterior') and a
# pathological title can never stall intake.
#
# Markers are stored in canonical (accented) spelling for the operator's
# alert and folded once at import for matching; the structural invariants
# — tiers non-empty, pairwise disjoint, no two markers folding to the same
# token, no collision with the promo tiers — are pinned by
# ``TestContentGateMarkerSets``.
# ---------------------------------------------------------------------------

#: HOLD markers (category 1): the post's SUBJECT is a poster, a catalog or
#: packaging — a picture-dump genre with little to translate. One marker in
#: the title (or slug) is enough: the operator said outright they would not
#: have published the incident post, and a hold costs one button press
#: while a bad publish cannot be taken back.
_HOLD_TITLE_MARKERS = (
    # Poster / promo art. 'pôster' folds to the same token as EN 'poster',
    # so one entry covers both languages; plurals need their own entries
    # because matching is word-bounded.
    'poster',
    'posters',
    'pôsteres',
    'cartaz',
    'cartazes',
    # Catalog (PT + both EN spellings).
    'catálogo',
    'catálogos',
    'catalog',
    'catalogs',
    'catalogue',
    'catalogues',
    # Packaging / card art.
    'embalagem',
    'embalagens',
    'packaging',
    'cartela',
    'cartelas',
    'blister',
    'blisters',
    'cardback',
    'cardbacks',
    'card back',
    'card art',
    'box art',
)

#: VIDEO markers (category 2), matched ANYWHERE in the subject: words that
#: name the genre. 'vídeo' folds to 'video', which also catches the EN word.
#:
#: NEVER sufficient on their own — see ``_is_rejected_genre`` for the
#: two-branch rule. Review round 1 (F1/F2) found the bare word being used
#: as ordinary headline language in genuine car reveals («Mattel drops
#: video revealing the 2027 Corvette Z06», «Vídeo revela o novo Porsche
#: 911 GT3 RS», «Unboxing surprises us: … Supra revealed»), and unlike a
#: HOLD a DROP is permanent and unrecoverable.
_GENRE_VIDEO_MARKERS = (
    'vídeo',
    'vídeos',
    'unboxing',
    'assista',
    'assistam',
    'youtube',
)

#: Genre words that count ONLY in the SEPARATOR form («Watch: …»), never
#: as an anywhere-marker and never via the noun-phrase branch.
#:
#: 'watch' is an ordinary imperative VERB, unlike the noun-like
#: 'vídeo'/'unboxing' that head a labelled headline. As an anywhere-marker
#: it would eat «watch out for these five Treasure Hunts» / «a casting
#: worth watching»; via the noun-phrase branch it would eat every
#: «Watch {this|these|our|my|all|every} …» sentence, which is just English
#: and says nothing about video content (code review round 2). Only
#: «Watch:» — the explicit headline convention — is unambiguous.
_GENRE_VIDEO_LEAD_ONLY_MARKERS = (
    'watch',
)

#: REVIEW co-markers — the second independent signal for branch (B). These
#: name the ACT of reviewing or opening a product the author ALREADY HAS,
#: so «vídeo» + one of these is a video review, while «vídeo» alone is
#: just a word. Deliberately contains no video marker as a substring
#: (a phrase like 'case unboxing' would supply both signals by itself and
#: silently collapse the two-signal bar — the promo filter's round-1 bug).
#:
#: Review round 2 (R2) removed the "I touched it" preview words —
#: 'hands-on' and 'first impressions'. Both are ordinary PREVIEW-
#: journalism vocabulary for a not-yet-released casting ("First hands-on
#: video of the 2027 Corvette Z06 STH"), i.e. exactly the coverage the
#: channel exists for, and lexical proximity to 'video' is not proof of
#: genre. Losing them can only cause a miss (an unwanted review gets
#: published — recoverable, and the pre-feature status quo), whereas
#: keeping them caused an unrecoverable drop.
_GENRE_VIDEO_REVIEW_MARKERS = (
    # EN
    'review',
    'reviews',
    'full case',
    'assortment',
    'we open',
    # PT
    'análise',
    'análises',
    'abrimos',
    'caixa completa',
    'caixa fechada',
    'lote completo',
)

#: HEADLINE VERBS — finite verbs that turn a title into a NEWS CLAUSE
#: rather than a label for the post's subject. Their presence suppresses
#: branch (A2) only (see ``_genre_video_subject_marker``).
#:
#: Review round 2 (R1): "Video of the 2027 Corvette Z06 reveal LEAKED
#: online", "Video of the new HW Legends Tour winner SURFACES online",
#: "Video of the 2027 Toyota Supra STH debut GOES viral" — a standard
#: automotive-journalism template where the video is merely the vehicle
#: for a reveal. Grammatically it is identical to the required-drop case
#: «Unboxing DA caixa J de 2026» (both are genre word + genitive + noun
#: phrase), which is why narrowing the NP-head list to bare determiners
#: was NOT the fix: it would have broken that required case while leaving
#: the template intact. The finite verb is the discriminator the data
#: actually supports — a label has no predicate, a news clause does.
#:
#: A closed list, so a miss is the failure mode (cheap by our weighting).
#: NOTE: PT «é» is deliberately absent — it accent-folds to «e», the
#: Portuguese word for "and", which would suppress nearly every PT title.
_GENRE_HEADLINE_VERBS = (
    # EN
    'leaked', 'leaks', 'surfaces', 'surfaced', 'goes', 'went',
    'breaks', 'broke', 'shows', 'showed', 'confirms', 'confirmed',
    'debuts', 'debuted', 'arrives', 'arrived', 'reveals', 'revealed',
    'appears', 'appeared', 'hits', 'drops', 'dropped', 'lands', 'landed',
    'returns', 'returned', 'gets', 'got', 'becomes', 'became',
    'is', 'are', 'was', 'were', 'has', 'have', 'wins', 'won',
    # PT
    'revela', 'revelou', 'revelado', 'vaza', 'vazou', 'aparece',
    'apareceu', 'mostra', 'mostrou', 'confirma', 'confirmou',
    'chega', 'chegou', 'ganha', 'ganhou', 'traz', 'trouxe',
    'volta', 'voltou', 'saiu', 'virou',
)

#: Every genre word eligible for SUBJECT POSITION (branch A).
_GENRE_VIDEO_LEAD_MARKERS = (
    _GENRE_VIDEO_MARKERS + _GENRE_VIDEO_LEAD_ONLY_MARKERS
)

#: Determiners / prepositions that turn a leading genre word into the HEAD
#: OF A NOUN PHRASE — «Unboxing THE 2026 H case», «Unboxing DA caixa J»,
#: «Assista AO review». First half of what separates a genre post from a
#: news clause: a leading genre word followed by a VERB («Vídeo REVELA o
#: Porsche», «Unboxing SURPRISES us») is a sentence ABOUT something, not a
#: label for the post's subject. Kept to closed-class function words only —
#: no verbs, and deliberately NOT 'out' (which would re-open «Watch out
#: for these five Treasure Hunts»).
#:
#: NOT sufficient on its own (review round 2, R1): the genitives here are
#: also how a news clause names its subject («Video OF THE Corvette reveal
#: leaked online»), so branch (A2) additionally requires that no
#: ``_GENRE_HEADLINE_VERBS`` token follow.
_GENRE_VIDEO_NP_HEADS = (
    # EN
    'the', 'a', 'an', 'this', 'these', 'that', 'those', 'all', 'every',
    'my', 'our', 'of', 'with',
    # PT
    'o', 'os', 'as', 'um', 'uma', 'uns', 'umas',
    'do', 'da', 'dos', 'das', 'de',
    'no', 'na', 'nos', 'nas', 'ao', 'aos', 'com',
    'todo', 'toda', 'todos', 'todas',
    'este', 'esta', 'esse', 'essa',
)

#: EVENT NAME markers (category 3, first half): the name of a gathering.
#: NEVER sufficient on its own — see ``_GENRE_EVENT_ORG_MARKERS``.
_GENRE_EVENT_NAME_MARKERS = (
    'convention',
    'conventions',
    'convenção',
    'convenções',
    'expo',
    'feira',
    'feiras',
    'encontro',
    'encontros',
    'meetup',
    'nationals',
    'swap meet',
    'legends tour',
)

#: EVENT ORGANIZATIONAL markers (category 3, second half): the logistics
#: vocabulary that makes a post ABOUT the event rather than about a car
#: that happens to debut there.
#:
#: THIS PAIRING IS THE CRITICAL FALSE-POSITIVE GUARD. A convention-
#: exclusive casting reveal ("Hot Wheels Convention 2026 exclusive Datsun
#: revealed", "exclusivo da convenção") is legitimate model news — exactly
#: what the channel exists for — and the mere name of a convention must
#: never drop it. Equally, the org words alone are ordinary release
#: language ("2026 mainline release dates", "datas de lançamento"), so
#: BOTH halves must appear in the subject.
_GENRE_EVENT_ORG_MARKERS = (
    # PT
    'datas',
    'ingresso',
    'ingressos',
    'inscrições',
    'inscrição',
    'programação',
    'credenciamento',
    'acontece',
    # EN
    'dates',
    'tickets',
    'registration',
    'will be held',
    'schedule',
    'venue',
)

#: Markers pre-folded once at import so the per-entry scan is pure
#: substring checks (same shape as ``_PROMO_STRONG_FOLDED``).
_HOLD_TITLE_FOLDED = tuple((m, _promo_fold(m)) for m in _HOLD_TITLE_MARKERS)
_GENRE_VIDEO_FOLDED = tuple((m, _promo_fold(m)) for m in _GENRE_VIDEO_MARKERS)
_GENRE_VIDEO_REVIEW_FOLDED = tuple(
    (m, _promo_fold(m)) for m in _GENRE_VIDEO_REVIEW_MARKERS)
_GENRE_EVENT_NAME_FOLDED = tuple(
    (m, _promo_fold(m)) for m in _GENRE_EVENT_NAME_MARKERS)
_GENRE_EVENT_ORG_FOLDED = tuple(
    (m, _promo_fold(m)) for m in _GENRE_EVENT_ORG_MARKERS)


def _content_gate_deaccent(text):
    """NFKD accent-strip + lowercase, PUNCTUATION PRESERVED.

    ``_promo_fold`` throws punctuation away to get word-bounded tokens,
    which is right for marker matching but destroys the very signal the
    subject-position rule reads: the «Vídeo:» headline colon. This is the
    same normalisation minus the tokenisation, so the lead regexes can be
    written against plain ASCII while still matching accented titles.
    """
    if not isinstance(text, str):
        return ''
    text = unicodedata.normalize('NFKD', text)
    return ''.join(
        ch for ch in text if not unicodedata.combining(ch)).lower()


#: Folded token → canonical (accented) marker, for reporting a subject-
#: position hit under the same name the operator sees everywhere else.
_GENRE_VIDEO_LEAD_CANON = {
    _promo_fold(m).strip(): m for m in _GENRE_VIDEO_LEAD_MARKERS
}

def _genre_lead_alternation(markers):
    """Longest-first regex alternation over the FOLDED forms of ``markers``.

    Longest-first so 'vídeos' wins over 'vídeo' — otherwise «Vídeos:» would
    fail to match, because after 'video' comes 's', not a separator. Built
    FROM the marker tuples so regex and markers cannot drift apart.
    """
    return '|'.join(
        re.escape(_promo_fold(m).strip()).replace(r'\ ', r'\s+')
        for m in sorted(markers, key=lambda m: len(_promo_fold(m)), reverse=True)
    )


#: Branch (A1) — genre word at the head of the title, immediately followed
#: by a separator: «Vídeo: …», «Watch: …», «Unboxing — …». The classic
#: "the subject is the video" headline convention. Accepts EVERY lead
#: marker, including the separator-only ones.
_GENRE_VIDEO_LEAD_SEP_RE = re.compile(
    rf'^\s*({_genre_lead_alternation(_GENRE_VIDEO_LEAD_MARKERS)})\s*[:\-–—]')

#: Branch (A2) — genre word at the head of the title followed by a
#: determiner/preposition, i.e. heading a NOUN PHRASE: «Unboxing the 2026
#: H case …», «Assista ao …». A leading genre word followed by anything
#: else (a verb: «Vídeo revela …», «Unboxing surprises …») is a clause
#: reporting news, and is deliberately NOT matched.
#:
#: Built from ``_GENRE_VIDEO_MARKERS`` ONLY — the separator-only markers
#: are excluded (code review round 2). Those are imperative VERBS, and
#: «Watch {this|these|our|my|all|every} …» is ordinary English that says
#: nothing about video content, whereas the noun-like markers genuinely
#: head a labelled noun phrase («Unboxing da caixa J …»).
_GENRE_VIDEO_LEAD_NP_RE = re.compile(
    rf'^\s*({_genre_lead_alternation(_GENRE_VIDEO_MARKERS)})\s+'
    rf'(?:{"|".join(re.escape(w) for w in _GENRE_VIDEO_NP_HEADS)})\b'
)


_GENRE_HEADLINE_VERBS_FOLDED = tuple(
    (v, _promo_fold(v)) for v in _GENRE_HEADLINE_VERBS)


def _genre_headline_verbs(folded_title):
    """Matched ``_GENRE_HEADLINE_VERBS`` in the folded title — i.e. the
    evidence that the title is a news CLAUSE, not a label."""
    return [v for v, f in _GENRE_HEADLINE_VERBS_FOLDED if f in folded_title]


def _genre_video_subject_marker(raw_title, folded_title):
    """Return ``(marker, branch)`` when a video genre word occupies
    SUBJECT POSITION at the head of ``raw_title``, else ``None``.

    Two accepted shapes, reported as separate BRANCHES so the caller can
    treat them differently (see ``_GENRE_BRANCH_ACTION``):

      * ``'video_lead'`` — ``'vídeo:'``: genre word + separator
        («Vídeo: …», «Watch: …»). Unambiguous headline convention, no
        further conditions.
      * ``'video_np'`` — ``'vídeo …'``: genre word heading a noun phrase
        («Unboxing da caixa J …»). Additionally requires that NO finite
        headline verb appear in the title (review round 2, R1): «Video of
        the Corvette reveal LEAKED online» is grammatically the same
        shape but is a news clause, and dropping it is unrecoverable.

    Title-only by design: subject position is a property of word order and
    punctuation, neither of which a URL slug preserves (a reveal whose
    slug happens to start with 'video-' must not be dropped).
    """
    text = _content_gate_deaccent(raw_title)

    match = _GENRE_VIDEO_LEAD_SEP_RE.match(text)
    if match:
        return (_genre_lead_marker(match, ':'), 'video_lead')

    match = _GENRE_VIDEO_LEAD_NP_RE.match(text)
    if match and not _genre_headline_verbs(folded_title):
        return (_genre_lead_marker(match, ' …'), 'video_np')
    return None


def _genre_lead_marker(match, suffix):
    """Render a lead-position hit under the marker's canonical spelling."""
    token = re.sub(r'\s+', ' ', match.group(1))
    return f'{_GENRE_VIDEO_LEAD_CANON.get(token, token)}{suffix}'


def _content_gate_subject(entry, article):
    """Coerce ``entry``/``article`` into the SUBJECT text the content gate
    scans: ``(raw_title, folded_title, folded_url_path)``.

    Title comes from the feed entry, falling back to the parsed article.
    The URL path is corroboration only in spirit — on the sources we read
    (blogspot, wordpress, autoevolution) the slug is generated FROM the
    title, so it adds no independent false-positive surface while
    surviving a feed that truncates or mangles the title.

    Never raises: non-dict inputs, non-str fields and unparseable URLs all
    degrade to empty text (the filter must never crash the tick —
    audit SEC-PROMO-1's lesson applies verbatim here). Both inputs go
    through ``_promo_scan_input``, so the documented char cap holds.
    """
    if not isinstance(entry, dict):
        entry = {}
    if not isinstance(article, dict):
        article = {}
    raw_title = (_promo_scan_input(entry.get('title'))
                 or _promo_scan_input(article.get('title')))
    try:
        path = urlparse(_promo_scan_input(entry.get('link'))).path
    except ValueError:
        # Malformed URL (e.g. an unterminated IPv6 literal) — the slug
        # signal is optional, the gate carries on without it.
        path = ''
    return raw_title, _promo_fold(raw_title), _promo_fold(path)


def _content_gate_hits(folded_title, folded_path, folded_markers):
    """Matched markers for one tier. Title hits are reported bare; a
    marker found ONLY in the slug is reported as ``url:<marker>`` so the
    operator can see at a glance which signal fired."""
    title_hits = [m for m, f in folded_markers if f in folded_title]
    path_hits = [
        f'url:{m}' for m, f in folded_markers
        if f in folded_path and m not in title_hits
    ]
    return title_hits + path_hits


class GenreVerdict(NamedTuple):
    """Result of ``_is_rejected_genre``.

    ``genre``   — ``'video'`` / ``'event'`` / ``None`` (keep).
    ``markers`` — matched markers, surfaced in the [E037] ping so a false
                  positive is diagnosable at a glance.
    ``branch``  — WHICH rule fired (``'video_lead'`` / ``'video_np'`` /
                  ``'video_signals'`` / ``'event'`` / ``None``). Kept
                  separate from ``genre`` so the ACTION each rule triggers
                  is a one-place policy decision — see
                  ``_GENRE_BRANCH_ACTION``.
    """

    genre: Optional[str]
    markers: list
    branch: Optional[str]


#: What each content-gate branch DOES. Single source of policy: the
#: detector only reports which rule matched, ``job()`` looks the action up
#: here. Valid actions: ``'drop'`` (reject at intake, [E037], link pinned)
#: and ``'hold'`` (stage but park for operator approval, [E036] — the same
#: path posters take).
#:
#: Operator decision 2026-07-25 — «очевидные резать, спорные спрашивать»:
#:
#:   * ``video_lead``  → drop. «Vídeo: …» / «Watch: …» is an explicit
#:     headline convention; there is nothing to weigh up.
#:   * ``video_np``    → HOLD. Grammatical position is strong evidence but
#:     not proof (two review rounds found real reveals in this shape).
#:   * ``video_signals`` → HOLD. Two lexical signals, same caveat.
#:   * ``event``       → drop. The name+organisational-word bar is high
#:     confidence and the convention-exclusive reveal guard is verified
#:     independently, so «ивенты отсекаем» stands.
#:
#: The asymmetry is the whole argument: a wrong HOLD costs the operator one
#: button press, a wrong DROP is unrecoverable (the link is pinned in
#: processed_news and the article is never seen again). So only the branch
#: we are certain about is allowed to drop.
_GENRE_BRANCH_ACTION = {
    'video_lead': 'drop',
    'video_np': 'hold',
    'video_signals': 'hold',
    'event': 'drop',
}

#: Which HOLD reason a held article carries into the [E036] ping, so the
#: operator can see WHY they are being asked. ``'poster'`` comes from
#: ``_hold_for_review_reason``; the genre branches routed to 'hold' above
#: come in as ``'video'``.
_HOLD_REASON_POSTER = 'poster'
_HOLD_REASON_VIDEO = 'video'


def _hold_for_review_reason(entry, article):
    """Return the list of matched markers when the post's SUBJECT is a
    poster / catalog / packaging piece (truthy → HOLD for operator
    approval, [E036]); empty list otherwise.

    Prod incident 2026-07-25: t-hunted published «As fotos do último
    poster da Hot Wheels» — four sentences, 12 images, an unembeddable
    video — and the bot published it. The operator's instruction: do not
    publish this genre automatically, ASK. With no answer the article
    stays parked forever (`pending_articles.hold_reason`); silence is a
    decision, not a timeout.

    Rule: ONE marker in the title or the URL slug holds. Deliberately
    strict — a hold is one button press away from publishing, so the cost
    of a false hold is far below the cost of a false publish, and the
    operator asked for exactly this asymmetry.

    Scanned inputs: title + URL path only (see ``_content_gate_subject``).
    The BODY is not scanned on purpose: an ordinary car-reveal post that
    mentions the poster, the catalog spread and the new packaging in one
    paragraph is still a car-reveal post.

    Tolerates malformed input (non-dict entry/article, non-str fields,
    unparseable link) by returning ``[]`` rather than raising; the call
    site wraps it fail-open anyway.
    """
    _raw, folded_title, folded_path = _content_gate_subject(entry, article)
    return _content_gate_hits(folded_title, folded_path, _HOLD_TITLE_FOLDED)


def _is_rejected_genre(entry, article):
    """Return ``(genre, markers)`` when the post's SUBJECT is a genre the
    operator wants dropped outright at intake ([E037]), else ``(None, [])``.

    ``genre`` is ``'video'`` (video review / unboxing / "watch this") or
    ``'event'`` (convention / expo announcement). Markers are returned for
    the alert so a false positive is diagnosable at a glance.

    Both genres demand TWO independent signals, for the same reason: a
    single content word is not proof of genre, and a DROP is permanent and
    unrecoverable (unlike a HOLD, which costs one button press).

      * VIDEO — the video must be the SUBJECT of the post, not a word the
        post happens to use. Either:
          (A) SUBJECT POSITION — the genre word HEADS the title, followed
              by a separator («Vídeo: …», «Watch: …») or by a
              determiner/preposition so it heads a noun phrase («Unboxing
              the 2026 H case …», «Assista ao …»); or
          (B) TWO SIGNALS — a genre word plus a review-shaped co-marker
              (review / análise / assortment / abrimos …), or two DISTINCT
              genre words («Check the full video unboxing here …» — note
              that «Assista ao unboxing …» is NOT a branch-(B) example, it
              resolves earlier via (A2)'s noun-phrase form).
        Review round 1 (F1/F2): the previous single-marker rule dropped
        genuine car reveals that merely used the word — «Mattel drops
        video revealing the 2027 Corvette Z06», «Vídeo revela o novo
        Porsche 911 GT3 RS», «Unboxing surprises us: … Supra revealed».
        A leading genre word followed by a VERB is a news clause, which is
        exactly why branch (A) requires a separator or a function word.
        A post that merely EMBEDS a video was already safe (the body is
        never scanned) — this closes the TITLE-language hole.
      * EVENT — an event NAME **and** an ORGANIZATIONAL word, both in the
        subject. Two independent signals are required because a
        convention-exclusive CAR REVEAL is legitimate model news: the name
        of a convention says WHERE a casting was announced, not that the
        post is about the convention. Conversely the org words alone are
        ordinary release language ("2026 release dates").

    Reveal language ('revealing'/'revela'/'first look'/'unveils') is
    deliberately NOT used as a blanket negative signal. It cannot apply to
    branch (A) — the operator's own required case «Watch: FIRST LOOK at
    the 2027 HW Nationals mainline» carries reveal language and must still
    drop — and on branch (B) it would punch a hole through «Video review:
    … reveals …», a genuine video review. The two-signal bar already
    closes the reported false-drop class without a third, subtler rule
    whose failure mode (an undropped video review) is silent.

    Video is checked first; on a title that satisfies both, either verdict
    is a drop, so the order is a tie-break, not a policy. HOLD outranks
    BOTH — see ``job()``, which evaluates ``_hold_for_review_reason``
    first and skips this check entirely on a hold.

    Returns a ``GenreVerdict(genre, markers, branch)``. ``branch`` names
    WHICH rule fired, so the outcome of each rule is a policy decision
    made in ONE place (``_GENRE_BRANCH_ACTION``) rather than baked into
    the detector.

    Same malformed-input tolerance as ``_hold_for_review_reason``.
    """
    raw_title, folded_title, folded_path = _content_gate_subject(entry, article)

    # (A) Subject position — title-only (word order + punctuation, which a
    # slug does not preserve). Sufficient on its own.
    subject = _genre_video_subject_marker(raw_title, folded_title)
    video = _content_gate_hits(folded_title, folded_path, _GENRE_VIDEO_FOLDED)
    review = _content_gate_hits(
        folded_title, folded_path, _GENRE_VIDEO_REVIEW_FOLDED)
    if subject:
        marker, branch = subject
        return GenreVerdict('video', [marker] + video + review, branch)
    # (B) Two independent signals: a genre word plus either a review
    # co-marker or a second, distinct genre word.
    if video and (review or len(video) >= 2):
        return GenreVerdict('video', video + review, 'video_signals')

    names = _content_gate_hits(
        folded_title, folded_path, _GENRE_EVENT_NAME_FOLDED)
    org = _content_gate_hits(
        folded_title, folded_path, _GENRE_EVENT_ORG_FOLDED)
    if names and org:
        return GenreVerdict('event', names + org, 'event')

    return GenreVerdict(None, [], None)


#: Hard-block threshold for cross-source dedup (tech-spec Decision 7,
#: user-spec AC3). Articles whose ``similarity`` against any candidate in
#: the 7-day window meets or exceeds this value are dropped before
#: ``insert_pending`` and pinned in ``processed_news`` so the same URL is
#: not re-fetched on subsequent ticks (Decision 8).
_DEDUP_BLOCK_THRESHOLD = 0.50

#: How long an [E014] soft-flagged article is withheld from the queue so the
#: operator can actually use the «🚫 Не публиковать» button (operator decision
#: 2026-07-28). Before this, the flag and the first publish slot both landed at
#: intake time: on 2026-07-28 the ping went out at 10:00:10 and the article was
#: published at 10:00:17 — SEVEN SECONDS later. The operator pressed cancel at
#: 10:14 and got «Уже опубликовано, отменить нельзя». Silence still means
#: PUBLISH: the row simply becomes visible to the queue again once the delay
#: elapses, so an unavailable operator never costs the channel an article.
_DEDUP_DEFER_HOURS = 24

#: Soft-flag threshold for cross-source dedup (Decision 7, user-spec AC4).
#: Articles in ``[0.30, 0.50)`` pass through to ``insert_pending`` but
#: trigger a per-pair-rate-limited E014 ping so the operator can review.
_DEDUP_FLAG_THRESHOLD = 0.30

#: Candidate-fetch window for the tiered pair rule (tech-spec Decision 3):
#: a distinctive/broad pair blocks/flags against any row seen in the last
#: 30 days. The single ``_fetch_dedup_candidates`` call uses this window;
#: the set-overlap backstop derives its narrower subset in Python.
_DEDUP_PAIR_WINDOW_DAYS = 30

#: Window for the legacy set-overlap backstop (tech-spec Decision 5): the
#: backstop only ever compares rows from the last 7 days, a subset of the
#: 30-day fetch derived in Python (no second SQL round-trip).
_DEDUP_BACKSTOP_WINDOW_DAYS = 7


def _fetch_dedup_candidates(conn: sqlite3.Connection) -> list:
    """Single 30-day candidate fetch (pending + published) shared by BOTH
    dedup rules — the tiered pair rule (30-day window) and the set-overlap
    backstop (7-day subset derived in Python from these rows). Fetched at
    most once per gate invocation so the two rules never make a second SQL
    round-trip (tech-spec Decision 5; ~200 rows over 30 days is a cheap
    full scan, shipped §13.5).
    """
    return (
        pending_repo.list_recent_pending_fingerprints(
            conn, _DEDUP_PAIR_WINDOW_DAYS)
        + pending_repo.list_recent_published_fingerprints(
            conn, _DEDUP_PAIR_WINDOW_DAYS)
    )


def _pair_match(row: dict, shared_pairs: list, n_total: int) -> dict:
    """Build a gate ``match`` dict for a tiered pair-rule verdict.

    ``shared_pairs`` is the sorted list of shared ``"<model>|<series>|<tier>"``
    keys. The dict is compatible with both alert builders and carries a
    ``pairs`` key — its presence is what tells ``job()`` to pass ``pairs=`` to
    the Task-2 E015/E014 builders (which then render the matched-series block).
    A set-overlap match omits ``pairs`` so those builders fall back to the
    legacy overlap-percent / model-list rendering. ``models`` mirrors
    ``pairs`` for legacy-caller compatibility.
    """
    n_matches = len(shared_pairs)
    overlap_pct = int(round(100 * n_matches / n_total)) if n_total else 0
    return {
        'link': row.get('link'),
        'source_name': row.get('source_name') or 'other',
        'models': shared_pairs,
        'pairs': shared_pairs,
        'overlap_pct': overlap_pct,
        'n_matches': n_matches,
        'n_total': n_total,
    }


def _pair_rule_verdict(pairs: list, candidates: list):
    """Rule 1 body — the tiered series/theme pair rule over already-fetched
    ``candidates`` (30-day pending + published, NO same-source skip).

    Scan-and-remember: the first shared distinctive (``|D``) pair hard-blocks
    and stops the scan; the first shared broad (``|B``) pair is remembered as
    a soft flag but the scan CONTINUES so a later ``|D`` still wins. Returns
    ``('block', match)`` / ``('flag', match)`` when a shared pair fires, or
    ``('pass', None)`` when no candidate shares a pair (the gate then falls
    through to the empty short-circuit + set-overlap backstop). Candidates
    with a ``None``/non-dict fingerprint are skipped silently.
    """
    new_pairs = set(pairs)
    flag_match = None
    for row in candidates:
        cand_fp = row.get('model_fingerprint')
        if not isinstance(cand_fp, dict):
            # NULL / pre-feature / malformed row — skip silently.
            continue
        cand_pairs = set(cand_fp.get('pairs') or [])
        shared = new_pairs & cand_pairs
        if not shared:
            continue
        shared_sorted = sorted(shared)
        n_total = len(new_pairs | cand_pairs)
        if any(p.endswith('|D') for p in shared):
            # Distinctive → hard block. Stop scanning: a |D verdict can
            # never be improved on, and a remembered |B must not survive.
            return ('block', _pair_match(row, shared_sorted, n_total))
        if flag_match is None:
            # First broad match — remember but KEEP scanning so a later
            # |D from another candidate can still win over this |B.
            flag_match = _pair_match(row, shared_sorted, n_total)
    if flag_match is not None:
        return ('flag', flag_match)
    return ('pass', None)


def _set_overlap_backstop_verdict(fingerprint, strict: list,
                                  candidates: list, new_source):
    """Rule 2 body — the legacy set-overlap backstop (≥0.50 block /
    ``[0.30, 0.50)`` flag), CROSS-SOURCE ONLY, over the 7-day subset of the
    single 30-day ``candidates`` fetch (subset derived here in Python — no
    second SQL round-trip). SQLite's ``CURRENT_TIMESTAMP`` columns are UTC
    'YYYY-MM-DD HH:MM:SS', so the lexical ``<`` cutoff compare is
    chronological. Same-source candidates are skipped (Decision 9 reversed
    2026-06-14). Returns ``('block'|'flag', match)`` or ``('pass', None)``.
    """
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=_DEDUP_BACKSTOP_WINDOW_DAYS)
    ).strftime('%Y-%m-%d %H:%M:%S')

    best_sim = 0.0
    best_row = None
    for row in candidates:
        ts = row.get('fetched_at') or row.get('published_at')
        if not ts or str(ts) < cutoff:
            # Outside the backstop's 7-day window (or undatable) — the legacy
            # backstop only ever saw 7-day rows; keep it that way.
            continue
        if new_source and row.get('source_name') == new_source:
            # Same-source candidate — never deduped by the backstop
            # (Decision 9 reversed 2026-06-14).
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


def _check_cross_source_dedup(article: dict, fingerprint: dict,
                              conn: sqlite3.Connection, new_source=None):
    """Decide block / flag / pass for ``fingerprint`` against recent rows.

    Two rules run in strict order — the tiered pair rule FIRST, the legacy
    set-overlap backstop only on a pair-rule pass:

      1. **Tiered pair rule** (series/theme, dedup-model-series feature; only
         when ``news_bot.DEDUP_SERIES_ENABLED`` and the article has ``pairs``).
         Scans ALL 30-day candidates (pending + published) with NO same-source
         skip — a distinctive (``|D``) shared pair means the SAME casting +
         franchise even from the same outlet ("more photos"), so it blocks
         any-source. Scan-and-remember: the first shared ``|D`` pair hard-blocks
         and stops the scan; a shared broad (``|B``) pair is only remembered as
         a soft flag and the scan CONTINUES so a later ``|D`` still wins. Both a
         ``block`` and a ``flag`` verdict are TERMINAL — the backstop does NOT
         run, so an article can never earn two verdicts / two admin pings in
         one tick.
      2. **Set-overlap backstop** (legacy ≥50% block / ``[0.30,0.50)`` flag,
         7-day window, CROSS-SOURCE ONLY). Reached only when the pair rule did
         not fire (toggle off, no ``pairs``, or no shared pair). Unchanged
         behaviour — including the same-source skip (Decision 9 reversed
         2026-06-14: within-source republishes don't happen; comparing them
         only yields false positives). The 7-day subset is derived in Python
         from the single 30-day fetch (no second SQL round-trip).

    Returns ``('block', match)`` / ``('flag', match)`` / ``('pass', None)``.
    ``match`` carries ``link`` / ``source_name`` / ``models`` / ``overlap_pct``
    / ``n_matches`` / ``n_total`` (+ ``pairs`` for pair-rule verdicts) — the
    keys the E015/E014 builders read.

    Back-compat & fail-safe: ``fingerprint`` and every candidate fingerprint
    are read with ``.get('pairs') or []`` etc. so pre-feature rows (no
    ``pairs``/``series`` keys) never ``KeyError``; candidates with a
    ``None``/non-dict fingerprint are skipped silently. A crash anywhere here
    is caught by the gate's degraded-mode ``try/except`` in ``job()`` (AC9).
    """
    fp_is_dict = isinstance(fingerprint, dict)
    strict = (fingerprint.get('strict') or []) if fp_is_dict else []
    series = (fingerprint.get('series') or []) if fp_is_dict else []
    pairs = (fingerprint.get('pairs') or []) if fp_is_dict else []

    # Lazy single 30-day fetch, reused by whichever rule needs it.
    candidates = None

    # ---- Rule 1: tiered pair rule (30-day window, ANY source) ----
    if DEDUP_SERIES_ENABLED and pairs:
        candidates = _fetch_dedup_candidates(conn)
        decision, match = _pair_rule_verdict(pairs, candidates)
        if decision != 'pass':
            # block / flag are TERMINAL — the backstop never runs, so an
            # article can never earn two verdicts / two admin pings per tick.
            return (decision, match)

    # ---- Empty short-circuit (AC8, re-gated to strict AND series) ----
    # Reached only AFTER the pair scan above has run (or been skipped). The
    # ``and not series`` clause is what makes a franchise tie-in with empty
    # ``strict`` reach that scan at all: an empty-``strict``-only short-circuit
    # would return before Rule 1 was ever consulted, which was the bug that let
    # pop-culture dupes through.
    #
    # Since the 2026-07-28 theme-only precision fix a broad-line article can
    # arrive here with non-empty ``series`` but EMPTY ``pairs`` (Rule 1 skipped).
    # It then falls through to the backstop, which is a guaranteed no-op for it:
    # ``similarity()`` returns 0.0 at its AC6 empty-``strict`` guard. Correct,
    # but it costs one useless 30-day candidate fetch — a cheap ~200-row scan on
    # a daily cron, left alone deliberately rather than adding a second
    # short-circuit that would fork the AC8 contract.
    if not strict and not series:
        return ('pass', None)

    # ---- Rule 2: set-overlap backstop (7-day subset, CROSS-SOURCE ONLY) ----
    # Reached only on a pair-rule pass. Reuses the single 30-day fetch.
    if candidates is None:
        candidates = _fetch_dedup_candidates(conn)
    return _set_overlap_backstop_verdict(fingerprint, strict, candidates,
                                         new_source)


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


# Per-source image cap, keyed by the same `source_name` values as
# NETLOC_TO_SOURCE. Built FROM THE PARSER MODULES' OWN CONSTANTS on purpose —
# numbers retyped here would drift from the parsers silently, and nothing
# would catch it.
#
# Why the map exists at all: the per-source limits only ever sliced the flat
# `images` list, and `telegraph_publisher` ignores that list entirely once
# `blocks` is non-empty. Measured on 14 real articles, all four lamley posts
# exceed their limit of 10 (14, 41, 48, 50 images); t-hunted peaks at 27
# against a limit of 30.
#
# `t_hunted_source._IMAGE_LIMIT` is private. Referencing it is the lesser
# evil: the alternative is retyping 30 here, and adding a public alias would
# mean editing a file that Task 7 of source-formatting-parity owns in the
# same wave. If a public name is wanted, that is a request to that task.
SOURCE_IMAGE_LIMITS = {
    't-hunted':      t_hunted_source._IMAGE_LIMIT,
    'lamley':        lamley_source.IMAGE_LIMIT,
    'orangetrack':   orangetrack_source.IMAGE_LIMIT,
    'autoevolution': autoevolution_source.MAX_IMAGES,
}


def _blocks_if_aligned(link, blocks, paragraphs):
    """Return `blocks` when it lines up with `paragraphs`, else None.

    `_llm_common` pairs the two lists POSITIONALLY: `_build_user_message`
    walks the blocks and takes the next entry from `paragraphs` for each
    patchable one, and `_patch_text_with_ru_paragraphs` does the same with the
    Russian paragraphs coming back. Off by one and the translations splice
    shifted, so the tail block reaches the channel in the source language —
    the 2026-05-06 outage. Both sides swallow the shortfall SILENTLY
    (`except StopIteration: pass` and a bare `break`), which is why a runtime
    guard exists at all: a test only covers the articles somebody wrote an
    example for.

    Dropping the blocks costs the article its formatting. That is the
    deliberate trade (Decision 4): it publishes as plain text rather than
    published half-translated.

    THE COUNT COMES FROM `_llm_common._PATCHED_TEXT_BLOCK_TYPES` — the tuple
    both sides of the pairing actually read. A literal retyped here would be
    exactly the kind of drifted copy this guard is meant to catch. The
    same-named tuples in the four engine modules are a DIFFERENT thing: they
    gate the caption-translation pass, not the paragraph pairing.

    ZERO patchable blocks is not a mismatch. With none, no positional pairing
    happens at all — and firing here would drop the blocks of an orangetrack
    video-only post, which synthesizes `paragraphs = [title]`, and take the
    video off the page (an AC10 violation on a working source).

    Fail-open on internal error, same contract as the promo filter, the
    content gate and the dedup gate: a broken check must not cost the article.

    No operator ping (Decision 3b). The WARNING below is the ONLY trace — the
    drop writes no `last_error` and appears in no recap — so it names the
    article AND both counts. A line like "mismatch, dropping blocks" would
    leave the operator with nothing to act on.
    """
    if not blocks:
        return blocks
    try:
        patchable = sum(
            1 for b in blocks
            if isinstance(b, dict)
            and b.get("type") in _llm_common._PATCHED_TEXT_BLOCK_TYPES
        )
        if patchable == 0:
            return blocks
        n_paragraphs = len(paragraphs or [])
        if patchable != n_paragraphs:
            logger.warning(
                "[align] blocks/paragraphs mismatch for %s: "
                "patchable_blocks=%d paragraphs=%d — dropping blocks, "
                "the article publishes as plain text",
                link, patchable, n_paragraphs,
            )
            return None
        return blocks
    except Exception:
        logger.exception(
            "[align] alignment check failed for %s — keeping blocks", link,
        )
        return blocks


def _image_limit_for_source(source_name):
    """Resolve the image cap for `source_name`; None means no cap.

    Fail-open, like the promo filter, the content gate and the dedup gate: an
    unknown or missing source publishes UNCAPPED rather than not at all. It
    does log a WARNING, because blocks arriving from a source we cannot name
    means something is wrong further upstream.
    """
    if not source_name:
        logger.warning(
            "[fallback] no source_name on the row — publishing without an image cap"
        )
        return None
    limit = SOURCE_IMAGE_LIMITS.get(source_name)
    if limit is None:
        logger.warning(
            f"[fallback] unknown source_name={source_name!r} — "
            "publishing without an image cap"
        )
    return limit


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

    Spec: work/completed/telegraph-pipeline/post-format.md.
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

    It also clears the hold counters, which is what makes ``hold_count`` mean
    "holds IN A ROW". A working LLM proves the holds those rows collected were
    global; leaving them banked would let an innocent article cross ``HOLD_CAP``
    weeks later, on the first bad day, and get an [E038] blaming it for someone
    else's outage. Rows already at the cap keep their marker — see
    ``pending_repo.reset_hold_counts_below``.
    """
    try:
        cleared = pending_repo.reset_hold_counts_below(HOLD_CAP)
        if cleared:
            logger.info(
                f"[recovery] LLM answered — cleared hold_count on {cleared} "
                f"row(s) below the cap."
            )
    except Exception as reset_err:  # noqa: BLE001 — bookkeeping, never raise
        logger.error(
            f"[recovery] reset_hold_counts_below failed: "
            f"{sanitize_error_message(reset_err)}"
        )

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
    except ClaudeOutageError as exc:
        # API-level outage (402 / 429 / 5xx / auth / network). Advance the
        # 2-ping/2h notification state machine so the operator is kept
        # informed, then HOLD: re-raise WITHOUT publishing. The row stays
        # in pending; the slot loop (``job()``) catches this, does NOT
        # strike, and the next slot retries the LLM. No Google fallback —
        # we wait for the LLM rather than ship a machine translation.
        #
        # The CAUSE is logged here on purpose. A held row never reaches
        # ``increment_attempt`` (no ``last_error`` written) and never enters
        # the [E034] recap — both are 'failed'-branch only — so the [E010]/
        # [E011]/[E012] pings are the operator's ONLY other signal, and those
        # are generic «LLM недоступна». Without this line an out-of-credits
        # 402 and a dead network look identical in the journal — which has
        # already cost one wrong diagnosis (2026-06-10: E011 fired, every
        # external check was green, and the real cause was server-side DNS
        # loss found only in the logs).
        logger.warning(
            f"[hold] LLM outage for {link}: {type(exc).__name__}: "
            f"{sanitize_error_message(exc)} — holding article, will retry "
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
            image_limit=_image_limit_for_source(row.get('source_name')),
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
        # In-flight cancel guard (audit CA-1a): the operator may press
        # «🚫 Не публиковать» while THIS publish is mid-flight — the LLM +
        # Telegraph steps above take minutes, and ``resolve_dedup_callback``'s
        # cancel branch deletes the pending row via ``skip_pending`` and
        # answers «✅ Отменено оператором». Re-check the row right before
        # the LAST irreversible step (the channel teaser; an orphan
        # Telegraph page is harmless): row gone → honour the cancel and
        # abort. Return True mirrors the idempotency guard above —
        # success-without-publish, so the slot loop neither strikes the
        # row nor treats this as a failure. The teaser-already-sent retry
        # path deliberately bypasses this guard: with a post in the
        # channel, completing ``move_to_published`` (which dozapis-guards
        # a missing row itself, CA-1b) is the consistent outcome.
        if pending_repo.get_pending(link) is None:
            logger.info(
                f"[review-cancel] {link} pending row vanished mid-publish "
                f"(operator cancelled via [E014] button) — aborting before "
                f"the Telegram teaser; no channel post, no strike"
            )
            return True
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
        except ClaudeOutageError as exc:
            # The exception is handed back, not dropped: the slot loop needs
            # the cause for the [E038] ping when this row hits ``HOLD_CAP``.
            # It is the only record of WHY — a held row writes no `last_error`
            # and never enters the [E034] recap.
            return 'held', exc
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

    # Once-a-day heads-up if OpenRouter credits are running low (non-blocking).
    # Placed AFTER the crash-loop guard so a restart storm is dampened by the
    # guard's sleep rather than emitting one [E019] per rapid restart.
    _maybe_alert_openrouter_balance()

    # ------------------------------------------------------------------
    # Step (b1): fetch all sources via the SOURCES registry.
    # One source failing must not abort the tick — sanitise the error
    # string and surface it to the admin, then carry on.
    # ------------------------------------------------------------------
    # Intake-funnel diagnostic (watchdog): a per-tick breakdown of where
    # articles disappear on the way to being staged. Plain ints only — these
    # counters can never raise, and the ping builders render them defensively
    # (see admin_alerts._format_funnel). Covers INTAKE/STAGING (step b) only.
    funnel = {
        'sources_fetched': 0,     # len(all_entries) after the b1 fetch loop
        'sources_failed': 0,      # SOURCES that threw in the b1 loop
        'new_count': 0,           # len(new_entries) after both b2 filters
        'dropped_no_article': 0,  # b3: no link OR no article/paragraphs
        'dropped_checklist': 0,   # b3: text-only checklist reject
        'dropped_promo': 0,       # b3: shop-promo/ad reject (E035)
        'dropped_genre': 0,       # b3: content-gate genre drop — video/event (E037)
        'held_for_review': 0,     # b3: content-gate HOLD — staged but parked (E036)
        'dropped_dedup_block': 0, # b3: cross-source hard-block (E015)
        'dedup_degraded': 0,      # b3: dedup crashed → degraded (E016), still attempts to stage
        'staged': 0,              # == inserted
    }

    all_entries = []
    for fetcher in SOURCES:
        fetcher_name = getattr(fetcher, '__name__', repr(fetcher))
        try:
            items = fetcher(notifier=send_admin_notification) or []
        except Exception as exc:
            funnel['sources_failed'] += 1
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
    funnel['sources_fetched'] = len(all_entries)
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
    funnel['new_count'] = len(new_entries)

    # ------------------------------------------------------------------
    # Step (b3): stage each accepted entry into pending_articles.
    # ``fetch_full_article`` network-failure → skip. The repo owns all
    # JSON serialisation — we pass Python lists/dicts verbatim.
    # ------------------------------------------------------------------
    inserted = 0
    for entry in new_entries:
        link = entry.get('link')
        if not link:
            funnel['dropped_no_article'] += 1
            logger.warning("Entry has no link, skipping.")
            continue

        article = fetch_full_article(entry)
        if not article or not article.get('paragraphs'):
            funnel['dropped_no_article'] += 1
            logger.warning(f"No article data for {link}, skipping")
            continue

        # Reject bare "checklist" posts (title says checklist + body is
        # near-empty). Orangetrack publishes these often; subscribers
        # don't want a translated bullet list. Reviews that mention a
        # checklist in the title but have substantive body text pass.
        if _is_text_only_checklist(entry, article):
            funnel['dropped_checklist'] += 1
            logger.info(
                "Skipping checklist-only article (no editorial body): %s",
                link,
            )
            continue

        # Reject shop-promo/ad posts BEFORE staging — zero LLM tokens
        # spent (prod incident 2026-07-25: t-hunted published a pure
        # store ad and the bot translated + posted it). Scoring rule in
        # _is_promo_article; the matched markers go into the [E035]
        # alert so a false positive is diagnosable at a glance. Placed
        # before the (more expensive) dedup gate. Unlike the checklist
        # drop the link is pinned in processed_news (E015 precedent):
        # a shop ad stays a shop ad — without the pin the same post
        # would be re-fetched and re-alerted every daily tick.
        try:
            promo_markers = _is_promo_article(entry, article)
        except Exception as exc:
            # Fail-open, mirroring the dedup gate's Decision 12 / AC9
            # contract: an intake FILTER must never crash the tick or
            # block publishing. A crash here would also land BEFORE
            # mark_processed, so the same entry would be refetched and
            # crash-loop the daemon on restart (audit SEC-PROMO-1).
            logger.error(
                "promo filter failed for %s, treating as not-promo: %s",
                link, sanitize_error_message(exc),
            )
            promo_markers = []
        if promo_markers:
            funnel['dropped_promo'] += 1
            logger.info(
                "[E035] Promo article dropped %s (markers: %s)",
                link, ", ".join(promo_markers),
            )
            mark_processed(
                link,
                article.get('title') or entry.get('title') or '',
                entry.get('published') or entry.get('pub_date') or '',
            )
            try:
                send_admin_notification(
                    admin_alerts.alert_promo_blocked(
                        link,
                        article.get('title') or entry.get('title') or '',
                        promo_markers,
                    )
                )
            except Exception as notify_err:
                logger.error(
                    "Failed to send E035 notification: %s", notify_err,
                )
            continue

        # --------------------------------------------------------------
        # CONTENT GATE (2026-07-25). Three genres the operator does not
        # want published automatically, two verdicts:
        #
        #   HOLD  (poster / catalog / packaging) — stage the row but park
        #         it (hold_reason), ping [E036] with approve/reject
        #         buttons. NO answer = it never publishes.
        #   DROP  (video review / event announcement) — reject right here
        #         like a promo post, ping [E037].
        #
        # PRECEDENCE: hold is evaluated FIRST and short-circuits the genre
        # check. The incident post is a poster post that also says «no
        # vídeo abaixo» — it must reach the operator for a decision, not
        # be silently binned as a video post.
        #
        # The dedup gate still runs BELOW this and can hard-block a held
        # candidate before it is ever staged. That ordering is deliberate:
        # a poster post we have already covered from another source is a
        # duplicate, and there is nothing for the operator to decide.
        #
        # Both detectors are wrapped fail-open, exactly like the promo
        # filter and the dedup gate (Decision 12 / AC9, audit
        # SEC-PROMO-1): an intake FILTER must never crash the tick, and a
        # crash here would land BEFORE mark_processed, so the same entry
        # would be refetched and crash-loop the daemon on every restart.
        # Fail-open means "publish as usual" for BOTH — a detector fault
        # must not silently park articles either.
        # --------------------------------------------------------------
        hold_reason_kind = _HOLD_REASON_POSTER
        try:
            hold_markers = _hold_for_review_reason(entry, article)
        except Exception as exc:
            logger.error(
                "content-gate hold check failed for %s, treating as "
                "not-held: %s", link, sanitize_error_message(exc),
            )
            hold_markers = []

        if not hold_markers:
            try:
                verdict = _is_rejected_genre(entry, article)
            except Exception as exc:
                logger.error(
                    "content-gate genre check failed for %s, treating as "
                    "not-rejected: %s", link, sanitize_error_message(exc),
                )
                verdict = GenreVerdict(None, [], None)
            if verdict.genre:
                # What the matched branch DOES is policy, looked up in one
                # place — the detector only says which rule fired. An
                # unknown branch falls back to 'drop' (today's behaviour
                # for every branch).
                action = _GENRE_BRANCH_ACTION.get(verdict.branch, 'drop')
                if action == 'hold':
                    # Route this branch into the same HOLD path posters
                    # take: staged but parked, [E036] with buttons. Falls
                    # through to the shared row assembly below. The reason
                    # kind travels with it so the ping explains WHY the
                    # operator is being asked (a suspected video review
                    # reads very differently from a poster dump).
                    hold_markers = list(verdict.markers)
                    hold_reason_kind = _HOLD_REASON_VIDEO
                    logger.info(
                        "[content-gate] %s branch=%s → HOLD for %s "
                        "(markers: %s)",
                        verdict.genre, verdict.branch, link,
                        ", ".join(verdict.markers),
                    )
                else:
                    funnel['dropped_genre'] += 1
                    logger.info(
                        "[E037] Genre dropped %s genre=%s branch=%s "
                        "(markers: %s)",
                        link, verdict.genre, verdict.branch,
                        ", ".join(verdict.markers),
                    )
                    # Pin the link (E015/E035 precedent): a video review
                    # stays a video review — without the pin the same post
                    # would be re-fetched and re-alerted every daily tick.
                    mark_processed(
                        link,
                        article.get('title') or entry.get('title') or '',
                        entry.get('published') or entry.get('pub_date') or '',
                    )
                    try:
                        send_admin_notification(
                            admin_alerts.alert_genre_blocked(
                                link,
                                article.get('title')
                                or entry.get('title') or '',
                                verdict.genre,
                                verdict.markers,
                            )
                        )
                    except Exception as notify_err:
                        logger.error(
                            "Failed to send E037 notification: %s", notify_err,
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
        # Set by the soft-flag branch below; reaches the row dict so the
        # article is staged but withheld from the queue for a day.
        dedup_defer_until = None
        new_source = entry.get('source_name') or _resolve_source_name(link)
        try:
            dedup_conn = pending_repo._connect()
            try:
                fp = model_extractor.extract_fingerprint(article)
                decision, match = _check_cross_source_dedup(
                    article, fp, dedup_conn, new_source,
                )

                if decision == 'block':
                    funnel['dropped_dedup_block'] += 1
                    logger.info(
                        "[E015] Cross-source hard-block %s; matched %s "
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
                                pairs=match.get('pairs'),
                            )
                        )
                    except Exception as notify_err:
                        logger.error(
                            "Failed to send E015 notification: %s", notify_err,
                        )
                    continue

                if decision == 'flag':
                    # Withhold from the queue for a day REGARDLESS of the
                    # alert rate-limit: the delay protects the article, the
                    # rate-limit only protects the operator's notifications.
                    # Tying them together would publish a flagged dupe
                    # immediately just because a similar pair pinged recently.
                    dedup_defer_until = (
                        datetime.now(timezone.utc)
                        + timedelta(hours=_DEDUP_DEFER_HOURS)
                    ).strftime('%Y-%m-%d %H:%M:%S')
                    alerted = not pending_repo.is_pair_rate_limited(
                        dedup_conn, link, match['link'],
                    )
                    # Full logging: record EVERY soft-flag decision (link +
                    # [E014] + match) so a dedup flag is diagnosable straight
                    # from the logs — even when the per-pair 7-day alert
                    # rate-limit (AC5) suppresses the Telegram ping.
                    logger.info(
                        "[E014] Cross-source soft-flag %s; matched %s "
                        "(overlap %d%%, %s->%s)%s",
                        link, match['link'], match['overlap_pct'],
                        new_source, match['source_name'],
                        "" if alerted else " (alert rate-limited)",
                    )
                    if alerted:
                        try:
                            # dedup-review-buttons: when the LISTENER
                            # gate is open (flag + bot token + numeric
                            # admin — audit SEC-A8-1: same effective
                            # gate as the listener, so we never mint
                            # tokens / render buttons nothing will ever
                            # serve), mint a short token, persist
                            # token→link BEFORE the send (a button press
                            # must never race an unwritten token), and
                            # attach the two-button review keyboard.
                            # Gate closed → kb stays None and the call
                            # is identical to the pre-feature behaviour.
                            # Mint/put live INSIDE this try so a
                            # storage/build fault logs as a failed E014
                            # ping instead of breaking the "dedup never
                            # blocks publishing" contract.
                            kb = None
                            if _review_listener_enabled():
                                token = secrets.token_urlsafe(9)
                                pending_repo.put_review_token(
                                    token, link,
                                    kind=pending_repo.
                                    REVIEW_TOKEN_KIND_DEDUP,
                                )
                                kb = admin_alerts.build_dedup_review_keyboard(
                                    token,
                                )
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
                                    pairs=match.get('pairs'),
                                    # «Что сделать» must match what the
                                    # operator actually sees: derive it from
                                    # the SAME kb we are about to attach, not
                                    # from a second flag read — so the advice
                                    # can never promise buttons that are not
                                    # rendered (gate closed, or the mint above
                                    # left kb None).
                                    buttons_enabled=kb is not None,
                                ),
                                reply_markup=kb,
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
            funnel['dedup_degraded'] += 1
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
            # Runtime alignment guard (Decision 4). Last point where both
            # final lists sit side by side before the row hits the DB, and
            # compared against the SAME paragraph list that goes into it.
            'blocks': _blocks_if_aligned(
                link, article.get('blocks'), article.get('paragraphs') or [],
            ),
            'pub_date': entry.get('published') or entry.get('pub_date') or '',
            'model_fingerprint': fp,
            # Cross-source dedup soft flag: staged now, invisible to
            # list_pending/count_pending until the timestamp passes, then
            # published automatically unless the operator cancelled it.
            'publish_after': dedup_defer_until,
            # Content gate: a non-NULL hold_reason parks the row — staged,
            # but invisible to list_pending/count_pending until approved.
            # Stored as the human-readable marker list so [E036] can be
            # rebuilt from the row alone.
            'hold_reason': ", ".join(hold_markers) if hold_markers else None,
        }
        try:
            if pending_repo.insert_pending(row):
                inserted += 1
                if hold_markers:
                    # HELD: ask the operator. Sent only AFTER the row is
                    # committed, so the token can never point at a row
                    # that does not exist. Best-effort like every other
                    # ping — a Telegram failure must not undo the staging
                    # (the row stays parked and shows up in the daily
                    # «На утверждении: N» line, which is exactly the
                    # backstop this ping needs).
                    funnel['held_for_review'] += 1
                    logger.info(
                        "[E036] Held for review %s (markers: %s)",
                        link, ", ".join(hold_markers),
                    )
                    try:
                        # Same gate as the E014 send site (audit
                        # SEC-A8-1): mint a token and render buttons only
                        # when a listener with the identical effective
                        # config would serve them — no dead buttons, no
                        # orphan tokens. Mint/put live INSIDE the try so a
                        # storage fault logs as a failed ping rather than
                        # breaking intake.
                        kb = None
                        if _review_listener_enabled():
                            token = secrets.token_urlsafe(9)
                            pending_repo.put_review_token(
                                token, link,
                                kind=pending_repo.REVIEW_TOKEN_KIND_HOLD,
                            )
                            kb = admin_alerts.build_hold_review_keyboard(token)
                        send_admin_notification(
                            admin_alerts.alert_held_for_review(
                                link,
                                article.get('title')
                                or entry.get('title') or '',
                                hold_markers,
                                # Why the operator is being asked — poster
                                # dump vs suspected video review.
                                reason=hold_reason_kind,
                                # Derived from the SAME kb about to be
                                # attached, so «Что сделать» can never
                                # promise a button that is not there.
                                buttons_enabled=kb is not None,
                            ),
                            reply_markup=kb,
                        )
                    except Exception as notify_err:
                        logger.error(
                            "Failed to send E036 notification: %s", notify_err,
                        )
            else:
                # UNIQUE conflict — another prep tick raced us; expected.
                logger.info(f"Pending row already exists for {link} — skipped.")
        except Exception as exc:
            safe = sanitize_error_message(exc)
            logger.error(f"insert_pending failed for {link}: {safe}")

    funnel['staged'] = inserted
    # One structured, greppable funnel line per tick — pinpoints the intake
    # stage where articles vanished on a quiet day (E009). Plain ints, so this
    # cannot raise.
    logger.info(
        "[funnel] sources=%d(failed=%d) new=%d "
        "dropped(no_article=%d,checklist=%d,promo=%d,genre=%d,dedup_block=%d,"
        "dedup_degraded=%d) held=%d staged=%d",
        funnel['sources_fetched'], funnel['sources_failed'],
        funnel['new_count'], funnel['dropped_no_article'],
        funnel['dropped_checklist'], funnel['dropped_promo'],
        funnel['dropped_genre'], funnel['dropped_dedup_block'],
        funnel['dedup_degraded'], funnel['held_for_review'],
        funnel['staged'],
    )

    # ------------------------------------------------------------------
    # Step (c): compute today's publish slots.
    # ``compute_fixed_slots`` returns (slots, carry_over) for the three
    # fixed daily times (10:00/15:00/19:30 МСК, operator pacing 2026-06-13).
    # N=0 → empty list, no admin ping (Step (d) guard), no loop (Step (e)
    # guard). The function already bounds the result to ≤3 slots, so the old
    # MAX_DAILY_POSTS trim is no longer needed.
    # ------------------------------------------------------------------
    now_msk = datetime.now(MSK_TZ)
    # ``count_pending`` counts PUBLISHABLE rows only — content-gate holds
    # are excluded, so a parked poster post never buys the day an extra
    # slot it can never fill, and never inflates the `> 50` backlog alarm
    # with rows the queue cannot drain on its own. The operator still sees
    # them: the held backlog goes into the plan ping as its own
    # «На утверждении: N» line (read below).
    queue_size = pending_repo.count_pending()
    # Rows deferred by the hold cap are NOT in ``queue_size`` — they are not
    # publishable at this instant. But the slot list is computed ONCE for the
    # whole day, so sizing it on ``queue_size`` alone would hand a tick that
    # starts fully deferred zero slots and skip the day entirely — including
    # the moment a 24 h window elapses a few hours in. Over-allocating is safe:
    # the loop breaks as soon as ``list_pending()`` comes back empty.
    # Deliberately NOT folded into ``count_pending``: everything else that
    # reads it (the `> 50` backlog alarm, the plan ping's «В очереди») means
    # "publishable right now", and a deferred row is not.
    try:
        deferred_backlog = pending_repo.count_deferred()
    except Exception as exc:
        logger.error(
            f"count_deferred failed, sizing slots on the publishable queue "
            f"only: {sanitize_error_message(exc)}"
        )
        deferred_backlog = 0
    slots, carry_over = compute_fixed_slots(
        queue_size + deferred_backlog, now_msk,
    )

    # Held backlog for the plan ping. Never blocks the tick: with the
    # «нет ответа = не публикуем» rule nothing else surfaces a forgotten
    # hold, but a DB hiccup here must not cost the heartbeat.
    try:
        held_count = len(pending_repo.list_held())
    except Exception as exc:
        logger.error(
            f"Failed to read the held backlog: {sanitize_error_message(exc)}"
        )
        held_count = 0

    # ------------------------------------------------------------------
    # Step (d): admin ping with plan-of-day. Always sent — operator wants a
    # heartbeat that confirms the cron tick fired, even on quiet days when
    # there are no new articles and the queue is empty.
    # Quiet day: single-line «🟢 Бот сработал, новых статей нет.»
    # Busy day:  multi-line columnar «🟢 План на сегодня — …»
    # Backlog warning fires as a separate ping at queue_size > 50 (AC20).
    # ------------------------------------------------------------------
    # Build the ping. The funnel renderers already fail safe internally, but
    # wrap the BUILD too (belt-and-suspenders): a formatting bug in the funnel
    # path must never break the tick — fall back to the no-funnel legacy call.
    try:
        if queue_size == 0 and inserted == 0:
            plan_msg = admin_alerts.alert_quiet_day(
                funnel=funnel, held_count=held_count,
                deferred_count=deferred_backlog)
        else:
            plan_msg = admin_alerts.alert_plan_of_day(
                inserted, queue_size, slots, carry_over, funnel=funnel,
                held_count=held_count, deferred_count=deferred_backlog,
            )
    except Exception as build_err:
        logger.error(
            f"Failed to build plan-of-day ping with funnel, falling back to "
            f"legacy: {sanitize_error_message(build_err)}"
        )
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
    # Per-slot outcome counters for the end-of-tick PUBLISH RECAP ([E034],
    # companion to the E008/E009 intake funnel). All plain ints — increment-only
    # and cannot raise. ``recap_failures`` keeps a de-duped, capped list of
    # ``(link, sanitized_reason)`` for the 'failed' outcomes (reason already run
    # through ``sanitize_error_message`` — never raw text, never secrets).
    published_count = 0
    held_count = 0
    failed_count = 0
    moved_to_failed_count = 0
    deferred_count = 0
    recap_failures = []
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
            held_count += 1
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
            # Hold cap: a hold never strikes, and the next slot re-reads
            # ``list_pending()[0]`` — so without a bound one permanently-failing
            # row blocks every article behind it, quietly (outage pings stop at
            # ping_count >= 3). Past HOLD_CAP the row steps aside for
            # HOLD_DEFER_HOURS and the queue moves on. It is NOT struck:
            # nothing is lost, it returns to the head when the window elapses.
            # Wrapped: a bookkeeping failure must not change the outcome of the
            # slot — the article is already correctly held either way.
            try:
                holds = pending_repo.increment_hold(link)
            except Exception as repo_err:
                logger.error(
                    f"increment_hold failed for {link}: "
                    f"{sanitize_error_message(repo_err)}"
                )
                holds = 0
            if holds >= HOLD_CAP:
                safe_hold = (
                    sanitize_error_message(err) if err else 'причина не записана'
                )
                until = (
                    datetime.now(timezone.utc)
                    + timedelta(hours=HOLD_DEFER_HOURS)
                ).strftime('%Y-%m-%d %H:%M:%S')
                # Two separate try blocks on purpose. Nesting the ping inside
                # the defer's would mean a repo failure produced NEITHER the
                # defer NOR the ping — the silent block [E038] exists to end.
                try:
                    deferred = pending_repo.defer_publish(link, until)
                except Exception as repo_err:
                    deferred = False
                    logger.error(
                        f"defer_publish failed for {link}: "
                        f"{sanitize_error_message(repo_err)} — row stays at "
                        f"the head, the next slot retries the defer"
                    )
                if deferred:
                    deferred_count += 1
                    logger.warning(
                        f"[slot {idx}/{len(slots)}] {link} held {holds}× in a "
                        f"row — deferring {HOLD_DEFER_HOURS}h so the queue can "
                        f"move. Cause: {safe_hold}"
                    )
                try:
                    # Ping only on a real defer. ``deferred`` is False either
                    # because the row vanished (the review listener deleted it
                    # — nothing to report) or because the write raised (already
                    # logged above; the row keeps the head and the next slot
                    # retries). In both cases [E038]'s text — «отложена, вернётся
                    # сама» — would be false.
                    #
                    # Rate-limited GLOBALLY: in a sustained stall many rows
                    # cross the cap in sequence, and a ping each would recreate
                    # the noise the outage machine's ping_count>=3 cutoff
                    # exists to prevent. The daily [E008]/[E009] «Отложено: N»
                    # line carries the running total, so suppressing the extra
                    # pings loses no information the operator needs.
                    if deferred and not pending_repo.is_hold_cap_ping_rate_limited(
                        HOLD_CAP_PING_WINDOW_HOURS
                    ):
                        send_admin_notification(
                            admin_alerts.alert_hold_cap_reached(
                                link, row.get('title') or '', holds,
                                safe_hold, HOLD_DEFER_HOURS,
                            )
                        )
                        pending_repo.mark_hold_cap_pinged()
                except Exception as notify_err:
                    logger.error(
                        f"[E038] send failed for {link}: "
                        f"{sanitize_error_message(notify_err)}"
                    )
        else:  # 'failed' — per-article problem, or transient error that
               # survived the in-slot retries (each retry logged above).
            safe = sanitize_error_message(err)
            failed_count += 1
            # Collect a de-duped, capped (link, reason) for the [E034] recap.
            # ``safe`` is already sanitized — never raw text, never secrets.
            if (
                link
                and len(recap_failures) < PUBLISH_RECAP_MAX_FAILURES
                and not any(existing == link for existing, _ in recap_failures)
            ):
                recap_failures.append((link, safe))
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
                    moved_to_failed_count += 1
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

    # ------------------------------------------------------------------
    # End-of-tick PUBLISH RECAP ([E034], companion to the E008/E009 intake
    # funnel). Surfaces per-slot outcomes so the operator sees WHAT posted and
    # WHY a post failed — the 'failed' reason was LOG-ONLY before this.
    #
    #   * Skip on a quiet/no-slot tick (nothing was attempted) — the intake
    #     [E009] heartbeat already covers those; avoid a redundant ping.
    #   * All-clean (held==failed==0) → compact 🟢 «опубликовано N/N».
    #   * Any held/failed → 🟡 with the tally + held note + failure reasons.
    #
    # NON-BLOCKING / fail-safe: runs AFTER all publishing (cannot affect a
    # post); counters are plain ints; the build+send is wrapped in try/except
    # (log-and-continue) so a recap fault never breaks the tick.
    # ------------------------------------------------------------------
    if published_count + held_count + failed_count > 0:
        try:
            recap_msg = admin_alerts.alert_publish_recap({
                'published': published_count,
                'held': held_count,
                'failed': failed_count,
                'moved_to_failed': moved_to_failed_count,
                'failures': recap_failures,
            })
            send_admin_notification(recap_msg)
        except Exception as exc:
            logger.error(
                f"[publish-recap] failed to build/send recap: "
                f"{sanitize_error_message(exc)}"
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
    a single daily fixed-time tick at 10:00 МСК via ``schedule.every().
    day.at("10:00", tz=pytz.timezone("Europe/Moscow"))``. ``schedule==
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

    # Background review listener (dedup-review-buttons Task 5) — started
    # BEFORE the cron registration and the immediate job() so the daemon
    # thread lives through the whole process lifetime, including the long
    # blocking publish window. Fail-closed gate inside; default (flag off)
    # is a silent no-op.
    _maybe_start_review_listener()

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
