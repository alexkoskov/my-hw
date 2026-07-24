# Decisions Log: dedup-review-buttons

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

## Task 1: DB concurrency guard + review-token store

**Status:** Done
**Commit:** e0c8bc7
**Agent:** coder-repo
**Summary:** `pending_articles_repo._connect()` now passes `sqlite3.connect(..., timeout=5.0)`, pinning the 5000 ms busy-timeout explicitly (no extra `execute()`, so the fault-injection execute-counter in `test_move_to_published_rollback_on_error` is untouched and stays green). Added `put_review_token` / `get_review_token_link` / `delete_review_token` over the existing `bot_state` table under key prefix `review_token:` (module constant `_KEY_REVIEW_TOKEN_PREFIX`), modeled on the self-connecting `outage_state._get`/`_set` pattern. New tests: `TestReviewTokenStore` (6), `TestConnectBusyTimeout` (1), `TestConcurrentWriters` two-writer test — no `database is locked`.
**Deviations:** None functionally. One finding worth recording: the task premise «голый connect падает мгновенно (0 мс)» is inaccurate — Python's `sqlite3.connect` default `timeout` is already 5.0 s, so `PRAGMA busy_timeout` read 5000 even before the change. The explicit `timeout=5.0` pins the contract in code (protected by the new tests) instead of relying on a stdlib default; all acceptance criteria hold as written.

**Reviews:**

*Round 1:*
- code-reviewer: approved
- security-auditor: approved
- test-reviewer: changes_required (3 findings) → [logs/working/task-1/test-reviewer-round1.json]

Fixes (commit 7847cc1, tests only): (HIGH) call-spy test `test_connect_passes_explicit_timeout_parameter` pins the explicit `timeout=5.0` argument — verified it fails against a reverted `_connect()`; (MEDIUM) sleep-based sync in the two-writer test replaced with a `threading.Event` fired from a wrapping connection right before the blocking `INSERT INTO processed_news`, so contention is guaranteed while the lock is provably held; (LOW) accepted — added negative control `test_zero_timeout_control_raises_database_locked` (single-threaded `timeout=0` writer vs held `BEGIN IMMEDIATE` → immediate `database is locked`; fast, deterministic).

*Round 2:* pending — orchestrated by lead

**Verification:**
- `python3 -m pytest tests/test_pending_articles_repo.py -q` → 66 passed (56 baseline + 10 new)
- `python3 -m pytest tests/test_pending_articles_repo.py -k "rollback_on_error or two_writers" -v` → 2 passed
- `python3 -m pytest tests/ -q` → 1301 passed
- Smoke (put/get/delete round-trip on schema-initialized temp `DB_FILE`) → printed `ok`
- Regression-guard self-check: spy test fails with `timeout=5.0` reverted, passes restored

## Task 2: Keyboard builder + reply_markup forwarding

**Status:** Done
**Commit:** 3dbd896
**Agent:** coder-keyboard
**Summary:** Added pure builder `admin_alerts.build_dedup_review_keyboard(token)` → `InlineKeyboardMarkup` with two buttons in contract order: «🚫 Не публиковать» (`dd:c:<token>`) first, «👍 Оставить» (`dd:k:<token>`) second (Decision 3 grammar), plus the `telegram` import in `admin_alerts.py`. `news_bot.send_admin_notification` got a keyword-only `reply_markup=None` forwarded verbatim to `bot.send_message` — it bypasses `_redact_text` (telegram object, not text), and the `None` default keeps the call identical to pre-keyboard behaviour. Alert texts untouched («Похож на дубль» anchor intact); TDD: 7 new tests written first (red 6/7 — the keyword-only test pins today's existing contract), then implementation (green).
**Deviations:** None.

**Reviews:**

pending — orchestrated by lead

**Verification:**
- `pytest tests/test_admin_alerts.py tests/test_admin_ping.py -v` → 89 passed (82 baseline + 7 new)
- `python3 -m pytest tests/ -q` → 1299 passed (incl. `test_no_token_leak_in_logs.py::TestAdminNotifyRedaction` — no regressions)
- Smoke (`build_dedup_review_keyboard('t')` flat callback_data == `['dd:c:t','dd:k:t']`) → printed `ok`

## Task 4: Callback decision logic (`resolve_dedup_callback`)

**Status:** Done
**Commit:** b5e742a (+ polish c2c7e0d)
**Agent:** coder-callback
**Summary:** Added pure module-level `news_bot.resolve_dedup_callback(action, token, from_user_id)` (placed right after `send_admin_notification`, no new imports) — no Telegram I/O, returns `(status_text, answer_text)`; ignored press → `(None, "")` so the Task 5 listener knows not to edit the message. Order of checks per wave-1 security note: numeric-admin gate FIRST (int-compare with module-attr `TELEGRAM_ADMIN_ID` read at call time; non-numeric → fail-closed), then token resolve, then keep/cancel branching by DB state (Decisions 2/5/9/10). Token deleted only on terminal outcomes (keep + three cancel branches); unknown `action` returns the stale text without consuming the token. TDD: 8 new tests in `tests/test_integration.py::TestResolveDedupCallback` (`_IntegrationBase`, real tempfile SQLite) written first (red 8/8), then implementation (green), covering every branch incl. the cancel-then-slot invariant (cancelled link never handed to stubbed `_fallback_publish`, absent from `list_pending()`/`published_articles`) and idempotent second press → «⚠️ Кнопка устарела».
**Deviations:** None functionally. One clarification recorded: for unknown `action` values the task allowed "no-op / «устарела»" — chose to return «⚠️ Кнопка устарела» WITHOUT deleting the token (listener grammar filters these; a malformed callback must not burn a still-valid button).

**Reviews:**

*Round 1:* code-reviewer, security-auditor, test-reviewer — ALL approved. Two non-blocking items applied as a polish round (commit c2c7e0d):
- (security, low) Slot-boundary race in the cancel branch: row published between the `get_pending` check and `skip_pending` (which then no-ops silently) previously still answered «✅ Отменено оператором». Fix: re-read `get_published(link)` AFTER the skip — non-None means the publish won, answer the honest «⚠️ Уже опубликовано, отменить нельзя». Race-safe by post-hoc re-read, no transaction needed (skip_pending never writes published_articles). New test `test_cancel_race_published_between_check_and_skip` pins it (red before fix, green after).
- (code+tests, minor) Missing coverage for the defensive unknown-action branch: new `test_unknown_action_safe_fallback_token_not_consumed` — raw letters `'c'`/`'k'`, garbage, `''`, `None` all return the safe stale text, token NOT consumed, no state change, `skip_pending` never called.

**Verification:**
- `pytest tests/test_integration.py -k ResolveDedupCallback -q` → 10 passed (8 original + 2 polish)
- `python3 -m pytest tests/ -q` → 1314 passed, 0 failed (baseline 1312 after the parallel time-freeze fix c8519da for `test_job_distributed_publish.py`, + 2 new tests). Historical note from the initial run: 2 pre-existing time-of-day-flaky failures in `TestSlotLoopTransientRetry` (real `job()` without frozen `datetime.now`, run at 22:39 МСК past the 20:00 window-end guard) were verified identical on clean baseline 679dba8 via `git stash` — since fixed in c8519da.

## Task 3: Flag-gated review keyboard at the E014 send site

**Status:** Done
**Commit:** 8ba09b7 (+ review fix 3feada9)
**Agent:** coder-sendsite
**Summary:** Introduced the single module-level `news_bot.REVIEW_BUTTONS_ENABLED` flag next to `DEDUP_SERIES_ENABLED` — same const↔env name contract but deliberately INVERTED default: OFF unless the env var is an explicit on-word (`1/true/yes/on`, case-insensitive); unset/blank/off-words → disabled. At the E014 send site (`job()` → `decision == 'flag'` → `if alerted:`), when the flag is up: `secrets.token_urlsafe(9)` → `pending_repo.put_review_token(token, link)` BEFORE the send → `admin_alerts.build_dedup_review_keyboard(token)` passed as `reply_markup=` to the existing `send_admin_notification` call. Mint/put/build live inside the existing E014 `try` (a storage/build fault logs as "Failed to send E014 notification", publishing unaffected) and inside `if alerted:` (rate-limited flags mint nothing). Flag down → `kb=None`, call behaviourally identical to pre-feature; alert text and all other alerts (E006/E008/E009/E015/E016/E034) untouched. `import secrets` added to the stdlib import block. TDD: 3 new tests in `tests/test_integration.py::TestDedupReviewButtons` written first (red 3/3 — `AttributeError: no attribute 'REVIEW_BUTTONS_ENABLED'`), then implementation (green): flag-on keyboard with `['dd:c:<token>','dd:k:<token>']` + `get_review_token_link(token) == new_link`; flag-off parity (no `reply_markup`, zero `review_token:*` rows in `bot_state`); mixed E014+E015 run where only E014 carries buttons. Helpers reused from `TestCrossSourceDedup` via plain-function assignment (subclassing would have re-run its 20 tests).
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved
- security-auditor: approved
- test-reviewer: changes_required (1 MEDIUM finding) → [logs/working/task-3/test-reviewer-round1.json]

Fix (commit 3feada9, tests only): (MEDIUM) the "token persisted BEFORE send" invariant was not mutation-proof — post-`job()` assertions stayed green with `put_review_token` moved AFTER `send_admin_notification`. The flag-on test now installs a `side_effect` probe on the send mock that AT CALL TIME extracts the token from `reply_markup`'s `dd:c:<token>` callback_data and asserts `get_review_token_link(token)` already resolves to the flagged link, plus a `probe_hits` counter guarding against a vacuous probe. Mutation-verified: put moved after send → test FAILS with the race message; order restored (byte-identical to 8ba09b7, empty `git diff` on `news_bot.py`) → green. No production code changes.

*Round 2:* pending — orchestrated by lead

**Verification:**
- `pytest tests/test_integration.py -k "DedupReviewButtons" -v` → 3 passed
- `pytest tests/test_integration.py -q` → 53 passed (no regressions, existing E014 tests green)
- `python3 -m pytest tests/ -q` (initial, at 8ba09b7) → 1310 passed, 2 failed — both the KNOWN pre-existing time-of-day flake `tests/test_job_distributed_publish.py::TestSlotLoopTransientRetry::{test_exhausted_retries_strikes_once_not_per_retry,test_transient_failure_recovers_in_slot_no_strike}`; re-verified on clean baseline via `git stash` (fail identically without task 3 changes, run after 20:00 МСК window-end). Flake since fixed upstream (time-freeze, c8519da).
- `python3 -m pytest tests/ -q` (after review fix 3feada9, baseline incl. c8519da/c2c7e0d) → 1314 passed, 0 failed — fully green.
- Mutation check (round 1 fix): swap put/send order → `test_flag_on_e014_send_includes_review_keyboard` fails; restore → green.

## Ad-hoc: flaky-test fix — TestSlotLoopTransientRetry (evening-run flake)

**Status:** Done
**Commit:** c8519da
**Agent:** fixer-flaky
**Summary:** Pre-existing test debt discovered during task 4 (NOT feature code): `tests/test_job_distributed_publish.py::TestSlotLoopTransientRetry` (2 tests) drove the real `news_bot.job()` slot loop off `_JobBase` without freezing `datetime.now`, so any suite run after 20:00 МСК hit the window-end guard and the loop exited before publishing — both tests failed (reproduced at 22:51 МСК on baseline, green in daytime; this is what broke the 1312-count in the task 4/3 full-suite runs). Fix mirrors the file's own precedent: the class now inherits `_DistribLoopBase` (the existing time-frozen base that patches `news_bot.datetime` to 10:00 МСК, already used by `TestDistributedPublishLoop`) instead of `_JobBase`, plus a docstring note explaining why. Retry intent fully preserved — same 2 scenarios (transient failure recovers in-slot → no strike; exhausted retries → exactly one strike), same mocks/assertions, zero changes to `news_bot.py` or any other test class. Note: the assignment cited `tests/test_integration.py` as the location, but the class lives (and was fixed) in `tests/test_job_distributed_publish.py` — no such class exists in `test_integration.py`, which is untouched.
**Deviations:** File corrected from `tests/test_integration.py` (as assigned) to `tests/test_job_distributed_publish.py` (actual location of the class).

**Verification:**
- `pytest tests/test_job_distributed_publish.py::TestSlotLoopTransientRetry -q` at 22:51 МСК → 2 passed (was 2 failed on baseline at the same wall-clock time)
- `pytest tests/test_job_distributed_publish.py tests/test_integration.py -q` → 79 passed
- `python3 -m pytest tests/ -q` → 1312 passed, 0 failed (full suite green at any time of day)
