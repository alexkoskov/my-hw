# Test Audit — cross-source-dedup (Task 8)

**Auditor:** test-master (read-only test-source audit)
**Date:** 2026-06-06
**Scope:** Test SOURCE CODE only (not test execution — that is Task 9 / Pre-deploy QA).
**Contract audited against:** `work/cross-source-dedup/tech-spec.md` Testing Strategy (lines 277-324).

---

## Executive Summary

The cross-source-dedup test base is **strong and deploy-ready**. All 7 integration gate scenarios are present and assert real system behavior (DB state + admin-ping introspection, not mock wiring); all 4 dedup-gate branches (block / flag / pass / degraded) plus within-source and pass-through-with-non-empty-fp are covered. The calibration must-pass split, schema-pin tests, JSON-roundtrip pin, and both rate-limit window-expiry tests (E016 + per-pair) all use freeze-by-backdated-`bot_state` (no `time.sleep`) and verify the real mechanism. Two non-blocking gaps: the calibration accuracy test asserts via verdict-string but the must-pass test asserts the raw threshold `sim >= 0.50` instead of the `'block'`/`'duplicate'` verdict (semantically weaker than the AC asks), and there is no direct `assert 'model_fingerprint' in _PENDING_JSON_COLS` unit guard (the constant is pinned only indirectly via roundtrip). Final verdict: **PASS WITH NOTES**.

---

## Findings by Severity

### Critical
None.

### High
None.

### Medium

#### M1 — Must-pass split asserts threshold (`sim >= 0.50`), not the decision verdict `'block'`/`'duplicate'`
- **File:** `tests/test_model_extractor.py:363-378`
- **Quote:**
  ```python
  def test_calibration_real_pair_must_pass():
      pair = next(p for p in DUPE_PAIRS if p['label'] == 'pair-1-real-2026-06-03')
      fp_a = extract_fingerprint(pair['a'])
      fp_b = extract_fingerprint(pair['b'])
      sim = similarity(fp_a, fp_b)
      assert sim >= 0.50, (
          f"Real 2026-06-03 pair must classify as duplicate "
          f"(similarity ≥ 0.50). Got: {sim:.3f}. "
  ```
- **Risk:** Task AC (dimension 2.b) explicitly requires this guard to assert the *decision* semantic (`'block'`/`'duplicate'`), not the numeric threshold, because "порог может уехать, нужна именно семантика решения." If the block threshold is later tuned (e.g. raised to 0.55) the `_classify` boundary moves but this test keeps asserting the hardcoded `0.50`, so it would silently stop tracking the actual production block decision. The companion `test_calibration_accuracy` already routes through `_classify(sim)` → verdict string, so the verdict path exists and is trivially reusable here.
- **Recommendation:** Change the assertion to go through the classifier and assert the verdict, e.g. `assert _classify(sim) == 'duplicate'` (and keep `sim` in the failure message for diagnostics). This couples the must-pass guard to the same decision boundary the gate uses, satisfying AC dimension 2.b literally.

#### M2 — No direct `assert 'model_fingerprint' in _PENDING_JSON_COLS` unit guard
- **File:** `tests/test_pending_articles_repo.py:959-972` (the indirect roundtrip pin) — the direct guard is **absent** anywhere in `tests/`.
- **Quote (the only existing pin — indirect):**
  ```python
  def test_insert_pending_with_fingerprint_roundtrip(self):
      """A dict fingerprint round-trips through insert/get as a dict —
      pins ``_PENDING_JSON_COLS`` registration against typos."""
      ...
      self.assertEqual(row['model_fingerprint'], fp)
      # CRITICAL: must be a dict not a str — that's what guards
      # _PENDING_JSON_COLS membership.
      self.assertIsInstance(row['model_fingerprint'], dict)
  ```
- **Verification of source:** `pending_articles_repo.py:133-134` — `_PENDING_JSON_COLS = ('paragraphs', 'images', 'blocks', 'ru_paragraphs', 'ru_blocks', 'model_fingerprint')`. The string literal `'model_fingerprint'` **is** present in the tuple (not merely the identifier referenced elsewhere). `_PUBLISHED_JSON_COLS = ('model_fingerprint',)` at line 138.
- **Risk:** The roundtrip test (M2's quote) is a real behavioral pin and would catch a dropped tuple element via type/value mismatch — so this is **not** a coverage hole, only a missing low-cost defense-in-depth guard. Task dimension 6 explicitly says: "Если такого теста нет — finding severity medium с рекомендацией добавить." The risk it addresses: a developer removing `'model_fingerprint'` from the tuple would be caught by the DB-backed roundtrip, but a fast direct unit assert localises the failure to the constant immediately (cheaper to diagnose) and survives even if the roundtrip test is refactored to use a mock.
- **Recommendation:** Add a one-line guard:
  ```python
  from pending_articles_repo import _PENDING_JSON_COLS, _PUBLISHED_JSON_COLS
  def test_model_fingerprint_registered_in_json_cols():
      assert 'model_fingerprint' in _PENDING_JSON_COLS
      assert 'model_fingerprint' in _PUBLISHED_JSON_COLS
  ```

### Low

#### L1 — `test_threshold_block_50_percent` and `test_threshold_flag_30_percent` carry dead scratch-work reassignments
- **File:** `tests/test_model_extractor.py:252-315`
- **Quote (excerpt):**
  ```python
  def test_threshold_block_50_percent(self):
      a = {...}; b = {...}
      assert similarity(a, b) == 1.0  # sanity — same set
      # Now actual 0.50:
      a = {...}; b = {...}
      # ... two more reassignments of a/b ...
      assert similarity(a, b) == 0.5
  ```
- **Risk:** Only the final `a`/`b` binding before each `assert` is meaningful; the earlier reassignments and inline arithmetic comments are author scratch-work left in the test body. No correctness impact (the final assert is valid and non-tautological), but the intermediate `assert similarity(a, b) == 1.0` mid-function is a second hidden assertion that obscures the test's stated single concern and would confuse a future reader about which case the test name describes.
- **Recommendation:** Trim each test to a single `a`/`b` pair matching the docstring boundary; move the derivation arithmetic to a one-line comment. Non-blocking.

#### L2 — `test_mixed_brands_single_body` contains a near-tautological disjunction
- **File:** `tests/test_model_extractor.py:126-133`
- **Quote:**
  ```python
  fp = extract_fingerprint(_article(title='Boulevard Mix — Camaro Z28, Mustang Boss, Datsun 510'))
  assert 'chevrolet' in fp['brands'] or 'chevy' not in fp['brands']
  assert 'datsun 510' in fp['strict']
  ```
- **Risk:** The first assertion `'chevrolet' in fp['brands'] or 'chevy' not in fp['brands']` is true in almost every realistic universe (it only fails if the extractor emits the alias `'chevy'` un-normalised while *also* dropping `'chevrolet'`), so it adds little protective value. The second assertion (`'datsun 510'`) is the real, meaningful check and carries the test. Litmus: removing the brand-extraction logic for Chevrolet would not reliably fail the first assert.
- **Recommendation:** Replace the disjunction with the intended invariant: `assert 'chevrolet' in fp['brands']` (the alias must normalise). Keep the Datsun assert. Non-blocking — the test is not harmful, just weak on one line.

### Info

#### I1 — Calibration accuracy floor (≥7/8) is intentionally below the user-spec AC12 target (≥95%)
- **File:** `tests/test_model_extractor.py:332-360`
- **Note:** This is by design (Decision 13 — the floor is the gating threshold, the must-pass split protects the load-bearing pair from the 1-misclassification budget). Documented in the docstring. No action; recorded for traceability.

#### I2 — Calibration fixture brand+model tokens are externally grounded (non-tautological)
- **File:** `tests/fixtures/cross_source_dedup_pairs.py:9-28, 83-88, 255-257, 289-291, 361-363`
- **Note:** Each pair carries a human-readable `note` rationale; Pair 1 documents the real 2026-06-03 origin and the Cloudflare-block caveat (autoevolution side synthesised). Labels are justified by the prose, not back-fitted to extractor output. No action.

---

## PASS Dimensions (7 of 7)

### ✓ Dimension 1 — Calibration test meaningfulness / non-tautology
`test_calibration_accuracy` (`test_model_extractor.py:332-360`) loads the fixture, runs the **real** `extract_fingerprint` + `similarity` + a `_classify` thresholder (`:323-329`), and compares the resulting verdict against each pair's `expected_verdict`. The expected verdicts are externally grounded: every fixture pair carries a `note` with a human-readable rationale (`cross_source_dedup_pairs.py:83-88, 130-131, 169, 210-213, 255-257, 289-291, 326, 361-363`), and Pair 1 cites the real 2026-06-03 origin. Labels are *not* derived from "whatever the code outputs" — they encode brand+model overlap reasoning a human can verify. **Non-tautological. ✓**

### ✓ Dimension 2 — Calibration must-pass split asserts the real 2026-06-03 pair classifies as block
`test_calibration_real_pair_must_pass` (`test_model_extractor.py:363-378`) exists as a **separate function**, selects Pair 1 by label `'pair-1-real-2026-06-03'`, runs the real extractor + similarity, and asserts the pair clears the block threshold. It is **not** masked by the 1-misclassification budget in `test_calibration_accuracy` because it is an independent assertion on the load-bearing pair. Pair 1 is annotated as the real pair with source rationale (`cross_source_dedup_pairs.py:38-89`). **Present and effective. ✓** *(Note M1: the assertion is on `sim >= 0.50` rather than the `'duplicate'` verdict string — passes the dimension but flagged Medium for AC literalism.)*

### ✓ Dimension 3 — Integration coverage of all 4 gate branches + within-source + pass-through-with-non-empty-fp (7 scenarios)
`TestCrossSourceDedup` (`test_integration.py:868-1328`). All 7 scenarios present and asserting real behavior:

| # | Scenario | Test | Line | Branch | ✓/✗ |
|---|----------|------|------|--------|-----|
| 1 | Hard-block (100% overlap) | `test_hard_block_path` | 947 | **block** | ✓ |
| 2 | Pass-through with non-empty fp | `test_pass_through_with_non_empty_fp` | 1002 | **pass** | ✓ |
| 3 | Soft-flag (35-40% overlap) | `test_soft_flag_path` | 1044 | **flag** | ✓ |
| 4 | Soft-flag rate-limit | `test_soft_flag_rate_limited` | 1123 | flag (rate-limit) | ✓ |
| 5 | Within-source dedup (AC7) | `test_within_source_dedup` | 1179 | block (no source filter) | ✓ |
| 6 | Empty fingerprint | `test_empty_fingerprint` | 1232 | pass (early return) | ✓ |
| 7 | Degraded mode | `test_degraded_mode` | 1280 | **degraded** | ✓ |

All 4 gate branches: **block** (#1, #5), **flag** (#3), **pass** (#2, #6), **degraded** (#7) — covered. Within-source (#5) and pass-through-with-non-empty-fp (#2) — covered. Each test asserts on real DB state (`count_pending`, `get_pending(...).model_fingerprint`, `processed_news` SELECT) and introspects the **real** admin-ping mock's `call_args_list` for the exact `[E014]/[E015]/[E016]` code and count (e.g. `:992-996, 1108-1117, 1308-1313`). **All 7 ✓.**

### ✓ Dimension 4 — TestFingerprintCarryThrough asserts dict equality post-roundtrip
`test_pending_to_published_roundtrip` (`test_integration.py:1337-1381`) inserts a pending row with a dict `model_fingerprint`, stages it, calls `move_to_published`, then SELECTs the raw column from `published_articles` and asserts `json.loads(stored_raw[0]) == fp` (`:1379-1381`). It compares the **deserialised dict object** to the input dict (not a raw JSON-string compare), so it pins the carry-through AND the JSON encoding shape. Source confirms `move_to_published` carries the column (`pending_articles_repo.py:617, 631-635`). **✓** *(The published side is JSON-decoded explicitly via `json.loads` rather than via `_PUBLISHED_JSON_COLS` auto-deserialize, which is fine — the dict-equality contract is met.)*

### ✓ Dimension 5 — Schema-pin tests catch a missing/wrong column
`tests/test_migration.py::test_pending_articles_has_expected_columns` (`:150-164`) and `::test_published_articles_has_expected_columns` (`:166-182`) assert the **full column set** equality (`set(actual) == set(EXPECTED_*)`) AND per-column `{type, notnull, dflt_value, pk}` shape. `EXPECTED_PENDING_COLUMNS`/`EXPECTED_PUBLISHED_COLUMNS` both include `model_fingerprint` (`:62, 79`). `tests/test_pending_articles_repo.py::test_pragma_table_info_matches_spec` (`:186-217`) does the same with `EXPECTED_PENDING`/`EXPECTED_PUBLISHED` (`:52, 65`), plus dedicated `TestCrossSourceDedupSchemaPin` (`:934-949`).
**Litmus:** removing `"ALTER TABLE pending_articles ADD COLUMN model_fingerprint TEXT"` (`pending_articles_repo.py:211`) or the published equivalent (`:212`) means the column is never created → `set(actual) != set(expected)` → both migration tests fail on the column-set assertion. The test pins the column, it does not merely echo "whatever was found." **✓**
Double-init idempotency: `test_migration.py::test_init_schema_idempotent` (`:184-207`) calls `init_schema(conn)` twice on the same connection and asserts no raise + both columns present — confirms the Decision 11 per-statement `try/except sqlite3.OperationalError` block (`pending_articles_repo.py:213-219`). Also `test_pending_articles_repo.py::test_init_schema_is_idempotent` (`:154-184`) proves re-init is non-destructive (row counts preserved). **✓**

### ✓ Dimension 6 — JSON-column roundtrip pins `_PENDING_JSON_COLS` containing the string `'model_fingerprint'`
`test_insert_pending_with_fingerprint_roundtrip` (`test_pending_articles_repo.py:959-972`): `insert_pending` writes a dict, `get_pending` returns it, asserts `row['model_fingerprint'] == fp` AND `assertIsInstance(row['model_fingerprint'], dict)`. The `isinstance dict` check is the load-bearing pin: if `'model_fingerprint'` were dropped from `_PENDING_JSON_COLS`, `_row_to_dict` would return the raw JSON **string** and the isinstance assert fails — exactly the silent-typo regression dimension 6 targets. Empty-fp distinct-from-NULL roundtrip (`:974-986`) and backward-compat NULL (`:988-997`) round out the path.
**Source verification (per AC):** `pending_articles_repo.py:133-134` — the tuple **literally contains the string value** `'model_fingerprint'` (element of the tuple, not merely an identifier mentioned elsewhere): `_PENDING_JSON_COLS = ('paragraphs', 'images', 'blocks', 'ru_paragraphs', 'ru_blocks', 'model_fingerprint')`; `_PUBLISHED_JSON_COLS = ('model_fingerprint',)` at `:138`. **✓** *(Note M2: no direct `assert 'model_fingerprint' in _PENDING_JSON_COLS` unit test — recommended as a Medium-severity low-cost addition.)*

### ✓ Dimension 7 — Mocks don't leak into assertions
Audited every patched test in `TestCrossSourceDedup` and `test_backfill_fingerprints.py`. No instance of the `mock.return_value = X; assert mock(...) == X` anti-pattern. Mocks are used to **drive inputs** (`fetch_rss` / `fetch_full_article` / `load_feeds` return article streams) and assertions are on **system effects**: `count_pending()`, `get_pending(...).model_fingerprint`, `processed_news` SELECT, and filtered `mock_admin.call_args_list` (asserting the alert *content* `[E0XX]` and *count* the production code passed to `send_admin_notification`, which is real behavior — not the mock's own return).
**Degraded-mode test** (`test_integration.py:1280-1328`) — the special-attention case: `extract_fingerprint` patched with `side_effect=RuntimeError("boom")`; assertions verify (a) article inserted with `model_fingerprint` **None** (`:1300-1305`), (b) exactly one E016 with `'RuntimeError'` in the body (`:1308-1313`), (c) second `job()` within the hour fires **zero** E016 (`:1317-1326`), (d) second article still published (`:1328`). This asserts the **real degraded behavior**, not `mock.assert_called()`. **✓**
Backfill tests assert persisted DB state (`_get_fp_raw`) and summary-counter strings, not mock returns. The extractor is deliberately **not** mocked in backfill (`test_backfill_fingerprints.py:32-36`) so the real extractor+repo wiring is exercised. **✓**

---

## Additional AC checks

### ✓ E016 window-expiry uses freeze/backdate, not `time.sleep`
`test_pending_articles_repo.py::TestDedupDegradedRateLimit` — `test_degraded_rate_limit_within_hour_true` (`:1244-1251`): mark → assert `is_dedup_degraded_rate_limited() is True`. `test_degraded_rate_limit_after_hour_false` (`:1253-1268`): mark → backdate the `bot_state` row to `'2020-01-01T00:00:00+00:00'` via direct UPDATE → assert `is_dedup_degraded_rate_limited() is False`. Full 3-step (mark → within-window True → fast-forward → False). **No `time.sleep`** (grep-confirmed: the only `time.sleep` reference in the dedup tests is the *no-op monkeypatch* in `test_backfill_fingerprints.py:76`). The integration-level expiry is additionally exercised in `test_degraded_mode`'s within-hour rate-limit assert. **✓**

### ✓ Per-pair soft-flag rate-limit independence + window-expiry
`TestPairRateLimit` (`:1162-1233`): `test_pair_rate_limit_independent_pairs` (`:1207-1216`) — `mark_pair_pinged(A)` does NOT rate-limit `is_pair_rate_limited(C, D)`. `test_pair_rate_limit_within_window_true` (`:1174-1183`) + `test_pair_rate_limit_after_window_false` (`:1185-1205`, backdate to 2020 via direct `bot_state` UPDATE keyed on `'softflag_pair:...\n...'`) give the full mark → True → fast-forward → False cycle. Plus corrupted-timestamp tolerance (`:1218-1233`). **✓**

### ✓ Fixture has all 8 pairs in correct categories
`cross_source_dedup_pairs.py` — `DUPE_PAIRS` (4): Pair 1 real 2026-06-03 (`:42-89`), Pair 2 boulevard-mix synthetic (`:90-132`), Pair 3 pop-culture synthetic (`:133-170`), Pair 4 jdm-premium-partial 0.50-boundary synthetic (`:171-213`). `NON_DUPE_PAIRS` (4): Pair 5 AC8 same-brand-different-model (`:222-258`), Pair 6 1-token guard probe (`:259-292`), Pair 7 different-series-same-brand (`:293-327`), Pair 8 industry-news vs car-review / AC6 empty-fp probe (`:328-364`). All 8 present, categories match tech-spec line 304 and task Details lines 118-124. **✓**

---

## Final Verdict

# PASS WITH NOTES

**Rationale:** Zero critical, zero high findings. All 7 audit dimensions pass; all 7 integration scenarios and all 4 gate branches are covered with behavioral (non-tautological, non-mock-leaking) assertions; the must-pass split, schema-pins, JSON-roundtrip pin, and both rate-limit window-expiry tests are present and verify the real mechanism via backdated `bot_state` (no `time.sleep`). The two Medium findings (M1: must-pass asserts the numeric threshold instead of the decision verdict; M2: missing the direct `assert 'model_fingerprint' in _PENDING_JSON_COLS` guard) are **optional hardening**, not regressions — the underlying behaviors are already protected by adjacent tests. The test base is **cleared for Pre-deploy QA (Task 9)**.

**Recommendation to orchestrator:** Proceed to Task 9. M1 and M2 may be folded into Task 9 scope as quick test-hardening edits (≈5 lines total) or deferred — neither blocks deploy. L1/L2 are cosmetic and can be left as-is.
