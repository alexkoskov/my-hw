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

    # Migration (2026-04-30): preserve telegraph_url + telegraph_path on
    # the failed row so ``retry_from_failed`` can restore them and a
    # retry doesn't create a second Telegraph page (Decision 9 idempotency).
    # SQLite has no ``ADD COLUMN IF NOT EXISTS``; use a try/except so the
    # ALTER is a no-op on already-migrated DBs.
    #
    # Migration (2026-06-XX, cross-source-dedup, Decision 11): store the
    # model fingerprint JSON on both pending and published — same idempotent
    # try/except OperationalError pattern.
    #
    # Migration (2026-07-25, content-gate): ``pending_articles.hold_reason``
    # — nullable TEXT holding the matched content-gate markers. NULL (the
    # value every pre-migration row gets for free) means "publishable";
    # non-NULL means "HELD, awaiting the operator's «✅ Опубликовать»" and
    # is filtered out of ``list_pending`` / ``count_pending``. Nullable +
    # no default, so the ALTER is safe on the live prod DB with rows in it.
    for ddl in (
        "ALTER TABLE failed_articles ADD COLUMN telegraph_url TEXT",
        "ALTER TABLE failed_articles ADD COLUMN telegraph_path TEXT",
        "ALTER TABLE pending_articles ADD COLUMN model_fingerprint TEXT",
        "ALTER TABLE published_articles ADD COLUMN model_fingerprint TEXT",
        "ALTER TABLE pending_articles ADD COLUMN hold_reason TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            # Column already exists — idempotent path on subsequent
            # init_schema calls.
            pass
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
            " images, blocks, pub_date, model_fingerprint, hold_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            "SELECT COUNT(*) FROM pending_articles WHERE hold_reason IS NULL"
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
    except ValueError:
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

def _review_token_key(token: str) -> str:
    """Build the bot_state key for a review token: prefix + token."""
    return f"{_KEY_REVIEW_TOKEN_PREFIX}{token}"


def put_review_token(token: str, link: str) -> None:
    """UPSERT ``review_token:<token>`` → ``link`` in ``bot_state``.

    A repeat put with the same token overwrites the stored link.
    ``BEGIN IMMEDIATE`` mirrors ``outage_state._set`` — grab the write
    lock up front so the busy handler (not a lock-upgrade deadlock)
    resolves contention with the publish loop.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (_review_token_key(token), link),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_review_token_link(token: str) -> Optional[str]:
    """Return the link stored for ``token``, or ``None`` if unknown
    (expired, bot restarted, already consumed)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (_review_token_key(token),),
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


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
