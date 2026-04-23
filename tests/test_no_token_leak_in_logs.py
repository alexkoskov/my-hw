#!/usr/bin/env python3
"""Regression tests for the httpx/httpcore bot-token leak discovered during
live-publish QA of manual-review-workflow.

What we pin down:

* ``httpx``, ``httpcore``, ``urllib3``, and ``requests`` loggers are bumped
  to WARNING at ``news_bot`` import time — so the INFO-level line that
  ``httpx`` emits on every outbound request (which embeds the bot token in
  the URL path) never reaches handlers in the first place.
* The defensive ``_TokenRedactingFilter`` scrubs any Telegram-bot-token-
  shaped substring from any LogRecord that does make it to the root
  logger — belt-and-suspenders for future third-party loggers we have not
  yet audited.

Every fixture uses a SYNTHETIC token.  No real ``TELEGRAM_BOT_TOKEN`` is
ever materialised in the test process.
"""

import logging
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot  # noqa: E402 — import triggers logging configuration


# ---------------------------------------------------------------------------
# Synthetic token — NEVER a real BotFather value.  Shape matches the pattern
# the fix regex targets: 10-digit bot_id + ':' + 35-char alnum/_/- suffix.
# ---------------------------------------------------------------------------
FAKE_TOKEN = "1234567890:FAKE_token_value_for_tests_0123456789A"
FAKE_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"


# ===========================================================================
# Part 1 — httpx / httpcore INFO logs are suppressed
# ===========================================================================

class TestThirdPartyLoggerLevels:
    """After ``import news_bot`` the noisy HTTP loggers must be at WARNING or
    higher, so they can no longer leak the token-in-URL INFO line."""

    @pytest.mark.parametrize("name", ["httpx", "httpcore", "urllib3", "requests"])
    def test_noisy_logger_is_at_warning_or_higher(self, name):
        level = logging.getLogger(name).level
        assert level >= logging.WARNING, (
            f"{name!r} logger level is {level} — httpx-family loggers must "
            f"be WARNING+ to prevent token-in-URL leakage."
        )

    def test_httpx_info_record_is_not_emitted(self, caplog):
        """A direct call to ``httpx.INFO`` — what ``python-telegram-bot``
        internally produces on every outbound request — must not be captured
        at INFO level by the root handler chain."""
        httpx_logger = logging.getLogger("httpx")

        # Capture at the *root* logger so propagation-filtering is honoured.
        with caplog.at_level(logging.DEBUG, logger=""):
            httpx_logger.info(
                'HTTP Request: POST %s "HTTP/1.1 200 OK"', FAKE_URL
            )

        infos = [r for r in caplog.records if r.name == "httpx" and r.levelno == logging.INFO]
        assert infos == [], (
            "httpx INFO record was captured — the logger is still at INFO "
            "and routine Telegram API calls will leak the bot token."
        )


# ===========================================================================
# Part 2 — the defensive redaction filter works
# ===========================================================================

class TestTokenRedactingFilter:
    """Unit-level tests of ``news_bot._TokenRedactingFilter`` / the root
    filter installation: even if a future third-party logger forces an INFO
    line through, the token gets scrubbed before any handler sees it."""

    def test_filter_instance_redacts_token_in_msg(self):
        """Given a LogRecord with a bot-token substring, ``filter()`` must
        replace the token with ``***`` on ``record.msg`` and return True."""
        flt = news_bot._TokenRedactingFilter()
        record = logging.LogRecord(
            name="httpx",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=f'HTTP Request: POST {FAKE_URL} "HTTP/1.1 200 OK"',
            args=None,
            exc_info=None,
        )
        assert flt.filter(record) is True
        assert FAKE_TOKEN not in record.getMessage()
        assert "***" in record.getMessage()

    def test_filter_redacts_token_passed_via_args(self):
        """``%s``-style logging passes the URL via ``record.args`` — the
        filter must still catch it because it pre-renders the message."""
        flt = news_bot._TokenRedactingFilter()
        record = logging.LogRecord(
            name="httpx",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg='HTTP Request: POST %s "HTTP/1.1 200 OK"',
            args=(FAKE_URL,),
            exc_info=None,
        )
        assert flt.filter(record) is True
        assert FAKE_TOKEN not in record.getMessage()
        assert "***" in record.getMessage()

    def test_filter_is_noop_on_clean_record(self):
        """Records without a token shape must pass through untouched."""
        flt = news_bot._TokenRedactingFilter()
        original = "job() completed, processed 3 entries"
        record = logging.LogRecord(
            name="news_bot",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=original,
            args=None,
            exc_info=None,
        )
        assert flt.filter(record) is True
        assert record.getMessage() == original

    def test_filter_is_installed_on_root_logger(self):
        """The redacting filter must be attached to the *root* logger — only
        then does it intercept records from every third-party library."""
        root = logging.getLogger()
        assert any(
            isinstance(f, news_bot._TokenRedactingFilter) for f in root.filters
        ), "_TokenRedactingFilter is not installed on the root logger."


# ===========================================================================
# Part 3 — end-to-end: a WARNING record carrying a token does not leak
# ===========================================================================

class TestEndToEndNoTokenInCapturedLogs:
    """Even if some library emits at WARNING+ and slips past the level bump,
    the filter must sanitise the captured text."""

    def test_warning_record_with_token_is_redacted_when_captured(self, caplog):
        # Force a WARNING on the httpx logger — this level is *not* filtered
        # out by our level bump, so it would reach handlers.
        httpx_logger = logging.getLogger("httpx")

        with caplog.at_level(logging.WARNING, logger="httpx"):
            httpx_logger.warning(
                'HTTP Request: POST %s "HTTP/1.1 200 OK"', FAKE_URL
            )

        # ``caplog`` attaches its own handler to the root propagation path;
        # the root filter runs before that handler, so the captured text
        # must be scrubbed.
        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert FAKE_TOKEN not in combined, (
            "Bot token survived the redaction filter — captured text was:\n"
            + combined
        )

    def test_bot_token_regex_shape(self):
        """Sanity-check the redaction regex matches the real Telegram shape
        and does not over-match common non-token strings."""
        assert news_bot._BOT_TOKEN_RE.search(FAKE_TOKEN) is not None

        # Non-tokens that should NOT match:
        for benign in (
            "12345",                                     # too short
            "https://example.com/foo:bar",               # no digit prefix
            "user:password",                             # alpha prefix
            "12:short",                                  # suffix too short
        ):
            assert news_bot._BOT_TOKEN_RE.search(benign) is None, (
                f"Regex spuriously matched benign string: {benign!r}"
            )
