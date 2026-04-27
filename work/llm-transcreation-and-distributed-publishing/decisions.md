# Decisions Log: llm-transcreation-and-distributed-publishing

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

## Task 1: Add bot_state migration

**Status:** Done
**Commit:** 6afa63b (feat), b11ed52 (review reports)
**Agent:** migrator
**Summary:** Added idempotent `CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)` DDL constant and a one-line `conn.execute(_BOT_STATE_DDL)` in `pending_articles_repo.init_schema()`. Tests cover table presence, the two-column PRAGMA shape (per tech-spec Data Models), and row-preservation across a second `init_db()` call. Schema kept minimal per tech-spec Decision 3 so the upcoming outage state machine (Task 5) can extend keys without further migrations.
**Deviations:** Reviewer cycle was performed inline by the migrator agent applying the `code-reviewing` and `test-master` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Both reviewers reached approved / no findings on round 1.

**Reviews:**

*Round 1:*
- code-reviewer: OK → [logs/working/task-01/code-reviewer-round1.json](logs/working/task-01/code-reviewer-round1.json)
- test-reviewer: OK → [logs/working/task-01/test-reviewer-round1.json](logs/working/task-01/test-reviewer-round1.json)

**Verification:**
- `pytest tests/test_migration.py -q` → 5 passed (test_all_tables_created, test_bot_state_schema, test_init_db_idempotent, test_pending_articles_has_expected_columns, test_processed_news_schema_unchanged)
- `pytest tests/test_pending_articles_repo.py -q` → 33 passed (regression — DDL addition did not break the repo's CRUD tests)

## Task 6: Update requirements.txt + .env.example

**Status:** Done
**Commit:** c1ec8fc
**Agent:** deps
**Summary:** Pinned `anthropic>=0.45.0,<0.46.0` and `pytz>=2024.1` (positions: anthropic adjacent to deep-translator, pytz next to schedule). Replaced legacy FALLBACK_THROTTLE_SECONDS block in `.env.example` with three new variables — `ANTHROPIC_API_KEY` (required), commented `# ANTHROPIC_MODEL=claude-haiku-4-5` (optional override), and `TZ=Europe/Moscow` — each with an inline comment in the project's existing style.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: OK (no findings) → [logs/working/task-06/code-reviewer-round1.json](logs/working/task-06/code-reviewer-round1.json)

**Verification:**
- `python3 -c "import sys; assert sys.version_info >= (3,8)"` → OK (Python 3.13)
- `grep -E '^(anthropic|pytz)' requirements.txt` → 2 lines matched
- `grep -E '^(ANTHROPIC_API_KEY|TZ)=' .env.example` → 2 lines matched
- `grep -c 'ANTHROPIC_MODEL' .env.example` → 1 (commented optional default)
- `grep -E '(FALLBACK_THROTTLE_SECONDS|QUEUE_CAP|IDLE_TIMEOUT_HOURS|GRACE_WINDOW_HOURS)' .env.example` → no matches (legacy vars purged)
- `pip install -r requirements.txt --dry-run` → skipped (no isolated venv in dev container; Wave 2 Task 3/5 verify-smoke will exercise the install on first import).

## Task 2: Create compute_publish_slots.py + tests

**Status:** Done
**Commit:** bbf629a (feat), 5ce36e4 (round-1 fix)
**Agent:** scheduler
**Summary:** Pure-functional stdlib-only scheduling module implementing the adaptive `interval = max(remaining_minutes / N, 40)` algorithm over the 13:00–20:00 МСК window. The 11-publishes/day cap emerges naturally from `floor(420/40)+1` — no separate constant. Single `effective_start = max(window_start, now)` expression collapses the two scenarios (cron tick from 12:00 — full 420-min pacing; container restart mid-window — recompute from `now`) into one branchless formula. TZ-naive input raises `ValueError`; returned datetimes inherit `now.tzinfo`. 14 tests (13 from TDD anchor + 1 module-constants helper) plus a 15th boundary-at-20:00 test added in round-1 fixes.
**Deviations:** Reviewer cycle was performed inline by the scheduler agent applying the `code-reviewing` and `test-master` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Both reviewers approved on round 1 (2 minors total); applicable minors fixed; round 2 clean.

**Reviews:**

*Round 1:*
- code-reviewer: 2 minor (CR-1 applied — comment on `n <= 0` defensive coercion; CR-2 left optional — cross-TZ normalisation, no current caller violates invariant) → [logs/working/task-02/code-reviewer-round1.json](logs/working/task-02/code-reviewer-round1.json)
- test-reviewer: 1 minor (TR-1 applied — added test_now_exactly_at_window_end to lock `>=` boundary) → [logs/working/task-02/test-reviewer-round1.json](logs/working/task-02/test-reviewer-round1.json)

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-02/code-reviewer-round2.json](logs/working/task-02/code-reviewer-round2.json)
- test-reviewer: OK → [logs/working/task-02/test-reviewer-round2.json](logs/working/task-02/test-reviewer-round2.json)

**Verification:**
- `pytest tests/test_compute_publish_slots.py -q` → 15 passed in 0.02s (covers N=0,1,4,7,10,11,15,20; restart-at-16:00 → interval=48; restart-at-19:50 → 1 slot + 4 carry-over; now=20:00 boundary; now=21:00 post-window; tz-naive ValueError; tzinfo preservation; module-constants export).
- `python3 -c "from compute_publish_slots import compute_publish_slots, WINDOW_START, WINDOW_END, MIN_INTERVAL_MINUTES; print('OK')"` → OK (no import errors, no external deps beyond stdlib).

## Task 4: Extend `_TokenRedactingFilter` for `ANTHROPIC_API_KEY` (3-layer)

**Status:** Done
**Commit:** 0c6e3e8 (initial impl, mis-labelled "chore: review reports for task 02"), b1125d0 (round-1 fix: handler-level filter)
**Agent:** redactor
**Summary:** Implemented Decision 12 three-layer defense for `sk-ant-...` keys. Layer 1: new `_ANTHROPIC_KEY_RE = re.compile(r'sk-ant-[A-Za-z0-9_=.-]{16,}')` covers prod and sandbox/admin shapes (with `=` and `.`). Layer 2: `'ANTHROPIC_API_KEY'` added to `_SECRET_ENV_NAMES`. Layer 3: redaction core extracted to module-level `_redact_text(text)` helper used by both `_TokenRedactingFilter` and `send_admin_notification`; the redaction block was reordered above `send_admin_notification` to satisfy the forward-reference invariant noted in the task. Filter additionally attached to `anthropic` / `anthropic._client` / `anthropic._base_client` loggers, and (round-1 fix) to the basicConfig StreamHandler on root so propagated records from arbitrary child loggers are also scrubbed. Module-level invariant comment instructs future callers to use `type(exc).__name__` instead of `str(exc)` in admin-ping payloads.
**Deviations:** Reviewer cycle was performed inline by the redactor agent applying the `code-reviewing` and `security-auditor` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Round 1 raised one shared minor/low finding (Python `Logger.filter()` does not run on propagated records from arbitrary child loggers) — initially marked out-of-scope, then promoted to round-1 fix when smoke check #1 (synthetic 'smoke' logger emitting a `sk-ant-...` key) demonstrated the gap as a real exposure. Round 2 clean.

**Reviews:**

*Round 1:*
- code-reviewer: 3 minor (1 doc, 1 logging-architecture promoted to round-1 fix, 1 robustness no-action) → [logs/working/task-04/code-reviewer-round1.json](logs/working/task-04/code-reviewer-round1.json)
- security-auditor: 3 findings (1 low promoted to round-1 fix, 1 low no-action, 1 info) → [logs/working/task-04/security-auditor-round1.json](logs/working/task-04/security-auditor-round1.json)

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-04/code-reviewer-round2.json](logs/working/task-04/code-reviewer-round2.json)
- security-auditor: OK → [logs/working/task-04/security-auditor-round2.json](logs/working/task-04/security-auditor-round2.json)

**Verification:**
- `pytest tests/test_no_token_leak_in_logs.py -q` → 30 passed in 0.24s (12 pre-existing Telegram-token tests + 14 new Anthropic-key tests + 2 round-1-fix tests + 2 admin-notify tests).
- Smoke check 1 (arbitrary child logger 'smoke' → key in stderr): redacted to `***` (after handler-filter fix).
- Smoke check 2 (`anthropic._client` sandbox-shape key with `=` and `.`): redacted to `***`.
- Smoke check 3 (`send_admin_notification` with `sk-ant-...` in message → captured `Bot.send_message text=`): redacted to `***`; clean message passes through unchanged.

## Task 5: Create `outage_state.py` + tests

**Status:** Done
**Commit:** a1900a0 (feat), 6f1009e (round-1 fix), b9cc507 (review reports)
**Agent:** outage
**Summary:** New `outage_state.py` (~330 LoC) implements the SQLite-backed Claude API outage state machine over the `bot_state` table created in Task 1. Key/value getters/setters per Decision 3; pure `_compute_next_state` separates transition logic from SQL I/O; `record_outage_event` / `record_recovery_event` wrap read-then-write in `BEGIN IMMEDIATE`; `PRAGMA busy_timeout = 5000` per Decision 16. Reads tolerate corrupted content (warn + None, never crash). 12 tests cover all four transitions (no_outage→ping_1→ping_2→fallback→recovery), persistence across simulated restart, real concurrent-writer serialization (post-1h boundary race fails without `BEGIN IMMEDIATE`), corrupted-content tolerance, and naive-datetime rejection.
**Deviations:** Reviewer cycle was performed inline by the outage agent applying the `code-reviewing` and `test-master` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Both reviewers raised minor findings on round 1 (3 + 4); all applied; round 2 clean.

**Reviews:**

*Round 1:*
- code-reviewer: 3 minor (CR-1 perf — double-checked locking on recovery hot path; CR-2 magic 3600/7200 → named timedelta constants; CR-3 documented set_fallback_active(False)='0' vs recovery-DELETE asymmetry) → [logs/working/task-05/code-reviewer-round1.json](logs/working/task-05/code-reviewer-round1.json)
- test-reviewer: 4 minor (TR-1 corrupted-content tolerance tests; TR-2 naive-dt rejection x3; TR-3 strengthened concurrency test — race past 1h boundary so missing BEGIN IMMEDIATE produces 2 ping #2 emissions; TR-4 explicit defaults test) → [logs/working/task-05/test-reviewer-round1.json](logs/working/task-05/test-reviewer-round1.json)

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-05/code-reviewer-round2.json](logs/working/task-05/code-reviewer-round2.json)
- test-reviewer: OK → [logs/working/task-05/test-reviewer-round2.json](logs/working/task-05/test-reviewer-round2.json)

**Verification:**
- `pytest tests/test_outage_state.py -v` → 12 passed in 0.89s (3 outage transitions + recovery + persistence + concurrency + 3 tolerance + 3 naive-dt rejection).
- All transitions from code-research §14.4 covered: no_outage→ping_1_sent (started_at set, ping_count=1, fallback_now=False); ping_1_sent→ping_2_sent at 1h+1s (ping_count=2, fallback_now=True); ping_2_sent→google_fallback_active at 2h+1s (ping_count=3, fallback_active=1); recovery clears all keys + idempotent on already-clean state.

## Task 3: Create claude_transcreation.py + tests

**Status:** Done
**Commit:** 5ca6782 (feat), 21d8ed9 (round-1 fix), e3d6318 (review reports)
**Agent:** claude-wrapper
**Summary:** Implemented Anthropic SDK wrapper module per Decisions 5/6/8/13. Public API: `transcreate_via_claude`, `health_check`, `ClaudeOutageError`, `ClaudeTranscreationError`, `is_outage_error`, `is_per_article_error`. System prompt = `ux-guidelines.md` body verbatim + JSON envelope appended after a horizontal rule (Decision 6); flat-path fallback in `_load_prompt` with cache keyed by `(path, mtime, body)` so subdir-miss → flat-hit doesn't return stale content (round-1 fix). Output validation enforces paragraph-count match against input length and applies a defensive 4000-char per-paragraph truncation with warning log (Decision 13). Emoji-prefix safety net mirrors the legacy `transcreate_text(is_title=True)` cascade in `news_bot.py:404-423` byte-for-byte. `_classify_exception` covers all 9 SDK exception classes from Decision 5; `health_check` is non-raising and returns `False` on any failure (prompt missing, SDK init failure, network/auth error). The module-level singleton client is lazy-initialised on first real call so the module imports without `ANTHROPIC_API_KEY` (required for test collection and Decision 14 startup health checks).
**Deviations:** Reviewer cycle was performed inline by the claude-wrapper agent applying the `code-reviewing`, `security-auditor`, `test-master`, and `prompt-master` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Round 1 raised one medium finding (cache key — fixed) plus several info-level notes (no action). Round 2 clean.

**Reviews:**

*Round 1:*
- code-reviewer: 1 medium (C1 cache-key bug — fixed) + 2 info (no action) → [logs/working/task-03/code-reviewer-round1.json](logs/working/task-03/code-reviewer-round1.json)
- security-auditor: OK (5 info-level notes; ANTHROPIC_API_KEY redaction correctly delegated to Task 04) → [logs/working/task-03/security-auditor-round1.json](logs/working/task-03/security-auditor-round1.json)
- test-reviewer: OK (15 tests match TDD anchor; shared SDK-exception fixtures handle httpx.Response/Request signatures; no real network) → [logs/working/task-03/test-reviewer-round1.json](logs/working/task-03/test-reviewer-round1.json)
- prompt-reviewer: OK (system prompt verbatim + JSON envelope; user message structured-JSON; emoji cascade copied verbatim from legacy) → [logs/working/task-03/prompt-reviewer-round1.json](logs/working/task-03/prompt-reviewer-round1.json)

*Round 2 (after C1 fix):*
- code-reviewer: OK → [logs/working/task-03/code-reviewer-round2.json](logs/working/task-03/code-reviewer-round2.json)

**Verification:**
- `pytest tests/test_claude_transcreation.py -q` → 15 passed in 0.28s (happy path with token observability; 7 outage branches: RateLimit/Authentication/APIConnection/APITimeout/InternalServer/PermissionDenied/NotFound; 2 per-article branches: BadRequest/UnprocessableEntity; malformed-JSON; paragraph-count mismatch; emoji safety-net 2 cascade branches; 4000-char defensive truncation with warning; subdir → flat-path fallback).
- `python3 -c "import claude_transcreation; print(sorted(n for n in dir(claude_transcreation) if not n.startswith('_')))"` → public API present (ClaudeOutageError, ClaudeTranscreationError, health_check, is_outage_error, is_per_article_error, transcreate_via_claude).
- `python3 -c "import claude_transcreation; print(claude_transcreation.health_check())"` (no `ANTHROPIC_API_KEY` in env) → `False` (non-raising — Decision 14 contract holds).
- Real Claude smoke verification deferred to pre-deploy QA (Task 17) per task-03 constraint: ANTHROPIC_API_KEY not in local `.env`.

## Task 7: Refactor _fallback_publish for Claude primary + Google per-article fallback

**Status:** Done
**Commit:** 9d9e53b (feat), fd6f1ed (round-1 fix), 59e75c0 (review reports)
**Agent:** publish-refactor
**Summary:** Refactored step 1 of `_fallback_publish` to follow the dual-path translation contract from Decisions 1/5/9. Primary path: `transcreate_via_claude(row)` when `outage_state.is_fallback_active() == False`; per-article `ClaudeTranscreationError` falls back to `transcreate_text` (Google) for THAT row only with state machine NOT advanced; API-level `ClaudeOutageError` advances the state machine via `outage_state.record_outage_event(now=utc)`, dispatches admin pings, runs Steps 2-5 in degraded-mode Google, and re-raises `ClaudeOutageError` so the upstream `job()` loop (Task 8) can advance its slot-counter without a strike. Already-in-fallback shortcut routes straight to Google. Steps 2-5 (Telegraph publish, persist URL via `mark_telegraph_published`, Telegram teaser, `move_to_published(via_review=False)`, preview cleanup) untouched per Decision 9 idempotency. Both engines share the uniform `↳ автоперевод` Telegraph marker via the existing `auto_marker=not via_review` kwarg. Per-fixture `transcreate_via_claude` patches added to `_IdleFallbackCase` / `_ThrottleCase` / `_OverflowCase` so legacy Google-only assertions still hold via the per-article fallback branch.
**Deviations:** Reviewer cycle was performed inline by the publish-refactor agent applying the `code-reviewing`, `security-auditor`, and `test-master` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Task file `decisions.md`-snippet template requires the inline-review note pattern.

**Reviews:**

*Round 1:*
- code-reviewer: approved-with-minor-suggestions (CR-1 datetime/timezone hoist applied; CR-2/CR-3/CR-4 deferred or info) → [logs/working/task-07/code-reviewer-round1.json](logs/working/task-07/code-reviewer-round1.json)
- security-auditor: approved (4 info-level notes — log discipline, admin-ping fixed strings, BEGIN IMMEDIATE concurrency, defensive .get patterns) → [logs/working/task-07/security-auditor-round1.json](logs/working/task-07/security-auditor-round1.json)
- test-reviewer: approved-with-minor-suggestions (TR-1 parameterized blocks deferred; TR-2 not applicable on inspection; TR-3/TR-4 strengths) → [logs/working/task-07/test-reviewer-round1.json](logs/working/task-07/test-reviewer-round1.json)

*Round 2 (after fix):*
- code-reviewer: approved → [logs/working/task-07/code-reviewer-round2.json](logs/working/task-07/code-reviewer-round2.json)
- security-auditor: approved → [logs/working/task-07/security-auditor-round2.json](logs/working/task-07/security-auditor-round2.json)
- test-reviewer: approved → [logs/working/task-07/test-reviewer-round2.json](logs/working/task-07/test-reviewer-round2.json)

**Verification:**
- `pytest tests/test_fallback_publish_paths.py -q` → 4 passed (Claude success; ClaudeTranscreationError per-article Google fallback; already-in-fallback shortcut; ClaudeOutageError degraded-publish + state-advance + admin-ping + re-raise).
- `pytest tests/test_idle_fallback.py tests/test_fallback_throttle.py tests/test_overflow.py tests/test_fallback_publish_paths.py -q` → 38 passed (legacy idle-fallback / throttle / overflow tests still green via per-fixture `ClaudeTranscreationError` injection).
- `pytest tests/test_claude_transcreation.py tests/test_outage_state.py tests/test_telegram.py tests/test_integration.py tests/test_job_prep_phase.py -q` → 92 passed in broader smoke covering Wave 2 dependencies + downstream send paths.
- Smoke verification (mocked, no real Anthropic API per Constraints — `ANTHROPIC_API_KEY` not in `.env`): all branches asserted via unittest.mock fixtures in `tests/test_fallback_publish_paths.py`.

## Task 8: Refactor `job()` for distributed-publish loop + cron change

**Status:** Done
**Commit:** 0c6d81d (feat), 645d96c (round-1 fix), 9008e8c (review reports)
**Agent:** scheduler-refactor
**Summary:** Replaced the manual-review-workflow prep-phase tick with the llm-transcreation distributed-publish flow per Decisions 2/4/9/14/15. `job()` now runs crash-loop guard (`MAX(published_at)` + 40-min wait) → fetch+filter+insert → `compute_publish_slots` → plan-of-day admin ping (suppressed on N=0; backlog warning at >50) → distributed-publish loop with window-end guard, `ClaudeOutageError`-aware slot advance (no strike), and standard 3-strikes flow on unexpected exceptions. `main()` adds Decision 14 startup health checks: `claude_transcreation.health_check()` (False → admin ping + `outage_state.set_fallback_active(True)`) and TZ env validation. Cron switched from `schedule.every(12).hours` to `schedule.every().day.at("12:00", tz=pytz.timezone("Europe/Moscow"))` — pytz only, since `schedule==1.2.1` rejects `zoneinfo.ZoneInfo`. New repo helper `get_max_published_at()` in `pending_articles_repo` backs the crash-loop guard. Idle-fallback / overdue-auto-publish / overflow-fast-track call sites removed from `job()` (the helper functions remain for Task 9 cleanup).
**Deviations:** Reviewer cycle was performed inline by the scheduler-refactor agent applying the `code-reviewing` and `test-master` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Both reviewers raised minor findings on round 1 (2 + 2); all applied; round 2 clean. Existing legacy test files (`test_idle_fallback.py`, `test_overflow.py`, `test_fallback_throttle.py`) are deferred to Task 11 per tech-spec Risks — they exercise the now-removed `job()` call sites and would hang on the new distributed-publish loop without further adaptation. Per task constraints we ran only Task 8's own test files plus `test_job_prep_phase.py` (which we touched).

**Reviews:**

*Round 1:*
- code-reviewer: 2 minor applied (CR-1 exception-class log on `_parse_published_at_utc`; CR-2 inline comment on degraded-mode `published_count` fold-in) + 3 info-level no-action → [logs/working/task-08/code-reviewer-round1.json](logs/working/task-08/code-reviewer-round1.json)
- test-reviewer: 2 minor applied (TR-1 expanded `test_three_strikes_moves_to_failed` to full end-to-end with 3 job() runs; TR-2 tightened `test_outage_active_routes_via_google` to assert Google-only called AND Claude-primary NOT called) + 3 info-level no-action → [logs/working/task-08/test-reviewer-round1.json](logs/working/task-08/test-reviewer-round1.json)

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-08/code-reviewer-round2.json](logs/working/task-08/code-reviewer-round2.json)
- test-reviewer: OK → [logs/working/task-08/test-reviewer-round2.json](logs/working/task-08/test-reviewer-round2.json)

**Verification:**
- `pytest tests/test_job_distributed_publish.py tests/test_job_prep_phase.py -q` → 28 passed (15 distributed-publish + 13 prep-phase).
- Smoke 1 — crash-loop guard unit: `pytest tests/test_job_distributed_publish.py::TestCrashLoopGuard::test_sleeps_when_last_published_recent` → PASSED (asserts `time.sleep` called with ~2100s ± 5% on `MAX(published_at) = now - 5min`).
- Smoke 2 — TZ-aware schedule: `python3 -c "import pytz, schedule; schedule.every().day.at('12:00', tz=pytz.timezone('Europe/Moscow')).do(lambda: None); print('OK')"` → `OK` (no `ScheduleValueError`). Regression on Decision 4.
- Smoke 3 — integration 3-publishes: `pytest tests/test_job_distributed_publish.py::TestDistributedPublishLoop::test_publishes_three_articles_at_expected_slots` → PASSED (3 mocked entries → 3 `_fallback_publish` calls with `via_review=False`).
- `pytest tests/test_pending_articles_repo.py tests/test_outage_state.py tests/test_compute_publish_slots.py tests/test_fallback_publish_paths.py -q` → 64 passed (Wave 1–3 dependencies green).

## Task 9: Delete legacy auto-publish code + env vars

**Status:** Done
**Commit:** 050b6eb (refactor)
**Agent:** cleaner
**Summary:** Pure deletion task — removes the legacy auto-publish machinery now superseded by the Task 08 distributed-publish loop: `_overflow_fast_track` helper (~270 LoC), four legacy env vars (`IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `QUEUE_CAP`, `FALLBACK_THROTTLE_SECONDS`), `tests/test_overflow.py`, `tests/test_idle_fallback.py`, and the throttle-related sections of `tests/test_fallback_throttle.py`. Doc-comments in `_fallback_publish` referencing removed helpers (`list_notified_overdue`) are rewritten to point at the new distributed-publish loop. `TestFallbackPublishPassesAutoMarkerToPublishArticle` is rewritten to drive `_fallback_publish` directly with a fixture row (the prior `job()` + `_age_notified` trigger no longer reaches the publish helper after Task 08). The auto_marker invariants are preserved verbatim. Net diff: −1762 LoC.
**Deviations:** Side-effect adjustment to four prep-phase integration tests (`test_feed_iteration`, `test_integration`, `test_mattel_integration`, `test_job_prep_phase`): the prior `FALLBACK_THROTTLE_SECONDS=0` patch is gone, so we add `news_bot.time.sleep` + `news_bot._fallback_publish` patches to keep the post-Task-08 distributed-publish loop from blocking these tests. Per task spec ("точечное «погасить красное»") this is minimal scaffolding; Wave 7 (Task 11) reshapes these test files properly.

**Reviews:**

*Round 1:*
- code-reviewer: approve, no findings → [logs/working/task-9/code-reviewer-1.json](logs/working/task-9/code-reviewer-1.json)

**Verification:**
- `pytest tests/ -q` → 548 passed in 8.58s.
- `pytest tests/test_fallback_throttle.py tests/test_job_distributed_publish.py tests/test_fallback_publish_paths.py -q` → 23 passed.
- `grep -nE 'FALLBACK_THROTTLE_SECONDS|QUEUE_CAP|IDLE_TIMEOUT_HOURS|GRACE_WINDOW_HOURS|_overflow_fast_track' news_bot.py tests/` → empty.
- `grep -nE 'list_pending_stale|list_notified_overdue|mark_notified' news_bot.py` → empty.
- `python3 -c "import news_bot; print('ok')"` → ok.

## Task 10: Strip bureaucratic regex + 4000-char truncation from `transcreate_text`

**Status:** Done
**Commit:** f9eee57 (refactor), 108af0b (review reports)
**Agent:** translator-trim
**Summary:** Pure-deletion refactor applying Decision 11. Removed the 19-pattern `bureaucratic` dict + apply loop, the 4 passive→active `re.sub` flips, and the 4000-char body truncation block from `transcreate_text` in `news_bot.py`. Kept the GoogleTranslator try/except, the 14-pattern `hw_glossary` post-pass, and the `is_title` content-aware emoji prefix. Docstring rewritten to point at Decision 11 and clarify the function's surviving role as a Google-Translate fallback (per-article Claude failure or global outage). `tests/test_translation.py` extended with a new `TestTranscreateText` class (7 tests): 2 HW-glossary positive cases, 3 regression tests asserting the deleted bureaucratic / passive-flip / truncation behavior is gone, 1 title emoji-prefix path, 1 GoogleTranslator-error fallback. TDD: tests written first, 3 failed against the legacy code, all 12 passed after the deletion.
**Deviations:** Reviewer cycle was performed inline by the translator-trim agent applying the `code-reviewing` and `test-master` skills to the diff directly, because the Agent/Task subagent tool is not exposed in this execution environment. Both reviewers approved with zero findings on round 1.

**Reviews:**

*Round 1:*
- code-reviewer: approved, no findings → [logs/working/task-10/code-reviewer-round1.json](logs/working/task-10/code-reviewer-round1.json)
- test-reviewer: approved, no findings → [logs/working/task-10/test-reviewer-round1.json](logs/working/task-10/test-reviewer-round1.json)

**Verification:**
- `pytest tests/test_translation.py -v` → 12 passed in 0.65s (5 pre-existing `TestTranslateText` + 7 new `TestTranscreateText`).
- `pytest tests/ -q` → 555 passed in 9.22s.
- `grep -n "bureaucratic\|был выполн\|был представлен\|было объявлено\|был запущен" news_bot.py` → only the docstring's Decision 11 reference (`bureaucratic-regex cleanup … were removed`).
- `grep -n "if len(result) > 4000" news_bot.py` → empty.

## Task 11: Update integration tests for the new auto-publish path

**Status:** Done
**Commit:** 7216d95 (test), 7cbb72a (round-1 fix), 6a92ab7 (review reports)
**Agent:** integration-update
**Summary:** Synchronised `tests/test_integration.py` and `tests/test_job_prep_phase.py` with Wave 3–6: switched publish-branch patch targets from `news_bot.transcreate_text` to `news_bot.transcreate_via_claude` (pinned per Task 7 import), added `TestOutageStateIntegration` (AC14/15/16/17 — read state via `outage_state` public getters, never raw SQL on `bot_state`), `TestRestartMidWindow` (AC7), `TestManualReviewPreemption` (AC21), `TestCrashLoopGuard` (AC8), and replaced `TestCronScheduleTwelveHourly` with `TestCronScheduleDailyAtNoonMSK`. Fixed a pre-existing bug in the restart-mid-window test where the crash-loop guard's `datetime.strptime` call returned a `MagicMock` (the `news_bot.datetime` patch swallowed it) — added `mock_dt.strptime` passthrough alongside `combine`. Replaced deprecated `datetime.utcnow()` calls with the explicit UTC-naive form to clean up DeprecationWarnings.
**Deviations:** Reviewer cycle performed inline by this agent applying `code-reviewing` and `test-master` skills directly, because the Agent/Task subagent tool is not exposed in this execution environment. Both reviewers approved on round 1 with minor findings only; round-1 fix applied 3 of 6 (CR-1 dead mock attrs, TR-2 verbatim ping assertion, TR-3 notify_patcher dance comment); CR-2 / CR-3 / TR-1 logged as no-action with reasoning in the JSON reports. Round 2 both approve, zero new findings.

**Reviews:**

*Round 1:*
- code-reviewer: 3 minor findings (CR-1 apply, CR-2 skip, CR-3 skip) → [logs/working/task-11/code-reviewer-round1.json](logs/working/task-11/code-reviewer-round1.json)
- test-reviewer: 3 minor findings (TR-1 skip, TR-2 apply, TR-3 apply) → [logs/working/task-11/test-reviewer-round1.json](logs/working/task-11/test-reviewer-round1.json)

*Round 2 (after fixes):*
- code-reviewer: approve, no findings → [logs/working/task-11/code-reviewer-round2.json](logs/working/task-11/code-reviewer-round2.json)
- test-reviewer: approve, no findings → [logs/working/task-11/test-reviewer-round2.json](logs/working/task-11/test-reviewer-round2.json)

**Verification:**
- `pytest tests/test_integration.py tests/test_job_prep_phase.py -q` → 21 passed in 0.87s.
- `pytest tests/ -q` → 566 passed in 5.47s (no regressions across the full suite).
- `grep -nE "FALLBACK_THROTTLE_SECONDS|_overflow_fast_track|QUEUE_CAP|IDLE_TIMEOUT_HOURS|GRACE_WINDOW_HOURS" tests/test_integration.py tests/test_job_prep_phase.py` → empty (no leftover refs to deleted symbols).

## Task 12: Create tests/test_distributed_schedule_integration.py

**Status:** Done
**Commit:** 682d833 (test), ba04acc (round-1 fix)
**Agent:** schedule-integ
**Summary:** New integration test file with 4 end-to-end scenarios exercising the post-Wave-1–6 distributed-publish flow: full happy path (3 articles → 3 slots → 3 Claude publishes), API-level outage on slot 2 (ping #1 + Google fallback for that one article + state-machine advance), container restart mid-window with crash-loop guard (5 pre-seeded pending + 1 already-published, time frozen at 16:00 МСК), manual-review preemption (operator publishes one row mid-loop via `update_staged` + `move_to_published`). All 4 scenarios use `freezegun.freeze_time` for deterministic `now`, mock the Claude SDK at the pinned `news_bot.transcreate_via_claude` bound name, and verify slot timing against `compute_publish_slots` rather than hard-coded values per task constraint. Coverage: user-spec AC1–AC8, AC14–AC17 (AC17 implicit), AC21.
**Deviations:** Reviewer cycle performed inline by this agent applying `code-reviewing` and `test-master` skills directly because the Agent/Task subagent tool is not exposed in this execution environment. Round-1 produced two minor `apply` findings: T7 — added a `mock_teaser.call_count == 3` assertion in scenario 1 to pin the channel-side handoff; T4 — added a negative assertion that no error-shaped admin ping fires during the manual-review-preemption scenario. Both fixes are 8 lines total, all 4 tests still pass, full suite remains 566 passed.

**Reviews:**

*Round 1:*
- code-reviewer: 7 minor findings (all `skip`) → [logs/working/task-12/code-reviewer-round1.json](logs/working/task-12/code-reviewer-round1.json)
- test-reviewer: 8 findings (2 `apply` — T4, T7; 6 `skip`) → [logs/working/task-12/test-reviewer-round1.json](logs/working/task-12/test-reviewer-round1.json)

*Round 2 (after fixes):*
- code-reviewer: approve, no findings → [logs/working/task-12/code-reviewer-round2.json](logs/working/task-12/code-reviewer-round2.json)
- test-reviewer: approve, no findings → [logs/working/task-12/test-reviewer-round2.json](logs/working/task-12/test-reviewer-round2.json)

**Verification:**
- `pytest tests/test_distributed_schedule_integration.py -v` → 4 passed in 0.62s (test_full_happy_path_three_articles_three_slots_three_publishes, test_outage_mid_day_advances_state_and_recovers_on_next_slot, test_container_restart_mid_window_recomputes_slots_and_continues, test_manual_review_preemption_skips_locally_published_row).
- `pytest tests/ -q` → 566 passed in 6.15s (no regressions from the new file).
- `python3 -c "import tests.test_distributed_schedule_integration"` → imports clean (no syntax errors / missing deps).


## Task 13: Update deploy bundle (deploy.sh + GitHub Actions + PK docs)

**Status:** Done
**Commit:** f50fac8 (chore), 74fffdf (review reports)
**Agent:** deployer
**Summary:** Extended `deploy.sh` and `.github/workflows/deploy.yml` FILES arrays from 9 → 13 entries, adding the four files the llm-transcreation feature needs at runtime: three Python modules (`claude_transcreation.py`, `compute_publish_slots.py`, `outage_state.py` — without any of them `news_bot` crashes with `ImportError` on cron startup) and `ux-guidelines.md` (Claude API system prompt; lands flat at `$DEPLOY_PATH/ux-guidelines.md` due to scp's subdir-flattening — covered by Decision 8 fallback in `_load_prompt`). Added a new `Write runtime env vars to server .env` step that idempotently writes `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` / `TZ` to the server `.env` via ssh+heredoc on stdin (no secrets on command lines, GitHub Actions auto-redacts `env:` mappings in workflow logs). Updated `Verify required secrets` to fail-fast when `ANTHROPIC_API_KEY` is unset; `ANTHROPIC_MODEL` and `TZ` use documented defaults via repo `vars`. Refreshed stale "hourly cron path" / "next 12h tick" comments to "daily 12:00 МСК cron path" / "next 12:00 МСК cron tick" in both deploy files. Updated all three PK references (architecture.md captures the architectural shift for ux-guidelines.md closing AC28, the three new cron-side runtime modules, the new `bot_state` table, Anthropic Claude API as external integration, anthropic+pytz deps, daily distributed-publish data flow, and removes the delivered "LLM-powered transcreation fallback" from Planned Enhancements; patterns.md rewrites Transcreation/Channel-post-format/Scheduling, deletes Auto-fallback-throttle and Overflow-fast-track sections, adds Auto-publish-path section with state machine + classification + output validation + token redaction, refreshes Logging and Test Infrastructure; deployment.md adds ANTHROPIC_API_KEY/TZ as required and ANTHROPIC_MODEL as optional, drops legacy QUEUE_CAP/IDLE_TIMEOUT_HOURS/GRACE_WINDOW_HOURS/FALLBACK_THROTTLE_SECONDS, documents GitHub Secrets/Variables setup, rewrites Scheduling for daily 12:00 МСК + 13:00–20:00 МСК window, adds Cost Monitoring section with Anthropic console URL and a sanity threshold).
**Deviations:** Reviewer cycle performed inline by this agent applying `code-reviewing`, `security-auditor`, and `deploy-pipeline` skills to the diff directly because the Agent/Task subagent tool is not exposed in this execution environment. Round 1 produced no critical or apply findings — code-reviewer flagged two informational notes (single-quote inlining in heredoc is theoretically brittle if Anthropic ever changes key shape; YAML block-scalar EOF terminator is robust today but sensitive to future indent edits — both deferred), security-auditor approved with minor notes (same single-quote forward-looking observation), deploy-reviewer approved without findings. No round 2 needed.

**Reviews:**

*Round 1:*
- code-reviewer: approve_with_minor_notes (2 info notes) → [logs/working/task-13/code-reviewer-round1.json](logs/working/task-13/code-reviewer-round1.json)
- security-auditor: approve_with_minor_notes (1 low + 5 info) → [logs/working/task-13/security-auditor-round1.json](logs/working/task-13/security-auditor-round1.json)
- deploy-reviewer: approve (7 info confirmations) → [logs/working/task-13/deploy-reviewer-round1.json](logs/working/task-13/deploy-reviewer-round1.json)

**Verification:**
- `bash -n deploy.sh` → clean.
- `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"` → clean.
- Smoke step 2 (FILES list integrity + scp-flatten emulation): all 13 elements present in repo; `cp "${FILES[@]}" /tmp/hwbot-staging/` lands `claude_transcreation.py`, `compute_publish_slots.py`, `outage_state.py`, `ux-guidelines.md` flat in the staging dir.
- Smoke step 3 (env-write idempotency): two consecutive runs of the env-write logic against a seeded `.env` keep each managed key (`ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `TZ`) at exactly one occurrence, preserve unrelated keys (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAPH_ACCESS_TOKEN`) byte-for-byte, mode 0600 on `.env`.
- Smoke step 7 (stale cron-comment): `grep -nE 'hourly|12h' deploy.sh .github/workflows/deploy.yml` → empty.
- Doc-grep for legacy symbols (`_overflow_fast_track`, `FALLBACK_THROTTLE_SECONDS`, `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `idle-fallback`) on the three PK files → only two intentional "removed in feature X" historical notes in patterns.md remain (allowed per AC).
- `pytest tests/ -q` → 566 passed in 5.30s (matches the ≥ 566 baseline).
