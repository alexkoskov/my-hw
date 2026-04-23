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
from typing import Any, Iterable, Optional

import news_bot  # for DB_FILE at call time — see module docstring


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS pending_articles (
    link           TEXT PRIMARY KEY,
    source_name    TEXT NOT NULL,
    feed_url       TEXT,
    title          TEXT NOT NULL,
    subtitle       TEXT NOT NULL DEFAULT '',
    paragraphs     TEXT NOT NULL,
    images         TEXT NOT NULL DEFAULT '[]',
    blocks         TEXT,
    ru_title       TEXT,
    ru_subtitle    TEXT,
    ru_paragraphs  TEXT,
    ru_blocks      TEXT,
    telegraph_url  TEXT,
    telegraph_path TEXT,
    fetched_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notified_at    TIMESTAMP,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    pub_date       TEXT
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

# JSON-serialised columns per table. Used by the row→dict converters to
# deserialise list/dict fields, distinguishing NULL (absence) from "[]"
# (empty-but-present). See tech-spec §9.13.
_PENDING_JSON_COLS = ('paragraphs', 'images', 'blocks', 'ru_paragraphs', 'ru_blocks')
_FAILED_JSON_COLS = ('paragraphs', 'images', 'blocks')


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
    call time so tempfile patches in tests are honoured."""
    return sqlite3.connect(news_bot.DB_FILE)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_schema(conn: sqlite3.Connection) -> None:
    """Create the three new tables if missing. Idempotent.

    Takes an already-open ``conn`` so ``news_bot.init_db`` can re-use its own
    connection, and tests can pass ``:memory:``.
    """
    conn.execute(_PENDING_DDL)
    conn.execute(_PUBLISHED_DDL)
    conn.execute(_FAILED_DDL)
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
        conn.execute(
            "INSERT INTO pending_articles "
            "(link, source_name, feed_url, title, subtitle, paragraphs, images, blocks, pub_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def list_pending() -> list[dict]:
    """All pending rows, oldest first. Backs CLI ``list`` + admin-ping
    counter."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles ORDER BY fetched_at ASC"
        )
        rows = cur.fetchall()
        desc = cur.description
    finally:
        conn.close()
    return [_row_to_dict(r, desc, _PENDING_JSON_COLS) for r in rows]


def list_pending_stale(hours: int = 48) -> list[dict]:
    """Rows older than ``hours`` with no heads-up ping yet (``notified_at IS
    NULL``). Caller will ping + ``mark_notified`` for each (Decision 12)."""
    conn = _connect()
    try:
        # `hours` is parameterised — the SQL body is constant.
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE notified_at IS NULL "
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
    ``ru_paragraphs`` is still NULL — the idle-fallback GT-publish pool."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE notified_at IS NOT NULL "
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
    oldest first (Decision 7 — staged rows are never evicted)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT * FROM pending_articles "
            "WHERE ru_paragraphs IS NULL "
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
    """Number of rows currently in the queue."""
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM pending_articles").fetchone()
        return int(row[0])
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
    """
    conn = _connect()
    try:
        # Step 0: read the pending row's EN/RU fields for the published-row copy.
        #
        # Deliberate SELECT-by-name (title, ru_title, source_name, pub_date)
        # rather than SELECT * — if future schema adds columns we don't want
        # to drag them into published_articles silently.
        src = conn.execute(
            "SELECT title, ru_title, source_name, pub_date "
            "FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        if src is None:
            # Nothing to move; treat as no-op rather than error. Caller should
            # have guarded against this, but a missing row is not corruption.
            return
        title, ru_title, source_name, pub_date = src

        # Step 1
        conn.execute(
            "INSERT INTO published_articles "
            "(link, title, ru_title, telegraph_url, telegraph_path, "
            " source_name, via_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                link, title, ru_title, telegraph_url, telegraph_path,
                source_name, 1 if via_review else 0,
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
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def move_to_failed(link: str, last_error: Optional[str]) -> None:
    """Move a pending row into ``failed_articles`` and DELETE from pending,
    atomically. EN fields are preserved on failed so ``retry_from_failed``
    can re-queue without re-fetching (AC user-spec L72).
    """
    conn = _connect()
    try:
        src = conn.execute(
            "SELECT title, source_name, paragraphs, images, blocks, "
            "       subtitle, pub_date, feed_url, fetched_at "
            "FROM pending_articles WHERE link=?",
            (link,),
        ).fetchone()
        if src is None:
            return
        (title, source_name, paragraphs, images, blocks, subtitle,
         pub_date, feed_url, fetched_at) = src

        conn.execute(
            "INSERT INTO failed_articles "
            "(link, title, source_name, paragraphs, images, blocks, "
            " subtitle, pub_date, feed_url, last_error, original_fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link, title, source_name, paragraphs, images, blocks,
                subtitle or '', pub_date, feed_url, last_error, fetched_at,
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
            "       subtitle, pub_date, feed_url "
            "FROM failed_articles WHERE link=?",
            (link,),
        ).fetchone()
        if src is None:
            return False
        (title, source_name, paragraphs, images, blocks, subtitle,
         pub_date, feed_url) = src

        # INSERT pending with explicit resets. `subtitle` may be NULL on legacy
        # failed rows — coerce to ''. JSON fields are already TEXT, passed
        # through unchanged.
        conn.execute(
            "INSERT INTO pending_articles "
            "(link, source_name, feed_url, title, subtitle, paragraphs, "
            " images, blocks, fetched_at, attempt_count, pub_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0, ?)",
            (
                link, source_name, feed_url, title, subtitle or '',
                paragraphs, images, blocks, pub_date,
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
