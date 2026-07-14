#!/usr/bin/env python3
"""One-shot backfill: compute ``model_fingerprint`` for historical
``published_articles`` rows that pre-date the cross-source-dedup feature.

When to run
-----------
- **Once after first deploy** of cross-source-dedup. The dedup gate added in
  Task 4 looks back 7 days through ``pending_articles`` + ``published_articles``
  fingerprints; on a fresh deploy every historical row has
  ``model_fingerprint IS NULL`` so the gate has nothing to compare against
  and cannot catch cross-source duplicates for ~the first week.
- **Optionally** for any later partial top-up (e.g. after a Cloudflare outage
  left a batch of rows un-backfilled). Re-running is safe — see below.

Idempotency contract
--------------------
A row is "not yet processed" when ``model_fingerprint IS NULL`` **or** its
JSON blob is missing the ``$.pairs`` key (i.e. a pre-``dedup-model-series``
two-key fingerprint written before ``series``/``pairs`` existed). The widened
re-select warms both the base car-fingerprint and the new pairs in one pass;
a row is terminal only once its blob carries the four-key structure. Terminal
non-NULL states:

- ``'{"strict": [...], "brands": [...], "series": [...], "pairs": [...]}'`` —
  real fingerprint, the article body was reachable and the extractor produced
  tokens. Terminal.
- ``'{"strict": [], "brands": [], "series": [], "pairs": []}'`` —
  **computed-empty**. The article body was reachable but contained no
  brand+model tokens the extractor recognises (industry news, retrospective,
  or the article was deleted upstream so we got an empty body back). Also
  terminal — the ``$.pairs`` key is present, so a later run will not
  re-select it, and re-fetching would yield the same empty result anyway.

Old two-key blobs (``'{"strict": [...], "brands": [...]}'`` with no
``$.pairs``) are NOT terminal: they are re-selected and rewritten in the
four-key form so they gain ``series``/``pairs``.

If ``fetch_full_article`` raises (transient — Cloudflare 403, network
timeout, DNS hiccup), the row is left with ``model_fingerprint IS NULL`` and
counted as ``Errors``. A subsequent run picks it up again.

CLI
---
- ``--days N`` (default 14, clamped to ``[1, 90]``): backfill window in days.
  Clamp via custom ``argparse type=`` callable so out-of-range values exit
  cleanly with usage message + ``SystemExit(2)``.
- ``--dry-run``: compute fingerprints but skip the
  ``update_published_fingerprint`` call. The SELECT still runs, the rate-
  limit ``time.sleep(1)`` still fires, summary still printed — useful for
  estimating duration and verifying which rows would be touched.
- ``--verbose``: switch root logger to ``DEBUG`` and log every row's verdict.

Convention — import order: ``import news_bot`` lives at module top, BEFORE
``logging.basicConfig`` runs in ``main()``. ``news_bot`` installs the
``_TokenRedactingFilter`` unconditionally at module-load (news_bot.py:347),
so technically the order doesn't change the redaction behaviour — but we
keep this convention to match ``hw_review.py`` and to future-proof: if
``news_bot`` ever moves filter installation under a condition, OR we re-
order our own ``basicConfig`` call, this layout already does the right thing
without a silent regression.

Exit codes
----------
- 0 on success (including a clean "nothing to do" path).
- argparse handles usage errors with its own exit code 2.
- Unexpected exceptions propagate with a traceback — same convention as
  ``hw_review.py``; we never silently swallow ``Exception`` at top level.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time

# Convention: import news_bot before logging.basicConfig() — see module
# docstring. ``_TokenRedactingFilter`` (news_bot.py:347) is attached
# unconditionally on import, so order is technically irrelevant for
# redaction; we keep it to match hw_review.py and future-proof against
# upstream changes to either side.
import news_bot
import model_extractor
import pending_articles_repo as repo


logger = logging.getLogger(__name__)


# Window bounds for ``--days``. 1 day floor — anything smaller is just
# operator-error (a 0-day window matches nothing). 90 day ceiling — beyond
# that we'd re-fetch hundreds of articles, hit upstream rate limits, and
# the dedup gate only looks 7 days back anyway so additional coverage
# brings no value to the gate. Picked deliberately wide so a "catch up
# after a 2-month feature freeze" use case still works.
_DAYS_MIN = 1
_DAYS_MAX = 90

# Inter-fetch sleep. Cloudflare on autoevolution.com is the load-bearing
# upstream; 1 second per request keeps us well under any reasonable rate
# threshold while still finishing a ~50-row backfill in under a minute.
_FETCH_SLEEP_SECONDS = 1.0


# ---------------------------------------------------------------------------
# argparse helpers
# ---------------------------------------------------------------------------


def _days_in_range(raw: str) -> int:
    """``type=`` callable for ``--days``. Rejects non-int, then clamps to
    ``[_DAYS_MIN, _DAYS_MAX]`` via ``ArgumentTypeError`` (argparse renders
    it as a clean usage message + ``SystemExit(2)``)."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--days must be an integer, got {raw!r}")
    if value < _DAYS_MIN or value > _DAYS_MAX:
        raise argparse.ArgumentTypeError(
            f"--days must be in [{_DAYS_MIN}, {_DAYS_MAX}], got {value}"
        )
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='backfill_fingerprints',
        description=(
            'One-shot backfill: extract model_fingerprint for '
            'published_articles rows in the last N days. Idempotent — '
            'reprocesses rows missing the "pairs" key (NULL, corrupt, or a '
            'pre-dedup two-key fingerprint); skips rows already carrying '
            '"pairs".'
        ),
    )
    parser.add_argument(
        '--days', type=_days_in_range, default=14,
        help='backfill window in days (default 14, clamped to [1, 90])',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='compute fingerprints but do not write to DB',
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='log DEBUG details for every row processed',
    )
    return parser


# ---------------------------------------------------------------------------
# Per-row worker
# ---------------------------------------------------------------------------


def _already_backfilled(raw_fp) -> bool:
    """True when the row's RAW ``model_fingerprint`` blob already carries the
    ``pairs`` key and must be left untouched — a concurrent writer (or an
    already-upgraded row) beat us to it.

    The widened SELECT hands us the RAW JSON string (``dict(zip(...))``, not a
    repo helper that would deserialise), so we parse here. Everything that is
    NOT a dict already carrying ``pairs`` returns ``False`` → the row is
    (re)processed: a NULL blob, a genuinely-corrupt / non-JSON blob, a valid
    non-dict blob (e.g. a JSON array), or an old two-key form (no ``pairs``).
    ``json.loads`` is wrapped so a corrupt blob is treated as "not backfilled"
    (reprocess) rather than crashing the whole run — the SQL predicate lets
    such a row through, so this Python guard is its second line of defence.
    """
    if raw_fp is None:
        return False
    try:
        decoded_fp = json.loads(raw_fp)
    except (ValueError, TypeError):
        return False
    return isinstance(decoded_fp, dict) and 'pairs' in decoded_fp


def backfill_one(conn, row: dict, *, dry_run: bool) -> str:
    """Process a single ``published_articles`` row.

    Returns one of:
      * ``'skipped'`` — defensive race guard: the blob already carries a
        ``pairs`` key, so a concurrent writer (or an already-upgraded row)
        beat us to it and we must not clobber it.
      * ``'updated'`` — real fingerprint extracted and persisted.
      * ``'empty-fp'`` — fetch succeeded but the article body had no
        recognisable brand+model tokens; computed-empty marker
        ``{"strict": [], "brands": [], "series": [], "pairs": []}`` persisted.
      * ``'error'`` — ``fetch_full_article`` raised; row left NULL for a
        later retry.

    ``dry_run=True`` skips the ``update_published_fingerprint`` call but
    still classifies and returns the verdict, so the summary counters
    reflect what WOULD have happened.
    """
    # Skip only when the blob already carries the ``pairs`` key — the widened
    # SELECT deliberately re-selects old two-key blobs (NULL or missing
    # ``$.pairs``) so they get upgraded, so we must NOT reject them here. See
    # ``_already_backfilled`` for the parse-then-probe (corrupt-blob-safe).
    if _already_backfilled(row.get('model_fingerprint')):
        return 'skipped'

    link = row['link']
    entry_stub = {
        'link': link,
        'source_name': row.get('source_name') or '',
        'title': row.get('title') or '',
        # ``published`` is a feedparser convention — autoevolution's fetcher
        # accepts an empty string here (no upstream sort key needed for a
        # one-off backfill fetch).
        'published': '',
    }

    # Narrow try/except — ONLY over the network call. Bugs in
    # extract_fingerprint or update_published_fingerprint should bubble
    # up with a traceback (they would indicate a real implementation bug,
    # not a transient upstream failure).
    try:
        article = news_bot.fetch_full_article(entry_stub)
    except Exception as exc:  # noqa: BLE001 — broad catch is the contract
        logger.error("backfill failed for %s: %s", link, exc)
        return 'error'

    # Empty body — terminal computed-empty path. ``None``, ``{}`` without
    # the paragraphs key, and ``{'paragraphs': []}`` all collapse here.
    if not article or not article.get('paragraphs'):
        # Four-key empty form — must match extract_fingerprint's empty
        # early return. A two-key marker would lack ``$.pairs`` and be
        # re-selected on every run, breaking idempotency.
        empty_fp = {'strict': [], 'brands': [], 'series': [], 'pairs': []}
        if not dry_run:
            repo.update_published_fingerprint(conn, link, empty_fp)
            conn.commit()
        return 'empty-fp'

    fp = model_extractor.extract_fingerprint(article)
    if not dry_run:
        repo.update_published_fingerprint(conn, link, fp)
        conn.commit()
    return 'updated'


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _format_duration(elapsed_s: float) -> str:
    """Render an elapsed seconds count as ``"Xm Ys"`` (integer parts)."""
    minutes = int(elapsed_s // 60)
    seconds = int(elapsed_s % 60)
    return f"{minutes}m {seconds}s"


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    start = time.monotonic()

    # Open a single long-lived connection — the conn-accepting repo
    # helpers (list_recent_published_fingerprints, update_published_
    # fingerprint) all share it. Schema init is defensive: covers the case
    # where the operator runs this on a dev DB that never executed
    # news_bot.init_db (e.g. fresh checkout, manual `python3 backfill_
    # fingerprints.py` run before the daily cron has fired once).
    conn = sqlite3.connect(news_bot.DB_FILE)
    try:
        repo.init_schema(conn)

        # SELECT projection mirrors list_recent_published_fingerprints'
        # row shape but adds the "not yet processed" filter — we cannot
        # reuse that helper directly because it returns ALL rows in the
        # window (used by the dedup gate to inspect every candidate).
        # Backfill only cares about un-processed rows: NULL fingerprint OR
        # a blob missing the ``$.pairs`` key (a pre-dedup-model-series two-key
        # fingerprint, OR a corrupt/non-JSON blob) — all must be re-selected so
        # they gain ``series``/``pairs``. The ``json_valid`` CASE guard is
        # load-bearing: a bare ``json_extract(model_fingerprint, '$.pairs')``
        # raises ``OperationalError: malformed JSON`` on a corrupt blob, and
        # because ``fetchall()`` materialises eagerly ONE bad row would abort
        # the whole run (a ``NOT json_valid(x) OR json_extract(x, ...)`` guard
        # does NOT reliably short-circuit across SQLite builds). Wrapping the
        # extract in ``CASE WHEN json_valid(...) THEN json_extract(...) ELSE
        # NULL END`` yields NULL (→ "needs reprocessing") for malformed blobs
        # instead of throwing. ``json_extract(..., '$.pairs')`` stays a STATIC
        # SQL literal (no interpolation); ``days`` is the only bound ``?``
        # parameter — never f-string untrusted data into SQL bodies
        # (TestSqlAudit invariant).
        cur = conn.execute(
            "SELECT link, source_name, title, model_fingerprint "
            "FROM published_articles "
            "WHERE published_at >= datetime('now', ? || ' days') "
            "  AND (CASE WHEN json_valid(model_fingerprint) "
            "            THEN json_extract(model_fingerprint, '$.pairs') "
            "            ELSE NULL END) IS NULL",
            (f"-{int(args.days)}",),
        )
        col_names = [d[0] for d in cur.description]
        rows = [dict(zip(col_names, r)) for r in cur.fetchall()]
        total = len(rows)

        logger.info(
            "Scanning published_articles WHERE published_at >= -%d days "
            "AND (model_fingerprint IS NULL OR '$.pairs' missing) → %d rows",
            args.days, total,
        )

        counters = {
            'updated': 0,
            'skipped': 0,
            'empty_fp': 0,
            'errors': 0,
        }

        for i, row in enumerate(rows, start=1):
            # Inter-fetch sleep BEFORE the second row onwards — no point
            # waiting before the first one.
            if i > 1:
                time.sleep(_FETCH_SLEEP_SECONDS)

            verdict = backfill_one(conn, row, dry_run=args.dry_run)
            if verdict == 'updated':
                counters['updated'] += 1
            elif verdict == 'skipped':
                counters['skipped'] += 1
            elif verdict == 'empty-fp':
                counters['empty_fp'] += 1
            else:  # 'error'
                counters['errors'] += 1

            logger.debug(
                "[%d/%d] %s -> %s", i, total, row['link'], verdict,
            )
    finally:
        conn.close()

    duration = _format_duration(time.monotonic() - start)

    # Summary — print() (NOT logger.info) per task contract: this is the
    # operator-visible result line, not a progress log. Format matches
    # code-research §14.E.5.
    print('Backfill complete:')
    print(f"  Window:    {args.days} days ({total} rows scanned)")
    print(f"  Processed: {counters['updated']} (computed fingerprint)")
    print(f"  Skipped:   {counters['skipped']} (already had fingerprint)")
    print(f"  Empty fp:  {counters['empty_fp']} (no brands found / unreachable)")
    print(f"  Errors:    {counters['errors']}")
    print(f"  Duration:  {duration}")

    return 0


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
