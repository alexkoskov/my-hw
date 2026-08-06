# Decisions Log: source-formatting-parity

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

---

## Task 4: Bound the request-path marker encoding

**Status:** Done
**Commit:** (wave 1)
**Agent:** main agent
**Summary:** `_encode_format_markers` now returns the text untouched, before any
`str.find`, when the block exceeds `_MAX_TEXT_FOR_RUNS` (100 000) or
`_MAX_RUNS_PER_BLOCK` (100) — same names and values as the render path, duplicated
rather than imported so neither layer depends on the other. Degradation is exactly
the contract from Decision 8: the paragraph still reaches the LLM in full, just
without `**` markers, and one WARNING is logged per block.
**Deviations:** One addition beyond the file list — a cross-reference comment was
added above `telegraph_publisher._MAX_TEXT_FOR_RUNS`. The implementation hint asked
for the "change one, change both" note "in both places", and only the `_llm_common`
side was in scope; the publisher side is two comment lines, no behaviour.

**Reviews:**

*Not run this session.* code-reviewer / security-auditor / test-reviewer are still
outstanding for this task — see the wave-level note at the end of this file.

**Verification:**
- `pytest tests/test_llm_common.py -q` → 33 passed (was 25)
- 5 behavioural tests confirmed RED before the fix; the 3 boundary positives
  (at-bound encoding, empty-runs silence) were green on both sides, as intended
- Smoke, security-review scenario verbatim (2.04 MB × 200 000 runs):
  **0.0005 s**, text returned unchanged, no markers. Same input measured at
  ≈160 s unbounded — three orders of magnitude of discriminating power.

---

## Task 3: `list_item` as a patchable block type in all four engines

**Status:** Done
**Commit:** (wave 1)
**Agent:** main agent
**Summary:** Added `list_item` to the local `_PATCHED_TEXT_BLOCK_TYPES` in all four
engines so they match `_llm_common`, which already had it — the divergence made
variant B+ re-translate already-Russian bullets RU→RU. The value is in the tests:
nine new ones, two per engine (behavioural + anti-drift) plus a negative control,
because `_translate_block_strings` is physically duplicated four times and a test
against one engine executes no line of the other three.
**Deviations:** None. `_llm_common.py` was left untouched by this task as required.

**Out-of-scope finding:** the docstring of `_llm_common._patch_text_with_ru_paragraphs`
still says "lead / paragraph / heading" although the module's own tuple already
contains `list_item`. Not fixed here — Task 4 owns `_llm_common.py` in the same wave
and the edit would have collided.

**Reviews:**

*Not run this session.* code-reviewer / test-reviewer still outstanding.

**Verification:**
- All 8 behavioural + anti-drift tests confirmed RED before the four-line fix; the
  negative control (`skip_patched_text=False`) was green on both sides, as intended
- `-k list_item` collects: claude **2**, gemini **2**, openai **2**, openrouter **3**
  — counted from the `N passed` line, not the exit code
- Tuple parity check across all four engines vs `_llm_common` → `True`
- Four engine test files together → 122 passed, 19 subtests

---

## Task 2: Feature flag module

**Status:** Done
**Commit:** (wave 1)
**Agent:** main agent
**Summary:** New `feature_flags.py` at the repo root with a single constant
`SOURCE_FORMATTING_ENABLED` (env name byte-identical) and
`source_formatting_enabled()`, which reads the module global at call time so tests
can `monkeypatch.setattr` without `importlib.reload`. Default ON per the operator's
2026-07-30 decision, documented in `.env.example` together with the sharp edge that
a typo while disabling leaves the feature ON. Registered in all three deploy
manifests; the flag is wired to nothing yet, by design.
**Deviations:** None. Default kept ON; no new arguments for opt-in surfaced.

**Documentation drift to pick up in the feature's doc pass:** `deployment.md`
§ "How strong is the invariant, really?" still describes the pre-2026-08-03 version
of the invariant test (two hardcoded names). Out of this task's file scope.

**Reviews:**

*Not run this session.* code-reviewer / security-auditor / test-reviewer outstanding.

**Verification:**
- `pytest tests/test_feature_flags.py tests/test_deploy_files_invariant.py -q`
  → 16 passed, 19 subtests
- `pytest -k flag_off` → **3 passed** (count read off `N passed`, not exit code)
- Smoke: default `True`; `=0` → `False`; `=disabled` → `True`; leaked heavy
  imports → `[]`

---

## Task 1: HTML corpus + orangetrack golden gate

**Status:** Done
**Commit:** (wave 1)
**Agent:** main agent
**Summary:** The 14 real pages moved out of `corpus-raw/` into
`tests/fixtures/articles/{t_hunted,lamley}/` via `git mv`, and 24 orangetrack
fixtures were carved out of the bodies already living inside
`tests/test_orangetrack_source.py` (copied, not moved — that file had to stay
byte-identical, and it did). `tests/fixtures/orangetrack_golden.json` was generated
on unmodified code by a one-off script kept in the scratchpad and deliberately not
committed: a one-command regeneration would be run reflexively and the gate would
disappear without a trace.
**Deviations:** Two, both from the corpus turning out to be 18 files rather than 14.
(1) Four of the 18 are Cloudflare challenge pages, not articles — they are what
autoevolution answers on article URLs. They went to
`tests/fixtures/articles/autoevolution-blocked/` with a README saying they are
evidence, not fixtures, rather than being deleted: they are the proof behind
"the corpus cannot be re-fetched". (2) Layout is `articles/<source>/` as the task
allowed, not flat `tests/fixtures/`.

**Reviews:**

*Not run this session.* test-reviewer outstanding.

**Verification:**
- All 18 moves are `git`-verified renames at **100 % similarity** — byte identity,
  no hook rewrite
- `pytest tests/test_orangetrack_golden.py -q` → 27 passed on **unmodified** code
  (nothing outside `tests/` changed for this task)
- `git diff --stat tests/test_orangetrack_source.py` → empty; that file's own
  suite → 100 passed
- Mutation checks, both reverted: one character changed in the baseline →
  `test_golden_matches[standard-paragraphs-and-image]` fails; one fixture removed →
  `test_golden_matches[list-ol]` **and** `test_fixture_set_matches_golden` fail
- AC9/AC10 gallery pinned at today's numbers: **20** image blocks / **10** flat
  images / **20** `figure` nodes. After Task 6 the last number must become 10 and
  nothing else may move
- `pre-commit run --all-files` passes without `--no-verify`; the fixtures are
  covered by the existing `trailing-whitespace` / `end-of-file-fixer` excludes

---

## Wave 1 — outstanding

Reviewer passes (code-reviewer, security-auditor, test-reviewer per each task's
Reviewers section) have **not** been run for any of the four tasks. The code, tests
and verification above are complete; the review round is what remains before the
wave can be called closed.

---

## Task 5: Extract `dom_blocks.py` and migrate orangetrack onto it

**Status:** Done
**Commit:** (wave 2)
**Agent:** main agent
**Summary:** The inline-markup walker, the runs flattener, the URL-safety
filters and the five emitters now live once in `dom_blocks.py`; `BlockBuilder`
owns the state that used to be captured by `_parse_content_encoded`'s frame,
and orangetrack became its first consumer — 509 lines deleted there against 49
added. The golden gate matched on the FIRST run: bodies were carried over
verbatim rather than rewritten, which was the whole point of the approach.
**Deviations:** Three, all recorded rather than smoothed over.

1. **`BlockBuilder` takes five site inputs, not four.** The AC counts four
   seams (junk class, `src` picker, dedup key, video-provider data). The fifth,
   `has_color_class`, is the `runs_from_tag` hook threaded through the builder —
   the builder has to produce runs, so it must carry it. No new decision was
   made; calling it "four" would just have been a word game. Documented in the
   module docstring so a reviewer does not count it as scope creep.
2. **Resource bounds are checked AFTER the walk, not before.** The walk itself
   is linear in the DOM; what needs bounding is the runs list handed
   downstream, where locating each run costs a scan of the text. Verified the
   golden is untouched by this: the largest orangetrack fixture has 3 runs and
   69 characters against bounds of 100 / 100 000.
3. **A test assertion was corrected, not the code.** The first draft of
   `test_empty_and_whitespace_only_runs_are_dropped` asserted whitespace-only
   runs disappear. They do not — they survive as a single space run and vanish
   only in the flattened text. Confirmed the pre-extraction code behaves
   identically before touching anything, then renamed the test to state the
   real contract. Asserting the tidier version would have been a behaviour
   change, and the golden gate would have caught it a step later.

**Out-of-scope observations (not fixed here):**
- `pre-commit run --all-files` rewrites ~33 unrelated files under
  `work/archived/`, `work/completed/` and `.claude/` on every invocation, so
  the repo-wide hook run cannot pass without an unrelated 33-file diff. Already
  noted in patterns.md; it bit twice in this session. Worth one cleanup commit.
- `orangetrack_source` still imports `Callable`/`Tuple` from `typing`; harmless,
  but a tidy-up candidate once Task 7 settles the file.

**Reviews:**

*Not run this session.* code-reviewer / security-auditor / test-reviewer
outstanding — same as Wave 1.

**Verification:**
- `tests/test_dom_blocks.py` → **47 passed**; all were red before the module
  existed (collection error), and the two that failed against the finished
  module were both wrong ASSERTIONS, corrected after checking the old code
- **Golden gate passed on the first run**, and passed HONESTLY:
  `git diff --stat tests/test_orangetrack_source.py tests/fixtures/orangetrack_golden.json`
  is empty — the baseline was not edited to make anything go green
- `tests/test_orangetrack_source.py` + `tests/test_orangetrack_golden.py` →
  127 passed, orangetrack test file untouched
- `orangetrack_source._video_embed_url` still imports and takes one argument;
  YouTube wraps, Vimeo returns `None` (Vimeo hosts stay another provider's data)
- Module isolation: importing `dom_blocks` pulls in no parser, no publisher, no
  `news_bot`, no `feature_flags` → `[]`
- `dom_blocks.py` registered in all three manifests; added to the
  guard-of-the-guard tuple, where the derived invariant now covers it through
  orangetrack's import closure
- Hooks pass on all 13 changed files
- Full suite: **1797 passed, 481 subtests** (was 1750 / 481), no regressions
