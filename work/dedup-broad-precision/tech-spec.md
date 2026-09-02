---
created: 2026-08-30
status: approved
branch: dev
size: M
---

# Tech Spec: Broad-Pair Dedup Precision

## Solution

The fix narrows only the broad (`|B`) branch of the existing cross-article dedup gate. The full article fingerprint remains unchanged, but a shared broad pair becomes eligible for `[E014]` only when the canonical series embedded in that pair is also extracted from the effective original title of both articles. The effective title is the same value persisted to the queue (`article.title`, falling back to the feed-entry title), so the verdict remains symmetric regardless of which article arrives second.

`news_bot._pair_rule_verdict()` will distinguish qualified broad matches from subject-rejected matches while preserving scan-and-remember ordering: a qualified broad match is remembered, candidate scanning continues, and any later distinctive (`|D`) match still wins with `[E015]`. The function returns the decision, the selected match, and bounded diagnostics for subject-rejected candidate comparisons. Only the qualified pair subset is attached to an E014 match, so the alert never names a series that failed the title test.

When all broad matches are rejected, the existing seven-day cross-source set-overlap backstop still runs with its current 30%/50% thresholds. If that backstop would hard-block, the gate downgrades only that result to a soft flag and marks it as `overlap_capped`; an unrelated later `|D` verdict from the pair rule is never capped. Every actual `flag` decision receives the existing 24-hour `publish_after` deferral, including the capped result, while a subject rejection followed by `pass` stages immediately.

The operator contour receives three explicit E014 reasons (`broad_subject`, `overlap`, and `overlap_capped`), bounded one-line suppression logs, and one per-tick count of new articles for which at least one broad candidate was rejected by the title rule. Precision is measured by a sanitized offline corpus of 24 production article pairs: all 3 known true duplicates must remain flagged and no more than 1 of the 21 known false pairs may remain flagged. The change adds no schema, fingerprint key, dependency, external classifier, or network call to the runtime gate.

## Architecture

### What we're building/modifying

- **`news_bot.py`** — establish one effective title, qualify broad pairs against both titles, return suppression diagnostics, cap only the backstop hard-block reached through a subject rejection, preserve exact defer semantics, and emit bounded logs/funnel telemetry.
- **`admin_alerts.py`** — render truthful E014 explanations for the three reason values and expose the suppression count in both daily funnel formats without counting it as a dropped article.
- **`model_extractor.py` (reused, not modified unless a regression test exposes a defect)** — provide the existing `extract_series(text)` canonical title matcher and unchanged full-article fingerprint extraction.
- **Regression corpus and tests** — store sanitized titles, public article identifiers, sources, and existing fingerprints for 24 labelled production pairs; score the real pair-rule and cover the gate, deferral, alerts, logging, telemetry, distinctive precedence, and fail-open paths.
- **Project Knowledge** — update the high-level project description, dedup data flow, coding patterns, and rollout guidance to remove the obsolete claim that every broad pair is terminal.

### How it works

1. `job()` computes `effective_title = article.get('title') or entry.get('title') or ''` once before the dedup gate and reuses that exact value for the gate, queue row, and any processed-row write.
2. `extract_fingerprint(article)` continues to scan title, subtitle, and body and returns the existing `strict`, `brands`, `series`, and `pairs` keys unchanged.
3. The pair rule computes `new_subject_series = set(model_extractor.extract_series(effective_title))` once per new article.
4. For each 30-day candidate, malformed or non-dict fingerprints are skipped as today. Shared distinctive pairs immediately return `block`; broad processing never weakens that path.
5. For a candidate with shared broad pairs, the rule extracts the canonical series from each well-formed pair key and computes the candidate's title-only series with `extract_series(candidate.title or '')`.
6. A broad pair qualifies only when its canonical series exists in both title-series sets. At least one qualified pair is sufficient; only qualifying pairs are placed in the selected match.
7. The first qualified broad match is remembered while scanning continues. Subject-rejected candidates are recorded once per candidate, and a later qualified broad match or distinctive match keeps its normal precedence.
8. The pair rule returns `(decision, match, suppressed_matches)`. The gate returns the same three-part result to `job()` so one result supplies verdict, diagnostics, cap state, and telemetry without side effects inside the pure rule.
9. On a pair-rule `pass`, the existing set-overlap backstop runs unchanged. If `suppressed_matches` is non-empty and the backstop returns `block`, the gate converts that result to `flag` and sets `match.reason = 'overlap_capped'`; ordinary backstop flags use `overlap`, and qualified broad flags use `broad_subject`.
10. `job()` sets `publish_after` only when the final decision is `flag`. Therefore every E014 has a usable cancellation window and every silent pass is immediately eligible for ordinary queue scheduling.
11. `job()` logs each rejected candidate comparison with bounded, redacted, single-line identifiers/titles, then increments `dedup_subject_suppressed` once for the new article when at least one rejection occurred. The count is informational and never enters the dropped total.
12. Any exception in extraction, title matching, pair parsing, candidate reads, logging preparation, or backstop handling remains inside the existing dedup fail-open boundary: the article is staged with no fingerprint and the operator receives the rate-limited `[E016]` path.

### Shared resources

No new heavy process resource is introduced. Existing shared values are listed to make ownership explicit.

| Resource | Owner (creates) | Consumers | Instance count |
|----------|-----------------|-----------|----------------|
| Compiled series lexicon/regexes | `model_extractor` at module import | Full fingerprint extraction and title-only `extract_series()` | 1 module-level set per process |
| 30-day dedup candidate list | `_check_cross_source_dedup()` | Pair rule and seven-day backstop subset | 1 list per incoming article gate |
| SQLite state | Existing `pending_articles_repo` / deployment | Candidate reads, queue staging, rate-limit/token state | 1 database file; existing short-lived connections |
| Root progress tracker | Current-session orchestration | User and future feature turns | 1 `.project-progress.json`; updated after substantial tasks |

## Decisions

Traceability labels `US-AC1` through `US-AC12` refer to the twelve top-level acceptance-criteria bullets in `user-spec.md`; `US-E1` through `US-E5` refer to its edge cases, and `US-C1` through `US-C6` refer to its constraints in order.

### Decision 1: Use the persisted effective title as the symmetric subject boundary

**Decision:** Hoist the existing `article title -> feed title -> empty string` fallback before the gate and use the same value for both title matching and persistence. Candidates continue to use the original-language `title` already selected from pending and published rows.

**Rationale:** This satisfies US-AC1, US-AC2, US-E1, and US-C3. Reading only `article.title` on the new side while later reading the feed fallback from the candidate row would make the verdict depend on arrival order.

**Alternatives considered:** Read the body or first paragraph (asymmetric because published rows do not store it); add a declared-series fingerprint key (requires warm-up/backfill behavior); use only `article.title` (order-dependent on empty fetcher titles).

### Decision 2: Reuse canonical series extraction and test only the pair's series

**Decision:** Call the existing `model_extractor.extract_series(raw_title)` on both titles and compare the canonical series encoded in each broad pair. The model portion of the pair is not required in either title.

**Rationale:** This satisfies US-AC1, US-AC2, US-AC3, US-E4, and US-E5. The approved rule defines the declared subject by line/series, while a concrete casting may legitimately appear only in the subtitle or body of a set article.

**Alternatives considered:** Add Portuguese aliases or translation (unnecessary because brand line names already resolve); require both model and series in titles (would reject measured true duplicates); substring-match raw pair keys (would duplicate lexicon normalization and acronym rules).

### Decision 3: Preserve scan-and-remember precedence and expose only qualified pairs

**Decision:** Remember the first qualified broad candidate, continue through all candidates, and return immediately only for a distinctive pair. Build the broad E014 match from the qualified subset, never from every shared pair.

**Rationale:** This satisfies US-AC3, US-AC4, US-AC8, and US-AC11. A subject rejection removes only that comparison; it must not hide a later true broad match or irreversible distinctive match, and the alert must not name a rejected line.

**Alternatives considered:** Return on the first rejected or qualified broad pair (can hide a later `|D`); stop the pair rule after one candidate (current-noise behavior); render every shared pair and explain only one (factually misleading).

### Decision 4: Return per-candidate diagnostics but count one affected new article

**Decision:** `_pair_rule_verdict()` returns `suppressed_matches`, with at most one record per candidate article and the rejected canonical series set. `job()` logs every record but increments the daily funnel once when the list is non-empty.

**Rationale:** This satisfies US-AC9 and US-AC10 while keeping the metric comparable to the historical E014 unit: the old rule emitted at most one alert per new article even when many candidates matched. Per-candidate logs retain enough evidence to inspect an individual suppression.

**Alternatives considered:** Count every pair key (cartesian round-ups dominate the number); count every candidate in the ping (not comparable to the 31-alert baseline); log only the selected candidate (hides missed-dedup evidence).

### Decision 5: Cap the backstop at the gate boundary and carry an explicit reason

**Decision:** Leave `_set_overlap_backstop_verdict()` and its thresholds unchanged. After it returns, `_check_cross_source_dedup()` downgrades only a `block` reached with at least one subject-rejected broad match and sets the explicit reason `overlap_capped` instead of deriving the state from rounded display percentages.

**Rationale:** This satisfies US-AC6, US-AC8, US-C1, and US-C4. The existing threshold function remains authoritative, a rounded 50% display cannot falsely imply the raw threshold was met, and a pair-rule `|D` block remains outside the cap.

**Alternatives considered:** Add a cap parameter inside the backstop (mixes policy into the function fenced as unchanged); downgrade in `job()` (too late, near irreversible side effects); infer capped state from `overlap_pct >= 50` (rounding can lie for raw similarities below 0.50).

### Decision 6: Make the final `flag` decision the sole deferral trigger

**Decision:** Preserve one lifecycle invariant: `decision == 'flag'` if and only if `publish_after` receives the existing 24-hour value. A subject rejection followed by backstop `pass` receives neither E014 nor a deferral; an `overlap_capped` flag receives both.

**Rationale:** This satisfies US-AC7. A cancel button without a decision window repeats the production race the deferral was introduced to prevent.

**Alternatives considered:** Skip deferral for every subject-rejected contour (breaks capped E014); tie deferral to whether the Telegram alert was rate-limited (could publish a flagged article immediately); add a second deferral field (no need and violates US-C2).

### Decision 7: Use reason-aware messages and bounded untrusted diagnostics

**Decision:** Pass `reason` and the rejected canonical series to `alert_cross_source_dupe()` as keyword-only data. Broad logs omit pair-overlap percentages, and suppression logs sanitize/redact links and titles, remove control characters, and apply strict length bounds before logging.

**Rationale:** This satisfies US-AC8, US-AC9, US-AC10, and US-AC12. Article URLs and titles are external input; diagnostic value does not require log-forging risk, embedded credentials/query values, unbounded text, or secret-shaped strings.

**Alternatives considered:** Keep one generic 50% sentence (false for broad matches and capped matches); log raw titles/URLs (unsafe and noisy); remove `overlap_pct` from match dictionaries (existing direct-index consumers would fail-open through `[E016]`).

### Decision 8: Make the production corpus the first implementation dependency

**Decision:** Capture only public article metadata and existing fingerprint JSON for the 24 recoverable labelled pairs, preserve a sanitized raw evidence copy, and build a pure offline scorer around the real pair-rule. Fixture-integrity tests land before the target verdict assertion is activated by the core implementation task.

**Rationale:** This satisfies US-AC5 and US-C5. The corpus is the acceptance oracle and protects against optimizing only for synthetic examples; separating integrity from the target assertion avoids committing a permanently red intermediate wave.

**Alternatives considered:** Re-fetch all source bodies (unnecessary and unreliable); synthesize fingerprints (does not reproduce the measured cartesian noise); use a live database in tests (non-deterministic and credential-bearing).

### Decision 9: Keep storage and extraction formats backward-compatible

**Decision:** Add no SQLite column, migration, fingerprint key, backfill, dependency, model service, or network request. Legacy fingerprints without `pairs` and malformed/non-dict candidate fingerprints retain their current safe behavior.

**Rationale:** This satisfies US-E2, US-E3, US-C2, and US-C6. Every datum needed for the title rule already exists at verdict time, and changing persisted fingerprints would create an avoidable mixed-history rollout.

**Alternatives considered:** Persist declared series in fingerprints (old rows remain missing without an unreliable refetch); store article bodies in published rows (historical data cannot be recovered); embeddings/LLM classification (latency, cost, nondeterminism, and scope expansion).

### Decision 10: Roll out with the existing feature switch and observational acceptance

**Decision:** Use the existing `DEDUP_SERIES_ENABLED` switch as the immediate runtime mitigation, deploy through the established manual release flow, and classify live E014/E015 outcomes during the user-spec observation window.

**Rationale:** This supports US-AC5, US-AC11, US-AC12, and the user-spec deployment/verification sections. Offline corpus accuracy is necessary but production traffic is the only independent sample that can reveal overfitting.

**Alternatives considered:** Add a new feature flag for only the subject check (extra config and split states); dark-run a second dedup implementation (duplicate logic and telemetry); auto-disable based on a small live sample (unapproved autonomous behavior).

## Data Models

### Persistent data

No SQLite schema or fingerprint-format change. Existing fields remain authoritative:

- `pending_articles.title` / `published_articles.title` — original-language effective title used for candidate subject matching.
- `model_fingerprint.pairs` — existing `"<model>|<series>|<tier>"` values.
- `pending_articles.publish_after` — existing timed E014 deferral.
- `bot_state` — existing per-link-pair alert rate limit and review tokens; unchanged.

### Internal dedup result

The gate returns:

```text
(decision, match, suppressed_matches)
```

- `decision`: `block | flag | pass`.
- `match`: existing match fields plus `reason` (`broad_subject | overlap | overlap_capped`) and, for capped overlap, `subject_rejected_series`; `None` on pass.
- `suppressed_matches`: list of bounded diagnostic records with `link`, `source_name`, `title`, and canonical `series`, at most one record per candidate article.

The result is internal Python data only. It is not persisted and does not change callback/token schemas.

### Regression corpus

`tests/fixtures/dedup_broad_precision.json` contains a README/provenance header and exactly 24 labelled pair records. Each side contains only a public article identifier, original title, source name, and existing fingerprint object. Pair labels use `dupe` or `not_a_dupe`; no tokens, chat identifiers, credentials, server coordinates, raw database rows, or private operator data are allowed.

## Dependencies

### New packages

- None.

### Using existing (from project)

- `model_extractor.extract_series()` — canonical title-only series detection, including current Portuguese-title and acronym-casing behavior.
- `model_extractor.extract_fingerprint()` — unchanged full-article fingerprint construction.
- `pending_articles_repo` — existing 30-day candidate projections, E014 rate limit/token state, and queue deferral.
- `news_bot._set_overlap_backstop_verdict()` — unchanged seven-day cross-source Jaccard thresholds.
- `admin_alerts` — existing E008/E009/E014 builders and review-button copy contract.
- `pytest`, `unittest.mock`, and temporary SQLite fixtures — current unit/integration infrastructure.
- Existing manual Docker release, log inspection, and rollback procedures from Project Knowledge.

## Testing Strategy

**Feature size:** M

### Unit tests

- Corpus fixture integrity: exactly 24 pairs, 3 `dupe`, 21 `not_a_dupe`, required fields/types, no prohibited private/secret-shaped fields, and deterministic labels.
- Pure corpus score through the real pair rule: 3/3 true duplicates flagged and no more than 1/21 false pairs flagged.
- Title-only canonical extraction for English and Portuguese line names; uppercase `RLC`/`STH` match and lowercase forms do not.
- Broad qualification matrix: both titles, one title, empty candidate title, several shared series with one qualified, malformed pair keys, and candidate fingerprints without `pairs`.
- Scan precedence: suppressed then qualified broad; qualified broad then later distinctive; suppressed then later distinctive.
- Gate-level cap: rejected broad plus raw backstop similarity below 0.50, at/above 0.50, same-source-only backstop candidate, and no rejected broad control.
- E014 builder matrix for `broad_subject`, `overlap`, and `overlap_capped`, including keyword-only signaling and no misleading 50% sentence.
- Funnel rendering with zero/non-zero suppression values, malformed values, and proof that suppression is not included in the dropped count.

### Integration tests

- `job()` uses the same effective title for gate and persistence when an article fetcher title is empty and the feed title supplies the fallback.
- Qualified broad match produces one E014, one 24-hour deferral, unchanged pair rate-limit/token behavior, and no E015.
- Subject-rejected broad plus backstop `pass` stages immediately, emits no E014/E015, increments the suppression counter once, and logs every rejected candidate without a broad overlap percentage.
- Subject-rejected broad plus 100% strict overlap produces capped E014 and a deferral, never `mark_processed()`/E015.
- A later distinctive candidate remains an irreversible E015; distinctive, toggle-off, legacy fingerprint, and same-source contracts remain unchanged.
- Exception inside title qualification or result handling follows the existing E016 fail-open path and stages the article with no fingerprint.
- Existing review-button keep/cancel behavior continues to operate on the deferred row without callback grammar or token-kind changes.

External Telegram, article sources, production SQLite, and publication services remain mocked or absent from automated tests. Assertions must verify final queue/processed state and rendered operator text, not only mock call counts.

### E2E tests

None before deployment. There is no staging instance, the rule is deterministic, and a live publication is irreversible. Post-deploy verification is observational against ordinary production intake and does not inject test articles.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

The implementation starts by materializing and integrity-checking the sanitized corpus, then runs the scorer against the current gate to record the baseline. Core logic is implemented with focused pair/gate tests before job integration, after which the three real E014 builder variants, suppression logs, funnel output, defer invariant, distinctive precedence, and fail-open behavior are verified together.

Before release, the agent runs the focused dedup suites, the full repository suite, JSON validation for the corpus and `.project-progress.json`, scoped pre-commit hooks for changed files, and `git diff --check`. The QA report maps every user and technical acceptance criterion to a concrete assertion/result. No local verification reads secrets or performs a live publish.

Deployment is a separate final task after explicit approval and green CI. The operator performs the established manual release at any time, verifies a clean container start, and can disable the existing dedup-series switch or roll back the release if E016/E015 behavior changes unexpectedly. A restart immediately replans remaining publication opportunities, but time of day is not a deployment gate. Post-deploy verification classifies naturally occurring E014 alerts and compares the daily suppression count with detailed bounded logs during the approved observation window.

### Tools required

- `bash`/`zsh`, `python3`, and `venv/bin/python -m pytest` for local deterministic checks.
- `python3 -m json.tool` for JSON artifact validation.
- `git diff --check`, scoped `pre-commit`, and read-only Git history/status inspection.
- An operator-mediated, read-only production SQLite export for the sanitized corpus; no credentials or server details enter repository artifacts.
- Existing operator shell/Docker commands for deployment and log inspection.
- Telegram client/manual review for post-deploy E014/E015 classification; no automated test messages.

## Risks

| Risk | Mitigation |
|------|------------|
| The title-only rule suppresses a genuine duplicate whose line is absent from one title | Preserve the set-overlap safety net with a soft cap, log rejected comparisons with both titles/series, expose daily volume, and observe independent live traffic after deployment |
| The rule overfits the same 24 pairs used to derive it | Keep the corpus as a hard regression gate but treat live E014/E015 classification as the independent acceptance sample |
| A feed-title fallback makes the rule order-dependent | Compute one effective title once and reuse it for both gate and persistence; integration-test the empty fetcher-title case |
| A qualified match alert names a series that failed subject qualification | Attach only the qualified pair subset to the E014 match and unit-test mixed shared-series candidates |
| Backstop capping accidentally weakens distinctive blocking | Apply the cap only after a pair-rule pass with suppressed broad matches; test a later `|D` candidate and keep its immediate E015 path unchanged |
| Rounded display percentages misclassify an ordinary overlap flag as capped | Carry an explicit `reason` from the raw decision branch; never derive the reason from `overlap_pct` |
| Suppression telemetry is inflated by cartesian pair counts | Log per candidate but increment the funnel once per affected new article; keep it outside the dropped total |
| New diagnostic logs expose control characters, URL credentials, secrets, or unbounded titles | Reuse bounded URL-identifier sanitization, add a bounded one-line redacted title formatter, and test malicious-shaped inputs |
| A bug in new title/pair parsing disables dedup | Keep all new work inside the existing broad fail-open boundary and pin E016 plus staged-without-fingerprint behavior |
| Corpus extraction imports operational/private data | Select only public article metadata and fingerprints, validate prohibited fields, preserve no raw bot-state values, and review the artifact before commit |
| Reduced false deferrals change same-day queue pressure | Keep the fixed three-slot scheduler unchanged and verify that only actual flags receive `publish_after` |
| Documentation continues to state that every broad pair is terminal | Make Project Knowledge update a required task before audits and QA |

## User-Spec Deviations

None.

## Acceptance Criteria

- [ ] A shared broad pair can produce E014 only when that pair's canonical series is extracted from the effective original title of both articles.
- [ ] The effective title fallback is computed once and used symmetrically for the new-side check and the title later persisted for candidate checks; neither article body is read for subject qualification.
- [ ] One qualified shared broad series is sufficient, and only qualified pairs are named in E014.
- [ ] A subject-rejected candidate does not stop the scan: a later qualified broad candidate produces E014 and a later distinctive candidate produces unchanged E015.
- [ ] The sanitized 24-pair corpus scores all 3 true duplicates as flagged and at most 1 of 21 false pairs as flagged.
- [ ] Subject-rejected broad matches still reach the unchanged set-overlap backstop; a raw backstop block is downgraded to capped E014, while an ordinary backstop flag and a no-suppression block retain existing behavior.
- [ ] `publish_after` is set if and only if the final decision is `flag`, including `overlap_capped`; silent passes receive no dedup deferral.
- [ ] E014 text is truthful for all three reasons: `broad_subject` names the line and has no 50% claim; `overlap` retains the below-threshold explanation; `overlap_capped` says the raw block threshold was reached but title-subject evidence prevented an automatic block.
- [ ] Broad and suppression log lines contain no pair-overlap percentage; suppression fields are bounded, redacted, and single-line.
- [ ] E008/E009 show a non-zero suppression line when applicable, using one count per affected new article and never adding it to the dropped total.
- [ ] Distinctive (`|D`) extraction, any-source blocking, E015 side effects, pair alert rate limits, review-button tokens/callbacks, and the `DEDUP_SERIES_ENABLED` switch remain unchanged.
- [ ] Empty/missing titles fail subject qualification without raising; malformed/non-dict and legacy fingerprints retain safe skip behavior; Portuguese line names and existing acronym casing are pinned.
- [ ] Any failure inside the dedup contour still produces rate-limited E016 and stages the article with `model_fingerprint = NULL`.
- [ ] No SQLite migration, fingerprint migration/key, backfill, external runtime call, or new dependency is introduced.
- [ ] Focused suites, full pytest, scoped pre-commit hooks, JSON validation, and diff checks pass before deployment; post-deploy work injects no test article.

## Implementation Tasks

### Wave 1: Regression oracle

#### Task 1: Production corpus and scoring harness
- **Description:** Materialize the sanitized 24-pair production corpus, preserve its provenance, and add a pure scorer plus fixture-integrity coverage. The artifact establishes the measurable baseline and contains no operational or private data.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `venv/bin/python -m pytest tests/test_dedup_broad_precision.py -q` -> fixture integrity passes and the deterministic baseline score is produced without database or network access
- **Files to modify:** `work/dedup-broad-precision/corpus-raw/README.md` (new), `work/dedup-broad-precision/corpus-raw/dedup-pairs.json` (new), `tests/fixtures/dedup_broad_precision.json` (new), `tests/test_dedup_broad_precision.py` (new)
- **Files to read:** `work/dedup-broad-precision/user-spec.md`, `work/dedup-broad-precision/code-research.md`, `work/dedup-broad-precision/logs/userspec/measurements.md`, `tests/fixtures/cross_source_dedup_pairs.py`, `news_bot.py`

### Wave 2: Core decision flow

#### Task 2: Subject-aware pair rule and capped backstop
- **Description:** Implement title-subject qualification, continued candidate scanning, qualified-pair selection, suppression diagnostics, and the gate-level block cap. Activate the corpus target and cover lifecycle state, symmetry, edge cases, distinctive precedence, and fail-open behavior.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `tests/test_dedup_broad_precision.py`, `tests/test_integration.py`, `tests/test_model_extractor.py`
- **Files to read:** `model_extractor.py`, `pending_articles_repo.py`, `admin_alerts.py`, `work/dedup-broad-precision/tech-spec.md`, `tests/fixtures/dedup_broad_precision.json`

### Wave 3: Operator diagnostics

#### Task 3: E014 reasons, suppression logs, and funnel telemetry
- **Description:** Integrate the three truthful E014 variants, bounded subject-rejection diagnostics, and daily suppression telemetry without changing review-button or alert-rate-limit semantics. Verify that broad logs omit percentages and that suppression is informational rather than a drop.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `admin_alerts.py`, `tests/test_integration.py`, `tests/test_admin_alerts.py`, `tests/test_no_token_leak_in_logs.py`
- **Files to read:** `work/dedup-broad-precision/user-spec.md`, `work/dedup-broad-precision/tech-spec.md`, `model_extractor.py`, `pending_articles_repo.py`

### Wave 4: Documentation

#### Task 4: Project Knowledge update
- **Description:** Document the title-subject rule, non-terminal suppressed branch, capped backstop, telemetry unit, rollout mitigation, and post-deploy observation contract. Remove claims that any shared broad pair automatically produces a terminal flag.
- **Skill:** documentation-writing
- **Reviewers:** code-reviewer
- **Files to modify:** `.claude/skills/project-knowledge/references/project.md`, `.claude/skills/project-knowledge/references/architecture.md`, `.claude/skills/project-knowledge/references/patterns.md`, `.claude/skills/project-knowledge/references/deployment.md`
- **Files to read:** `work/dedup-broad-precision/user-spec.md`, `work/dedup-broad-precision/tech-spec.md`, `news_bot.py`, `admin_alerts.py`, `model_extractor.py`

### Audit Wave

#### Task 5: Code Audit
- **Description:** Review the completed gate, lifecycle, diagnostics, tests, and documentation holistically for complexity, duplicate logic, error handling, reason consistency, and preservation of existing dedup invariants. Write the feature code-audit report without modifying implementation files.
- **Skill:** code-reviewing
- **Reviewers:** none
- **Files to modify:** `work/dedup-broad-precision/logs/audit/code-audit.json` (new)
- **Files to read:** `news_bot.py`, `admin_alerts.py`, `model_extractor.py`, `tests/test_dedup_broad_precision.py`, `tests/test_integration.py`, `tests/test_model_extractor.py`, `tests/test_admin_alerts.py`, `.claude/skills/project-knowledge/references/project.md`, `.claude/skills/project-knowledge/references/architecture.md`, `.claude/skills/project-knowledge/references/patterns.md`, `.claude/skills/project-knowledge/references/deployment.md`

#### Task 6: Security Audit
- **Description:** Audit the completed feature for fail-open abuse, irreversible-block safety, log injection/redaction, secret/private-data leakage in the corpus, malformed fingerprint handling, SQLite boundaries, and unchanged review authorization/token behavior. Write the security report without modifying implementation files.
- **Skill:** security-auditor
- **Reviewers:** none
- **Files to modify:** `work/dedup-broad-precision/logs/audit/security-audit.json` (new)
- **Files to read:** `news_bot.py`, `admin_alerts.py`, `pending_articles_repo.py`, `tests/fixtures/dedup_broad_precision.json`, `tests/test_dedup_broad_precision.py`, `tests/test_integration.py`, `tests/test_admin_alerts.py`, `tests/test_no_token_leak_in_logs.py`

#### Task 7: Test Audit
- **Description:** Review corpus provenance/integrity and feature tests for behavioral assertions, state verification, boundary coverage, mutation value, fragmentation, and preservation of existing distinctive/fail-open controls. Write the test-audit report without modifying implementation files.
- **Skill:** test-master
- **Reviewers:** none
- **Files to modify:** `work/dedup-broad-precision/logs/audit/test-audit.json` (new)
- **Files to read:** `tests/fixtures/dedup_broad_precision.json`, `tests/test_dedup_broad_precision.py`, `tests/test_integration.py`, `tests/test_model_extractor.py`, `tests/test_admin_alerts.py`, `tests/test_no_token_leak_in_logs.py`, `work/dedup-broad-precision/user-spec.md`, `work/dedup-broad-precision/tech-spec.md`

### Final Wave

#### Task 8: Pre-deploy QA
- **Description:** Execute acceptance traceability, focused and full regression suites, JSON validation, scoped hooks, and diff checks over the final feature state. Produce a machine-readable QA report and leave deployment blocked unless every irreversible-block, corpus, defer, telemetry, and fail-open criterion passes.
- **Skill:** pre-deploy-qa
- **Reviewers:** none
- **Files to modify:** `work/dedup-broad-precision/logs/working/pre-deploy-qa-report.json` (new)
- **Files to read:** `work/dedup-broad-precision/user-spec.md`, `work/dedup-broad-precision/tech-spec.md`, `news_bot.py`, `admin_alerts.py`, `model_extractor.py`, `tests/fixtures/dedup_broad_precision.json`, `tests/test_dedup_broad_precision.py`, `tests/test_integration.py`, `tests/test_model_extractor.py`, `tests/test_admin_alerts.py`, `work/dedup-broad-precision/logs/audit/code-audit.json`, `work/dedup-broad-precision/logs/audit/security-audit.json`, `work/dedup-broad-precision/logs/audit/test-audit.json`

#### Task 9: Manual production deployment
- **Description:** Release the approved commit through the established manual production flow, preserve a known-good rollback target, and verify the rebuilt service starts cleanly with the existing configuration and state. Do not change runtime configuration unless rollback mitigation is required.
- **Skill:** deploy-pipeline
- **Reviewers:** code-reviewer, security-auditor, deploy-reviewer
- **Verify-smoke:** `docker compose ps --status running` -> the news service is running; `docker compose logs --tail=200 news-bot` -> clean startup with no E016/schema/startup regression
- **Verify-user:** explicitly authorize the release and authorize rollback if startup verification fails
- **Files to modify:** `work/dedup-broad-precision/logs/working/deploy-report.json` (new; sanitized outcome and commit metadata only, no raw logs or configuration values)
- **Files to read:** `.claude/skills/project-knowledge/references/deployment.md`, `work/dedup-broad-precision/tech-spec.md`, `work/dedup-broad-precision/logs/working/pre-deploy-qa-report.json`

#### Task 10: Post-deploy precision verification
- **Description:** Observe naturally occurring dedup decisions, classify E014 outcomes, compare daily suppression counts with bounded diagnostic logs, and watch for an unexpected E015/E016 change. Record the evidence and final milestone verdict without injecting test articles or publishing from a verification tool.
- **Skill:** post-deploy-qa
- **Reviewers:** none
- **Verify-user:** classify each naturally occurring E014 during the user-spec observation window and report any false flag, missed duplicate, or E015 spike
- **Files to modify:** `work/dedup-broad-precision/logs/working/post-deploy-qa-report.json` (new; no private operator identifiers)
- **Files to read:** `work/dedup-broad-precision/user-spec.md`, `work/dedup-broad-precision/tech-spec.md`, `.claude/skills/project-knowledge/references/deployment.md`, `work/dedup-broad-precision/logs/working/deploy-report.json`
