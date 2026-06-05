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
* ``test_fetch_empty_stores_computed_empty`` — ``fetch_full_article``
  returning ``None`` writes the terminal computed-empty marker
  ``{"strict": [], "brands": []}``; subsequent runs treat the row as
  already-processed.
* ``test_summary_structure`` — stdout summary contains every header field
  (``Window:``, ``Processed:``, ``Skipped:``, ``Empty fp:``, ``Errors:``,
  ``Duration:``).
* ``test_days_clamp_rejects_out_of_range`` — argparse rejects ``--days 0``
  and ``--days 100`` with ``SystemExit(2)``.

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


# ---------------------------------------------------------------------------
# TDD anchor tests
# ---------------------------------------------------------------------------


def test_idempotency_second_run_processed_zero(temp_db, monkeypatch, capsys):
    """Two consecutive runs: first processes N rows, second has nothing to
    do because all rows now carry a non-NULL ``model_fingerprint``.

    Canonical idempotency marker per tech-spec Decision 10: ``IS NULL`` ==
    "not processed". Both ``updated`` and ``empty-fp`` write a non-NULL
    value, so the second run's SELECT returns zero rows and the summary
    reports ``Processed: 0`` (and ``Empty fp: 0``).
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
    assert 'Empty fp:  0 ' in out2, out2
    assert '0 rows scanned' in out2, out2


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
    assert 'Errors:    1' in out, out
    assert _get_fp_raw(temp_db, 'https://example.com/boom') is None


def test_fetch_empty_stores_computed_empty(temp_db, monkeypatch, capsys):
    """``fetch_full_article`` returning ``None`` → terminal computed-empty
    fingerprint ``{"strict": [], "brands": []}`` is persisted; counter
    ``Empty fp`` bumps. Subsequent runs see non-NULL and skip the row."""
    _seed_published(temp_db, 'https://example.com/empty')

    monkeypatch.setattr(news_bot, 'fetch_full_article',
                        lambda *_a, **_kw: None)

    rc = backfill_fingerprints.main(['--days', '14'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Empty fp:  1 ' in out, out

    raw = _get_fp_raw(temp_db, 'https://example.com/empty')
    assert raw is not None
    decoded = json.loads(raw)
    assert decoded == {'strict': [], 'brands': []}

    # Idempotency probe — re-run, second time it's a skip not an empty-fp.
    rc2 = backfill_fingerprints.main(['--days', '14'])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert 'Processed: 0 ' in out2
    assert 'Empty fp:  0 ' in out2


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
    for label in ('Window:', 'Processed:', 'Skipped:', 'Empty fp:',
                  'Errors:', 'Duration:'):
        assert label in out, f'missing {label} in summary:\n{out}'


@pytest.mark.parametrize('bad_value', ['0', '-1', '91', '100', '9000'])
def test_days_clamp_rejects_out_of_range(temp_db, bad_value):
    """``--days`` outside ``[1, 90]`` exits via argparse with code 2 and a
    usage message on stderr."""
    with pytest.raises(SystemExit) as exc_info:
        backfill_fingerprints.main(['--days', bad_value])
    assert exc_info.value.code == 2
