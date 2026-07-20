#!/usr/bin/env python3
"""Unit tests for ``backfill_fingerprints.py`` (Task 5 of cross-source-dedup).

Mirrors the tempfile-DB pattern used by ``tests/test_pending_articles_repo.py``
and ``tests/test_hw_review_cli.py``: allocate a .db file, monkeypatch
``news_bot.DB_FILE``, run ``news_bot.init_db()`` so both the script's repo
calls and any test-side ``sqlite3.connect`` cursors target the same on-disk
file.

Test scenarios (TDD anchors from tasks/5.md):

* ``test_idempotency_second_run_processed_zero`` — second consecutive run
  observes ``model_fingerprint IS NOT NULL`` on all rows seeded by the first
  run, so SELECT returns 0 rows, summary reports ``Processed: 0``.
* ``test_days_window_honored`` — ``--days 7`` skips a row published 30 days
  ago and processes a row published "now".
* ``test_dry_run_writes_nothing`` — after ``--dry-run`` the DB column for the
  target row is still NULL.
* ``test_fetch_exception_leaves_null`` — ``fetch_full_article`` raising
  ``ConnectionError`` leaves the fingerprint NULL (transient, retry on next
  run) and bumps the ``Errors`` counter.
* ``test_fetch_no_body_left_null_for_retry`` — a no-usable-body result
  (``None``, a dict without ``paragraphs``, or empty ``paragraphs`` — all three
  guard legs) leaves the row NULL (bumps ``Unreachable``) and a later run
  RE-SELECTS it. Regression pin for the 403 write-off trap.
* ``test_reachable_empty_extractor_result_persists_terminal`` — a REACHABLE
  body whose extractor output is empty still persists the terminal four-key
  empty marker and is skipped on re-run (the legitimate computed-empty case is
  preserved, NOT collapsed into the retry path).
* ``test_summary_structure`` — stdout summary contains every header field
  (``Window:``, ``Processed:``, ``Skipped:``, ``Unreachable:``, ``Errors:``,
  ``Duration:``).
* ``test_days_clamp_rejects_out_of_range`` — argparse rejects ``--days 0``
  and ``--days 100`` with ``SystemExit(2)``.

Task-5 widened re-select scenarios (dedup-model-series):

* ``test_old_shape_row_reselected_and_upgraded`` — a row carrying the OLD
  two-key blob ``{"strict": [...], "brands": [...]}`` (no ``pairs`` key) is
  re-selected by the widened SELECT, re-fetched, and rewritten with the new
  four-key fingerprint; summary reports ``Processed: 1``.
* ``test_row_with_pairs_key_skipped`` — a row already carrying a ``pairs``
  key (e.g. the four-key empty form) is NOT re-selected → ``Processed: 0``,
  ``0 rows scanned``.
* ``test_backfilled_row_has_pairs_and_series_keys`` — after a run the
  persisted blob carries both ``pairs`` AND ``series`` keys (AC10/AC9).
* ``test_days_30_window_honored`` — ``--days 30`` includes a row ~29 days
  old and excludes one ~31 days old.
* ``test_real_body_persists_terminal_none_stays_retryable`` — the fix's core
  distinction in one run: a reachable body persists a terminal fingerprint
  (skipped next run) while a None fetch is left NULL (re-selected next run).
* ``test_corrupt_blob_reprocessed_without_crash`` — a raw invalid-JSON (or
  valid non-dict) ``model_fingerprint`` seeded directly via SQL does NOT crash
  the widened SELECT (guarded by ``json_valid``) nor the Python skip-guard; the
  row is re-selected and reprocessed into a valid four-key blob. Regression pin
  for the ``CASE WHEN json_valid(...)`` SQL predicate.
* ``test_old_shape_row_none_fetch_stays_retryable`` — compound edge case: an
  OLD *empty* two-key blob ``{"strict": [], "brands": []}`` whose re-fetch
  returns None is left UNCHANGED (still missing ``$.pairs``) and re-selected
  next run — not frozen into a terminal marker over a transient block.

Mock policy: ``news_bot.fetch_full_article`` is monkeypatched
(``monkeypatch.setattr``). ``model_extractor.extract_fingerprint`` is NOT
mocked — synthetic articles run through the real extractor so the round-trip
verifies repo + extractor + script wiring together (integration-flavoured,
cheaper than mocking).

``time.sleep`` is monkeypatched to a no-op so the suite stays fast even on
small (N×1s) windows.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot  # noqa: E402  — env-reads at import are harmless in tests
import model_extractor  # noqa: E402
import pending_articles_repo as repo  # noqa: E402
import backfill_fingerprints  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(monkeypatch):
    """Tempfile sqlite DB + ``news_bot.DB_FILE`` patch + schema init.

    Yields the path string so individual tests can hand it to a raw
    ``sqlite3.connect`` if they want to seed rows / assert state directly.
    """
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    monkeypatch.setattr(news_bot, 'DB_FILE', path)
    news_bot.init_db()
    # Also defang ``time.sleep`` so the 1-s inter-fetch pause doesn't pile
    # up across multi-row tests.
    monkeypatch.setattr(backfill_fingerprints.time, 'sleep', lambda _s: None)
    yield path
    if os.path.exists(path):
        os.unlink(path)


def _seed_published(db_path: str, link: str, *,
                    title: str = 'Sample title',
                    source_name: str = 'autoevolution',
                    ru_title: str = 'Заголовок',
                    telegraph_url: str = 'https://telegra.ph/x',
                    telegraph_path: str = 'x',
                    via_review: int = 0,
                    model_fingerprint: object = None,
                    published_at: str = None) -> None:
    """INSERT one row into ``published_articles``. ``published_at=None`` uses
    SQLite's ``CURRENT_TIMESTAMP`` default. ``model_fingerprint`` is JSON-
    encoded when not None (matches the storage contract)."""
    conn = sqlite3.connect(db_path)
    try:
        if published_at is None:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review, model_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (link, title, ru_title, telegraph_url, telegraph_path,
                 source_name, via_review,
                 json.dumps(model_fingerprint, ensure_ascii=False)
                 if model_fingerprint is not None else None),
            )
        else:
            conn.execute(
                "INSERT INTO published_articles "
                "(link, title, ru_title, telegraph_url, telegraph_path, "
                " source_name, via_review, published_at, model_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (link, title, ru_title, telegraph_url, telegraph_path,
                 source_name, via_review, published_at,
                 json.dumps(model_fingerprint, ensure_ascii=False)
                 if model_fingerprint is not None else None),
            )
        conn.commit()
    finally:
        conn.close()


def _get_fp_raw(db_path: str, link: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT model_fingerprint FROM published_articles WHERE link=?",
            (link,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _fake_article_with_brand(*_args, **_kwargs):
    """Stub for ``news_bot.fetch_full_article`` that returns a well-formed
    article body whose paragraphs carry brand+model tokens the real
    ``extract_fingerprint`` will pick up. Returning a non-trivial fingerprint
    makes the ``updated`` path observable without us hand-rolling JSON."""
    return {
        'title': '2018 Toyota 4Runner premium release',
        'subtitle': '',
        'paragraphs': [
            'The new Toyota 4Runner casting joins the lineup alongside the '
            'Subaru Legacy GT.',
        ],
        'images': [],
        'blocks': None,
    }


def _fake_reachable_body(*_args, **_kwargs):
    """Stub returning a well-formed REACHABLE body (has ``paragraphs``). Pairs
    with a monkeypatched ``extract_fingerprint`` so a test can drive the
    "reachable but extractor-empty" path deterministically, independent of the
    real extractor's brand list."""
    return {
        'title': 'Quarterly collector market recap',
        'subtitle': '',
        'paragraphs': ['A general market overview with no specific castings.'],
        'images': [],
        'blocks': None,
    }


# ---------------------------------------------------------------------------
# TDD anchor tests
# ---------------------------------------------------------------------------


def test_idempotency_second_run_processed_zero(temp_db, monkeypatch, capsys):
    """Two consecutive runs: first processes N rows, second has nothing to
    do because all rows now carry a non-NULL ``model_fingerprint``.

    Canonical idempotency marker per tech-spec Decision 10: ``IS NULL`` ==
    "not processed". The reachable-body ``updated`` path writes a non-NULL
    value, so the second run's SELECT returns zero rows and the summary
    reports ``Processed: 0`` (and ``Unreachable: 0``).
    """
    for i in range(3):
        _seed_published(temp_db, f'https://example.com/a{i}')

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    rc1 = backfill_fingerprints.main(['--days', '14'])
    out1 = capsys.readouterr().out
    assert rc1 == 0
    assert 'Processed: 3 ' in out1, out1

    rc2 = backfill_fingerprints.main(['--days', '14'])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    # Second run sees 0 rows matching the IS NULL filter — scanned count
    # AND processed/empty-fp counters are all zero.
    assert 'Processed: 0 ' in out2, out2
    assert 'Unreachable: 0 ' in out2, out2
    assert '(0 rows scanned)' in out2, out2


def test_days_window_honored(temp_db, monkeypatch, capsys):
    """``--days 7`` must include a row published "now" and exclude a row
    published 30 days ago. Verified by counting the ``Processed`` summary
    line and checking that only the in-window row's fingerprint became
    non-NULL.
    """
    _seed_published(temp_db, 'https://example.com/fresh')
    _seed_published(temp_db, 'https://example.com/stale')
    # Re-stamp the stale row via direct UPDATE so its published_at is a
    # real -30d datetime (the seed helper uses parameterised INSERTs —
    # passing a SQL expression as a string would store the literal text).
    conn = sqlite3.connect(temp_db)
    try:
        conn.execute(
            "UPDATE published_articles "
            "SET published_at = datetime('now', '-30 days') "
            "WHERE link=?",
            ('https://example.com/stale',),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    rc = backfill_fingerprints.main(['--days', '7'])
    out = capsys.readouterr().out
    assert rc == 0
    # One row (the fresh one) processed, stale untouched.
    assert 'Processed: 1 ' in out, out

    assert _get_fp_raw(temp_db, 'https://example.com/fresh') is not None
    assert _get_fp_raw(temp_db, 'https://example.com/stale') is None


def test_dry_run_writes_nothing(temp_db, monkeypatch, capsys):
    """``--dry-run`` short-circuits the ``update_published_fingerprint``
    call — the DB column stays NULL even though the extractor ran (we
    can't observe the in-flight ``fp`` here; the contract is that the DB
    is untouched)."""
    _seed_published(temp_db, 'https://example.com/dry')

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    rc = backfill_fingerprints.main(['--dry-run', '--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Processed: 1 ' in out, out
    # Persisted value remains NULL.
    assert _get_fp_raw(temp_db, 'https://example.com/dry') is None


def test_fetch_exception_leaves_null(temp_db, monkeypatch, capsys):
    """``fetch_full_article`` raising → row's ``model_fingerprint`` stays
    NULL (so a subsequent run retries) and the ``Errors`` counter bumps."""
    _seed_published(temp_db, 'https://example.com/boom')

    def _raise(*_a, **_kw):
        raise ConnectionError('Cloudflare 403')

    monkeypatch.setattr(news_bot, 'fetch_full_article', _raise)

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Errors: 1' in out, out
    assert 'Unreachable: 0 ' in out, out   # a raise is the Errors path, not Unreachable
    assert _get_fp_raw(temp_db, 'https://example.com/boom') is None


@pytest.mark.parametrize('no_body', [
    None,                              # fetch returned nothing (swallowed 403)
    {'title': 't'},                    # dict without a 'paragraphs' key
    {'title': 't', 'paragraphs': []},  # present but empty paragraphs
], ids=['none', 'no-paragraphs-key', 'empty-paragraphs'])
def test_fetch_no_body_left_null_for_retry(temp_db, monkeypatch, capsys, no_body):
    """A no-usable-body fetch result leaves the row's ``model_fingerprint``
    NULL — NOT a terminal empty marker — so a later run retries it. Covers all
    three legs of the ``not article or not article.get('paragraphs')`` guard:
    ``None`` (e.g. a transient 403 the source swallows into a None "defer"), a
    dict lacking ``paragraphs``, and one with empty ``paragraphs``.

    Regression pin for the 403 write-off trap: persisting a four-key empty here
    would carry a ``$.pairs`` key and drop the row from the dedup gate forever
    over a transient block. The row must therefore stay NULL and be RE-SELECTED
    on the next run (it does not converge — the deliberate trade documented in
    the module docstring)."""
    _seed_published(temp_db, 'https://example.com/none')

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        lambda *_a, **_kw: no_body)

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Unreachable: 1 ' in out, out
    assert 'Errors: 0' in out, out        # no exception raised → not the Errors path
    # Nothing persisted — the column is still NULL.
    assert _get_fp_raw(temp_db, 'https://example.com/none') is None

    # Second run RE-SELECTS the still-NULL row (proves it is NOT terminal).
    rc2 = backfill_fingerprints.main(['--days', '14'])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert '(1 rows scanned)' in out2, out2
    assert 'Unreachable: 1 ' in out2, out2
    assert _get_fp_raw(temp_db, 'https://example.com/none') is None


def test_reachable_empty_extractor_result_persists_terminal(
        temp_db, monkeypatch, capsys):
    """A REACHABLE body whose extractor output is empty (genuine "no brands
    found") still persists the terminal four-key empty marker and is skipped on
    a re-run — distinct from an unreachable None fetch, which is left NULL.

    Pins that the 403-trap fix did NOT collapse the legitimate computed-empty
    case into the retry path. ``extract_fingerprint`` is stubbed to the empty
    form (the one deliberate mock in this file) so the scenario is deterministic
    regardless of the real extractor's brand list."""
    _seed_published(temp_db, 'https://example.com/nobrands')

    monkeypatch.setattr(news_bot, 'fetch_full_article', _fake_reachable_body)
    monkeypatch.setattr(
        model_extractor, 'extract_fingerprint',
        lambda *_a, **_kw: {'strict': [], 'brands': [], 'series': [], 'pairs': []},
    )

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Processed: 1 ' in out, out        # reachable → terminal, counted updated
    assert 'Unreachable: 0 ' in out, out       # NOT the retry path

    decoded = json.loads(_get_fp_raw(temp_db, 'https://example.com/nobrands'))
    assert decoded == {'strict': [], 'brands': [], 'series': [], 'pairs': []}

    # Terminal — the ``$.pairs`` key means the second run skips it (0 scanned).
    rc2 = backfill_fingerprints.main(['--days', '14'])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert '(0 rows scanned)' in out2, out2


def test_summary_structure(temp_db, monkeypatch, capsys):
    """Stdout summary contains every header field in the contract shape
    (code-research §14.E.5 — operator-facing). Exact whitespace not
    required; field labels load-bearing."""
    _seed_published(temp_db, 'https://example.com/shape')

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    for label in ('Window:', 'Processed:', 'Skipped:', 'Unreachable:',
                  'Errors:', 'Duration:'):
        assert label in out, f'missing {label} in summary:\n{out}'


@pytest.mark.parametrize('bad_value', ['0', '-1', '91', '100', '9000'])
def test_days_clamp_rejects_out_of_range(temp_db, bad_value):
    """``--days`` outside ``[1, 90]`` exits via argparse with code 2 and a
    usage message on stderr."""
    with pytest.raises(SystemExit) as exc_info:
        backfill_fingerprints.main(['--days', bad_value])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Task-5 widened re-select tests (dedup-model-series)
# ---------------------------------------------------------------------------


def test_old_shape_row_reselected_and_upgraded(temp_db, monkeypatch, capsys):
    """A row that already carries the OLD two-key car-fingerprint
    ``{"strict": [...], "brands": [...]}`` (missing the ``pairs`` key) is
    re-selected by the widened SELECT, re-fetched, and rewritten with the
    new four-key fingerprint that now carries ``series``/``pairs``.

    This is the core motivation of Task 5: the old ``IS NULL``-only filter
    would silently skip such rows, so they'd never get car+series pairs.
    """
    _seed_published(
        temp_db, 'https://example.com/old',
        model_fingerprint={'strict': ['toyota 4runner'], 'brands': ['toyota']},
    )

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    # Re-selected + re-fetched + rewritten (not skipped).
    assert 'Processed: 1 ' in out, out

    decoded = json.loads(_get_fp_raw(temp_db, 'https://example.com/old'))
    assert 'pairs' in decoded, decoded
    assert 'series' in decoded, decoded
    # Real extractor picked up the brand+model tokens from the stub body.
    assert decoded['strict'], decoded


def test_row_with_pairs_key_skipped(temp_db, monkeypatch, capsys):
    """A row that ALREADY carries a ``pairs`` key (here the four-key empty
    form) must NOT be re-selected by the widened SELECT — ``json_extract``
    returns a non-NULL value for it — so nothing is scanned or processed."""
    _seed_published(
        temp_db, 'https://example.com/haspairs',
        model_fingerprint={'strict': [], 'brands': [], 'series': [], 'pairs': []},
    )

    def _should_not_be_called(*_a, **_kw):
        raise AssertionError('fetch_full_article must not run for a row that '
                             'already carries a pairs key')

    monkeypatch.setattr(news_bot, 'fetch_full_article', _should_not_be_called)

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Processed: 0 ' in out, out
    assert '(0 rows scanned)' in out, out


def test_backfilled_row_has_pairs_and_series_keys(temp_db, monkeypatch, capsys):
    """After a normal backfill the persisted blob carries the full four-key
    structure — both ``pairs`` and ``series`` keys present (AC10/AC9)."""
    _seed_published(temp_db, 'https://example.com/warm')

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Processed: 1 ' in out, out

    decoded = json.loads(_get_fp_raw(temp_db, 'https://example.com/warm'))
    assert 'pairs' in decoded, decoded
    assert 'series' in decoded, decoded


def test_days_30_window_honored(temp_db, monkeypatch, capsys):
    """``--days 30`` includes a row published ~29 days ago and excludes a
    row published ~31 days ago (window boundary honoured)."""
    _seed_published(temp_db, 'https://example.com/in29')
    _seed_published(temp_db, 'https://example.com/out31')
    # Re-stamp published_at via direct UPDATE (parameterised INSERT would
    # store a SQL expression as literal text).
    conn = sqlite3.connect(temp_db)
    try:
        conn.execute(
            "UPDATE published_articles "
            "SET published_at = datetime('now', '-29 days') WHERE link=?",
            ('https://example.com/in29',),
        )
        conn.execute(
            "UPDATE published_articles "
            "SET published_at = datetime('now', '-31 days') WHERE link=?",
            ('https://example.com/out31',),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    rc = backfill_fingerprints.main(['--days', '30'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Processed: 1 ' in out, out

    assert _get_fp_raw(temp_db, 'https://example.com/in29') is not None
    assert _get_fp_raw(temp_db, 'https://example.com/out31') is None


def test_real_body_persists_terminal_none_stays_retryable(
        temp_db, monkeypatch, capsys):
    """The core of the 403-trap fix in a single run, two rows, opposite fates:
    a reachable body persists a terminal fingerprint (carries ``$.pairs`` →
    skipped next run), while a None fetch is left NULL (re-selected next run).

    Pins the whole distinction at once — that ``updated`` and ``unreachable``
    diverge precisely on whether a body came back."""
    _seed_published(temp_db, 'https://example.com/reachable')
    _seed_published(temp_db, 'https://example.com/blocked')

    def _fetch(entry, *_a, **_kw):
        if entry['link'] == 'https://example.com/reachable':
            return _fake_article_with_brand()
        return None  # simulates a 403 the source layer swallowed into None

    monkeypatch.setattr(news_bot, 'fetch_full_article', _fetch)

    rc = backfill_fingerprints.main(['--days', '30'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Processed: 1 ' in out, out        # the reachable row
    assert 'Unreachable: 1 ' in out, out       # the blocked row

    reachable = json.loads(_get_fp_raw(temp_db, 'https://example.com/reachable'))
    assert 'pairs' in reachable, reachable
    assert _get_fp_raw(temp_db, 'https://example.com/blocked') is None

    # Second run re-selects ONLY the blocked row (reachable converged).
    rc2 = backfill_fingerprints.main(['--days', '30'])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert '(1 rows scanned)' in out2, out2
    assert 'Unreachable: 1 ' in out2, out2


@pytest.mark.parametrize('raw_blob', ['not-valid-json{', '[1, 2, 3]'])
def test_corrupt_blob_reprocessed_without_crash(temp_db, monkeypatch, capsys,
                                                raw_blob):
    """A row whose ``model_fingerprint`` holds a raw invalid-JSON blob (or a
    valid-but-non-dict blob like a JSON array) must NOT crash the run and must
    be treated as "not yet backfilled" → re-selected, re-fetched, rewritten as
    a valid four-key fingerprint.

    Seeded with a RAW ``UPDATE`` (not ``_seed_published``, which always
    ``json.dumps`` a valid object) so the stored text is genuinely malformed.

    Two layers are exercised:
      * SQL — with the naive ``json_extract(model_fingerprint, '$.pairs') IS
        NULL`` predicate, ``'not-valid-json{'`` makes SQLite raise
        ``OperationalError: malformed JSON`` on the eager ``fetchall()`` and
        aborts the whole run; the ``CASE WHEN json_valid(...)`` guard yields
        NULL (→ re-selected) instead. Revert that fix and this test ERRORS.
      * Python — the ``json.loads`` try/except and ``isinstance(..., dict)``
        check in ``_already_backfilled`` keep a corrupt / non-dict blob from
        crashing the skip-guard. The ``[1, 2, 3]`` case pins the isinstance
        branch (valid JSON, so it survives even the naive SQL predicate).
    """
    _seed_published(temp_db, 'https://example.com/corrupt')
    # Overwrite with a raw blob via direct SQL — bypasses the seed helper's
    # json.dumps so the stored text is exactly what we set.
    conn = sqlite3.connect(temp_db)
    try:
        conn.execute(
            "UPDATE published_articles SET model_fingerprint = ? WHERE link = ?",
            (raw_blob, 'https://example.com/corrupt'),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        _fake_article_with_brand)

    # Must NOT raise (a reverted SQL fix would propagate OperationalError here).
    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    # Re-selected + reprocessed (proves the row survived the SELECT and fell
    # through the guard to fetch_full_article, rather than crashing or skipping).
    assert 'Processed: 1 ' in out, out

    raw = _get_fp_raw(temp_db, 'https://example.com/corrupt')
    assert raw is not None
    decoded = json.loads(raw)  # now valid JSON
    assert set(decoded) == {'strict', 'brands', 'series', 'pairs'}, decoded


def test_old_shape_row_none_fetch_stays_retryable(temp_db, monkeypatch, capsys):
    """Compound edge case: an OLD *empty* two-key blob ``{"strict": [],
    "brands": []}`` whose re-fetch returns None is left UNCHANGED — still the
    two-key form, still missing ``$.pairs`` — and re-selected on the next run
    (counted ``Unreachable``), rather than frozen into a terminal marker.

    Under the pre-fix behaviour this row would have been rewritten to a terminal
    four-key empty marker on a transient 403 and written off the dedup gate
    forever. Now it waits for a reachable retry. Distinct from
    ``test_old_shape_row_reselected_and_upgraded`` (reachable body → real
    upgrade)."""
    _seed_published(
        temp_db, 'https://example.com/oldempty',
        model_fingerprint={'strict': [], 'brands': []},
    )

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        lambda *_a, **_kw: None)

    rc1 = backfill_fingerprints.main(['--days', '30'])
    out1 = capsys.readouterr().out
    assert rc1 == 0
    assert 'Unreachable: 1 ' in out1, out1

    # Blob unchanged — still the old two-key form, still missing pairs.
    decoded = json.loads(_get_fp_raw(temp_db, 'https://example.com/oldempty'))
    assert decoded == {'strict': [], 'brands': []}

    # Still re-selected on the next run (did NOT converge to a terminal marker).
    rc2 = backfill_fingerprints.main(['--days', '30'])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert '(1 rows scanned)' in out2, out2
    assert 'Unreachable: 1 ' in out2, out2
