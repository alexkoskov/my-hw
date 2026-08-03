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

> **Отчёты ревью УТРАЧЕНЫ (пометка 2026-08-03).** 1 ссылок на `logs/…` ниже
> ведут в никуда: этих файлов нет ни на диске, ни в истории git. Причина —
> правило `logs/` в `.gitignore` срабатывало на любой глубине и не пускало
> `work/*/logs/` в репозиторий, поэтому отчёты жили только на машине, где
> выполнялась работа. Правило сужено 2026-08-03, но задним числом файлы не
> восстанавливаются. **Сам факт, что ревью проводились, достоверен** — он
> записан в тексте ниже; недоступны только подробные JSON-отчёты.


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

## Task 7: Code Audit (holistic)

**Status:** Done
**Commit:** 0351438 (audit findings applied)
**Agent:** code-reviewer (opus, analysis-only) + audit-fixer
**Summary:** Holistic whole-feature code audit — verdict **READY**, 0 blockers/majors. Every load-bearing invariant verified live: pair-key format byte-consistent producer↔all consumers (8/8 fixture), gate terminality (block/flag terminal, backstop only on pass), fail-safe polarity, no duplicate init, FILES-array invariant, back-compat/degraded/toggle-off/carry-through. Applied M1 (acronym regex derived from aliases — kills drift + closes lowercase-`zamac` gap) + M2 (runbook note: broad-tier republish now soft-flags instead of the legacy ≥50% hard-block). 2 nits (N3/N4) left as cosmetic.
**Deviations:** None. Report: [logs/working/task-7/code-audit.md].

**Reviews:** self (audit). Fix re-review — code-reviewer: approve (ReDoS-safe confirmed) → [logs/working/task-7/code-audit-fix-review.json]

**Verification:** `pytest -q` → 1244 passed; ReDoS probe worst 3.9ms/214KB; 8/8 fixture holds.

## Task 8: Security Audit (holistic, OWASP)

**Status:** Done
**Commit:** — (analysis-only; 0 findings, no fix needed)
**Agent:** security-auditor (opus, analysis-only)
**Summary:** Holistic OWASP whole-feature audit — verdict **PASS-WITH-NOTES**, 0 critical/major/minor (2 informational). All 4 target threats confirmed safe as verified invariants: ReDoS (bounded, ~6ms/50KB), SQL (`json_extract` static literal, `days` parameterized, tokens stay in the opaque blob), plain-text pings (`parse_mode=None` + `_redact_text`), lexicon charset assertion (all 12 canonicals, no `|`/newline). No new deps/imports/secrets; degraded `[E016]` logs a full traceback (no silent swallow).
**Deviations:** None. Report: [logs/working/task-8/security-audit.md].

**Reviews:** self (audit). No fix required.

**Verification:** targeted threat probes + OWASP trace — all clean.

## Task 9: Test Audit (holistic)

**Status:** Done
**Commit:** 0351438 (audit findings applied)
**Agent:** test-reviewer (opus, analysis-only) + audit-fixer
**Summary:** Holistic test-quality/coverage audit — verdict **PASS**, 0 critical/high. Calibration harness clean (new pair-tier classifier; both asymmetric hard invariants non-vacuous), fixture 8/8 self-consistent with the real extractor. Applied M-1 (pin pair-1's must-NOT-hard-block directly, not just via the aggregate) + L-1..L-4 (stale fixture comments; `TestSqlAudit` now scans `backfill_fingerprints.py`; a documenting test for the model-token exact-match limitation; an end-to-end AC8 both-empty test through real extraction).
**Deviations:** None. Report: [logs/working/task-9/test-audit.md].

**Reviews:** self (audit). Fix re-review — test-reviewer: approve (mutation-verified) → [logs/working/task-9/test-audit-fix-review.json]

**Verification:** `pytest -q` → 1244 passed (+3 net tests); M-1/L-3/L-4 mutation-verified.

## Task 9: Test Audit

**Status:** Done
**Agent:** main agent (audit teammate)
**Summary:** Holistic test-quality audit of the whole feature test layer. **Verdict: PASS (pass-with-findings)** — 0 critical, 0 high, 1 medium, 4 low. Confirmed the critical axis: the calibration harness runs the NEW classifier (`shares_pair`/`any_distinctive`/`|D` tier), no `_classify(similarity())` remains; the two asymmetric hard invariants (3 SDCC-must-block / not-dupes-must-not-block) are pinned as separate, non-vacuous tests; both behaviour-reversal tests genuinely pin (same-source distinctive block + retained same-source-no-series-publishes half); backstop-blocks-positive, terminal-verdict, single-fetch, 7-day boundary, AC2 carry-through, AC8 both-empty, backfill (idempotency + widened re-select + corrupt-blob + 30-day window), and degraded mode all covered. Grounding run 138 passed; fixture 8/8 self-consistent with the real extractor. Medium (M-1): pair-1 (the one broad dupe in `DUPE_PAIRS`) has its irreversible must-not-hard-block property pinned only by the ≥7/8 aggregate, not by the dedicated not-dupe invariant (which iterates `NON_DUPE_PAIRS` only) — currently caught coincidentally via pair-7's shared `car culture` series; fix is a one-line selector broadening. Lows: stale fixture comment referencing the removed `test_calibration_real_pair_must_pass`; `TestSqlAudit` not scanning `backfill_fingerprints.py` (already a tech-spec note; static-literal predicate → nil risk); model-token exact-match limitation lacks a documenting test; AC8 both-empty exercised via mock only. Full report → [logs/working/task-9/test-audit.md].
**Deviations:** None (analysis-only task; no code/tests changed). No spec/tech-spec defect found.

## Task 10: Pre-deploy QA

**Status:** Done
**Agent:** qa-runner
**Summary:** QA **passed** — zero criticals. Whole suite `python3 -m pytest -q` → 1244 passed, 0 failed, 0 skipped (no `-k` exclusion). 19 acceptance checks (AC1–AC11 + 6 tech-spec ACs + 2 live-only): 17 passed, 2 not_verifiable (operator/live), 0 failed. All four focus points confirmed against real code paths: toggle-off parity (`DEDUP_SERIES_ENABLED=0` → pair rule short-circuited, identical to legacy dedup; off-word set verified `0/false/no/off` any-case, unset/blank → ON), degraded publishes (`[E016]` rate-limited + article publishes), calibration 8/8 with both asymmetric hard invariants non-vacuous (real `extract_fingerprint`+`shares_pair` harness), FILES-array invariant (no new module; all touched modules in deploy.sh/deploy.yml/deploy_test.yml), no regressions. Backfill widened re-select confirmed on an isolated temp DB (scans in-window rows missing `$.pairs`, skips 4-key rows, honours the 30-day window). 1 minor finding: documented model-token exact-match limitation (safe-direction false-negative, accepted). No blockers — cleared to deploy.
**Deviations:** None. Full report: [logs/working/qa-report.json].

**Deferred to post-deploy:** 3 live/operator criteria (Task 11) — pre-deploy cold-DB `SELECT COUNT` on Moscow prod; `backfill --days 30` on prod + re-count > 0; 2-week `@myhwchannel` spot-check of live `[E015]`/`[E014]`. Contract in `deferredToPostDeploy` of qa-report.json.

**Verification:**
- `python3 -m pytest -q` → 1244 passed, 0 failed, 0 skipped
- `tests/test_model_extractor.py` → 63 passed (incl. 3 calibration tests)
- `tests/test_integration.py::TestCrossSourceDedup` → 18 passed
- `tests/test_admin_alerts.py` + `tests/test_backfill_fingerprints.py` + `tests/test_deploy_files_invariant.py` → 64 passed
- Toggle-parse + SDCC-pair + backfill widened-select smoke → all as expected

## Post-deploy fix: theme-only key precision (2026-07-28)

**Status:** Done
**Agent:** main agent (`/write-code` shortcut — S-size, 2 files)
**Summary:** Prod `[E014]` false-flag: a t-hunted PT «lote da série Pop Culture» post and an autoevolution EN «Super Treasure Hunt … Is a Lincoln» article soft-flagged each other. Neither side had an extractable model (Lincoln is outside the 36-brand lexicon; the case-sensitive `Lotus` pass captures brand-only), so both degraded to the theme-only key `*|pop culture|B` — and autoevolution's «pop culture» was ordinary prose («a pop culture icon»), not a Hot Wheels line name. Amends **Decision 1**: the theme-only variant is no longer emitted for every recognised series. New `_theme_only_eligible(canonical, tier)` gates it to one-off franchises/events — lexicon-`distinctive` (derived through `_tier_suffix`, so the unknown-tier→broad fail-safe governs here too) AND not in the new `_RECURRING_SERIES` frozenset (в коде: `_RECURRING_PROGRAMS`, `model_extractor.py:224`) (`super treasure hunt`, `red line club`: distinctive, but shipped continuously, so the program name alone identifies no particular news item). Broad recurrent car-lines and recurring programs now contribute NO key without a concrete model. Model-bearing keys `"<model>|<series>|<tier>"` and the whole `strict`/`brands`/`series`/`similarity()` path are byte-unchanged, so the change narrows MATCHING, never the lexicon — `series` still lists `pop culture` on both sides. A load-time assertion pins every `_RECURRING_SERIES` (в коде: `_RECURRING_PROGRAMS`) entry to a real lexicon canonical (a rename would otherwise silently restore the noisy key).
**Deviations:** None. Trade-off accepted: two genuine dupes about the same broad-line drop where NEITHER names an extractable model no longer soft-flag — an under-match in the fail-safe direction (costs a flag, can never cause a silent hard block).

**Open asymmetry (noted, not changed):** ⚠️ **Superseded 2026-07-28 (вечер) — см. «Post-deploy fix 2: recurring programmes are BROAD, not distinctive» ниже: обе программы перетегированы в `broad`, поэтому `|D` HARD-BLOCK на пути с моделью они больше НЕ дают (только мягкий флаг `[E014]`); ложный `[E015]`, которого ждал последний абзац этой записи, всплыл в тот же день. Абзац ниже оставлен как запись исходного намерения.** `super treasure hunt` / `red line club` lose theme-only power but keep full `|D` HARD-BLOCK power on the model-bearing path — two STH articles naming the same casting within 30 days block irreversibly. That is the intended reading (same casting + same program = the same release), but it rests on the model token being right; the comment justifying `_RECURRING_PROGRAMS` («shipped continuously») argues the opposite way for the theme-only path, so the two paths are deliberately, not accidentally, asymmetric. Revisit if a false `[E015]` on an STH pair ever appears.

**Reviews:** code-reviewer (`approved_with_suggestions`, 0 critical / 2 major / 5 minor) + test-reviewer (`needs_improvement`, 2 major / 5 minor) → [logs/working/task-standalone/]. All 14 findings applied except one perf-only suggestion (dropping the now-dead `and not series` clause from the gate short-circuit) — skipped as out of scope: it is behaviour-neutral, and retiring it would also retire `test_theme_only_pop_culture_not_short_circuited`, whose only observation is exactly that fetch. Comment corrected instead.

**Verification:**
- `python3 -m pytest -q` → 1582 passed
- `tests/test_model_extractor.py` → 70 passed; `TestCrossSourceDedup` → 21 passed
- Calibration fixture gained **pair-9** (the real prod bodies) + a THIRD hard invariant `test_calibration_non_dupes_share_no_pair` (`any_shared is False` for every non-duplicate pair). Needed because the old invariants are blind to this bug class: the incident was a shared `|B` key, so `expected_any_distinctive is False` was already satisfied by the buggy code. Floor moved ≥7/8 → ≥8/9 (same one-error budget) and now also asserts `expected_shared_pairs` per pair.
- Mutation-verified: forcing `_theme_only_eligible` to `return True` (pre-fix behaviour) fails `test_calibration_non_dupes_share_no_pair` AND the new gate-level `test_broad_line_prose_comention_does_not_soft_flag`; both pass again on restore.
- Existing fixture pairs unaffected: pair-1/pair-7 ride on `<model>|car culture|B`, pair-6 on `*|stranger things|B` (a one-off franchise, still eligible)

## Post-deploy fix 2: recurring programmes are BROAD, not distinctive (2026-07-28, вечер)

**Status:** Done
**Agent:** main agent
**Summary:** Прод-инцидент через 4 минуты после выкатки первого фикса. `[E015]` жёстко заблокировал и ОТБРОСИЛ пост t-hunted про один RLC-эксклюзив (1985 Audi Sport Quattro S1, $28, Mattel Creations) против статьи autoevolution «Unboxing: 10 Affordable Cars for July of 2026». Совпавшая пара — `audi sport|red line club|D`. Общего у статей нет: один конкретный премиум-дроп против распаковки десяти дешёвых мейнлайнов. Причина — незакрытая асимметрия, которую утренняя запись выше сама же пометила как «пересмотреть, если всплывёт ложный `[E015]`»: `_RECURRING_PROGRAMS` гасил только тема-ключ, а на пути с моделью `super treasure hunt` / `red line club` сохраняли тир `D`. Обоснование «та же модель + та же программа = тот же релиз» ломается о статью-ПОДБОРКУ: она называет десяток кастингов, поэтому любой из них может столкнуться с мимоходом упомянутой программой. Исправлено в лексиконе — обе программы перетегированы в `broad`; `_RECURRING_PROGRAMS` остался как документированный список с load-time проверкой «каждый элемент обязан быть broad», чтобы обратное перетегирование падало на импорте, а не молча возвращало необратимый ложный блок в прод.
**Deviations:** None. Принятый размен: два поста об одном и том же RLC-кастинге теперь дают мягкий флаг вместо жёсткого блока. Это верное направление — ложный жёсткий блок необратим (статья дропается И пиннится в `processed_news`), пропущенный дубль оператор просто удаляет.

**Verification:**
- `python3 -m pytest -q` → 1620 passed
- Реальная прод-пара закреплена end-to-end (`test_prod_false_block_roundup_vs_single_drop`): совпадение остаётся, но только `|B` → мягкий флаг
- Тест `test_recurring_program_still_pairs_with_a_model`, фиксировавший ровно неверное намерение, заменён на `test_recurring_program_with_a_model_is_broad_not_distinctive` — и теперь идёт через РЕАЛЬНЫЙ лексикон, а не через тир, переданный руками
