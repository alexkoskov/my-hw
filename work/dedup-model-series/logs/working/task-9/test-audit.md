# Test Audit — `dedup-model-series`

- **Feature:** dedup-model-series (Hot Wheels news bot; pair-rule «машина + серия/тема» + тиринг)
- **Task:** 9 — Test Audit (holistic test-quality-and-coverage audit; analysis only, no code/tests changed)
- **Date:** 2026-07-14
- **Auditor:** Task 9 test-master agent
- **Scope:** `tests/test_model_extractor.py`, `tests/fixtures/cross_source_dedup_pairs.py`, `tests/test_admin_alerts.py`, `tests/test_integration.py::{TestCrossSourceDedup,TestFingerprintCarryThrough}`, `tests/test_backfill_fingerprints.py`, `tests/test_pending_articles_repo.py::TestSqlAudit`; source: `model_extractor.py`, `news_bot.py`, `admin_alerts.py`, `backfill_fingerprints.py`.

## Verdict: **PASS (pass-with-findings)**

- **critical: 0 · high: 0 · medium: 1 · low: 4**
- The critical axis is satisfied: the calibration harness runs the **NEW** classifier (`shares_pair` / `any_distinctive` / `|D` tier), not `_classify(similarity(...))`. No old-classifier calibration remains anywhere in the tree.
- Both asymmetric hard invariants (3 SDCC-must-block / not-dupes-must-not-block) are pinned as **separate** tests, out of the ≥7/8 aggregate budget, and are non-vacuous (empty-selection guards present).
- The two behaviour-reversal tests both genuinely pin behaviour (positive same-source-block case present; negative same-source-no-series-publishes half retained).
- Grounding run: `pytest tests/test_model_extractor.py tests/test_admin_alerts.py TestCrossSourceDedup TestFingerprintCarryThrough tests/test_backfill_fingerprints.py -q` → **138 passed**. Fixture self-consistency probe vs the real extractor → **8/8** verdict + `any_distinctive` + concrete `shared_pairs` all match.

The single medium is a hardening gap in the not-dupe invariant's selector (not an active bug); everything else is low/informational (doc drift, an already-tech-spec-noted audit-scope gap, one un-documented accepted limitation).

---

## Per-axis status

### Axis 1 — Calibration meaningfulness (most important) · **COVERED**
- Harness `_pair_tier_verdict` (`test_model_extractor.py:359`) calls the **real** `extract_fingerprint` + `shares_pair` and reads the verdict straight off the `|D`/`|B` suffix (`any_distinctive`). No mock, no hard-coded verdict.
- `grep -nE "shares_pair|any_distinctive|_classify|similarity\(" tests/test_model_extractor.py` → the only `similarity(` hits are in `TestSimilarity` (the shipped-Jaccard unit tests, lines 219–338); **no `_classify` anywhere**. Repo-wide grep confirms `test_calibration_accuracy` / `test_calibration_real_pair_must_pass` no longer exist (only unrelated `_classify_exception` in transcreation modules).
- Asymmetric invariants pinned separately and non-vacuously:
  - `test_calibration_sdcc_dupes_hard_block` (`:411`) — selects the 3 `expected_any_distinctive` dupes, **guards `len(...) == 3`** (blocks a vacuous empty loop), asserts `any_distinctive is True` each via the real path.
  - `test_calibration_not_dupes_never_hard_block` (`:436`) — guards `NON_DUPE_PAIRS` non-empty, asserts `any_distinctive is False` for all 4 probes.
  - `test_calibration_pair_tier_accuracy` (`:384`) — aggregate ≥7/8 (currently 8/8), 1-error budget explicitly documented.
- Fixture N = **8** (4 dupe + 4 not-dupe) confirmed in `cross_source_dedup_pairs.py` (`DUPE_PAIRS` 4, `NON_DUPE_PAIRS` 4). Empirical extractor run: labels/`expected_verdict`/`expected_any_distinctive`/`expected_shared_pairs` are all self-consistent (8/8, exact shared-pair sets match).
- False-green risk (pop-culture dupes with empty `strict` scored ~0 by the old Jaccard) is closed — pair-2/3/4 hard-block via the pair rule despite empty car-set on the themed side.
- **Finding:** M-1 (pair-1 broad-dupe "must-not-block" only budget-pinned).

### Axis 2 — Tier / fail-safe coverage · **COVERED**
- Untagged/unknown → broad: `test_default_tier_is_broad` (`:468`, pins the constant) **and** `test_unrecognized_tier_defaults_to_broad` (`:473`) exercises the real `_tier_suffix`/`_build_pairs` path (mutation-closing: deleting the `.get(..., default)` fallback fails it).
- Distinctive requires tagged-distinctive **AND** concrete model: `test_distinctive_requires_concrete_model` (`:599`), `test_theme_only_pair_is_broad` (`:589`), `test_extract_fingerprint_theme_only_when_no_model` (`:640`).
- `|D` wins over `|B` order-independently: unit `TestSharesPair::test_distinctive_wins_over_broad` (`:672`) + integration `test_distinctive_wins_over_broad` (`test_integration.py:1107`, asserts **both** candidate seeding orders).
- Load-time pair-key integrity mirrored by `test_canonicals_have_no_pipe_or_newline` (`:460`).

### Axis 3 — No mock-leak · **COVERED**
- Calibration → real `extract_fingerprint`; integration → real `news_bot.job()` / `_check_cross_source_dedup` / real SQLite DB (only I/O boundaries mocked: `fetch_rss`, `load_feeds`, `fetch_full_article`, `send_admin_notification`). These are integration tests with the unit-under-test real, so 4 boundary mocks is appropriate, not over-mocking.
- `test_empty_fingerprint` (`:1807`) updated to the **4-key** mock shape `{strict, brands, series, pairs}` (was the stale 2-key form) — AC8 confirmed.
- The two deliberate extractor mocks (`test_empty_fingerprint` forces both-empty; `test_degraded_mode` forces `RuntimeError`) are shape/behaviour forcing, not verdict-leaking.

### Axis 4 — Two behaviour-reversal tests actually pin behaviour · **COVERED**
- Any-source pair loop (positive): `test_within_source_distinctive_pair_blocks` (`:1743`) — same-source shared `porsche 911|k-pop demon hunters|D` → real hard block, link in `processed_news`, exactly one `[E015]` rendered via the pair builder (`Совпавшие пары`). Proves the same-source-skip branch is removed from the pair loop.
- Split `test_within_source_not_deduped` — negative half retained as `test_within_source_no_series_publishes` (`:1673`): same-source + **no series** at 100% car overlap → still publishes, no `[E014]`/`[E015]`. Proves the backstop stays cross-source-only (2026-06-14 reversal not undone).

### Axis 5 — Backstop-blocks positive case · **COVERED**
- `test_pair_pass_falls_through_to_backstop_block` (`:1176`) — pair rule passes (same cars, different series → no shared pair), backstop **really blocks** the 100% cross-source overlap: `count_pending()==0`, link in `processed_news`, one `[E015]` with legacy `Совпадение:` signature (`assertNotIn('Совпавшие пары')`). Not a vacuous "the rule ran".
- Same test also pins the single-fetch guarantee (`m_pend.assert_called_once()` / `m_pub.assert_called_once()` — the one scenario reaching both rules).
- 7-day boundary pinned both edges: `test_backstop_excludes_candidate_older_than_7_days` (10d → publishes) and `test_backstop_includes_candidate_within_7_days` (3d → blocks).

### Axis 6 — Terminal verdict · **COVERED**
- `test_broad_pair_soft_flag_is_terminal` (`:1045`) — broad seed also shares the **full** car fingerprint (backstop would 100%-block if it ran); asserts exactly one `[E014]`, **no** `[E015]`/`[E016]` → proves the flag is terminal and the backstop does not double-fire.
- `test_theme_only_pop_culture_flags_no_model` (`:1910`) — real-extraction theme-only soft-flag, one `[E014]`, terminal.

### Axis 7 — AC2 carry-through · **COVERED**
- `TestFingerprintCarryThrough::test_pending_to_published_roundtrip_with_pairs` (`:2098`) — real repo `insert_pending`→`update_staged`→`move_to_published`, then direct SELECT on `published_articles.model_fingerprint`, `assertEqual(json.loads(...), fp)` byte-for-byte on the **4-key** shape. Legacy 2-key roundtrip also retained (`:2052`).

### Axis 8 — AC8 both-empty · **COVERED**
- `test_empty_fingerprint` (`:1807`) — no strict AND no series → publishes, stores `{strict:[],brands:[],series:[],pairs:[]}`, no pings.
- Companion re-gate pin `test_theme_only_pop_culture_not_short_circuited` (`:1976`) — empty-strict/non-empty-series must NOT hit the short-circuit; the load-bearing observation is the backstop candidate fetch (`m_pend/m_pub.assert_called_once`), which the buggy `if not strict` revert would skip. Strong.

### Axis 9 — Backfill · **COVERED**
- Idempotency: `test_idempotency_second_run_processed_zero` (`:182`) + `test_second_run_noop_after_empty_fp` (`:447`) + `test_fetch_empty_stores_computed_empty` (`:283`) — all assert `Processed: 0` / `0 rows scanned` on the second run.
- Widened re-select: `test_old_shape_row_reselected_and_upgraded` (old 2-key → 4-key), `test_row_with_pairs_key_skipped` (fetch raises `AssertionError` if a 4-key row is re-fetched), `test_corrupt_blob_reprocessed_without_crash` (parametrized invalid-JSON + non-dict array — pins the `CASE WHEN json_valid(...)` SQL guard *and* the `_already_backfilled` isinstance branch), `test_old_empty_shape_row_reselected_and_upgraded_to_empty`.
- 30-day window: `test_days_30_window_honored` (~29d in / ~31d out) plus `test_days_window_honored` (7d/30d). SELECT predicate verified static-literal `json_extract(model_fingerprint,'$.pairs')` inside the `json_valid` CASE.

### Axis 10 — Degraded mode · **COVERED**
- `test_degraded_mode` (`:1856`) — `extract_fingerprint` raises `RuntimeError` → article publishes with NULL fingerprint, exactly one `[E016]` (body contains `RuntimeError`), and second `job()` within the hour does **not** re-fire (rate-limit), second article still publishes.

### Axis 11 — Overall test quality · **COVERED (with minor notes)**
- Assertions are behaviour/verdict-specific (block vs flag vs pass; `Совпавшие пары` vs `Совпадение:` distinguishes pair-block from legacy-overlap-block), not raw-threshold peeking.
- Candidate-order independence pinned (`test_distinctive_wins_over_broad` both orders).
- Failure messages are meaningful (misclassification list, expected-vs-got tier).
- ReDoS guard present in both extractor passes: `test_long_body_no_hang` at `:192` (fingerprint) and `:567` (series).
- `[E014]`/`[E015]` builders: plain-text passthrough, no-`None%`-leak edge (`test_e015_..._never_renders_none_pct`), deterministic sorted pair rendering, no raw-key artifact leak (`*`, `|`, `|D`/`|B`) — all pinned.
- Accepted limitation `zamac` lowercase **has** a documenting test (`test_zamac_acronym_dual_case:540` asserts `extract_series('zamac diecast news') == []`). The model-token exact-match limitation does **not** (see L-3).

---

## Findings (sorted by severity)

### MEDIUM

**M-1 — pair-1 (broad Car Culture dupe) "must-not-hard-block" is pinned only by the aggregate ≥7/8 budget, not by a dedicated invariant.**
- **severity:** medium
- **issue:** `test_calibration_not_dupes_never_hard_block` iterates **only** `NON_DUPE_PAIRS`, but pair-1 (`pair-1-real-2026-06-03`) lives in `DUPE_PAIRS` while carrying `expected_any_distinctive=False` (a broad soft-flag dupe). Its irreversible "must never hard-block" property is therefore not pinned by either hard invariant — only by the accuracy test, which tolerates 1 misclassification (≥7/8). This is exactly the "hard invariant not pinned separately from an aggregate budget" pattern the audit is told to flag, on the irreversibility axis. Empirically, escalating `car culture` to distinctive **is** caught today (accuracy drops to 6/8 and pair-7 — a `car culture` NON_DUPE probe — trips the not-dupe invariant), so this is currently a latent/structural gap rather than an exploitable hole; the coverage is coincidental coupling (pair-1 and pair-7 happen to share the `car culture` series), not by-design. If the fixture ever evolved so pair-1's series diverged from every NON_DUPE probe, a silent broad-dupe→hard-block regression could hide inside the 1-error budget.
- **evidence:** `tests/test_model_extractor.py:436` (`test_calibration_not_dupes_never_hard_block` — `for pair in NON_DUPE_PAIRS`); `tests/fixtures/cross_source_dedup_pairs.py:147-203` (pair-1 in `DUPE_PAIRS`, `expected_any_distinctive: False`). Mutation probe: `car culture`→distinctive ⇒ pair-1 verdict `(True,'duplicate')`, accuracy 6/8, not-dupe test fails via pair-7 (not pair-1).
- **fix:** Broaden the invariant's selector to key on the flag rather than list membership, e.g. iterate `[p for p in DUPE_PAIRS + NON_DUPE_PAIRS if not p['expected_any_distinctive']]` and assert `any_distinctive is False`. That pins pair-1 directly and future-proofs against a fixture where the broad-dupe series stops overlapping a NON_DUPE probe. One-line change; no new fixture data.

### LOW

**L-1 — Stale fixture comments reference the removed `test_calibration_real_pair_must_pass`.**
- **severity:** low
- **issue:** pair-1's inline rationale claims its label + high-strict-overlap bodies are "KEPT" because "the surviving `test_calibration_real_pair_must_pass` finds this pair by label via `next()` (StopIteration if renamed/removed) and asserts the old similarity still classifies it 'duplicate'." That test was deleted in Task 6 (decisions.md: "Old calibration removed"). The comment is now misleading, and the implied guard (pair-1's ≥0.50 `similarity` real-body constraint) is no longer enforced by any test — pair-1's bodies could be thinned without a red test.
- **evidence:** `tests/fixtures/cross_source_dedup_pairs.py:140-146` and `:193-202`; repo grep: `test_calibration_real_pair_must_pass` appears only in that comment. `pytest` confirms no such test.
- **fix:** Update the comment to state pair-1's current role (the broad Car Culture dupe that must soft-flag / must-not-hard-block, scored by `test_calibration_pair_tier_accuracy` and, after M-1, by the broadened not-dupe invariant). Drop the `next()`/`>=0.50` rationale. Pure documentation edit.

**L-2 — `TestSqlAudit` scans only `pending_articles_repo.py`; `backfill_fingerprints.py` is not covered.**
- **severity:** low (informational — already recorded in tech-spec AC)
- **issue:** The SQL-parameterization smoke net hardcodes the `pending_articles_repo.py` path, so the feature's other SQL-bearing file (`backfill_fingerprints.py`) is outside its scope. Actual injection risk is nil (the `json_extract(model_fingerprint,'$.pairs')` predicate is a static literal, `--days` the only bound param — verified), so this is a coverage-scope note, not a vulnerability.
- **evidence:** `tests/test_pending_articles_repo.py:1294-1300` (hardcoded `'pending_articles_repo.py'`); tech-spec Acceptance Criteria line — "`TestSqlAudit` сканирует только `pending_articles_repo.py`, поэтому backfill он НЕ покрывает".
- **fix:** Optional — parametrize `TestSqlAudit` over `['pending_articles_repo.py', 'backfill_fingerprints.py']` to extend the net. Low priority given the static-literal predicate.

**L-3 — The accepted "model-token exact-match" limitation has no documenting behaviour test.**
- **severity:** low
- **issue:** decisions.md (Task 1) records "exact-key matching won't rescue a dupe when only one side names a valid model" as an accepted limitation — i.e. `porsche 911|k-pop demon hunters|D` (one side names the model) will not intersect `*|k-pop demon hunters|B` (other side theme-only), so a real dupe is missed. Unlike the `zamac` limitation (which has an explicit documenting test), this behaviour is not pinned anywhere, so a future change to theme-only↔model matching could silently alter it undetected.
- **evidence:** no test asserts the non-match of `porsche 911|...|D` vs `*|...|B`; contrast `tests/test_model_extractor.py:540` (`test_zamac_acronym_dual_case` documents its accepted under-match). Fixture pair-5/pair-8 cover *different-series* / *different-model* mismatches, not the *model-vs-theme-only* mismatch.
- **fix:** Add a small `TestSharesPair` case asserting `shares_pair({'pairs':['porsche 911|k-pop demon hunters|D']}, {'pairs':['*|k-pop demon hunters|B']})` returns `(False, [], False)`, with a docstring naming it as the documented exact-match limitation. Locks current behaviour so any future intent-change is a conscious, red-test decision.

**L-4 — AC8 both-empty short-circuit is exercised only through a forced extractor mock, never end-to-end through real empty extraction with a candidate present.**
- **severity:** low
- **issue:** `test_empty_fingerprint` mocks `extract_fingerprint` to return the 4-key empty shape. The body used would in fact extract empty for real, but the mock means the both-empty gate short-circuit is never proven against genuine extraction output. (The neighbouring re-gate test *does* use real extraction, but for the empty-strict/**non-empty**-series shape, not both-empty.) Low impact: the empty extraction path itself is unit-tested (`test_empty_body_four_keys`), and the gate logic is shared.
- **evidence:** `tests/test_integration.py:1804-1806` (`@patch('news_bot.model_extractor.extract_fingerprint', return_value={...empty...})`).
- **fix:** Optional — drop the mock (or add a sibling test) so the body with no brand/series is scored by the real extractor while a candidate row is seeded, confirming the real both-empty short-circuit fires with no pings. Belt-and-suspenders.

---

## Pyramid balance

- **Unit** (~57): `TestExtractFingerprint`, `TestSimilarity`, `TestSeriesLexicon`, `TestExtractSeries`, `TestPairs`, `TestSharesPair`, 3 calibration functions.
- **Integration** (~20 + ~17 backfill): `TestCrossSourceDedup` (real `job()` + SQLite), `TestFingerprintCarryThrough`, backfill (integration-flavoured: real extractor + real DB, only `fetch_full_article` mocked).
- **E2E**: none by design — replaced by the 2-week post-deploy channel monitoring (user-spec / tech-spec).
- **Assessment: healthy.** Many fast unit tests over the pure extractor/classifier; a focused integration band over the gate and the DB carry-through; boundary-only mocking. No inversion, no over-mocking of the unit under test.

---

## Recommendation (before Task 10 pre-deploy QA)

- **Ship-ready.** No critical/high; the fail-safe/distinctive irreversible invariants are correctly pinned as separate, non-vacuous tests, and the calibration is on the new classifier at 8/8.
- **Address M-1** as a one-line hardening before or during Task 10 — broaden the not-dupe invariant to select by `expected_any_distinctive is False` across both lists, so pair-1's must-not-hard-block property is pinned directly rather than by coincidence. Cheap, closes the only irreversibility-adjacent gap.
- **L-1** (stale comment) is worth a quick cleanup to prevent future confusion; **L-2/L-3/L-4** are optional low-priority hardening — safe to defer or waive.
- No spec/tech-spec defect found: the ≥7/8 aggregate + two separate asymmetric invariants match user-spec AC11 and the tech-spec Testing Strategy; every Testing-Strategy bullet maps to a concrete, mutation-resistant test.
