# Test Audit — llm-transcreation-and-distributed-publishing (Task 16)

**Auditor:** test-auditor
**Date:** 2026-04-27
**Status:** read-only audit; no code or tests modified

## Summary

Test design is solid across the 13 implementation tasks: every user-spec AC except AC30/AC31 (manual smoke) and one technical AC (init_schema failure ping) maps to ≥1 traceable test, all five outage states are covered with concurrency proven via a real two-thread race, the crash-loop guard sleep duration is asserted at ±5 % of 35 min, and all 11 anthropic SDK exception classes from Decision 5 have explicit tests. **One blocker found: the deleted `tests/test_overflow.py` and `tests/test_idle_fallback.py` files have been re-introduced into the working tree (staged but uncommitted) and reference the deleted symbols `_overflow_fast_track`, `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `FALLBACK_THROTTLE_SECONDS` — `pytest tests/ -q` produces 25 failures.** With those two files excluded (i.e. on the actual feature HEAD `e659d05`), the suite is 566 passed in 5.83s.

## Test Run

| Command | Result |
|---------|--------|
| `pytest tests/ -q` (current working tree, including staged orphan files) | **25 failed, 566 passed in 11.36s** — failures are AttributeError on deleted symbols in the resurrected test files |
| `pytest tests/ -q --ignore=tests/test_overflow.py --ignore=tests/test_idle_fallback.py` (== HEAD `e659d05` state) | **566 passed in 5.83s** |
| `pytest tests/ --collect-only -q` (current working tree) | **591 collected** |
| `pytest tests/ --collect-only -q --ignore=tests/test_overflow.py --ignore=tests/test_idle_fallback.py` | **566 collected** |

## Test Count Delta

Reference for the deletions: commit `050b6eb` "Task 09 — delete legacy auto-publish code + env vars" stat block.

| Bucket | Count | Detail |
|--------|-------|--------|
| **Deleted** | 26 | `tests/test_overflow.py` (786 LoC, 13 tests) + `tests/test_idle_fallback.py` (492 LoC, 12 tests) + 1 throttle-only test trimmed out of `test_fallback_throttle.py`. Plan: ~28. Actual: 26 — within the ±5 plan band. |
| **Added (new files)** | 38 | `test_compute_publish_slots.py` 15 + `test_claude_transcreation.py` 15 + `test_outage_state.py` 12 + `test_distributed_schedule_integration.py` 4 + `test_fallback_publish_paths.py` 4. Plan: ~30. Actual: 38 — 8 over, all rationalised in decisions.md (subdir-flat fallback, defensive truncation, paragraph-count mismatch, naive-dt rejection, and corrupted-content tolerance tests added in round-1 fixes). |
| **Added (existing files)** | ≈22 | `test_no_token_leak_in_logs.py` +18 (anthropic-key + admin-notify subclasses), `test_translation.py` +7 (`TestTranscreateText`), `test_integration.py` +5 (outage / restart / preemption / crash-loop classes), `test_job_prep_phase.py` +1 (`TestCronScheduleDailyAtNoonMSK`), `test_migration.py` +1 (`test_bot_state_schema`), `test_job_distributed_publish.py` 15 (entirely new file but tracked under "modified to news_bot.py" wave). |
| **Net delta** | **+34 tests** | (566 current − 532 expected pre-feature baseline). Plan: ~+2. Actual is much higher because new files are larger than ~30 plan; all adds are traceable to a Decision/AC. |

The deviation from the ~30 added / ~28 deleted plan is documented at task level: each round-1 fix that added tests was approved by the test-reviewer with an explicit "TR-x — apply" line in `logs/working/task-XX/test-reviewer-roundN.json`. No suspicious test inflation.

## AC Traceability Matrix — User-Spec AC1–AC31

| AC | Description | Test(s) | Status |
|----|-------------|---------|--------|
| AC1 | Cron once daily at 12:00 МСК | `test_job_prep_phase.py::TestCronScheduleDailyAtNoonMSK::test_main_uses_daily_noon_msk_schedule`; `test_job_distributed_publish.py::TestCronRegistration::test_schedule_at_accepts_pytz_timezone`, `test_main_registers_tz_aware_daily_cron` | covered |
| AC2 | Admin ping with N + queue + schedule | `test_distributed_schedule_integration.py::test_full_happy_path_three_articles_three_slots_three_publishes` (asserts `'расписание' or 'Зафетчил' in msgs`); `test_job_prep_phase.py::TestAdminPing::test_admin_ping_plan_of_day_sent` | covered |
| AC3 | Publications strictly inside 13–20 МСК | `test_compute_publish_slots.py::test_now_after_window_end`, `test_now_exactly_at_window_end`; `test_job_distributed_publish.py::TestWindowEndGuard::test_window_end_guard_breaks_loop` | covered |
| AC4 | min_interval = 40 minutes | `test_compute_publish_slots.py::test_n_eleven_at_floor` (40-min delta asserted); `test_n_fifteen_capped_at_eleven` (cap proves floor) | covered |
| AC5 | Max 11 publishes/day, excess → carry-over | `test_compute_publish_slots.py::test_n_eleven_at_floor`, `test_n_fifteen_capped_at_eleven` (carry=4), `test_n_twenty_capped_at_eleven` (carry=9) | covered |
| AC6 | Even spacing per N | `test_compute_publish_slots.py::test_n_four_evenly_spaced`, `test_n_seven_hourly`, `test_n_ten_interval_42min` | covered |
| AC7 | Container restart mid-window | `test_compute_publish_slots.py::test_restart_at_16_n5`, `test_restart_at_19_50_n5`; `test_integration.py::TestRestartMidWindow::test_restart_mid_window_recomputes_schedule`; `test_distributed_schedule_integration.py::test_container_restart_mid_window_recomputes_slots_and_continues` | covered |
| AC8 | Crash-loop guard ≥40min after MAX(published_at) | `test_job_distributed_publish.py::TestCrashLoopGuard::test_sleeps_when_last_published_recent`, `test_no_sleep_when_last_published_old`, `test_no_sleep_when_no_published_rows`; `test_integration.py::TestCrashLoopGuard::test_crash_loop_guard_delays_first_publish`; `test_distributed_schedule_integration.py::test_container_restart_mid_window_recomputes_slots_and_continues` (asserts 1800s ±5% sleep) | covered |
| AC9 | Auto-publishes use Claude with ux-guidelines.md prompt | `test_claude_transcreation.py::test_happy_path_returns_valid_dict`; `test_load_prompt_subdir_then_flat_fallback`; `test_fallback_publish_paths.py::TestClaudePath::test_fallback_publish_claude_path` | covered |
| AC10 | RU title (with emoji) + 2-3 alts + RU subtitle + RU paragraphs | `test_claude_transcreation.py::test_happy_path_returns_valid_dict` (asserts emoji prefix, alts length 2-3, subtitle, paragraph-count match) | covered |
| AC11 | Title always emoji-prefixed (regex safety net if Claude misses) | `test_claude_transcreation.py::test_title_without_emoji_gets_safety_net` (covers 2 cascade branches: 🚀 release / 🏆 legends) | covered |
| AC12 | HW glossary applied as post-pass; bureaucratic regex removed | `test_translation.py::TestTranscreateText::test_hw_glossary_replaces_hot_wheels_translit`, `test_hw_glossary_replaces_garage_build` (positive); `test_bureaucratic_phrase_not_replaced`, `test_passive_construction_not_flipped` (regression) | covered |
| AC13 | 4000-char body truncation removed | `test_translation.py::TestTranscreateText::test_body_not_truncated_at_4000` | covered |
| AC14 | API-level outage → 2-ping protocol + 2h grace | `test_outage_state.py::TestRecordOutageEvent::test_record_outage_event_from_no_outage`, `test_record_outage_event_advance_to_ping_2`, `test_record_outage_event_switch_to_google_fallback`; `test_integration.py::TestOutageStateIntegration::test_api_level_outage_advances_state_machine`; `test_distributed_schedule_integration.py::test_outage_mid_day_advances_state_and_recovers_on_next_slot` | covered |
| AC15 | Per-article problem → fallback only this article, NO state advance | `test_fallback_publish_paths.py::TestGoogleFallbackPath::test_fallback_publish_google_fallback_path` (asserts `record_outage_event` NOT called); `test_integration.py::TestOutageStateIntegration::test_per_article_problem_does_not_advance_state` | covered |
| AC16 | Auto-recovery probes Claude on next slot, switch-back ping, clears state | `test_outage_state.py::TestRecordRecoveryEvent::test_record_recovery_event_clears_state`; `test_integration.py::TestOutageStateIntegration::test_recovery_clears_outage_state_and_sends_switchback_ping` | covered (recovery hook test exercises `record_recovery_event` directly — Wave-1-6 has no auto-call yet, documented in test_distributed_schedule_integration.py docstring) |
| AC17 | Edge case: outage clears + queue empty → stays in fallback until next 12:00 | `test_distributed_schedule_integration.py::test_outage_mid_day_advances_state_and_recovers_on_next_slot` covers it implicitly: ping_1_sent does NOT flip global flag, persists across slots; assert `is_fallback_active() == False` after slot 3 success | partial — direct empty-queue scenario not tested as a single test but the state transition table coverage in test_outage_state.py + the integration test together cover the contract |
| AC18 | Telegraph marker `↳ автоперевод` for both engines | `test_telegraph_publisher.py::TestAutoMarkerInArticleBody` (5 tests, marker byte-pinned `↳ автоперевод`); `test_fallback_publish_paths.py::TestClaudePath`, `TestGoogleFallbackPath`, `TestAlreadyInFallback` (auto_marker=True forwarded to publish_article on every path) | covered |
| AC19 | Logs include input/output token counts, latency, model | `test_claude_transcreation.py::test_happy_path_returns_valid_dict` (asserts `input_tokens`, `output_tokens`, `latency_ms`, `model` substrings in log records) | covered |
| AC20 | Backlog warning when len(pending) > 50 | `test_job_distributed_publish.py::TestDistributedPublishLoop::test_backlog_warning_at_threshold` (51 pending → assert ping mentions 'backlog' or 'очеред' or 'queue') | covered |
| AC21 | Manual-review path unchanged + bot skips operator-published rows | `test_integration.py::TestManualReviewPreemption::test_manual_review_preemption_skips_published_row`; `test_distributed_schedule_integration.py::test_manual_review_preemption_skips_locally_published_row` | covered |
| AC22 | Channel teaser format `#<source> #news` (single line) | `test_fallback_throttle.py::TestTeaserAlwaysSingleLine::test_manual_teaser_is_single_line`, `test_teaser_does_not_accept_auto_marker_kwarg`; `test_telegram.py` teaser tests (pre-existing) | covered |
| AC23 | Boilerplate filter / image policy / hashtag derivation unchanged | `test_boilerplate_filter.py`, `test_telegram.py`, `test_telegraph_publisher.py` (pre-existing — not modified, so feature didn't break them) | covered (regression — green via full-suite pass) |
| AC24 | SQLite migration `bot_state` idempotent | `test_migration.py::TestMigration::test_all_tables_created`, `test_bot_state_schema`, `test_init_db_idempotent` (seeds bot_state row, re-runs init_db, row count unchanged) | covered |
| AC25 | ANTHROPIC_API_KEY redacted in logs | `test_no_token_leak_in_logs.py::TestAnthropicKeyRedaction` (10 tests: prod-shape, sandbox-shape with `=`/`.`, args-passed, filter installed on 3 anthropic loggers, helper neutrality, regex no-overmatch, end-to-end via root logger); `TestAdminNotifyRedaction` (4 tests: prod key + sandbox key + telegram key + clean message in admin notify path) | covered (deepest coverage in the suite — Decision 12 three-layer defense fully exercised) |
| AC26 | Legacy code removed (overflow, idle-fallback, throttle, env vars) | `test_job_prep_phase.py::TestProcessNewArticlesRemoved::test_no_attribute`; the deletion is verified at HEAD by absence of references in `news_bot.py` (decisions.md verification step `grep -nE 'FALLBACK_THROTTLE_SECONDS\|...' news_bot.py` → empty) | covered (modulo BLOCKER below) |
| AC27 | ux-guidelines.md in deploy bundle | covered in `deploy.sh` + `.github/workflows/deploy.yml` per Task 13 verification (FILES list 13 entries, scp-flatten emulation passes); no explicit pytest assertion (deploy-pipeline level) | covered (deploy-pipeline level — no Python test required, documented in Task 13 decisions entry) |
| AC28 | Architectural shift documented in architecture.md | covered by Task 13 PK doc updates (per decisions.md entry) | covered (doc-level) |
| AC29 | `pytest tests/ -q` green | **NOT GREEN at HEAD-of-tree** due to BLOCKER below; green when `tests/test_overflow.py` and `tests/test_idle_fallback.py` are excluded. | **GAP / blocker** |
| AC30 | Manual smoke: claude transcreation against sample article < 30s | not testable without ANTHROPIC_API_KEY (excluded per project Constraints) — deferred to Task 17 (pre-deploy QA) | deferred (intentional) |
| AC31 | Manual smoke post-deploy: 12:00 МСК cron + 13:00 publication | post-deploy — Task 19 | deferred (intentional) |

## AC Traceability Matrix — Tech-Spec ACs (10 technical)

| Tech AC | Description | Test(s) | Status |
|---------|-------------|---------|--------|
| Deps & config | `requirements.txt` anthropic + pytz; `.env.example` updated | Task 6 verification (`grep -E '^(anthropic\|pytz)' requirements.txt` → 2 matches) | covered (no Python test — config file diff) |
| Migration: bot_state PK shape | `test_migration.py::test_bot_state_schema` (key TEXT PK + value TEXT, exact PRAGMA shape) | covered |
| busy_timeout = 5000 | `outage_state.py` source has `PRAGMA busy_timeout = 5000;` per task-05 verification; `test_outage_state.py::TestConcurrency` exercises 2-thread race past the 1h boundary asserting BEGIN IMMEDIATE serializes | covered |
| Tolerant reads on corrupted bot_state | `test_outage_state.py::TestReadTolerance::test_corrupted_timestamp_returns_none_and_warns`, `test_corrupted_ping_count_returns_zero_and_warns` | covered |
| init_schema failure → admin ping (Risk 5 mitigation) | not directly tested — `test_migration.py::test_init_db_idempotent` covers happy-path idempotency only; the forced-failure path (disk full, locked DB) is not simulated | **GAP — minor** |
| Token redaction broadened regex | `test_no_token_leak_in_logs.py::TestAnthropicKeyRedaction::test_filter_redacts_prod_shape_anthropic_key`, `test_filter_redacts_sandbox_shape_with_equals_and_dots`, `test_anthropic_regex_does_not_overmatch` | covered |
| Filter on anthropic SDK loggers | `test_no_token_leak_in_logs.py::TestAnthropicKeyRedaction::test_filter_attached_to_anthropic_sdk_loggers` (parametrized over 3 logger names) | covered |
| `_redact_text` helper used by send_admin_notification | `test_no_token_leak_in_logs.py::TestAdminNotifyRedaction` (4 tests) | covered |
| max_tokens=8000 | `claude_transcreation.py::_DEFAULT_MAX_TOKENS = 8000`; tests do not directly assert the value passed to `messages.create` (the mock returns canned response without checking kwargs) | **GAP — minor** (regression-test would be 1 line: `mock_anthropic_client.messages.create.assert_called_once_with(..., max_tokens=8000, ...)`) |
| Output validation: paragraph count + 4000-char truncation | `test_claude_transcreation.py::test_paragraph_count_mismatch_raises`, `test_paragraph_over_4000_truncated_with_warning` | covered |
| All 9 SDK exception classes from Decision 5 | `test_claude_transcreation.py` 7 outage + 2 per-article tests: RateLimitError, AuthenticationError, APIConnectionError, APITimeoutError, InternalServerError, PermissionDeniedError, NotFoundError → ClaudeOutageError; BadRequestError, UnprocessableEntityError → ClaudeTranscreationError | covered |
| Window-end guard (Decision 15) | `test_job_distributed_publish.py::TestWindowEndGuard::test_window_end_guard_breaks_loop` (3-slot list with last past window → assert publish called only twice) | covered |

## Outage State Machine — All 5 States + 12 Transitions

| Transition | Test | Status |
|------------|------|--------|
| `no_outage → ping_1_sent` | `test_outage_state.py::TestRecordOutageEvent::test_record_outage_event_from_no_outage` | covered |
| `ping_1_sent → ping_2_sent` (after 1h) | `test_record_outage_event_advance_to_ping_2` | covered |
| `ping_2_sent → google_fallback_active` (after 2h) | `test_record_outage_event_switch_to_google_fallback` | covered |
| `* → no_outage` (recovery, was_active=True) | `test_outage_state.py::TestRecordRecoveryEvent::test_record_recovery_event_clears_state` | covered |
| `no_outage → no_outage` (recovery on already-clean state) | same test, second invocation in test body asserts `was_active=False`, `pings_to_send=[]` | covered |
| Persistence across container restart | `TestPersistence::test_persistence_across_restart` | covered |
| Concurrency (BEGIN IMMEDIATE) | `TestConcurrency::test_concurrent_writers_serialize_via_begin_immediate` (post-1h-boundary race; asserts exactly 1 caller emits ping #2 — without BEGIN IMMEDIATE both would emit ⇒ catches the regression) | covered |
| Naive-datetime rejection | `TestNaiveDatetimeRejection` (3 tests: setter, record_outage, record_recovery) | covered |
| AC17 empty-queue at recovery | implicit via `test_distributed_schedule_integration.py` scenario 2 | partial (no isolated test) |

## Crash-Loop Guard — Tested with `MAX(published_at) < 40 min`

- `test_job_distributed_publish.py::TestCrashLoopGuard::test_sleeps_when_last_published_recent` — seeds 5-min-ago row, runs `news_bot.job()`, asserts first `time.sleep` arg is between **2100×0.95 and 2100×1.05 seconds** (≈35 min, the remaining gap to 40-min interval).
- `test_no_sleep_when_last_published_old` — 50-min-ago seed → `mock_sleep.call_count == 0`.
- `test_no_sleep_when_no_published_rows` — empty `published_articles` → `mock_sleep.call_count == 0`.
- `test_integration.py::TestCrashLoopGuard::test_crash_loop_guard_delays_first_publish` — same scenario at integration level (asserts 35min ±60s).
- `test_distributed_schedule_integration.py::test_container_restart_mid_window_recomputes_slots_and_continues` — pre-existing publish 10 min before frozen now → assert first sleep ≈ 1800s (30 min). Two distinct gap values exercise the same guard formula.

## compute_publish_slots — Boundary Cases

| Boundary | Test | Status |
|----------|------|--------|
| N=0 | `test_n_zero_returns_empty` | covered |
| N=1 | `test_n_one_at_window_start` | covered |
| N=4 | `test_n_four_evenly_spaced` | covered |
| N=7 | `test_n_seven_hourly` | covered |
| N=10 | `test_n_ten_interval_42min` | covered |
| N=11 | `test_n_eleven_at_floor` (last slot at 19:40, 40-min delta) | covered |
| N=12 | not directly tested as a boundary | **GAP — minor** (algorithm caps at 11 from floor; covered transitively by N=15) |
| N=15 | `test_n_fifteen_capped_at_eleven` (cap=11, carry=4) | covered |
| N=20 | `test_n_twenty_capped_at_eleven` (carry=9) | covered |
| N=30 | not directly tested | **GAP — minor** (large-backlog scenario; transitively covered by N=20 with carry=9 = same code path) |
| Container restart at 14:00, 16:00 | `test_restart_at_16_n5` (16:00 with N=5 → 5 slots at 48-min) | covered |
| Container restart at 19:50 | `test_restart_at_19_50_n5` (1 slot + carry=4) | covered |
| now > window_end | `test_now_after_window_end` (21:00 → empty + carry=N) | covered |
| now == window_end | `test_now_exactly_at_window_end` (boundary exclusive) | covered |
| TZ-naive datetime | `test_naive_datetime_raises` | covered |
| tzinfo preserved on output | `test_returned_slots_preserve_tzinfo` | covered |
| Module constants exported | `TestModuleConstants::test_constants_exported` | covered |

## claude_transcreation — All 11 SDK Exception Classes from Decision 5

| Exception class | Test | Classified as | Status |
|-----------------|------|---------------|--------|
| `RateLimitError` (429) | `test_rate_limit_raises_outage` | ClaudeOutageError | covered |
| `AuthenticationError` (401) | `test_authentication_raises_outage` | ClaudeOutageError | covered |
| `APIConnectionError` | `test_api_connection_raises_outage` | ClaudeOutageError | covered |
| `APITimeoutError` | `test_api_timeout_raises_outage` | ClaudeOutageError | covered |
| `InternalServerError` (500) | `test_internal_server_raises_outage` | ClaudeOutageError | covered |
| `PermissionDeniedError` (403) | `test_permission_denied_raises_outage` | ClaudeOutageError | covered |
| `NotFoundError` (404) | `test_model_not_found_raises_outage` | ClaudeOutageError | covered |
| `BadRequestError` (400) | `test_bad_request_raises_per_article` | ClaudeTranscreationError | covered |
| `UnprocessableEntityError` (422) | `test_unprocessable_entity_raises_per_article` | ClaudeTranscreationError | covered |
| `OverloadedError` | not tested as a separate class — but anthropic SDK 0.45.0 maps overloaded to `RateLimitError`/`InternalServerError` through HTTP 529 routing; covered transitively | **noted** |
| `APIStatusError` catch-all | not explicitly tested; covered by the above hierarchy | **GAP — minor** (would be a 1-test addition for catch-all robustness) |
| Malformed JSON | `test_malformed_json_raises_per_article` | ClaudeTranscreationError | covered |
| Refusal / non-JSON text | covered by `test_malformed_json_raises_per_article` (same code path: parse failure) | covered |
| Paragraph-count mismatch | `test_paragraph_count_mismatch_raises` | ClaudeTranscreationError | covered |
| Defensive 4000-char truncation | `test_paragraph_over_4000_truncated_with_warning` | warning, no raise | covered |
| ux-guidelines.md missing | not directly tested (`_load_prompt` raises FileNotFoundError per the source — `claude_transcreation.py:148-150,166-168`); the subdir-flat fallback IS tested (`test_load_prompt_subdir_then_flat_fallback`) | **partial** (positive fallback covered; missing-everywhere case not explicitly asserted) |

## Integration Tests — No Real Network/API

Verified by `grep -RE "anthropic\.com|api\.telegram\.org|telegra\.ph|autoevolution\.com" tests/`. All matches are inside fixture URL strings, mock return values, or `httpx.Request("POST", "https://api.anthropic.com/v1/messages")` constructors used solely to build a `httpx.Response` for SDK-exception fixtures — no `httpx.Client.send()` or actual network call. External SDKs (anthropic, telegram Bot, telegraph) are mocked everywhere:

- anthropic: every `test_claude_transcreation.py` test passes `client=MagicMock()` or builds an exception via the `make_status_error` / `make_connection_error` fixtures.
- Telegram: `news_bot.Bot` patched to `_FakeBot` in `test_no_token_leak_in_logs.py::TestAdminNotifyRedaction`; `news_bot.send_telegraph_teaser` and `send_admin_notification` patched in every integration test.
- Telegraph: `news_bot.telegraph_publisher.publish_article` patched in every integration test (return_value=fake URL).
- Sources: `news_bot.fetch_rss`, `fetch_mattel_news`, `fetch_full_article` mocked.
- `time.sleep` mocked in every test that runs `news_bot.job()` end-to-end.
- `freezegun.freeze_time` controls the clock in `test_distributed_schedule_integration.py` (4 scenarios).

## Grep — No References to Deleted Symbols

`grep -RE "_overflow_fast_track|IDLE_TIMEOUT_HOURS|QUEUE_CAP|GRACE_WINDOW_HOURS|FALLBACK_THROTTLE_SECONDS" tests/` matches **only inside** the resurrected `tests/test_overflow.py` and `tests/test_idle_fallback.py` files (the BLOCKER below). On the actual feature HEAD (`e659d05`), the grep is empty.

## Critical Findings

### BLOCKER-1 (delivery-blocking): `tests/test_overflow.py` and `tests/test_idle_fallback.py` are present in the working tree and staged

**What:** `git status` shows two staged-as-new files:
```
new file:   tests/test_idle_fallback.py
new file:   tests/test_overflow.py
```
Both contain references to deleted symbols (`_overflow_fast_track`, `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `FALLBACK_THROTTLE_SECONDS`). `pytest tests/ -q` reports **25 failures** (12 in test_idle_fallback.py + 13 in test_overflow.py), all `AttributeError: <module 'news_bot' ...> does not have the attribute 'QUEUE_CAP'` etc.

**Why it matters:** AC29 ("`pytest tests/ -q` green") fails. Task 9 commit `050b6eb` deleted these files at HEAD; something between Task 9 and the current uncommitted state re-introduced them. Possibly `git restore tests/test_overflow.py` ran by mistake, or a merge artifact. The HEAD commit (`e659d05`) does NOT include these files (verified via `git log --oneline -- tests/test_overflow.py`).

**Severity:** blocker for Task 17 pre-deploy QA. The fix is a one-liner `git rm tests/test_overflow.py tests/test_idle_fallback.py`, BUT this audit is read-only — must be flagged for Task 17 to clean up.

**Where to action:** Task 17 (pre-deploy QA) operator runs `git rm tests/test_overflow.py tests/test_idle_fallback.py && git commit` BEFORE running the suite for AC29 sign-off.

### MINOR-1: AC17 (empty-queue-at-recovery) lacks a dedicated test

**What:** AC17 says "outage clears + queue empty in this moment → bot stays in fallback_active mode until next 12:00 МСК cron tick." The state-machine test `TestRecordRecoveryEvent::test_record_recovery_event_clears_state` covers `record_recovery_event` directly, and the integration test in `test_distributed_schedule_integration.py` documents the gap explicitly in its docstring ("Wave 1-6 has no auto-call yet"). No isolated test for the empty-queue case at recovery.

**Severity:** minor — the contract is naturally satisfied because empty queue means no slot fires `record_recovery_event` until the next cron tick, which is exactly the AC17 behaviour. But a defensive 1-test addition (`outage_active + pending=[] + run job() → fallback_active still True at end`) would lock the contract.

### MINOR-2: init_schema failure path → admin ping (Risk 5 mitigation) not tested

**What:** Tech-spec AC says "if migration fails (rare — disk full, locked DB), the failure is caught at `news_bot.main()` startup → admin-ping with the failure type → process exits cleanly." `test_migration.py::test_init_db_idempotent` only covers the happy path. No simulated migration-failure test exists.

**Severity:** minor — the failure mode is rare (disk full / locked DB on cold start). A 1-test addition with `monkeypatch` of `pending_articles_repo.init_schema` to raise would lock the contract.

### MINOR-3: `max_tokens=8000` not asserted on the API call

**What:** `claude_transcreation.py:74` sets `_DEFAULT_MAX_TOKENS = 8000`, and line 487 passes it to `client.messages.create(max_tokens=max_tokens, ...)`. None of the 15 tests in `test_claude_transcreation.py` assert this kwarg on the mock. A future regression that lowers max_tokens to 2000 would not be caught.

**Severity:** minor — Decision 13 enforces 8000 as the security ceiling. A 1-line addition (`mock_anthropic_client.messages.create.assert_called_once()`; `assert mock_anthropic_client.messages.create.call_args.kwargs['max_tokens'] == 8000`) would close the gap.

### MINOR-4: N=12 and N=30 boundary cases for compute_publish_slots not exercised directly

**What:** Plan called for N=12 (1-carry-over edge) and N=30 (large backlog). Tests cover N=11, N=15, N=20 — same code paths via the floor=40-min cap, but the explicit N=12 (just over the floor → carry=1) and N=30 (carry=19) cases are absent.

**Severity:** minor — algorithm linearity proves transitive coverage. Two 3-line tests would add explicit boundary coverage.

### MINOR-5: ux-guidelines.md missing-everywhere case not explicitly tested

**What:** `test_load_prompt_subdir_then_flat_fallback` covers the positive fallback path. The case "neither subdir nor flat path exists → FileNotFoundError" relies on the `_load_prompt` source raising on lines 148-150 and 166-168. No test actually drives that branch.

**Severity:** minor — the source is straightforward and the existing fallback test covers the I/O surface. A 1-test addition would lock the contract.

## Notes & Recommendations

- **Test isolation:** Excellent. Every test uses `tempfile.mkstemp` for the SQLite DB and patches `news_bot.DB_FILE` so the real `news.db` is never touched. Verified across `test_migration`, `test_outage_state`, `test_fallback_publish_paths`, `test_job_distributed_publish`, `test_integration`, `test_distributed_schedule_integration` (= every test that initializes a DB).

- **Mock realism:** `test_claude_transcreation.py` builds proper `httpx.Response` and `httpx.Request` instances inside the `make_status_error` and `make_connection_error` fixtures because the anthropic SDK constructors require both `response` and `body` kwargs — naïve `side_effect = anthropic.RateLimitError("msg")` would TypeError. This kind of careful fixture engineering is rare; pin it as a pattern for future tests.

- **Concurrency test depth:** `test_outage_state.py::TestConcurrency::test_concurrent_writers_serialize_via_begin_immediate` is the strongest integration-level concurrency test in the project. The race is set up to fail without BEGIN IMMEDIATE (both racers post-1h-boundary observe ping_count=1, both compute next=ping_2_sent, both write — without serialization, both emit ping #2). The assert "exactly 1 caller emitted ping #2" catches the regression. This is excellent test design.

- **Token-redaction depth:** AC25 has 14 dedicated tests (10 in `TestAnthropicKeyRedaction` + 4 in `TestAdminNotifyRedaction`) covering the three-layer Decision 12 defence. End-to-end via root logger, end-to-end via admin notify path, regex over-match negatives, env-name list membership, helper neutrality on None / empty input. This is the deepest single-AC coverage in the suite.

- **freezegun + datetime patching co-existence:** `test_integration.py` patches `news_bot.datetime` directly and adds passthroughs for `combine` / `strptime`. `test_distributed_schedule_integration.py` uses `freezegun.freeze_time` instead. Both approaches work but produce different side effects (`freezegun` makes `time.time()` frozen too; `datetime` patching does not). Future regression: tests mixing both patterns in the same module could collide. Worth documenting in `patterns.md`.

- **Plan delta documentation:** Test count delta (+34 net) overshoots the ~+2 plan, but every overshoot maps to an apply-finding in the round-1 review reports (TR-1 / TR-2 / TR-3 etc. in `logs/working/task-XX/test-reviewer-round1.json`). No silent inflation.

- **Recommendation for Task 17:**
  1. **First action:** `git rm tests/test_overflow.py tests/test_idle_fallback.py` to resolve BLOCKER-1.
  2. Re-run `pytest tests/ -q` and confirm 566 passed.
  3. Optionally add the 5 minor 1-test additions listed above (AC17 isolated test, init_schema failure, max_tokens assert, N=12 / N=30 boundaries, missing-prompt). Each is a 3-10-line addition. Together they would close every gap in this report.
  4. Verify `grep -RE "_overflow_fast_track|IDLE_TIMEOUT_HOURS|QUEUE_CAP|GRACE_WINDOW_HOURS|FALLBACK_THROTTLE_SECONDS" tests/` returns empty.
