# Decisions Log: manual-review-workflow

Agent reports on completed tasks. Each entry is written by the agent that executed the task.

---

<!-- Entries are added by agents as tasks are completed.

Format is strict — use only these sections, do not add others.
Do not include: file lists, findings tables, JSON reports, step-by-step logs.
Review details — in JSON files via links. QA report — in logs/working/.

## Task N: [title]

**Status:** Done
**Commit:** abc1234
**Agent:** [teammate name or "main agent"]
**Summary:** 1-3 sentences: what was done, key decisions. Not a file list.
**Deviations:** None / Deviated from spec: [reason], did [what].

**Reviews:**

*Round 1:*
- code-reviewer: 2 findings → [logs/working/task-N/code-reviewer-1.json]
- security-auditor: OK → [logs/working/task-N/security-auditor-1.json]

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-N/code-reviewer-2.json]

**Verification:**
- `npm test` → 42 passed
- Manual check → OK

-->

## Task 8: `hw_review publish` with Telegraph-URL reuse

**Status:** Done
**Commit:** 8d8e755
**Agent:** teammate (task-8)
**Summary:** Added the 6th subcommand `publish N` to `hw_review.py`, composing `telegraph_publisher.publish_article` + `pending_articles_repo.mark_telegraph_published` + `news_bot.send_telegraph_teaser` + `pending_articles_repo.move_to_published(via_review=True)` with the Decision 9 retry-idempotency contract: once `createPage` has succeeded, the resulting `telegraph_url` + derived `telegraph_path` are persisted on the pending row BEFORE the Telegram step, so a teaser failure (False-return OR bubbled exception) leaves the row retry-ready with `publish_article` guaranteed NOT to be called a second time. Decision 14 wiring verified via mock-arg assertion: `send_telegraph_teaser(telegraph_url, row['link'])` — source URL, not `source_name`. Added named module-level `_cleanup_preview_html(path)` helper (Task 9's `_fallback_publish` will reuse verbatim) that tolerates `FileNotFoundError` / `OSError` on the `os.unlink`. Three-state vanished-row matrix (already published / in failed / not found) surfaces single-line stderr diagnostics with zero Python traceback and zero external calls. All exception paths route through `news_bot.sanitize_error_message` (Decision 11) before logging or stderr.

**Deviations:**
- Reviews done inline. Real subagents (`code-reviewer`, `security-auditor`, `test-reviewer`) were not spawnable — the Task tool is not exposed in this environment. Each review was performed by re-reading the diff against its methodology dimensions and written as JSON under `logs/working/task-8/`. Same fallback as Tasks 1 / 5 / 6 / 7.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 6 findings (0 critical/major/minor, 2 low, 4 info — all "no action", intentional documentation or spec-aligned decisions) → [logs/working/task-8/code-reviewer-round1.json](logs/working/task-8/code-reviewer-round1.json)
- security-auditor: ok, 0 HIGH/CRITICAL/MEDIUM, 6 findings (1 low, 5 info — sanitiser applied at both sinks, stored-URL reuse trusted because self-written, path-guard already enforced at persist time, all SQL parameterised, `last_error` read-back safe because sanitised at write per Decision 11) → [logs/working/task-8/security-auditor-round1.json](logs/working/task-8/security-auditor-round1.json)
- test-reviewer: ok-with-nits, 8 findings (0 critical/major/minor, 1 low, 7 info — 15 tests cover all 10 TDD anchors plus 5 extras; gating smoke `test_publish_retry_reuses_telegraph_url` asserts `mock_publish.call_count == 1` across two runs) → [logs/working/task-8/test-reviewer-round1.json](logs/working/task-8/test-reviewer-round1.json)

*Round 2:* Skipped. Zero actionable findings across all three reviewers. No HIGH/CRITICAL / no MEDIUM / no MINOR. Every low/info item is explicitly flagged with "no action" (intentional, documented, or spec-aligned). Per Step 5 of the task runbook, round 1 is final.

**Verification:**
- `pytest tests/test_hw_review_publish_flow.py -v` → 15 passed (0.57s)
- `pytest tests/ -q` → 339 passed (324 baseline + 15 new, no regression)
- Smoke (gating): `pytest tests/test_hw_review_publish_flow.py::TestPublishRetryIdempotency::test_publish_retry_reuses_telegraph_url -v` → 1 passed, `mock_publish.call_count == 1` across both publish runs (Decision 9 verified end-to-end against tempfile SQLite with Telegraph+Telegram mocked)
- CLI smoke: `python3 hw_review.py --help` shows `{list,show,stage,skip,preview,publish}`; `python3 hw_review.py publish --help` shows `usage: hw_review publish [-h] n`
- **User verification pending** (Task 8 has `verify: [smoke, user]`): user must stage and publish a single article against the live Telegram channel (`@myhwchannel123`) + live Telegra.ph API and visually confirm (a) the channel post carries the correct source hashtag (`#autoevolution` / `#mattel` / `#lamleygroup`), (b) the Instant View preview card renders above the hashtag pointing at the freshly-created Telegraph URL, (c) tapping the card opens the Telegra.ph page with hero image + decorated subtitle + body + source footer. Can be bundled with the Task 7 user verification since both live on the same deployed `dev` branch.

## Task 7: `hw_review` CLI with `list` / `show` / `stage` / `skip` / `preview`

**Status:** Done
**Commits:** 5450d53 (feature), 38b2766 (round-1 fix)
**Agent:** teammate (task-7)
**Summary:** New stdlib-only `hw_review.py` in the repo root — argparse dispatcher for five operator subcommands (`list`, `show`, `stage`, `skip`, `preview`). Each subcommand returns an `int` exit code via `cmd_*`, routed through `main()` → `sys.exit`. `stage` reads JSON on `sys.stdin.buffer` with a 256 KiB byte-cap BEFORE `json.loads`, then runs a hardened validator (depth ≤ 3, top-key allowlist `{ru_paragraphs, ru_blocks}`, per-block-type key allowlist, per-string 10 KiB cap, ≤ 100 paragraphs) and cross-checks block parity against the pending row. `preview` lazily renders `telegraph_publisher.preview_nodes(...)` → `preview_renderer.render_html(...)` → `tempfile.NamedTemporaryFile` inside `~/.cache/hw-review/` (mode 0o700, chmod'd defensively), asserts `path.parent == CACHE_DIR.resolve()` BEFORE any `webbrowser.open`, and persists the path via the newly-added `pending_articles_repo.set_preview_path` helper so Task 8's publish/skip cleanup can unlink it.

**Deviations:**
- Added `preview_html_path` column to `pending_articles.DDL` and `set_preview_path(link, path|None)` helper to `pending_articles_repo.py` — explicitly deferred from Task 1 per that teammate's deviation note ("Task 7 extends pending_articles_repo.py with the column and helper at the time the preview CLI needs them"). Updated `EXPECTED_PENDING` in both `tests/test_pending_articles_repo.py` and `tests/test_migration.py` so schema-drift tests stay honest. Added two unit tests for `set_preview_path` (write + clear; no-op on missing link).
- Reviews done inline. Real subagents (`code-reviewer`, `security-auditor`, `test-reviewer`) were not spawnable: the Task tool is not exposed in this environment (only `ToolSearch` / `TaskStop`). Each review was performed by re-reading the diff against its methodology dimensions and written as JSON under `logs/working/task-7/`. Same fallback as Tasks 1 / 5 / 6.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 4 findings (1 low/ux fixed — skip prompt now uses `sys.stderr.write` + flush so the `[y/N]:` cursor stays inline; 3 info-level "no action") → [logs/working/task-7/code-reviewer-round1.json](logs/working/task-7/code-reviewer-round1.json)
- security-auditor: clean, 0 HIGH/CRITICAL, 6 info-level confirmations (stdin cap pre-parse, validator allowlists, `0o700` cache dir, path-guard via resolved `Path` equality, no shell-injection in `webbrowser.open`, no secret logging) → [logs/working/task-7/security-auditor-round1.json](logs/working/task-7/security-auditor-round1.json)
- test-reviewer: clean, 0 findings, full TDD-anchor coverage matrix plus 4 extras (EOF-on-skip, headless webbrowser, idempotent re-preview, empty `ru_paragraphs` accepted) → [logs/working/task-7/test-reviewer-round1.json](logs/working/task-7/test-reviewer-round1.json)

*Round 2:* Skipped. Only one actionable finding across all three reviewers (cr-1, low/UX) — fixed in 38b2766. No HIGH/CRITICAL/MEDIUM open.

**Verification:**
- `pytest tests/test_hw_review_cli.py -v` → 46 passed
- `pytest tests/ -q` → 324 passed (276 baseline + 48 new; 46 CLI + 2 repo `set_preview_path`)
- Smoke 1 (empty-queue `list`): seeded tempfile DB → `python3 hw_review.py list` → stdout `queue is empty`, exit 0
- Smoke 1b (list with failed footer): inserted one `failed_articles` row → stdout includes `⚠️ 1 неопубликованных в failed: [...]`, exit 0 (Decision 8 verified)
- Smoke 2 (preview --no-open on staged row): stdout printed `/home/vscode/.cache/hw-review/hw-XXXXXXXX.html`; file exists with `-rw-------`, directory is `drwx------`, body starts with `<!DOCTYPE html>` + CSP meta + Cyrillic `<h1>РУ заголовок</h1>`; exit 0.
- **User verification pending** (Task 7 has `verify: [smoke, user]`): user must run `python3 hw_review.py preview <N>` on a staged row without `--no-open` and visually confirm hero image + `💬 «…»` subtitle + body + `Источник:` footer in the opened browser page.

## Task 6: Refactor `job()` into prep-only + cron bump + delete `process_new_articles`

**Status:** Done
**Commit:** 0140a98
**Agent:** teammate (task-6)
**Summary:** Rewrote `news_bot.job()` into a prep-only 6-step pipeline (fetch via SOURCES → filter against processed_news + pending_articles → stage into pending_articles → single `build_admin_ping` notification) per Decision 10. Fully deleted `process_new_articles` (no deprecation, no feature flag). Bumped the cron cadence from daily to hourly via `schedule.every().hour.do(job)` per Decision 5 so the 2h grace window is enforceable. `init_db` now delegates DDL for `pending_articles` / `published_articles` / `failed_articles` to `pending_articles_repo.init_schema` while retaining its own `processed_news` DDL. Idle-fallback (Task 9) and overflow fast-track (Task 10) are explicit TODO-tagged placeholders: the repo queries (`list_pending_stale`, `list_notified_overdue`) are real so the scaffolding stays exercised; only the admin-ping and publish bodies are deferred. Added 16 new tests (3 in `test_migration.py`, 13 in `test_job_prep_phase.py`); flipped 17 existing integration assertions to staging-only semantics.
**Deviations:**
- The old `test_integration.py::test_telegraph_failure_skips_teaser_and_db` case was REMOVED rather than flipped — in the prep phase there is no Telegraph call to fail, so that behaviour has naturally moved into Task 8's `hw_review publish` (and will be covered by `test_hw_review_publish_flow.py`).
- `tests/test_feed_iteration.py::test_global_limit` was renamed to `test_no_global_limit` — the `limit=3` cap is gone with `process_new_articles`; hard-cap enforcement is Task 10's overflow pass.
- Reviews were performed inline (loaded each methodology mentally, analysed against its dimensions, wrote JSON reports manually). The Task tool for spawning subagents was not available in this session; the teammate brief explicitly authorised this fallback.
- Tests patch `news_bot.SOURCES` as a list rather than patching the individual fetcher names, because `SOURCES = [_fetch_rss_entries, _fetch_mattel_entries]` is bound at module load time — patching the attribute `news_bot._fetch_rss_entries` does NOT change what the stored list references. A `_patch_sources` helper in `test_job_prep_phase.py` encapsulates this.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 4 findings (all low/info, recommend "no action"; unused `requests` / `TelegraphError` imports flagged for Task 11 audit) → [logs/working/task-6/code-reviewer-round1.json](logs/working/task-6/code-reviewer-round1.json)
- security-auditor: ok, 0 HIGH/CRITICAL, 4 info findings (Markdown disruption via exception messages already sanitised, fetcher payload validation owned by downstream layers, double-commit on init_db is harmless, N+1 filter query cost negligible at QUEUE_CAP scale) → [logs/working/task-6/security-auditor-round1.json](logs/working/task-6/security-auditor-round1.json)
- test-reviewer: ok-with-nits, 5 findings (all low/info, "no action"; 4 litmus mutations traced mentally), full AC coverage matrix → [logs/working/task-6/test-reviewer-round1.json](logs/working/task-6/test-reviewer-round1.json)

*Round 2:* Skipped. All 13 findings across three reviewers are low/info with "no action" recommendations; no HIGH/CRITICAL/MEDIUM requiring a fix. Per the task runbook's Step 5 ("if only nits → break round loop and defer"), round 1 is final.

**Verification:**
- `pytest tests/test_migration.py tests/test_job_prep_phase.py -v` → 16 passed (0.65s)
- `pytest tests/test_integration.py tests/test_mattel_integration.py tests/test_feed_iteration.py tests/test_database.py -q` → 17 passed (Verify-smoke green)
- `pytest tests/ -q` → 276 passed (260 baseline + 16 new, zero regression)
- Smoke 1 (all 4 tables created): `python3 -c "...init_db()...SELECT name FROM sqlite_master..."` → `['failed_articles', 'pending_articles', 'processed_news', 'published_articles']`, exit 0
- Smoke 2 (process_new_articles absent): `python3 -c "import news_bot; assert not hasattr(news_bot, 'process_new_articles')"` → exit 0
- Smoke 3 (hourly cron): `python3 -c "...assert 'every().hour' in inspect.getsource(news_bot.main)"` → exit 0

## Task 4: Public `preview_nodes` wrapper in `telegraph_publisher`

**Status:** Done
**Commit:** 570dac4
**Agent:** teammate (task-4)
**Summary:** Added public `preview_nodes(title, paragraphs, images, source_url, subtitle, blocks)` in `telegraph_publisher.py` — offline mirror of the Telegraph node tree that `publish_article` uploads, with no network call and no `TELEGRAPH_ACCESS_TOKEN` required. Refactored `publish_article` to delegate node building to `preview_nodes`, making the new wrapper the single source of truth and eliminating any drift risk between preview and real publish. Private `_build_content` / `_build_content_from_blocks` left untouched per spec.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved, 1 minor suggestion (documented-as-designed `title` parameter is unused inside the body — expected per task spec) → [logs/working/task-4/code-reviewer-round1.json](logs/working/task-4/code-reviewer-round1.json)
- test-reviewer: passed, 0 findings, 12/12 litmus → [logs/working/task-4/test-reviewer-round1.json](logs/working/task-4/test-reviewer-round1.json)

**Verification:**
- `pytest tests/test_telegraph_publisher.py -v` → 32 passed (20 existing + 12 new `TestPreviewNodes`)
- `pytest tests/ --ignore=tests/test_pending_articles_repo.py --ignore=tests/test_admin_ping.py -q` → 173 passed (other ignored files are new in-progress work from parallel teammates)
- Smoke 1: `python3 -c "import telegraph_publisher; print(telegraph_publisher.preview_nodes(title='t', paragraphs=['p'])[0]['tag'])"` → `p`, exit 0
- Smoke 2: `python3 -c "... preview_nodes(..., paragraphs=['a','b'], images=[...], source_url=..., subtitle='sub'); assert all('tag' in n for n in nodes); print('OK, nodes:', len(nodes))"` → `OK, nodes: 6`, exit 0

## Task 2: `preview_renderer` module

**Status:** Done
**Commits:** bcf8121 (initial), 55df5b7 (round-2 fix)
**Agent:** teammate (task-2), resumed by main agent after rate-limit
**Summary:** New stdlib-only `preview_renderer.py` (pure `render_html(nodes, title) -> str`) implementing tech-spec Decision 1's three hardening layers: tag allowlist (frozenset of the 10 tags `telegraph_publisher` emits), URL-scheme allowlist (`^https?://` IGNORECASE on `img src`, `iframe src`, `a href` — drops javascript/data/file/vbscript/relative/leading-whitespace), and an exact CSP meta (`default-src 'none'; img-src https:; frame-src https:; style-src 'unsafe-inline'`) in `<head>`. All text and retained attribute values go through `html.escape(quote=True)`; Cyrillic preserved. 63 pytest cases, stdlib-only, no `requirements.txt` change. Round-2 fix added `_SAFE_ATTR_NAME_RE = ^[A-Za-z][A-Za-z0-9_-]*$` to drop malformed attribute names (e.g. `'x" onerror="alert(1)'`) — covered by 8 new assertions plus a full `html.parser.HTMLParser` round-trip structural check.
**Deviations:** None (`_ALLOWED_TAGS` is a `frozenset` rather than a plain `set`; functionally equivalent, arguably safer — acknowledged as a nit in round-1 code-review and left as-is).

**Reviews:**

*Round 1:*
- code-reviewer: OK, 2 nits (frozenset choice, `quote=True` on title — both "no action") → [logs/working/task-2/code-reviewer-round1.json](logs/working/task-2/code-reviewer-round1.json)
- security-auditor: OK (1 HIGH finding was fixed in-review in the initial commit: attribute-name injection guard `_SAFE_ATTR_NAME_RE` added) → [logs/working/task-2/security-auditor-round1.json](logs/working/task-2/security-auditor-round1.json)
- test-reviewer: OK, 1 nit (documented `</style>` carve-out in `test_other_unknown_tags_dropped`) → [logs/working/task-2/test-reviewer-round1.json](logs/working/task-2/test-reviewer-round1.json)

*Round 2 (after fixes):*
- code-reviewer: OK, 0 findings → [logs/working/task-2/code-reviewer-round2.json](logs/working/task-2/code-reviewer-round2.json)
- security-auditor: OK, 0 findings → [logs/working/task-2/security-auditor-round2.json](logs/working/task-2/security-auditor-round2.json)
- test-reviewer: OK, 0 findings → [logs/working/task-2/test-reviewer-round2.json](logs/working/task-2/test-reviewer-round2.json)

Nits deferred (all from round 1, judged not worth fixing): frozenset vs set (safer as-is), `quote=True` on `<title>` text (defensive uniformity), `</style>` carve-out in unknown-tag test (the tested invariant — payload absence — holds unconditionally; the carve-out is inline-documented).

**Verification:**
- `pytest tests/test_preview_renderer.py -v` → 63 passed (0.04s)
- `pytest tests/ -q` → 237 passed (full suite, no regression)
- Smoke 1 (`render_html([{'tag':'p','children':['test']}], 'T')[:60]` starts with `<!DOCTYPE html>`) → OK, exit 0
- Smoke 2 (URL-scheme filter drops `javascript:` from `img src`) → OK, exit 0
- Smoke 3 (CSP meta contains `default-src 'none'`, `img-src https:`, `frame-src https:`) → OK, exit 0

## Task 3: admin-ping + source vocabulary + sanitize_error_message

**Status:** Done
**Commit:** c3858cc
**Agent:** teammate (task-3), resumed by main agent after rate-limit
**Summary:** Added three foundational helpers to `news_bot.py` consumed by later waves (Decisions 4/11/12): `SOURCE_EMOJI` / `SOURCE_LABEL` dicts keyed by `autoevolution`/`mattel`/`lamley` (no `rss`, no `other`), `build_admin_ping(rows)` emitting byte-exact `"N ждут review: 🟠 autoevolution ×K, 🟣 mattel ×M, 🟢 lamley ×L"` with stable literal-tuple ordering and `None` on empty queue, and `sanitize_error_message(exc)` that redacts the four env-secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_ID`, `TELEGRAPH_ACCESS_TOKEN`) with an explicit `if value and value.strip()` guard against the `str.replace('', ...)` character-interleaving pathology, plus an outer try/except so sanitizer bugs can never break the caller's error path. Also introduced env-overridable knobs `IDLE_TIMEOUT_HOURS` / `GRACE_WINDOW_HOURS` / `QUEUE_CAP` (consumed by Task 6). 25 new pytest cases, stdlib-only.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 4 minor findings, all recommend "no action" → [logs/working/task-3/code-reviewer-round1.json](logs/working/task-3/code-reviewer-round1.json)
- security-auditor: ok, 5 findings (1 low, 1 low, 3 info), all recommend "no action" → [logs/working/task-3/security-auditor-round1.json](logs/working/task-3/security-auditor-round1.json)
- test-reviewer: ok-with-nits, 5 minor findings, all recommend "no action" → [logs/working/task-3/test-reviewer-round1.json](logs/working/task-3/test-reviewer-round1.json)

*Round 2:* Skipped. All 14 findings are minor/low/info with the reviewers themselves recommending "no action" — either matches spec explicitly (CR-1 graceful-degradation wrapper, CR-3 literal-tuple ordering, CR-4 strict `Counter` subscript per task-3 Edge Cases, SEC-2/3/4 secret-redaction guards, TR-2/TR-3/TR-5 coverage confirmations) or is explicitly out of scope for task 3 (CR-2 / SEC-5 env-knob parsing — Task 6 job loop owns those; SEC-1 secret substring overlap — 30-50 char opaque tokens make overlap astronomically unlikely, documented as accept-as-is; TR-1 pre-existing `[REDACTED]` marker — optional test, not in acceptance criteria). No HIGH/CRITICAL / no MEDIUM requiring action. Per Step 5 of the task runbook, round 1 is final.

**Verification:**
- `pytest tests/test_admin_ping.py -v` → 25 passed (0.77s)
- `pytest tests/ -q` → 237 passed (full suite, no regression from parallel tasks 1/2/4)
- Smoke 1 (`sanitize_error_message(Exception('api abc xyz'))` with `TELEGRAM_BOT_TOKEN='abc'`) → `api [REDACTED] xyz`, exit 0
- Smoke 2 (`build_admin_ping([])`) → `None`, exit 0
- Smoke 3 (`build_admin_ping([...2×autoevolution, 1×mattel])`) → `3 ждут review: 🟠 autoevolution ×2, 🟣 mattel ×1`, exit 0
- Smoke 4 (`set(SOURCE_EMOJI) == set(SOURCE_LABEL) == {'autoevolution', 'mattel', 'lamley'}`) → `ok`, exit 0

## Task 1: `pending_articles_repo` DAO

**Status:** Done
**Commit:** 553710f
**Agent:** teammate (task-1), reviews resumed by main agent after rate-limit
**Summary:** New stdlib-only `pending_articles_repo.py` implementing `init_schema` plus the 19 CRUD / list / transactional-move functions prescribed by Task 1 scope. DDL uses `CREATE TABLE IF NOT EXISTS` for all three new tables (`pending_articles`, `published_articles`, `failed_articles`) with columns matching tech-spec §Data Models via `PRAGMA table_info` dict-literal assertions. JSON fields (`paragraphs`, `images`, `blocks`, `ru_paragraphs`, `ru_blocks`) round-trip through `json.dumps(..., ensure_ascii=False)` / `json.loads`, distinguishing NULL from `'[]'`. Transactional moves (`move_to_published`, `move_to_failed`, `skip_pending`, `retry_from_failed`) hold one connection with explicit `try` / `commit()` / `except: rollback(); raise` / `finally: close()`. All SQL uses `?` placeholders (grep-style audit test enforces this). 31 pytest cases, all green.
**Deviations:** `preview_html_path` column and `set_preview_path` helper — listed in tech-spec §Data Models and §Repo module interface — are intentionally NOT in this commit. Per Task 7 scope (tasks/7.md §Details and §What-to-do step linking `set_preview_path`), Task 7 extends `pending_articles_repo.py` with the column and helper at the time the preview CLI needs them. Task 1's task-file What-to-do lists exactly 19 functions; the implementation matches that list.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 5 findings (3 low, 2 info; all "no action" or docs-only nits — unused module-level logger, undocumented `increment_attempt` missing-row return, `news_bot` cyclic-import awareness, SQLite TOCTOU theoretical window) → [logs/working/task-1/code-reviewer-round1.json](logs/working/task-1/code-reviewer-round1.json)
- security-auditor: ok, 5 findings (all info, no action) — confirmed parameterised SQL, safe `int()`-coerced datetime modifier, intentional raw-`last_error` storage per Decision 11 / AC L103, empirical rollback verification, no JSON-deserialisation attack surface → [logs/working/task-1/security-auditor-round1.json](logs/working/task-1/security-auditor-round1.json)
- test-reviewer: ok-with-nits, 6 findings (3 low, 3 info — all optional: missing-link no-op tests for `increment_attempt` / `move_to_failed`, entry-dict key-omission coverage, WrappingConn execute-count coupling note, stubbed `processed_news` DDL drift awareness, regex-audit scope) → [logs/working/task-1/test-reviewer-round1.json](logs/working/task-1/test-reviewer-round1.json)

*Round 2:* Skipped. All 16 findings across three reviewers are low/info with explicit "no action" or docs-only recommendations; no HIGH/CRITICAL, no correctness defect, no security vulnerability. Per Step 4 of the task runbook (all status `ok`/`ok-with-nits` and no HIGH/CRITICAL → break out), round 1 is final.

**Verification:**
- `pytest tests/test_pending_articles_repo.py -v` → 31 passed (2.08s)
- Smoke 1 (`python3 -c "import pending_articles_repo; pending_articles_repo.init_schema(__import__('sqlite3').connect(':memory:'))"`) → exit 0
- Smoke 2 (tempfile DB → `init_schema` → `insert_pending({link, source_name, title, paragraphs=['p'], images=[], blocks=None, pub_date=None})` → `count_pending()`) → prints `1`, exit 0

## Task 5: Source registry + source_name tagging

**Status:** Done
**Commits:** 93b8004 (impl), 349fd4f (round-1 fix)
**Agent:** teammate (task-5)
**Summary:** Added Decision 4's source-dispatch groundwork to `news_bot.py`: `NETLOC_TO_SOURCE` dict with the five explicit netloc keys → three `source_name` values (`autoevolution`/`lamley`/`mattel`); `_resolve_source_name(link)` helper (case-insensitive, exception-safe, returns `'other'` on miss); `_fetch_rss_entries(notifier)` with per-feed try/except error isolation, explicit field-selection normalisation of FeedParserDict entries to plain dicts (only `link`/`title`/`published`/`summary` survive, so feedparser internals never leak into Task 6's `pending_articles` JSON columns), `feed_url` stamping, and a WARNING log on `'other'`; `_fetch_mattel_entries(notifier)` thin wrapper that None-guards `fetch_mattel_news` and stamps `source_name='mattel'`; and the module-level `SOURCES = [_fetch_rss_entries, _fetch_mattel_entries]` list. `job()`, `process_new_articles`, `_source_hashtag`, and Task 3's `SOURCE_EMOJI`/`SOURCE_LABEL`/`build_admin_ping` are untouched — Task 6 owns the `job()` refactor. 23 new pytest cases covering all 11 TDD anchors plus 12 additional edge cases (empty string, no scheme, missing link, empty list fallback, notifier passthrough, real `feedparser.parse()` round-trip for the normalisation check).
**Deviations:** Reviews were performed inline (loaded each methodology skill, analysed against its dimensions, wrote JSON reports manually) because the Task tool for spawning subagents was not available in this session. The teammate brief explicitly authorised this fallback.

**Reviews:**

*Round 1:*
- code-reviewer: pass-with-minor, 1 minor (unused `_RSS_ENTRY_FIELDS` tuple) → [logs/working/task-5/code-reviewer-round1.json](logs/working/task-5/code-reviewer-round1.json)
- security-auditor: pass, 0 findings — `@`-userinfo / IDN / percent-encoded-dot / subdomain-suffix / port-injection spoofing vectors all fail-closed to `'other'` via exact-match dict lookup; warning log only prints public URL strings → [logs/working/task-5/security-auditor-round1.json](logs/working/task-5/security-auditor-round1.json)
- test-reviewer: pass, 2 minors (optional callable-redundancy test, comment-clarity nit — both deferred) → [logs/working/task-5/test-reviewer-round1.json](logs/working/task-5/test-reviewer-round1.json)

*Round 2:* Skipped. The single valid minor (dead `_RSS_ENTRY_FIELDS` tuple) was fixed in 349fd4f and its rationale was inlined where the code lives. No HIGH/CRITICAL / no behavioural change from the fix, so a second review round would only re-confirm what round 1 already approved. The two test-reviewer nits are documented above as deferred.

**Verification:**
- `pytest tests/test_sources_registry.py -v` → 23 passed (0.25s) — all 11 TDD anchors + 12 edge cases
- `pytest tests/ -q` → 260 passed (237 baseline + 23 new, no regression)
- Smoke 1 (`python3 -c "from news_bot import SOURCES, _fetch_rss_entries; print([f.__name__ for f in SOURCES])"`) → `['_fetch_rss_entries', '_fetch_mattel_entries']`, exit 0
- Smoke 2 (`python3 -c "from news_bot import _resolve_source_name; print(_resolve_source_name('https://lamleygroup.com/post/x'), ..., _resolve_source_name('https://unknown.example/z'))"`) → `lamley autoevolution mattel other`, exit 0

## Task 9: Idle-fallback pass + hw_review take

**Status:** Done
**Commits:** 0d1ad98 (impl), a4daeb4 (round-1 CR-1 mitigation)
**Agent:** teammate (task-9)
**Summary:** Wired the idle-fallback pass (tech-spec §Prep phase 1a/1b) into `news_bot.job()`: step (1a) sends ONE consolidated admin ping (Decision 12) summarising every stale row and `mark_notified`'s each; step (1b) calls the new module-level helper `_fallback_publish(row, via_review=False)` per overdue row with per-row try/except so one failure can't abort the pass. `_fallback_publish` reuses Task 8's Decision-9 idempotency pattern (reuse stored `telegraph_url` if non-NULL, else `publish_article` → `mark_telegraph_published`), the Decision-11 `sanitize_error_message` helper on every error path, the Decision-13 shared `increment_attempt` counter (3 strikes → `move_to_failed`), and the same `_cleanup_preview_html` contract that Task 8 introduced (parallel helper in `news_bot.py` to avoid a news_bot→hw_review reverse import). Also added `hw_review take N` subcommand: `clear_notified` on pending rows, clean `exit 1` + "already auto-published" stderr when the row has already left pending (AC L67), `exit 1` + "index out of range" on bad N. 20 new tests across `test_idle_fallback.py` (12) and `test_hw_review_take.py` (8); 339 → 359 total, no regressions.

**Deviations:** Reviews were performed inline (loaded each methodology skill and scored the diff against its dimensions, wrote JSON reports manually to `logs/working/task-9/`) because the Task tool for spawning subagents was not available in this session. The teammate brief explicitly authorised this fallback.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 5 findings (1 low CR-1 ordering fix APPLIED in a4daeb4, 1 low CR-2 deliberate parallel helper, 3 info no-action) → [logs/working/task-9/code-reviewer-round1.json](logs/working/task-9/code-reviewer-round1.json)
- security-auditor: ok, 5 findings (all info/low, zero HIGH/MEDIUM) — confirmed no secret leakage in `_fallback_publish` paths (all exceptions pass through `sanitize_error_message`), attempt-counter race benign under single-threaded cron, admin-ping title concatenation safe under Telegram's 4096-char limit → [logs/working/task-9/security-auditor-round1.json](logs/working/task-9/security-auditor-round1.json)
- test-reviewer: ok-with-nits, 6 findings (all info/low) — all 7 TDD anchors for idle_fallback + 5 for take covered, SQL-based time manipulation per Testing Strategy, two-layer unit+integration split for `_fallback_publish` → [logs/working/task-9/test-reviewer-round1.json](logs/working/task-9/test-reviewer-round1.json)

*Round 2:* Skipped. Only CR-1 required action; applied in a4daeb4 (reordered `update_staged` after `mark_telegraph_published`) — preserves `ru_paragraphs IS NULL` on Telegraph failure so `list_notified_overdue` re-matches the row on the next tick, keeping auto-retry viable. All other findings are explicit accept-as-is / no-action per the reviewer's own recommendation. Per the task runbook (status ok/ok-with-nits + no HIGH/CRITICAL → round 1 final).

**Verification:**
- `pytest tests/test_idle_fallback.py -v` → 12 passed — all 7 TDD anchors + 5 bonus (helper unit tests, per-row isolation, teaser-False persists URL)
- `pytest tests/test_hw_review_take.py -v` → 8 passed — all 5 TDD anchors + 3 bonus (zero-index, empty queue, never-notified)
- `pytest tests/ -q` → 359 passed (339 baseline + 20 new, no regression)
- Smoke 1 (3 pending rows with `fetched_at = datetime('now', '-50 hours')` → mock `send_admin_notification` → call `job()` with `SOURCES=[]`) → exactly ONE heads-up call: `"Will auto-publish in ~2h: First, Second, Third. Intercept via hw_review take N"`, exit 0
- Smoke 2 (`python3 -c "import hw_review; hw_review.main(['take', '--help'])"`) → usage printed, exit 0

## Task 10: Overflow fast-track + hw_review retry + failed-footer

**Status:** Done
**Commits:** e6e4b95 (impl + tests)
**Agent:** teammate (task-10)
**Summary:** Wired the overflow fast-track pass (tech-spec §Prep phase step 4) into `news_bot.job()`: the new module-level `_overflow_fast_track(new_entries)` helper runs when `count_pending() + len(new) > QUEUE_CAP`, calls `pending_articles_repo.list_pending_for_eviction()[:needed]` (repo enforces `ru_paragraphs IS NULL` + `fetched_at ASC` per Decision 7 — staged rows never evicted), and runs `_fallback_publish(row, via_review=False)` per candidate — **reusing** Task 9's helper so overflow and idle-fallback share the exact same Decision-9 Telegraph-URL idempotency contract. Failures sanitise via `sanitize_error_message` (Decision 11) and bump the shared Decision-13 `attempt_count`; the third strike in any combination moves the row to `failed_articles`. Admin ping `"Queue pressure: auto-published {E}, {D} new deferred, {S} staged rows protected"` + optional suffix `", fast-track failed for {F}"` fires only when there's pressure to report (deferred, protected, or errors). Added `hw_review retry N` — 1-based index into `list_failed()` (ORDER BY `failed_at DESC`, matching the Decision-8 footer's rendering order so operator indices line up), calls `retry_from_failed(link)` which resets `attempt_count=0` and stamps a fresh `fetched_at` per Decision 10. The failed-footer in `hw_review list` was already landed by Task 7 (byte-for-byte format `"⚠️ {K} неопубликованных в failed: [t1, t2, ...]. hw_review retry N чтобы переподнять."`); Task 10's new tests pin that format byte-exact and assert the index-ordering contract between footer and `retry N`. 24 new tests across `test_overflow.py` (10) and `test_hw_review_retry.py` (14); 359 → 383 total, no regressions.

**Deviations:** Reviews were performed inline (loaded each methodology skill's dimensions and scored the diff against them, wrote JSON reports manually to `logs/working/task-10/`) because the Task tool for spawning subagents was not available in this session. The teammate brief explicitly authorised this fallback.

**Reviews:**

*Round 1:*
- code-reviewer: approved, 4 low findings (all no-action: intentional _fallback_publish reuse per Decision 13, defensive accepted=[] on overflow-pass exception, evicted_count +1 on move_to_failed is semantically "slot freed", retry-False exit-1 is safer than silent success) → [logs/working/task-10/code-reviewer-round1.json](logs/working/task-10/code-reviewer-round1.json)
- security-auditor: approved, 5 low findings — admin-ping uses only integer counts (no Telegram 4096-limit risk, no secret-leak surface); all `last_error` writes go through `sanitize_error_message` (Decision 11); staged-row protection delegated to repo-level SQL filter (Decision 7); `retry N` uses `argparse type=int` + DB-originated link (no injection surface); `retry_from_failed` transactional (verified in pending_articles_repo.py:652–657) → [logs/working/task-10/security-auditor-round1.json](logs/working/task-10/security-auditor-round1.json)
- test-reviewer: approved, 6 low findings — all 13 TDD anchors covered, byte-exact format pinning on admin-ping and failed-footer strings, tempfile-DB integration matches project pattern, SQL-based `failed_at` pinning makes DESC-ordering deterministic (no timing flake) → [logs/working/task-10/test-reviewer-round1.json](logs/working/task-10/test-reviewer-round1.json)

*Round 2:* Skipped. Zero HIGH/CRITICAL findings across all three reviewers; all low findings are explicit accept-as-is / no-action per each reviewer's own recommendation.

**Verification:**
- `pytest tests/test_overflow.py -v` → 10 passed — all 7 TDD anchors + 3 bonus (empty-entries no-op, happy-path-no-ping, job() integration smoke)
- `pytest tests/test_hw_review_retry.py -v` → 14 passed — all 6 TDD anchors + 8 bonus (print-on-success, index-2 selection, negative-index, already-in-pending defensive, count+order, exact-format, retry→list integration)
- `pytest tests/ -q` → 383 passed (359 baseline + 24 new, no regression)
- Smoke (`pytest tests/test_overflow.py::TestOverflowInJob::test_job_overflow_smoke_mixed_queue -q`) → queue pre-filled 10/10 (4 staged + 6 unstaged), 2 new RSS entries → staged rows untouched, oldest 2 unstaged evicted to `published_articles` with `via_review=0`, both new entries inserted; queue size remains at cap (10), exit 0

## Task 14: Pre-deploy QA

**Status:** Done
**Commit:** ec97d88
**Agent:** qa-runner
**Summary:** PASS — ready to deploy. 383/383 tests green in 14.79s. All 22 user-spec ACs + all 17 tech-spec ACs (Decisions 1–14) verified `pass`; 4 criteria (channel-post visual, local HTML preview visual, live Telegraph render fidelity, live admin-ping format) deferred to Task 15 post-deploy. Full end-to-end smoke on tempfile DB (prep → stage → preview --no-open → publish with Telegraph/Telegram mocked) confirmed byte-exact admin ping `"3 ждут review: 🟠 autoevolution ×1, 🟣 mattel ×1, 🟢 lamley ×1"`, row migration to `published_articles` with `via_review=1`, dedup-table write, and preview-file cleanup. Schema sanity: 4 tables present (`processed_news`, `pending_articles`, `published_articles`, `failed_articles`) with `preview_html_path` column; `init_db()` idempotent. Inherited zero blockers from Task 11 (code audit — clean) and Task 12 (security audit — clean); Task 13 test-audit MEDIUM gaps (mixed-path 3-strike test, overflow Decision 9 transitive coverage) are test-coverage only, code implements invariants correctly via shared `_fallback_publish`. Static checks: no `ruff`/`mypy`/`flake8`/`pyproject.toml` configured — CI runs only `pytest`. CLI smoke: 9 subcommand `--help` invocations + `list` on empty ephemeral DB all exit 0.

**Deviations:** None. Pre-deploy QA is read-only by contract; any code changes would be escalations.

**Deferred to post-deploy (4):** US-User-01 (channel post visual), US-User-02 (local HTML preview visual), Tech-AVP-01 (Telegraph page render fidelity via curl), Tech-AVP-02 (live admin-ping format via Telegram MCP). Full handoff contract in JSON `deferred_to_post_deploy` block.

**QA reports:**
- [logs/working/qa/pre-deploy-qa.json](logs/working/qa/pre-deploy-qa.json) — machine-readable (orchestrator reads `status`)
- [logs/working/qa/pre-deploy-qa.md](logs/working/qa/pre-deploy-qa.md) — human-readable summary + AC matrix
- [logs/working/task-14/qa-report.json](logs/working/task-14/qa-report.json) — task-scoped copy (identical)

**Verification:**
- `python3 -m pytest tests/ -q` → `383 passed in 14.79s`
- `python3 -c "import news_bot; import hw_review; import pending_articles_repo; import preview_renderer; import telegraph_publisher; print('ok')"` → `ok`
- `grep -nP "f['\"].*(SELECT|INSERT|UPDATE|DELETE)|\+.*(SELECT|INSERT|UPDATE|DELETE)" pending_articles_repo.py hw_review.py` → empty (exit 1) — parametrised SQL invariant
- `python3 -c "from news_bot import build_admin_ping; assert build_admin_ping([]) is None"` → exit 0
- sanitize_error_message smoke with all 4 env-secrets → `[REDACTED]` present, no secret leak
- preview_renderer smoke: `javascript:` dropped, `default-src 'none'` present
- Schema smoke: `init_db()` twice → 4 tables (`failed_articles`, `pending_articles`, `processed_news`, `published_articles`), `preview_html_path` column present on `pending_articles`
- Cron smoke: `inspect.getsource(main)` contains `every().hour.do(job)` → True
- End-to-end smoke (tempfile DB, patched SOURCES + fetch_full_article + send_admin_notification + telegraph_publisher._api_call + hw_review.send_telegraph_teaser): prep 3 rows → ping byte-match → stage 1 → preview --no-open prints cache path → publish → `published_articles.via_review=1`, `processed_news` dedup written, `pending_articles` count 3→2, preview HTML unlinked → `SMOKE OK`
- CLI smoke: 9 `--help` subcommand calls + `list` on empty ephemeral DB → all exit 0

## Task 13: Test Audit

**Status:** Done
**Commit:** a187071
**Agent:** test-audit
**Summary:** needs_fixes (non-blocking) — 368 distinct `def test_` functions across 23 test files, 383 green per Task 10 baseline. Findings: 0 critical / 0 high / 2 medium / 5 low / 2 nit. Medium findings: (a) no mixed-path 3-strike test pinning user-spec L70 "3 failures in any combination of idle-fallback and overflow" (shared counter is pinned at repo level, cross-path invariant is not); (b) Decision 9 Telegraph-URL reuse independently pinned at 2 of 3 call-sites (hw_review publish + idle fallback) — overflow site inherits transitively via `_fallback_publish`. Litmus-sampled 15 tests, all fail when the SUT's core logic is hollowed. All tech-spec Testing-Strategy checklist items are covered except the two gaps above.
**Deviations:** None.

**Audit report:** [logs/working/audit/test-audit.json](logs/working/audit/test-audit.json)

**Verification:**
- Holistic read of 17 test files (all new + modified from Tasks 1–10).
- Litmus-sampled 15 tests across the suite — each would fail if the SUT's core logic were removed.
- Cross-checked user-spec ACs + tech-spec Decisions 1–14 against suite coverage; 25 coverage-matrix rows recorded in JSON (23 OK, 1 partial, 1 missing).
- Verified Decision 9 (Telegraph URL reuse idempotency) pinned at 2 of 3 call-sites (hw_review publish + idle fallback); overflow site inherits via `_fallback_publish` helper (transitive only) — logged as medium gap.
- Verified Decision 13 (shared counter, 3-strike → failed) pinned at every increment boundary in same-path tests (idle 1st/3rd, overflow 1st/3rd); mixed-path (idle+overflow combined) is the missing case — logged as medium gap.
- Weak-assertion scan: 1 bare `assertTrue(mock_admin.called)` in test_job_prep_phase.py:341, 1 bare `assertRaises(Exception)` without match in test_idle_fallback.py:450 — logged as low findings.
- Neither medium gap blocks Pre-deploy QA; both are strong candidates for a P1 follow-up wave before post-deploy verification.

## Task 12: Security Audit

**Status:** Done
**Commit:** 2653e7a
**Agent:** security-audit
**Summary:** clean (PASS-WITH-FINDINGS). Zero CRITICAL / zero HIGH / zero MEDIUM / 3 LOW / 4 INFO findings. All five tech-spec invariants (a–e) PASS with code evidence: parameterised SQL, `last_error` routed through `sanitize_error_message` on every write, CSP+URL-scheme+attribute-name allowlists in `preview_renderer`, `path.parent == CACHE_DIR` guard before `webbrowser.open`, and the 8-vector `stage` validator (256 KiB cap, depth, key allowlist, size cap, block-type/key allowlist). Safe to proceed to Pre-deploy QA (Task 14); the LOW findings are defense-in-depth / hygiene follow-ups, none block deploy.
**Deviations:** None.

**Audit report:** [logs/working/audit/security-audit.json](logs/working/audit/security-audit.json)

**Verification:**
- OWASP coverage: A01 PASS · A02 PASS · A03 PASS · A04 PASS · A05 PASS · A06 PASS · A07 N/A · A08 PASS · A09 PASS with info-level uniformity nit · A10 PASS.
- Grep invariants: `execute(f|%|.format)` in pending_articles_repo.py → 0 hits. `last_error=` writes → 1 (UPDATE ? placeholder). `webbrowser.open` → 1 call-site, guarded. `default-src 'none'` in preview_renderer.py → present. `ensure_ascii=False` → present in `_dumps`.
- Feature-specific checks: secret-exposure in `hw_review show` (PASS, last_error sanitised at write), Cyrillic encoding (PASS, ensure_ascii=False round-trip), symlink/TOCTOU on preview cache (PASS via resolved path-guard), Telegraph token in network errors (PASS, token is in POST body not URL), empty-env-var sanitise pathology (PASS, guarded at news_bot.py:88), overflow eviction race (PASS, `ru_paragraphs IS NULL` applied in SQL WHERE, not Python).
- LOW findings queued as P3 follow-ups: (1) mirror path-guard on `preview_html_path` cleanup unlinks, (2) validate `--ru-title`/`--ru-subtitle` arg length, (3) detect symlink-replaced CACHE_DIR before `mkdir(exist_ok=True)`.

## Task 11: Code Audit

**Status:** Done
**Commit:** bccb584
**Agent:** code-audit
**Summary:** clean — zero CRITICAL / zero HIGH / zero MEDIUM; 6 LOW + 5 NIT findings across 11 dimensions. Decision 9 (Telegraph-URL reuse idempotency) verified consistent at 3 call-sites (`hw_review.cmd_publish`, `news_bot._fallback_publish`, `news_bot._overflow_fast_track` via shared helper). Decision 13 (3-strike shared `attempt_count` → `failed_articles`) verified consistent at 2 auto-publish call-sites (`job()` step 1b + `_overflow_fast_track`) — manual `hw_review publish` intentionally excluded per user-spec L70. `process_new_articles` fully removed (grep confirms only backward-pointing comments). Findings are defense-in-depth hygiene: 3 unused imports in `news_bot.py`, 4 non-uniform `sanitize_error_message` sites, 1 stale docstring on `hw_review._cleanup_preview_html`, 2 mixed import-style sites, 1 unused logger in repo, 2 misnumbered step comments, 1 semantic-naming nit on `evicted_count` (counts 3-strike-failed rows as "auto-published"), 1 import-ordering nit on `pending_articles_repo` vs `DB_FILE`, 1 --ru-title length validation gap (also flagged by security-audit as P3). None block Pre-deploy QA.
**Deviations:** None.

**Audit report:** [logs/working/audit/code-audit.json](logs/working/audit/code-audit.json)

**Verification:**
- Holistic read of 5 feature source files (pending_articles_repo.py 657 LOC, preview_renderer.py 192 LOC, news_bot.py 1177 LOC, telegraph_publisher.py 310 LOC, hw_review.py 797 LOC — 3133 total).
- Contract verification — Decision 9: walked all 3 call-sites, confirmed each persists `telegraph_url` via `mark_telegraph_published` BEFORE Telegram step so a teaser failure leaves retry-ready state. CR-1 fix from Task 9 (reorder `update_staged` AFTER `mark_telegraph_published`) verified at news_bot.py:628 then :637.
- Contract verification — Decision 13: walked both auto-publish call-sites, confirmed each calls `pending_repo.increment_attempt(link, safe)` on `_fallback_publish` raise and `pending_repo.move_to_failed(link, safe)` at `new_count >= 3`. Shared SQL counter via `UPDATE pending_articles SET attempt_count = attempt_count + 1, last_error = ? WHERE link=?` (pending_articles_repo.py:266-269) — no per-path counters.
- Grep invariants: `process_new_articles` only in 2 comments (news_bot.py:567, :1099); `sanitize_error_message` applied at 19 feature-introduced sites, missing at 4 (flagged as findings #2, #3); `_cleanup_preview_html` has 2 copies (flagged as finding #4).
- Cross-reviewed against security-audit.json (0 CRITICAL/HIGH/MEDIUM, 3 LOW, 4 INFO) and test-audit.json (0 CRITICAL/HIGH, 2 MEDIUM coverage-gaps, 5 LOW, 2 NIT) — two MEDIUM test-audit gaps (mixed-path 3-strike + overflow Decision 9) are test-coverage issues, not code-quality issues; the code itself implements both invariants correctly via the shared `_fallback_publish` helper.


---

## Ad-hoc: deploy.sh FILES update

**Status:** Done
**Commit:** e341480
**Agent:** ad-hoc teammate (deploy-pipeline skill)
**Summary:** Feature delivered 10 coding tasks but `deploy.sh` FILES list was never updated — it only shipped `news_bot.py`, `feeds.json`, `requirements.txt`, `.env.example`, so the cron path would `ImportError` on the first run after deploy. Added the five first-party modules the hourly `job()` actually reaches: `autoevolution_source.py`, `mattel_news_source.py`, `lamley_source.py` (all in `news_bot.SOURCES`), `telegraph_publisher.py` (used by `_fallback_publish` / `_overflow_fast_track`), and `pending_articles_repo.py` (heavily used by `job()` for DB ops). Added a comment above FILES documenting the split: server-deployed modules vs operator-only CLI modules (`hw_review.py`, `preview_renderer.py`) vs never-deployed user data (`news.db`). Kept the existing SCP + `pip install --user` shape — no migration to git-based deploy, no CI pipeline, no venv hardening (out of scope; LOW nit logged in review JSON for a future ticket).
**Deviations:** None.
**Reviews:** [deploy-fix-round1.json](logs/working/adhoc/deploy-fix-round1.json) — 1 round, verdict APPROVE, 0 HIGH / 0 MEDIUM / 1 LOW (pre-existing `pip install --user` without venv, out of scope). Reviewer fell back to inline — no `deploy-reviewer` subagent type is registered in this environment.
**Verification:** `bash -n deploy.sh` → `syntax OK`. No scp/ssh executed (target is the placeholder `user@example.com`; actual deploy is the operator's call).


---

## Ad-hoc: `_overflow_fast_track.staged_protected` empty-queue bug

**Status:** Done
**Commit:** 0669419
**Agent:** adhoc-staged-protected
**Summary:** Local QA Variant B (single `job()` on empty queue + 36 fetched entries) surfaced a classification bug: `staged_protected = needed - len(candidates)` reported 26 phantom "protected" rows on empty queue. Fixed to `max(0, min(gap, pre_count - len(candidates)))` — now reports 0 on empty queue, N on partial protection. 3 new tests pin the three semantic regimes (empty / partial / full). Captured `pre_count` once at function start and reused it for `slots_free` to avoid an extra DB query.
**Deviations:** None.

**Reviews:**
- code-reviewer: PASS (0 critical / 0 major / 2 minor — both out-of-scope: repo-outage mislabelled-as-protection is a pre-existing concern, and expression density is a stylistic preference). Reviewer ran inline — no `code-reviewer` subagent type is registered in this environment. Report: [staged_protected_fix-code-review.json](../../logs/working/adhoc/staged_protected_fix-code-review.json).
- test-reviewer: PASS (0 critical / 0 major / 2 minor — kept: three-regime symmetry is self-documenting, and ping-string assertion matches existing file conventions). Reviewer ran inline. Report: [staged_protected_fix-test-review.json](../../logs/working/adhoc/staged_protected_fix-test-review.json).

**Verification:**
- New tests: `test_overflow_empty_queue_protected_is_zero` (red→green: pre-fix asserted `'protected 26'` got `'protected 0'`), `test_overflow_partial_protection` (4 ru-NULL + 6 ru-staged, needed=6 → protected=2), `test_overflow_full_protection` (10 ru-staged, needed=6 → protected=6).
- Full suite: `python3 -m pytest tests/ -q` → 386 passed (383 baseline + 3 new), 0 existing tests broken.
- Local smoke re-run of `python3 -c "from news_bot import job; job()"` on empty queue — **needs live verification after fix** (expected log: `[overflow] evicted=0, deferred=26, protected=0, errors=0`).
