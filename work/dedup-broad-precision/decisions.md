# Decisions Log: dedup-broad-precision

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

## Task 1: Production corpus and scoring harness

**Status:** Done
**Commit:** Pending (uncommitted working tree)
**Agent:** main agent
**Summary:** Built the sanitized 24-pair production oracle with three reviewed duplicates and 21 non-duplicates, documented its provenance, and added a deterministic offline scorer against the real pair rule. Integrity checks now validate both the raw evidence and its exact fixture derivation, pin operator labels, and reject malformed fingerprints or private data.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: 3 findings → [logs/working/task-1/code-reviewer-1.json](logs/working/task-1/code-reviewer-1.json)
- security-auditor: 4 findings → [logs/working/task-1/security-auditor-1.json](logs/working/task-1/security-auditor-1.json)
- test-reviewer: 5 findings → [logs/working/task-1/test-reviewer-1.json](logs/working/task-1/test-reviewer-1.json)

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-1/code-reviewer-2.json](logs/working/task-1/code-reviewer-2.json)
- security-auditor: OK → [logs/working/task-1/security-auditor-2.json](logs/working/task-1/security-auditor-2.json)
- test-reviewer: OK → [logs/working/task-1/test-reviewer-2.json](logs/working/task-1/test-reviewer-2.json)

**Verification:**
- `venv/bin/python -m pytest tests/test_dedup_broad_precision.py -q` → 19 passed
- `venv/bin/python -m pytest -q` → 2063 passed, 2 skipped, 549 subtests passed
- JSON validation, scoped pre-commit hooks, and `git diff --check` → passed

## Task 2: Subject-aware pair rule and capped backstop

**Status:** Done
**Commit:** Pending (uncommitted working tree)
**Agent:** main agent
**Summary:** Broad E014 pairs now require their canonical series in both effective original titles, rejected candidates no longer terminate the scan, and only a backstop hard block reached after subject rejection is capped to `overlap_capped`. The same effective title is reused at the gate and persistence boundary, while distinctive E015 precedence, legacy handling, thresholds, deferral, and E016 fail-open behavior remain intact.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: 4 findings → [logs/working/task-2/code-reviewer-1.json](logs/working/task-2/code-reviewer-1.json)
- security-auditor: 2 findings → [logs/working/task-2/security-auditor-1.json](logs/working/task-2/security-auditor-1.json)
- test-reviewer: 5 findings → [logs/working/task-2/test-reviewer-1.json](logs/working/task-2/test-reviewer-1.json)

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-2/code-reviewer-2.json](logs/working/task-2/code-reviewer-2.json)
- security-auditor: OK → [logs/working/task-2/security-auditor-2.json](logs/working/task-2/security-auditor-2.json)
- test-reviewer: 1 minor finding → [logs/working/task-2/test-reviewer-2.json](logs/working/task-2/test-reviewer-2.json)

*Round 3 (after approved test fix):*
- test-reviewer: OK → [logs/working/task-2/test-reviewer-3.json](logs/working/task-2/test-reviewer-3.json)

**Verification:**
- `venv/bin/python -m pytest tests/test_dedup_broad_precision.py tests/test_model_extractor.py tests/test_integration.py -q` → 280 passed, 71 subtests passed
- `venv/bin/python -m pytest tests/test_dedup_broad_precision.py -q` after the Round 3 fix → 54 passed
- JSON validation, scoped pre-commit hooks, and `git diff --check` → passed

## Task 3: E014 reasons, suppression logs, and funnel telemetry

**Status:** Done
**Commit:** Pending (current Task 3 commit)
**Agent:** main agent
**Summary:** E014 now renders truthful reason-specific explanations for qualified broad subjects, ordinary overlap, and title-capped overlap. Subject-rejected comparisons produce bounded redacted one-line diagnostics, while E008/E009 expose one informational suppression count per affected article without changing dropped totals, review controls, rate limiting, or E015 behavior.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: OK → [logs/working/task-3/code-reviewer-1.json](logs/working/task-3/code-reviewer-1.json)
- security-auditor: OK → [logs/working/task-3/security-auditor-1.json](logs/working/task-3/security-auditor-1.json)
- test-reviewer: 4 findings → [logs/working/task-3/test-reviewer-1.json](logs/working/task-3/test-reviewer-1.json)

*Round 2 (after strengthening existing tests):*
- test-reviewer: OK → [logs/working/task-3/test-reviewer-2.json](logs/working/task-3/test-reviewer-2.json)

**Verification:**
- `venv/bin/python -m pytest tests/test_admin_alerts.py tests/test_no_token_leak_in_logs.py -q` → 192 passed, 17 subtests passed
- `venv/bin/python -m pytest tests/test_integration.py -q` → 154 passed, 71 subtests passed
- `venv/bin/python -m pytest -q` → 2112 passed, 2 skipped, 556 subtests passed
- JSON validation, scoped pre-commit hooks, and `git diff --check` → passed

## Task 4: Project Knowledge update

**Status:** Done
**Commit:** Pending (current Task 4 commit)
**Agent:** main agent
**Summary:** Durable project guidance now describes subject-qualified broad matching, non-terminal candidate suppression, qualified-pair selection, the unchanged overlap backstop with a capped hard-block result, all three E014 reasons, and article-unit suppression telemetry. Project-specific coding conventions and a two-to-four-week natural-traffic observation contract were added without changing code, tests, schema, toggles, review controls, or deployment automation.
**Deviations:** None. `user-spec.md` and `tech-spec.md` remain unchanged because the accepted contract did not change.

**Reviews:**

*Round 1:*
- code-reviewer: OK → [logs/working/task-4/code-reviewer-1.json](logs/working/task-4/code-reviewer-1.json)

**Verification:**
- Required-term search across the four Project Knowledge references → passed
- Obsolete-terminal-claim search and added-line secret/private-data scan → no matches
- JSON validation, scoped pre-commit hooks, and `git diff --check` → passed

## Task 9: Manual production deployment

**Status:** Done
**Commit:** 8dc916c741bddd1c797e6877399b1ec2615a453d
**Agent:** main agent
**Summary:** Deployed the subject-aware dedup build from `main` by fast-forward and Docker rebuild while preserving the existing database volume and runtime configuration. Both services are running; the plan-of-day and review-listener startup signals are present, with no E016, E018, duplicate-poller, startup, or schema errors in the bounded check.
**Deviations:** The original separate Tasks 5–8 were removed from the active roadmap under the mandatory minimum-scope rule. Existing task-level reviews and the green final `main` CI run were reused; deployment timing was not treated as a gate per the operator's updated rule.

**Reviews:**

- No new review round; existing completed review evidence was reused.

**Verification:**
- [deploy report](logs/working/deploy-report.json) → deployed commit, preserved state, and bounded startup checks recorded
- GitHub `main` CI for the deployed commit → passed
