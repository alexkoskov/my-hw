---
created: 2026-05-03
status: draft
branch: dev
size: S
---

# Tech Spec: author-plug-filter

## Solution

Two complementary regex-based filters that strip author social-media plugs from auto-published articles before they reach Telegraph + Telegram.

- **Variant A** extends the existing source-side allowlist `boilerplate_filter._BOILERPLATE_PATTERNS` with five new patterns covering `(follow|check|subscribe to) (me|us) on <platform>`, `<platform>: <handle>`, orphan `@handle`, parenthesised plug with `@handle`, and author `subscribe to my <feed>` shapes. Runs in every source parser before the article is staged. Length-bounded by `_MAX_BOILERPLATE_LEN` (bumped 80 → 120 to fit the new shapes).
- **Variant B** is a new module `author_plug_filter.py` providing `strip_author_plugs(text)`, `strip_in_paragraphs(paragraphs)`, and `strip_in_blocks(blocks)`. Three pattern families cover (B1) RU sentences with cue verb anchors, (B2) parenthesised umbrella with mandatory `@handle` regardless of verb, (B3) defensive orphan-handle paragraph. Called once from `news_bot._fallback_publish` after RU translation results converge from any engine (Claude / OpenRouter / OpenAI / Gemini / Google Translate fallback) and before Telegraph upload + DB persist. Wrapped in `try/except`: any internal failure logs ERROR and yields the original RU strings (publish-something > publish-nothing).
- **Soft-defence in the LLM prompt:** `ux-guidelines.md` is extended with one bullet point under "Единственные разрешённые дропы → category (a) Author social links" naming inline parenthesised plugs explicitly. Zero code surface, helps the LLM produce already-clean output and reduces variant-B work.

The manual-review path (`hw_review publish`) does NOT call `_fallback_publish` — by architecture variant B never runs there. Plugs on the manual path are removed by the operator at `hw_review stage` per `ux-guidelines.md`.

## Architecture

### What we're building/modifying

- **`boilerplate_filter.py`** — extend `_BOILERPLATE_PATTERNS` with 5 new patterns (A1–A5), remove legacy `^follow us on \w+` (shadowed by A1), bump `_MAX_BOILERPLATE_LEN` 80 → 120.
- **`author_plug_filter.py` (new module)** — pure-stdlib helpers: `strip_author_plugs(text) -> tuple[str, list[str]]`, `strip_in_paragraphs(paragraphs) -> tuple[list[str], list[str]]`, `strip_in_blocks(blocks) -> tuple[list[dict], list[str]]`. Each returns the cleaned content plus the list of removed fragments (drives the AC9 INFO log). Three pattern families: B1 cue-verb sentence (RU), B2 parenthesised umbrella with mandatory `@handle`, B3 orphan-handle paragraph.
- **`news_bot._fallback_publish`** — single insertion at line ~1006 (between `_google_translate` unpack at line 1004–1005 and Telegraph upload at line 1027). Calls all three variant-B helpers on `ru_title` / `ru_subtitle` / `ru_paragraphs` / `ru_blocks`, accumulates removed fragments, emits one INFO log line per fragment, wraps in `try/except` returning unchanged RU on internal failure. Single call site — no per-engine branching.
- **`.claude/skills/project-knowledge/references/ux-guidelines.md`** — append one bullet to the existing "Единственные разрешённые дропы" list (line ~64–69, sub-item a) explicitly naming inline parenthesised plugs and cue-verb-anchored variants.
- **`.claude/skills/project-knowledge/references/patterns.md:28`** — update one line: "Length-bounded at 80 chars" → "Length-bounded at 120 chars".
- **`tests/test_boilerplate_filter.py`** — fix the literal-`80` assertion at line 129 to import `_MAX_BOILERPLATE_LEN` from the module; add positive + negative test cases per AC1–AC5.
- **`tests/test_author_plug_filter.py` (new)** — covers variant B comprehensively: B1/B2/B3 pattern positives, AC9–AC15 negatives, empty-paragraph handling, INFO-logging, `try/except` defence.
- **`tests/test_fallback_publish_paths.py`** — add ONE smoke test asserting variant B is invoked exactly once on the canonical path (per AC10–AC11).
- **`deploy.sh`** + **`.github/workflows/deploy.yml`** — both `FILES=(...)` arrays gain `author_plug_filter.py` (deploy invariant per `patterns.md`).

### How it works

```
Source parser (autoevolution / lamley / mattel)
   │
   ├─→ collect EN paragraphs / blocks
   │
   └─→ filter_boilerplate / filter_blocks ───[VARIANT A]──→ short standalone plugs DROPPED
   │
   ▼
news_bot.job() → pending_articles row
   │
   ▼
news_bot._fallback_publish(row, via_review=False)
   │
   ├─→ Claude / OpenRouter / OpenAI / Gemini ── transcreate_via_claude(...) ──┐
   │                                                                          │
   ├─→ Google Translate fallback (per-article OR global) ── _google_translate()┤
   │                                                                          │
   ▼                                                                          ▼
   ru_title / ru_subtitle / ru_paragraphs / ru_blocks  (converged locals)
   │
   ├─→ [VARIANT B]  author_plug_filter.strip_*  ──→ inline plugs REMOVED + INFO logged
   │       (try/except: on failure return originals + ERROR log)
   ▼
   telegraph_publisher.publish_article(...)
   │
   ▼
   pending_repo.update_staged(...) → move_to_published(...) → channel
```

Manual path (`hw_review publish`) bypasses `_fallback_publish` entirely; variant B never runs there.

### Shared resources

| Resource | Owner (creates) | Consumers | Instance count |
|----------|----------------|-----------|----------------|
| Compiled regex set (`_BOILERPLATE_PATTERNS` + variant A additions) | `boilerplate_filter.py` (module-level) | 3 source parsers via `is_boilerplate` / `filter_boilerplate` / `filter_blocks` | 1 (compiled once at import) |
| Compiled regex set (B1 cue, B2 parenthetical, B3 orphan) | `author_plug_filter.py` (module-level) | `news_bot._fallback_publish` | 1 (compiled once at import) |

No DB pools, ML models, or browser instances introduced.

## Decisions

### Decision 1: Two-tier filter (A on source side, B on LLM-output side)

**Decision:** Two regex passes — variant A drops standalone-paragraph plugs before LLM, variant B surgically strips inline plugs from RU after LLM.

**Rationale:** The 2026-05-02 14:40 leak phrase «(подписывайтесь на меня в Instagram @diecast215 )» was inline within a longer paragraph; variant A alone can't catch it (length-bounded). Variant B alone can catch it but at higher operational cost (runs on every article, more chances of false positives). A handles cheap-and-easy cases pre-LLM (saves LLM tokens too); B handles the residual inline cases. **Supports user-spec AC1–AC17 collectively.**

**Alternatives considered:**
- *Prompt-only fix.* Asks the LLM to drop plugs in its system prompt. Rejected as primary because Google-Translate fallback path doesn't see system prompts, and LLM behaviour is non-deterministic. Kept as soft-defence (Decision 6).
- *Variant A only.* Cheaper but doesn't catch inline plugs (the actual trigger). Rejected.
- *Status quo.* Spec already declares plug-drop policy; reality leaks. Rejected.

### Decision 2: Single integration site in `_fallback_publish`

**Decision:** Variant B is called exactly once from `news_bot._fallback_publish` at the convergence point of the four LLM paths and Google Translate fallback.

**Rationale:** Both the LLM-success branch and the `_google_translate()` branch unpack into the same locals (`ru_title`, `ru_subtitle`, `ru_paragraphs`, `ru_blocks`). Inserting variant B once after the convergence (line 1006 per code-research §12) covers all engines without duplication. **Supports user-spec AC11.**

**Alternatives considered:** Per-engine call (4× duplication, drift risk). Rejected.

### Decision 3: Variant B module — separate file vs extend `boilerplate_filter.py`

**Decision:** Create a new module `author_plug_filter.py` rather than extending `boilerplate_filter.py`.

**Rationale:** Different semantics — boilerplate filter drops whole paragraphs (predicate), variant B strips substrings within paragraphs (transform). Different return types — `bool` vs `tuple[str, list[str]]`. Different timing — pre-LLM vs post-LLM. Mixing them in one module obscures intent. **Supports user-spec Технические решения § «отдельный модуль».**

**Alternatives considered:** Extend `boilerplate_filter.py`. Rejected — semantic clash.

### Decision 4: Cue-verb anchored RU regex + parenthetical umbrella with mandatory `@handle`

**Decision:** Variant B uses three patterns:
- **B1 (cue-verb sentence):** anchors on RU verbs `подпиш[иу]тесь / подпис[ыа]вайтесь / подписаться / следите за (нами|мной) / (мой|наш|моего|нашего) (Instagram|Twitter|...)` plus surrounding sentence boundary.
- **B2 (parenthetical umbrella):** any parenthesised text containing one of the listed platforms followed by `@handle` of 2–30 chars. The `@handle` is **mandatory** to avoid catching journalistic references like `(см. фото в Instagram)`.
- **B3 (orphan handle paragraph):** RU paragraph that is exactly `@handle` (defensive — should be caught by variant A but kept as belt-and-suspenders).

**Rationale:** B1 catches faithful translations of «подписывайтесь на меня в Instagram». B2 catches Google-Translate variants where the verb is not a clean cue (e.g. «(посмотрите меня в Instagram @x)»). Mandatory `@handle` in B2 anchors on the third-party-promo signal, not platform mention. **Supports user-spec AC11, AC12, AC15.**

**Alternatives considered:**
- NLP sentence tokenizer (e.g. `nltk.sent_tokenize`). Rejected — adds dependency, less predictable, overkill for this surface.
- Bare-platform parenthetical (no `@handle` requirement). Rejected — would over-strip journalistic references (user-spec Risk 2).

### Decision 5: Variant B applied to all RU fields

**Decision:** Variant B is applied to `ru_title`, `ru_subtitle`, every entry in `ru_paragraphs`, and `text` / `caption` fields of every block in `ru_blocks` (per the block-type table from code-research §13: `paragraph` / `lead` / `heading` use `text`; `image` uses `caption`; `video` and unknown types pass through).

**Rationale:** Plugs occasionally appear in titles or subtitles (less common but observed in lamley sources). Block traversal is mandatory because `_patch_text_with_ru_paragraphs` (in `_llm_common.py`) splices `ru_paragraphs` content into block `text` fields when LLM returns `blocks=null` — by the time variant B runs, both `ru_paragraphs` and `ru_blocks[*].text` may contain the same plug. Cleaning both independently is redundant-but-safe (idempotent regex). **Supports user-spec AC10.**

**Alternatives considered:** Skip blocks (relies on `ru_paragraphs` cleaning). Rejected — when LLM returns model-translated blocks (no splice), block content can diverge from `ru_paragraphs` (different phrasing), so block cleaning is necessary, not redundant.

### Decision 6: Soft-defence in `ux-guidelines.md` prompt

**Decision:** Append one bullet to `ux-guidelines.md` under "разрешённые дропы → category (a)" naming inline parenthesised plugs and cue-verb-anchored variants explicitly.

**Rationale:** Costs nothing at runtime (just prompt tokens already loaded). LLM drops plug at translation step → variant B never sees it → reduces INFO-log noise and false-positive risk in the regex. Independent of code-side variants — cannot be the only defence (Google Translate fallback path doesn't load this prompt). **Supports user-spec Альтернативы decision (combined approach).**

**Alternatives considered:** Skip the prompt edit (rely on code-only). Rejected as user-spec explicitly chose combined (1)+(2).

### Decision 7: `_MAX_BOILERPLATE_LEN` 80 → 120

**Decision:** Bump the length bound on `boilerplate_filter.is_boilerplate`.

**Rationale:** New variant-A patterns (especially A2 parenthesised with `@handle`) yield candidate strings up to ~110 chars (e.g. `(follow me on Instagram for the latest reveals @diecast215)`). 80-char bound would shadow them. 120 is a round number with headroom. **Supports user-spec AC1.**

**Alternatives considered:** Per-pattern length bound. Rejected — adds complexity with no real-world benefit; the global bound is a coarse safety net, not a precision tool.

### Decision 8: `try/except` defensive wrap around variant B

**Decision:** Variant B call in `_fallback_publish` is wrapped in `try/except Exception`. On any internal error, log ERROR with `sanitize_error_message(exc)` and use the original (uncleaned) RU strings.

**Rationale:** Project's general posture is publish-something > publish-nothing — channel filling continuously is more important than perfect filtering. A regex bug must not crash the publish loop. **Supports user-spec Risk 1 mitigation.**

**Alternatives considered:** Let exceptions propagate (3-strikes flow). Rejected — single regex bug would disable the entire publish path until someone deploys a fix.

### Decision 9: INFO log per stripped fragment

**Decision:** For every fragment removed by variant B, emit a single line `logger.info(f"[author_plug] stripped from {link}: {frag!r}")` to journalctl.

**Rationale:** Per-strip granularity gives operator a paste-able fragment when investigating false positives. Tag `[author_plug]` matches the existing `[fallback]` / `[recovery]` pattern in `news_bot.py`. `repr()` of fragment preserves whitespace and quotes. **Supports user-spec AC9 + verification step «journalctl grep».**

**Alternatives considered:** Aggregate count in plan-of-day admin ping. Rejected by user-spec; per-strip INFO is enough.

### Decision 10: Empty-paragraph / empty-block disposal

**Decision:**
- After variant B strips a paragraph, if the result is whitespace-only — drop it (`[p for p in cleaned if p.strip()]`).
- After variant B strips a block of type `paragraph`/`lead`/`heading`, if the result is empty `text` — drop the entire block from `ru_blocks`.
- Image/video blocks where the `caption` becomes empty — keep the block (the image still renders).

**Rationale:** Empty `<p>` in Telegraph renders as a visible blank line — operator-undesired noise. No downstream invariant requires `len(ru_paragraphs) == len(en_paragraphs)` (LLM count validator at `_llm_common.py:226–230` is soft warning only). Dropping the block keeps DOM clean and image/video order intact. **Supports user-spec Risk 3.**

**Alternatives considered:** Keep empty `<p>`. Rejected — visible cosmetic harm.

### Decision 11 [TECHNICAL]

**Decision:** Use `_PATCHED_TEXT_BLOCK_TYPES` constant from `_llm_common.py` instead of re-declaring the tuple inside `author_plug_filter.py`.

**Rationale:** Single source of truth for the block-type → text-field mapping. Existing import path used by `_llm_common._patch_text_with_ru_paragraphs` is stable.

**Alternatives considered:** Re-declare locally. Rejected — drift risk if upstream changes. Pure-stdlib constraint not violated (constant is a tuple of strings, no extra dep).

## Data Models

No new tables, columns, JSON shapes, or DB migrations.

## Dependencies

### New packages

None.

### Using existing (from project)

- `re` (stdlib) — pattern compile + apply (both variants).
- `logging` (stdlib) — INFO/ERROR logging in variant B (matches `news_bot.py` style).
- `boilerplate_filter` — variant A extends `_BOILERPLATE_PATTERNS` and `_MAX_BOILERPLATE_LEN`; consumers (3 source parsers) unchanged.
- `_llm_common._PATCHED_TEXT_BLOCK_TYPES` — re-used in `author_plug_filter.strip_in_blocks` (Decision 11).
- `news_bot.sanitize_error_message` — re-used in the variant-B `try/except` wrap (Decision 8).

## Testing Strategy

**Feature size:** S

### Unit tests

**`tests/test_boilerplate_filter.py` (extend existing):**
- One positive + one negative per new pattern A1–A5 (10 cases).
- Fix line 129 assertion: replace `assert len(long_text) > 80` with `assert len(long_text) > _MAX_BOILERPLATE_LEN` after `from boilerplate_filter import _MAX_BOILERPLATE_LEN`.
- Confirm legacy `^follow us on \w+` removal does not regress the existing positives that depended on it (every existing positive should still match A1).

**`tests/test_author_plug_filter.py` (new, ~60–80 LoC):**
- B1 positives: standalone «(подписывайтесь на меня в Instagram @diecast215)»; cue-verb sentence in middle / start / end of paragraph; multiple plugs in one paragraph (AC8).
- B2 positives: parenthesised umbrella with various platforms + `@handle`; Google-Translate variants («(посмотрите меня в Twitter @x)», «(подпишитесь… в YouTube @y)»).
- B3 positives: orphan-handle paragraph.
- Cross-cutting positives: title plug, subtitle plug, plug in `text` of paragraph block, plug in `caption` of image block.
- Negatives (AC13/AC14/AC15): real-content sentences mentioning Instagram, RU body content without cue verb, journalistic parenthetical without `@handle` («(см. фото в Instagram)», «(трансляция шла на YouTube)»), corporate plug «Follow Mattel on Instagram, X, and Facebook».
- Empty-paragraph handling: paragraph with only a plug becomes empty → dropped.
- INFO logging: capture `caplog` for `news_bot` logger, assert one record per fragment, format matches `[author_plug] stripped from ... :`.
- `try/except` defence: monkeypatch one of the regex modules to raise, assert variant B returns original strings + ERROR log.

### Integration tests

Minimal — one smoke test added to `tests/test_fallback_publish_paths.py`:
- Patch `news_bot.author_plug_filter.strip_*` with mocks; run `_fallback_publish` once on Claude path; assert `strip_author_plugs` called for `ru_title` / `ru_subtitle` and `strip_in_paragraphs` / `strip_in_blocks` each called once. Same assertion on Google-fallback path.
- No new file. No E2E.

### E2E tests

None — feature is pure-functional regex layer; full pipeline already covered by existing integration tests in `tests/test_fallback_publish_paths.py`. Live-environment verification handled by post-deploy QA task.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

**During implementation** (per task `Verify-smoke`): pytest run after each wave to confirm no regression and new tests green.

**Post-deploy**: spot-check the next auto-publication from lamley/autoevolution by fetching the Telegraph URL via `curl` and visually checking the rendered article body for absence of plug-shaped phrases. Cross-reference with `journalctl -u news_bot.service` for `[author_plug] stripped` INFO lines that confirm the filter triggered when relevant.

### Tools required

- `pytest` (already in CI / dev environment).
- `bash` + `curl` (post-deploy: fetch Telegraph URL).
- `ssh` (post-deploy: `journalctl` inspection by operator).
- No MCP tools required — Telegraph pages are public HTTPS, no Telegram MCP needed for verification.

## Risks

| Risk | Mitigation |
|------|-----------|
| Variant B over-strips a real-content sentence | Patterns anchor on cue verbs OR mandatory `@handle` — never on bare platform mention; INFO log per strip exposes false positives quickly; rollback `git revert HEAD && git push` ~3 min. |
| Bumping `_MAX_BOILERPLATE_LEN` 80→120 breaks existing test pinned to literal 80 | Replace literal with `from boilerplate_filter import _MAX_BOILERPLATE_LEN` (constant import); fixture is 113 chars — survives bump unchanged. |
| New module forgotten in `deploy.sh` / `deploy.yml` FILES — server hits ImportError on next cron tick | Explicit task in Wave 3 (Task 5) requires updating BOTH FILES arrays; pre-deploy QA reads both and verifies. |
| Empty paragraph after strip leaves visible blank line in Telegraph | Decision 10 — drop empty paragraphs / empty `paragraph`-blocks from `ru_paragraphs` / `ru_blocks` lists. |
| Variant B regex bug crashes publish loop | `try/except Exception` wrapper at the call site (Decision 8); ERROR log + original RU returned; channel keeps publishing. |
| Architecture change makes `hw_review publish` route through `_fallback_publish` in future | Tech-spec documents the via_review boundary in Decision 2; reviewer of any such future architecture change must re-evaluate this feature's behaviour on the manual path. |

## User-Spec Deviations

None.

## Acceptance Criteria

Technical criteria complementing user-spec AC1–AC17:

- [ ] All 682 existing tests still pass.
- [ ] New tests in `tests/test_boilerplate_filter.py` and `tests/test_author_plug_filter.py` are green; new smoke test in `tests/test_fallback_publish_paths.py` is green.
- [ ] Test count grows by ~25 (10 variant-A cases + ~12 variant-B cases + 1 wiring smoke + a few empty-handling / log / defence tests).
- [ ] No new pip dependencies in `requirements.txt`.
- [ ] `deploy.sh` and `.github/workflows/deploy.yml` FILES arrays both contain `author_plug_filter.py`.
- [ ] `git diff main...HEAD` shows ~250 lines added across 10 files; ~3 removed.
- [ ] `news_bot.py` import block has `import author_plug_filter` near line 55–60.
- [ ] `boilerplate_filter._MAX_BOILERPLATE_LEN == 120` after the change.
- [ ] `boilerplate_filter._BOILERPLATE_PATTERNS` no longer contains the legacy `^follow us on \w+` pattern.
- [ ] `ux-guidelines.md` — "разрешённые дропы → (a)" list includes a new bullet naming parenthesised plugs and cue-verb-anchored shapes.
- [ ] No regex in either module uses catastrophic-backtracking shapes (`(.+)+`, `(a|a)*`).

## Implementation Tasks

### Wave 1 (parallel — independent pre-LLM defences)

#### Task 1: Variant A — extend boilerplate filter

- **Description:** Add five regex patterns (A1–A5 per code-research §9) to `boilerplate_filter._BOILERPLATE_PATTERNS`, remove the legacy `^follow us on \w+` line (shadowed by A1), bump `_MAX_BOILERPLATE_LEN` 80→120. Update `tests/test_boilerplate_filter.py` to import the constant for the line-129 assertion and add positive + negative test cases per AC1–AC5. Result: variant A drops standalone plug paragraphs at all three source parsers without touching their contracts.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_boilerplate_filter.py tests/test_lamley_source.py tests/test_mattel_news_source.py tests/test_autoevolution_source.py -v` → all green
- **Files to modify:** `boilerplate_filter.py`, `tests/test_boilerplate_filter.py`
- **Files to read:** `lamley_source.py`, `autoevolution_source.py`, `mattel_news_source.py`, `work/author-plug-filter/code-research.md`

#### Task 2: ux-guidelines + patterns.md prose updates

- **Description:** Append one bullet to `.claude/skills/project-knowledge/references/ux-guidelines.md` under existing "разрешённые дропы → (a) Author social links" naming inline parenthesised plugs and cue-verb-anchored shapes (Decision 6). Update `.claude/skills/project-knowledge/references/patterns.md:28` "Length-bounded at 80 chars" → "Length-bounded at 120 chars" so the prose matches the bumped constant from Task 1.
- **Skill:** prompt-master
- **Reviewers:** prompt-reviewer
- **Files to modify:** `.claude/skills/project-knowledge/references/ux-guidelines.md`, `.claude/skills/project-knowledge/references/patterns.md`
- **Files to read:** `.claude/skills/project-knowledge/references/ux-guidelines.md`, `work/author-plug-filter/code-research.md`

### Wave 2 (depends on Wave 1)

#### Task 3: Create author_plug_filter.py module

- **Description:** Create `author_plug_filter.py` with three public helpers — `strip_author_plugs(text)`, `strip_in_paragraphs(paragraphs)`, `strip_in_blocks(blocks)` — each returning `(cleaned, removed_fragments)`. Compile B1 cue-verb-RU, B2 parenthetical-umbrella-with-@handle, B3 orphan-handle patterns at module level (Decision 4). Re-use `_PATCHED_TEXT_BLOCK_TYPES` from `_llm_common` for block traversal (Decision 11). Drop empty paragraphs / empty `paragraph`-blocks (Decision 10). Create `tests/test_author_plug_filter.py` covering all positives + negatives per AC1–AC15 plus empty-handling and `try/except` defence. Module is pure stdlib; no API/IO; no log calls inside (caller logs).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_author_plug_filter.py -v` → all green; `python3 -c "import author_plug_filter; print(author_plug_filter.strip_author_plugs('(подписывайтесь на меня в Instagram @x )')[0])"` → empty-or-whitespace
- **Files to modify:** `author_plug_filter.py` (new), `tests/test_author_plug_filter.py` (new)
- **Files to read:** `_llm_common.py`, `boilerplate_filter.py`, `work/author-plug-filter/code-research.md`

### Wave 3 (parallel — both depend on Wave 2)

#### Task 4: Integrate variant B into _fallback_publish

- **Description:** Add `import author_plug_filter` to `news_bot.py` import block. Insert ~12-line variant-B call block at line ~1006 (between `_google_translate` unpack at line 1004–1005 and Telegraph upload at line 1027) per code-research §12. Wrap in `try/except Exception` (Decision 8) — on failure, log ERROR with `sanitize_error_message` and yield original `ru_*` strings. Emit one `logger.info(f"[author_plug] stripped from {link}: {frag!r}")` per removed fragment (Decision 9). Add ONE smoke test in `tests/test_fallback_publish_paths.py` asserting variant B helpers called exactly once on canonical path (Claude success branch + Google fallback branch). Do not modify `cmd_publish` in `hw_review.py` — manual path bypasses `_fallback_publish` by architecture.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_fallback_publish_paths.py tests/test_integration.py -v` → all green
- **Files to modify:** `news_bot.py`, `tests/test_fallback_publish_paths.py`
- **Files to read:** `author_plug_filter.py`, `news_bot.py`, `work/author-plug-filter/code-research.md`

#### Task 5: Add author_plug_filter.py to deploy FILES

- **Description:** Add `author_plug_filter.py` to the `FILES=(...)` array in both `deploy.sh:52` and `.github/workflows/deploy.yml:132` (per the `patterns.md` deploy invariant — both arrays must mirror byte-for-byte). Without this entry, the file never reaches the production server and `news_bot.py` hits `ImportError` on the next cron tick. No code change beyond the two array updates.
- **Skill:** deploy-pipeline
- **Reviewers:** code-reviewer, security-auditor, deploy-reviewer
- **Verify-smoke:** `grep -n author_plug_filter deploy.sh .github/workflows/deploy.yml` → both files show the new line
- **Files to modify:** `deploy.sh`, `.github/workflows/deploy.yml`
- **Files to read:** `deploy.sh`, `.github/workflows/deploy.yml`, `.claude/skills/project-knowledge/references/patterns.md`

### Audit Wave (parallel)

#### Task 6: Code Audit

- **Description:** Holistic code-quality review of all files created/modified by Wave 1–3. Check for: regex catastrophic-backtracking risks; cross-component duplication; idiomatic Python style; error-handling consistency with project conventions; reuse of `_PATCHED_TEXT_BLOCK_TYPES` per Decision 11. Write report to `work/author-plug-filter/logs/audit/code-audit.md`.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 7: Security Audit

- **Description:** OWASP-Top-10 review of all feature code. Focus areas: regex DoS / catastrophic backtracking on adversarial inputs; log-injection through stripped fragments (`{frag!r}` in journalctl); secret-leak risk in INFO logs (verify `[author_plug] stripped from {link}: ...` does not unintentionally include secrets via the `link` value); input-validation on block traversal (malformed block dicts shouldn't crash the helper). Write report to `work/author-plug-filter/logs/audit/security-audit.md`.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 8: Test Audit

- **Description:** Test-quality review of `tests/test_boilerplate_filter.py` (new + modified cases), `tests/test_author_plug_filter.py` (new), `tests/test_fallback_publish_paths.py` (new smoke). Check: AC1–AC17 each has at least one mapping test; negatives are meaningful (not just `assert True`); `caplog` usage is correct; mock boundaries are right (don't mock `re` itself, do mock `author_plug_filter` module in integration smoke); test pyramid balance (mostly unit + 1 integration smoke is appropriate for size S). Write report to `work/author-plug-filter/logs/audit/test-audit.md`.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 9: Pre-deploy QA

- **Description:** Acceptance testing pre-deploy: run `python3 -m pytest tests/ -v` (full suite, expect ~707 tests green: 682 existing + ~25 new). Verify each user-spec AC1–AC17 has mapping in either tests or manual checklist. Verify each technical AC in this tech-spec is met. Confirm `deploy.sh`/`deploy.yml` FILES arrays contain `author_plug_filter.py`. Confirm `ux-guidelines.md` change shipped as part of the deploy bundle. Confirm `git diff main...HEAD` line-count is in expected ballpark. Block deploy if any AC unverified.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 10: Deploy

- **Description:** Standard CI/CD flow: confirm `git push origin main` triggered `ci.yml` green, then `deploy.yml` ran on `workflow_run`, SCPed updated FILES list (now including `author_plug_filter.py`) to VPS, ran `pip install --user -r requirements.txt` (no diff expected — no new deps), restarted `news_bot.service`. Verify the systemd restart succeeded via the workflow log; verify the new module landed at `$DEPLOY_PATH/author_plug_filter.py`.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 11: Post-deploy verification

- **Description:** Live-environment checks AFTER `news_bot.service` restart on prod:
  - Wait for the next auto-publication from lamley/autoevolution (typical interval 90 min within 10:00–20:00 МСК window).
  - Fetch the published Telegraph URL via `curl` and read body — confirm absence of plug-shaped phrases (parenthesised `@handle`, «подписывайтесь на меня в …»).
  - SSH to server: `journalctl -u news_bot.service -S today | grep author_plug` — confirm INFO lines appear when the filter triggered (or are absent if no plug present in the day's articles).
  - Verify no ERROR-level log from `[author_plug]` (would indicate the `try/except` defence fired — needs investigation).
  - Spot-check 2–3 articles from the day for false positives — real Instagram mentions in content (e.g. «фото из Instagram», «постит в Instagram» without cue verb / `@handle`) must survive.
  Tools: `bash`, `curl`, `ssh` (operator-driven; no MCP needed since Telegraph pages are public HTTPS).
- **Skill:** post-deploy-qa
- **Reviewers:** none
