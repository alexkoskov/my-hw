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
        """Two threads call record_outage_event simultaneously against an
        empty DB. BEGIN IMMEDIATE forces serialization; the LATE thread sees
        the EARLY thread's row and treats itself as a follow-up call within
        the same window (now-t0 < 1h → ping_1_sent, no extra ping).
        Net: ping_count == 1 (not 2), exactly one started_at recorded."""
        t_early = datetime(2026, 4, 27, 13, 0, 0, tzinfo=MSK)
        t_late = datetime(2026, 4, 27, 13, 0, 0, 500_000, tzinfo=MSK)  # +0.5s

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

        t1 = threading.Thread(target=worker, args=(t_early,))
        t2 = threading.Thread(target=worker, args=(t_late,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertFalse(errors, f"thread errors: {errors}")
        self.assertEqual(len(results), 2)

        # Exactly one inc → ping_count == 1. (If BEGIN IMMEDIATE were absent
        # both threads would read 0 pre-write and write 1 → still 1, masking
        # the bug. The real signal is on a SECOND outage event past the 1h
        # boundary — but at unit-test scale we exploit the cleaner property:
        # only ONE thread sees no_outage and emits ping #1; the other sees
        # ping_1_sent within the 1h grace and emits no ping.)
        self.assertEqual(outage_state.get_ping_count(), 1)
        self.assertTrue(outage_state.get_outage_started_at() in (t_early, t_late))

        ping_1_calls = [r for r in results if r['pings_to_send']]
        self.assertEqual(len(ping_1_calls), 1,
                         f"expected exactly one ping #1, got: {results}")
        # The other call should report state ping_1_sent with empty pings.
        no_ping_calls = [r for r in results if not r['pings_to_send']]
        self.assertEqual(len(no_ping_calls), 1)
        self.assertEqual(no_ping_calls[0]['state'], 'ping_1_sent')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
