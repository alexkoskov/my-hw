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
