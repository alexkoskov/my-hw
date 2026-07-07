#!/usr/bin/env python3
"""Unit tests for the ``news_bot.fetch_full_article`` dispatcher routing.

These tests sit at the unit level (no real HTTP, no source-fetcher work)
and guard the dispatcher's branch ordering — specifically the t-hunted
blogspot branch added in the t-hunted-pt-source feature (Decision 1).

Why a separate file: the dispatcher routing is a tiny, focused concern;
extending ``test_integration.py`` would bury these checks in a large
end-to-end mocking harness. A standalone file keeps the regression signal
obvious if a future edit reorders the if-chain.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot


def test_send_admin_notification_retries_on_telegram_error(monkeypatch, caplog):
    """Regression for 2026-06-09 prod tick: a single ``TelegramError:
    Timed out`` on the morning [E008] dropped that day's plan ping with
    no retry. send_admin_notification must now try up to 3 times with
    exponential backoff before giving up.
    """
    import logging as _logging
    import asyncio as _asyncio
    from unittest.mock import AsyncMock
    from telegram.error import TelegramError as _TE

    monkeypatch.setattr(news_bot, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(news_bot, "TELEGRAM_ADMIN_ID", "@admin")
    monkeypatch.setattr(news_bot, "INSTANCE_LABEL", "")

    # First two attempts raise TelegramError, third succeeds.
    fake_send = AsyncMock(side_effect=[
        _TE("Timed out"),
        _TE("Timed out"),
        None,
    ])

    class _FakeBot:
        def __init__(self, token): self.send_message = fake_send

    monkeypatch.setattr(news_bot, "Bot", _FakeBot)
    # Don't actually sleep during backoff in tests.
    monkeypatch.setattr(news_bot.time, "sleep", lambda s: None)

    with caplog.at_level(_logging.WARNING):
        ok = news_bot.send_admin_notification("test message")

    assert ok is True
    assert fake_send.await_count == 3, (
        f"expected 3 send attempts (2 fails + 1 success), got {fake_send.await_count}"
    )
    warns = [r.message for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("attempt 1/3" in w and "retrying in 1s" in w for w in warns), (
        f"expected attempt 1/3 retry log with 1s backoff; got {warns}"
    )
    assert any("attempt 2/3" in w and "retrying in 2s" in w for w in warns), (
        f"expected attempt 2/3 retry log with 2s backoff; got {warns}"
    )


def test_send_admin_notification_gives_up_after_max_attempts(monkeypatch, caplog):
    """All 3 attempts fail with TelegramError → log final ERROR, return False.
    No exception propagates (callers must keep working — admin ping is
    monitoring, not correctness)."""
    import logging as _logging
    from unittest.mock import AsyncMock
    from telegram.error import TelegramError as _TE

    monkeypatch.setattr(news_bot, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(news_bot, "TELEGRAM_ADMIN_ID", "@admin")
    monkeypatch.setattr(news_bot, "INSTANCE_LABEL", "")
    fake_send = AsyncMock(side_effect=_TE("Timed out"))

    class _FakeBot:
        def __init__(self, token): self.send_message = fake_send

    monkeypatch.setattr(news_bot, "Bot", _FakeBot)
    monkeypatch.setattr(news_bot.time, "sleep", lambda s: None)

    with caplog.at_level(_logging.ERROR):
        ok = news_bot.send_admin_notification("test message")

    assert ok is False
    assert fake_send.await_count == 3
    errors = [r.message for r in caplog.records if r.levelno == _logging.ERROR]
    assert any("after 3 attempts" in e and "Timed out" in e for e in errors), (
        f"expected final error log with attempt count; got {errors}"
    )


def test_record_heartbeat_writes_unix_timestamp(tmp_path):
    """Regression for 2026-06-08 prod-hang: ``_record_heartbeat`` must
    write a fresh Unix timestamp every successful ``job()`` so the
    external watchdog (``watchdog.sh`` cron'd at 22:00 МСК) can detect
    alive-but-stuck instances by mtime age.
    """
    import time as _time
    target = tmp_path / "subdir" / "last_tick.ts"
    news_bot._record_heartbeat(str(target))
    assert target.exists()
    content = target.read_text().strip()
    assert content.isdigit(), f"expected Unix timestamp, got {content!r}"
    age = abs(int(content) - int(_time.time()))
    assert age < 5, f"timestamp not fresh: age={age}s"


def test_record_heartbeat_swallows_oserror(caplog):
    """``_record_heartbeat`` is monitoring infrastructure — its failure
    must NOT propagate into ``job()`` (which would falsely mark the
    tick as crashed and trigger ``Restart=on-failure``).
    """
    import logging as _logging
    # Path under a file (not dir) — makedirs will succeed (sees existing
    # path component), but open() will hit ENOTDIR on the file segment.
    # Cross-platform safe: just pass an empty string to break os.path.
    with caplog.at_level(_logging.WARNING):
        # Will fail because empty path has no dirname.
        # os.makedirs("", exist_ok=True) → FileNotFoundError → caught.
        news_bot._record_heartbeat("")
    assert any("failed to write" in r.message for r in caplog.records), (
        "expected a [heartbeat] WARNING log, got: "
        + repr([r.message for r in caplog.records])
    )


def test_singleton_lock_acquires_when_free(tmp_path):
    """Regression for 2026-06-08 multi-instance incident: a fresh deploy
    path must acquire the flock without raising — first instance wins.
    """
    lock_path = tmp_path / ".news_bot.lock"
    news_bot._acquire_singleton_lock(str(lock_path))
    # Cleanup so other tests can re-acquire; close releases the kernel lock.
    if news_bot._singleton_lock_fd is not None:
        news_bot._singleton_lock_fd.close()
        news_bot._singleton_lock_fd = None
    assert lock_path.exists()


def test_singleton_lock_refuses_second_instance(tmp_path):
    """If another process holds the flock, a second call to
    ``_acquire_singleton_lock`` must exit with code 1 instead of returning.
    This is the core defense against the four-parallel-bots class of
    incident that motivated this code.
    """
    import fcntl as _fcntl
    lock_path = tmp_path / ".news_bot.lock"
    # First holder — open fd + take exclusive lock, keep alive for the test.
    holder = open(str(lock_path), "w")
    _fcntl.flock(holder.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    try:
        try:
            news_bot._acquire_singleton_lock(str(lock_path))
        except SystemExit as exc:
            assert exc.code == 1, f"expected exit code 1, got {exc.code!r}"
        else:
            raise AssertionError(
                "second _acquire_singleton_lock call must SystemExit; instead "
                "it returned normally — singleton-lock contract broken."
            )
    finally:
        _fcntl.flock(holder.fileno(), _fcntl.LOCK_UN)
        holder.close()


def test_socket_default_timeout_set_on_module_load():
    """Regression for 2026-06-08 prod incident: importing ``news_bot``
    must install a global ``socket.setdefaulttimeout`` so any code path
    that creates a socket without explicit timeout (notably
    ``feedparser.parse`` → ``urllib.request.urlopen`` for RSS) cannot
    block job() forever on a slow/non-responsive server. Both call sites
    of ``feedparser.parse`` (autoevolution RSS via ``fetch_rss``;
    orangetrack feed via ``_fetch_orangetrack_entries``) rely on this
    floor — they pass no timeout themselves because feedparser 6.0.12's
    signature exposes no ``timeout`` kwarg.
    """
    import socket as _socket
    assert _socket.getdefaulttimeout() == 20.0


def test_fetch_full_article_routes_blogspot_to_t_hunted():
    """A blogspot.com link must route to
    ``t_hunted_source.fetch_t_hunted_article(link, …)`` exactly once and
    propagate its return value. Guards the dispatcher branch added in
    Task 3 (t-hunted-pt-source feature) against accidental reordering or
    removal."""
    expected = {
        'title': 'mocked',
        'subtitle': '',
        'paragraphs': ['p'],
        'images': [],
    }
    link = 'https://t-hunted.blogspot.com/2026/05/post.html'

    mock_fetch = MagicMock(return_value=expected)
    with patch(
        'news_bot.t_hunted_source.fetch_t_hunted_article',
        new=mock_fetch,
    ):
        result = news_bot.fetch_full_article({'link': link})

    assert mock_fetch.call_count == 1
    # First positional argument MUST be the link as-supplied.
    args, _ = mock_fetch.call_args
    assert args[0] == link
    assert result == expected


def test_fetch_full_article_unknown_domain_returns_none():
    """Regression: an unknown domain falls through the entire dispatcher
    if-chain and returns ``None`` (no source handler matched). Locks the
    fall-through behaviour so a future edit can't silently route unknown
    domains to the wrong handler."""
    result = news_bot.fetch_full_article(
        {'link': 'https://unknown.example.com/some-path'}
    )
    assert result is None


def test_fetch_full_article_userinfo_attack_does_not_route_to_autoevolution():
    """SSRF hardening (CWE-918): a userinfo-attack link whose pre-@ label is
    ``autoevolution.com`` but whose real host is an internal/metadata IP must
    NOT route to the autoevolution fetcher. The dispatcher matches on
    ``urlparse().hostname`` (the post-@ host = the IP), so it falls through to
    ``None`` and never fetches."""
    link = 'http://autoevolution.com@169.254.169.254/latest/meta-data/'
    mock_fetch = MagicMock(return_value={'title': 'x'})
    with patch(
        'news_bot.autoevolution_source.fetch_autoevolution_article',
        new=mock_fetch,
    ):
        result = news_bot.fetch_full_article({'link': link})
    mock_fetch.assert_not_called()
    assert result is None
