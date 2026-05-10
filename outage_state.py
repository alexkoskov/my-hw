#!/usr/bin/env python3
"""Claude API outage state machine — persisted via the ``bot_state`` table.

Lives next to ``pending_articles_repo.py`` and re-uses the same connection
pattern: every public function opens a short-lived
``sqlite3.connect(news_bot.DB_FILE)`` per call, applies
``PRAGMA busy_timeout = 5000;`` (Decision 16 — absorbs cron-vs-CLI lock
contention without surfacing ``OperationalError`` on every hiccup), and
closes in ``finally``.

The DDL for ``bot_state`` lives in ``pending_articles_repo.init_schema``
(Task 1). This module never creates the table — callers must have called
``init_schema`` once at startup.

Two layers of API:

* **Key/value getters/setters** — thin wrappers around ``_get`` / ``_set``
  with type parsing (ISO-8601 timestamps, int counters, bool flags).
* **State-machine helpers** — ``record_outage_event(now)`` and
  ``record_recovery_event(now)`` — open one connection, ``BEGIN
  IMMEDIATE``, read the current snapshot, compute the next state via the
  pure ``_compute_next_state`` function, write the diff, ``COMMIT``.
  ``BEGIN IMMEDIATE`` is the linchpin: two concurrent fallback-publish
  callers cannot double-increment ``ping_count`` because the second
  caller blocks on the SQLite write lock until the first commits and
  thereafter reads the just-written row.

Reads are tolerant: a missing key returns ``None`` / ``0`` / ``False``;
a corrupted ISO timestamp logs a warning and returns ``None`` rather than
crashing the bot at startup (tech-spec AC: ``bot_state`` value reads
tolerate corrupted/unexpected text).

State machine details: see ``code-research.md §14.4`` and
``tech-spec.md`` Decision 3, Decision 5.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

import news_bot  # for DB_FILE at call time — see pending_articles_repo


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Admin ping texts — fetched lazily from admin_alerts so the wording lives in
# one place. Defined as module-level functions (not constants) so that future
# edits to admin_alerts don't require restarting interactive sessions, and so
# that tests can patch admin_alerts builders directly.
# ---------------------------------------------------------------------------

import admin_alerts


def _ping_1_text() -> str:
    return admin_alerts.alert_outage_first_ping()


def _ping_2_text() -> str:
    return admin_alerts.alert_outage_second_ping()


def _ping_3_text() -> str:
    return admin_alerts.alert_outage_fallback_engaged()


def _recovery_text() -> str:
    return admin_alerts.alert_outage_recovery()

# State-transition thresholds. Named so future tuning is one-place and the
# transition table reads in plain language (`elapsed >= _PING_2_THRESHOLD`).
_PING_2_THRESHOLD = timedelta(hours=1)
_FALLBACK_THRESHOLD = timedelta(hours=2)


# ---------------------------------------------------------------------------
# bot_state keys
# ---------------------------------------------------------------------------

_KEY_OUTAGE_STARTED_AT = 'outage_started_at'
_KEY_LAST_PING_SENT_AT = 'last_ping_sent_at'
_KEY_PING_COUNT = 'ping_count'
_KEY_FALLBACK_ACTIVE = 'fallback_active'
_KEY_LAST_HEALTH_CHECK_AT = 'last_health_check_at'

# Keys cleared atomically on recovery. Order doesn't matter for correctness
# (it's a single DELETE…WHERE IN clause) but kept stable for readability.
_OUTAGE_KEYS = (
    _KEY_OUTAGE_STARTED_AT,
    _KEY_LAST_PING_SENT_AT,
    _KEY_PING_COUNT,
    _KEY_FALLBACK_ACTIVE,
    _KEY_LAST_HEALTH_CHECK_AT,
)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Short-lived connection to ``news_bot.DB_FILE`` with a 5s busy_timeout.

    ``news_bot.DB_FILE`` is read at call time so tempfile patches in tests
    are honoured (same pattern as ``pending_articles_repo._connect``).
    """
    conn = sqlite3.connect(news_bot.DB_FILE)
    # PRAGMA busy_timeout — Decision 16. Absorbs typical cron-vs-CLI
    # contention (sub-50ms typical write window).
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


# ---------------------------------------------------------------------------
# Internal _get / _set (raw string values)
# ---------------------------------------------------------------------------

def _get(key: str) -> Optional[str]:
    """Return the raw string value for ``key``, or ``None`` if absent."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (key,),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _set(key: str, value: Optional[str]) -> None:
    """UPSERT ``key=value``. ``value=None`` deletes the key. Wrapped in
    ``BEGIN IMMEDIATE`` for read-then-write atomicity wrt other writers."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if value is None:
            conn.execute("DELETE FROM bot_state WHERE key=?", (key,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                (key, value),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Type parsing helpers
# ---------------------------------------------------------------------------

def _parse_dt(raw: Optional[str], key: str) -> Optional[datetime]:
    """ISO-8601 string → tz-aware datetime, or ``None`` on missing/corrupt.

    Corrupt content logs a warning but does NOT raise — the bot must keep
    starting up even if a manual edit broke the value.
    """
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("corrupted bot_state value at key=%s: %r", key, raw)
        return None


def _serialise_dt(dt: datetime) -> str:
    """tz-aware datetime → ISO-8601 string. Naive datetimes rejected — the
    state machine compares timestamps across boundaries that must be unambiguous.
    """
    if dt.tzinfo is None:
        raise ValueError(
            "outage_state requires tz-aware datetime; got naive %r" % (dt,)
        )
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Public getters / setters
# ---------------------------------------------------------------------------

def get_outage_started_at() -> Optional[datetime]:
    """When the current outage was first detected, or ``None`` if healthy."""
    return _parse_dt(_get(_KEY_OUTAGE_STARTED_AT), _KEY_OUTAGE_STARTED_AT)


def set_outage_started_at(dt: datetime) -> None:
    _set(_KEY_OUTAGE_STARTED_AT, _serialise_dt(dt))


def get_last_ping_sent_at() -> Optional[datetime]:
    return _parse_dt(_get(_KEY_LAST_PING_SENT_AT), _KEY_LAST_PING_SENT_AT)


def set_last_ping_sent_at(dt: datetime) -> None:
    _set(_KEY_LAST_PING_SENT_AT, _serialise_dt(dt))


def get_ping_count() -> int:
    """Number of admin warning pings sent in the current outage. ``0`` if no
    outage. Tolerates corrupt content (returns 0)."""
    raw = _get(_KEY_PING_COUNT)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        logger.warning("corrupted bot_state value at key=%s: %r",
                       _KEY_PING_COUNT, raw)
        return 0


def set_ping_count(n: int) -> None:
    _set(_KEY_PING_COUNT, str(int(n)))


def is_fallback_active() -> bool:
    """``True`` iff ``fallback_active='1'``. Missing/corrupt → ``False``."""
    return _get(_KEY_FALLBACK_ACTIVE) == '1'


def set_fallback_active(active: bool) -> None:
    # Storage asymmetry note: this writes literal '0' for the False case,
    # while ``clear_outage_state`` / ``record_recovery_event`` DELETE the row
    # entirely. ``is_fallback_active()`` treats both equivalently
    # (only '1' returns True). The two paths exist because clearing on
    # recovery is part of a multi-key DELETE batch — special-casing this
    # one key would complicate the SQL with no behavioural gain.
    _set(_KEY_FALLBACK_ACTIVE, '1' if active else '0')


def get_last_health_check_at() -> Optional[datetime]:
    return _parse_dt(_get(_KEY_LAST_HEALTH_CHECK_AT), _KEY_LAST_HEALTH_CHECK_AT)


def set_last_health_check_at(dt: datetime) -> None:
    _set(_KEY_LAST_HEALTH_CHECK_AT, _serialise_dt(dt))


def clear_outage_state() -> None:
    """Atomically delete every outage-related key. Called on recovery.

    Single ``DELETE … WHERE key IN (?, ?, …)`` inside ``BEGIN IMMEDIATE`` —
    cheap for 5 keys, leaves the table empty if no other keys ever land here.
    """
    conn = _connect()
    placeholders = ','.join('?' for _ in _OUTAGE_KEYS)
    sql = f"DELETE FROM bot_state WHERE key IN ({placeholders})"
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, _OUTAGE_KEYS)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# State machine — pure logic (no I/O)
# ---------------------------------------------------------------------------

def _compute_next_state(
    started_at: Optional[datetime],
    ping_count: int,
    now: datetime,
) -> tuple[dict, list[str], bool, str]:
    """Pure transition function. Given the persisted snapshot
    (``started_at``, ``ping_count``) and the current timestamp ``now``,
    return ``(writes, pings_to_send, fallback_now, state_label)``.

    ``writes`` is a dict of ``{key: value}`` to UPSERT (string values).
    ``state_label`` is the post-transition state name returned to caller.

    Transition table (code-research §14.4):

    * Fresh DB (started_at is None) → ping_1_sent: write started_at=now,
      ping_count=1, last_ping_sent_at=now. Send ping #1.
      ``fallback_now=False`` because the caller will retry Claude once.
    * started_at set, ping_count==1, now-t0 ≥ 1h → ping_2_sent: ping_count=2,
      last_ping_sent_at=now. Send ping #2. ``fallback_now=True``.
    * started_at set, ping_count==1, now-t0 < 1h → still ping_1_sent.
      No writes, no extra ping. ``fallback_now=True``.
    * started_at set, ping_count==2, now-t0 ≥ 2h → google_fallback_active:
      ping_count=3, last_ping_sent_at=now, fallback_active=1. Send ping #3.
      ``fallback_now=True``.
    * started_at set, ping_count==2, now-t0 < 2h → still ping_2_sent.
      No writes. ``fallback_now=True``.
    * started_at set, ping_count>=3 → google_fallback_active (steady state).
      No writes. ``fallback_now=True``.
    """
    if started_at is None:
        # First-ever outage event in this window.
        writes = {
            _KEY_OUTAGE_STARTED_AT: _serialise_dt(now),
            _KEY_PING_COUNT: '1',
            _KEY_LAST_PING_SENT_AT: _serialise_dt(now),
        }
        return writes, [_ping_1_text()], False, 'ping_1_sent'

    elapsed = now - started_at

    if ping_count <= 1:
        # ping_1_sent
        if elapsed >= _PING_2_THRESHOLD:
            writes = {
                _KEY_PING_COUNT: '2',
                _KEY_LAST_PING_SENT_AT: _serialise_dt(now),
            }
            return writes, [_ping_2_text()], True, 'ping_2_sent'
        return {}, [], True, 'ping_1_sent'

    if ping_count == 2:
        # ping_2_sent
        if elapsed >= _FALLBACK_THRESHOLD:
            writes = {
                _KEY_PING_COUNT: '3',
                _KEY_LAST_PING_SENT_AT: _serialise_dt(now),
                _KEY_FALLBACK_ACTIVE: '1',
            }
            return writes, [_ping_3_text()], True, 'google_fallback_active'
        return {}, [], True, 'ping_2_sent'

    # ping_count >= 3 — steady-state google_fallback_active.
    return {}, [], True, 'google_fallback_active'


# ---------------------------------------------------------------------------
# State machine — atomic write paths
# ---------------------------------------------------------------------------

def record_outage_event(now: datetime) -> dict:
    """Advance the outage state machine on a fresh Claude API outage error.

    Wraps the read-then-write sequence in ``BEGIN IMMEDIATE`` so two
    concurrent fallback-publish callers serialize via the SQLite write
    lock; the second caller reads the just-written snapshot and treats
    itself as a follow-up event in the same window (no double-increment).

    Returns a dict::

        {
            'state':           '<post-transition state label>',
            'pings_to_send':   list[str],   # admin Telegram messages
            'fallback_now':    bool,        # route THIS article via Google?
        }
    """
    if now.tzinfo is None:
        raise ValueError(
            "record_outage_event requires tz-aware datetime; got naive %r"
            % (now,)
        )
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = dict(conn.execute(
            "SELECT key, value FROM bot_state WHERE key IN (?, ?, ?, ?, ?)",
            _OUTAGE_KEYS,
        ).fetchall())

        started_at = _parse_dt(rows.get(_KEY_OUTAGE_STARTED_AT),
                               _KEY_OUTAGE_STARTED_AT)
        try:
            ping_count = int(rows.get(_KEY_PING_COUNT, '0') or '0')
        except ValueError:
            ping_count = 0

        writes, pings, fallback_now, state = _compute_next_state(
            started_at, ping_count, now,
        )

        for key, value in writes.items():
            conn.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
                (key, value),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        'state': state,
        'pings_to_send': list(pings),
        'fallback_now': fallback_now,
    }


def record_recovery_event(now: datetime) -> dict:
    """Clear all outage keys atomically and return the recovery ping.

    No-op (no DB write, no ping) if there is no active outage —
    ``record_recovery_event`` is idempotent and safe to call on every
    successful Claude transcreation. Returns::

        {
            'was_active':     bool,
            'pings_to_send':  list[str],
        }

    Implementation uses double-checked locking: a read-only probe first
    (no write lock) avoids per-publish contention in the steady-state
    healthy case (which is the overwhelmingly common path). Only when the
    probe shows an active outage do we open ``BEGIN IMMEDIATE`` and re-read
    inside the transaction (race-safe: a concurrent writer that just set
    ``outage_started_at`` will be observed by the second read).
    """
    if now.tzinfo is None:
        raise ValueError(
            "record_recovery_event requires tz-aware datetime; got naive %r"
            % (now,)
        )

    # Fast read-only probe — no BEGIN IMMEDIATE, no contention with writers
    # for the steady-state "healthy bot" case.
    if _get(_KEY_OUTAGE_STARTED_AT) is None:
        return {'was_active': False, 'pings_to_send': []}

    # Probe said outage was active — escalate to a write transaction and
    # re-read inside the lock to guard against a concurrent recovery.
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (_KEY_OUTAGE_STARTED_AT,),
        ).fetchone()
        was_active = row is not None and row[0] is not None

        if not was_active:
            # Lost the race — another caller already cleared. Commit the
            # empty txn (releases the write lock) and report no-op.
            conn.commit()
            return {'was_active': False, 'pings_to_send': []}

        placeholders = ','.join('?' for _ in _OUTAGE_KEYS)
        conn.execute(
            f"DELETE FROM bot_state WHERE key IN ({placeholders})",
            _OUTAGE_KEYS,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {'was_active': True, 'pings_to_send': [_recovery_text()]}
