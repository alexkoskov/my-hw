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
