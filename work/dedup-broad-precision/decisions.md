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
