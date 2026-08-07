# Test Audit — `source-formatting-parity` Phase 1 (Task 12)

- **Feature:** source-formatting-parity, Phase 1 (Tasks 1–9)
- **Date:** 2026-08-06
- **Branch:** `dev`, tree byte-identical to `4ad4592`
- **Suite ground state:** 1899 collected / 1899 passed (+504 subtests) — no red tests
- **Pre-feature base commit used for diffs:** `ccdc1a7` (= `26d6954^`)
- **Verdict: pass-with-findings** — 0 critical, 3 high, 3 medium, 3 low, 3 informational

The single defect that would have made this a `fail` — an alignment guard whose
false-positive control does not bite — was **measured** and it bites hard (19 kills, § 7 M1).
The three `high` findings are all defects in the **verification gate**, not in the feature:
a selector that collects nothing, a tech-spec command that was never narrowed, and one
negative control that the tech-spec names by hand and nobody wrote.

> Retry note: a previous attempt died holding source mutations. Every mutation below was
> applied by a script that reverts in a `finally:` block, one at a time, never two at once.

---

## 1. `-k` selector census

Baseline column = "collected BEFORE the feature", measured at decomposition 2026-08-05 on the
unmodified tree (1693 tests then), quoted from Task 12 Details. "Now" measured this session
from the repo root with `python3 -m pytest -q -k "<sel>" --collect-only`.

Verdict rule (from the task): honest iff (a) ≥1 test collected, (b) at least one collected
test belongs to THIS feature, (c) the delta vs baseline is non-zero.

| Selector | Source | Before | Now (global) | Now (file-scoped) | Feature tests among them | Verdict |
|---|---|---|---|---|---|---|
| `mismatch` | user-spec step 4 (AC8) | 8 | **21** | — | 13 in `test_news_bot_alignment.py` | **honest** (+13); 8 namesakes ride along |
| `mismatch or aligned` | tech-spec Task 8 Verify-smoke | 8 | **26** | **16** (`test_news_bot_alignment.py`) | 13+3 alignment, 2 t-hunted | **honest**; narrowing landed in task 8 only — see H-2 |
| `aligned` | derived | 0 | **5** | — | 3 alignment + 2 t-hunted | **honest** (+5, ≥3 required) |
| `heading_heuristic` | user-spec step 3 | 0 | **0** | **0** | none | **VACUOUS** — see H-1 |
| `heading` | Task 7 | 7 | **38** | — | 16 `test_dom_blocks.py`, 7 `test_t_hunted_source.py` | **honest** (+31) |
| `markers_lost` | user-spec step 5 (AC6) | 0 | **4** | — | all in `test_integration.py` | **honest** (was the one already fixed in Task 9) |
| `bold_heavy` | user-spec step 6 (AC7) | 0 | **3** | — | all in `test_integration.py` | **honest** |
| `image_limit` | user-spec step 7 (AC9), Task 6 | 3 | **22** | — | 11 `test_telegraph_publisher.py`, 3 `test_integration.py`, 3 `test_fallback_publish_paths.py` | **honest** (+19) |
| `flag_off` | user-spec step 9 (AC11), Task 2 | 2 | **6** | — | 2 `test_t_hunted_source.py`, 1 `test_feature_flags.py`, 1 new in `test_integration.py` | **honest** (+4); 2 namesakes remain |
| `list_item` | tech-spec Task 3 Verify-smoke | 6 global / **0** in claude | 21 | claude **2**, gemini **2**, openai **2**, openrouter **3** | all four engines | **honest only file-scoped** (as designed) |
| `bound` | Task 4 (≥6 required) | 14 global / **0** in llm_common | 31 | **9** in `test_llm_common.py` | 9 ≥ 6 ✓ | **honest only file-scoped** |
| `align or corpus` | Task 7 (≥2 required) | 0 | 35 | **18** in `test_t_hunted_source.py` | 18 ≥ 2 ✓ | **honest** |
| `FormattingChain or MarkersLost or BoldHeavy or ImageCap or ChecklistFloor` | Task 9 | 0 | **20** | — | all in `test_integration.py` | **honest** |
| ↳ `FormattingChain` | — | 0 | **6** | — | yes | honest |
| ↳ `MarkersLost` | — | 0 | **4** | — | yes | honest |
| ↳ `BoldHeavy` | — | 0 | **3** | — | yes | honest |
| ↳ `ImageCap` | — | 0 | **3** | — | yes | honest |
| ↳ `ChecklistFloor` | — | 0 | **4** | — | yes | honest |

**Result: 1 of 13 selectors is vacuous — `heading_heuristic`.** Every other selector collects
≥1 test of this feature with a non-zero delta. The Task 9 union was checked disjunct by
disjunct: none of the five is empty, so the union is not hiding four unwritten classes.

### 1a. `mismatch or aligned` (explicitly required by AC)

Confirmed on the current tree. Without a file it collects **26**, of which the 8 pre-feature
namesakes the decomposition measured are all still there — `test_admin_alerts.py` TZ alerts,
the paragraph-count `test_count_mismatch_*` in the four engine files, `test_mattel_news_source.py`,
`test_job_distributed_publish.py`. None is about block/paragraph desync. Task 8 recognised this
and its own task file narrowed the command to
`tests/test_news_bot_alignment.py -k "mismatch or aligned"` → **16 collected**, all this
feature's. But `tech-spec.md:580` still carries the unnarrowed form, and that is the document
Task 13 and the AVP read. → **H-2**.

### 1b. `heading_heuristic` (explicitly required by AC)

`pytest -k heading_heuristic` collects **0 tests**, exits 0, and reads as a pass. Its user-spec
expectation ("80 chars → heading, 81 → not") was cancelled by the approved tech-spec deviation
that removed the length limit, so the *expectation* is legitimately dead. The *behaviour*,
however, did not go away: it moved into the five named negative controls, which all exist and
all match `-k heading` (38 collected, 23 of them this feature's). So this is **not a coverage
gap — it is a live vacuous gate**: whoever works down the user-spec verification table gets a
green tick for a command that ran nothing. → **H-1**.

---

## 2. Form 1 of the 2026-08-04 lesson — assertions matching static text

Method: for each suspect assertion, grep the implementation's static strings for the expected
substring.

| Assertion | Static-text collision? | Status |
|---|---|---|
| `test_news_bot_alignment.py:88-89` — WARNING must name link + both counts, asserts `"5" in text and "4" in text` | **No.** `news_bot.py:3007-3012` template is `"[align] blocks/paragraphs mismatch for %s: patchable_blocks=%d paragraphs=%d — dropping blocks…"` — no digits in the static part, and `LINK` carries none | **covered**, but see **L-1** (order-blind) |
| `test_llm_common.py:153-161` — bound WARNING, asserts `str(len(text))` (=504) and `"101"` | **No.** Case-specific numbers, exactly the prescribed shape | **covered** |
| Heading heuristic — all assertions | Assert `is True` / `is False` on `looks_like_heading`, or `b["type"] == "heading"`. **Never** "the heading text is present" | **covered** — the prescribed shape |
| Heading level | `test_dom_blocks.py:241-244` asserts the whole dict incl. `"level": 3` as a **literal** | **covered** |
| AC12 preview — `test_preview_renderer.py:629-636` | Asserts `"<h3>Case A breakdown</h3>"` and `"<strong>Alphard</strong>"` — tag **and** the word inside it; and `test_preview_does_not_swallow_words_around_bold` is a dedicated guard for the 2026-07-28 "unknown tag dropped WITH its children" bug | **covered** — the exact weakness the task warned about is explicitly closed |

**No Form-1 defect found.** The suite consistently asserts on type/structure, not on text
presence.

---

## 3. Form 2 — expectations derived from the constant under test

| Site | Derived? | Killing mutation | Result |
|---|---|---|---|
| `test_llm_common.py:121-151` request-path bounds | **No** — literals `100`, `101`, `100_000`, `100_001` on both poles | `_MAX_RUNS_PER_BLOCK = 0` must kill `test_run_count_at_bound_is_still_encoded` | safe by construction |
| `test_dom_blocks.py:361-393` render-path bounds | **Yes** — input built from `dom_blocks.MAX_RUNS_PER_BLOCK` | M12: `MAX_RUNS_PER_BLOCK = 0` | **kills 10+ tests** incl. `test_within_the_bound_runs_are_kept` → the lower pole is real despite being derived |
| Image cap | **No** — `test_image_limit_per_source_30_and_10` uses literal `30`/`10`; `TestPerSourceImageCap` reads `SOURCE_IMAGE_LIMITS` but adds `assertLess(default_cap, IMAGE_COUNT)` and a second run at a literal control cap of 5 | M4: hard-code `10` in the renderer | **kills 9 tests** incl. `[t-hunted-30]` |
| Heading levels | **No** — literal `3` | — | safe |
| Bold-span counts in the chain | **No** — `EXPECTED_BOLD_SPANS = 20` / `EXPECTED_HEADINGS = 12` pinned literals, with an explicit comment that a derived count cannot tell "bold survived" from "there was no bold" | — | safe, and the best-written case in the suite |
| Golden gate | **No regeneration mode** — `_GOLDEN` is loaded from disk, never re-shot; `test_golden_records_todays_gallery_render_counts` binds `IMAGE_LIMIT == 10` to a literal | — | safe; see **L-3** for a cosmetic overclaim |

**No Form-2 defect found.** One derived construction exists (`test_dom_blocks` bounds) and was
proven to still bite.

---

## 4. Mandated negative controls

| # | Control (tech-spec § Testing Strategy / Task 12 Details) | Test | Status |
|---|---|---|---|
| 1 | partially-bold short paragraph → NOT a heading | `test_dom_blocks.py:192 [partial-coverage]`; `test_t_hunted_source.py:642 [partial-bold]` | **covered** |
| 2 | whole-bold ending in `.` → NOT a heading | `test_dom_blocks.py:173 [period]`; t-hunted `[ends-with-period]` | **covered** |
| 3 | whole-bold **long** → IS a heading | `test_dom_blocks.py:199` (81/101/200 words); `test_t_hunted_source.py:648` | **covered** |
| 4 | `<p><strong>Part</strong> and <strong>two</strong></p>` → NOT whole-bold | `test_dom_blocks.py:192 [two-separate-spans]` | **covered** |
| 5 | `<p><strong>Ford</strong> vs Ford</p>` — repeated substring must not fake coverage | `test_dom_blocks.py:192 [repeated-substring-must-not-fake-coverage]` | **covered** |
| 6 | heuristic OFF for orangetrack, ON for t-hunted — asserted per source | `test_dom_blocks.py:207` (default off **and** opt-in on, in one test) + t-hunted `TestHeadingHeuristic` + the golden gate | **covered** |
| 7 | guard false-positive control; `None` vs `[]` explicit | 6 dedicated tests at `test_news_bot_alignment.py:116-177`, incl. one that runs the **real** orangetrack parser, plus `test_mismatch_stores_sql_null_not_empty_list` asserting SQL NULL rather than `'[]'` | **covered — and measured to bite** (§ 7 M1) |
| 8a | flag OFF → t-hunted emits no blocks | `TestFeatureFlag` ×2; `test_integration.py:6068` asserts `off['blocks'] is None` with an explicit message | **covered** |
| 8b | flag OFF → **orangetrack still emits blocks** | *(none)* | **MISSING — measured** → **H-3** |
| 9 | non-YouTube host with a YouTube-shaped path → NOT wrapped; Vimeo IS | `test_dom_blocks.py:331` + `:341` (both poles in adjacent tests) | **covered** |
| 10 | cap that does not bite truncates nothing | `test_integration.py:5974`; `test_image_limit_none_means_unlimited`; `test_image_limit_boundaries_are_literal[0-0]` | **covered** |
| 11 | AC4 — bold added by the translator where the original had none is NOT stripped | `test_telegraph_publisher.py:1030-1041` (block with `**` and no runs → real `<strong>`, no literal `**`). Carve-out: when the block **does** carry runs, the model's extra markers are stripped and the source wins — pinned at `:1055-1072` with the policy spelled out | **covered** |

10 of 11 present. The one gap is #8b and the tech-spec names it in so many words
(`tech-spec.md:357-358`: "flag off ⇒ the three new parsers emit no `blocks`; **orangetrack
still does** (it must not be gated)").

---

## 5. Both poles of every invariant (2026-07-28 lesson)

| Invariant | Pole A | Pole B | Status |
|---|---|---|---|
| Heading heuristic | whole-bold, no period → heading ✓ | five distinct "everything else" shapes ✓ | **both** |
| Alignment guard | mismatch → dropped + WARNING ✓ (M2: 7 kills) | aligned → kept, no WARNING ✓ (M1: 19 kills) | **both, measured** |
| Kill switch | ON → t-hunted emits blocks ✓ | OFF → t-hunted silent ✓ / **orangetrack still emits ✗** | **one-sided** → H-3 |
| Request-path bound | over → runs stripped, text intact ✓ | at/under → runs kept ✓ (literal 100) | **both** |
| Render-path bound | over → runs stripped ✓ | under → runs kept ✓ (M12 kills it) | **both** |
| Image cap | above → cut to N, hero kept, tail dropped, order preserved ✓ | below → untouched ✓ | **both** |
| Image dedup key | size variants collapse ✓ | distinct images do **not** collapse ✓ (same test, both halves) | **both** |
| Video host gate | allowed host → wrapped ✓ | look-alike host → not wrapped ✓; Vimeo data ≠ YouTube data ✓ | **both** |
| Translation markers | markers present → bold restored ✓ | markers lost → plain text, `send_admin_notification` not called ✓ | **both** |
| `list_item` in engines | patched from the main response ✓ (4× `test_patched_types_include_list_item_and_match_shared`, whole-tuple equality against `_llm_common`) | does **not** reach the caption pass ✓ (4× `test_list_item_text_skipped_by_second_pass`, each with an anti-vacuity guard on the translation count); openrouter also has the opposite pole `test_list_item_translated_when_skip_patched_text_false` | **both, per engine** |
| Intake floor vs the flag | borderline article stays in ✓ | true bare checklist stays out ✓ — checked in the same test, in both flag states | **both** |

**One one-sided invariant found: the kill switch.**

---

## 6. Coverage vs tech-spec § Testing Strategy, line by line

| tech-spec bullet | Where | Status |
|---|---|---|
| `dom_blocks` positive: bold in paragraph, nested bold-in-italic, overlapping first-wins, empty/whitespace skipped, no doubled spaces, every run locatable via `text.find` | `test_dom_blocks.py:55-152`, `test_llm_common.py:95-104` | **covered** |
| 5 heading negative controls | § 4 rows 1–5 | **covered** |
| heuristic off for orangetrack, on for the others — per source | § 4 row 6 | **covered** |
| h2/h3/h4 → level 3, h5 → paragraph, h1/h6 dropped | `test_dom_blocks.py:238-257` | **covered** |
| `<br>` splitting, bullet-less `<li>` | `test_dom_blocks.py:259-283` | **covered** |
| image dedup key both ways | `test_dom_blocks.py:292-306` | **covered** |
| video host gate both ways | `test_dom_blocks.py:331-353` | **covered** |
| resource bounds: runs stripped, article intact | `test_dom_blocks.py:361-401`, `test_llm_common.py:107-196` | **covered**, but the "text intact" half is truncation-blind → **M-2** |
| kill switch: 3 new parsers silent, **orangetrack still emits** | half | **partial** → **H-3** |
| chain parser → SQLite → translation → publisher nodes | `TestFormattingChainTHunted` (6 tests, incl. an explicit SQLite round-trip) | **covered** |
| alignment guard + false-positive control + `None` vs `[]` | `test_news_bot_alignment.py` (16 tests) | **covered** |
| …"the mismatch is reached through the REAL parser via the title-dedup divergence, not via a fabricated dict" | every **positive** mismatch test uses hand-built dicts; the **false-positive** control does use the real parser | **not satisfiable as written** → **M-1** |
| image cap: per-source value threaded, t-hunted 30 AND lamley 10, lower control | `test_telegraph_publisher.py:1227-1237` + `TestPerSourceImageCap` | **covered** |
| AC6 `markers_lost` → plain text, no ping | `TestMarkersLostDegradeSilently` (4) | **covered** |
| AC7 `bold_heavy` ≥90 % bold, every span reaches nodes | `TestBoldHeavyArticle` (3), pinned literal span count | **covered** |
| AC3 behavioural half in all four engine files | 4× `test_list_item_text_skipped_by_second_pass` | **covered** |
| article near the 500-char checklist floor not newly dropped | `TestChecklistFloorNotNewlyDropped` (4) incl. the negative control | **covered** |
| orangetrack golden-file equality | `test_orangetrack_golden.py` (4 tests + 1 case per fixture, plus a fixture-set drift guard) | **covered** |
| every named `-k` selector matches ≥1 collected test | § 1 | **1 violation** → H-1 |

### tech-spec acceptance criterion `grep -c "strong\|<b>\|<em>"`

Was 0 across all three parser test files before the feature.

| File | Count now | Verdict |
|---|---|---|
| `tests/test_t_hunted_source.py` | **9** | **> 0 ✓** (Phase 1 target met) |
| `tests/test_lamley_source.py` | 0 | Phase 2 — correctly untouched |
| `tests/test_autoevolution_source.py` | 0 | Phase 2 — correctly untouched |
| `tests/test_orangetrack_source.py` | 26 | unchanged file; pre-existing |
| `tests/test_dom_blocks.py` | 20 | new |

### AC10 gates — unedited?

`git diff --stat ccdc1a7 -- tests/test_orangetrack_source.py` → **empty**. The 1391-line
orangetrack suite is byte-identical to the pre-feature tree. The gate holds.

`tests/fixtures/orangetrack_golden.json` did change: **+2 / −112**. Classified line by line:
10 removed `figure` nodes, 10 removed `img` tags with their `x10..x19.jpg` srcs and wrappers,
and the two summary counters `preview_figure_nodes` and `preview_img_nodes` 20 → 10. **Fewer
images and nothing else** — exactly the operator's sanctioned Task 6 deviation, no other field
moved. `tests/test_orangetrack_golden.py` was amended (threading `image_limit=IMAGE_LIMIT`
into `_render` and re-pinning the third number); the amendment **tightens** the gate rather
than loosening it, and there is still no regeneration mode in the repo.

---

## 7. Mutations run

All applied one at a time via a script reverting in `finally:`, each followed by
`git status --short <file>` confirming an empty result.

| # | Mutation | Expected kills | Measured result |
|---|---|---|---|
| **M1** | `news_bot.py:3007` `if patchable != n_paragraphs:` → `if True:` (**guard always fires** — the feature dies silently in prod) | the false-positive controls | **19 kills**, incl. all 6 false-positive controls, `test_real_orangetrack_article_stays_aligned`, `test_alignment_guard_does_not_fire_on_a_real_t_hunted_article`, and the whole `TestFormattingChainTHunted` class. **The single most important control in the feature is real.** |
| **M2** | same line → `if False:` (**guard never fires**) | the positive mismatch tests | **7 kills**, incl. `test_mismatch_drops_blocks`, both directions of `test_mismatch_in_either_direction_drops_blocks`, the WARNING test and the SQL-NULL test |
| **M3** | `t_hunted_source.py:329` drop `"list_item"` from the flat-paragraph derivation (the retyped-tuple drift Task 10 flags) | ≥1 | **2 kills**: `test_list_items_emit_list_item_blocks`, `test_list_item_reaches_telegraph_with_exactly_one_bullet`. Drift is caught **behaviourally** |
| **M4** | `telegraph_publisher.py:477` `[:image_limit]` → `[:10]` (single hard-coded cap) | the t-hunted-30 test | **9 kills**, incl. `test_image_limit_per_source_30_and_10[t-hunted-30]` and both `TestPerSourceImageCap` value tests |
| **M8** | `dom_blocks.py:391` over-bound path returns `flat[:50]` (**truncates a 100 k-char paragraph to 50 chars**) | the "text survives in full" test | **0 kills** across `test_dom_blocks` + `test_t_hunted_source` + `test_integration` + `test_orangetrack_golden`. **Survivor** → **M-2** |
| **M11** | `orangetrack_source.py:400` gate `blocks` on `feature_flags.source_formatting_enabled()` (AC10 violation the tech-spec forbids by name) | the "orangetrack still emits" control | **0 kills** across `test_orangetrack_source` + `test_orangetrack_golden` + `test_integration` + `test_feature_flags` + `test_news_bot_alignment` + `test_t_hunted_source`. **Survivor** → **H-3** |
| **M12** | `dom_blocks.py:84` `MAX_RUNS_PER_BLOCK = 0` | the under-bound tests (Form-2 probe) | **10+ kills**, incl. `test_within_the_bound_runs_are_kept` and the whole `TestBoldHeavyArticle` class. Derived construction still bites |

Not run, and why: mutations 2, 3, 5, 7, 8, 9 of the Details starter list target behaviours
whose controls I read directly and which are asserted on `is True`/`is False`/`type ==` with
both poles in adjacent tests (§ 4 rows 1–6, 9; § 5 `list_item` row). Their kill sets are
statically obvious and the budget was better spent on the two survivors above. Mutation 10
(title-dedup) is **unbuildable as specified** — see M-1.

---

## 8. General quality

- **Mock leak:** none in the feature's own tests. The false-positive control deliberately runs
  the real orangetrack parser (`test_news_bot_alignment.py:116`), the chain tests run the real
  t-hunted parser over corpus HTML, and the engine tests each execute their **own** module's
  copy of `_translate_block_strings` with a comment explaining why one engine's test proves
  nothing about the other three. The one place where dicts are hand-built is the positive
  mismatch pole, and that is forced — see M-1.
- **Fixture realism:** the corpus is used, not bypassed. `test_corpus_blocks_align_with_paragraphs`
  parametrises over `tests/fixtures/articles/t_hunted/*.html`; the golden gate parametrises over
  every orangetrack fixture on disk **and** guards against a fixture being deleted
  (`test_fixture_set_matches_golden`); `TestChecklistFloorNotNewlyDropped` and
  `TestBoldHeavyArticle` load named corpus articles. No synthetic three-line HTML has been
  substituted for a corpus page. Short inline HTML is used only in true unit tests of
  `dom_blocks`, which is correct.
- **Assertion specificity:** `None` vs `[]` is distinguished in three separate places, twice
  with an explicit failure message saying why the distinction matters, and once down to the
  raw SQLite column (`assert raw is None, f"stored as {raw!r} instead of SQL NULL"`).
- **Order independence:** the run was done with `-p no:randomly`; the suite also passes in its
  normal randomised order (1899/1899). `test_feature_flags.py` reloads the module and restores
  it in `tearDown`; `TestPerSourceImageCap` resets tables between arms.
- **Failure messages:** unusually good. Most assertions carry a message that names the
  *hypothesis* the failure implies ("the configured value is not reaching the renderer, it is
  hard-coded there"), which is what makes a red test actionable at 20:01 МСК.
- **Fragmentation (2026-08-05 rule):** the feature added ~2200 test lines and honours the rule
  — `parametrize`/`subTest` for input sets, one function per behaviour. One redundancy worth
  merging is noted at **L-4**. This audit recommends exactly **one** new test in total.

---

## 9. Findings

### HIGH

**H-1 — `-k heading_heuristic` collects zero tests; the user-spec verification row is a
vacuous gate**
`work/source-formatting-parity/user-spec.md:231`
`pytest -k heading_heuristic` → `no tests collected (1899 deselected)`, exit code **0**. The
row's stated expectation (80 → heading, 81 → not) was cancelled by the approved tech-spec
deviation removing the length limit, but the row was left in place unchanged. Anyone working
the table — including Task 13 — records a pass for a command that executed nothing.
*Consequence of silence:* the heuristic is in fact the best-covered part of the feature, so
this hides no real defect today; it hides the *absence of a check* forever.
**Fix (spec edit, no new tests):** replace the step-3 row with
`pytest tests/test_dom_blocks.py tests/test_t_hunted_source.py -k heading` (**23 collected**)
and replace the 80/81 expectation with the deviation's actual contract: "whole-bold without a
final period → heading at any length; partial bold, two spans, repeated substring or a final
period → paragraph". Add `--collect-only` count checking to the row, as Task 9 did for
`markers_lost`.

**H-2 — tech-spec's Task 8 Verify-smoke selector was never narrowed to a file**
`work/source-formatting-parity/tech-spec.md:580`
Reads `python3 -m pytest -q -k "mismatch or aligned"` with no file. On the pre-feature tree
that command collected 8 foreign tests and passed; it would have certified Task 8 before a
line of it existed. Task 8's own task file fixed this (`tests/test_news_bot_alignment.py -k
"mismatch or aligned"`, 16 collected) but the tech-spec — the document the AVP and Task 13
read — still carries the unsafe form.
*Consequence of silence:* the pre-deploy gate for AC8, the feature's most dangerous failure
mode, is satisfiable by tests that have nothing to do with it.
**Fix (spec edit):** change `tech-spec.md:580` to the file-scoped form and state the expected
count (≥16). Same treatment for the `mismatch` row of the user-spec table (step 4).

**H-3 — the mandated control "flag OFF ⇒ orangetrack still emits blocks" does not exist**
missing; belongs in `tests/test_feature_flags.py` (**not** in `tests/test_orangetrack_source.py`,
which AC10 requires to stay unedited)
`tech-spec.md:357-358` names this control explicitly. **Measured (M11):** adding a
`feature_flags` gate to `orangetrack_source.py:400` — the precise regression the tech-spec
forbids — fails **zero** tests across `test_orangetrack_source.py`, `test_orangetrack_golden.py`,
`test_integration.py`, `test_feature_flags.py`, `test_news_bot_alignment.py` and
`test_t_hunted_source.py`. Nothing in the suite ever parses an orangetrack article with the
flag off. `test_dom_blocks.py::test_module_imports_stay_leaf` guards the shared module against
importing `feature_flags`, but says nothing about the parser.
*Consequence of silence:* the kill switch is the operator's only mid-incident lever. If a
future refactor gates orangetrack too, pulling that lever strips a working source's blocks —
an AC10 violation, discovered only by a reader noticing the page went flat. Deploys are barred
10:00–20:00 МСК, so the fix would wait a day.
**Fix — the one new test this audit asks for:**
```
# tests/test_feature_flags.py
def test_flag_off_does_not_gate_orangetrack(monkeypatch):
    monkeypatch.setattr(feature_flags, "SOURCE_FORMATTING_ENABLED", False)
    article = orangetrack_source.fetch_orangetrack_article(_entry_with_two_paragraphs())
    assert article["blocks"], "the kill switch reached orangetrack — AC10 regression"
    assert [b["type"] for b in article["blocks"]] == ["paragraph", "paragraph"]
```
Reuse the entry dict already written at `tests/test_news_bot_alignment.py:121-132` rather than
inventing a new fixture. Verify by re-running M11: it must then fail.

### MEDIUM

**M-1 — tech-spec requires a mismatch produced by the real parser; the shipped design makes
that unreachable**
`work/source-formatting-parity/tech-spec.md:367-369`
The bullet says the integration mismatch must be "reached through the REAL parser via the
title-dedup divergence (`t_hunted_source.py:198` … skip a `<p>` equal to the title while the
walker does not), not via a fabricated dict". The delivered parser closed that divergence on
purpose: `t_hunted_source.py:286-294` applies title-dedup to **blocks**, and `:327-330` derives
`paragraphs` **from** the surviving blocks — the code comment says so ("Applied to the BLOCKS,
so it cannot desync the two lists"). Both lists now come from one source, so no parser-side
input can make them disagree, and every positive mismatch test necessarily uses hand-built
dicts. Details' starter mutation 10 is unbuildable for the same reason.
*This is a spec defect, not a test defect* — and the outcome is better than the spec asked
for: the *cause* is pinned by `test_title_repeating_paragraph_dropped_from_both_lists`,
`test_title_dedup_survives_the_flattener_change` and `test_corpus_blocks_align_with_paragraphs`
(one case per corpus article), while the guard is tested as the defence-in-depth net it now is.
**Fix (spec edit):** rewrite the bullet to "the desync *cause* is pinned at the parser (title
dedup and boilerplate applied to blocks, flat lists derived from them); the guard is exercised
with constructed block lists because no parser input can desync them any more." Flag to Task 13
so it does not go looking for a test that cannot exist.

**M-2 — the resource-bound test claims "text survives in full" but compares only 40 characters**
`tests/test_dom_blocks.py:363-380` (`test_over_the_bound_runs_are_stripped_and_the_text_survives`)
The docstring states "keeps its text IN FULL. Never truncated, never dropped — this is a
resource fuse, not an editorial threshold", but the assertion is
`runs[0]["text"].strip().replace("  ", " ")[:40] == plain.strip().replace("  ", " ")[:40]`.
**Measured (M8):** truncating the over-bound flat text to 50 characters fails **zero** tests.
A 100 000-character paragraph could be cut to a sentence and the suite would stay green.
*Consequence of silence:* AC7 forbids exactly this — an "editorial" suppression of long or
heavily formatted text. The fuse is supposed to cost formatting, never words.
**Fix (one-line edit, no new test):** compare the whole string —
```python
assert runs[0]["text"].strip() == plain.strip()
```
and if the `replace("  ", " ")` normalisation is genuinely needed, apply it to both sides in
full rather than slicing. Verify by re-running M8: it must then fail.

**M-3 — the t-hunted alignment tests measure against a fourth hand-retyped copy of the
patchable-type tuple**
`tests/test_t_hunted_source.py:453`
`PATCHABLE = ("lead", "paragraph", "heading", "list_item")` is a literal in the test file, so
`_patchable()` — the helper every alignment assertion in that file uses — checks the parser
against the *test's* opinion of the tuple, not against `_llm_common._PATCHED_TEXT_BLOCK_TYPES`.
If the shared tuple gains a type, this file keeps passing. This is the test-side half of the
retyping Task 10 flags as major-2.
*Mitigation, measured:* the practically reachable drift **is** caught behaviourally — M3
(dropping `list_item` from `t_hunted_source.py:329`) kills 2 tests, and `news_bot`'s guard
reads the shared tuple under a monkeypatch test (`test_news_bot_alignment.py:185`), and all
four engines compare their whole tuple to the shared one. The `"lead"` divergence between
`t_hunted_source.py:97` and `:329` is unreachable today because `dom_blocks` never emits a
`lead` block.
**Fix (one-line edit, no new test):** `from _llm_common import _PATCHED_TEXT_BLOCK_TYPES as
PATCHABLE`. `test_integration.py:6114` already does exactly this and is the model to copy.

### LOW

**L-4 is a merge recommendation, not a request for more tests.**

**L-1 — the mismatch WARNING assertion is order-blind**
`tests/test_news_bot_alignment.py:88-89` — `assert "5" in text and "4" in text` passes just as
well if the code logged `patchable_blocks=4 paragraphs=5`. The WARNING is the operator's only
trace (Decision 3b), and swapped counts point the investigation at the wrong list.
**Fix:** `assert "patchable_blocks=5" in text and "paragraphs=4" in text`.

**L-2 — the fail-open test does not assert that the *same* blocks came back**
`tests/test_news_bot_alignment.py:195-204` asserts only `kept is not None`. An `except` branch
that returned a fresh `[]`, or `blocks[:0]`, would pass while the article lost its formatting.
**Fix:** bind the argument and assert identity — `assert kept is exploding`.

**L-3 — a golden test asserts on static JSON while its docstring claims it catches a live
regression**
`tests/test_orangetrack_golden.py:153-167` reads `_GOLDEN[...]["summary"]` — a hand-written
annotation in the fixture that no test derives from a render — and claims "a cap that silently
stops applying fails here". It would not; that failure lands in `test_golden_matches`, which
compares real output. The one live assertion in it is `IMAGE_LIMIT == 10`.
**Fix:** either derive the three counts from `_render("gallery-20-images")` and compare against
the summary, or reword the docstring to "pins the recorded baseline numbers and orangetrack's
literal cap".

**L-4 — duplicated heading negative controls (merge candidate)**
`tests/test_t_hunted_source.py:633-646` re-tests the same three shapes (`partial-bold`,
`two-spans`, `ends-with-period`) already covered as unit cases at `tests/test_dom_blocks.py:179-197`.
What the t-hunted copy uniquely proves is only that the heuristic is **wired on** for this
source, which its positive test already establishes.
**Fix (reduces the suite):** cut the parametrize to a single representative negative case, or
drop the class body to the positive + the long-paragraph pin. Saves ~2 test cases with no loss
of coverage. Consistent with the 2026-08-05 fragmentation rule.

### INFORMATIONAL (for Task 13)

**I-1 — the tech-spec's suite baseline is stale.** tech-spec says **1626 passed**; the tree at
`4ad4592` reports **1899 passed + 504 subtests**. The 1626 figure predates the 2026-08-04 work
*and* this feature's own ~2200 test lines. Task 13 must not read the delta as a regression.
Recommended: update the tech-spec baseline to 1899 before Task 13 runs.

**I-2 — `grep -c "strong\|<b>\|<em>"` criterion met for Phase 1.** t-hunted 0 → **9**. lamley
and autoevolution remain 0, which is correct — they are Phase 2 and their parsers were not
touched.

**I-3 — AC10 gates intact.** `tests/test_orangetrack_source.py` is byte-identical to the
pre-feature base `ccdc1a7`. `orangetrack_golden.json` moved by exactly the sanctioned Task 6
delta (10 figure/img node groups + 2 summary counters, nothing else). There is still no
regeneration mode in the repo.

---

## 10. Conclusion and hand-off

**Verdict: pass-with-findings.** The test layer of Phase 1 does go red when the feature breaks.
That is not asserted — it is measured: seven mutations, five of which produced large kill sets
targeting exactly the intended controls, including the one mutation that matters most (an
always-firing alignment guard, which would otherwise disable the whole feature on 100 % of
publications in total silence). Form 1 of the 2026-08-04 lesson is absent from this feature
entirely; Form 2 appears once, in a derived construction that was proven to still bite.

Two mutations survived, and both are named above with the exact fix.

**Required before Task 13 (a fixer does this — this audit changed nothing):**

1. **H-3** — write the one missing test: flag OFF must not gate orangetrack. Verify by
   re-running mutation M11; it must fail afterwards.
2. **H-1** — repair the user-spec step-3 row so it names a selector that collects tests.
3. **H-2** — narrow `tech-spec.md:580` to the file-scoped form Task 8 already proved.
4. **M-2** — widen the 40-character comparison in `test_dom_blocks.py:378` to the full string.
   Verify by re-running M8.
5. **M-3** — import the shared tuple in `tests/test_t_hunted_source.py:453` instead of
   retyping it.
6. **M-1 / I-1** — amend the tech-spec: the real-parser-mismatch bullet is unbuildable against
   the shipped design, and the 1626 baseline is stale.

Items 1–3 are gate repairs and should land first: without them Task 13's pre-deploy run
certifies two acceptance criteria with commands that prove nothing. L-1 through L-4 are
optional; L-4 removes test cases rather than adding them.

**Total new tests recommended: 1.** Everything else is an edit to an existing assertion or to a
spec document.

---

## 11. Working-tree state

`git status --short` at the end of this audit:

```
 M work/source-formatting-parity/decisions.md
 M work/source-formatting-parity/tasks/10.md
 M work/source-formatting-parity/tasks/11.md
 M work/source-formatting-parity/tasks/12.md
?? work/source-formatting-parity/logs/working/task-10/
?? work/source-formatting-parity/logs/working/task-11/
?? work/source-formatting-parity/logs/working/task-12/
```

**No source or test file is modified.** All seven mutations were reverted by the runner's
`finally:` block and each revert was confirmed by a per-file `git status --short` that returned
empty. The four `M` entries and the two `task-10` / `task-11` directories were already present
when this audit started — they are the Task 10 and Task 11 reports, not this task's work. The
only entry belonging to Task 12 is `logs/working/task-12/` containing this report.
