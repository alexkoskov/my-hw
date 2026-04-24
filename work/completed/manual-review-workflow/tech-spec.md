---
created: 2026-04-22
status: approved
branch: dev
size: M
---

# Tech Spec: manual-review-workflow

## Solution

Split the existing `news_bot.job()` pipeline into a **prep phase** that stages articles into a new SQLite queue and a **review+publish phase** driven by a new CLI `hw_review.py`. The translation engine becomes the operator's interactive Claude Code session; the bot only stages, pings, and (for stale/overflow items) falls back to the existing `transcreate_text` helper.

Four tables live in `news.db`: the existing `processed_news` (dedup, semantic extension only), plus new `pending_articles` (WIP queue with JSON-serialized paragraphs/images/blocks), `published_articles` (audit of posts that reached the channel), `failed_articles` (dead letter after 3 GT attempts). A new `pending_articles_repo.py` module owns all DDL + CRUD — the first DAO in the repo. A new `preview_renderer.py` walks the same Telegraph node tree used by `telegraph_publisher` and produces a local HTML file the operator opens via `webbrowser.open`. Source fetching is reorganised into a `SOURCES` registry so adding a new source means appending to one list. The cron bumps from daily to hourly so the 2h grace window after idle-timeout has real meaning.

## Architecture

### What we're building/modifying

- **`pending_articles_repo.py`** (new) — DAO for the three new tables. Owns `init_schema`, all CRUD, transactional moves (pending→published, pending→failed, failed→pending, pending→skipped).
- **`preview_renderer.py`** (new) — pure function `render_html(nodes, title)` walking the Telegraph node tree into an HTML document for local viewing.
- **`hw_review.py`** (new) — stdlib `argparse` CLI with 8 subcommands: `list`, `show`, `stage`, `preview`, `publish`, `skip`, `take`, `retry`.
- **`news_bot.py`** (modified) — `job()` rewritten into prep + idle-fallback + overflow-fast-track passes; `process_new_articles` deleted; `init_db` delegates schema creation to the repo; new `SOURCES` registry + per-source fetcher helpers; `SOURCE_EMOJI`/`SOURCE_LABEL` + `build_admin_ping`; env-overridable constants `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `QUEUE_CAP`; cron bumped to hourly.
- **`telegraph_publisher.py`** (minor change) — expose a public `preview_nodes(...)` that returns the node tree used by `publish_article`, for `preview_renderer` to consume without re-implementing node building.
- **Source modules** (minor change) — `mattel_news_source.fetch_mattel_news` and any RSS-entry consumer gets a `source_name` stamp normalised from the URL's netloc (`autoevolution`/`mattel`/`lamley`).

### How it works

**Prep phase (cron, hourly):**
1. Idle-fallback pass in two steps. (a) Collect rows with `fetched_at` older than `IDLE_TIMEOUT_HOURS` AND `notified_at IS NULL`. Send ONE consolidated admin ping (Decision 12): `"Will auto-publish in ~{GRACE_WINDOW_HOURS}h: {title1}, {title2}, …. Intercept via hw_review take N"`. Stamp `notified_at` on each. (b) Collect rows with `notified_at` older than `GRACE_WINDOW_HOURS` AND `ru_paragraphs IS NULL`. For each, call the internal `_fallback_publish(row, via_review=False)` helper that runs `transcreate_text` on EN paragraphs → `publish_article` (reusing `telegraph_url` if already populated, per Decision 9) → `send_telegraph_teaser` → `move_to_published(via_review=False)`. On failure: `increment_attempt(link, sanitized_err)` (Decisions 11, 13); at 3 strikes → `move_to_failed`.
2. Fetch all sources via the `SOURCES` registry; each entry gets `source_name` by looking up `urlparse(link).netloc.lower()` in `NETLOC_TO_SOURCE` (Decision 4); unknown netlocs get `'other'` plus a warning log.
3. Filter against `processed_news` AND existing `pending_articles`.
4. Overflow fast-track: if `count_pending() + len(new) > QUEUE_CAP` (default 10). Compute `needed_slots = len(new) - (QUEUE_CAP - count_pending())`. Pull `list_pending_for_eviction()[:needed_slots]` (ru-NULL rows, oldest first — Decision 7 enforces NULL-only). Run `_fallback_publish` on each; same attempt-counter contract as step 1 (Decision 13). Any new entries that still don't fit after the pass are dropped for this tick and an admin ping is sent: `"Queue pressure: auto-published {evicted}, {deferred} new deferred, {staged_protected} staged rows protected"`.
5. For each accepted new entry: call `fetch_full_article(entry)`, then `insert_pending` (JSON-serialise paragraphs/images/blocks via `ensure_ascii=False`).
6. Compose admin ping via `build_admin_ping(rows)` — send only if queue non-empty.

**Review + publish (operator, interactive via Claude Code):**
1. Operator says "посмотрим очередь" → Claude runs `hw_review list`.
2. CLI prints the queue (numbered rows) plus, unconditionally, a `⚠️` footer listing entries in `failed_articles` and the retry hint.
3. Operator + Claude iterate on translation; Claude runs `hw_review stage N` feeding ru fields as JSON on stdin.
4. `hw_review preview N` calls `telegraph_publisher.preview_nodes(...)` → `preview_renderer.render_html(...)` → writes `/tmp/hw-review-{hash}.html` → `webbrowser.open`. `--no-open` flag prints the path only (used by tests and headless environments).
5. `hw_review publish N`: if `telegraph_url` is already set (retry path), skip `createPage`; otherwise call `publish_article` and `mark_telegraph_published`. Then `send_telegraph_teaser`. On both successes: `move_to_published(via_review=True)`, delete the local preview file.
6. `hw_review skip N`: if row has staged ru, interactive y/N prompt. On confirmation: `skip_pending` writes the link to `processed_news` and deletes the pending row — no `published_articles` write.
7. `hw_review take N`: `clear_notified(link)` — restores the row to normal review cycle. Refuses if row has already left pending.
8. `hw_review retry N`: N is an index into `list_failed()`. `retry_from_failed(link)` moves the row back into `pending_articles` with `attempt_count=0` and fresh `fetched_at`.

### Shared resources

| Resource | Owner (creates) | Consumers | Instance count |
|----------|----------------|-----------|----------------|
| `news.db` SQLite file | `news_bot.init_db()` | `news_bot`, `hw_review.py`, `pending_articles_repo` | 1 (file-based, per-call connections, no pool) |
| `TELEGRAPH_ACCESS_TOKEN` | `telegraph_publisher.ensure_access_token()` | `telegraph_publisher.publish_article`, `hw_review publish` | 1 (cached in `.env`) |
| Telegram `Bot` instances | per-call inside `send_admin_notification` and `send_telegraph_teaser` | same | N ephemeral (one per `asyncio.run`) — unchanged from today |

No long-lived connection pools, no ML models, no browser automation. The design stays process-per-invocation, matching the existing codebase pattern.

## Decisions

### Decision 1: Telegraph-draft preview dropped in favour of local HTML

**Decision:** `hw_review preview N` renders the Telegraph node tree into a local HTML file under `~/.cache/hw-review/` (mode `0700`) with filename `hw-{uuid4}.html`, created via `tempfile.NamedTemporaryFile(delete=False, dir=..., suffix='.html')`. `preview_renderer.render_html` applies three hardening layers: (a) tag allowlist inherited from `telegraph_publisher` (`p / figure / img / figcaption / iframe / h3 / h4 / hr / i / b / a`); (b) URL-scheme allowlist — `img src`, `iframe src`, `a href` must match `^(https?://)` otherwise the attribute is dropped (blocks `javascript:`/`data:` from a compromised upstream); (c) `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https:; frame-src https:; style-src 'unsafe-inline'">` in the rendered `<head>`. `webbrowser.open` receives the `pathlib.Path` resolved inside the cache dir; the CLI asserts `path.parent == CACHE_DIR.resolve()` before calling — any other path aborts with a safety error. No `createPage` during preview; the final `publish` is the only Telegraph API call per article.
**Rationale:** The structural checks the operator does in preview (paragraph order, image positions, subtitle framing, footer) are identical in both renderings. The user-spec explicitly chose local HTML after round 1 of user-spec validation. Removes ~40% of the feature's surface: no `edit_page` wrapper, no `preview_url`/`preview_path` churn during drafts, no `cleanup-drafts` subcommand, no Telegra.ph API-change risk. Cache-dir + uuid filename + path guard + URL-scheme allowlist + CSP close the TOCTOU/symlink/XSS surfaces flagged by the security review.
**User-spec anchor:** Supports "Preview — локальный HTML-файл" (Ограничения, user-spec).
**Alternatives considered:** Telegra.ph draft via `createPage`+`editPage` — rejected for surface cost; browser-headless diff against msg 35 — rejected as out-of-scope; predictable `/tmp/hw-review-{md5}.html` — rejected after security review (symlink-plant TOCTOU + argv-injection surface via stable path).

### Decision 2: Four-table data model (processed_news + pending + published + failed)

**Decision:** Extend the schema with three new tables: `pending_articles` (WIP queue), `published_articles` (audit of real publishes with `via_review` flag), `failed_articles` (terminal state after 3 GT attempts). `processed_news` stays schema-unchanged; its semantic broadens from "published" to "seen" — every link that was published OR skipped OR historically processed.
**Rationale:** The four concerns (dedup / queue / audit / dead-letter) have disjoint columns and query patterns. A single-table "status enum" approach would mix in-progress rows with audit rows and make overflow/idle queries heavier.
**User-spec anchor:** Supports "3 новые таблицы" and "Dedup-таблица содержит ссылку для любой статьи, которая была опубликована ИЛИ скипнута" (user-spec Технические решения + AC).
**Alternatives considered:** Single `articles` table with `status` column — rejected, muddles semantics; audit in external log file — rejected, loses SQL-based inspection.

### Decision 3: Preview created only at `hw_review preview N` time

**Decision:** Preview HTML is generated only when the operator explicitly runs `hw_review preview N`. Not at prep time, not at `stage`. If the operator goes straight from `stage` to `publish`, no preview file is ever created and `publish` skips any cleanup.
**Rationale:** Preview is an optional review step. Generating it eagerly wastes I/O and leaves more stale files to clean. The local-HTML cost is tiny either way, but lazy creation is simpler code and matches operator intent.
**User-spec anchor:** Supports "Preview — локальный HTML-файл... создаётся только при явном `hw_review preview N`" (user-spec Ограничения).
**Alternatives considered:** Generate preview HTML on every `stage` — rejected, not all staged rows get manually reviewed (fallback-published rows skip it entirely); generate at prep time for every new row — rejected, Russian text isn't available yet.

### Decision 4: Source registry with URL-netloc → `source_name` map

**Decision:** Introduce a `SOURCES` list of fetcher callables in `news_bot.py`. Each entry gets an explicit `source_name` set by an explicit `NETLOC_TO_SOURCE` map, not by bare netloc read:
```python
NETLOC_TO_SOURCE = {
    'www.autoevolution.com': 'autoevolution',
    'autoevolution.com':     'autoevolution',
    'lamleygroup.com':       'lamley',
    'www.lamleygroup.com':   'lamley',
    'corporate.mattel.com':  'mattel',
}
```
`_fetch_rss_entries` reads `urlparse(link).netloc.lower()` and looks it up; on miss, stamps `source_name='other'` and emits a warning log. `_fetch_mattel_entries` hardcodes `'mattel'`. `SOURCE_EMOJI` / `SOURCE_LABEL` dicts use the same three keys (`'autoevolution'` / `'mattel'` / `'lamley'`) — no `'rss'` key, no internal-vs-display split.
**Rationale:** Bare `urlparse` of `https://lamleygroup.com/...` yields `'lamleygroup.com'`, not `'lamley'`. An explicit map is the only correct way to tie RSS feeds to brand labels. Both the admin-ping counter (via `SOURCE_LABEL`) and the channel hashtag (via `_source_hashtag`, unchanged, which strips TLD) end up consistent. Adding a new source = one map entry + one fetcher + one `SOURCE_EMOJI`/`SOURCE_LABEL` pair.
**User-spec anchor:** Supports "Добавление нового источника — редактирование одного списка" (user-spec AC) and the exact admin-ping vocabulary "🟠 autoevolution ×K, 🟣 mattel ×M, 🟢 lamley ×L".
**Alternatives considered:** Per-source class hierarchy — rejected as overkill at 3 sources; bare `netloc` without map — rejected after skeptic flagged `lamleygroup.com` ≠ `lamley`; `'rss'` internal key with `'autoevolution'` display label — rejected because Lamley also arrives via RSS, so the breakdown would collapse the two outlets.

### Decision 5: Cron frequency bump from daily to hourly

**Decision:** `schedule.every().hour.do(job)` replaces `schedule.every().day.at("12:00").do(job)` in `news_bot.main`. `IDLE_TIMEOUT_HOURS` (default 48), `GRACE_WINDOW_HOURS` (default 2), and `QUEUE_CAP` (default 10) become env-overridable module constants.
**Rationale:** The user-spec's 2h grace window is meaningless at daily cadence — next tick is 24h away. Hourly ticks make `notified_at > 2h` enforceable. Cost is 72 fetches/day across three cheap sources (RSS feeds + 2 HTML pages), well below any rate limit. Admin-ping is suppressed on empty queue, so there's no notification spam.
**User-spec anchor:** Supports "Grace-окно ... дефолт 2 часа, значение параметризуется" and "Возможный bump до часовой гранулярности — осознанный 1-строчный follow-up" (user-spec Ограничения).
**Alternatives considered:** Keep daily, redefine grace as "next tick" — rejected, makes grace semantically 24h which the operator may not expect; run an out-of-band 2h-timer — rejected, adds process-management complexity.

### Decision 6: `stage N` reads ru-paragraphs/blocks as JSON on stdin with strict validation

**Decision:** `hw_review stage N` takes `ru_title` and `ru_subtitle` as `argparse` flags; `ru_paragraphs` + `ru_blocks` come from stdin as a JSON object. The payload passes through a validator before the repo sees it:
- Total stdin size ≤ 256 KiB (`sys.stdin.buffer.read(262145)`; if `len == 262145` → reject, stdin too large).
- `json.loads` happens inside a try/except that returns exit 1 with stderr `"invalid JSON: {err}"`.
- Parsed value must be a `dict` with exactly the allowed keys `{'ru_paragraphs', 'ru_blocks'}` — unknown keys rejected (prevents `__proto__`/extension drift).
- `ru_paragraphs` must be a `list[str]`, each element ≤ 10 KiB, list length ≤ 100.
- `ru_blocks` must be `None` OR a `list[dict]`. Each block dict: `type` in allowed set (`'paragraph'` / `'lead'` / `'heading'` / `'image'` / `'video'`), string fields (`text`, `src`, `caption`) are strings ≤ 10 KiB, `level` (headings) in `{3, 4}`. Unknown block types and unknown dict keys rejected.
- Maximum nesting depth 3; parser uses `json.loads(..., object_hook=_depth_check)` or equivalent.
- Cross-check: pending row's `blocks` non-NULL → `ru_blocks` required; pending row's `blocks` NULL → `ru_blocks` must be NULL.

A dedicated `hw_review_validators.py` (or function inside `hw_review.py`) owns this — unit-tested separately from the CLI dispatcher.

**Rationale:** Claude Code produces JSON naturally; shell-escaping Russian newlines for flags is fragile; a temp file adds cleanup burden. Strict validation closes the security review's critical finding on unvalidated JSON — malformed/malicious input is rejected before it reaches the repo or Telegraph renderer.
**User-spec anchor:** Supports `hw_review stage N` partial-staging rejection (AC L59) and `[TECHNICAL]` for the validation layer — user-spec leaves the CLI argument surface to tech-spec.
**Alternatives considered:** Repeated `--paragraph` flags — rejected, shell escaping + ordering brittle; YAML on stdin — rejected, adds a dependency; temp file path via flag — rejected, cleanup burden + extra TOCTOU surface; unvalidated `json.loads` — rejected after security review.

### Decision 7: Overflow fast-track never evicts rows with staged ru

**Decision:** `list_pending_for_eviction` returns only rows where `ru_paragraphs IS NULL`. When the queue is at cap and the operator has staged half of it, the bot drops new incoming entries rather than overwrite operator work. Admin ping surfaces both the evicted count and the protected count.
**Rationale:** This is the exact CLI-cron race the feature is built to close. An eviction that replaces staged translation with Google Translate output defeats the design.
**User-spec anchor:** Supports AC "Очередь никогда не превышает 10 pending-записей. При переполнении **только записи с пустым русским** прогоняются через GT-fast-track перед INSERT новых" (user-spec AC).
**Alternatives considered:** Evict staged rows and alert — rejected, destroys human work.

### Decision 8: Failed-articles backlog shown on every `list` invocation

**Decision:** `hw_review list` always appends a `⚠️` footer with count + titles + retry hint whenever `failed_articles` is non-empty, regardless of whether the main queue is empty or not.
**Rationale:** Failed rows are dead-letter state requiring operator intervention; hiding them is a silent backlog growth. The footer is cheap one-line rendering; its presence on `list` is the natural discovery path.
**User-spec anchor:** Supports AC "Если в таблице failed что-то накопилось — в любом случае показывает футер с количеством и списком" (user-spec Как должно работать + AC).
**Alternatives considered:** Show only when main queue is also non-empty — rejected, hides backlog exactly when operator might think "nothing to do"; separate `hw_review failed` command — rejected, discoverability falls off a cliff.

### Decision 9: Telegraph URL stored on the pending row for retry idempotency

**Decision:** `pending_articles` has `telegraph_url` and `telegraph_path` columns (NULL until first successful `createPage`). `hw_review publish N` calls `createPage` only when `telegraph_url` is NULL; on a Telegram-send failure, the stored URL is reused on subsequent retries and no duplicate Telegraph page is created.
**Rationale:** Partial success (Telegraph OK, Telegram fail) is a real failure mode at this latency. Without URL persistence, the second `publish` creates an orphan Telegraph page on every retry. `telegraph_path` is stored now (one extra column, near-zero cost) to keep a future `editPage` door open without another migration.
**User-spec anchor:** Supports AC "повторный `hw_review publish N` **переиспользует** существующую Telegraph-страницу (вторая не создаётся)" (user-spec AC).
**Alternatives considered:** Parse `path` from URL on retry — rejected because Telegraph slugs auto-suffix the publish date (so the slug of today and tomorrow differ); recreate Telegraph page on each retry and accept orphans — rejected, pollutes Telegraph account; introduce a separate `publish_state` table — rejected, adds coordination cost for one column pair.

### Decision 10: `process_new_articles` deleted; prep-phase does not publish

**Decision:** The existing `process_new_articles` function in `news_bot.py` (the fetch→translate→publish orchestrator, currently limit=3) is removed entirely. Its fetch work merges into the prep path; its publish work moves to `hw_review publish` + the idle-fallback + overflow-fast-track helpers. The `limit=3` concept is replaced by `QUEUE_CAP=10`.
**Rationale:** Keeping the function as legacy creates dead code + confused semantics ("is `process_new_articles` still called?"). Deleting it forces the test suite to honestly express the new shape.
**User-spec anchor:** Supports AC "Prep-фаза в `job()` больше не публикует в канал напрямую — ни одного вызова `send_telegraph_teaser` по ходу prep-цикла" (user-spec AC).
**Alternatives considered:** Keep `process_new_articles` as a behind-a-flag fallback — rejected, violates Decision 8 (dead-letter is explicit in `failed_articles`, not a silent code path); rename it to `_legacy_process_new_articles` and mark deprecated — rejected, tests would still need to be flipped and the name would linger in audit grep noise.

### Decision 11: Error-message sanitisation for `last_error` storage

**Decision:** Every write to `pending_articles.last_error` / `failed_articles.last_error` passes through a `sanitize_error_message(exc)` helper that strips known secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, `TELEGRAM_CHANNEL_ID`, `TELEGRAPH_ACCESS_TOKEN` are replaced with `[REDACTED]`. The helper reads env vars via `os.getenv` and **skips** any value that is empty, None, or whitespace-only (guards against `str.replace('', ...)` which would explode every character). Helper lives in `news_bot.py` next to `send_admin_notification`. Also applies before any admin-notification text is sent. `hw_review show N` prints the already-sanitised column verbatim.
**Rationale:** Security review flagged that `telegraph_publisher._api_call` POSTs `access_token` as form data, and `requests` can embed that token verbatim in network-error strings. Storing `str(exc)` unfiltered would leak the token into `news.db` and stdout (`hw_review show`) and admin chat. Sanitisation is a thin 10-line helper.
**User-spec anchor:** `[TECHNICAL]` — security hardening, not in user-spec.
**Alternatives considered:** Encrypt `last_error` column — rejected, overkill for single-file SQLite; drop `last_error` entirely — rejected, forensics value too high; redact only on display — rejected, DB still leaks if someone ships it by mistake.

### Decision 12: Batched idle-fallback heads-up ping

**Decision:** When the idle-fallback pass finds multiple stale rows in one cron tick, send ONE consolidated admin ping: `"Will auto-publish in ~{GRACE_WINDOW_HOURS}h: {title1}, {title2}, {title3}. Intercept via hw_review take N"`. Not one ping per row.
**Rationale:** User-spec phrases the alert in plural ("{titles}", comma-separated). A per-row loop would spam the operator with 5+ notifications in a stale-heavy tick. Consolidating matches the admin-ping pattern already used by `build_admin_ping` for the queue summary.
**User-spec anchor:** Supports user-spec Fallback-путь line "Will auto-publish in ~{grace}: [titles]. Intercept via `hw_review take N`" (plural list).
**Alternatives considered:** Per-row ping — rejected, noisy; only first row of N → rejected, operator may miss newer stale entries.

### Decision 13: Shared `attempt_count` column across idle-fallback and overflow paths

**Decision:** `pending_articles.attempt_count` is a single counter incremented by BOTH the idle-fallback GT failure path AND the overflow fast-track GT failure path. Three failures in any combination → `move_to_failed`. No per-path counter, no reset between paths.
**Rationale:** User-spec AC L70 explicitly says "3 failures in any combination of idle-fallback and overflow". A split counter would let a row fail twice in each path and survive. `pending_articles_repo.increment_attempt(link, err)` is the single mutator — both call sites use it.
**User-spec anchor:** Supports AC "Провалы в overflow-пути инкрементируют общий счётчик попыток по строке — после 3 провалов (в любом сочетании idle-fallback и overflow) строка уезжает в failed" (user-spec AC).
**Alternatives considered:** Separate `idle_attempts` and `overflow_attempts` columns — rejected, contradicts user-spec and doubles the 3-strike threshold in edge cases; reset on path change — rejected, makes a stuck row live forever.

### Decision 14: Channel-post hashtag continues to use `_source_hashtag(source_url)`, not `source_name`

**Decision:** `send_telegraph_teaser(telegraph_url, source_url)` stays unchanged — it still derives the channel-post hashtag via `_source_hashtag(source_url)` which strips TLD from the URL netloc. `source_name` is used only for admin-ping counting, not for the public channel hashtag. Result: `lamleygroup.com` → `#lamleygroup` (unchanged), `autoevolution.com` → `#autoevolution`, `corporate.mattel.com` → `#mattel`.
**Rationale:** Existing channel posts follow the TLD-stripped form (verified on `@myhwchannel123`); changing to `#lamley` would break the format operator is used to. `source_name` is internal; keeping the two concerns separate means adding a source only touches `NETLOC_TO_SOURCE` + fetchers, not the channel hashtag.
**User-spec anchor:** Preserves locked post format (`work/telegraph-pipeline/post-format.md`) per user-spec Ограничения.
**Alternatives considered:** Unify hashtag and source_name — rejected, requires rewriting historic channel URLs the operator reads today.

## Data Models

### New: `pending_articles` (WIP queue, hard-cap 10 rows)

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link` | TEXT | PRIMARY KEY | dedup; UNIQUE is a byproduct of PK |
| `source_name` | TEXT | NOT NULL | `'autoevolution'` / `'mattel'` / `'lamley'` |
| `feed_url` | TEXT | | original RSS URL or NULL |
| `title` | TEXT | NOT NULL | EN from fetcher |
| `subtitle` | TEXT | NOT NULL DEFAULT '' | empty if source has none |
| `paragraphs` | TEXT | NOT NULL | `json.dumps(list[str], ensure_ascii=False)` |
| `images` | TEXT | NOT NULL DEFAULT '[]' | JSON list of URLs |
| `blocks` | TEXT | | JSON list or NULL (autoevolution-only) |
| `ru_title` | TEXT | | NULL until staged |
| `ru_subtitle` | TEXT | | NULL until staged |
| `ru_paragraphs` | TEXT | | JSON list, NULL until staged |
| `ru_blocks` | TEXT | | JSON list, NULL until staged |
| `telegraph_url` | TEXT | | NULL until first `createPage` success |
| `telegraph_path` | TEXT | | path component of Telegraph URL |
| `fetched_at` | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP | drives idle + overflow ordering |
| `notified_at` | TIMESTAMP | | NULL until heads-up ping sent |
| `attempt_count` | INTEGER | NOT NULL DEFAULT 0 | fallback/overflow failures |
| `last_error` | TEXT | | last exception message |
| `pub_date` | TEXT | | preserved for `move_to_published` |
| `preview_html_path` | TEXT | | NULL until `hw_review preview` runs; absolute path inside `~/.cache/hw-review/`; cleared on `publish` / `skip` after file removal (Decision 1). |

### New: `published_articles` (audit)

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link` | TEXT | PRIMARY KEY | |
| `title` | TEXT | NOT NULL | |
| `ru_title` | TEXT | NOT NULL | |
| `telegraph_url` | TEXT | NOT NULL | |
| `telegraph_path` | TEXT | | |
| `source_name` | TEXT | NOT NULL | |
| `published_at` | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP | |
| `via_review` | INTEGER | NOT NULL | 1 operator-approved, 0 auto |

### New: `failed_articles` (dead letter)

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link` | TEXT | PRIMARY KEY | |
| `title` | TEXT | NOT NULL | for `list` footer |
| `source_name` | TEXT | NOT NULL | |
| `paragraphs` | TEXT | NOT NULL | EN preserved for retry |
| `images` | TEXT | NOT NULL DEFAULT '[]' | |
| `blocks` | TEXT | | |
| `subtitle` | TEXT | NOT NULL DEFAULT '' | |
| `pub_date` | TEXT | | |
| `feed_url` | TEXT | | |
| `last_error` | TEXT | | |
| `failed_at` | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP | |
| `original_fetched_at` | TIMESTAMP | | forensic |

### Existing: `processed_news` (unchanged schema)

Semantic extension only: every link that was published OR skipped OR historically processed. No DDL change.

### Repo module interface

`pending_articles_repo.py` exposes (all functions accept plain-dict parameters, return plain dicts — no dataclasses):

```
init_schema(conn) -> None
insert_pending(entry: dict) -> bool   # False on UNIQUE conflict
get_pending(link) -> dict | None
list_pending() -> list[dict]          # ORDER BY fetched_at ASC
list_pending_stale(hours) -> list[dict]
list_notified_overdue(grace_hours) -> list[dict]
list_pending_for_eviction() -> list[dict]
update_staged(link, ru_title, ru_subtitle, ru_paragraphs, ru_blocks) -> bool
mark_notified(link) -> None
clear_notified(link) -> None
increment_attempt(link, error) -> int
mark_telegraph_published(link, telegraph_url, telegraph_path) -> None
set_preview_path(link, path) -> None   # stores ~/.cache/hw-review/…html path on the row
move_to_published(link, telegraph_url, telegraph_path, via_review) -> None
move_to_failed(link, last_error) -> None
skip_pending(link) -> None
retry_from_failed(link) -> bool
list_failed() -> list[dict]
count_pending() -> int
get_published(link) -> dict | None
get_failed(link) -> dict | None
```

JSON fields are serialised inside the repo; callers pass and receive Python lists. Multi-statement operations (`move_to_*`, `skip_pending`, `retry_from_failed`) wrap in a single connection with explicit `commit()`/`rollback()` boundaries.

## Dependencies

### New packages

None. The feature uses only stdlib (`argparse`, `sqlite3`, `json`, `webbrowser`, `html.escape`, `tempfile`, `uuid`, `pathlib`, `os`).

### Using existing (from project)

- `telegraph_publisher` — reuse `publish_article`; add public `preview_nodes(...)` wrapper.
- `news_bot.transcreate_text` — unchanged, called from the idle-fallback + overflow-fast-track paths.
- `news_bot.send_admin_notification`, `send_telegraph_teaser`, `fetch_rss`, `fetch_full_article`, `load_feeds`, `filter_new_entries`, `init_db`, `is_processed`, `mark_processed` — all reused; some modified to accommodate source registry.
- `mattel_news_source.fetch_mattel_news`, `autoevolution_source.*`, `lamley_source.*` — called by new `SOURCES` fetcher helpers; small change to normalise `source_name`.
- `schedule` — unchanged; cadence bumped to `.hour`.
- `python-telegram-bot`, `deep-translator`, `feedparser`, `requests`, `curl_cffi`, `beautifulsoup4` — all unchanged.

## Testing Strategy

**Feature size:** M

### Unit tests

- `pending_articles_repo`: insert serialises JSON; insert on duplicate returns False; get deserialises; list orderings; stale/overdue/eviction filters; transactional moves (`move_to_published` + `processed_news` insert + pending delete happens atomically); `skip_pending` writes processed_news; `retry_from_failed` resets `attempt_count`; `update_staged` rejects rows that already left pending.
- `preview_renderer`: renders all allowed tags; escapes Cyrillic and HTML special chars; void tags emit self-closing; unknown tags silently dropped. URL-scheme filter test: `img src`/`iframe src`/`a href` with `javascript:` or `data:` → attribute dropped in rendered output; `https://` and `http://` → preserved. CSP meta-tag assertion: rendered `<head>` contains `default-src 'none'`, `img-src https:`, `frame-src https:`.
- `sanitize_error_message`: each of the four env secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, `TELEGRAM_CHANNEL_ID`, `TELEGRAPH_ACCESS_TOKEN`) individually replaced with `[REDACTED]` when present in the exception string; empty/unset env vars are skipped (no `str.replace('', ...)` pathology); compound exception with all four secrets yields all four replaced.
- `stage` JSON validator (Decision 6) — eight rejection vectors, each a separate test case: stdin >256 KiB, JSON depth >3, unknown top-level key, non-list `ru_paragraphs`, non-dict block entry, unknown block `type`, unknown block field key, string field >10 KiB. Plus block-parity: pending row's `blocks` non-NULL with `ru_blocks=None` → reject; pending row's `blocks` NULL with `ru_blocks` provided → reject.
- `hw_review preview` path guard: unit test patches `hw_review`'s resolve step to return a path outside `CACHE_DIR` → CLI aborts with stderr `"preview path escaped cache dir"` + exit 1 + no `webbrowser.open` call.
- `build_admin_ping`: empty → None; sources with zero count omitted; formatting string matches spec exactly (byte-compared).
- `hw_review.py` argparse: each subcommand accepts expected args; exit codes 0/1; `stage` rejects partial ru or invalid JSON; `skip` with staged ru requires confirmation; `publish` on vanished link emits clean error.
- `SOURCES` registry: RSS entries get `source_name` from URL netloc; Mattel entries get `'mattel'`; lamley RSS → `'lamley'`; autoevolution RSS → `'autoevolution'`.
- Overflow fast-track algorithm: staged rows never evicted; fast-track failure increments `attempt_count`; third failure moves to `failed_articles`.

### Integration tests

- Prep phase end-to-end with all source fetchers mocked: every source's new entries land in `pending_articles` with per-source fields populated — autoevolution row has non-NULL `blocks`, Mattel/Lamley rows have NULL `blocks`, all rows have NULL `ru_*`. Zero Telegraph/Telegram calls.
- Publish phase end-to-end with `_api_call` and `send_telegraph_teaser` and `transcreate_text` mocked — `preview_renderer` and `preview_nodes` run **real**. Stage → preview → publish; row moves pending → published; local HTML file deleted; `via_review=True`.
- Publish-retry, two variants: (a) `send_telegraph_teaser` returns False; (b) `send_telegraph_teaser` raises. Both: first publish writes `telegraph_url` on pending row; second publish does **not** call `_api_call('createPage', …)` again (strict mock assertion: `create_call_count == 1` across both runs).
- Idle-fallback with time simulation: set `fetched_at = datetime('now', '-50 hours')` via SQL update (not Python time-mock — `CURRENT_TIMESTAMP` defaults are SQLite-side). Verify consolidated admin ping lists all stale titles in one message. Overdue path → GT + auto-publish with `via_review=False`. `take N` in between clears `notified_at`, rows skip the overdue step.
- Overflow: queue pre-loaded to `QUEUE_CAP` with mix of staged + unstaged; prep with new entries → unstaged evicted via GT, staged survive untouched, deferred count and staged-protected count appear in admin ping.
- Attempt counter mixed-path: one failure via idle-fallback + one failure via overflow → `attempt_count=2`; third failure of any kind → `move_to_failed` (Decision 13). Assert single shared column.
- Migration via `PRAGMA table_info`: for every expected column, assert name + type + NOT-NULL + default + PK. Dict-literal comparison, no substring matches.
- Transactional rollback: force `move_to_published` to raise mid-transaction (monkeypatch cursor to raise on the second INSERT); assert no partial state — `pending_articles` row still present, `processed_news` and `published_articles` empty of the link.
- `take N` after auto-publish elapsed → CLI returns exit 1 with the expected stderr message (AC L67).
- Skip with staged ru → prompt appears. Parametrized inputs: `y` / `Y` write to `processed_news` and delete pending; `n` / `N` / empty string / any other input → abort with exit 0 + stderr `"skip cancelled"` + no DB changes.
- `publish N` on vanished row — three explicit states tested: (a) row in `published_articles` → stderr cites Telegraph URL; (b) row in `failed_articles` → stderr cites `last_error`; (c) row not found anywhere → stderr `"not found"`. All three exit 1 with no traceback.
- Flipped existing integration tests: `test_integration.py:54,115,146,178,205`, `test_mattel_integration.py:59,104`, `test_feed_iteration.py:35` — each asserts `mock_publish.assert_not_called()` and `pending_repo.count_pending() == expected`.
- `init_db()` idempotent: call twice on the same DB, second call must not raise and must not change row counts.
- Unknown netloc in RSS: an entry from an unknown domain → `source_name='other'` with a WARNING log record captured.
- CLI logger integration: `hw_review show N` emits through the same logger as `news_bot`; capturing the logger stream shows formatted output.

### E2E tests

None. Full stack verification happens manually via Telegram MCP + browser review of the channel post after a real `hw_review publish` run — the Pre-deploy QA task covers the acceptance criteria checklist.

## Agent Verification Plan

**Source:** `user-spec.md` "Как проверить" table.

### Verification approach

Per-task `Verify-smoke` fields in Implementation Tasks specify executable checks for the agent during implementation (`pytest` commands, SQL probes, CLI runs on a tempfile DB, `curl` for Telegraph URLs). The Pre-deploy QA task (Final Wave) runs the full test suite and walks the acceptance criteria from both user-spec and this tech-spec. The Post-deploy verification task uses Telegram MCP to read the admin chat and channel for format conformance and spot-checks one published Telegraph page.

### Tools required

- `bash` — running `pytest`, SQL one-liners (`sqlite3 news.db "..."`), CLI command invocations.
- `curl` — fetching Telegraph URLs to verify content shape.
- `Telegram MCP` — reading the admin chat (format of `"N ждут review: …"` ping), reading the channel (hashtag + preview card + Instant View presence on a published post).
- `python -c` — running one-off probes against `pending_articles_repo` on a test DB.
- `webbrowser` — manual preview check by the user.

No Playwright or headless browser required for agent verification; the user-side browser check of the local HTML preview is covered in the user-spec "Пользователь проверяет" section.

## Risks

| Risk | Mitigation |
|------|-----------|
| Refactoring `job()` breaks the 8 existing auto-publish integration tests | Flip those tests in the same wave as the refactor; run full `pytest tests/` before committing that wave. |
| Hourly cron multiplies source HTTP traffic 24× | All three sources are cheap; admin-ping suppressed on empty queue, so no operator spam. If autoevolution rate-limits, add `--backoff` env flag as follow-up (not in scope). |
| CLI-cron race: cron eviction touches operator-edited row | Eviction query filters `ru_paragraphs IS NULL`; unit test `test_overflow_protects_staged` enforces. |
| SQLite concurrent INSERT under hourly cron + overlapping prep | `pending_articles.link` is PRIMARY KEY → duplicate INSERT raises `IntegrityError` which is caught in `insert_pending` and returned as `False`. No WAL mode change needed. |
| `webbrowser.open` fails on headless Docker | `hw_review preview --no-open` flag always prints the HTML path; CLI exits 0 even if the launch returned False. |
| Telegraph URL orphaned if publish completes createPage but the pending row is deleted (e.g. concurrent skip from another session) | Only one Claude Code session at a time by convention; if the concern materialises, add a guard: `cmd_publish` re-fetches the pending row after `createPage` and moves it atomically. Not in scope for v1. |
| JSON serialisation of paragraphs with embedded quotes/newlines | `json.dumps(..., ensure_ascii=False)` handles all Unicode + escaping by spec. `json.loads` on read; unit-tested with Cyrillic fixtures. |
| Operator leaves preview HTML files accumulating in `~/.cache/hw-review/` | `publish` and `skip` delete the file tracked by `preview_html_path` column. Repeat preview on the same row creates a new file and clears the previous one (CLI re-runs "remove old → create new → update column"). Abandoned rows (never published, never skipped, re-previewed many times) leak files only until next `publish`/`skip`; startup cleanup hook removes any files in the cache dir whose paths aren't referenced from `pending_articles.preview_html_path` as a belt-and-suspenders measure. |
| `fetch_full_article` network failure drops the entry for this tick | Existing behaviour — entry is not marked in `processed_news` or `pending_articles`, so next cron tick retries. Matches `news_bot.py:372-374` current skip rule. |
| Upstream-compromised image URL from a source fetcher triggers XSS when operator opens local HTML preview (`file://` origin) | `preview_renderer` URL-scheme allowlist (`https?://` only) + CSP meta (`default-src 'none'`, `img-src https:`, `frame-src https:`) block `javascript:` and `data:` URLs (Decision 1). |
| Malformed / malicious JSON on `stage` stdin corrupts pending row or reaches Telegraph | Strict validation layer (Decision 6): size cap 256 KiB, depth cap 3, key + type allowlists. Rejects before DB write. |
| Telegraph token leaks into `last_error` via `requests`/`_api_call` network-error string | `sanitize_error_message` replaces all four env-secret values with `[REDACTED]` at the call site (Decision 11). |
| Symlink attack on predictable `/tmp/hw-review-*.html` filename | Preview files go under `~/.cache/hw-review/` at `0700` with `tempfile.NamedTemporaryFile`-issued `uuid4` name; `webbrowser.open` path is asserted to resolve inside the cache dir (Decision 1). |

## User-Spec Deviations

- **Hourly cron instead of "default daily with 1-line bump"**: user-spec says "Cron продолжает бежать минимум раз в сутки... Возможный bump до часовой гранулярности — осознанный 1-строчный follow-up, решение отложено до tech-spec". Tech-spec chooses the bump now because the 2h grace window is meaningless at daily cadence. All other timing parameters (48h idle, 2h grace) stay as spec'd. → [PENDING USER APPROVAL]
- **`source_name` vocabulary promoted to `autoevolution`/`mattel`/`lamley`** as the internal key (code-research §9.4 had `rss` as the key with `autoevolution` only as a display label). Reason: lamley articles arrive via RSS alongside autoevolution, so keeping both under a single `rss` key would collapse the two outlets in the admin ping. Decision 4 pins the mapping via an explicit `NETLOC_TO_SOURCE` dict — bare netloc inference would yield `lamleygroup`, not `lamley`. Channel-post hashtag keeps the TLD-stripped form via `_source_hashtag` (Decision 14) — unchanged user-visible output. → [PENDING USER APPROVAL]
- **Added public `telegraph_publisher.preview_nodes(...)`** helper (not in user-spec). Reason: `preview_renderer` needs the same node tree `publish_article` would generate. Adding a public wrapper around `_build_content*` keeps node-building logic inside the publisher module and preserves symmetry. → [PENDING USER APPROVAL]
- **Added `telegraph_path` column on pending & published tables** (user-spec mentions only `telegraph_url`). Reason: `editPage` requires `path`; parsing it from URL is brittle because slugs auto-suffix dates. Storing it now (one extra cheap column) keeps the editPage door open without a future migration. → [PENDING USER APPROVAL]

## Acceptance Criteria

Technical criteria additional to user-spec acceptance criteria:

- [ ] `init_db()` is idempotent: calling it twice on a non-empty DB is a no-op.
- [ ] `insert_pending` UNIQUE-conflict returns `False` without raising outside the repo.
- [ ] All repo functions that mutate multiple tables run in a single SQLite transaction with `commit()` / `rollback()` boundaries.
- [ ] `hw_review preview --no-open` exits 0 and prints the HTML file path to stdout; no call to `webbrowser.open`.
- [ ] `hw_review publish N` where N is out of `list_pending()` range returns exit 1 with a stderr message referencing current state (`published_articles` hit, `failed_articles` hit, or not-found) — no Python traceback.
- [ ] `_fetch_rss_entries` attaches `source_name` by URL netloc mapping; unknown netlocs default to `'other'` with a warning log.
- [ ] `build_admin_ping([])` returns `None`; non-None results match the documented format byte-for-byte.
- [ ] All existing test cases in `tests/` pass unchanged except those that explicitly assert auto-publish — those are rewritten in the same wave as the `job()` refactor.
- [ ] New migration test runs `init_db()` on an empty tempfile DB and asserts via `PRAGMA table_info` that all expected columns exist with expected types.
- [ ] No new package appears in `requirements.txt` under this feature.
- [ ] All repo SQL uses parameterised queries (`?` placeholders) — no string concatenation or f-string interpolation into SQL bodies. Code Audit task explicitly grep-verifies this.
- [ ] `last_error` is written only through `sanitize_error_message` — a unit test injects an exception containing each of `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_ID`, `TELEGRAM_CHANNEL_ID`, `TELEGRAPH_ACCESS_TOKEN` and asserts `[REDACTED]` replaces each in the resulting `last_error` column, in any admin-ping, and in `hw_review show N` stdout.
- [ ] `preview_renderer.render_html` output passes three invariants: tag allowlist (no unknown tags emitted), URL-scheme allowlist (`img src`/`iframe src`/`a href` only `https?://`), CSP meta tag present with `default-src 'none'` + allowed `img-src`/`frame-src`.
- [ ] `hw_review preview` never calls `webbrowser.open` with a path outside `~/.cache/hw-review/`; CLI-level assert + unit test verify.
- [ ] `stage` validator rejects: stdin >256 KiB, depth >3, unknown keys at top level, non-list `ru_paragraphs`, non-dict block entries, unknown block `type`, unknown block keys, string fields >10 KiB each.
- [ ] Attempt counter is shared across idle-fallback and overflow failures per Decision 13 — integration test above exercises the mixed-path path.
- [ ] Idle-fallback heads-up ping is sent once per cron tick per Decision 12 — integration test asserts a single admin-notification call even with 3 stale rows.

## Implementation Tasks

### Wave 1 (foundations — independent)

#### Task 1: `pending_articles_repo` module

- **Description:** New DAO module at repo root implementing `init_schema` + all CRUD + transactional moves for the three new tables (`pending_articles`, `published_articles`, `failed_articles`). JSON serialisation handled inside the repo; callers see Python lists. Used by `news_bot.init_db`, `news_bot.job`, and `hw_review.py`.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "import pending_articles_repo; pending_articles_repo.init_schema(__import__('sqlite3').connect(':memory:'))"` returns 0.
- **Files to modify:** `pending_articles_repo.py` (new), `tests/test_pending_articles_repo.py` (new)
- **Files to read:** `news_bot.py`, `work/manual-review-workflow/code-research.md` (§9.1, §9.2)

#### Task 2: `preview_renderer` module

- **Description:** New pure-function module `render_html(nodes, title) -> str` that walks the Telegraph node tree produced by `telegraph_publisher` and emits standalone HTML for local browser viewing. Escapes Cyrillic and HTML special chars; unknown tags silently dropped; void tags self-close.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "import preview_renderer; print(preview_renderer.render_html([{'tag':'p','children':['test']}], 'T')[:60])"` returns opening HTML.
- **Files to modify:** `preview_renderer.py` (new), `tests/test_preview_renderer.py` (new)
- **Files to read:** `telegraph_publisher.py`, `work/telegraph-pipeline/post-format.md`, `work/manual-review-workflow/code-research.md` (§9.6)

#### Task 3: Admin-ping helper + source vocabulary + `sanitize_error_message`

- **Description:** Add `SOURCE_EMOJI` / `SOURCE_LABEL` dicts (keyed by `autoevolution`/`mattel`/`lamley` per Decision 4), `build_admin_ping(rows)`, and `sanitize_error_message(exc)` helper (Decision 11) to `news_bot.py`. Ping returns `None` on empty queue; format matches user-spec byte-for-byte with zero-count sources omitted. Sanitiser strips all four env-secrets.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "import os; os.environ['TELEGRAM_BOT_TOKEN']='abc'; from news_bot import sanitize_error_message; print(sanitize_error_message(Exception('api abc xyz')))"` prints string without `abc`.
- **Files to modify:** `news_bot.py`, `tests/test_admin_ping.py` (new)
- **Files to read:** `work/manual-review-workflow/user-spec.md`, `work/manual-review-workflow/code-research.md` (§9.4)

#### Task 4: Public `preview_nodes` wrapper in `telegraph_publisher`

- **Description:** Expose a public helper in `telegraph_publisher.py` that returns the node tree the current `publish_article` would send — without calling `createPage`. `preview_renderer` uses it. Existing node-building functions (`_build_content`, `_build_content_from_blocks`) stay private; only the wrapper is public.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python -c "import telegraph_publisher; print(telegraph_publisher.preview_nodes(title='t', paragraphs=['p'])[0]['tag'])"` prints a valid tag like `p` or `figure`.
- **Files to modify:** `telegraph_publisher.py`, `tests/test_telegraph_publisher.py`
- **Files to read:** `telegraph_publisher.py`, `work/manual-review-workflow/code-research.md` (§9.6)

### Wave 2 (source registry — depends on Wave 1; Task 5 alone to serialize `news_bot.py` edits)

#### Task 5: Source registry + `source_name` tagging

- **Description:** Introduce `SOURCES` list in `news_bot.py` with named fetcher helpers (`_fetch_rss_entries`, `_fetch_mattel_entries`). RSS entries get `source_name` derived from URL netloc (`autoevolution`/`lamley`/fallback `other`). Mattel entries are tagged `'mattel'`. All entries are normalised to plain dicts (not `FeedParserDict`).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "from news_bot import SOURCES, _fetch_rss_entries; print([f.__name__ for f in SOURCES])"` includes `_fetch_rss_entries`, `_fetch_mattel_entries`.
- **Files to modify:** `news_bot.py`, `tests/test_sources_registry.py` (new)
- **Files to read:** `news_bot.py`, `mattel_news_source.py`, `autoevolution_source.py`, `lamley_source.py`, `feeds.json`, `work/manual-review-workflow/code-research.md` (§9.5)

### Wave 3 (prep-phase refactor — depends on Wave 2)

#### Task 6: Refactor `job()` into prep-only + cron bump + delete `process_new_articles`

- **Description:** Replace the current end-to-end `job()` with the prep-only pipeline per Decision 10. Delete `process_new_articles`, bump cron per Decision 5, add env-overridable constants, delegate schema creation to the repo, and flip existing auto-publish integration tests to staging-only.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_integration.py tests/test_mattel_integration.py tests/test_feed_iteration.py tests/test_database.py -q` is green.
- **Files to modify:** `news_bot.py`, `tests/test_integration.py`, `tests/test_mattel_integration.py`, `tests/test_feed_iteration.py`, `tests/test_job_prep_phase.py` (new), `tests/test_migration.py` (new)
- **Files to read:** `news_bot.py`, files from Wave 1, `work/manual-review-workflow/code-research.md` (§9.10, §9.11, §9.12)

### Wave 4 (CLI skeleton — depends on Waves 1-3; Task 7 creates `hw_review.py`)

#### Task 7: `hw_review` CLI with `list` / `show` / `stage` / `skip` / `preview`

- **Description:** Build the `hw_review.py` CLI surface for five subcommands driving the review flow, per Decisions 1, 3, 6 and 8 (local HTML preview, lazy draft, strict stdin-JSON validation for stage, mandatory skip-confirmation when ru is staged, failed-footer always visible).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python hw_review.py list` on an empty test DB prints the empty-queue marker; `python hw_review.py preview --no-open 1` on a staged row prints the HTML file path to stdout with exit 0.
- **Verify-user:** Run `python hw_review.py preview <N>` on a staged row — browser opens the preview with hero image, subtitle, paragraphs, and source footer in the expected order.
- **Files to modify:** `hw_review.py` (new), `tests/test_hw_review_cli.py` (new)
- **Files to read:** `pending_articles_repo.py`, `preview_renderer.py`, `telegraph_publisher.py`, `work/manual-review-workflow/code-research.md` (§9.3, §9.6, §9.13)

### Wave 5 (publish command — depends on Wave 4; Task 8 extends `hw_review.py`)

#### Task 8: `hw_review publish` with Telegraph-URL reuse

- **Description:** Implement `hw_review publish N`. Flow: `get_pending` → precondition check (staged) → if `telegraph_url` NULL, call `publish_article` and `mark_telegraph_published`; else skip. Then `send_telegraph_teaser`. On success: `move_to_published(via_review=True)` + delete local preview file. On partial failure (Telegraph OK, Telegram fail): pending row retained with `telegraph_url` populated; clean stderr message tells operator to retry. On publish-on-vanished-row: clean error with current state, exit 1.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** End-to-end test mock assertion: second `publish` after Telegram-send failure calls `publish_article` exactly once across both runs.
- **Verify-user:** After a real publish, the channel shows the post with the correct hashtag and Instant View preview card.
- **Files to modify:** `hw_review.py`, `tests/test_hw_review_publish_flow.py` (new)
- **Files to read:** `telegraph_publisher.py`, `news_bot.py` (for `send_telegraph_teaser`), `pending_articles_repo.py`, `work/manual-review-workflow/code-research.md` (§9.9)

### Wave 6 (idle-fallback — depends on Waves 1-5)

#### Task 9: Idle-fallback pass + `hw_review take` command

- **Description:** Add idle-fallback pass at the top of `job()` with batched heads-up (Decision 12) and shared attempt counter (Decision 13); add `hw_review take N` clearing `notified_at` and refusing if the row has already left pending.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_idle_fallback.py -q` green after stubbing `transcreate_text` + `publish_article` + `send_telegraph_teaser`; also assert with a hand-staged row that a single admin-ping message contains all three stale titles.
- **Files to modify:** `news_bot.py`, `hw_review.py`, `tests/test_idle_fallback.py` (new), `tests/test_hw_review_take.py` (new)
- **Files to read:** `news_bot.py`, `pending_articles_repo.py`, `work/manual-review-workflow/code-research.md` (§9.7, §9.10)

### Wave 7 (overflow + retry — depends on Wave 6)

<!-- Split from idle-fallback wave after template validator flagged
     file-conflicts on news_bot.py, hw_review.py. Task 10 also depends on
     the _fallback_publish helper produced by Task 9. Test file split into
     test_hw_review_take.py (Task 9) and test_hw_review_retry.py (Task 10)
     so each task owns its own test file. -->

#### Task 10: Overflow fast-track pass + `hw_review retry` + failed-footer

- **Description:** Add overflow fast-track to `job()` using the `_fallback_publish` helper from Task 9 (shared `attempt_count` per Decision 13, staged-row protection per Decision 7); add `hw_review retry N` indexing `list_failed()`; add the always-on failed-articles footer to `hw_review list` output.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_overflow.py -q` green with the queue pre-filled to `QUEUE_CAP` with mixed staged + unstaged rows; assert staged rows survive, unstaged evicted.
- **Files to modify:** `news_bot.py`, `hw_review.py`, `tests/test_overflow.py` (new), `tests/test_hw_review_retry.py` (new)
- **Files to read:** `news_bot.py`, `pending_articles_repo.py`, `work/manual-review-workflow/code-research.md` (§9.8)

### Audit Wave

#### Task 11: Code Audit

- **Description:** Full-feature code quality audit. Read all source files listed in each implementation task's `Files to modify`. Review for cross-component issues: shared-resource compliance with the Architecture table, architectural consistency with existing `*_source.py` style, error-handling uniformity (admin-notifier usage, sanitized `last_error`, logger usage, exit codes), and dead-code check (confirm `process_new_articles` fully removed).
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 12: Security Audit

- **Description:** Full-feature security audit. Read all source files created/modified. Analyse for OWASP Top 10 — SQL injection in repo module (parameterised queries?), JSON parse error handling, secret exposure (CLI may print rows containing Telegraph tokens — verify it doesn't), command-injection in `webbrowser.open` (file path sanitised?), input validation on `stage` JSON payload, confirmation-prompt bypass surface, Cyrillic encoding hazards in SQLite + HTML escapes. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 13: Test Audit

- **Description:** Full-feature test quality audit. Read all new + modified test files. Verify coverage, meaningful assertions (no bare `assert mock.called`), test pyramid balance (unit-heavy with targeted integration), that integration tests genuinely exercise the DB and CLI (not all mocks), that overflow and idle-fallback tests simulate time correctly. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 14: Pre-deploy QA

- **Description:** Acceptance testing: run `pytest tests/` and verify all acceptance criteria from both user-spec and tech-spec. Smoke-run the full operator flow on a tempfile DB end-to-end: simulate prep → confirm queue populated + admin ping text → `stage` a row → `preview --no-open` → `publish` (with Telegraph + Telegram mocked) → confirm row moved to `published_articles` with `via_review=true`. Produce QA report.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 15: Post-deploy verification

- **Description:** Live-environment verification after the user deploys to Docker/VPS:
  - Run `pytest tests/` against the production check-out — all green (tool: bash).
  - Read the admin chat last 24h of messages via Telegram MCP; assert admin ping format matches `"N ждут review: 🟠 … ×K, 🟣 … ×M, 🟢 … ×L"` with no zero-count sources and only when queue is non-empty.
  - Perform one end-to-end review cycle (list → show → stage → preview → publish) on a real unprocessed article. Verify via Telegram MCP that the channel received the post with the correct hashtag + Instant View preview card.
  - Spot-check the resulting Telegraph page via `curl` — hero image + subtitle + body paragraphs + source footer in expected positions.

  Tools: Telegram MCP, bash, curl.
- **Skill:** post-deploy-qa
- **Reviewers:** none
