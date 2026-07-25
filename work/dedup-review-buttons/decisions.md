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

## Task 6: Config + Project Knowledge docs

**Status:** Done
**Commit:** aa5c4c4 (+ review fix 1470468)
**Agent:** doc-writer
**Summary:** Documented the feature's config and operator surface across the three scoped files. `.env.example` gained a commented `REVIEW_BUTTONS_ENABLED` block next to `DEDUP_SERIES_ENABLED` (default OFF, prod-only, double gate listener+keyboard, numeric-admin fail-closed, one-poller/409). `architecture.md` gained a Data Flow subsection «Inbound review path — `_run_review_listener()`» (first inbound Telegram path: daemon-thread `get_updates(offset, timeout=30, allowed_updates=['callback_query'])`, `dd:<c|k>:<token>` grammar → `resolve_dedup_callback` outcomes incl. race-honest «уже опубликовано», error isolation, fail-closed gate) plus the `review_token:<token>` key with lifecycle in the `bot_state` section (busy_timeout note extended to cover the listener as second writer). `deployment.md` gained the Optional-env bullet and a `## Feature rollout: dedup-review-buttons` operator runbook (enable only in hand-managed prod `.env`, rebuild outside 10:00–20:00 МСК, verify «review listener active» in `docker logs hw-news-bot`, confirm test stays flag-off/no 409, disable path). Task 5 listener mechanics documented from the approved tech-spec design (written in parallel with its implementation, as planned).
**Deviations:** None. The User-Spec Deviation `[PENDING USER APPROVAL]` marker on `REVIEW_BUTTONS_ENABLED` in tech-spec.md left in place — operator confirmation not yet given.

**Reviews:**

*Round 1:* changes_requested — 1 MAJOR factual finding: architecture.md's bot_state section claimed `pending_articles_repo._connect()` sets `PRAGMA busy_timeout=5000`; in fact it deliberately uses the connect-time parameter `sqlite3.connect(..., timeout=5.0)` (see its docstring — no extra `execute()`, protects the fault-injection execute-counter). Fix (commit 1470468, architecture.md only): reworded to distinguish the two mechanisms — `outage_state` executes the PRAGMA, `pending_articles_repo._connect()` pins the equivalent connect-time `timeout=5.0` (same 5 s contention absorption). Verified against the actual `_connect()` source before rewording. Everything else verified clean by the reviewer (log line, grammar, flag list, gate, runbook — byte-exact vs code).

**Verification:**
- `grep -rl REVIEW_BUTTONS_ENABLED` over the 3 files → all three listed
- `grep "review_token|get_updates|_run_review_listener"` in architecture.md → 5 hits (inbound path + bot_state key)
- Log line «review listener active», key `review_token:<token>`, grammar `dd:c:/dd:k:`, status strings — byte-checked against tech-spec (Decisions 3/5/6, Data Models, Task 12)
- Consistency vs existing invariants (shared token/one poller, manual prod deploy outside window, hand-managed prod `.env`, deploy FILES unchanged) → no contradictions

## Task 5: Background review listener + main() wiring

**Status:** Done
**Commit:** 9a208b3
**Agent:** coder-listener
**Summary:** Added the bot's first inbound Telegram path: `_run_review_listener()` — a daemon-thread long-poll loop (`get_updates(offset, timeout=30, allowed_updates=['callback_query'])`) that parses presses against the exact `dd:<c|k>:<token>` grammar (anything else silently ignored, only offset advances), maps `c/k → 'cancel'/'keep'` BEFORE calling `resolve_dedup_callback`, and applies outcomes: terminal → `edit_message_text` (original text + status line, `reply_markup=None`) + `answer_callback_query` + operator-decision INFO log (action + link + status, no token); ignored → empty answer only. Resilience: per-update try/except (a DB/Telegram fault on one update is logged, acked via offset and skipped), poll-cycle try/except with 5s backoff (60s + explicit single-listener ERROR message on 409 `Conflict`), and a belt-and-braces outer handler — nothing ever escapes the thread. Wiring: `_maybe_start_review_listener()` in `main()` BEFORE cron registration, gated by the pure `_review_listener_enabled()` (flag on AND numeric `TELEGRAM_ADMIN_ID`, fail-closed); gate open → «review listener active» log + ping, flag on + non-numeric admin → WARNING + best-effort explanatory ping, flag off → silent. Testability seams: `stop_event` for the loop, sync `_handle_review_update(update)` per-update handler, patchable backoff constants. Key implementation decision: every Telegram call runs a FRESH `Bot` inside its own `asyncio.run` (helpers `_review_get_updates` / `_review_edit_message` / `_review_answer_callback`) — PTB's `HTTPXRequest` builds its `httpx.AsyncClient` in `__init__` and pools keep-alive connections bound to the creating event loop, so a single Bot reused across successive `asyncio.run` loops would fail every second poll with «Event loop is closed»; per-call Bot matches `send_admin_notification` exactly and keeps each connection inside its own loop. TDD: 10 new tests in `tests/test_integration.py::TestReviewListener` written first (red 10/10), then implementation (green) — gate on/off/non-numeric, error-does-not-propagate, 409 messaging, poisoned-update offset-ack survival, grammar rejection matrix (10 malformed shapes, zero Bot construction), letter→word mapping spy, real-DB admin-cancel dispatch (edit+answer+log+pending skipped), non-admin ignored press (empty answer, state untouched).
**Deviations:** None vs task WHAT/AC. One refinement vs the literal hint «создаёт собственный Bot и держит его в цикле»: the Bot is per-call rather than loop-lifetime, for the cross-event-loop httpx reason above; «свой экземпляр, не переиспользует чужой» and the `send_admin_notification` style are both honored.

**Reviews:**

*Round 1 (orchestrated by lead):*
- code-reviewer: approved → [logs/working/task-5/code-reviewer-1.json]
- security-auditor: PASS, 2 low → [logs/working/task-5/security-auditor-1.json]
- test-reviewer: approved_with_comments, 1 major → [logs/working/task-5/test-reviewer-1.json]

Fixes (commit afe4944):
- (test-reviewer, MAJOR) Backoff not test-pinned: deleting either `_review_listener_sleep(...)` call left all tests green — a silent busy-loop regression path. Both loop tests (409 + generic error) now wrap `news_bot._review_listener_sleep` in a `wraps=` spy and assert exactly one call with the correct patched backoff constant (Conflict → CONFLICT constant, generic → ERROR constant; the wrong-constant case fails too since only the expected one is patched to 0). Mutation-verified: sleep removed from the Conflict branch → `test_review_listener_conflict_409_logged_with_backoff` FAILS; removed from the generic branch → `test_review_listener_error_does_not_propagate` FAILS; both restored → green.
- (security-auditor SEC-T5-1, LOW) Handler performed a `get_review_token_link` DB read for every well-formed callback BEFORE the admin gate. Extracted the fail-closed comparison into `_is_admin_press(from_user_id)` — single source of truth, now used by BOTH `resolve_dedup_callback` (its gate, behaviour unchanged, all 10 `TestResolveDedupCallback` tests untouched and green) and `_handle_review_update`, which gates FIRST: non-admin → answer-only with ZERO DB reads (pinned by a `get_review_token_link` not-called spy in `test_ignored_press_answers_empty_and_never_edits`); the link pre-fetch for the decision log now happens only for admin presses. `test_callback_letter_maps_to_full_word` updated to a matching numeric admin so the spy still observes the mapped words.

Rejected finding:
- (security-auditor SEC-T5-2, LOW) «`logger.exception` traceback bypasses the redaction filter» — REJECTED, out of feature scope: `logger.exception` with raw tracebacks is the pre-existing project-wide pattern (e.g. the dedup-gate degraded-mode handler); the exceptions reachable from the listener's per-update guard were verified token-free (token values never appear in exception messages of `pending_repo`/PTB calls), and changing traceback redaction is a project-wide logging concern, not part of dedup-review-buttons.

**Verification:**
- Smoke: `python3 -c "import news_bot; print(hasattr(news_bot,'_run_review_listener'))"` → `True`
- `pytest tests/test_integration.py::TestReviewListener -q` → 10 passed; `::TestResolveDedupCallback` → 10 passed (shared `_is_admin_press` refactor regression-free)
- `python3 -m pytest -q` (full suite, after afe4944) → 1324 passed, 0 failed (baseline 1314 + 10 new, no regressions)
- Mutation checks (round 1 fix): each `_review_listener_sleep` call deleted in turn → corresponding backoff test fails; restored → green.

## Task 9: Test Audit

**Status:** Done
**Commit:** none (analysis-only task; no code/tests changed)
**Agent:** test-auditor
**Summary:** Audit verdict — **GO for Pre-deploy QA** (`passed` per test-master decision matrix): 0 critical, 0 high, 2 medium, 4 low. Full suite recorded green (1324 passed, 0 failed, 53 s; 1284→1324 = +40 matches tasks 1–5 exactly), all six+ `resolve_dedup_callback` branches, gate/fail-closed, concurrency (genuinely contended, sleep-free) and alert-regression coverage confirmed against code. The two mediums are cross-component seam gaps, not weak tests: (M-1) no builder→parser round-trip test for the `dd:<c|k>:<token>` grammar (silent-drift class); (M-2) `main()`'s `_maybe_start_review_listener()` call is unpinned — mutation-verified: deleting the line leaves all 1324 tests green (mitigated by the Task 12 «review listener active» runbook check). Both ≈5 LoC to close in the Task 10 window. Report: [logs/working/task-9/test-audit.md](logs/working/task-9/test-audit.md).
**Deviations:** None. One temporary mutation spot-check on `news_bot.py` for M-2 was fully restored (`git checkout`, verified clean).

**Reviews:**

Нет — аудит-задача (`reviewers: []`), результат — сам отчёт.

**Verification:**
- `python3 -m pytest tests/ -q` → 1324 passed, 0 failed (recorded in report)
- Targeted 4 audited files → 220 passed
- Mutation spot-check (main() wiring removed) → full suite still green → gap M-2 confirmed, file restored

## Task 7: Code Audit

**Status:** Done
**Agent:** code-auditor
**Summary:** Holistic audit of the feature's final state (full-file reads, all 4 focus dimensions + general quality). Verdict: **issues found, no blockers** — 0 blocker / 1 major / 2 minor / 3 nit. Thread-safety, shared-resource compliance, Bot-init consistency and listener error isolation are all structurally sound; the major finding (CA-1) is a residual cancel-vs-in-flight-publish race the tech-spec believed `_fallback_publish`'s top idempotency guard covered but does not: a cancel pressed while that article's slot publish is mid-flight still posts to the channel, answers «✅ Отменено оператором» and leaves `published_articles` without the row (`move_to_published` silently no-ops). Full report with evidence + recommendations: [logs/working/task-7/code-audit.md](logs/working/task-7/code-audit.md).
**Deviations:** None. Analysis only — no code changed (`git status` on feature source files clean).

**Verification:**
- `python3 -m pytest -q` (audit baseline) → 1324 passed, 0 failed
- Report covers thread-safety, shared resources, Bot-init drift, error isolation + general quality; every finding has severity + file:line + evidence + recommendation

## Task 8: Security Audit

**Status:** Done
**Commit:** — (analysis only, no code changes; audited state f50d974)
**Agent:** security-auditor
**Summary:** Full-feature security audit of the final state (OWASP Top 10 sweep + end-to-end auth chain, injection, secret hygiene, DoS on the public getUpdates path, cross-component trust incl. poisoned `bot_state` token values). Verdict: **PASS — 0 Critical, 0 High, 0 Medium, 3 Low** (flag-on+non-numeric-admin still renders dead buttons at the E014 send site; no TTL/cleanup for never-pressed `review_token:*` rows; `_review_edit_message` lacks a defense-in-depth `_redact_text` pass). All 8 tech-spec Risks confirmed closed; prior accepted/declined dispositions (SEC-T5-2 traceback redaction, token-not-secret, non-atomic token consume, builder-level token validation) re-verified as acceptable in the final state. Report: [logs/working/task-8/security-audit.md](logs/working/task-8/security-audit.md).
**Deviations:** None. No implementation-vs-tech-spec deviations found; no spec updates required.

**Reviews:**

- none (this task IS the security review instance; deliverable is the report above)

**Verification:**
- `git status` clean at audit start; no source files modified by this audit. At audit close a PARALLEL audit task's in-flight mutation was observed in the working tree (`news_bot.py:3302` listener wiring → `pass  # MUTATION`) — left untouched, flagged to lead: must be restored before any commit (restored + mutation-pinned in ed10c58).
- Report exists at `logs/working/task-8/security-audit.md` with verdict + per-severity findings + OWASP sweep + Risks-table closure
- **Fix verification round (ed10c58):** SEC-A8-1 RESOLVED (E014 send site gates on `_review_listener_enabled()`, single-sourced with the listener gate incl. the CA-3 bot-token check); SEC-A8-3 RESOLVED (`_redact_text` on the edit path); SEC-A8-2 declined per documented no-janitor trade-off — accepted. Quick security pass over the full ed10c58 diff (gate refactor, CA-1a/1b race guards, CA-2 log reorder, CA-5 byte cap): no new findings. Fix round PASS — details appended to the report.

## Ad-hoc: audit-wave fixes (fixer-audit)

**Status:** Done
**Commit:** ed10c58 (`fix: address audit wave findings — in-flight cancel race, wiring pin, gates`)
**Agent:** fixer-audit (ad-hoc, post-Audit-Wave)
**Summary:** All in-scope findings from the three audit reports (task-7 code, task-8 security, task-9 test) fixed in one commit, TDD where the finding was code behavior. Full suite **1333 passed, 0 failed** (baseline 1324 + 9 new tests). Scope additions beyond the fix list: `architecture.md` + `deployment.md` gate/race wording updated to match the new behavior (docs-are-deliverables rule).

| Finding | Severity | Disposition |
|---|---|---|
| CA-1a (in-flight cancel: pre-teaser re-check) | major | **Applied** — `_fallback_publish` re-checks `get_pending(link)` right before the Telegram teaser (last irreversible step); row gone → INFO `[review-cancel]` line + `return True` (success-without-publish, mirrors idempotency guard: no strike, no post). Teaser-already-sent retry path deliberately bypasses the guard (post exists → completing the move is the consistent outcome). Tests: `TestInFlightCancelGuard` (race + positive control), red→green. |
| CA-1b (move_to_published silent no-op) | major | **Applied** — `src is None` branch now WARN-logs + dozapis `published_articles` from the explicit args (`title` recovered from the `processed_news` stamp `skip_pending` left, link fallback; `ru_title`=title, `source_name`=''), plus `processed_news` re-stamp; mirrors the existing post-commit-verification style. Tests: 2 in `TestMoves`, red→green. |
| CA-1c (tech-spec false coverage claim) | major (part) | **Applied** — tech-spec «How it works» sentence replaced with the actual two-sided guard description; same correction to the mirrored claim in `architecture.md`. |
| M-2 (main() listener wiring unpinned) | medium | **Applied** — `test_main_wires_review_listener` spies `_maybe_start_review_listener` through the existing `_run_main_once` precedent. **Mutation-verified:** replacing the `main()` call with `pass` → test FAILS (AssertionError); restored → green. |
| M-1 (builder→parser round-trip) | medium | **Applied** — `test_keyboard_callback_data_round_trips_through_parser`: real `secrets.token_urlsafe(9)` through `build_dedup_review_keyboard` → `_parse_review_callback_data` returns `('cancel', token)` / `('keep', token)` for both buttons. |
| CA-3 (gate ignores empty bot token) | minor | **Applied** — gate refactored to `_review_listener_gate_reason()` (`'ok'|'off'|'no_token'|'bad_admin'`); empty `TELEGRAM_BOT_TOKEN` is fail-closed with a startup WARNING + ping naming the broken knob. Mutation-checked (guard removed → test fails). |
| SEC-A8-1 (E014 send site on bare flag) | low | **Applied** — send site gates on `_review_listener_enabled()`; flag-on + non-numeric admin now mints no token, renders no buttons. `TestDedupReviewButtons` setUp got a numeric-admin patch; new negative test. Mutation-checked (bare flag restored → test fails). |
| CA-2 (decision log after Telegram I/O) | minor | **Applied** — operator-decision INFO line moved BEFORE `edit_message_text`/`answer_callback_query`; test pins the line surviving a raising edit. |
| SEC-A8-3 (edit path bypasses redaction) | low | **Applied** — `_review_edit_message` passes `text` through `_redact_text` (Decision-12 parity with the send path). |
| CA-5 (64-byte cap counted in chars) | nit | **Applied** — `len(data.encode('utf-8'))` vs renamed `_REVIEW_CALLBACK_DATA_MAX_BYTES`; multibyte >64-byte case added to the grammar-rejection matrix. |
| CA-6 (redundant flag check in startup wiring) | nit | **Applied** — via the `_review_listener_gate_reason()` refactor: single flag read, three startup shapes branch on the reason. |
| CA-4 (duplicate token→link read, cosmetic TOCTOU) | nit | **Not applied** — deliberate, documented single-source-of-truth read; log-cosmetic only (audit's own assessment). |
| SEC-A8-2 (token TTL/janitor) | low | **Declined per adjudication** — documented no-janitor trade-off stands. |
| Test-audit lows L-1–L-4 | low | **Declined per adjudication** — recorded informational; no action. |

**Verification:**
- Targeted: `TestInFlightCancelGuard` 2, `TestMoves` +2, `TestMainHealthChecks` 4 (incl. new wiring pin), `TestReviewListener` +3, `TestDedupReviewButtons` +1 — all green
- `python3 -m pytest tests/ -q` → **1333 passed, 0 failed** (65 s)
- Mutations: M-2 wiring removed → FAIL/restored; CA-3 token guard removed → FAIL/restored; SEC-A8-1 bare-flag restored → FAIL/restored

## Task 10: Pre-deploy QA

**Status:** Done
**Commit:** none (терминальная приёмка — код и тесты не менялись)
**Agent:** qa-runner
**Summary:** QA **passed** — 0 critical, 0 major, 0 minor. Полный набор: `python3 -m pytest tests/ -q` → **1333 passed, 0 failed** (48.9 s; watch-item `test_main_wires_review_listener` прошёл с первого раза, флейк не повторился). Все 20 критериев приёмки проверены с доказательствами: **19 passed, 1 not_verifiable** (U8 — живое «слушает только прод»/409 на общем токене), 0 failed. Coverage: каждый файл scope фичи покрыт тестами, все обязательные edge-cases (устаревший токен, не-админ, гонка у слота, database is locked, fail-closed гейт, изоляция listener) имеют тесты, исполняющие реальное поведение (real SQLite, real job(), assertLogs INFO, mutation-verified пины). Smoke-команды задач 1/2/5 повторно прогнаны — все ok. Блокеров нет.
**Deferred to post-deploy:** 3 пункта → Task 12 (U8 живой single-listener/нет-409; визуальная часть U3/U4/U5 — реальный тап и edit в чате; строка «review listener active» в docker logs для T7). См. `deferredToPostDeploy` в qa-report.json.
**Deviations:** Нет. Примечание: отчёт записан в `logs/working/task-10/qa-report.json` (по указанию лида — единый формат task-N; текст задачи называл `logs/working/qa-report.json`).

**Verification:**
- `python3 -m pytest tests/ -q` → 1333 passed, 0 failed
- Фокус-набор 4 файлов фичи → 228 passed
- Smoke tasks 1/2/5 → ok / ok / True
- Full report: [logs/working/task-10/qa-report.json](logs/working/task-10/qa-report.json)

## Task 11: Deploy (operator-applied)

**Status:** Done (подготовка Claude завершена; серверные шаги применяет оператор)
**Commit:** — (правок кода/деплой-скриптов нет; проверено состояние 8e3be71)
**Agent:** deploy-prep
**Summary:** Инвариант FILES подтверждён на HEAD 8e3be71: `git diff --stat c8744f0..HEAD -- '*.py'` затрагивает только `news_bot.py` / `admin_alerts.py` / `pending_articles_repo.py` (+ tests/, которые не деплоятся); новых first-party модулей нет — добавленные в `news_bot.py` импорты только stdlib (`secrets`, `threading`) и уже установленный `telegram.error`. Все три файла присутствуют во всех трёх массивах FILES (deploy.sh:38/45/54, deploy.yml:124/131/140, deploy_test.yml:95/102/111) → deploy-скрипты править НЕ нужно. Strip-then-append в `deploy_test.yml` (строка 167) управляет только LLM-ключами + TZ — `REVIEW_BUTTONS_ENABLED` не в списке и на тесте вручную не задан → тест остаётся OFF (флаг по коду включается только явным on-word: `1/true/yes/on`). Операторский runbook написан по-русски простым языком: [logs/working/task-11/deploy-runbook.md](logs/working/task-11/deploy-runbook.md) — фазы: тест (push dev → авто-деплой, кнопки дремлют, проверка «нет review listener active / нет 409»), промоут dev→main ВНЕ окна 10:00–20:00 МСК, прод (числовой `TELEGRAM_ADMIN_ID` через @userinfobot + `REVIEW_BUTTONS_ENABLED=1` в hand-managed `/root/hw-news/.env` идемпотентными sed/grep-однострочниками без вывода секретов, затем `git pull && docker compose up -d --build`), верификация (`review listener active` есть на проде, нет на тесте, нет `[E018]`/409), откат (вариант А: флаг=0 + rebuild — фича полностью спит; вариант Б: git revert). Инвариант «ровно ОДИН листенер (прод)» выделен в правило №1 runbook. Сверено с deployment.md § «Feature rollout: dedup-review-buttons» — противоречий нет, runbook на раздел ссылается.
**Deviations:** Нет. Уточнение к тексту задачи: journalctl на NL даётся через `ssh root@148.135.207.54` (hwbot потерял NOPASSWD journalctl при усилении 2026-07-07).

**Verification:**
- `git diff --stat c8744f0..HEAD -- '*.py'` → только 3 деплоящихся модуля + tests/
- `git diff --diff-filter=A --name-only c8744f0..HEAD` → добавлены только work/-документы, ни одного нового .py вне tests/
- grep FILES по трём деплой-конфигам → все три модуля во всех трёх списках
- deploy_test.yml:167 regex не содержит REVIEW_BUTTONS_ENABLED; news_bot.py:147–149 — off по умолчанию, `0` = off
- Runbook: [logs/working/task-11/deploy-runbook.md](logs/working/task-11/deploy-runbook.md)
