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
