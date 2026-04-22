---
created: 2026-04-22
status: draft
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
1. Idle-fallback pass: rows with `fetched_at > 48h ago` AND `notified_at IS NULL` → send heads-up admin ping, stamp `notified_at`. Rows with `notified_at > 2h ago` AND `ru_paragraphs IS NULL` → run `transcreate_text` on EN paragraphs → `publish_article` → `send_telegraph_teaser` → `move_to_published(via_review=False)`. On failure: `increment_attempt`; at 3 strikes → `move_to_failed`.
2. Fetch all sources via the `SOURCES` registry; each entry gets `source_name` attached based on URL netloc (for RSS) or the fetcher's own tag (Mattel).
3. Filter against `processed_news` AND existing `pending_articles`.
4. Overflow fast-track: if `count_pending() + len(new) > 10`, run fallback-publish on oldest rows with `ru_paragraphs IS NULL` to free slots; never touch staged rows; any still-unfit new entries are dropped for this tick with an admin ping.
5. For each accepted new entry: call `fetch_full_article(entry)`, then `insert_pending` (JSON-serialise paragraphs/images/blocks).
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

**Decision:** `hw_review preview N` renders the Telegraph node tree into a local HTML file (`/tmp/hw-review-{hash}.html`) and opens it via `webbrowser.open`. No Telegra.ph `createPage` during preview; no `editPage` cleanup machinery; the final `publish` is the only Telegraph API call per article.
**Rationale:** The structural checks the operator does in preview (paragraph order, image positions, subtitle framing, footer) are identical in both renderings. The user-spec explicitly chose local HTML after round 1 of user-spec validation. Removes ~40% of the feature's surface: no `edit_page` wrapper, no `preview_url`/`preview_path` churn during drafts, no `cleanup-drafts` subcommand, no Telegra.ph API-change risk.
**User-spec anchor:** Supports "Preview — локальный HTML-файл" (Ограничения, user-spec).
**Alternatives considered:** Telegra.ph draft via `createPage`+`editPage` — rejected for surface cost; browser-headless diff against msg 35 — rejected as out-of-scope.

### Decision 2: Four-table data model (processed_news + pending + published + failed)

**Decision:** Extend the schema with three new tables: `pending_articles` (WIP queue), `published_articles` (audit of real publishes with `via_review` flag), `failed_articles` (terminal state after 3 GT attempts). `processed_news` stays schema-unchanged; its semantic broadens from "published" to "seen" — every link that was published OR skipped OR historically processed.
**Rationale:** The four concerns (dedup / queue / audit / dead-letter) have disjoint columns and query patterns. A single-table "status enum" approach would mix in-progress rows with audit rows and make overflow/idle queries heavier.
**User-spec anchor:** Supports "3 новые таблицы" and "Dedup-таблица содержит ссылку для любой статьи, которая была опубликована ИЛИ скипнута" (user-spec Технические решения + AC).
**Alternatives considered:** Single `articles` table with `status` column — rejected, muddles semantics; audit in external log file — rejected, loses SQL-based inspection.

### Decision 3: Preview created only at `hw_review preview N` time

**Decision:** Preview HTML is generated only when the operator explicitly runs `hw_review preview N`. Not at prep time, not at `stage`. If the operator goes straight from `stage` to `publish`, no preview file is ever created and `publish` skips any cleanup.
**Rationale:** Preview is an optional review step. Generating it eagerly wastes I/O and leaves more stale files to clean. The local-HTML cost is tiny either way, but lazy creation is simpler code and matches operator intent.
**User-spec anchor:** Supports "Preview — локальный HTML-файл... создаётся только при явном `hw_review preview N`" (user-spec Ограничения).

### Decision 4: Source registry with URL-netloc-derived `source_name`

**Decision:** Introduce a `SOURCES` list of fetcher callables in `news_bot.py`. Each entry received from a source gets a `source_name` attribute normalised to `'autoevolution'`, `'mattel'`, or `'lamley'` — inferred from the URL netloc for RSS entries, hardcoded for Mattel. Adding a new source means adding one fetcher function + one list entry.
**Rationale:** Hardcoded `fetch_mattel_news` inside `job()` is a scalability smell. The admin-ping format requires per-source breakdown; `source_name` must be consistent regardless of whether an entry arrived via `feeds.json` (RSS) or a dedicated fetcher (Mattel). URL netloc is the authoritative signal for RSS entries since both autoevolution and lamley currently come through RSS.
**User-spec anchor:** Supports "Добавление нового источника — редактирование одного списка" (user-spec AC).
**Alternatives considered:** Per-source class hierarchy — rejected as overkill at 3 sources; keep hardcoding Mattel — rejected, defeats the admin-ping counting contract.

### Decision 5: Cron frequency bump from daily to hourly

**Decision:** `schedule.every().hour.do(job)` replaces `schedule.every().day.at("12:00").do(job)` in `news_bot.main`. `IDLE_TIMEOUT_HOURS` (default 48), `GRACE_WINDOW_HOURS` (default 2), and `QUEUE_CAP` (default 10) become env-overridable module constants.
**Rationale:** The user-spec's 2h grace window is meaningless at daily cadence — next tick is 24h away. Hourly ticks make `notified_at > 2h` enforceable. Cost is 72 fetches/day across three cheap sources (RSS feeds + 2 HTML pages), well below any rate limit. Admin-ping is suppressed on empty queue, so there's no notification spam.
**User-spec anchor:** Supports "Grace-окно ... дефолт 2 часа, значение параметризуется" and "Возможный bump до часовой гранулярности — осознанный 1-строчный follow-up" (user-spec Ограничения).
**Alternatives considered:** Keep daily, redefine grace as "next tick" — rejected, makes grace semantically 24h which the operator may not expect; run an out-of-band 2h-timer — rejected, adds process-management complexity.

### Decision 6: `stage N` reads ru-paragraphs/blocks as JSON on stdin

**Decision:** `hw_review stage N` takes `ru_title` and `ru_subtitle` as `argparse` flags, and `ru_paragraphs` + `ru_blocks` from stdin as a JSON object: `echo '{"ru_paragraphs":[...], "ru_blocks":null}' | hw_review stage 3 --ru-title "..."`.
**Rationale:** Claude Code produces JSON naturally; shell-escaping Russian newlines for flags is fragile; a temp file adds cleanup burden. Repeated flags lose ordering guarantees on paragraphs.
**User-spec anchor:** `[TECHNICAL]` — user-spec leaves argument-surface open for tech-spec. Justification: simplest working ergonomics for Claude-driven flow.

### Decision 7: Overflow fast-track never evicts rows with staged ru

**Decision:** `list_pending_for_eviction` returns only rows where `ru_paragraphs IS NULL`. When the queue is at cap and the operator has staged half of it, the bot drops new incoming entries rather than overwrite operator work. Admin ping surfaces both the evicted count and the protected count.
**Rationale:** This is the exact CLI-cron race the feature is built to close. An eviction that replaces staged translation with Google Translate output defeats the design.
**User-spec anchor:** Supports AC "Очередь никогда не превышает 10 pending-записей. При переполнении **только записи с пустым русским** прогоняются через GT-fast-track перед INSERT новых" (user-spec AC).
**Alternatives considered:** Evict staged rows and alert — rejected, destroys human work.

### Decision 8: Failed-articles backlog shown on every `list` invocation

**Decision:** `hw_review list` always appends a `⚠️` footer with count + titles + retry hint whenever `failed_articles` is non-empty, regardless of whether the main queue is empty or not.
**Rationale:** Failed rows are dead-letter state requiring operator intervention; hiding them is a silent backlog growth. The footer is cheap one-line rendering; its presence on `list` is the natural discovery path.
**User-spec anchor:** Supports AC "Если в таблице failed что-то накопилось — в любом случае показывает футер с количеством и списком" (user-spec Как должно работать + AC).

### Decision 9: Telegraph URL stored on the pending row for retry idempotency

**Decision:** `pending_articles` has `telegraph_url` and `telegraph_path` columns (NULL until first successful `createPage`). `hw_review publish N` calls `createPage` only when `telegraph_url` is NULL; on a Telegram-send failure, the stored URL is reused on subsequent retries and no duplicate Telegraph page is created.
**Rationale:** Partial success (Telegraph OK, Telegram fail) is a real failure mode at this latency. Without URL persistence, the second `publish` creates an orphan Telegraph page on every retry. `telegraph_path` is stored now (one extra column, near-zero cost) to keep a future `editPage` door open without another migration.
**User-spec anchor:** Supports AC "повторный `hw_review publish N` **переиспользует** существующую Telegraph-страницу (вторая не создаётся)" (user-spec AC).

### Decision 10: `process_new_articles` deleted; prep-phase does not publish

**Decision:** The existing `process_new_articles` function in `news_bot.py` (the fetch→translate→publish orchestrator, currently limit=3) is removed entirely. Its fetch work merges into the prep path; its publish work moves to `hw_review publish` + the idle-fallback + overflow-fast-track helpers. The `limit=3` concept is replaced by `QUEUE_CAP=10`.
**Rationale:** Keeping the function as legacy creates dead code + confused semantics ("is `process_new_articles` still called?"). Deleting it forces the test suite to honestly express the new shape.
**User-spec anchor:** Supports AC "Prep-фаза в `job()` больше не публикует в канал напрямую — ни одного вызова `send_telegraph_teaser` по ходу prep-цикла" (user-spec AC).

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

None. The feature uses only stdlib (`argparse`, `sqlite3`, `json`, `webbrowser`, `html.escape`, `hashlib.md5`, `tempfile`).

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
- `preview_renderer`: renders all allowed tags; escapes Cyrillic and HTML special chars; void tags emit self-closing; unknown tags silently dropped.
- `build_admin_ping`: empty → None; sources with zero count omitted; formatting string matches spec exactly (byte-compared).
- `hw_review.py` argparse: each subcommand accepts expected args; exit codes 0/1; `stage` rejects partial ru or invalid JSON; `skip` with staged ru requires confirmation; `publish` on vanished link emits clean error.
- `SOURCES` registry: RSS entries get `source_name` from URL netloc; Mattel entries get `'mattel'`; lamley RSS → `'lamley'`; autoevolution RSS → `'autoevolution'`.
- Overflow fast-track algorithm: staged rows never evicted; fast-track failure increments `attempt_count`; third failure moves to `failed_articles`.

### Integration tests

- Prep phase end-to-end with all source fetchers mocked: `pending_articles` populated, zero Telegraph / Telegram calls.
- Publish phase end-to-end with Telegraph + Telegram mocked: stage → preview → publish; row moves pending → published; local HTML file deleted; `via_review=True`.
- Publish-retry: first publish creates Telegraph page and mock-Telegram fails → row stays pending with `telegraph_url` set; second publish reuses the URL and succeeds → moves to published.
- Idle-fallback: stale `fetched_at` → heads-up ping + `notified_at` stamped; overdue → GT + auto-publish; `take N` in between clears `notified_at`.
- Overflow: queue pre-loaded to 10 with mix of staged + unstaged; prep-phase with new entries → unstaged evicted via GT, staged protected, deferred count in admin ping.
- Migration: empty tempfile DB → `init_db()` creates all 4 tables with expected columns (assert via `PRAGMA table_info`).
- Flipped existing integration tests: `test_integration.py`, `test_mattel_integration.py`, `test_feed_iteration.py` — assert staging-only, zero auto-publish calls after prep.

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
| Operator leaves preview HTML files accumulating in `/tmp` | Files are overwritten on repeat preview by link-hash; `publish` and `skip` delete. Worst case: ≤10 files × ~50KB at a time; cleared on host reboot. Acceptable. |
| `fetch_full_article` network failure drops the entry for this tick | Existing behaviour — entry is not marked in `processed_news` or `pending_articles`, so next cron tick retries. Matches `news_bot.py:372-374` current skip rule. |

## User-Spec Deviations

- **Hourly cron instead of "default daily with 1-line bump"**: user-spec says "Cron продолжает бежать минимум раз в сутки... Возможный bump до часовой гранулярности — осознанный 1-строчный follow-up, решение отложено до tech-spec". Tech-spec chooses the bump now because the 2h grace window is meaningless at daily cadence. All other timing parameters (48h idle, 2h grace) stay as spec'd. → [PENDING USER APPROVAL]
- **`source_name` vocabulary: `autoevolution`/`mattel`/`lamley`** instead of the `rss`/`mattel`/`lamley` that code-research §9.4 initially proposed. Reason: lamley articles arrive via RSS (in `feeds.json`) alongside autoevolution, so tagging by URL-netloc gives the operator a clear per-outlet breakdown in the admin ping. User-spec fixes the format `"🟠 autoevolution ×K, 🟣 mattel ×M, 🟢 lamley ×L"` — matching the vocabulary exactly. → [PENDING USER APPROVAL]
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

#### Task 3: Admin-ping helper + source vocabulary

- **Description:** Add `SOURCE_EMOJI`, `SOURCE_LABEL` dicts and `build_admin_ping(rows)` function to `news_bot.py`. Ping returns `None` on empty queue; format matches user-spec exactly, zero-count sources are omitted.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `tests/test_admin_ping.py` (new)
- **Files to read:** `work/manual-review-workflow/user-spec.md`, `work/manual-review-workflow/code-research.md` (§9.4)

#### Task 4: Public `preview_nodes` wrapper in `telegraph_publisher`

- **Description:** Expose a public helper in `telegraph_publisher.py` that returns the node tree the current `publish_article` would send — without calling `createPage`. `preview_renderer` uses it. Existing node-building functions (`_build_content`, `_build_content_from_blocks`) stay private; only the wrapper is public.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `telegraph_publisher.py`, `tests/test_telegraph_publisher.py`
- **Files to read:** `telegraph_publisher.py`, `work/manual-review-workflow/code-research.md` (§9.6)

### Wave 2 (prep-phase refactor — depends on Wave 1)

#### Task 5: Source registry + `source_name` tagging

- **Description:** Introduce `SOURCES` list in `news_bot.py` with named fetcher helpers (`_fetch_rss_entries`, `_fetch_mattel_entries`). RSS entries get `source_name` derived from URL netloc (`autoevolution`/`lamley`/fallback `other`). Mattel entries are tagged `'mattel'`. All entries are normalised to plain dicts (not `FeedParserDict`).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "from news_bot import SOURCES, _fetch_rss_entries; print([f.__name__ for f in SOURCES])"` includes `_fetch_rss_entries`, `_fetch_mattel_entries`.
- **Files to modify:** `news_bot.py`, `tests/test_sources_registry.py` (new)
- **Files to read:** `news_bot.py`, `mattel_news_source.py`, `autoevolution_source.py`, `lamley_source.py`, `feeds.json`, `work/manual-review-workflow/code-research.md` (§9.5)

#### Task 6: Refactor `job()` into prep-only + cron bump + delete `process_new_articles`

- **Description:** Rewrite `news_bot.job()` into the prep-phase-only pipeline (idle-fallback and overflow passes deferred to Tasks 9–10; this task delivers just the fetch/filter/stage/ping path). Delete `process_new_articles`. Bump cron from `every().day.at("12:00")` to `every().hour`. Add env-overridable constants `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `QUEUE_CAP`. Flip the 8 existing auto-publish integration tests in `test_integration.py`, `test_mattel_integration.py`, `test_feed_iteration.py` to assert staging-only. `init_db()` delegates new-table creation to `pending_articles_repo.init_schema`.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_integration.py tests/test_mattel_integration.py tests/test_feed_iteration.py tests/test_database.py -q` is green.
- **Files to modify:** `news_bot.py`, `tests/test_integration.py`, `tests/test_mattel_integration.py`, `tests/test_feed_iteration.py`, `tests/test_job_prep_phase.py` (new), `tests/test_migration.py` (new)
- **Files to read:** `news_bot.py`, files from Wave 1, `work/manual-review-workflow/code-research.md` (§9.10, §9.11, §9.12)

### Wave 3 (CLI + publish — depends on Waves 1-2)

#### Task 7: `hw_review` CLI skeleton + `list` / `show` / `stage` / `skip` / `preview`

- **Description:** New `hw_review.py` using stdlib `argparse` subparsers. Implement `list` (with failed-footer), `show`, `stage` (JSON on stdin for paragraphs/blocks, argparse flags for title/subtitle), `skip` (y/N confirmation when ru staged), `preview` (builds nodes via `preview_nodes`, renders via `preview_renderer`, writes `/tmp/hw-review-{hash}.html`, `webbrowser.open`, `--no-open` flag). Exit codes 0/1; stdout for human output, stderr for errors.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python hw_review.py list` on an empty test DB prints `"queue is empty"`; `python hw_review.py preview --no-open 1` on a staged row prints the HTML file path.
- **Verify-user:** Run `python hw_review.py preview <N>` on a staged row — local browser opens the preview with hero, subtitle, paragraphs, and source footer in the expected order.
- **Files to modify:** `hw_review.py` (new), `tests/test_hw_review_cli.py` (new)
- **Files to read:** `pending_articles_repo.py`, `preview_renderer.py`, `telegraph_publisher.py`, `work/manual-review-workflow/code-research.md` (§9.3, §9.6, §9.13)

#### Task 8: `hw_review publish` with Telegraph-URL reuse

- **Description:** Implement `hw_review publish N`. Flow: `get_pending` → precondition check (staged) → if `telegraph_url` NULL, call `publish_article` and `mark_telegraph_published`; else skip. Then `send_telegraph_teaser`. On success: `move_to_published(via_review=True)` + delete local preview file. On partial failure (Telegraph OK, Telegram fail): pending row retained with `telegraph_url` populated; clean stderr message tells operator to retry. On publish-on-vanished-row: clean error with current state, exit 1.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** End-to-end test mock assertion: second `publish` after Telegram-send failure calls `publish_article` exactly once across both runs.
- **Verify-user:** After a real publish, the channel shows the post with the correct hashtag and Instant View preview card.
- **Files to modify:** `hw_review.py`, `tests/test_hw_review_publish_flow.py` (new)
- **Files to read:** `telegraph_publisher.py`, `news_bot.py` (for `send_telegraph_teaser`), `pending_articles_repo.py`, `work/manual-review-workflow/code-research.md` (§9.9)

### Wave 4 (automation paths — depends on Waves 1-3)

#### Task 9: Idle-fallback pass + `hw_review take` command

- **Description:** Add idle-fallback pass at the top of `job()`: rows stale >`IDLE_TIMEOUT_HOURS` get heads-up admin ping and `mark_notified`; rows overdue >`GRACE_WINDOW_HOURS` go through `transcreate_text` + publish + `move_to_published(via_review=False)`. On failure, `increment_attempt`; 3 strikes → `move_to_failed`. Implement `hw_review take N` command that calls `clear_notified`; refuses if row has already left pending.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `hw_review.py`, `tests/test_idle_fallback.py` (new), `tests/test_hw_review_cli.py`
- **Files to read:** `news_bot.py`, `pending_articles_repo.py`, `work/manual-review-workflow/code-research.md` (§9.7, §9.10)

#### Task 10: Overflow fast-track pass + `hw_review retry` + failed-footer

- **Description:** Add overflow fast-track pass to `job()`: when `count_pending() + len(new_entries) > QUEUE_CAP`, evict oldest rows from `list_pending_for_eviction()` (NULL ru only) via the fallback-publish helper from Task 9; on fast-track failure, increment attempt and at 3 strikes move to failed. Defer unfit new entries with admin pressure ping. Implement `hw_review retry N` indexing `list_failed()`. Add failed-articles footer to `hw_review list` output (unconditional on non-empty failed table).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `hw_review.py`, `tests/test_overflow.py` (new), `tests/test_hw_review_cli.py`
- **Files to read:** `news_bot.py`, `pending_articles_repo.py`, `work/manual-review-workflow/code-research.md` (§9.8)

### Audit Wave

#### Task 11: Code Audit

- **Description:** Full-feature code quality audit. Read all source files created/modified in this feature (`pending_articles_repo.py`, `preview_renderer.py`, `hw_review.py`, `news_bot.py`, `telegraph_publisher.py`). Review for cross-component issues: shared resource compliance (single `news.db` path), architectural consistency with existing `*_source.py` style, error-handling uniformity (admin-notifier usage, logger usage, exit codes), dead-code check (confirm `process_new_articles` fully removed). Write audit report.
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
