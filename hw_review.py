#!/usr/bin/env python3
"""Operator-facing CLI for the manual-review workflow.

Six subcommands are implemented here (Tasks 7 & 8):

* ``list``    — show the pending queue as numbered rows; append a ``⚠️``
  footer summarising ``failed_articles`` whenever that table is non-empty
  (tech-spec Decision 8).
* ``show N``  — detail view of pending row ``N`` (1-based), including EN
  title / subtitle / paragraphs, RU fields with ``NULL``/``FILLED`` markers
  and the bookkeeping columns (``attempt_count``, ``fetched_at``,
  ``notified_at``, sanitised ``last_error``, ``telegraph_url``).
* ``stage N`` — accept ``ru_paragraphs`` / ``ru_blocks`` as JSON on stdin
  plus ``--ru-title`` / ``--ru-subtitle`` flags. The JSON payload passes
  through a hardened validator (tech-spec Decision 6): 256 KiB size cap,
  max depth 3, strict key allowlist, block-type allowlist, per-string 10
  KiB cap, and a cross-check that ``ru_blocks``/``pending.blocks`` parity
  holds (both present or both NULL).
* ``skip N``  — if the row has RU staged, prompt ``y/N``; anything other
  than ``y``/``Y`` cancels. On confirm, ``pending_articles_repo.skip_pending``
  writes the link into ``processed_news`` and deletes the pending row.
* ``preview N`` — lazily render the row's node tree (via
  ``telegraph_publisher.preview_nodes``) into a standalone HTML document
  (via ``preview_renderer.render_html``), write it inside
  ``~/.cache/hw-review/`` with ``mkstemp``-style unique naming, assert the
  file's parent equals ``CACHE_DIR.resolve()`` (path-guard, Decision 1),
  persist the path via ``pending_articles_repo.set_preview_path`` (so
  Task 8's ``publish`` / ``skip`` flow can clean up), and open it in the
  default browser unless ``--no-open`` is passed.

Exit convention: 0 on success, 1 on any handled error. The raw-exception
path (unknown bug) would bubble up with its usual traceback; we never
catch ``Exception`` at the top level. ``argparse`` handles usage errors
with its own exit-code 2.

The ``publish N`` subcommand (Task 8) is the final operator-driven step: it
composes ``telegraph_publisher.publish_article`` + ``mark_telegraph_published``
+ ``send_telegraph_teaser`` + ``move_to_published(via_review=True)`` with the
retry-idempotency contract from tech-spec Decision 9 — once ``createPage`` has
succeeded the resulting Telegraph URL is persisted on the pending row, so any
retry after a Telegram-send failure reuses that URL instead of creating a
second Telegraph page.

* ``take N`` (Task 9) — clear ``notified_at`` on a pending row so the
  idle-fallback grace window is reset and the operator can resume normal
  review. Refuses (exit 1, clean stderr) if the row has already left
  pending — the auto-publish path has already fired and Telegraph/channel
  state is no longer reversible from the CLI.

* ``retry N`` (Task 10) — re-queue a failed row back into pending. ``N``
  is a 1-based index into ``pending_articles_repo.list_failed()`` (ORDER
  BY ``failed_at DESC``, matching the ``list`` footer's rendering order
  so operator-visible indices line up). ``retry_from_failed`` resets
  ``attempt_count=0`` and stamps a fresh ``fetched_at`` per Decision 10.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

# news_bot has module-level env reads (``TELEGRAM_BOT_TOKEN`` etc.) and calls
# ``logging.basicConfig``; importing it configures the CLI's logging too. In
# tests these env-reads return ``None`` without side effects.
import news_bot
from news_bot import send_telegraph_teaser
import pending_articles_repo as repo
import preview_renderer
import telegraph_publisher
from telegraph_publisher import publish_article, TelegraphError


# Re-use the already-configured project logger instead of creating a fresh
# one (AC L75 / tech-spec).
logger = news_bot.logger


# Local HTML preview storage. Resolved once at import so path-guard
# comparisons always run against the canonical form, free of ``..`` or
# symlinks (Decision 1). ``mkdir`` is deferred to the first ``preview``
# invocation — creating the directory on every CLI startup would be
# gratuitous I/O.
CACHE_DIR = Path('~/.cache/hw-review/').expanduser().resolve()

# Hard size cap on ``stage`` stdin: 256 KiB + 1 — we read one byte past
# the cap so "exactly 256 KiB+1" triggers the rejection path (``len ==
# cap+1`` means more bytes were available and we refuse to proceed).
_STDIN_CAP = 256 * 1024
_STDIN_READ_LIMIT = _STDIN_CAP + 1

# Validator constants — tech-spec Decision 6.
_MAX_PARAGRAPHS = 100
_MAX_STRING_BYTES = 10 * 1024  # 10 KiB
_MAX_JSON_DEPTH = 3
_VALID_BLOCK_TYPES = frozenset({'paragraph', 'lead', 'heading', 'image', 'video'})
_BLOCK_KEYS_BY_TYPE = {
    'paragraph': frozenset({'type', 'text'}),
    'lead':      frozenset({'type', 'text'}),
    'heading':   frozenset({'type', 'text', 'level'}),
    'image':     frozenset({'type', 'src', 'caption'}),
    'video':     frozenset({'type', 'src', 'caption'}),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(msg: str) -> None:
    """Write an error line to stderr — single point so tests match exact text."""
    print(msg, file=sys.stderr)


def _out(msg: str = '') -> None:
    """Write a line to stdout."""
    print(msg)


def _resolve_pending(n: int) -> Optional[dict]:
    """Return the 1-based pending row or ``None`` + stderr + False if the
    index is out of range. Each subcommand re-reads the queue, so the
    index is only stable within one invocation (tech-spec §9.3)."""
    rows = repo.list_pending()
    if n < 1 or n > len(rows):
        _err('index out of range')
        return None
    return rows[n - 1]


def _age_hours(fetched_at) -> int:
    """Return the integer-hour age of a ``pending_articles.fetched_at``
    value. The column stores a SQLite ``CURRENT_TIMESTAMP`` string in UTC
    (``'YYYY-MM-DD HH:MM:SS'``); we compare against ``datetime.now(UTC)``
    for a timezone-aware delta."""
    if not fetched_at:
        return 0
    try:
        if isinstance(fetched_at, str):
            parsed = datetime.strptime(fetched_at, '%Y-%m-%d %H:%M:%S')
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = fetched_at
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - parsed
        return max(0, int(delta.total_seconds() // 3600))
    except (TypeError, ValueError):
        return 0


def _n_paragraphs(row: dict) -> int:
    """Paragraph count for the list-row display. If ``blocks`` is set we
    count paragraph/lead/heading blocks (the human-body), otherwise we
    fall back to ``paragraphs``."""
    if row.get('blocks'):
        return sum(1 for b in row['blocks']
                   if isinstance(b, dict) and b.get('type') in ('paragraph', 'lead', 'heading'))
    return len(row.get('paragraphs') or [])


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def cmd_list(_args: argparse.Namespace) -> int:
    rows = repo.list_pending()
    if not rows:
        _out('queue is empty')
    else:
        for i, row in enumerate(rows, start=1):
            emoji = news_bot.SOURCE_EMOJI.get(row['source_name'], '•')
            title = row.get('title') or '(no title)'
            _out(f"{i}. [{emoji}] {title} ({_n_paragraphs(row)}п, {_age_hours(row['fetched_at'])}h)")

    # Decision 8: always append the ⚠️ footer when failed is non-empty,
    # regardless of pending state (AC L71).
    failed = repo.list_failed()
    if failed:
        titles = ', '.join(f.get('title') or '(no title)' for f in failed)
        _out(
            f"⚠️ {len(failed)} неопубликованных в failed: [{titles}]. "
            f"hw_review retry N чтобы переподнять."
        )
    return 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def _truncate(text: str, n: int = 200) -> str:
    if text is None:
        return ''
    if len(text) <= n:
        return text
    return text[:n] + '…'


def _fmt_ru(label: str, value) -> str:
    """One line in the RU-fields block, marked FILLED or NULL."""
    if value is None:
        return f"  {label}: [NULL]"
    # ``ru_paragraphs=[]`` IS staged (Decision §9.13).
    return f"  {label}: [FILLED] {_truncate(repr(value), 200)}"


def cmd_show(args: argparse.Namespace) -> int:
    row = _resolve_pending(args.n)
    if row is None:
        return 1

    logger.debug('hw_review show %s -> %s', args.n, row['link'])

    _out(f"# {args.n}. {row['title']}")
    _out(f"source:  {row['source_name']}")
    _out(f"link:    {row['link']}")
    _out(f"subtitle: {row.get('subtitle') or ''}")
    _out('')
    _out(f"EN paragraphs ({len(row.get('paragraphs') or [])}):")
    for p in (row.get('paragraphs') or []):
        _out(f"  - {_truncate(p)}")
    _out('')
    _out(f"images:  {len(row.get('images') or [])}")
    _out(f"blocks:  {len(row.get('blocks') or []) if row.get('blocks') else 0}")
    _out('')
    _out('Russian fields:')
    _out(_fmt_ru('ru_title', row.get('ru_title')))
    _out(_fmt_ru('ru_subtitle', row.get('ru_subtitle')))
    _out(_fmt_ru('ru_paragraphs', row.get('ru_paragraphs')))
    _out(_fmt_ru('ru_blocks', row.get('ru_blocks')))
    _out('')
    _out('Bookkeeping:')
    _out(f"  fetched_at:    {row.get('fetched_at')}")
    _out(f"  notified_at:   {row.get('notified_at')}")
    _out(f"  attempt_count: {row.get('attempt_count')}")
    _out(f"  last_error:    {row.get('last_error') or ''}")
    _out(f"  telegraph_url: {row.get('telegraph_url') or ''}")
    return 0


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------

def _validate_depth(obj, depth: int = 0) -> None:
    """Raise ``ValueError('depth exceeded')`` if any nested container breaches
    ``_MAX_JSON_DEPTH``. Strings / numbers / bools / None are leaves."""
    if depth > _MAX_JSON_DEPTH:
        raise ValueError('depth exceeded')
    if isinstance(obj, dict):
        for v in obj.values():
            _validate_depth(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _validate_depth(v, depth + 1)


def _validate_string_field(value, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be str, got {type(value).__name__}")
    if len(value.encode('utf-8')) > _MAX_STRING_BYTES:
        raise ValueError(f"{label} exceeds {_MAX_STRING_BYTES} bytes")


def _validate_block(block: dict) -> None:
    if not isinstance(block, dict):
        raise ValueError('ru_blocks entries must be dicts')
    btype = block.get('type')
    if btype not in _VALID_BLOCK_TYPES:
        raise ValueError(f"unknown block type: {btype!r}")
    allowed = _BLOCK_KEYS_BY_TYPE[btype]
    extra = set(block.keys()) - allowed
    if extra:
        raise ValueError(f"unknown keys in {btype} block: {sorted(extra)}")

    if btype in ('paragraph', 'lead', 'heading'):
        _validate_string_field(block.get('text', ''), f'{btype}.text')
    if btype == 'heading':
        level = block.get('level', 3)
        if level not in (3, 4):
            raise ValueError('heading.level must be 3 or 4')
    if btype in ('image', 'video'):
        _validate_string_field(block.get('src', ''), f'{btype}.src')
        if 'caption' in block:
            _validate_string_field(block.get('caption', ''), f'{btype}.caption')


def _validate_stage_payload(parsed) -> None:
    """Decision 6 — strict validator. Raises ``ValueError`` with a concrete
    reason on any rejection; caller translates to stderr + exit 1."""
    if not isinstance(parsed, dict):
        raise ValueError('payload must be a JSON object')
    expected_keys = {'ru_paragraphs', 'ru_blocks'}
    actual_keys = set(parsed.keys())
    if actual_keys != expected_keys:
        extra = actual_keys - expected_keys
        missing = expected_keys - actual_keys
        parts = []
        if extra:
            parts.append(f'unknown keys {sorted(extra)}')
        if missing:
            parts.append(f'missing keys {sorted(missing)}')
        raise ValueError('; '.join(parts))

    _validate_depth(parsed)

    ru_paragraphs = parsed['ru_paragraphs']
    if not isinstance(ru_paragraphs, list):
        raise ValueError('ru_paragraphs must be a list')
    if len(ru_paragraphs) > _MAX_PARAGRAPHS:
        raise ValueError(f"ru_paragraphs exceeds {_MAX_PARAGRAPHS} items")
    for i, para in enumerate(ru_paragraphs):
        _validate_string_field(para, f'ru_paragraphs[{i}]')

    ru_blocks = parsed['ru_blocks']
    if ru_blocks is not None:
        if not isinstance(ru_blocks, list):
            raise ValueError('ru_blocks must be a list or null')
        for block in ru_blocks:
            _validate_block(block)


def cmd_stage(args: argparse.Namespace) -> int:
    # Step 1: slurp stdin up to the cap + 1 byte. If we got cap+1 bytes back,
    # the producer had more — reject without parsing.
    try:
        raw = sys.stdin.buffer.read(_STDIN_READ_LIMIT)
    except AttributeError:
        # Some test stand-ins wrap a plain BytesIO that exposes `.buffer`;
        # if not, read bytes via sys.stdin.read + encode as UTF-8.
        raw = sys.stdin.read(_STDIN_READ_LIMIT).encode('utf-8')
    if len(raw) >= _STDIN_READ_LIMIT:
        _err('stdin too large (>256 KiB)')
        return 1

    # Step 2: parse.
    try:
        parsed = json.loads(raw.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _err(f'invalid JSON: {exc}')
        return 1

    # Step 3: validator.
    try:
        _validate_stage_payload(parsed)
    except ValueError as exc:
        _err(f'staging rejected: {exc}')
        return 1

    # Step 4: resolve row.
    row = _resolve_pending(args.n)
    if row is None:
        return 1

    # Step 5: block-parity cross-check (Decision 6 tail).
    en_has_blocks = row.get('blocks') is not None
    ru_has_blocks = parsed['ru_blocks'] is not None
    if en_has_blocks and not ru_has_blocks:
        _err('staging rejected: ru_blocks required (EN has blocks)')
        return 1
    if ru_has_blocks and not en_has_blocks:
        _err('staging rejected: ru_blocks must be null (EN has no blocks)')
        return 1

    # Step 6: persist.
    ok = repo.update_staged(
        row['link'],
        args.ru_title,
        args.ru_subtitle,
        parsed['ru_paragraphs'],
        parsed['ru_blocks'],
    )
    if not ok:
        _err('row no longer pending')
        return 1

    logger.info('hw_review staged %s', row['link'])
    return 0


# ---------------------------------------------------------------------------
# skip
# ---------------------------------------------------------------------------

def cmd_skip(args: argparse.Namespace) -> int:
    row = _resolve_pending(args.n)
    if row is None:
        return 1

    has_ru = row.get('ru_paragraphs') is not None
    if has_ru:
        # ``ru_paragraphs IS NOT NULL`` is the staged-marker per §9.13 п.1.
        # Prompt must go to stderr so stdout stays scriptable. No trailing
        # newline — keep the cursor next to the `[y/N]:` so a TTY user can
        # type inline. ``flush`` guarantees the prompt appears even if the
        # stream is line-buffered.
        sys.stderr.write(
            f"В записи {args.n} сохранён русский. Точно скипнуть? [y/N]: "
        )
        sys.stderr.flush()
        try:
            answer = input()
        except EOFError:
            _out('skip cancelled')
            return 0
        if answer not in ('y', 'Y'):
            _out('skip cancelled')
            return 0

    repo.skip_pending(row['link'])
    logger.info('hw_review skipped %s', row['link'])
    return 0


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------

def _ensure_cache_dir() -> None:
    """Create ``CACHE_DIR`` with mode 0o700. ``mkdir`` ignores ``mode`` on
    a pre-existing directory, so we also chmod it defensively — security
    review will flag lax perms on a shared system otherwise."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        CACHE_DIR.chmod(0o700)
    except OSError as exc:
        logger.warning('could not chmod %s: %s', CACHE_DIR, exc)


def cmd_preview(args: argparse.Namespace) -> int:
    row = _resolve_pending(args.n)
    if row is None:
        return 1

    # Precondition: all three RU fields must be staged.
    if (row.get('ru_title') is None
            or row.get('ru_subtitle') is None
            or row.get('ru_paragraphs') is None):
        _err('nothing to preview — stage Russian text first')
        return 1

    _ensure_cache_dir()

    # Idempotent re-preview: drop the prior file if we recorded one (Risks
    # §). Best-effort — if it's gone we just move on.
    old_path = row.get('preview_html_path')
    if old_path:
        try:
            Path(old_path).unlink()
        except (FileNotFoundError, OSError) as exc:
            logger.debug('old preview unlink skipped (%s): %s', old_path, exc)

    # Build the Telegra.ph-shaped node tree (offline mirror of
    # publish_article) then render standalone HTML.
    nodes = telegraph_publisher.preview_nodes(
        title=row['ru_title'],
        paragraphs=row.get('ru_paragraphs') or [],
        images=row.get('images') or [],
        source_url=row.get('link'),
        subtitle=row.get('ru_subtitle') or '',
        blocks=row.get('ru_blocks'),
    )
    html = preview_renderer.render_html(nodes, row['ru_title'])

    tmp = tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        delete=False,
        dir=str(CACHE_DIR),
        prefix='hw-',
        suffix='.html',
    )
    try:
        tmp.write(html)
    finally:
        tmp.close()

    path = Path(tmp.name).resolve()

    # Path-guard: the resolved parent MUST match CACHE_DIR. Any escape → remove
    # the file, don't persist, don't open browser (Decision 1).
    if path.parent != CACHE_DIR:
        try:
            Path(tmp.name).unlink()
        except OSError:
            pass
        _err('preview path escaped cache dir')
        return 1

    repo.set_preview_path(row['link'], str(path))

    # Always print the path on stdout so CI / headless users get it even
    # when the browser launch is a no-op.
    _out(str(path))

    if not args.no_open:
        opened = webbrowser.open(f"file://{path}")
        if not opened:
            logger.warning('webbrowser.open returned False — headless? path: %s', path)

    return 0


# ---------------------------------------------------------------------------
# publish (Task 8)
# ---------------------------------------------------------------------------


def _cleanup_preview_html(preview_path: Optional[str]) -> None:
    """Delete the local HTML preview file (if any) after a successful publish.

    Named module-level helper — Task 9's ``_fallback_publish`` reuses this
    verbatim. Failures are logged but never raise: the preview file may have
    already been removed by a re-preview (Decision 1's "remove old → create
    new" pattern), and a missing file is not a publish-flow error.
    """
    if not preview_path:
        return
    try:
        os.unlink(preview_path)
    except FileNotFoundError:
        logger.debug('preview file already gone: %s', preview_path)
    except OSError as exc:
        logger.warning('could not delete preview file %s: %s',
                       preview_path, exc)


def cmd_publish(args: argparse.Namespace) -> int:
    # Step 1: resolve the 1-based index. Use _resolve_pending so out-of-range
    # emits the same ``index out of range`` message as the other subcommands.
    peek = _resolve_pending(args.n)
    if peek is None:
        return 1
    link = peek['link']

    # Step 2: re-read via ``get_pending`` to pick up the latest ``telegraph_url``
    # / ``preview_html_path`` state (list_pending snapshot may be stale if
    # another CLI invocation mutated the row in between). If the row vanished
    # between list and publish, surface the current terminal state cleanly.
    row = repo.get_pending(link)
    if row is None:
        if (pub := repo.get_published(link)) is not None:
            _err(f"{link} already published at {pub['telegraph_url']}")
        elif (fail := repo.get_failed(link)) is not None:
            _err(f"{link} in failed: {fail.get('last_error') or ''}")
        else:
            _err(f"{link} not found")
        return 1

    # Step 3: precondition — Russian body must be staged.
    if row.get('ru_paragraphs') is None:
        _err('nothing to publish — stage Russian text first')
        return 1

    # Step 4: Telegraph (idempotent per Decision 9).
    telegraph_url = row.get('telegraph_url')
    telegraph_path = row.get('telegraph_path')
    if telegraph_url:
        logger.info('reusing stored telegraph_url for %s: %s', link, telegraph_url)
    else:
        try:
            telegraph_url = publish_article(
                title=row['ru_title'],
                paragraphs=row.get('ru_paragraphs') or [],
                images=row.get('images') or [],
                source_url=row['link'],
                subtitle=row.get('ru_subtitle') or '',
                blocks=row.get('ru_blocks'),
            )
        except (TelegraphError, requests.RequestException) as exc:
            sanitised = news_bot.sanitize_error_message(exc)
            logger.error('telegraph publish failed for %s: %s', link, sanitised)
            _err(f'telegraph publish failed: {sanitised}')
            return 1

        # Derive the canonical path from the URL. Edge case: a malformed URL
        # may yield an empty string — we store it anyway so the non-NULL
        # column still signals "createPage succeeded" and retry logic skips
        # the second call.
        telegraph_path = urlparse(telegraph_url).path.lstrip('/')
        if not telegraph_path:
            logger.warning('telegraph URL yielded empty path: %r', telegraph_url)

        # Persist BEFORE the Telegram step so a teaser failure leaves the
        # row retry-idempotent (Decision 9).
        repo.mark_telegraph_published(link, telegraph_url, telegraph_path)

    # Step 5: Telegram teaser. On False-return or exception → keep the pending
    # row with its populated telegraph_url so a retry skips createPage.
    # Catch ``Exception`` (NOT ``BaseException``) so ``KeyboardInterrupt`` /
    # ``SystemExit`` continue to propagate — never swallow operator-initiated
    # abort. ``send_telegraph_teaser`` itself already catches ``TelegramError``
    # internally and returns False, but we still guard for ``TelegramError``
    # explicitly in case it ever bubbles (defence in depth).
    try:
        ok = send_telegraph_teaser(telegraph_url, row['link'])
    except Exception as exc:  # noqa: BLE001 — intentional broad catch
        sanitised = news_bot.sanitize_error_message(exc)
        logger.error('telegram teaser raised for %s: %s', link, sanitised)
        _err(
            f'telegram send failed. Telegraph URL saved — rerun publish '
            f'{args.n} to retry send.'
        )
        return 1
    if not ok:
        logger.error('telegram teaser returned False for %s', link)
        _err(
            f'telegram send failed. Telegraph URL saved — rerun publish '
            f'{args.n} to retry send.'
        )
        return 1

    # Step 6: atomic move — single transaction inside the repo.
    repo.move_to_published(link, telegraph_url, telegraph_path, via_review=True)

    # Step 7: clean up local preview file. Non-fatal on failure.
    _cleanup_preview_html(row.get('preview_html_path'))

    logger.info('Published %s via_review=True url=%s', link, telegraph_url)
    _out(f'Published: {telegraph_url}')
    return 0


# ---------------------------------------------------------------------------
# take (Task 9)
# ---------------------------------------------------------------------------


def cmd_take(args: argparse.Namespace) -> int:
    """Clear ``notified_at`` on pending row ``N`` so the operator intercepts
    an idle-fallback grace window before auto-publish fires.

    Exit codes:
      * 0 — notification cleared, row back in normal review cycle.
      * 1 — index out of range, row has left pending (already
        auto-published or moved to failed), or any repo error.

    User-spec anchor: AC L66–L67. Tech-spec: §Review+publish.
    """
    peek = _resolve_pending(args.n)
    if peek is None:
        return 1
    link = peek['link']

    # Re-read via get_pending to catch the race where the row was
    # auto-published between ``list_pending`` (inside _resolve_pending)
    # and this call. If vanished, report the terminal state cleanly
    # without mutating anything.
    row = repo.get_pending(link)
    if row is None:
        if (pub := repo.get_published(link)) is not None:
            _err(f"{args.n} already auto-published: {pub.get('telegraph_url') or ''}")
        elif (fail := repo.get_failed(link)) is not None:
            _err(
                f"{args.n} already auto-published (now in failed): "
                f"{fail.get('last_error') or ''}"
            )
        else:
            # Extremely narrow window: row deleted without landing in
            # either archive table. Still a "can't take" state for the
            # operator — exit 1 with a terse message.
            _err(f"{args.n} already auto-published")
        return 1

    # Row still pending. ``clear_notified`` is idempotent (writes NULL
    # regardless of prior value) — safe to call even if notified_at was
    # already NULL, so we don't need a pre-check branch. Write either
    # way: the message reads naturally in both cases.
    repo.clear_notified(link)
    title = row.get('title') or '(no title)'
    _out(f"notification cleared — row returned to normal review cycle: {title}")
    logger.info('hw_review take %s -> %s', args.n, link)
    return 0


# ---------------------------------------------------------------------------
# retry (Task 10)
# ---------------------------------------------------------------------------


def cmd_retry(args: argparse.Namespace) -> int:
    """Re-queue a failed row back into ``pending_articles``.

    ``N`` is a 1-based index into ``pending_articles_repo.list_failed()``
    — the same ORDER BY ``failed_at DESC`` projection rendered in the
    ``hw_review list`` footer (Decision 8), so operator indices match.

    Exit codes:
      * 0 — row restored, now visible via ``list``.
      * 1 — index out of range, or ``retry_from_failed`` returned False
        (defensive race with a concurrent writer — row already left
        failed or clashed with pending). No traceback either way.

    User-spec anchor: AC L72. Tech-spec: §Review+publish, Decision 10.
    """
    failed = repo.list_failed()
    if args.n < 1 or args.n > len(failed):
        _err(f"retry N out of range (failed queue has {len(failed)} items)")
        return 1

    target = failed[args.n - 1]
    link = target['link']
    title = target.get('title') or '(no title)'

    ok = repo.retry_from_failed(link)
    if not ok:
        # Defensive: repo returned False — row already moved back or a
        # pending clash. Tell the operator cleanly without a traceback.
        _err(f"retry skipped: {link} no longer in failed or already pending")
        return 1

    logger.info('hw_review retry %s -> %s', args.n, link)
    _out(f"Restored: {title}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='hw_review',
        description='Manual-review CLI for the Hot Wheels news pipeline.',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('list', help='show the pending queue')

    p_show = sub.add_parser('show', help='print detail of pending row N')
    p_show.add_argument('n', type=int, help='1-based pending index')

    p_stage = sub.add_parser('stage', help='stage Russian fields from stdin')
    p_stage.add_argument('n', type=int, help='1-based pending index')
    p_stage.add_argument('--ru-title', required=True, dest='ru_title',
                         help='Russian article title')
    p_stage.add_argument('--ru-subtitle', required=True, dest='ru_subtitle',
                         help='Russian subtitle / lead (may be empty string)')

    p_skip = sub.add_parser('skip', help='drop row N without publishing')
    p_skip.add_argument('n', type=int, help='1-based pending index')

    p_prev = sub.add_parser('preview',
                            help='render a local HTML preview for row N')
    p_prev.add_argument('n', type=int, help='1-based pending index')
    p_prev.add_argument('--no-open', action='store_true',
                        help='print path and exit; do not open browser')

    p_pub = sub.add_parser('publish',
                           help='publish staged row N to Telegraph + Telegram')
    p_pub.add_argument('n', type=int, help='1-based pending index')

    p_take = sub.add_parser(
        'take',
        help='clear notified_at on pending row N — intercept auto-publish'
    )
    p_take.add_argument('n', type=int, help='1-based pending index')

    p_retry = sub.add_parser(
        'retry',
        help='re-queue failed row N back into pending',
    )
    p_retry.add_argument('n', type=int, help='1-based failed-queue index')

    return parser


_DISPATCH = {
    'list':    cmd_list,
    'show':    cmd_show,
    'stage':   cmd_stage,
    'skip':    cmd_skip,
    'preview': cmd_preview,
    'publish': cmd_publish,
    'take':    cmd_take,
    'retry':   cmd_retry,
}


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH[args.command]
    return handler(args)


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
