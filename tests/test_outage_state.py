#!/usr/bin/env python3
"""Unit tests for outage_state module — Claude API outage state machine.

Covers the 4 transition rows from code-research §14.4:
    no_outage → ping_1_sent
    ping_1_sent → ping_2_sent (after 1h)
    ping_2_sent → google_fallback_active (after 2h)
    any → no_outage (recovery)

Plus persistence across simulated container restart and concurrency
(BEGIN IMMEDIATE serializes two parallel record_outage_event callers).

Tests follow ``tests/test_pending_articles_repo.py`` tempfile pattern:
allocate a .db file, monkeypatch ``news_bot.DB_FILE`` to point at it.
This lets outage_state functions that open their own connection see the
same DB as test-opened ``sqlite3.connect`` cursors.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
import outage_state
import pending_articles_repo as repo


MSK = timezone(timedelta(hours=3))


class _TmpDbCase(unittest.TestCase):
    """Base: tempfile DB + init_schema + patch news_bot.DB_FILE."""

    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.db_patcher = patch.object(news_bot, 'DB_FILE', self.db_path)
        self.db_patcher.start()
        # Make processed_news + the four feature tables (incl. bot_state).
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS processed_news '
                '(link TEXT PRIMARY KEY, title TEXT, pub_date TEXT, '
                'processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'
            )
            conn.commit()
            repo.init_schema(conn)
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.db_patcher.stop()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# State-machine transition tests
# ---------------------------------------------------------------------------

class TestRecordOutageEvent(_TmpDbCase):
    """Cover the three forward outage transitions."""

    def test_record_outage_event_from_no_outage(self) -> None:
        """no_outage → ping_1_sent: started_at None, ping_count 0 → 1."""
        t0 = datetime(2026, 4, 27, 13, 0, tzinfo=MSK)

        result = outage_state.record_outage_event(now=t0)

        self.assertEqual(result['state'], 'ping_1_sent')
        self.assertEqual(len(result['pings_to_send']), 1)
        # Ping #1 must mention "1ч" / "Google" or similar — at minimum non-empty Russian.
        self.assertTrue(result['pings_to_send'][0].strip())
        # First transition: caller still retries Claude → fallback_now=False.
        self.assertFalse(result['fallback_now'])
        # Persisted state.
        self.assertEqual(outage_state.get_outage_started_at(), t0)
        self.assertEqual(outage_state.get_ping_count(), 1)
        self.assertFalse(outage_state.is_fallback_active())

    def test_record_outage_event_advance_to_ping_2(self) -> None:
        """ping_1_sent + (now - t0 ≥ 1h) → ping_2_sent: ping_count=2."""
        t0 = datetime(2026, 4, 27, 13, 0, tzinfo=MSK)
        outage_state.set_outage_started_at(t0)
        outage_state.set_ping_count(1)
        outage_state.set_last_ping_sent_at(t0)

        result = outage_state.record_outage_event(now=t0 + timedelta(hours=1, seconds=1))

        self.assertEqual(result['state'], 'ping_2_sent')
        self.assertEqual(len(result['pings_to_send']), 1)
        self.assertTrue(result['pings_to_send'][0].strip())
        self.assertTrue(result['fallback_now'])
        self.assertEqual(outage_state.get_ping_count(), 2)
        # Started_at must be unchanged (anchor of grace window).
        self.assertEqual(outage_state.get_outage_started_at(), t0)
        self.assertFalse(outage_state.is_fallback_active())

    def test_record_outage_event_switch_to_google_fallback(self) -> None:
        """ping_2_sent + (now - t0 ≥ 2h) → google_fallback_active: flag=True."""
        t0 = datetime(2026, 4, 27, 13, 0, tzinfo=MSK)
        outage_state.set_outage_started_at(t0)
        outage_state.set_ping_count(2)
        outage_state.set_last_ping_sent_at(t0 + timedelta(hours=1))

        result = outage_state.record_outage_event(now=t0 + timedelta(hours=2, seconds=1))

        self.assertEqual(result['state'], 'google_fallback_active')
        self.assertEqual(len(result['pings_to_send']), 1)
        self.assertTrue(result['pings_to_send'][0].strip())
        self.assertTrue(result['fallback_now'])
        self.assertTrue(outage_state.is_fallback_active())
        self.assertEqual(outage_state.get_ping_count(), 3)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

class TestRecordRecoveryEvent(_TmpDbCase):

    def test_record_recovery_event_clears_state(self) -> None:
        """Active outage → recovery: all keys cleared, recovery ping returned;
        repeat call on already-clean DB returns was_active=False, no pings."""
        t0 = datetime(2026, 4, 27, 13, 0, tzinfo=MSK)
        outage_state.set_outage_started_at(t0)
        outage_state.set_ping_count(3)
        outage_state.set_fallback_active(True)
        outage_state.set_last_ping_sent_at(t0 + timedelta(hours=2))

        result = outage_state.record_recovery_event(
            now=t0 + timedelta(hours=3),
        )

        self.assertTrue(result['was_active'])
        self.assertEqual(len(result['pings_to_send']), 1)
        self.assertTrue(result['pings_to_send'][0].strip())
        self.assertIsNone(outage_state.get_outage_started_at())
        self.assertEqual(outage_state.get_ping_count(), 0)
        self.assertFalse(outage_state.is_fallback_active())
        self.assertIsNone(outage_state.get_last_ping_sent_at())

        # Idempotent / no-op on already-clean state.
        again = outage_state.record_recovery_event(
            now=t0 + timedelta(hours=4),
        )
        self.assertFalse(again['was_active'])
        self.assertEqual(again['pings_to_send'], [])


# ---------------------------------------------------------------------------
# Persistence across simulated restart
# ---------------------------------------------------------------------------

class TestPersistence(_TmpDbCase):

    def test_persistence_across_restart(self) -> None:
        """Write state, simulate process restart (drop in-memory state), read
        the same keys via fresh _connect — values match."""
        t0 = datetime(2026, 4, 27, 13, 0, tzinfo=MSK)
        outage_state.set_outage_started_at(t0)
        outage_state.set_ping_count(2)
        outage_state.set_fallback_active(True)
        outage_state.set_last_ping_sent_at(t0 + timedelta(hours=1))

        # Simulated restart: outage_state opens new connection per call, so
        # no state to drop — but read via fresh sqlite3.connect to confirm
        # values are durable, not held in some hidden cache.
        with sqlite3.connect(self.db_path) as side:
            rows = dict(side.execute(
                "SELECT key, value FROM bot_state"
            ).fetchall())
        self.assertEqual(rows.get('outage_started_at'), t0.isoformat())
        self.assertEqual(rows.get('ping_count'), '2')
        self.assertEqual(rows.get('fallback_active'), '1')
        self.assertEqual(
            rows.get('last_ping_sent_at'),
            (t0 + timedelta(hours=1)).isoformat(),
        )

        # And via public API — a fresh "import-time" read returns parsed values.
        self.assertEqual(outage_state.get_outage_started_at(), t0)
        self.assertEqual(outage_state.get_ping_count(), 2)
        self.assertTrue(outage_state.is_fallback_active())
        self.assertEqual(
            outage_state.get_last_ping_sent_at(),
            t0 + timedelta(hours=1),
        )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency(_TmpDbCase):

    def test_concurrent_writers_serialize_via_begin_immediate(self) -> None:
        """Stronger concurrency check (per test-reviewer TR-3): seed
        ping_1_sent state, race two callers PAST the 1h boundary. Without
        BEGIN IMMEDIATE both would read ping_count=1, both compute
        next=ping_2_sent, both write ping_count='2' → only one ping #2 is
        emitted but ping_count would be '2' regardless.

        The real serialization signal is in `pings_to_send`: exactly ONE
        thread should observe the no_outage→ping_2 transition and produce
        a ping #2; the other (post-lock-release) re-reads ping_count=2
        and emits no extra ping (ping_2_sent steady state, no advance to
        fallback because elapsed is still < 2h).
        """
        t0 = datetime(2026, 4, 27, 13, 0, tzinfo=MSK)
        outage_state.set_outage_started_at(t0)
        outage_state.set_ping_count(1)
        outage_state.set_last_ping_sent_at(t0)

        # Both racers fire ~1.0001h after t0 — past _PING_2_THRESHOLD.
        t_a = t0 + timedelta(hours=1, seconds=1)
        t_b = t0 + timedelta(hours=1, seconds=1, milliseconds=500)

        barrier = threading.Barrier(2)
        results: list[dict] = []
        results_lock = threading.Lock()
        errors: list[BaseException] = []

        def worker(now: datetime) -> None:
            try:
                barrier.wait(timeout=5)
                r = outage_state.record_outage_event(now=now)
                with results_lock:
                    results.append(r)
            except BaseException as exc:  # noqa: BLE001 — surface in main thread
                errors.append(exc)

        t1 = threading.Thread(target=worker, args=(t_a,))
        t2 = threading.Thread(target=worker, args=(t_b,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertFalse(errors, f"thread errors: {errors}")
        self.assertEqual(len(results), 2)

        # Strong assertion: exactly ONE caller emitted ping #2. With a
        # missing BEGIN IMMEDIATE both threads would observe ping_count=1
        # pre-write and both would emit ping #2 → list of 2 pings, not 1.
        ping_emitting_calls = [r for r in results if r['pings_to_send']]
        self.assertEqual(
            len(ping_emitting_calls), 1,
            f"BEGIN IMMEDIATE failed to serialise — got: {results}",
        )

        # Final state: ping_count advanced exactly once.
        self.assertEqual(outage_state.get_ping_count(), 2)
        self.assertEqual(outage_state.get_outage_started_at(), t0)
        self.assertFalse(outage_state.is_fallback_active())

        # Other caller saw ping_2_sent (already advanced) and emitted nothing.
        non_emitting = [r for r in results if not r['pings_to_send']]
        self.assertEqual(len(non_emitting), 1)
        self.assertEqual(non_emitting[0]['state'], 'ping_2_sent')


# ---------------------------------------------------------------------------
# Tolerance + contract tests (added per test-reviewer round 1)
# ---------------------------------------------------------------------------

class TestReadTolerance(_TmpDbCase):
    """Reads tolerate missing keys and corrupted content (AC: never crash)."""

    def test_missing_keys_return_defaults(self) -> None:
        """Fresh DB: getters return None / 0 / False, no exceptions."""
        self.assertIsNone(outage_state.get_outage_started_at())
        self.assertIsNone(outage_state.get_last_ping_sent_at())
        self.assertIsNone(outage_state.get_last_health_check_at())
        self.assertEqual(outage_state.get_ping_count(), 0)
        self.assertFalse(outage_state.is_fallback_active())

    def test_corrupted_timestamp_returns_none_and_warns(self) -> None:
        """Corrupted ISO string (manual DB edit, file corruption, …) →
        getter returns None and emits a logger.warning. Bot must keep
        starting up; reads must never crash."""
        with sqlite3.connect(self.db_path) as side:
            side.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                ('outage_started_at', 'not-a-valid-iso-string'),
            )
            side.commit()

        with self.assertLogs('outage_state', level='WARNING') as captured:
            self.assertIsNone(outage_state.get_outage_started_at())
        self.assertTrue(
            any('outage_started_at' in m for m in captured.output),
            f"expected warning mentioning the key, got: {captured.output}",
        )

    def test_corrupted_ping_count_returns_zero_and_warns(self) -> None:
        """Corrupted ping_count (non-int) → 0, with warning. No crash."""
        with sqlite3.connect(self.db_path) as side:
            side.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                ('ping_count', 'banana'),
            )
            side.commit()
        with self.assertLogs('outage_state', level='WARNING'):
            self.assertEqual(outage_state.get_ping_count(), 0)


class TestNaiveDatetimeRejection(_TmpDbCase):
    """tz-aware contract: setters and state-machine helpers reject naive."""

    def test_set_outage_started_at_rejects_naive(self) -> None:
        with self.assertRaises(ValueError):
            outage_state.set_outage_started_at(datetime(2026, 4, 27, 13, 0))

    def test_record_outage_event_rejects_naive(self) -> None:
        with self.assertRaises(ValueError):
            outage_state.record_outage_event(now=datetime(2026, 4, 27, 13, 0))

    def test_record_recovery_event_rejects_naive(self) -> None:
        with self.assertRaises(ValueError):
            outage_state.record_recovery_event(now=datetime(2026, 4, 27, 13, 0))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
