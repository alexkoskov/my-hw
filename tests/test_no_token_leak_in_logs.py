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
from unittest.mock import AsyncMock

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

    def test_filter_is_installed_on_root_stream_handler(self):
        """The redacting filter must ALSO be attached to the StreamHandler
        installed by ``logging.basicConfig`` on the root logger.  Logger-level
        filters in Python's logging only run for records originating at that
        logger; propagated records from arbitrary child loggers (e.g.
        ``logging.getLogger("smoke").warning(...)``) reach root's handlers
        WITHOUT root's logger-filter being consulted.  The handler-level
        attachment on the StreamHandler is what closes that gap.

        We assert at least one of root's StreamHandlers carries our filter
        — additional handlers (e.g. pytest's ``LogCaptureHandler``, attached
        per-test by ``caplog``) may be present without our filter; that's
        fine, as caplog has its own handler-level filter chain."""
        root = logging.getLogger()
        stream_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not h.__class__.__name__.startswith("LogCaptureHandler")
        ]
        assert stream_handlers, (
            "root logger has no StreamHandler — basicConfig should have added one"
        )
        for h in stream_handlers:
            assert any(
                isinstance(f, news_bot._TokenRedactingFilter) for f in h.filters
            ), (
                f"_TokenRedactingFilter not installed on root StreamHandler {h!r} "
                "— propagated records from child loggers will leak."
            )

    def test_token_redacted_on_propagated_record_from_arbitrary_child_logger(
        self
    ):
        """Regression: a record emitted on an arbitrary child logger (one we
        haven't explicitly named in the addFilter loop) and propagated to
        root's StreamHandler must be scrubbed.  This exercises the
        handler-level filter path — without ``addFilter`` on the StreamHandler
        itself, propagated records bypass the logger-level filter on root and
        leak to stderr / journald.

        We can't use ``caplog`` here: pytest's ``LogCaptureHandler`` is a
        separate handler with its own filter chain, so it would not catch a
        regression in the basicConfig StreamHandler's filter wiring.  Instead
        we redirect the StreamHandler's stream to a buffer and assert.
        """
        import io
        root = logging.getLogger()
        stream_handlers = [
            h for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not h.__class__.__name__.startswith("LogCaptureHandler")
        ]
        assert stream_handlers, "no StreamHandler on root — basicConfig should have added one"
        target = stream_handlers[0]
        original_stream = target.stream
        buf = io.StringIO()
        target.stream = buf
        try:
            child = logging.getLogger("propagation_smoke_test_arbitrary")
            child.warning(
                'HTTP Request: POST %s "HTTP/1.1 200 OK"', FAKE_URL
            )
        finally:
            target.stream = original_stream
        captured = buf.getvalue()
        assert FAKE_TOKEN not in captured, (
            "Bot token survived on propagated record from arbitrary child "
            "logger — captured text was:\n" + captured
        )
        assert "***" in captured, (
            "Redaction marker '***' missing from captured StreamHandler output"
        )


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


# ===========================================================================
# Part 4 — Anthropic API key redaction (Decision 12, Task 4)
# ===========================================================================
#
# All keys below are SYNTHETIC fixtures.  The strings below intentionally
# resemble real ``sk-ant-...`` formats so the regex is exercised, but no
# real Anthropic credential is ever materialised in the test process.

FAKE_ANTHROPIC_KEY_PROD = "sk-ant-api03-FAKE_KEY_FOR_TESTS_0123456789ABCDEF"
FAKE_ANTHROPIC_KEY_SANDBOX = "sk-ant-FAKE=SANDBOX.value.0123456789ABCDEF"


class TestAnthropicKeyRedaction:
    """Unit-level tests for the broadened ``_TokenRedactingFilter`` and the
    new ``_redact_text`` helper introduced by Decision 12 / Task 4.

    These tests cover:
      * regex coverage for prod-shape and sandbox-shape ``sk-ant-...`` keys;
      * end-to-end redaction via the filter on records routed through the
        ``anthropic`` SDK loggers;
      * filter installation on each anthropic-family logger;
      * env-name list extension;
      * ``_redact_text`` helper covering both Telegram-bot-token and
        Anthropic-key shapes in a single pass.
    """

    def test_filter_redacts_prod_shape_anthropic_key(self):
        """Prod-shape ``sk-ant-api03-...`` substring on ``record.msg`` is
        replaced by ``***`` after the filter runs."""
        flt = news_bot._TokenRedactingFilter()
        record = logging.LogRecord(
            name="anthropic._client",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=f"AuthenticationError: invalid api key {FAKE_ANTHROPIC_KEY_PROD}",
            args=None,
            exc_info=None,
        )
        assert flt.filter(record) is True
        assert FAKE_ANTHROPIC_KEY_PROD not in record.getMessage()
        assert "***" in record.getMessage()

    def test_filter_redacts_sandbox_shape_with_equals_and_dots(self):
        """Sandbox-shape key with ``=`` and ``.`` characters — the old
        ``[A-Za-z0-9_-]{20,}`` regex would not have matched this."""
        flt = news_bot._TokenRedactingFilter()
        record = logging.LogRecord(
            name="anthropic",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=f"BadRequestError: key={FAKE_ANTHROPIC_KEY_SANDBOX} rejected",
            args=None,
            exc_info=None,
        )
        assert flt.filter(record) is True
        assert FAKE_ANTHROPIC_KEY_SANDBOX not in record.getMessage()
        assert "***" in record.getMessage()

    def test_filter_redacts_anthropic_key_passed_via_args(self):
        """``%s``-style logging passes the key via ``record.args`` — the
        filter must catch it because it pre-renders the message."""
        flt = news_bot._TokenRedactingFilter()
        record = logging.LogRecord(
            name="anthropic._base_client",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="auth failed for key=%s",
            args=(FAKE_ANTHROPIC_KEY_PROD,),
            exc_info=None,
        )
        assert flt.filter(record) is True
        assert FAKE_ANTHROPIC_KEY_PROD not in record.getMessage()
        assert "***" in record.getMessage()

    @pytest.mark.parametrize("name", [
        "anthropic",
        "anthropic._client",
        "anthropic._base_client",
    ])
    def test_filter_attached_to_anthropic_sdk_loggers(self, name):
        """The redacting filter must be attached to each anthropic-family
        logger so SDK-emitted records get scrubbed even when handlers are
        wired directly to them and propagation is short-circuited."""
        anthropic_logger = logging.getLogger(name)
        assert any(
            isinstance(f, news_bot._TokenRedactingFilter)
            for f in anthropic_logger.filters
        ), f"_TokenRedactingFilter not installed on {name!r} logger."

    def test_anthropic_api_key_in_secret_env_names(self):
        """``ANTHROPIC_API_KEY`` must be in ``_SECRET_ENV_NAMES`` so
        ``sanitize_error_message`` cleans its value out of stored error
        strings."""
        assert "ANTHROPIC_API_KEY" in news_bot._SECRET_ENV_NAMES

    def test_redact_text_helper_redacts_both_telegram_and_anthropic(self):
        """``_redact_text`` is the single source of truth — it must catch
        both Telegram-bot-token and Anthropic-key shapes in one pass."""
        text = (
            f"telegram leak: {FAKE_URL} | anthropic leak: {FAKE_ANTHROPIC_KEY_PROD}"
        )
        out = news_bot._redact_text(text)
        assert FAKE_TOKEN not in out
        assert FAKE_ANTHROPIC_KEY_PROD not in out
        assert out.count("***") >= 2

    def test_redact_text_is_noop_on_clean_input(self):
        """``_redact_text`` must return clean text untouched."""
        clean = "job() completed, processed 3 entries"
        assert news_bot._redact_text(clean) == clean

    def test_redact_text_preserves_surrounding_text(self):
        """Regex with ``=`` and ``.`` in the character class is greedy until
        the first non-matching char (whitespace, quote, etc.) — surrounding
        text after the key must be preserved."""
        text = f"prefix {FAKE_ANTHROPIC_KEY_SANDBOX} suffix-after-key"
        out = news_bot._redact_text(text)
        assert FAKE_ANTHROPIC_KEY_SANDBOX not in out
        assert "prefix" in out
        assert "suffix-after-key" in out

    def test_anthropic_regex_does_not_overmatch(self):
        """Short ``sk-ant-...`` strings (< 16 chars after the prefix) must
        NOT be matched."""
        for benign in (
            "sk-ant-shortish",                # 8 chars after prefix
            "sk-ant-tiny",                    # 4 chars after prefix
            "https://example.com/sk-ant-",    # nothing after prefix
        ):
            out = news_bot._redact_text(benign)
            assert out == benign, (
                f"Regex spuriously matched benign string: {benign!r} -> {out!r}"
            )

    def test_redact_text_handles_none_safely(self):
        """``_redact_text`` must never raise — empty / None input is returned
        untouched (or coerced to string), matching the filter's silent-fallback
        invariant."""
        # None and empty must not crash the helper.  The exact return value is
        # implementation-defined, but it must not propagate an exception.
        try:
            news_bot._redact_text(None)
            news_bot._redact_text("")
        except Exception as e:  # pragma: no cover — safety net
            pytest.fail(f"_redact_text raised on edge input: {e!r}")

    def test_anthropic_key_redacted_through_root_logger(self, caplog):
        """End-to-end: a record carrying a sandbox-shape key emitted on the
        ``anthropic._client`` logger must be scrubbed before any handler
        captures the text."""
        anthropic_logger = logging.getLogger("anthropic._client")

        with caplog.at_level(logging.WARNING, logger="anthropic._client"):
            anthropic_logger.warning(
                "AuthenticationError: invalid api key %s",
                FAKE_ANTHROPIC_KEY_SANDBOX,
            )

        combined = "\n".join(r.getMessage() for r in caplog.records)
        assert FAKE_ANTHROPIC_KEY_SANDBOX not in combined, (
            "Anthropic key survived the redaction filter — captured text was:\n"
            + combined
        )


class TestAdminNotifyRedaction:
    """Tests for ``send_admin_notification`` — the admin-notify path lives
    OUTSIDE the logging pipeline (builds the Telegram payload via Python
    f-strings).  Without explicit ``_redact_text`` here, an exception text
    containing the API key would land in operator's chat as plain text.
    """

    def _patch_credentials_and_bot(self, monkeypatch):
        """Install fake creds + a FakeBot whose ``send_message`` is an
        ``AsyncMock`` (the function awaits it inside ``asyncio.run``).

        ``Mock`` is wrong here because it returns a non-awaitable; pytest
        would emit ``RuntimeWarning: coroutine was never awaited`` and the
        assert would fail vacuously.
        """
        monkeypatch.setattr(
            news_bot,
            "TELEGRAM_BOT_TOKEN",
            "1234567890:fake_for_test_AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        monkeypatch.setattr(news_bot, "TELEGRAM_ADMIN_ID", "@fake_admin")

        fake_send = AsyncMock()

        class _FakeBot:
            def __init__(self, token):
                self.token = token
                self.send_message = fake_send

        monkeypatch.setattr(news_bot, "Bot", _FakeBot)
        return fake_send

    def test_send_admin_notification_redacts_anthropic_key_before_send(
        self, monkeypatch
    ):
        fake_send = self._patch_credentials_and_bot(monkeypatch)

        ok = news_bot.send_admin_notification(
            f"Claude API auth failed: AuthenticationError: key={FAKE_ANTHROPIC_KEY_PROD}"
        )

        assert ok is True
        fake_send.assert_awaited_once()
        captured = fake_send.await_args.kwargs["text"]
        assert FAKE_ANTHROPIC_KEY_PROD not in captured, (
            "Anthropic key survived send_admin_notification — captured text:\n"
            + captured
        )
        assert "***" in captured

    def test_send_admin_notification_redacts_sandbox_anthropic_key(
        self, monkeypatch
    ):
        fake_send = self._patch_credentials_and_bot(monkeypatch)

        ok = news_bot.send_admin_notification(
            f"Outage: BadRequestError key={FAKE_ANTHROPIC_KEY_SANDBOX}"
        )

        assert ok is True
        captured = fake_send.await_args.kwargs["text"]
        assert FAKE_ANTHROPIC_KEY_SANDBOX not in captured
        assert "***" in captured

    def test_send_admin_notification_redacts_telegram_token(self, monkeypatch):
        """Same path also catches the Telegram-bot-token shape — single
        helper, both regexes."""
        fake_send = self._patch_credentials_and_bot(monkeypatch)

        ok = news_bot.send_admin_notification(
            f"Outage: HTTP error talking to {FAKE_URL}"
        )

        assert ok is True
        captured = fake_send.await_args.kwargs["text"]
        assert FAKE_TOKEN not in captured
        assert "***" in captured

    def test_send_admin_notification_preserves_clean_message(self, monkeypatch):
        """Clean text (no secrets) passes through unchanged."""
        fake_send = self._patch_credentials_and_bot(monkeypatch)
        clean = "Job completed: processed 3 entries"

        ok = news_bot.send_admin_notification(clean)

        assert ok is True
        captured = fake_send.await_args.kwargs["text"]
        assert captured == clean
