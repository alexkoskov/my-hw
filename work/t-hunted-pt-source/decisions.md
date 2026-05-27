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

## Task 2: Admin alerts E031-E033

**Status:** Done
**Commit:** a318841b
**Agent:** teammate (general-purpose, opus)
**Summary:** Added 3 admin-alert builders to `admin_alerts.py` between E028 (last lamley alert) and E030 (orangetrack aggregator): `alert_t_hunted_host_rejected` (E031, 🟡 SSRF rejection), `alert_t_hunted_fetch_error` (E032, 🟡 HTTP/timeout), `alert_t_hunted_no_body` (E033, 🟡 parser couldn't find body, mentions `<div class="post-body">` selector). Mirrors lamley E025/E027/E028 shape with Russian copy + Ссылка/Ошибка/Что сделать sections. E029 intentionally skipped per code-research §A.5.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved (2 minor cosmetic optional) → [logs/working/task-2/code-reviewer-round1.json]
- test-reviewer: passed (0 findings) → [logs/working/task-2/test-reviewer-round1.json]

**Verification:**
- `pytest tests/test_admin_alerts.py -v` → 29 passed (+3 from 26)
- `pytest tests/ -q` → 950 passed, 2 skipped (baseline +3, no regressions)

