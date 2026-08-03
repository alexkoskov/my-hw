Verdict: PASS WITH ISSUES

# Wave 3 Code Audit — orangetrack-rendering-fixes

## Summary

Audited the three Wave-1 source files (`orangetrack_source.py`, `_llm_common.py`, `telegraph_publisher.py`) at commits e4ed9fb / 18ea5a6 / 43d078f against tech-spec Decisions 1-10 and the AC2 negative invariant. All ten Decisions trace to concrete code locations and behave as specified; Decision 7's uniform helper application across paragraph / heading / list_item is verified line-by-line; the security-critical `removeprefix("www.")` choice (vs `lstrip`) is correctly used in both directions; AC2 negative holds — parser never prepends `"• "` to `block.text`. Two minor findings around stale comments and a small spec-vs-implementation drift in the bullet-strip character class — neither blocks Wave 4. Verdict: PASS WITH ISSUES; minor findings are technical debt, not deploy blockers.

## Decision 1-10 traceability

1. **Decision 1 — list_item type + bullet at publisher only** — PASS. Parser emits `{"type": "list_item", "text": li_text, "runs": li_runs}` at `orangetrack_source.py:594-598`; bullet `"• "` prepended at `telegraph_publisher.py:313` only. Block dict has no bullet character at parser level.
2. **Decision 2 — h2/h3/h4 → heading level=3, h5 → paragraph** — PASS. `_emit_heading` dispatch at `orangetrack_source.py:436-449`: `level in (2, 3, 4)` branch emits heading; default branch (level==5) emits paragraph. babc67c carve-out preserved.
3. **Decision 3 — all headings level=3** — PASS. `orangetrack_source.py:439` hardcodes `"level": 3`. h1/h6 dropped at `_walk` (lines 543-545).
4. **Decision 4 — strict scheme + non-empty netloc + safe www-prefix** — PASS. `_strip_www` uses `removeprefix("www.")` (`telegraph_publisher.py:123`); `_is_same_site` checks scheme in `("http","https")` (line 149), non-empty netloc (line 151), urlparse exception logged + False (lines 140-148), then symmetric `_strip_www` comparison (line 153).
5. **Decision 5 — first-wrap-wins for overlapping spans** — PASS. `telegraph_publisher.py:209-217` sorts spans by start, accepts only if `start >= last_end`; overlapping spans skipped silently → second run's text remains in the plain-text segment via `text[cursor:start]` rebuild loop (lines 222-229). No `continue` on the rebuilt text path drops substrings.
6. **Decision 6 — substring approach (not sentinel)** — PASS. `text.find(run_text)` at `telegraph_publisher.py:204`; case-sensitive, literal, no regex.
7. **Decision 7 — helper applied to paragraph / heading / list_item** — PASS. See dedicated section below.
8. **Decision 8 — ul/ol uniform bullet** — PASS. The `name == "li"` branch in `_walk` (`orangetrack_source.py:578-599`) does not inspect the parent tag; both `<ul>` and `<ol>` reach the wrapper-recursion path at line 600-603 because `ul`/`ol` are intentionally NOT in `handled_tags` (comment at lines 480-483 documents this).
9. **Decision 9 — empty run.text guard before `str.find`** — PASS. `telegraph_publisher.py:199-200`: `if not run_text or not run_text.strip(): continue` — runs to plain-text-skip BEFORE the `text.find(run_text)` call on line 204. Zero-width-`<a>`-at-position-0 hazard closed.
10. **Decision 10 — bullet-doubling guard + DoS bound** — PASS (with minor drift, see Findings). DoS bound at `telegraph_publisher.py:181-187` (WARNING-logged, helper falls through to `[text]`); list_item leading-bullet strip at line 312.

## Decision 7 uniform helper application

Verdict: PASS — uniform across all three text-bearing block types.

- `paragraph` branch — `telegraph_publisher.py:298-301`: `nodes.append(p(*_render_paragraph_with_runs(block["text"], block.get("runs"), source_url)))`.
- `heading` branch — `telegraph_publisher.py:304-310`: `nodes.append(heading(block.get("level", 3), *_render_paragraph_with_runs(block["text"], block.get("runs"), source_url)))`.
- `list_item` branch — `telegraph_publisher.py:311-315`: text first lstripped, then `nodes.append(p("• ", *_render_paragraph_with_runs(text, block.get("runs"), source_url)))`.

Empty/None runs path: helper short-circuits at line 178-179 (`if not runs: return [text]`), so callers don't need a special-case branch — they pass `block.get("runs")` (may be None or `[]`) and helper returns `[text]`. Single render path, single regression surface — Decision 7 rationale upheld.

The `heading()` closure was widened from a single `text` arg to `*children` at line 271-273 — necessary so that the helper's return list (mix of strings and `<a>`-dicts) can be unpacked cleanly. Variadic widening is consistent with `p()`'s closure on line 259.

## Duplicated logic

None found.

- `_strip_www` is the single www-normalisation site; both sides of the comparison in `_is_same_site` route through it (line 153).
- `_is_same_site` is invoked once per run from inside `_render_paragraph_with_runs`; no second copy of same-site logic anywhere in the publisher.
- `_runs_from_tag` (parser side) is the single text-runs walker; the new `<li>` branch reuses it directly (line 588).
- Bullet stripping is a single line (line 312) inside the `list_item` branch; not duplicated elsewhere.
- Heading dispatch (h2/h3/h4 vs h5) is centralised in `_emit_heading` — caller only passes `level`, helper picks the type. No second dispatcher in `_walk`.

## Helper boundaries

Clean — no leakage between layers.

- `_is_same_site(href, source_netloc)` takes only (href, source_netloc); does not touch `block.text`, does not know about runs, does not log href contents at INFO/DEBUG (only truncated in WARNING on `urlparse` exception, line 144).
- `_render_paragraph_with_runs(text, runs, source_url)` takes only the rendering inputs; does NOT prepend the bullet (that is the list_item caller's job at line 313). Does NOT translate text. Does NOT mutate the input runs.
- `_strip_www` is a pure string transform; no logging.
- `_emit_heading(h_tag, level)` — caller decides the level; helper dispatches type. No tag-name introspection inside the helper.

`_build_content_from_blocks` correctly owns: bullet prepend (list_item only), block-type dispatch, hero-image skip, footer/auto-marker emission. Helpers don't reach into these concerns.

## Code style consistency

Consistent with the rest of the project.

- Naming: `_is_same_site`, `_strip_www`, `_render_paragraph_with_runs` follow the leading-underscore module-private convention used throughout `telegraph_publisher.py` (e.g. `_api_call`, `_save_token_to_env`, `_build_content`, `_footer_nodes`).
- Module-level constants: `_MAX_TEXT_FOR_RUNS`, `_MAX_RUNS_PER_BLOCK` (lines 156-157) follow the existing convention (`REQUEST_TIMEOUT`, `ENV_TOKEN_KEY`, `AUTO_MARKER_TEXT`); thresholds in numeric-literal form with underscores (`100_000`) for readability.
- Logging: `logger.warning` with `[orangetrack-render]` tag (lines 144-147, 182-186) gives the operator a clear grep filter; structured-arg style (lazy `%s` formatting) matches the rest of the file.
- Type hints: existing functions in `telegraph_publisher.py` already mix typed and untyped helpers (`p`, `figure_img`, `iframe` lambdas have no annotations); the new `_render_paragraph_with_runs(text, runs, source_url)` and `_is_same_site(href, source_netloc)` are also untyped, which is consistent with the local style. Not strict, but uniform with the file.
- Parser side (`orangetrack_source.py`): the new `<li>` branch (lines 578-599) follows the same `_emit_*` style as `_emit_paragraph` / `_emit_heading` / `_emit_image` — it could have been factored into `_emit_list_item`, but tech-spec explicitly says "inline in `_walk`, no separate helper function" (Solution section). Inline placement matches spec.
- `_PATCHED_TEXT_BLOCK_TYPES` extension in `_llm_common.py:106` is single-line; no docstring change needed because the tuple itself documents the contract.

## Comment quality

Mostly good with one stale reference.

Strong points:
- `_strip_www` docstring (lines 116-121) explicitly explains the `removeprefix` vs `lstrip` security distinction and references the audit finding.
- `_is_same_site` docstring (lines 127-137) lists every False-return reason and references Decision 4.
- `_render_paragraph_with_runs` docstring (lines 161-177) covers Behaviour with bullet points keyed to Decisions 5, 9, 10.
- `_build_content_from_blocks` docstring (lines 239-258) lists block shapes and explicitly mentions Decision 10 bullet-doubling guard.
- Parser `<li>` branch (lines 578-587) explicitly references Decisions 1, 8 and AC2 (bullet-not-here invariant).
- `_emit_heading` docstring/comment (lines 415-429) walks through Decisions 2 + 3 + babc67c rationale clearly.
- `paragraphs_flat` extension comment (lines 613-624) explains the alignment requirement with `_patch_text_with_ru_paragraphs` and references Task 2.

Weak points (see Findings):
- `orangetrack_source.py:540` references "Decision 15" from an older feature; misleading post-Wave-1 because h5 now flows into `paragraphs_flat` (line 627) — the "blocks only" semantic no longer holds.

## Findings

### Minor 1 — Stale comment "Decision 15: h5 → blocks only" misleading post-Wave-1

- **Location:** `orangetrack_source.py:540`.
- **Severity:** minor.
- **Issue:** Comment reads `# Decision 15: h5 → blocks only (not flat paragraphs).` This referenced an older decision in a previous feature where h5 was emitted only into `blocks` and excluded from `paragraphs_flat`. Wave-1 changes (line 625-628) explicitly include `paragraph` blocks in `paragraphs_flat`, and h5 is now emitted as `type: "paragraph"` (line 444-449) — so h5 IS in flat paragraphs. The comment contradicts the actual behavior introduced by this very feature.
- **Impact:** A future reader chasing why h5 text appears in `paragraphs_flat` will hit this comment and be misled. Does not affect runtime.
- **Recommendation:** Replace with `# Decision 2 (orangetrack-rendering-fixes): h5 → paragraph type, preserves babc67c.` Single-line edit; non-blocking.

### Minor 2 — Bullet-strip character set diverges slightly from tech-spec text

- **Location:** `telegraph_publisher.py:312`.
- **Severity:** minor (informational).
- **Issue:** Tech-spec Decision 10 specifies `block.text.lstrip(" ••\t")`. Implementation uses `lstrip(" •\t\n")`. Functional differences:
  - Tech-spec lists `••` (two `•` chars, but both U+2022 BULLET — set-strip semantics dedupe to one); implementation uses one `•` — equivalent.
  - Implementation adds `\n`, which is not in the tech-spec set.
  - Implementation does NOT include any second bullet variant (e.g. U+2023 `‣`, U+25E6 `◦`); tech-spec didn't either, so no regression.
- **Impact:** Stripping a leading newline is defensive overkill — LLM output passes through `.strip()` upstream in most paths, so a leading `\n` is unlikely to survive to this layer. No incorrectness; very slight over-strip if a translated list_item legitimately started with a literal newline (cosmetic-only).
- **Recommendation:** Either align to spec (`lstrip(" •\t")`) or amend tech-spec Decision 10 to mention the `\n` addition. Non-blocking.

## Cross-file consistency check

- `_PATCHED_TEXT_BLOCK_TYPES` extension in `_llm_common.py:106` matches the new `list_item` block type emitted by parser at `orangetrack_source.py:595` — name string `"list_item"` is identical.
- `paragraphs_flat` filter (`orangetrack_source.py:625-628`) uses the same triple `("paragraph", "heading", "list_item")`, matching what `_patch_text_with_ru_paragraphs` will consume; counts will align.
- `telegraph_publisher._build_content_from_blocks` consumes block shape `{"type": "list_item", "text": str, "runs": list}` — matches parser emission shape.
- `heading()` closure widening from `(level, text)` to `(level, *children)` at line 271-273 is internal — no external callers (it's a closure inside `_build_content_from_blocks`). No callsite breakage risk.
- `_render_paragraph_with_runs` returns `list` of `str | dict`; callers use `*children` unpack — compatible with `p(*children)` and `heading(level, *children)`.

## AC2 negative verification

Verified — parser does NOT prepend `"• "` to `block.text`. The `<li>` branch at `orangetrack_source.py:578-599` constructs the block dict with `"text": li_text` where `li_text = " ".join(r["text"] for r in li_runs).strip()` (line 591) — no bullet character is concatenated. The bullet is added EXCLUSIVELY at `telegraph_publisher.py:313` as a separate first child of the `<p>` node, which is immune to LLM stripping/translation because the LLM never sees it.

## Regression check — h5 carve-out (babc67c)

Verified — `_emit_heading` at `orangetrack_source.py:444-449` emits `{"type": "paragraph", "text": text, "runs": runs}` for level==5. `_walk` dispatches `<h5>` to `_emit_heading(child, 5)` at line 541, which lands in the paragraph branch. The existing `test_h5_emitted_as_paragraph_block` (per Task 1 verification log, 71 passed) confirms behavioural preservation.

## Security-critical: removeprefix vs lstrip

Verified — `_strip_www` at `telegraph_publisher.py:123` uses `n.removeprefix("www.")`. NOT `lstrip("www.")`. Lookalike domain bypass (`wwwfake-orangetrackdiecast.com`) cannot match: removeprefix performs exact-substring strip, whereas lstrip would have stripped any combination of `w`, `.` characters from the left edge. Applied symmetrically to BOTH sides of the comparison (line 153) — both `u.netloc` and `source_netloc` are normalised the same way. No second normalisation site exists, so there is no risk of one side using lstrip and the other removeprefix.

## Conclusion

PASS WITH ISSUES. All ten Decisions are implemented correctly and traceable to specific code locations. Decision 7's uniform helper invocation across paragraph / heading / list_item is the integral correctness check — it holds. Helper boundaries are clean. No duplicated logic. The two minor findings (stale comment, slight bullet-strip char-set drift) are documentation/polish — they do not affect runtime behavior and do not block Wave 4. Recommend proceeding to Pre-deploy QA; minor findings can be tracked as technical debt or fixed inline by the operator on the next pass.
