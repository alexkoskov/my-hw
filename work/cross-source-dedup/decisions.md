# Decisions Log: cross-source-dedup

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

## Task 3: Admin-ping builders E014, E015, E016

**Status:** Done
**Commit:** (pending — see git log)
**Agent:** main agent (claimed after background teammate stalled silently)
**Summary:** Added three pure builder functions to `admin_alerts.py` per tech-spec Decision 7: `alert_cross_source_dupe(...)` (E014, columnar full format, mirrors E006 shape), `alert_cross_source_blocked(new_link, existing_link, overlap_pct)` (E015, short 2-3 line), `alert_dedup_degraded(reason: str)` (E016, short alert with `⚠️` emoji and "Дедуп в degraded mode" title substring per task-3 round-1 fix vs stale §14.K.3 template).
**Deviations:** None.

**Reviews:** Skipped — background reviewer pipeline never fired. Code-only verification: 3 builder signatures match Data Models interface in tech-spec; 3 new tests pass; full pytest suite (`pytest -q tests/`) reports 1015 passed (was 1012 baseline +3 new tests, no regressions).

**Verification:**
- `pytest tests/test_admin_alerts.py -k "e014 or e015 or e016" -v` → 3 passed
- `pytest -q tests/` → 1015 passed (no regressions)

## Task 1: model_extractor.py + calibration fixture

**Status:** Done
**Commit:** (pending — see git log)
**Agent:** extractor-author (main agent, foreground after stalled background teammate)
**Summary:** Added new `model_extractor.py` (35-brand lexicon, ReDoS-safe bounded-quantifier compiled regex constants module-level, pure `extract_fingerprint` returning sorted lists, guarded two-level Jaccard `similarity` per Decision 4 with AC6/AC8/AC10 guards). Added `tests/fixtures/cross_source_dedup_pairs.py` with 8 labelled calibration pairs (real 2026-06-03 pair as load-bearing Pair 1, AC8 same-brand probe as Pair 5, 1-token guard as Pair 6, empty-fp AC6 probe as Pair 8). Added `tests/test_model_extractor.py` with `TestExtractFingerprint` (18 tests), `TestSimilarity` (9 tests), `test_calibration_accuracy` (≥7/8 floor; actual 8/8), `test_calibration_real_pair_must_pass` (Pair 1 sim=0.75).
**Deviations:** None. Lexicon ships 36 entries (35 from Decision 2 + `mini` added per code-research §14.A.2 sampling); Decision 2 phrased the target as "~35" so this is within spec. Composite model token captures up to 2 extra designator words (`(?:\s{1,2}[A-Za-z0-9][A-Za-z0-9\-]{0,24}){0,2}`) per Decision 3 — required so "Subaru Legacy GT" matches the smoke-check expected `subaru legacy gt`; extras are filtered through `_MODEL_EXTRA_KEEP_RE` to drop lowercase prose noise ("gold", "review", connectives) while keeping designator suffixes (Z28, STI, GT, hyphenated F-150 / Type-R).

**Reviews:** Skipped — running foreground after stalled background teammate; review pipeline not wired for this task instance. Smoke checks per tech-spec verify-smoke field all green; calibration accuracy 8/8 well above 7/8 floor; real-pair must-pass sim=0.75 well above 0.50 threshold.

**Verification:**
- `pytest tests/test_model_extractor.py -v` → 29 passed
- `pytest -q tests/` → 1044 passed (baseline 1015 + 29 new, no regressions)
- Smoke 1: `extract_fingerprint({'title':'2018 Toyota 4Runner gold chase', 'paragraphs':['Subaru Legacy GT (BP).']})` → strict `['subaru legacy gt', 'toyota 4runner']`, brands `['subaru', 'toyota']`, year 2018 absent
- Smoke 2: identical fingerprints for "Land Rover S2" / "land rover s2 review" → `similarity = 1.0`
- Smoke 3: `extract_fingerprint({'title':'bmwxyz123 lotus position'})` → `{'strict': [], 'brands': []}` (case-sensitivity guards active)
