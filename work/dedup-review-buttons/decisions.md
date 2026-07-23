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
