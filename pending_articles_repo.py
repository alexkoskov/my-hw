#!/usr/bin/env python3
"""Data-access layer for the manual-review workflow.

Owns DDL and CRUD for three new SQLite tables:
- ``pending_articles``  — WIP queue, hard-cap 10 rows (enforced by callers).
- ``published_articles`` — audit of real publishes (``via_review`` flag).
- ``failed_articles``   — dead letter after 3 GT-fallback attempts.

The existing ``processed_news`` table is touched only as a write target for
``move_to_published`` / ``skip_pending``; its schema is owned by
``news_bot.init_db`` and NOT redefined here.

JSON serialisation for ``paragraphs`` / ``images`` / ``blocks`` /
``ru_paragraphs`` / ``ru_blocks`` is encapsulated here: callers pass and
receive Python lists (or ``None`` for the optional ``blocks`` / ``ru_*``
columns).

Connection policy mirrors ``news_bot.is_processed`` (news_bot.py:123-130):
one short-lived ``sqlite3.connect(news_bot.DB_FILE)`` per call, closed in a
``finally``. Multi-statement operations (``move_to_*``, ``skip_pending``,
``retry_from_failed``) hold a single connection and wrap the mutations in an
explicit try / ``commit()`` / ``rollback()`` / ``finally close()`` block.

All SQL uses ``?`` placeholders — no f-string / ``%`` interpolation into SQL
bodies (enforced by ``tests/test_pending_articles_repo.py::
TestSqlAudit::test_parameterized_queries_only``).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import news_bot  # for DB_FILE at call time — see module docstring


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# bot_state keys for cross-source-dedup feature (Decision 6)
# ---------------------------------------------------------------------------

# Per-pair soft-flag rate-limit key prefix. Full key shape:
#   ``softflag_pair:{new_link}\n{existing_link}``
# Newline separator chosen because links contain ``:`` and ``/`` — a newline
# is unambiguous (URLs cannot contain it).
_KEY_SOFTFLAG_PAIR_PREFIX = 'softflag_pair:'

# Global degraded-mode rate-limit key (1-hour window).
_KEY_DEDUP_DEGRADED = 'dedup_degraded_last_pinged_at'
_KEY_HOLD_CAP_PINGED = 'hold_cap_last_pinged_at'
_KEY_DRY_SPELL_PINGED = 'dry_spell_last_pinged_at'

# Review-token key prefix (dedup-review-buttons, tech-spec Decision 3).
# Telegram callback_data is capped at 64 bytes while the queue PK is a full
# article URL, so buttons carry a short token and ``bot_state`` maps
# ``review_token:<token>`` → link. Full key shape: ``review_token:{token}``
# — the token comes from ``secrets.token_urlsafe`` (URL-safe alphabet, no
# separator collisions possible).
_KEY_REVIEW_TOKEN_PREFIX = 'review_token:'


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS pending_articles (
    link              TEXT PRIMARY KEY,
    source_name       TEXT NOT NULL,
    feed_url          TEXT,
    title             TEXT NOT NULL,
    subtitle          TEXT NOT NULL DEFAULT '',
    paragraphs        TEXT NOT NULL,
    images            TEXT NOT NULL DEFAULT '[]',
    blocks            TEXT,
    ru_title          TEXT,
    ru_subtitle       TEXT,
    ru_paragraphs     TEXT,
    ru_blocks         TEXT,
    telegraph_url     TEXT,
    telegraph_path    TEXT,
    preview_html_path TEXT,
    fetched_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notified_at       TIMESTAMP,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    pub_date          TEXT
)
"""

_PUBLISHED_DDL = """
CREATE TABLE IF NOT EXISTS published_articles (
    link           TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    ru_title       TEXT NOT NULL,
    telegraph_url  TEXT NOT NULL,
    telegraph_path TEXT,
    source_name    TEXT NOT NULL,
    published_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    via_review     INTEGER NOT NULL
)
"""

_FAILED_DDL = """
CREATE TABLE IF NOT EXISTS failed_articles (
    link                TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    source_name         TEXT NOT NULL,
    paragraphs          TEXT NOT NULL,
    images              TEXT NOT NULL DEFAULT '[]',
    blocks              TEXT,
    subtitle            TEXT NOT NULL DEFAULT '',
    pub_date            TEXT,
    feed_url            TEXT,
    last_error          TEXT,
    failed_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    original_fetched_at TIMESTAMP
)
"""

# Tiny key/value store backing the Claude-API outage state machine
# (tech-spec llm-transcreation-and-distributed-publishing, Decision 3).
# Active keys: ``outage_started_at``, ``last_ping_sent_at``, ``ping_count``,
# ``fallback_active``, ``last_health_check_at`` — all values stored as
# ISO-8601 strings or short flag strings (``'0'`` / ``'1'`` / ``'2'``).
# Schema kept deliberately minimal so future additions do not require a
# migration. ``key`` is the primary key; ``value`` is nullable — readers
# treat a missing row as ``None``.
_BOT_STATE_DDL = """
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""

# Consecutive fetch failures per link (2026-08-13). A link that cannot be
# fetched is deliberately NOT marked processed — an autoevolution 403 is
# usually a single-tick transient and the next tick gets the article. During
# the 9-12 August block that turned into escalating hammering: the same
# articles failed every tick and the per-tick attempt count climbed 2 → 5 → 6
# as more of them piled up, i.e. the bot knocked harder every day on a door
# that had been shut. This table bounds that.
#
# Separate from `processed_news` on purpose: a row there means "handled,
# never look again", which on a FIRST failure would silently lose the article.
_FETCH_FAILURES_DDL = """
CREATE TABLE IF NOT EXISTS fetch_failures (
    link            TEXT PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    first_failed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_failed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# JSON-serialised columns per table. Used by the row→dict converters to
# deserialise list/dict fields, distinguishing NULL (absence) from "[]"
# (empty-but-present). See tech-spec §9.13.
_PENDING_JSON_COLS = ('paragraphs', 'images', 'blocks', 'ru_paragraphs', 'ru_blocks',
                      'model_fingerprint')
_FAILED_JSON_COLS = ('paragraphs', 'images', 'blocks')
# JSON columns on ``published_articles``. Currently only the dedup
# fingerprint added in the 2026-06-XX migration (Decision 11).
_PUBLISHED_JSON_COLS = ('model_fingerprint',)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dumps(value: Any) -> Optional[str]:
    """JSON-encode ``value`` with ``ensure_ascii=False`` so Cyrillic round-
    trips without ``\\uXXXX`` escaping in SQLite TEXT. ``None`` passes through."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _loads_or_none(raw: Optional[str]) -> Any:
    """Inverse of ``_dumps``. NULL → ``None``; ``'[]'`` → ``[]``."""
    if raw is None:
        return None
    return json.loads(raw)


def _row_to_dict(row: Optional[tuple], description: Iterable, json_cols: tuple) -> Optional[dict]:
    """Convert a cursor row (using its ``description``) into a plain dict,
    deserialising the named JSON-TEXT columns."""
    if row is None:
        return None
    out = {}
    for col, value in zip([d[0] for d in description], row):
        if col in json_cols:
            out[col] = _loads_or_none(value)
        else:
            out[col] = value
    return out


def _connect() -> sqlite3.Connection:
    """Open a connection to the configured DB. Read ``news_bot.DB_FILE`` at
    call time so tempfile patches in tests are honoured.

    ``timeout=5.0`` pins a 5000 ms busy-timeout (same 5 s contract as
    ``outage_state._connect``): the dedup-review-buttons feature adds a
    second concurrent writer (listener thread calling ``skip_pending``
    while the publish loop holds the write lock), and the busy handler
    makes the second writer wait out the lock window instead of raising
    ``database is locked``. Set via the ``sqlite3.connect`` parameter —
    equivalent to ``PRAGMA busy_timeout = 5000`` but WITHOUT an extra
    ``execute()`` inside ``_connect()``, which would shift the execute()
    counter in the fault-injection test
    ``test_move_to_published_rollback_on_error``.
    """
    return sqlite3.connect(news_bot.DB_FILE, timeout=5.0)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class SchemaMigrationError(RuntimeError):
    """A column migration in ``init_schema`` did not take effect.

    Raised LOUDLY and early (audit SEC-CG-1). The alternative — swallowing
    the failure — is far worse than a failed startup: every-tick queries
    (`list_pending`, `count_pending`, `insert_pending`) name the migrated
    columns unconditionally, so a silently-absent column turns into
    `no such column` inside `job()` on every tick and restart, with
    nothing in the logs pointing at the migration.
    """


#: SQLite's error text for "this column is already there". The ONLY
#: OperationalError the column migration may treat as success — everything
#: else (`database is locked`, `no such table`, disk I/O) means the ALTER
#: did not happen and must not be mistaken for an already-migrated DB.
_DUPLICATE_COLUMN_ERROR = 'duplicate column name'


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True iff ``table`` already has ``column``.

    Uses the ``pragma_table_info`` table-valued function rather than the
    ``PRAGMA table_info(x)`` statement form specifically so the table name
    goes through a ``?`` placeholder — no SQL string interpolation
    anywhere in this module (``TestSqlAudit``).
    """
    rows = conn.execute(
        "SELECT name FROM pragma_table_info(?)", (table,),
    ).fetchall()
    return any(r[0] == column for r in rows)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """Idempotently add ``column`` to ``table``, VERIFYING the result.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``. The historical pattern here
    was `try: ALTER / except sqlite3.OperationalError: pass`, which cannot
    tell "already migrated" from "the ALTER failed" — a writer holding an
    IMMEDIATE lock past the busy timeout raises `database is locked`, also
    an OperationalError, and the migration would report success with the
    column absent (audit SEC-CG-1).

    Three steps: check → ALTER only if genuinely missing → re-read and
    confirm. A `duplicate column name` error is the one benign outcome
    (another process won the race between our check and our ALTER, so the
    column IS there); anything else is logged and raised.

    ``ddl`` is a module-level literal, never caller data — the parameters
    exist so the check and the statement cannot drift apart.
    """
    if _column_exists(conn, table, column):
        return
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError as exc:
        if _DUPLICATE_COLUMN_ERROR in str(exc).lower():
            # Raced by a concurrent init_schema — the column is present.
            return
        logger.error(
            "schema migration failed: could not add %s.%s (%s: %s)",
            table, column, type(exc).__name__, exc,
        )
        raise SchemaMigrationError(
            f"could not add column {table}.{column}: {exc}"
        ) from exc
    if not _column_exists(conn, table, column):
        # ALTER reported success but the column is not there. Should be
        # unreachable; treated as fatal rather than trusted.
        logger.error(
            "schema migration verification failed: %s.%s still missing "
            "after ALTER reported success", table, column,
        )
        raise SchemaMigrationError(
            f"column {table}.{column} missing after a successful ALTER"
        )


#: Column migrations applied by ``init_schema``, as
#: ``(table, column, DDL literal)``. The table/column pair drives the
#: existence check; the DDL is a constant string (no interpolation).
_COLUMN_MIGRATIONS = (
    # 2026-04-30 (manual-review-workflow, Decision 9 idempotency): preserve
    # telegraph_url + telegraph_path on the failed row so retry_from_failed
    # can restore them and a retry doesn't create a second Telegraph page.
    ('failed_articles', 'telegraph_url',
     "ALTER TABLE failed_articles ADD COLUMN telegraph_url TEXT"),
    ('failed_articles', 'telegraph_path',
     "ALTER TABLE failed_articles ADD COLUMN telegraph_path TEXT"),
    # 2026-06-XX (cross-source-dedup, Decision 11): model fingerprint JSON
    # on both pending and published.
    ('pending_articles', 'model_fingerprint',
     "ALTER TABLE pending_articles ADD COLUMN model_fingerprint TEXT"),
    ('published_articles', 'model_fingerprint',
     "ALTER TABLE published_articles ADD COLUMN model_fingerprint TEXT"),
    # 2026-07-25 (content-gate): nullable TEXT holding the matched
    # content-gate markers. NULL (what every pre-migration row gets for
    # free) means "publishable"; non-NULL means "HELD, awaiting the
    # operator's «✅ Опубликовать»" and is filtered out of list_pending /
    # count_pending. Nullable + no default, so the ALTER is safe on the
    # live prod DB with rows in it.
    ('pending_articles', 'hold_reason',
     "ALTER TABLE pending_articles ADD COLUMN hold_reason TEXT"),
    # 2026-07-28 (dedup defer): nullable UTC 'YYYY-MM-DD HH:MM:SS' timestamp.
    # NULL (what every pre-migration row gets for free) means "publishable
    # now"; a future value means "staged but withheld until then", and is
    # filtered out of list_pending / count_pending exactly like hold_reason.
    # Unlike hold_reason this expires BY ITSELF — silence means publish.
    # Nullable + no default, so the ALTER is safe on the live prod DB.
    ('pending_articles', 'publish_after',
     "ALTER TABLE pending_articles ADD COLUMN publish_after TIMESTAMP"),
    # 2026-08-04 (hold cap): how many times IN A ROW the slot loop held this
    # row. Deliberately NOT ``attempt_count``: a strike walks the row toward
    # failed_articles, a hold must never do that. Nullable with no default, so
    # the ALTER is safe on the live prod DB — every pre-migration row reads
    # NULL, which ``increment_hold`` treats as 0 via COALESCE.
    ('pending_articles', 'hold_count',
     "ALTER TABLE pending_articles ADD COLUMN hold_count INTEGER"),
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the four feature tables if missing. Idempotent.

    Tables: ``pending_articles``, ``published_articles``, ``failed_articles``
    (manual-review-workflow), and ``bot_state`` (llm-transcreation-and-
    distributed-publishing — outage state machine; Decision 3).

    Takes an already-open ``conn`` so ``news_bot.init_db`` can re-use its own
    connection, and tests can pass ``:memory:``.
    """
    conn.execute(_PENDING_DDL)
    conn.execute(_PUBLISHED_DDL)
    conn.execute(_FAILED_DDL)
    conn.execute(_BOT_STATE_DDL)
    conn.execute(_FETCH_FAILURES_DDL)

    # Column migrations (see ``_COLUMN_MIGRATIONS`` for what and why).
    # Each one is check → ALTER-if-missing → verify (``_ensure_column``);
    # a migration that does not take effect raises SchemaMigrationError
    # rather than silently leaving the column absent (audit SEC-CG-1).
    for table, column, ddl in _COLUMN_MIGRATIONS:
        _ensure_column(conn, table, column, ddl)
    conn.commit()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def insert_pending(entry: dict) -> bool:
    """Insert one row into ``pending_articles``.

    ``entry`` is a plain dict with keys:
        link, source_name, feed_url, title, subtitle,
        paragraphs (list[str]), images (list[str]), blocks (list|None),
        pub_date.

    JSON fields are serialised inside this function with ``ensure_ascii=False``.

    Returns ``True`` on insert, ``False`` on UNIQUE conflict (race with a
    concurrent prep run — tech-spec §Risks). Other sqlite errors propagate.
    """
    conn = _connect()
    try:
        # ``model_fingerprint`` (cross-source-dedup, Decision 5/11): callers
        # before Task 4 do not set this key — ``entry.get`` returns None →
        # ``_dumps(None)`` returns None → NULL stored. NULL means "not
        # processed by the dedup gate"; ``{'strict':[],'brands':[]}`` (an
        # empty dict-shape, JSON-encoded) means "processed, no brands found".
        # ``hold_reason`` (content-gate, 2026-07-25): callers that do not
        # set the key get NULL → a normal publishable row. A non-NULL
        # marker string parks the row: staged, visible to ``get_pending``,
        # but invisible to ``list_pending`` / ``count_pending`` until the
        # operator approves it. Plain TEXT, not JSON — it is a
        # human-readable marker list rendered straight back into [E036].
        conn.execute(
            "INSERT INTO pending_articles "
            "(link, source_name, feed_url, title, subtitle, paragraphs, "
            " images, blocks, pub_date, model_fingerprint, hold_reason,"
            " publish_after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry['link'],
                entry['source_name'],
                entry.get('feed_url'),
                entry['title'],
                entry.get('subtitle') or '',
                _dumps(entry.get('paragraphs') or []),
                _dumps(entry.get('images') or []),
                _dumps(entry.get('blocks')),  # NULL-preserving
                entry.get('pub_date'),
                _dumps(entry.get('model_fingerprint')),  # NULL-preserving
                entry.get('hold_reason'),  # NULL-preserving, plain TEXT
                entry.get('publish_after'),  # NULL-preserving, UTC timestamp
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # UNIQUE PK conflict — expected under concurrent prep (tech-spec §Risks).
        conn.rollback()
        return False
    finally:
        conn.close()


def update_staged(link: str, ru_title: str, ru_subtitle: str,
                  ru_paragraphs: list, ru_blocks: Optional[list]) -> bool:
    """Populate the Russian fields on a pending row.

    Returns ``False`` if the row is no longer in ``pending_articles`` (user-
    spec AC L63). JSON fields serialised here; ``ru_blocks=None`` → NULL.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE pending_articles "
            "SET ru_title=?, ru_subtitle=?, ru_paragraphs=?, ru_blocks=? "
            "WHERE link=?",
            (ru_title, ru_subtitle, _dumps(ru_paragraphs), _dumps(ru_blocks), link),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_notified(link: str) -> None:
    """Stamp ``notified_at = CURRENT_TIMESTAMP`` — used by idle-fallback
    heads-up ping (Decision 12)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_articles SET notified_at=CURRENT_TIMESTAMP WHERE link=?",
            (link,),
        )
        conn.commit()
    finally:
        conn.close()


def clear_notified(link: str) -> None:
    """Clear ``notified_at`` — backs ``hw_review take N`` (user-spec AC L66)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_articles SET notified_at=NULL WHERE link=?",
            (link,),
        )
        conn.commit()
    finally:
        conn.close()


def increment_attempt(link: str, error: Optional[str]) -> int:
    """Bump ``attempt_count`` and overwrite ``last_error`` for ``link``.

    Returns the new count. Shared counter for idle-fallback AND overflow
    failures (Decision 13). Caller is responsible for sanitising ``error``
    before passing — repo stores the string verbatim (see Decision 11 and
    AC L103).
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_articles "
            "SET attempt_count = attempt_count + 1, last_error = ? "
            "WHERE link=?",
            (error, link),
        )
        row = conn.execute(
            "SELECT attempt_count FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        conn.commit()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def increment_hold(link: str) -> int:
    """Bump ``hold_count`` for ``link`` and return the new value.

    The hold counter's whole reason to exist separately from ``attempt_count``:
    an LLM outage must not walk an article toward ``failed_articles``, but it
    must not pin it to the queue head forever either — ``news_bot.job()`` reads
    ``list_pending()[0]`` at every slot, so a row that always fails blocks every
    article behind it. The caller compares the returned count against
    ``news_bot.HOLD_CAP`` and defers the row once it is exceeded.

    ``COALESCE`` is load-bearing: the column arrived by migration as nullable,
    so rows staged before it read NULL, and a plain ``hold_count + 1`` would
    evaluate to NULL — leaving those rows permanently under any cap.

    Returns 0 if the row is gone (manual review can publish it between the slot
    loop reading it and this call), so the caller stays below the cap rather
    than raising inside an error path.
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_articles "
            "SET hold_count = COALESCE(hold_count, 0) + 1 "
            "WHERE link=?",
            (link,),
        )
        row = conn.execute(
            "SELECT hold_count FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        conn.commit()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def defer_publish(link: str, until: str) -> bool:
    """Withhold ``link`` from the queue until ``until`` (UTC
    ``'YYYY-MM-DD HH:MM:SS'``), by setting the existing ``publish_after``.

    Same column the dedup soft-flag uses at insert time — this is the only
    place it is set on an ALREADY-STAGED row. ``list_pending`` / ``count_pending``
    filter on it, so the effect is immediate: the next slot reads a different
    ``rows[0]`` and the channel keeps publishing.

    ``hold_count`` is deliberately NOT reset. A row that has already proven it
    can wedge the head gets ONE retry per defer window from here on; resetting
    would hand it a fresh full cap and let it block the head again for another
    ``HOLD_CAP`` slots every cycle.

    Returns ``True`` iff a row was actually updated — same convention as
    ``clear_hold``. The caller uses it to stay quiet when the row vanished
    between the slot loop reading it and this call (the review listener can
    delete it): pinging «отложена, вернётся сама» about an article the operator
    just cancelled would be a lie.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE pending_articles SET publish_after = ? WHERE link=?",
            (until, link),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear_deferral(link: str) -> bool:
    """Release a timed deferral: ``publish_after = NULL`` for ``link``.

    The «👍 Оставить» half of the [E014] keyboard, and the mirror image of
    ``defer_publish``. The row is already staged, so releasing it is a single
    UPDATE — from the next slot on it is an ordinary queue member.

    Returns ``True`` when a DEFERRED row was actually released, ``False`` when
    there was nothing to release (row gone, or never deferred). Same
    convention as ``clear_hold``, and it makes a double press a no-op rather
    than a second "released" report.

    Deliberately does NOT touch ``hold_reason``: a row can be frozen for a
    content-gate reason as well, and lifting a *timed* deferral must not
    smuggle a held article into the queue. Nor does it reset ``hold_count`` —
    if the hold cap was what parked this row, the operator's press gets it one
    more turn at the head, not a fresh full cap (same reasoning as
    ``defer_publish``).
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE pending_articles SET publish_after=NULL "
            "WHERE link=? AND publish_after IS NOT NULL",
            (link,),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_fetch_failure(link: str) -> int:
    """Count one more consecutive failed fetch for ``link``; return the total.

    The counter is CONSECUTIVE — ``clear_fetch_failure`` wipes it the moment
    the article is fetched successfully — so a link that fails once and works
    the next tick never accumulates. Only a link that keeps failing climbs, and
    that is exactly the shape the cap is meant to stop.
    """
    conn = _connect()
    try:
        # AT MOST ONE per calendar day. `job()` runs once daily, but it also
        # runs on every container restart — prod restarted four times on
        # 2026-08-13 alone — and a plain +1 would have retired six live
        # articles within the hour instead of after three days. Same-day
        # repeats refresh the timestamp and leave the count alone, which is
        # what keeps `attempts` readable as "days".
        conn.execute(
            "INSERT INTO fetch_failures (link, attempts) VALUES (?, 1) "
            "ON CONFLICT(link) DO UPDATE SET "
            "  attempts = attempts + ("
            "    CASE WHEN date(last_failed_at) < date('now') THEN 1 ELSE 0 END"
            "  ), "
            "  last_failed_at = CURRENT_TIMESTAMP",
            (link,),
        )
        row = conn.execute(
            "SELECT attempts FROM fetch_failures WHERE link=?", (link,)
        ).fetchone()
        conn.commit()
        return int(row[0]) if row else 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_fetch_failure(link: str) -> bool:
    """Forget ``link``'s failure streak. Returns whether a row was removed.

    Called on every successful fetch, which is what makes the counter mean
    "in a row". Without it the count is lifetime-cumulative and a link that
    fails once a month eventually trips the cap on an innocent day — the same
    wrong-attribution trap ``reset_hold_counts_below`` exists to avoid.
    """
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM fetch_failures WHERE link=?", (link,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_schedule_backlog_counts() -> tuple[int, int, int]:
    """Return one atomic ``(publishable, deferred, held)`` queue snapshot.

    Content holds dominate timed deferrals. Rows without a hold are deferred
    only while ``publish_after`` is in the future; otherwise they are
    publishable. The three conditional aggregates run in one SQLite statement
    so scheduler planning cannot combine counts from different queue states.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN hold_reason IS NULL "
            "AND (publish_after IS NULL "
            "OR publish_after <= CURRENT_TIMESTAMP) "
            "THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN hold_reason IS NULL "
            "AND publish_after IS NOT NULL "
            "AND publish_after > CURRENT_TIMESTAMP "
            "THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN hold_reason IS NOT NULL "
            "THEN 1 ELSE 0 END), 0) "
            "FROM pending_articles"
        ).fetchone()
        if row is None:
            return 0, 0, 0
        return int(row[0]), int(row[1]), int(row[2])
    finally:
        conn.close()


def count_deferred() -> int:
    """Rows withheld ONLY by a future ``publish_after`` — publishable in every
    other respect, just not yet.

    ``count_pending`` deliberately excludes them, and ``news_bot.job()`` sizes
    the day's slots ONCE from that number. Without this counter a tick that
    starts with everything deferred computes zero slots and skips the whole
    day — including the moment a defer window elapses mid-tick. Held rows
    (``hold_reason``) are excluded because those wait for the operator and may
    never become publishable at all.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_articles "
            "WHERE hold_reason IS NULL "
            "AND publish_after IS NOT NULL AND publish_after > CURRENT_TIMESTAMP"
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def is_hold_cap_ping_rate_limited(window_hours: int = 6) -> bool:
    """True iff an [E038] «статья уступила очередь» ping went out within the
    last ``window_hours``. GLOBAL, not per link.

    During a sustained stall rows cross ``HOLD_CAP`` one after another, and a
    ping each would recreate the noise ``outage_state``'s ``ping_count >= 3``
    cutoff exists to prevent. The operator needs to know THAT the queue is
    stalling plus one representative cause; the running total already appears
    in the daily [E008]/[E009] «Отложено (уступили очередь): N» line.

    Fails OPEN — missing or corrupt value returns False. Silencing an alert
    because its own bookkeeping broke is the worse failure: this ping is the
    only thing that surfaces a stalling queue.

    Opens its own connection (the ``outage_state`` contour, like the review
    token store) because the caller — the slot loop's held branch — holds none.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (_KEY_HOLD_CAP_PINGED,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    last = _parse_dt_tolerant(row[0], _KEY_HOLD_CAP_PINGED)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) < timedelta(hours=window_hours)


def mark_hold_cap_pinged() -> None:
    """UPSERT the current UTC timestamp at the [E038] rate-limit key."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (_KEY_HOLD_CAP_PINGED, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def is_dry_spell_ping_rate_limited() -> bool:
    """True when [E017] «канал молчит» already went out on today's UTC date.

    ``job()`` fires once a day on the 10:00 МСК cron AND once on every
    container start. Prod restarted four times on 2026-08-13 and the operator
    got four identical dry-spell pings for one outage; a repeated alarm reads
    as a worsening situation when it is the same one.

    Bounded by CALENDAR DAY, not by a rolling window like the [E038] gate
    above. The cron fires from a 60 s poll loop, so consecutive daily ticks can
    sit a minute SHORT of 24 h apart and a rolling 24 h window would swallow
    tomorrow's legitimate alarm on that jitter. ``record_fetch_failure`` bounds
    itself the same way for the same reason.

    TZ-independent by construction — both sides of the comparison come from
    ``datetime.now(timezone.utc)``; the container's own TZ (Europe/Moscow, per
    the Dockerfile and the startup guard) does not enter into it. Note the
    accepted edge: the cron tick lands at 07:00 UTC, far from the boundary, but
    RESTART ticks land at any hour, so two restarts straddling 00:00 UTC ping
    twice minutes apart. That is two, not the four this gate exists to stop,
    and it is per calendar day as specified — not an oversight to "fix" by
    reaching back for a rolling window.

    Fails OPEN on every path it can control — missing row, corrupt value,
    unreadable DB, future-dated marker. The guarantee is structural (a blanket
    ``except`` around the whole body) rather than a bet on which exception type
    a parser happens to raise: silencing the only prolonged-outage alarm
    because its own bookkeeping broke is strictly worse than one duplicate
    ping, and this alert has no redundant channel the way [E038] has the daily
    «Отложено: N» line.

    The one path it CANNOT control is a stopped clock: if the container's clock
    stalls on the marked date the gate stays shut for as long as it is stuck.
    That is deliberately not defended here — the backstop is the external
    ``.github/workflows/uptime.yml`` watchdog, which reads the age of the last
    Telegra.ph page from outside the host and so depends on neither this clock
    nor this DB.
    """
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE key=?",
                (_KEY_DRY_SPELL_PINGED,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return False
        last = _parse_dt_tolerant(row[0], _KEY_DRY_SPELL_PINGED)
        if last is None:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        # ``.date()`` reads the value's OWN offset, so normalise first: a
        # marker written as +05:00 would otherwise match a UTC day it does not
        # belong to and eat that day's alarm.
        marked = last.astimezone(timezone.utc).date()
        today = datetime.now(timezone.utc).date()
        if marked > today:
            # Future-dated marker (manual edit, clock jump): it can never be
            # overwritten by a later ping, so honouring it would silence the
            # alarm permanently. Treat as unusable.
            # Logs ``marked``, not the raw value: "parses as ISO-8601" does
            # not bound length — ``fromisoformat`` accepts unlimited fractional
            # digits, so a crafted marker would emit a half-megabyte WARNING on
            # every tick. The normalised date is bounded and is also the more
            # diagnostic number, since it is what the gate actually compared.
            logger.warning(
                "future-dated bot_state marker at key=%s: %s — ignoring",
                _KEY_DRY_SPELL_PINGED, marked,
            )
            return False
        return marked == today
    except Exception:
        logger.warning(
            "dry-spell rate-limit marker unreadable — allowing the ping",
            exc_info=True,
        )
        return False


def mark_dry_spell_pinged() -> None:
    """UPSERT the current UTC timestamp at the [E017] rate-limit key."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (_KEY_DRY_SPELL_PINGED, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def reset_hold_counts_below(cap: int) -> int:
    """Zero ``hold_count`` on every row still under ``cap``. Returns how many.

    This is what makes the counter mean "holds IN A ROW" rather than "holds
    ever". Called when the LLM answers successfully: that proves the failures
    those rows accumulated were GLOBAL, so charging them to the articles would
    eventually defer an innocent row and ping the operator blaming it — the
    same wrong-attribution the 2026-06-10 E011 incident cost a day to.

    Rows at or above ``cap`` keep their count on purpose: they have already
    proven they can wedge the head, and the marker is what makes them yield on
    their FIRST hold in the next window instead of blocking for another ``cap``
    slots.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE pending_articles SET hold_count = 0 "
            "WHERE COALESCE(hold_count, 0) > 0 AND COALESCE(hold_count, 0) < ?",
            (cap,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def set_preview_path(link: str, preview_html_path: Optional[str]) -> None:
    """Record the absolute path of the local HTML preview file on the pending
    row. Called by ``hw_review preview N`` after writing the file inside
    ``~/.cache/hw-review/`` (tech-spec Decision 1). ``publish`` / ``skip``
    (Task 8) read this column to delete the stale file on queue exit.

    Passing ``None`` clears the column. No-op if the row is missing — the
    caller already validated the row through ``_resolve_pending``.
    """
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_articles SET preview_html_path=? WHERE link=?",
            (preview_html_path, link),
        )
        conn.commit()
    finally:
        conn.close()


def mark_telegraph_published(link: str, telegraph_url: str,
                             telegraph_path: str) -> None:
    """Record the Telegraph URL + path on the pending row (Decision 9) so a
    retry after partial Telegram failure reuses the existing page."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE pending_articles "
            "SET telegraph_url=?, telegraph_path=? WHERE link=?",
            (telegraph_url, telegraph_path, link),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_pending(link: str) -> Optional[dict]:
    """Return one pending row as a dict with JSON columns deserialised,
    or ``None`` if the link is not in the queue."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles WHERE link=?",
            (link,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description, _PENDING_JSON_COLS)
    finally:
        conn.close()


def get_published(link: str) -> Optional[dict]:
    """Return one published row or ``None``. No JSON columns on this table."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM published_articles WHERE link=?",
            (link,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description, ())
    finally:
        conn.close()


def get_failed(link: str) -> Optional[dict]:
    """Return one failed row or ``None``."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM failed_articles WHERE link=?",
            (link,),
        )
        row = cur.fetchone()
        return _row_to_dict(row, cur.description, _FAILED_JSON_COLS)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Content-gate hold (2026-07-25).
#
# Every helper below that hands rows to a publish, eviction or nag path
# carries the literal predicate ``hold_reason IS NULL``. A row with a
# non-NULL ``hold_reason`` is HELD (poster / catalog / packaging post
# awaiting the operator's «✅ Опубликовать») and must never reach any of
# them. That predicate IS the operator's rule «нет ответа = не публикуем»:
# no timer, no auto-publish, no auto-drop — an unapproved row simply stays
# invisible to the queue forever.
#
# The predicate is spelled out inline in each query rather than shared via
# a module constant on purpose: string-concatenating SQL fragments (even
# constant ones) is exactly the shape ``TestSqlAudit`` forbids.
#
# Deliberately NOT filtered: ``get_pending`` (by-PK accessor — the intake
# duplicate guard must keep seeing held rows or the same article would be
# re-staged every tick, and both button resolvers look rows up by link).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cross-source dedup defer (2026-07-28).
#
# ``publish_after`` is the timed sibling of ``hold_reason``: a row whose
# timestamp is still in the future is staged but invisible to the publish
# path, exactly like a held row. The difference is what happens when nobody
# acts — a hold waits FOREVER for «✅ Опубликовать», a defer expires by
# itself and the row publishes. That asymmetry is the operator's rule for
# each gate: «нет ответа = не публикуем» for the content gate, «нет ответа =
# публикуем» for a suspected duplicate.
#
# The predicate is ``(publish_after IS NULL OR publish_after <=
# CURRENT_TIMESTAMP)``. NULL is the pre-migration default and means «now»,
# so every existing row stays publishable. SQLite's CURRENT_TIMESTAMP is UTC
# 'YYYY-MM-DD HH:MM:SS' — the same format the writer stores — so the lexical
# ``<=`` compare is chronological.
#
# Spelled out inline per query for the same reason as ``hold_reason``:
# ``TestSqlAudit`` forbids string-concatenating SQL fragments.
# ---------------------------------------------------------------------------


def list_pending() -> list[dict]:
    """All PUBLISHABLE pending rows in publish order: today's batch first,
    then the carry-over backlog **oldest-first**.

    Rows held by the content gate (``hold_reason IS NOT NULL``) are
    EXCLUDED — the slot loop reads this function, so the exclusion is what
    guarantees a held article never publishes without an explicit
    «✅ Опубликовать». Use ``list_held()`` to see them.

    Two-tier ordering:
      * Tier 0 — rows whose ``date(fetched_at) = date('now')``: today's
        freshly-fetched batch goes to the top of the queue, in the
        natural order it was fetched (``fetched_at ASC``).
      * Tier 1 — everything else (carry-over from earlier days): drained
        oldest-first so stale items don't starve forever.

    SQL: ``ORDER BY CASE WHEN date(fetched_at) = date('now') THEN 0
                         ELSE 1 END, fetched_at ASC``.

    The publish loop reads ``rows[0]`` to pick the next slot, so this
    yields: today's news → very-oldest carry-over → … → almost-recent
    carry-over.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE hold_reason IS NULL "
            "AND (publish_after IS NULL OR publish_after <= CURRENT_TIMESTAMP) "
            "ORDER BY "
            "  CASE WHEN date(fetched_at) = date('now') THEN 0 ELSE 1 END, "
            "  fetched_at ASC"
        )
        rows = cur.fetchall()
        desc = cur.description
    finally:
        conn.close()
    return [_row_to_dict(r, desc, _PENDING_JSON_COLS) for r in rows]


def list_pending_stale(hours: int = 48) -> list[dict]:
    """Rows older than ``hours`` with no heads-up ping yet (``notified_at IS
    NULL``). Caller will ping + ``mark_notified`` for each (Decision 12).

    Held rows are excluded: a parked article is old ON PURPOSE, and nagging
    the operator about it every run would train them to ignore the ping.
    """
    conn = _connect()
    try:
        # `hours` is parameterised — the SQL body is constant.
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE hold_reason IS NULL "
            "AND notified_at IS NULL "
            "AND fetched_at < datetime('now', ? || ' hours') "
            "ORDER BY fetched_at ASC",
            (f"-{int(hours)}",),
        )
        rows = cur.fetchall()
        desc = cur.description
    finally:
        conn.close()
    return [_row_to_dict(r, desc, _PENDING_JSON_COLS) for r in rows]


def list_notified_overdue(grace_hours: int = 2) -> list[dict]:
    """Rows whose ``notified_at`` is older than ``grace_hours`` AND whose
    ``ru_paragraphs`` is still NULL — the idle-fallback GT-publish pool.

    Held rows excluded (uniform rule): this pool feeds a publish path.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE hold_reason IS NULL "
            "AND notified_at IS NOT NULL "
            "AND ru_paragraphs IS NULL "
            "AND notified_at < datetime('now', ? || ' hours') "
            "ORDER BY notified_at ASC",
            (f"-{int(grace_hours)}",),
        )
        rows = cur.fetchall()
        desc = cur.description
    finally:
        conn.close()
    return [_row_to_dict(r, desc, _PENDING_JSON_COLS) for r in rows]


def list_pending_for_eviction() -> list[dict]:
    """Rows eligible for overflow fast-track: ``ru_paragraphs IS NULL``,
    oldest first (Decision 7 — staged rows are never evicted).

    Held rows excluded: the overflow drain exists to relieve the
    PUBLISHABLE queue, and a held row is not in it. Evicting one would
    also destroy a decision the operator has not made yet.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE hold_reason IS NULL "
            "AND (publish_after IS NULL OR publish_after <= CURRENT_TIMESTAMP) "
            "AND ru_paragraphs IS NULL "
            "ORDER BY fetched_at ASC"
        )
        rows = cur.fetchall()
        desc = cur.description
    finally:
        conn.close()
    return [_row_to_dict(r, desc, _PENDING_JSON_COLS) for r in rows]


def list_held() -> list[dict]:
    """Rows HELD by the content gate, oldest-first — the operator's
    «на утверждении» backlog.

    Exact complement of ``list_pending``'s filter: together they partition
    ``pending_articles``. Read by the daily plan ping so a forgotten hold
    stays visible instead of silently rotting (the operator's «нет ответа =
    не публикуем» rule means nothing else will ever surface it).
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE hold_reason IS NOT NULL "
            "ORDER BY fetched_at ASC"
        )
        rows = cur.fetchall()
        desc = cur.description
    finally:
        conn.close()
    return [_row_to_dict(r, desc, _PENDING_JSON_COLS) for r in rows]


def list_failed() -> list[dict]:
    """Failed rows, newest first — footer in ``hw_review list`` (AC L71)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM failed_articles ORDER BY failed_at DESC"
        )
        rows = cur.fetchall()
        desc = cur.description
    finally:
        conn.close()
    return [_row_to_dict(r, desc, _FAILED_JSON_COLS) for r in rows]


def count_pending() -> int:
    """Number of PUBLISHABLE rows in the queue (held rows excluded).

    Feeds ``compute_fixed_slots(N=count_pending())`` and the `> 50` backlog
    warning, so the exclusion keeps a held article from buying the day an
    extra publish slot it can never fill, or from inflating the backlog
    alarm with rows the queue cannot drain on its own.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM pending_articles "
            "WHERE hold_reason IS NULL "
            "AND (publish_after IS NULL OR publish_after <= CURRENT_TIMESTAMP)"
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def get_max_published_at() -> Optional[str]:
    """Return the most recent ``published_at`` from ``published_articles``
    as the raw SQLite TEXT (UTC-naive ISO ``YYYY-MM-DD HH:MM:SS``), or
    ``None`` if the table is empty.

    Backs the crash-loop guard in ``news_bot.job()`` (Decision 9 of the
    llm-transcreation-and-distributed-publishing tech-spec): on cron tick
    or container restart, the bot reads this and sleeps until
    ``last_published + MIN_INTERVAL_MINUTES`` if the gap is too small —
    so a systematic restart loop cannot produce burst-publishes.

    The column default is SQLite ``CURRENT_TIMESTAMP`` (UTC, naive). The
    caller is responsible for parsing into a tz-aware datetime; this
    helper stays string-typed to keep the storage contract single-
    source-of-truth at the column.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(published_at) FROM published_articles"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transactional moves
# ---------------------------------------------------------------------------

def move_to_published(link: str, telegraph_url: str, telegraph_path: str,
                      via_review: bool) -> None:
    """Move a pending row into ``published_articles`` and stamp
    ``processed_news``, all in one SQLite transaction.

    Steps, in order:
      1. INSERT into ``published_articles`` (title / ru_title / source_name
         copied from the pending row).
      2. INSERT OR IGNORE into ``processed_news`` so dedup catches it.
      3. DELETE the pending row.

    Any exception rolls back the whole sequence (AC tech-spec — "no partial
    states"). ``telegraph_url`` / ``telegraph_path`` are passed by the caller
    because they've already been persisted via ``mark_telegraph_published``
    (Decision 9) and passing them explicitly avoids a hidden read.

    **Post-commit defensive verification (added 2026-05-08):** after the
    main transaction commits, re-query ``processed_news`` for the link.
    If missing, dozapis with ``INSERT OR IGNORE``. This guards against a
    historical anomaly where ``published_articles`` ended up populated but
    ``processed_news`` did not (root cause unknown — possibly a transient
    SQLite issue, an older code path that lacked Step 2, or a manual
    ``ATTACH``-merge that overwrote ``processed_news``). The defensive
    check makes the eventual state idempotent regardless of how it was
    reached. A WARNING is emitted iff the dozapis actually inserts —
    surface to operator for investigation. See SESSION-2026-05-08.md.
    """
    conn = _connect()
    try:
        # Step 0: read the pending row's EN/RU fields for the published-row copy.
        #
        # Deliberate SELECT-by-name (title, ru_title, source_name, pub_date,
        # model_fingerprint) rather than SELECT * — if future schema adds
        # columns we don't want to drag them into published_articles silently.
        # ``model_fingerprint`` is carried through for cross-source-dedup
        # AC2 — the fingerprint computed at fetch time stays addressable
        # via ``list_recent_published_fingerprints`` for the 7-day window
        # without re-running the extractor on the published-articles table.
        src = conn.execute(
            "SELECT title, ru_title, source_name, pub_date, model_fingerprint "
            "FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        if src is None:
            # Missing pending row at move time (audit CA-1b): the only
            # production path here is an operator cancel racing an
            # in-flight publish — ``skip_pending`` deleted the row AFTER
            # the Telegram teaser went out but BEFORE this move (the
            # pre-teaser guard in ``_fallback_publish`` closes the wider
            # window). The channel post EXISTS at this point, so a silent
            # no-op would leave ``published_articles`` without a row for a
            # real post — skewing the 7-day fingerprint window, the E017
            # dry-spell check and the E034 recap. Mirror the post-commit
            # defensive verification below: WARN loudly, then dozapis the
            # published row from the explicit args. ``title`` is recovered
            # from the ``processed_news`` stamp the skip left (falling
            # back to the link for the NOT NULL columns); the RU title is
            # unrecoverable (``update_staged`` no-oped on the deleted row).
            logger.warning(
                "[move_to_published] pending row for %s missing at move "
                "time (operator cancel raced an in-flight publish?) — the "
                "channel post exists, so dozapis published_articles from "
                "explicit args instead of silently dropping the audit row",
                link,
            )
            recovered = conn.execute(
                "SELECT title, pub_date FROM processed_news WHERE link=?",
                (link,),
            ).fetchone()
            title = (recovered[0] if recovered and recovered[0] else None) \
                or link
            pub_date = recovered[1] if recovered else None
            conn.execute(
                "INSERT OR IGNORE INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    link, title, title, telegraph_url, telegraph_path,
                    '', 1 if via_review else 0,
                ),
            )
            # Dedup stamp — a no-op when skip_pending already wrote it.
            conn.execute(
                "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
                "VALUES (?, ?, ?)",
                (link, title, pub_date),
            )
            conn.commit()
            return
        title, ru_title, source_name, pub_date, model_fingerprint = src

        # Step 1
        conn.execute(
            "INSERT OR IGNORE INTO published_articles "
            "(link, title, ru_title, telegraph_url, telegraph_path, "
            " source_name, via_review, model_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link, title, ru_title, telegraph_url, telegraph_path,
                source_name, 1 if via_review else 0, model_fingerprint,
            ),
        )
        # Step 2
        conn.execute(
            "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
            "VALUES (?, ?, ?)",
            (link, title, pub_date),
        )
        # Step 3
        conn.execute(
            "DELETE FROM pending_articles WHERE link=?",
            (link,),
        )
        conn.commit()

        # Post-commit defensive verification: ensure processed_news has
        # the entry. In the happy path this is a no-op (Step 2 already
        # inserted). The historical anomaly case (May 2026 production
        # incident) had published_articles populated but processed_news
        # missing — this re-query + dozapis closes that loophole regardless
        # of how the inconsistency arose.
        check = conn.execute(
            "SELECT 1 FROM processed_news WHERE link=?",
            (link,),
        ).fetchone()
        if check is None:
            logger.warning(
                "[move_to_published] post-commit defensive: processed_news "
                "missing entry for %s after main transaction; dozapis with "
                "INSERT OR IGNORE — investigate why Step 2 did not persist",
                link,
            )
            conn.execute(
                "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
                "VALUES (?, ?, ?)",
                (link, title, pub_date),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def move_to_failed(link: str, last_error: Optional[str]) -> None:
    """Move a pending row into ``failed_articles`` and DELETE from pending,
    atomically. EN fields + (if set) ``telegraph_url`` / ``telegraph_path``
    are preserved on failed so ``retry_from_failed`` can re-queue without
    re-fetching AND without creating a second Telegraph page (Decision 9
    idempotency holds across the failed/retry boundary).
    """
    conn = _connect()
    try:
        src = conn.execute(
            "SELECT title, source_name, paragraphs, images, blocks, "
            "       subtitle, pub_date, feed_url, fetched_at, "
            "       telegraph_url, telegraph_path "
            "FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        if src is None:
            return
        (title, source_name, paragraphs, images, blocks, subtitle,
         pub_date, feed_url, fetched_at,
         telegraph_url, telegraph_path) = src

        conn.execute(
            "INSERT INTO failed_articles "
            "(link, title, source_name, paragraphs, images, blocks, "
            " subtitle, pub_date, feed_url, last_error, original_fetched_at, "
            " telegraph_url, telegraph_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link, title, source_name, paragraphs, images, blocks,
                subtitle or '', pub_date, feed_url, last_error, fetched_at,
                telegraph_url, telegraph_path,
            ),
        )
        conn.execute(
            "DELETE FROM pending_articles WHERE link=?",
            (link,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def skip_pending(link: str) -> None:
    """Write the link to ``processed_news`` (dedup) and DELETE from pending.

    NO write to ``published_articles`` — skip is not a publish (AC user-spec
    L74 / tech-spec Decision 2).
    """
    conn = _connect()
    try:
        src = conn.execute(
            "SELECT title, pub_date FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        if src is None:
            return
        title, pub_date = src

        conn.execute(
            "INSERT OR IGNORE INTO processed_news (link, title, pub_date) "
            "VALUES (?, ?, ?)",
            (link, title, pub_date),
        )
        conn.execute(
            "DELETE FROM pending_articles WHERE link=?",
            (link,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_hold(link: str) -> bool:
    """Release a content-gate hold: ``hold_reason = NULL`` for ``link``.

    The «✅ Опубликовать» half of the [E036] keyboard. The row is already
    staged (title, body, images, fingerprint), so approval is a single
    UPDATE — from the next slot on it is an ordinary queue member and
    publishes in its turn.

    Returns ``True`` when a HELD row was actually released, ``False`` when
    there was nothing to release (row gone, or already approved). The
    caller uses that to tell «одобрено» from «статья уже недоступна»
    without a second round-trip, and a double press resolves to ``False``
    instead of reporting a fresh approval.

    Never touches ``processed_news`` / ``published_articles``: approving is
    not publishing, it only unparks the row.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE pending_articles SET hold_reason=NULL "
            "WHERE link=? AND hold_reason IS NOT NULL",
            (link,),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def retry_from_failed(link: str) -> bool:
    """Re-queue a failed row.

    Creates a fresh pending row (``attempt_count=0``, all ``ru_*`` NULL,
    ``fetched_at=CURRENT_TIMESTAMP``, no ``notified_at`` / ``telegraph_*`` /
    ``last_error``) and DELETEs the failed row — one transaction.

    Returns ``False`` and leaves state untouched if the failed row is
    missing OR if a pending row for the same link already exists (defensive
    PK-collision guard matching ``insert_pending``'s style).
    """
    conn = _connect()
    try:
        # Guard 1: link must NOT be in pending already.
        clash = conn.execute(
            "SELECT 1 FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        if clash is not None:
            return False

        # Guard 2: link must be present in failed.
        src = conn.execute(
            "SELECT title, source_name, paragraphs, images, blocks, "
            "       subtitle, pub_date, feed_url, "
            "       telegraph_url, telegraph_path "
            "FROM failed_articles WHERE link=?",
            (link,),
        ).fetchone()
        if src is None:
            return False
        (title, source_name, paragraphs, images, blocks, subtitle,
         pub_date, feed_url, telegraph_url, telegraph_path) = src

        # INSERT pending with explicit resets. `subtitle` may be NULL on legacy
        # failed rows — coerce to ''. JSON fields are already TEXT, passed
        # through unchanged. ``telegraph_url`` / ``telegraph_path`` are
        # preserved (may be NULL pre-migration / pre-Telegraph step) so the
        # next publish attempt re-uses the existing Telegraph page rather
        # than creating an orphan duplicate.
        conn.execute(
            "INSERT INTO pending_articles "
            "(link, source_name, feed_url, title, subtitle, paragraphs, "
            " images, blocks, fetched_at, attempt_count, pub_date, "
            " telegraph_url, telegraph_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0, ?, ?, ?)",
            (
                link, source_name, feed_url, title, subtitle or '',
                paragraphs, images, blocks, pub_date,
                telegraph_url, telegraph_path,
            ),
        )
        conn.execute(
            "DELETE FROM failed_articles WHERE link=?",
            (link,),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-source dedup helpers (tech-spec cross-source-dedup, Decisions 5/6/11)
# ---------------------------------------------------------------------------
#
# The three query/write helpers below accept ``conn`` as their first
# parameter (vs the short-lived ``_connect()`` pattern used by older repo
# helpers). This is deliberate — the backfill script (Task 5) holds a single
# long-lived transaction across many rows, so the helpers must let the
# caller decide when to commit / rollback.

def list_recent_pending_fingerprints(conn: sqlite3.Connection,
                                     days: int = 7) -> list[dict]:
    """Return pending rows fetched within the last ``days`` days as a list
    of dicts with ``model_fingerprint`` JSON-deserialised.

    Projection: ``link``, ``source_name``, ``title``, ``model_fingerprint``,
    ``fetched_at``. ``days`` is bound via a ``?`` placeholder
    (``datetime('now', ? || ' days')`` with ``-{days}``) — matches the
    ``list_pending_stale`` style so the ``TestSqlAudit`` SQL-injection
    audit stays green.

    Used by the dedup gate in ``news_bot.job()`` (Task 4): reads the
    7-day window of fingerprint candidates and runs
    ``model_extractor.similarity`` against each.
    """
    cur = conn.execute(
        "SELECT link, source_name, title, model_fingerprint, fetched_at "
        "FROM pending_articles "
        "WHERE fetched_at >= datetime('now', ? || ' days')",
        (f"-{int(days)}",),
    )
    rows = cur.fetchall()
    desc = cur.description
    return [_row_to_dict(r, desc, _PENDING_JSON_COLS) for r in rows]


def list_recent_published_fingerprints(conn: sqlite3.Connection,
                                       days: int = 7) -> list[dict]:
    """Return published rows published within the last ``days`` days as
    a list of dicts with ``model_fingerprint`` JSON-deserialised.

    Same shape as ``list_recent_pending_fingerprints`` but against
    ``published_articles`` / ``published_at``. Used by the dedup gate.
    """
    cur = conn.execute(
        "SELECT link, source_name, title, model_fingerprint, published_at "
        "FROM published_articles "
        "WHERE published_at >= datetime('now', ? || ' days')",
        (f"-{int(days)}",),
    )
    rows = cur.fetchall()
    desc = cur.description
    return [_row_to_dict(r, desc, _PUBLISHED_JSON_COLS) for r in rows]


def update_published_fingerprint(conn: sqlite3.Connection,
                                 link: str,
                                 fingerprint: Optional[dict]) -> None:
    """UPDATE ``published_articles.model_fingerprint`` for ``link``.

    Used by the backfill script (Task 5) to populate fingerprints on
    historical published rows that pre-date the dedup feature.
    ``fingerprint`` is JSON-encoded via ``_dumps`` (so a dict is stored as
    JSON text, ``None`` becomes NULL). Caller controls the transaction —
    no internal commit.
    """
    conn.execute(
        "UPDATE published_articles SET model_fingerprint=? WHERE link=?",
        (_dumps(fingerprint), link),
    )


# ---------------------------------------------------------------------------
# Cross-source dedup rate-limit helpers (Decision 6, bot_state-backed)
# ---------------------------------------------------------------------------
#
# Pattern mirrors ``outage_state._parse_dt`` — corrupt/unexpected values
# log a warning and return ``None`` so the dedup gate keeps working even
# if a manual ``bot_state`` edit broke a row.

def _parse_dt_tolerant(raw: Optional[str], key: str) -> Optional[datetime]:
    """ISO-8601 string → tz-aware datetime, or None on missing / corrupt.

    Mirrors ``outage_state._parse_dt``: corrupt content logs a warning
    instead of raising so a manual ``bot_state`` edit (or a format drift
    between feature versions) cannot block the dedup gate.
    """
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        # TypeError, not just ValueError: ``bot_state.value`` is declared TEXT,
        # but SQLite TEXT affinity leaves a BLOB a BLOB, so a row written as
        # x'..' comes back as ``bytes`` and ``fromisoformat`` raises TypeError.
        # Catching only ValueError let that escape into the callers' broad
        # handlers, turning one malformed row into a permanently silent gate.
        logger.warning("corrupted bot_state value at key=%s: %r", key, raw)
        return None


def _pair_key(new_link: str, existing_link: str) -> str:
    """Build the bot_state key for a (new, existing) link pair.

    Newline separator is chosen over ``:`` / ``/`` because links contain
    those characters; a newline is unambiguous (URLs cannot include it
    without percent-encoding). Decision 6.
    """
    return f"{_KEY_SOFTFLAG_PAIR_PREFIX}{new_link}\n{existing_link}"


def is_pair_rate_limited(conn: sqlite3.Connection,
                         new_link: str,
                         existing_link: str,
                         window_days: int = 7) -> bool:
    """True iff a soft-flag ping for this (new, existing) pair was sent
    within the last ``window_days`` days. Decision 6 / user-spec AC5.

    Missing key → False (no prior ping). Corrupted timestamp → False +
    warning (tolerant — parity with ``outage_state._parse_dt``).
    """
    key = _pair_key(new_link, existing_link)
    row = conn.execute(
        "SELECT value FROM bot_state WHERE key=?",
        (key,),
    ).fetchone()
    if row is None:
        return False
    last = _parse_dt_tolerant(row[0], key)
    if last is None:
        return False
    now = datetime.now(timezone.utc)
    # If the stored timestamp is naive (legacy entry), assume UTC by
    # replacing tzinfo — keeps the comparison meaningful.
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) < timedelta(days=window_days)


def mark_pair_pinged(conn: sqlite3.Connection,
                     new_link: str,
                     existing_link: str) -> None:
    """UPSERT the current UTC timestamp at the soft-flag pair key.

    Decision 6 / user-spec AC5. Caller controls the transaction (no
    internal commit) — same as the other ``conn``-accepting helpers in
    this section.
    """
    key = _pair_key(new_link, existing_link)
    value = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
        (key, value),
    )


def is_dedup_degraded_rate_limited(conn: sqlite3.Connection,
                                   window_hours: int = 1) -> bool:
    """True iff an E016 «dedup in degraded mode» ping was sent within
    the last ``window_hours`` hours. Decision 6 / user-spec AC9.

    Tolerates missing / corrupted values (returns False).
    """
    row = conn.execute(
        "SELECT value FROM bot_state WHERE key=?",
        (_KEY_DEDUP_DEGRADED,),
    ).fetchone()
    if row is None:
        return False
    last = _parse_dt_tolerant(row[0], _KEY_DEDUP_DEGRADED)
    if last is None:
        return False
    now = datetime.now(timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) < timedelta(hours=window_hours)


def mark_dedup_degraded_pinged(conn: sqlite3.Connection) -> None:
    """UPSERT the current UTC timestamp at the degraded-mode key.

    Decision 6 / user-spec AC9. Caller controls the transaction.
    """
    value = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
        (_KEY_DEDUP_DEGRADED, value),
    )


# ---------------------------------------------------------------------------
# Review-token store (dedup-review-buttons, tech-spec Decision 3)
# ---------------------------------------------------------------------------
#
# Token → link mapping behind the [E014] review buttons: callback_data is
# capped at 64 bytes, the queue PK is a full URL, so buttons carry a short
# ``secrets.token_urlsafe`` token and ``bot_state`` resolves it back.
#
# Unlike the ``conn``-accepting dedup helpers above, these three follow the
# ``outage_state._get`` / ``_set`` contour — each opens its own short-lived
# connection via ``_connect()``, owns the transaction, and closes in
# ``finally`` — because the callers (listener thread, alert sender) hold no
# connection of their own.

#: Token KINDS — which keyboard minted a token (audit SEC-CG-2). The
#: token store is one flat namespace, and dispatch is by action word, so
#: without this a token minted for one keyboard could be redeemed by the
#: other resolver. Reproduced both directions: a dedup token redeemed as
#: ``hold``/``reject`` silently ``skip_pending``s a NON-held article; a
#: hold token redeemed as ``dedup``/``keep`` consumes the token with no
#: state change, leaving the held article PERMANENTLY orphaned (frozen,
#: no live button, no re-mint path). Each resolver checks the kind and
#: treats a mismatch as a stale button — without consuming the token.
REVIEW_TOKEN_KIND_DEDUP = 'dedup'
REVIEW_TOKEN_KIND_HOLD = 'hold'
_REVIEW_TOKEN_KINDS = (REVIEW_TOKEN_KIND_DEDUP, REVIEW_TOKEN_KIND_HOLD)

#: Separator between kind and link in the stored value.
_REVIEW_TOKEN_SEP = '|'


def _review_token_key(token: str) -> str:
    """Build the bot_state key for a review token: prefix + token."""
    return f"{_KEY_REVIEW_TOKEN_PREFIX}{token}"


def put_review_token(token: str, link: str,
                     kind: str = REVIEW_TOKEN_KIND_DEDUP) -> None:
    """UPSERT ``review_token:<token>`` → ``<kind>|<link>`` in ``bot_state``.

    A repeat put with the same token overwrites the stored value.
    ``BEGIN IMMEDIATE`` mirrors ``outage_state._set`` — grab the write
    lock up front so the busy handler (not a lock-upgrade deadlock)
    resolves contention with the publish loop.

    ``kind`` records WHICH keyboard minted the token (audit SEC-CG-2).
    Encoded in the VALUE rather than the key so the change needs no
    migration and no janitor: tokens written before this change have no
    kind prefix and read back as ``dedup``, which is what they are — the
    hold keyboard did not exist then. Defaults to ``dedup`` for the same
    reason.
    """
    if kind not in _REVIEW_TOKEN_KINDS:
        raise ValueError(f"unknown review token kind: {kind!r}")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (_review_token_key(token), f"{kind}{_REVIEW_TOKEN_SEP}{link}"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_review_token(token: str) -> Optional[tuple]:
    """Return ``(kind, link)`` for ``token``, or ``None`` if unknown
    (expired, bot restarted, already consumed).

    Tolerates the pre-SEC-CG-2 value format (a bare link with no kind
    prefix) by reporting it as ``dedup`` — the only keyboard that existed
    when such a token could have been written. Splits on the FIRST
    separator and only when the prefix is a known kind, so a link that
    happens to contain ``|`` is never mangled.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (_review_token_key(token),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    value = row[0]
    if not isinstance(value, str):
        return None
    prefix, sep, rest = value.partition(_REVIEW_TOKEN_SEP)
    if sep and prefix in _REVIEW_TOKEN_KINDS:
        return (prefix, rest)
    # Legacy value: bare link, minted before token kinds existed.
    return (REVIEW_TOKEN_KIND_DEDUP, value)


def get_review_token_link(token: str) -> Optional[str]:
    """Return just the link stored for ``token``, or ``None`` if unknown.

    Kind-agnostic convenience wrapper over ``get_review_token`` — used by
    the listener's decision log, which records what the operator acted on
    regardless of which keyboard it came from. Resolvers must use
    ``get_review_token`` so they can enforce the kind.
    """
    entry = get_review_token(token)
    return entry[1] if entry is not None else None


def delete_review_token(token: str) -> None:
    """Delete the token row. Deleting an absent token is a safe no-op —
    idempotent by design (double button press, restart races)."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM bot_state WHERE key=?",
            (_review_token_key(token),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


#: Outcomes returned by ``approve_hold_and_consume_token``. The repository
#: reports queue state; ``news_bot`` owns the operator-facing Russian text.
HOLD_APPROVAL_STALE = 'stale'
HOLD_APPROVAL_UNAVAILABLE = 'unavailable'
HOLD_APPROVAL_DEFERRED = 'deferred'
HOLD_APPROVAL_READY = 'ready'


def approve_hold_and_consume_token(token: str, link: str) -> str:
    """Atomically approve one E036 hold and consume its review token.

    Revalidates the exact hold-token mapping under ``BEGIN IMMEDIATE`` so
    concurrent presses cannot both report a fresh approval. The target row is
    read and updated in the same short transaction; a future ``publish_after``
    is reported as ``deferred`` while an otherwise eligible row is ``ready``.

    Returns one of the public ``HOLD_APPROVAL_*`` outcomes:

    - ``stale`` — token missing, wrong kind, or mapped to another link; no write;
    - ``unavailable`` — row missing or no longer held; token consumed;
    - ``deferred`` — hold cleared, future timed gate remains, token consumed;
    - ``ready`` — hold cleared with no future timed gate, token consumed.

    Hold release and token consumption are all-or-nothing. In particular, a
    failed token delete rolls the preceding hold update back.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        token_key = _review_token_key(token)
        token_row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (token_key,),
        ).fetchone()
        expected_value = (
            f"{REVIEW_TOKEN_KIND_HOLD}{_REVIEW_TOKEN_SEP}{link}"
        )
        if token_row is None or token_row[0] != expected_value:
            conn.rollback()
            return HOLD_APPROVAL_STALE

        row = conn.execute(
            "SELECT hold_reason, "
            "CASE WHEN publish_after IS NOT NULL "
            "AND publish_after > CURRENT_TIMESTAMP THEN 1 ELSE 0 END "
            "FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        if row is None or row[0] is None:
            outcome = HOLD_APPROVAL_UNAVAILABLE
        else:
            released = conn.execute(
                "UPDATE pending_articles SET hold_reason=NULL "
                "WHERE link=? AND hold_reason IS NOT NULL",
                (link,),
            )
            if released.rowcount != 1:
                raise sqlite3.OperationalError(
                    "hold approval lost its target row",
                )
            outcome = (
                HOLD_APPROVAL_DEFERRED if bool(row[1])
                else HOLD_APPROVAL_READY
            )

        consumed = conn.execute(
            "DELETE FROM bot_state WHERE key=?", (token_key,),
        )
        if consumed.rowcount != 1:
            raise sqlite3.OperationalError(
                "hold approval could not consume its review token",
            )
        conn.commit()
        return outcome
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
