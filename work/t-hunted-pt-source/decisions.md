# Decisions Log: t-hunted-pt-source

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

## Task 1: New parser module + unit tests

**Status:** Done
**Commit:** c12b7e81 (impl) + 98f3693e (round 2 fixes)
**Agent:** teammate (general-purpose, opus) + fixer (general-purpose, opus)
**Summary:** `t_hunted_source.py` (206 LOC) mirrors lamley_source.py minus WAF/throttle apparatus. SSRF allowlist exact-match on `t-hunted.blogspot.com`. Blogger-aware image dedup strips `=s\d+(-c)?` size suffix from path (broader than literal AC regex, documented). 14 unit tests in `tests/test_t_hunted_source.py` (6 TestFetch + 8 TestHostAllowlist). Production code references `admin_alerts.alert_t_hunted_*` builders (Task 2 creates them); test file uses autouse monkeypatch with `[E03N-STUB]` fingerprints.
**Deviations:** (1) Image dedup regex broadened `=s\d+(-c)?$` → `=s\d+(-c)?(?:/|$)` — Blogger places size token mid-path, anchor-only form would miss; documented in source. (2) LOC 206 vs ≤200 target — 6-line overshoot in docstrings, acceptable per AC.

**Reviews:**

*Round 1:*
- code-reviewer: approved_with_suggestions (3 minor, cosmetic) → [logs/working/task-1/code-reviewer-round1.json]
- security-auditor: approved (3 minor, systemic with lamley) → [logs/working/task-1/security-auditor-round1.json]
- test-reviewer: needs_improvement (3 MAJOR + 3 minor) → [logs/working/task-1/test-reviewer-round1.json]

*Round 2 (after fixes):*
- test-reviewer: passed/approved (0 findings, all 3 majors closed) → [logs/working/task-1/test-reviewer-round2.json]

**Verification:**
- `pytest tests/test_t_hunted_source.py -v` → 14 passed
- `pytest tests/ -q` → 947 passed, 2 skipped (no regressions, baseline +12)
