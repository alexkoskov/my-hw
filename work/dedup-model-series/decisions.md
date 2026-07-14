# Decisions Log: dedup-model-series

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

## Task 1: Series/theme extraction + tier-tagged lexicon (model_extractor.py)

**Status:** Done
**Commit:** 6dee790
**Agent:** main agent (feature-execution lead) via implementation + fix teammates
**Summary:** Added tier-tagged `SERIES_LEXICON` (distinctive/broad, default broad) + a ReDoS-safe series/theme extraction pass; `extract_fingerprint` now returns `{strict, brands, series, pairs}` with keys `"<model>|<series>|<tier>"` (theme-only `"*|<series>|B"`). `|D` only when the series is lexicon-distinctive AND a concrete model is present; connector-word primary tokens (e.g. "Porsche de K-Pop") degrade to theme-only `|B` instead of a bogus `|D`, and `strict`/`brands`/`similarity()` are byte-unchanged.
**Deviations:** None material. Follow-ups deferred to the Task 7 audit: `extract_fingerprint` length (~89 physical / 42 executable lines); zamac matches only uppercase (fail-safe under-match); exact-key matching won't rescue a dupe when only one side names a valid model.

**Reviews:**

*Round 1:*
- code-reviewer: approved_with_suggestions (2 major, 4 minor) → [logs/working/task-1/code-reviewer-round1.json]
- test-reviewer: FAILED (1 critical, 1 major, 2 minor) → [logs/working/task-1/test-reviewer-round1.json]

*Round 2 (after fixes):*
- code-reviewer: approved_with_suggestions — non-blocking only → [logs/working/task-1/code-reviewer-round2.json]
- test-reviewer: approve — all findings mutation-verified resolved → [logs/working/task-1/test-reviewer-round2.json]

**Verification:**
- `pytest tests/test_model_extractor.py -k 'not calibration'` → 58 passed
- Smoke: 3 SDCC titles → expected series/pairs/tiers; ReDoS ~6.5ms on 51KB
- Calibration tests red by design this wave (Task 3 fixture / Task 6 rewires the harness)

## Task 2: Pair-aware [E015] + broad [E014] builders (admin_alerts.py)

**Status:** Done
**Commit:** 54b1fee
**Agent:** main agent via implementation + fix teammates
**Summary:** `alert_cross_source_blocked` ([E015]) renders the matched distinctive pair(s) + earlier link; `alert_cross_source_dupe` ([E014]) renders the matched series/theme for the broad tier. Backward-compatible via optional keyword-only `pairs` (legacy set-overlap call sites unchanged), plain-text (`parse_mode=None`), anchors «Заблокирован дубль»/«Похож на дубль» verbatim, and guarded so `Совпадение: None%` never renders.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approve (2 optional minor) → [logs/working/task-2/code-reviewer-round1.json]
- test-reviewer: changes_required (2 major, 1 minor) → [logs/working/task-2/test-reviewer-round1.json]

*Round 2 (after fixes):*
- test-reviewer: approve — 3 findings mutation-verified resolved → [logs/working/task-2/test-reviewer-round2.json]

**Verification:**
- `pytest tests/test_admin_alerts.py` → 39 passed
- `pytest -q -k 'not calibration'` → no regressions

## Task 3: Calibration fixture — 8 pairs (pair-tier verdict)

**Status:** Done
**Commit:** 47c7be8
**Agent:** main agent via implementation + fix teammates
**Summary:** Reworked `tests/fixtures/cross_source_dedup_pairs.py` to the pair-tier verdict: 4 `DUPE_PAIRS` (3 real SDCC dupes sharing `porsche 911|k-pop demon hunters|D` + the real Car Culture cross-source pair as soft-flag) + 4 `NON_DUPE_PAIRS`, each carrying `expected_verdict`/`expected_any_distinctive`/`expected_shared_pairs`. Label `pair-1-real-2026-06-03` preserved for the surviving must-pass test.
**Deviations:** Dropped legacy `expected_overlap_min/max` fields — verified no surviving test reads them (compliant with the task's "keep only where read" rule).

**Reviews:**

*Round 1:*
- test-reviewer: approve — 1 minor (PT diacritics) fixed → [logs/working/task-3/test-reviewer-round1.json]

**Verification:**
- Structural self-check → `fixture OK 4 4`
- Real `extract_fingerprint`+`shares_pair` over all 8 pairs → 8/8 verdicts / any_distinctive / shared_pairs match
- Old `test_calibration_accuracy` red 4/8 by design (Task 6 rewires); `test_calibration_real_pair_must_pass` green

## Task 4: Toggle + tiered pair-rule gate refactor (news_bot.py)

**Status:** Done
**Commit:** 2d5d2e0
**Agent:** main agent via implementation + fix teammates
**Summary:** Wired the new (model+series) tiered pair rule into `_check_cross_source_dedup` on the publish path — pair rule FIRST (30d, any-source, scan-and-remember: distinctive `|D` → block/`[E015]`, broad `|B` → soft-flag/`[E014]`, both terminal) → empty short-circuit re-gated to "strict AND series both empty" → existing set-overlap backstop (7d, cross-source) only on pass. Single 30d fetch; 7d subset derived in Python. Toggle `DEDUP_SERIES_ENABLED` (env==const, default on). Split into `_pair_rule_verdict`/`_set_overlap_backstop_verdict` helpers.
**Deviations:** Toggle parse drops the trailing `""` off-word so blank/unset → ON (matches AC6; the task's impl-hint tuple would have made blank → OFF — deliberately corrected, do NOT revert). The "orangetrack flake" raised in review was a misdiagnosis: `_IntegrationBase` already excludes orangetrack from `SOURCES`; the redundant per-class `SOURCES` patch was removed because it leaked test state.

**Reviews:**

*Round 1:*
- security-auditor: approved, 0 findings → [logs/working/task-4/security-auditor-round1.json]
- code-reviewer: approved_with_suggestions (1 major, 2 minor) → [logs/working/task-4/code-reviewer-round1.json]
- test-reviewer: needs_improvement (1 major, 2 minor) → [logs/working/task-4/test-reviewer-round1.json]

*Round 2 (after fixes):*
- code-reviewer: approved — refactor behavior-preserving → [logs/working/task-4/code-reviewer-round2.json]
- test-reviewer: changes_required — 7d-backstop boundary unpinned → [logs/working/task-4/test-reviewer-round2.json]

*Round 3 (after fixes):*
- test-reviewer: changes_required — redundant SOURCES patch leaked state → [logs/working/task-4/test-reviewer-round3.json]; resolved by lead (patch removed; SOURCES-restore verified; 17 + 30×3 green)

**Verification:**
- `pytest tests/test_integration.py::TestCrossSourceDedup -v` → 17 passed
- `pytest tests/test_integration.py -q` ×3 → 30 passed (stable); SOURCES-leak check → restored True
- `pytest -q -k 'not calibration'` → 1238 passed

## Task 5: Widened backfill re-select for the pairs key (backfill_fingerprints.py)

**Status:** Done
**Commit:** f4217fd
**Agent:** main agent via implementation + fix teammates
**Summary:** Backfill re-selects rows missing the new `$.pairs` key (NULL, corrupt, or old 2-key fingerprint), not only `IS NULL`, guarded by `CASE WHEN json_valid(...)` so a malformed blob is reprocessed instead of crashing the eager `fetchall`. 4-key empty marker matches `extract_fingerprint`; parse-then-probe skip-guard via `_already_backfilled`; idempotent. `json_extract('$.pairs')` static literal, `--days` the only bound param.
**Deviations:** None. (Non-blocking: `backfill_one` 63 lines → deferred to Task 7 audit.)

**Reviews:**

*Round 1:*
- code-reviewer: approved_with_suggestions (2 major, 1 minor) → [logs/working/task-5/code-reviewer-round1.json]
- test-reviewer: needs_improvement (1 major, 2 minor) → [logs/working/task-5/test-reviewer-round1.json]

*Round 2 (after fixes):*
- code-reviewer: approved — SQL-crash fix verified, regression-pinned → [logs/working/task-5/code-reviewer-round2.json]

**Verification:**
- `pytest tests/test_backfill_fingerprints.py` → 19 passed
- `pytest -q -k 'not calibration'` → no regressions

## Task 6: Pair-tier calibration test + deploy runbook (test_model_extractor.py, deployment.md)

**Status:** Done
**Commit:** f7db210
**Agent:** main agent via implementation teammate (first attempt died on a transient API error mid-run; reverted the partial docstring edit and restarted clean)
**Summary:** Replaced the old `_classify(similarity())` calibration harness with a pair-tier harness on `shares_pair` — aggregate ≥7/8 (8/8) plus two SEPARATE non-vacuous hard invariants (3 SDCC dupes must hard-block; no not-dupe may). Old calibration removed; `TestSimilarity` untouched. The whole suite is now green (`pytest -q` → 1241, no exclusions). Added the feature rollout runbook to `deployment.md` (cold-DB pre-check → dark deploy `DEDUP_SERIES_ENABLED=0` → `backfill --days 30` → observe → toggle on, all outside the 10:00–20:00 МСК window).
**Deviations:** None of substance (the import already carried `shares_pair` from Task 1, so no import edit was needed).

**Reviews:**

*Round 1:*
- test-reviewer: approve, 0 findings → [logs/working/task-6/test-reviewer-round1.json]

**Verification:**
- `pytest tests/test_model_extractor.py -v` → 61 passed (incl. calibration)
- `pytest -q` (whole suite, no `-k`) → 1241 passed — calibration green, no regressions
- Runbook facts verified against code (column, toggle, service/container, paths, `[1,90]` clamp)
