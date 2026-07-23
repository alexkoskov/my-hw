#!/usr/bin/env python3
"""
Unit tests for `sanitize_error_message` and the source vocabulary
(`SOURCE_EMOJI` / `SOURCE_LABEL`) in `news_bot.py`.

Covers Decisions 4 (source vocabulary) and 11 (error sanitisation)
of the manual-review-workflow tech-spec.
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
from news_bot import (
    SOURCE_EMOJI,
    SOURCE_LABEL,
    sanitize_error_message,
)


# ---------------------------------------------------------------------------
# SOURCE_EMOJI / SOURCE_LABEL
# ---------------------------------------------------------------------------

class TestSourceVocabulary:
    """Decision 4 - keys are the registered source labels."""

    def test_source_emoji_has_exact_keys(self):
        assert set(SOURCE_EMOJI) == {
            'autoevolution', 'mattel', 'lamley', 'orangetrack', 't-hunted',
        }

    def test_source_label_has_exact_keys(self):
        assert set(SOURCE_LABEL) == {
            'autoevolution', 'mattel', 'lamley', 'orangetrack', 't-hunted',
        }

    def test_source_emoji_values(self):
        assert SOURCE_EMOJI['autoevolution'] == '\U0001F7E0'
        assert SOURCE_EMOJI['mattel'] == '\U0001F7E3'
        assert SOURCE_EMOJI['lamley'] == '\U0001F7E2'
        assert SOURCE_EMOJI['orangetrack'] == '\U0001F535'

    def test_source_label_values(self):
        assert SOURCE_LABEL['autoevolution'] == 'autoevolution'
        assert SOURCE_LABEL['mattel'] == 'mattel'
        assert SOURCE_LABEL['lamley'] == 'lamley'
        assert SOURCE_LABEL['orangetrack'] == 'orangetrack'

    def test_no_rss_key(self):
        # user-spec / Decision 4: lamley also arrives via RSS, so no
        # collapsed 'rss' key.
        assert 'rss' not in SOURCE_EMOJI
        assert 'rss' not in SOURCE_LABEL

    def test_no_other_key_in_ping_vocab(self):
        # 'other' is a netloc-fallback for Task 5; it has no ping emoji.
        assert 'other' not in SOURCE_EMOJI
        assert 'other' not in SOURCE_LABEL


# ---------------------------------------------------------------------------
# sanitize_error_message
# ---------------------------------------------------------------------------

SECRET_ENV_NAMES = (
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_CHANNEL_ID',
    'TELEGRAM_ADMIN_ID',
    'TELEGRAPH_ACCESS_TOKEN',
)


@pytest.fixture
def clean_secret_env(monkeypatch):
    """Unset all four secret env vars so dev-machine values don't leak
    into tests. Individual tests then re-set only what they need."""
    for name in SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield monkeypatch


class TestSanitizeErrorMessage:
    """Decision 11 - redact known secrets in exception strings."""

    def test_sanitize_redacts_bot_token(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAM_BOT_TOKEN', 'abc123')
        result = sanitize_error_message(Exception('api error: token abc123 invalid'))
        assert 'abc123' not in result
        assert '[REDACTED]' in result

    def test_sanitize_redacts_channel_id(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAM_CHANNEL_ID', '@mychan')
        result = sanitize_error_message(Exception('channel @mychan not found'))
        assert '@mychan' not in result
        assert '[REDACTED]' in result

    def test_sanitize_redacts_admin_id(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAM_ADMIN_ID', '12345')
        result = sanitize_error_message(Exception('admin 12345 blocked'))
        assert '12345' not in result
        assert '[REDACTED]' in result

    def test_sanitize_redacts_telegraph_token(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAPH_ACCESS_TOKEN', 'xyz789')
        result = sanitize_error_message(Exception('telegraph token xyz789 rejected'))
        assert 'xyz789' not in result
        assert '[REDACTED]' in result

    def test_sanitize_redacts_all_four_in_one_string(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAM_BOT_TOKEN', 'bot123')
        clean_secret_env.setenv('TELEGRAM_CHANNEL_ID', '@chan123')
        clean_secret_env.setenv('TELEGRAM_ADMIN_ID', 'admin123')
        clean_secret_env.setenv('TELEGRAPH_ACCESS_TOKEN', 'tg789')
        message = 'err bot123 and @chan123 and admin123 and tg789 end'
        result = sanitize_error_message(Exception(message))
        assert 'bot123' not in result
        assert '@chan123' not in result
        assert 'admin123' not in result
        assert 'tg789' not in result
        # Four distinct occurrences of the redaction marker.
        assert result.count('[REDACTED]') == 4

    def test_sanitize_skips_empty_env(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAM_BOT_TOKEN', '')
        message = 'api error without token in the string'
        result = sanitize_error_message(Exception(message))
        # No character-level interleaving - output matches the raw
        # exception string.
        assert result == message
        assert '[REDACTED]' not in result

    def test_sanitize_skips_whitespace_env(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAM_BOT_TOKEN', '   ')
        message = 'api error without token'
        result = sanitize_error_message(Exception(message))
        assert result == message
        assert '[REDACTED]' not in result

    def test_sanitize_skips_unset_env(self, clean_secret_env):
        # clean_secret_env already delenv'd TELEGRAM_BOT_TOKEN.
        message = 'api error without token'
        result = sanitize_error_message(Exception(message))
        assert result == message
        assert '[REDACTED]' not in result

    def test_sanitize_does_not_raise_on_weird_exc(self, clean_secret_env):
        # Exception with unicode args, embedded newline, and NUL byte.
        weird = Exception('oshibka\nmultiline' + chr(0) + ' cyr text')
        # Must not raise, returns a string.
        result = sanitize_error_message(weird)
        assert isinstance(result, str)

    def test_sanitize_on_empty_str_exc(self, clean_secret_env):
        clean_secret_env.setenv('TELEGRAM_BOT_TOKEN', 'abc')
        # Exception with empty str - replace on '' haystack is a no-op.
        result = sanitize_error_message(Exception(''))
        assert result == ''

    def test_sanitize_never_raises_internally(self, clean_secret_env):
        # If str(exc) raises, sanitize must still return a string
        # (graceful degradation per task-3 spec: "Iskliuchenie vnutri
        # funktsii ne podnimaet").
        class BadExc(Exception):
            def __str__(self):
                raise RuntimeError('broken __str__')

        result = sanitize_error_message(BadExc())
        assert isinstance(result, str)

    def test_sanitize_env_with_same_value_across_two_vars(self, clean_secret_env):
        # Two env-vars with the same secret value - second replace on an
        # already-redacted string is a no-op, no crash.
        clean_secret_env.setenv('TELEGRAM_BOT_TOKEN', 'sameval')
        clean_secret_env.setenv('TELEGRAPH_ACCESS_TOKEN', 'sameval')
        result = sanitize_error_message(Exception('err sameval tail'))
        assert 'sameval' not in result
        assert '[REDACTED]' in result


# ---------------------------------------------------------------------------
# send_admin_notification — reply_markup forwarding (dedup-review-buttons
# Task 2). Mock pattern mirrors
# tests/test_no_token_leak_in_logs.py::TestAdminNotifyRedaction.
# ---------------------------------------------------------------------------

class TestReplyMarkupForwarding:
    """Keyword-only ``reply_markup`` must reach ``bot.send_message``
    untouched (it is a telegram object, NOT text — it must never pass
    through ``_redact_text``), and omitting it must keep the call
    byte-for-byte equivalent to today's behaviour."""

    def _patch_credentials_and_bot(self, monkeypatch):
        """Fake creds + FakeBot whose ``send_message`` is an ``AsyncMock``
        (the function awaits it inside ``asyncio.run``; a plain ``Mock``
        would return a non-awaitable and fail vacuously)."""
        monkeypatch.setattr(
            news_bot,
            "TELEGRAM_BOT_TOKEN",
            "1234567890:fake_for_test_AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        monkeypatch.setattr(news_bot, "TELEGRAM_ADMIN_ID", "@fake_admin")
        # Deterministic text assertions: no [INSTANCE_LABEL] prefix.
        monkeypatch.setattr(news_bot, "INSTANCE_LABEL", "")

        fake_send = AsyncMock()

        class _FakeBot:
            def __init__(self, token):
                self.token = token
                self.send_message = fake_send

        monkeypatch.setattr(news_bot, "Bot", _FakeBot)
        return fake_send

    def test_reply_markup_forwarded_when_passed(self, monkeypatch):
        fake_send = self._patch_credentials_and_bot(monkeypatch)
        kb = object()  # sentinel: forwarding must preserve identity

        ok = news_bot.send_admin_notification("msg", reply_markup=kb)

        assert ok is True
        fake_send.assert_awaited_once()
        assert fake_send.await_args.kwargs["reply_markup"] is kb

    def test_send_unchanged_when_omitted(self, monkeypatch):
        fake_send = self._patch_credentials_and_bot(monkeypatch)

        ok = news_bot.send_admin_notification("plain ping")

        assert ok is True
        fake_send.assert_awaited_once()
        # Default reply_markup=None == PTB send_message default (no keyboard).
        assert fake_send.await_args.kwargs["reply_markup"] is None
        # Text is not distorted by the new parameter.
        assert fake_send.await_args.kwargs["text"] == "plain ping"

    def test_reply_markup_is_keyword_only(self, monkeypatch):
        self._patch_credentials_and_bot(monkeypatch)

        # Positional second arg must raise — the contract that guarantees
        # every existing positional caller stays green.
        with pytest.raises(TypeError):
            news_bot.send_admin_notification("m", object())
