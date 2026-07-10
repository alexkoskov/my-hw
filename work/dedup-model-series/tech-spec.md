---
created: 2026-07-10
status: draft
branch: dev
size: L
---

# Tech Spec: dedup-model-series

## Solution

Extend the shipped cross-source-dedup with a NEW pair-rule keyed on
`(car model + series/theme)`, layered ON TOP of the existing car-model
set-overlap dedup. Series/theme is extracted by a tier-tagged lexicon inside
`model_extractor.py` (no new module, no new dependency) and stored in the
EXISTING `model_fingerprint` JSON blob (no schema migration). The gate runs the
pair-rule first (30-day window, ANY source), classifies each shared pair as
**distinctive** (specific model + specific franchise/event/limited-series →
hard-block `[E015]`) or **broad** (theme-only or recurring car-line → soft-flag
`[E014]`, publish + notify), with a **fail-safe default of broad** for any
untagged series. If the pair-rule does not hard-block, the existing 7-day
set-overlap dedup always backstops. The whole pair-rule is behind an env
**toggle** (default on) as a runtime kill-switch. A one-time `backfill_fingerprints.py
--days 30` warms both the base car-fingerprint and the new pairs.

## Architecture

### What we're building/modifying

- **`model_extractor.py`** — add a tier-tagged `SERIES_LEXICON` + a series/theme
  extraction pass; enrich `extract_fingerprint` output from `{strict, brands}` to
  `{strict, brands, series, pairs}`. Owns the pair-key format + tier tagging.
- **`news_bot.py`** — `_check_cross_source_dedup` refactor: env toggle → tiered
  30-day any-source pair-rule → existing 7-day set-overlap backstop; keep the
  degraded `try/except` + `[E016]`. New `DEDUP_SERIES_ENABLED` import-time const.
- **`admin_alerts.py`** — extend the `[E015]` builder to render the matched
  pair(s) + the earlier article link. `[E014]`/`[E016]` reused unchanged.
- **`pending_articles_repo.py`** — the 30-day fingerprint window query (the
  `model_fingerprint` JSON already carries the new keys — no schema change).
- **`backfill_fingerprints.py`** — widen the re-select + skip-guard to also catch
  rows that have a car-fingerprint but no `pairs` key.
- **Calibration fixture** — add the 3 real SDCC dupes + not-dupe probes.

### How it works

Per new article, at the dedup gate in `job()`:
1. `extract_fingerprint` returns `{strict, brands, series, pairs}`. Each pair is
   `"<model>|<series>|<tier>"` (or `"*|<series>|B"` for a pop-culture item with no
   recognised casting). `tier ∈ {D, B}`: **D** iff the series is lexicon-tagged
   distinctive AND a concrete model exists; otherwise **B** (fail-safe default).
2. If `DEDUP_SERIES_ENABLED` and `pairs` non-empty: fetch the 30-day candidate
   fingerprints (pending + published, ANY source — no same-source skip). First
   shared `|D` pair → **block** (`mark_processed`, `[E015]`, skip publish). Else
   first shared `|B` pair → **flag** (`[E014]` via the existing rate-limited path,
   article continues).
3. If the pair-rule neither blocked nor is disabled/empty → fall through to the
   UNCHANGED existing 7-day cross-source set-overlap dedup (≥0.50). Candidates are
   fetched once at 30 days; the 7-day subset is derived in Python.
4. The whole block is wrapped in the existing `try/except` → on any error log +
   rate-limited `[E016]`, `fp=None`, article publishes (degraded mode).

### Shared resources

None. (Extraction is pure/stateless; the SQLite connection is the existing
per-tick `conn` already threaded through the dedup gate.)

## Decisions

### Decision 1: Pair-key format + fingerprint shape
**Decision:** Enrich `model_fingerprint` JSON to `{strict, brands, series, pairs}`;
pair = `"<model>|<series>|<tier>"`, model always a full `strict` token (never a
bare brand), theme-only variant `"*|<series>|B"`.
**Rationale:** Full-model keys keep any-source matching precise (brand-only would
over-match); a stored tier avoids re-deriving it at compare time. Serves **AC1, AC2**.
**Alternatives:** brand-level pairs (rejected — over-blocks); new DB column
(rejected — needless migration).

### Decision 2: Two-tier verdict + fail-safe polarity
**Decision:** Distinctive pair (model + lexicon-tagged-distinctive series) →
hard-block `[E015]`. Broad pair (theme-only, or recurring car-line) → soft-flag
`[E014]`. Untagged/unknown series → **broad** (`_SERIES_DEFAULT_TIER = 'broad'`).
**Rationale:** Bounds the false-block blast radius to a curated distinctive set;
a new/unknown series can never silently hard-block. Serves **AC3, AC4, AC7**.
**Alternatives:** any-overlap silent block (rejected by adequacy validator — kills
legit new drops); default-distinctive (rejected — inverts the fail-safe).

### Decision 3: Composition — pair-rule first, set-overlap always backstops
**Decision:** Run the pair-rule first; if it does NOT hard-block, ALWAYS run the
existing 7-day set-overlap dedup.
**Rationale:** True "in addition, not instead" — closes the composition gap the
quality validator flagged (series-recognised-but-no-pair-match must still get the
old check). Serves **AC5**.
**Alternatives:** replace old dedup for series-recognised articles (rejected —
contradicts user intent).

### Decision 4: Env toggle (default on) — runtime kill-switch
**Decision:** `DEDUP_SERIES_ENABLED` (default `"1"`) read at import like `DB_FILE`;
the gate reads `news_bot.DEDUP_SERIES_ENABLED` (monkeypatchable in tests, flipped
via env + restart in prod). Off → pair-rule fully skipped, only the old dedup runs.
**Rationale:** Aggressive new rule needs an instant off switch without a code
change. Serves **AC6**.
**Alternatives:** no toggle (rejected — adequacy validator "no runtime safety valve").

### Decision 5: Any-source pair matching (reverses shipped Decision 9) `[TECHNICAL]`
**Decision:** The pair loop drops the same-source skip (`news_bot.py:990-994`) and
`test_within_source_not_deduped` is flipped/split; the OLD set-overlap backstop
KEEPS cross-source-only.
**Rationale:** The full model+series key is specific enough that same-source repeats
(«ещё фото») are real dupes — the 2026-06-14 within-source reversal was for the
looser brand-level match, which stays cross-source. Serves **AC3** (any source).
**Alternatives:** keep cross-source-only (rejected — misses the same-source SDCC
follow-up, a motivating case).

### Decision 6: Extraction in `model_extractor.py`, storage in existing JSON `[TECHNICAL]`
**Decision:** No new top-level file, no new dependency, no schema migration.
**Rationale:** All three touched modules are already in the deploy FILES arrays —
avoids the ImportError-crashloop class from cross-source-dedup Task 10. Serves the
user-spec Ограничения.

### Decision 7: Backfill widened re-select + pre-deploy cold-DB check
**Decision:** Widen `backfill_fingerprints.py` SELECT to
`... OR json_extract(model_fingerprint,'$.pairs') IS NULL` and the `backfill_one`
skip-guard to require the `pairs` key; deploy runbook adds a pre-deploy
`SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL`.
**Rationale:** The Moscow prod DB is almost certainly cold (copied from the
pre-feature NL snapshot; base backfill never run — see Risks). Serves **AC9, AC10**.
**Alternatives:** `IS NULL`-only re-select (rejected — skips rows that already have
a car-fingerprint but no pairs).

## Data Models

No schema change. `model_fingerprint TEXT` (already on `pending_articles` +
`published_articles`) carries JSON, extended in-place:

```
{"strict": [...], "brands": [...], "series": [...], "pairs": ["porsche 911|k-pop demon hunters|D", "*|stranger things|B", ...]}
```

Backward-compatible: rows without `series`/`pairs` keys are treated as
un-backfilled (skipped in extraction path; re-selected by backfill).

## Dependencies

### New packages
None.

### Using existing (from project)
- `model_extractor` — extend `extract_fingerprint` / `similarity`.
- `pending_articles_repo` — `list_recent_*_fingerprints` (window param 30), the
  `model_fingerprint` JSON carry-through, `SqlAudit` parameterization rules.
- `admin_alerts` — `[E015]`/`[E014]`/`[E016]` builders.
- `news_bot._check_cross_source_dedup` / `job()` gate wiring; `send_admin_notification`.

## Testing Strategy

**Feature size:** L

### Unit tests
- `extract_fingerprint`: series/theme extraction (car-lines, events, franchises;
  PT text with EN names; item with no model → theme-only `*|…|B`; no series →
  empty); pair-key format; tier tagging incl. fail-safe untagged→broad.
- Tier classifier: distinctive iff tagged-distinctive AND model present.
- `alert_cross_source_blocked` `[E015]`: renders matched pair + earlier link.
- `backfill` widened select/skip: rows with car-fp but no `pairs` re-processed;
  idempotent on second run.

### Integration tests (`tests/test_integration.py::TestCrossSourceDedup`)
- Distinctive pair, cross-source → block + `[E015]`.
- Distinctive pair, same-source («ещё фото») → block (any-source).
- Broad/theme-only match → flag `[E014]`, article publishes (NOT blocked).
- Pair-rule passes (no match / no series / toggle off) → 7-day set-overlap backstop runs.
- Toggle off → pair-rule skipped entirely.
- Degraded (extractor raises) → `[E016]`, article publishes.
- Flip `test_within_source_not_deduped` (pair-rule is any-source; backstop stays cross-source).

### Calibration (`tests/fixtures/…`)
3 real SDCC dupes (t-hunted PT + autoevolution EN + same-source «mais fotos») →
hard-block; not-dupes: same-car-different-series → no block; theme-only mainline
vs SDCC Stranger Things → soft-flag (not block); same-source recurring-line
near-miss → soft-flag. Classifier ≥7/8.

### E2E tests
None — replaced by the 2-week post-deploy channel monitoring (user-spec).

## Agent Verification Plan

**Source:** user-spec "Как проверить".

### Verification approach
Automated: `pytest -q` (unit + integration + calibration). Per-task smoke checks
below. Lexicon validated by sampling the real DB (Task 1 smoke). Post-deploy
(operator): pre-deploy `SELECT COUNT`, `backfill --days 30`, 2-week channel +
`[E015]`/`[E014]` spot-check.

### Tools required
bash (pytest, sqlite3), Telegram (operator visual on the channel/pings). No
Playwright. Live prod checks are operator-applied (Claude cannot SSH prod).

## Risks

| Risk | Mitigation |
|------|-----------|
| Over-block on distinctive tier (no manual re-publish) | Distinctive set is curated + calibration-gated; toggle kill-switch; fail-safe untagged→broad |
| Fail-safe polarity inverted (untagged → distinctive) | `_SERIES_DEFAULT_TIER='broad'` + explicit test |
| Two behaviour reversals fumbled (any-source pair loop; flip `test_within_source_not_deduped`) | Scoped to the pair loop only; backstop stays cross-source; dedicated tests |
| Cold Moscow prod DB (base backfill never ran — confirmed likely) | Pre-deploy `SELECT COUNT` check + mandatory `backfill --days 30` warms base + pairs |
| Lexicon lag (open franchise namespace) | Seed from user-spec families; validate on real DB; top-up via PR |
| Regression in publish path | Degraded mode `[E016]` + toggle |

## User-Spec Deviations

None. (Decisions 5 and 6 are marked `[TECHNICAL]` — implementation choices that
serve the user-spec's stated constraints/ACs, not changes to user intent.)

## Acceptance Criteria

Технические (дополняют пользовательские AC1–AC11 из user-spec):
- [ ] Нет миграции схемы; `model_fingerprint` JSON расширен обратносовместимо.
- [ ] `SqlAudit` (parameterized-queries test) остаётся зелёным (`json_extract` — статический фрагмент).
- [ ] Ни один новый top-level модуль/first-party import не добавлен в `news_bot` без записи во ВСЕ FILES-массивы (проверить deploy.yml/deploy_test.yml/deploy.sh).
- [ ] Toggle off полностью пропускает парное правило; старый дедуп работает как раньше.
- [ ] Degraded mode: любой сбой экстрактора/сравнения → статья публикуется.
- [ ] Весь набор тестов зелёный; нет регрессий; калибровка ≥7/8.

## Implementation Tasks

### Wave 1 (независимые)

#### Task 1: Series/theme extraction + tier-tagged lexicon (`model_extractor.py`)
- **Description:** Add a tier-tagged `SERIES_LEXICON` (car-lines, events, pop-culture
  franchises; each tagged distinctive/broad, fail-safe default broad) and a
  series/theme extraction pass; enrich `extract_fingerprint` to
  `{strict, brands, series, pairs}` with the `"<model>|<series>|<tier>"` key.
  Validate the lexicon against real feed text. Serves AC1, AC6, AC7.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python3 -c "import model_extractor,news_bot; ..."` on the 3 SDCC
  titles → expected pairs/tiers; `sqlite3` sample of `published_articles.title` to
  confirm lexicon coverage of real series names.
- **Files to modify:** `model_extractor.py`, `tests/test_model_extractor.py`
- **Files to read:** `boilerplate_filter.py`, `work/dedup-model-series/code-research.md`

#### Task 2: Extend `[E015]` ping with matched pair + earlier link (`admin_alerts.py`)
- **Description:** Extend `alert_cross_source_blocked` to render the matched
  `model+series` pair(s) and the earlier article link, keeping the «Заблокирован
  дубль» anchor. Serves AC3.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `admin_alerts.py`, `tests/test_admin_alerts.py`
- **Files to read:** `work/dedup-model-series/code-research.md`

#### Task 3: Calibration fixture (real dupes + not-dupes)
- **Description:** Add the 3 real SDCC dupes (hard-block) + not-dupes
  (same-car-different-series; theme-only mainline-vs-SDCC; same-source recurring-line
  near-miss). Serves AC11.
- **Skill:** code-writing
- **Reviewers:** test-reviewer
- **Files to modify:** `tests/fixtures/cross_source_dedup_pairs.py`
- **Files to read:** existing fixture, `work/cross-source-dedup/user-spec.md`

### Wave 2 (зависит от Task 1)

#### Task 4: Toggle + tiered gate refactor (`news_bot.py`)
- **Description:** Add `DEDUP_SERIES_ENABLED` (default on); refactor
  `_check_cross_source_dedup` — toggle → 30-day any-source pair-rule (distinctive
  → block/`[E015]`, broad → flag/`[E014]`, fail-safe untagged→broad, re-gate the
  empty-fp short-circuit) → existing 7-day set-overlap backstop; keep degraded
  `[E016]`. Update integration scenarios; flip `test_within_source_not_deduped`.
  Serves AC3, AC4, AC5, AC6, AC8.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `tests/test_integration.py`, `.env.example`
- **Files to read:** `pending_articles_repo.py`, `work/dedup-model-series/code-research.md`

#### Task 5: Backfill widened re-select (`backfill_fingerprints.py`)
- **Description:** Widen the SELECT + `backfill_one` skip-guard to re-process rows
  that have a car-fingerprint but no `pairs` key (`json_extract(...,'$.pairs') IS
  NULL`); keep idempotent. Serves AC9, AC10.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python3 backfill_fingerprints.py --days 30 --dry-run` on a temp DB → summary; second run no-op.
- **Files to modify:** `backfill_fingerprints.py`, `tests/test_backfill_fingerprints.py`
- **Files to read:** `pending_articles_repo.py`

### Wave 3 (зависит от Wave 1–2)

#### Task 6: Calibration accuracy test + deploy/runbook docs
- **Description:** Wire the fixture through the tier classifier (`≥7/8`); document the
  pre-deploy `SELECT COUNT` check + safe rollout (deploy-dark → `backfill --days 30`
  → observe → toggle on, outside 10:00–20:00 МСК) in `deployment.md`. Serves AC10, AC11.
- **Skill:** code-writing
- **Reviewers:** test-reviewer, documentation-reviewer
- **Files to modify:** `tests/test_model_extractor.py`, `.claude/skills/project-knowledge/references/deployment.md`
- **Files to read:** `work/dedup-model-series/user-spec.md`

### Audit Wave

#### Task 7: Code Audit
- **Description:** Full-feature code quality audit of all created/modified source. Holistic: gate refactor consistency, fail-safe polarity, no duplicate init, FILES-array invariant. Write report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 8: Security Audit
- **Description:** Full-feature security audit. The gate classifies UNTRUSTED feed/LLM content; check ReDoS in the new regex, no injection via series tokens into SQL (json_extract stays parameterized), no untrusted content into pings unescaped. Write report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 9: Test Audit
- **Description:** Full-feature test quality audit: calibration meaningfulness, tier/fail-safe coverage, no mock-leak, the two behaviour-reversal tests actually pin behaviour. Write report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 10: Pre-deploy QA
- **Description:** Run the full suite; verify every user-spec AC1–AC11 + tech-spec AC. Confirm toggle-off parity, degraded mode, calibration ≥7/8, no regressions.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 11: Post-deploy verification (operator-applied)
- **Description:** Live checks the operator runs (Claude cannot SSH prod):
  - Pre-deploy `SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL` on Moscow prod — expect ~0 (cold) — tool: bash/sqlite3 over SSH.
  - After rebuild + `backfill --days 30`: re-count > 0 — tool: bash/sqlite3.
  - 2 weeks: channel has no repeated model+series; spot-check `[E015]`/`[E014]` pings — tool: Telegram visual.
  - Tools: bash, sqlite3 (operator SSH), Telegram visual.
- **Skill:** post-deploy-qa
- **Reviewers:** none
