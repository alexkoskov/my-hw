---
created: 2026-05-03
status: approved
branch: dev
size: S
---

# Tech Spec: author-plug-filter

> **⚠️ Реализовано ИНАЧЕ — этот документ не описывает код (пометка 2026-08-03).**
> Variant B был реализован не отдельным модулем, а функциями `_strip_plugs` /
> `_strip_plugs_in_blocks` внутри `news_bot.py` (сейчас `news_bot.py:1285-1322`);
> тесты — в `tests/test_translation.py`. Названные ниже
> `author_plug_filter.py`, `tests/test_author_plug_filter.py` и записи в
> манифестах деплоя **не существуют и никогда не существовали**.
> Причина отклонения и его последствия — в `decisions.md` этой же папки.
> Актуальное описание уровней фильтрации служебного текста —
> `patterns.md § Service-text stripping — three granularities`.
> Ниже — исторический документ: что задумывалось до реализации.

## Solution

Two complementary regex-based filters that strip author social-media plugs from auto-published articles before they reach Telegraph + Telegram.

- **Variant A** extends the existing source-side allowlist `boilerplate_filter._BOILERPLATE_PATTERNS` with five new patterns covering `(follow|check|subscribe to) (me|us) on <platform>`, `<platform>: <handle>`, orphan `@handle`, parenthesised plug with `@handle`, and author `subscribe to my <feed>` shapes. Runs in every source parser before the article is staged. Length-bounded by `_MAX_BOILERPLATE_LEN` (bumped 80 → 120 to fit the new shapes).
- **Variant B** is a new module `author_plug_filter.py` providing `strip_author_plugs(text)`, `strip_in_paragraphs(paragraphs)`, and `strip_in_blocks(blocks)`. Three pattern families cover (B1) RU sentences with cue verb anchors, (B2) parenthesised umbrella with mandatory `@handle` regardless of verb, (B3) defensive orphan-handle paragraph. Called once from `news_bot._fallback_publish` at the post-translation convergence point (after either the LLM-success branch or the `_google_translate()` branch sets `ru_*` locals) and before Telegraph upload + DB persist. Wrapped in `try/except`: any internal failure logs ERROR and yields the original RU strings (publish-something > publish-nothing).
- **Soft-defence in the LLM prompt:** `ux-guidelines.md` is extended with one bullet point under "Единственные разрешённые дропы → category (a) Author social links" naming inline parenthesised plugs explicitly. Zero code surface, helps the LLM produce already-clean output and reduces variant-B work.

**Engine paths that produce `ru_*` locals (verified against `news_bot.py`):** (1) LLM-success branch — Claude/OpenRouter/OpenAI/Gemini, the most common path; (2) `is_fallback_active()` shortcut — when the outage state machine has flipped to global Google Translate; (3) `ClaudeOutageError` degraded-mode publish — Claude raised an API-level outage and the article is published via Google Translate before the OutageError re-raises. Per-article `ClaudeTranscreationError` (refusal / malformed JSON) does NOT fall through to Google — it re-raises into the 3-strikes counter, so variant B never sees that path. Variant B's single insertion at the convergence point covers (1)+(2)+(3) — every article that actually reaches Telegraph.

The manual-review path (`hw_review publish`) does NOT call `_fallback_publish` — by architecture variant B never runs there. Plugs on the manual path are removed by the operator at `hw_review stage` per `ux-guidelines.md`.

## Architecture

### What we're building/modifying

(File:line refs are *not* repeated here — they live in `code-research.md` (round 2 §9–§21). Anchors below are logical, not numeric.)

- **`boilerplate_filter.py`** — extend `_BOILERPLATE_PATTERNS` with 5 new patterns (A1–A5), remove legacy `^follow us on \w+` (shadowed by A1), bump `_MAX_BOILERPLATE_LEN` 80 → 120.
- **`author_plug_filter.py` (new module)** — pure-stdlib helpers with this contract:
  - `strip_author_plugs(text: str | None) -> tuple[str, list[str]]` — `None` and non-`str` pass through as-is with empty `removed_fragments` (no exception, no log). Cleaned `text` is the input minus matched plug-substrings.
  - `strip_in_paragraphs(paragraphs: list[str]) -> tuple[list[str], list[str]]` — applies `strip_author_plugs` per element, drops paragraphs that became whitespace-only (per Decision 10), aggregates `removed_fragments` across the list.
  - `strip_in_blocks(blocks: list[dict]) -> tuple[list[dict], list[str]]` — block-aware: cleans `text` for `_PATCHED_TEXT_BLOCK_TYPES` (lead / paragraph / heading); cleans `caption` for `image`; passes `video` and unknown block types through unchanged. Drops blocks whose type is in `_PATCHED_TEXT_BLOCK_TYPES` AND whose cleaned `text` is empty (per Decision 10). Per-block `isinstance(text, str)` and `isinstance(caption, str)` guards: a malformed block with non-string fields passes through unchanged (degrade gracefully — Decision 8 try/except is whole-call defence, the per-block guard avoids triggering it on isolated noise).
  - Module is stdlib-only (`re`, `typing`). No logging from the module — caller emits INFO logs.
  - Three pattern families: B1 cue-verb sentence (RU), B2 parenthesised umbrella with mandatory `@handle`, B3 orphan-handle paragraph. Final regex strings reproduced verbatim under Decision 4.
- **`news_bot._fallback_publish`** — single insertion at the post-translation convergence point (after both the LLM-success branch and the `_google_translate()` branch set `ru_*`, before Telegraph upload + `update_staged`). Calls all three variant-B helpers on `ru_title` / `ru_subtitle` / `ru_paragraphs` / `ru_blocks`, accumulates removed fragments, emits one INFO log line per fragment, wraps in `try/except` returning unchanged RU on internal failure. Single call site — no per-engine branching.
- **`.claude/skills/project-knowledge/references/ux-guidelines.md`** — append one bullet to the existing "Единственные разрешённые дропы" list (sub-item a — Author social links) explicitly naming inline parenthesised plugs and cue-verb-anchored variants.
- **`.claude/skills/project-knowledge/references/patterns.md`** — update one line: "Length-bounded at 80 chars" → "Length-bounded at 120 chars".
- **`tests/test_boilerplate_filter.py`** — fix the literal-`80` assertion in `TestLengthThreshold.test_long_paragraph_with_trigger_preserved` to import `_MAX_BOILERPLATE_LEN` from the module; add positive + negative test cases per AC1–AC5; add explicit negative for «Follow Mattel on Instagram, X, and Facebook» (AC16 corporate plug pass-through).
- **`tests/test_author_plug_filter.py` (new)** — covers variant B comprehensively: B1/B2/B3 pattern positives across all 10 platforms (AC5), AC13–AC15 negatives, empty-paragraph + empty-block disposal, lead / heading / image-caption / video-passthrough block-type cases, `try/except` defence. The 7-fixture negative battery from `code-research.md` §10 (including bare-brand `Instagram` line and soft-edge «Подписывайтесь на новости индустрии через RSS-агрегаторы») is reproduced verbatim.
- **`tests/test_fallback_publish_paths.py`** — add (a) mock-wiring smoke asserting variant B helpers invoked exactly once on each engine path, (b) ONE behavioural test that does NOT mock `strip_*` and verifies the canonical leak phrase «(подписывайтесь на меня в Instagram @diecast215 )» is absent from the resulting `ru_paragraphs`, (c) caplog assertion on `news_bot` logger that the AC9 INFO line shape (`[author_plug] stripped from {link!r}: {frag!r}`) appears once per stripped fragment.
- **`tests/test_no_token_leak_in_logs.py`** — add ONE test injecting a `AIza…` shape into `ru_paragraphs`, asserting the new INFO line is redacted by the existing `_TokenRedactingFilter` (defence-in-depth verification per security review).
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
   ├─[A] LLM-success branch ── transcreate_via_claude(...) ─────────┐
   │     (Claude / OpenRouter / OpenAI / Gemini)                    │
   │                                                                │
   ├─[B] is_fallback_active() shortcut ── _google_translate() ──────┤
   │                                                                │
   ├─[C] ClaudeOutageError degraded mode ── _google_translate() ────┤
   │     (article publishes, then OutageError re-raises)            │
   │                                                                │
   │  (ClaudeTranscreationError ─→ re-raise to 3-strikes; never reaches Telegraph)
   │                                                                │
   ▼                                                                ▼
   ru_title / ru_subtitle / ru_paragraphs / ru_blocks  (converged locals from A or B or C)
   │
   ├─→ [VARIANT B]  author_plug_filter.strip_*  ──→ inline plugs REMOVED + INFO logged
   │       (try/except: on failure return originals + ERROR log; INFO emitted by caller)
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

**Rationale:** Both the LLM-success branch and the `_google_translate()` branch unpack into the same locals (`ru_title`, `ru_subtitle`, `ru_paragraphs`, `ru_blocks`). Inserting variant B once after the convergence (anchor in code-research §12) covers all engines without duplication. **Supports user-spec AC11.**

**Alternatives considered:** Per-engine call (4× duplication, drift risk). Rejected.

### Decision 3: Variant B module — separate file vs extend `boilerplate_filter.py`

**Decision:** Create a new module `author_plug_filter.py` rather than extending `boilerplate_filter.py`.

**Rationale:** Different semantics — boilerplate filter drops whole paragraphs (predicate), variant B strips substrings within paragraphs (transform). Different return types — `bool` vs `tuple[str, list[str]]`. Different timing — pre-LLM vs post-LLM. Mixing them in one module obscures intent. **Supports user-spec Технические решения § «отдельный модуль».**

**Alternatives considered:** Extend `boilerplate_filter.py`. Rejected — semantic clash.

### Decision 4: Cue-verb anchored RU regex + parenthetical umbrella with mandatory `@handle`

**Decision:** Variant B uses three patterns. Each cue verb MUST be paired with EITHER a target audience phrase (`на (меня|нас) / за (нами|мной) / (мой|наш|моего|нашего) <something>`) OR a recognised platform name within the same sentence. Bare cue verbs without that anchor (e.g. «Подписывайтесь на новости индустрии через RSS-агрегаторы») do NOT match — preserves generic content. Final regex strings:

```python
# B1 — cue-verb sentence with target-anchor or platform-anchor
_PLATFORM_OR_TARGET_RU = (
    r'(?:'
        r'на\s+(?:меня|нас)|'
        r'за\s+(?:нами|мной)|'
        r'(?:мой|наш|моего|нашего)\s+\w+|'
        r'(?:Instagram|Twitter|X|TikTok|YouTube|Facebook|Reddit|Patreon|Discord|Linktree)'
    r')'
)
_CUE_RU = (
    r'(?:'
        r'подпиш[иу]тесь|'
        r'подпис[ыа]вайтесь|'
        r'подписаться|'
        r'следите\s+за\s+(?:нами|мной)|'
        r'найди(?:те)?\s+меня'
    r')'
)
_RU_SENTENCE_WITH_CUE = re.compile(
    r'(?:(?<=[\.\!\?])\s*|^)'                          # start: after sentence-end OR string start
    r'[^\.\!\?]*?' + _CUE_RU + r'[^\.\!\?]*?'          # cue verb appears in the sentence
    r'(?:[\.\!\?]+|$)\s*',                              # terminal punct OR end of string
    re.I,
)
# Post-match guard: drop the match only if it ALSO contains _PLATFORM_OR_TARGET_RU.
# Implementation: re.finditer(_RU_SENTENCE_WITH_CUE, text), then
# `if re.search(_PLATFORM_OR_TARGET_RU, m.group(0), re.I)` — gate.

# B2 — parenthesised umbrella with mandatory @handle
_PARENTHETICAL_PLUG = re.compile(
    r'\s*\(\s*[^()]*?'
    r'(?:Instagram|Twitter|X|TikTok|YouTube|Facebook|Reddit|Patreon|Discord|Linktree)'
    r'\s*@\w{2,30}'
    r'[^()]*?\)\s*',
    re.I,
)

# B3 — orphan handle paragraph (full-match predicate)
_ORPHAN_HANDLE = re.compile(r'^\s*@\w{2,30}\s*$')
```

Replacement logic: B2 substitutes the match with a single space (preserves word boundaries), then B1 (gated by `_PLATFORM_OR_TARGET_RU` post-match check) substitutes its match with empty string, then B3 (full-match) replaces the whole paragraph with empty if it matches. Final pass: `re.sub(r'\s+', ' ', cleaned).strip()` collapses stray whitespace.

**Rationale:** B1 catches faithful translations of «подписывайтесь на меня в Instagram». B2 catches Google-Translate variants where the verb is not a clean cue (e.g. «(посмотрите меня в Instagram @x)»). Mandatory `@handle` in B2 anchors on the third-party-promo signal, not platform mention. The two-step gate on B1 (`_CUE_RU` matched + `_PLATFORM_OR_TARGET_RU` matched within the sentence) prevents over-strip on bare cue verbs in unrelated contexts. **Supports user-spec AC11, AC12, AC14, AC15.**

**Alternatives considered:**
- NLP sentence tokenizer (e.g. `nltk.sent_tokenize`). Rejected — adds dependency, less predictable, overkill for this surface.
- Bare-platform parenthetical (no `@handle` requirement). Rejected — would over-strip journalistic references (user-spec Risk 2).
- Single-step B1 without `_PLATFORM_OR_TARGET_RU` gate. Rejected — would over-strip generic «подписывайтесь на наш RSS» / «подпишитесь на рассылку».

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

**Decision:** For every fragment removed by variant B, emit a single line `logger.info(f"[author_plug] stripped from {link!r}: {frag!r}")` to journalctl. Both `link` and `frag` are passed through `repr()` (`!r` formatter) so any control characters or terminal escape sequences are escaped — no log-injection surface. The line routes through the `news_bot` logger and is automatically scrubbed by the existing `_TokenRedactingFilter` (defence-in-depth: even if a stripped fragment contained a leaked secret, the filter would redact it before it lands in journalctl).

**Rationale:** Per-strip granularity gives operator a paste-able fragment when investigating false positives. Tag `[author_plug]` matches the existing `[fallback]` / `[recovery]` pattern in `news_bot.py`. `repr()` of both `link` and `frag` neutralises log-injection (security review medium #1). **Supports user-spec AC9 + verification step «journalctl grep».**

**Alternatives considered:** Aggregate count in plan-of-day admin ping. Rejected by user-spec; per-strip INFO is enough.

### Decision 10: Empty-paragraph / empty-block disposal

**Decision:**
- After variant B strips a paragraph, if the result is whitespace-only — drop it (`[p for p in cleaned if p.strip()]`).
- After variant B strips a block of type `paragraph`/`lead`/`heading`, if the result is empty `text` — drop the entire block from `ru_blocks`.
- Image/video blocks where the `caption` becomes empty — keep the block (the image still renders).

**Rationale:** Empty `<p>` in Telegraph renders as a visible blank line — operator-undesired noise. No downstream invariant requires `len(ru_paragraphs) == len(en_paragraphs)` — `_llm_common._parse_response` issues a soft warning on count mismatch but does not raise. Dropping the block keeps DOM clean and image/video order intact. **Supports user-spec Risk 3.**

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

**Feature size:** S — pyramid is mostly unit (~38 cases), 3 integration smokes, 0 E2E.

### Unit tests

**`tests/test_boilerplate_filter.py` (extend existing, ~15 new cases):**
- One positive + one negative per new pattern A1–A5 (10 cases).
- AC5 platform coverage: ten positives, one per platform — instagram, twitter, x, tiktok, youtube, facebook, reddit, patreon, discord, linktree (ensures pattern alternation covers every named platform). May reuse the same plug shape, just rotating the platform.
- Fix `TestLengthThreshold.test_long_paragraph_with_trigger_preserved`: replace literal-`80` assertion with `from boilerplate_filter import _MAX_BOILERPLATE_LEN; assert len(long_text) > _MAX_BOILERPLATE_LEN`.
- Confirm legacy `^follow us on \w+` removal does not regress existing positives — each pre-existing «follow us on …» fixture should still match A1.
- AC16 negative: `"Follow Mattel on Instagram, X, and Facebook"` — corporate plug must pass through variant A unchanged. Lives in this file to assert *variant A* doesn't catch it (separate negative in `test_author_plug_filter.py` covers variant B).

**`tests/test_author_plug_filter.py` (new, ~120 LoC):**
- **B1 positives:** standalone canonical leak phrase; cue verb in middle / start / end of paragraph; multiple plugs in one paragraph (AC8); two adjacent plug sentences without terminal punct between them (AC8 corner case from completeness GAP-3).
- **B1 with all anchor variants:** «подписывайтесь **на меня**», «следите **за нами**», «**мой Instagram**», «**найди меня**» — each must match.
- **B2 positives:** parenthesised umbrella with each of the ten platforms + `@handle`; Google-Translate variants («(посмотрите меня в Twitter @x)», «(подпишитесь… в YouTube @y)»).
- **B3 positives:** orphan-handle paragraph («@diecast215» on its own line).
- **Cross-cutting positives:** title plug, subtitle plug, plug in `text` of paragraph block, plug in `text` of lead block, plug in `text` of heading block, plug in `caption` of image block.
- **Block-type pass-through:** video block with plug-shaped `caption` — caption survives unchanged (video captions don't render on Telegraph per code-research §13). Unknown block type passes through.
- **Empty-block disposal (AC10):** paragraph block whose `text` becomes empty after strip — block dropped from list. Image block whose `caption` becomes empty — block KEPT (image still renders).
- **Empty-paragraph disposal:** flat paragraph with only a plug → after strip, dropped from `ru_paragraphs` list per Decision 10.
- **None / non-string safety:** `strip_author_plugs(None)` returns `(None, [])` — no exception. `strip_author_plugs(42)` returns `(42, [])`. `strip_in_blocks` traversing a malformed block with `text=None` passes the block through unchanged.
- **Negatives (full 7-fixture battery from code-research §10):** «The collector posted his find to Instagram and gathered 50K likes»; «Коллекционер написал в Instagram, что нашёл редкий Chase»; «Хорошие фото можно найти в источнике (см. фото в Instagram).»; «Анонс прошёл вчера (трансляция шла на YouTube).»; «Follow Mattel on Instagram, X, and Facebook for more news.» (AC16 corporate); bare-brand `Instagram` on its own line (no @handle, not orphan); soft-edge «Подписывайтесь на новости индустрии через RSS-агрегаторы — это удобно.» (cue verb but no platform/target anchor — must NOT match per Decision 4 gate).
- **`try/except` defence (logged by caller):** This test lives in `test_fallback_publish_paths.py` (variant B is called from there, and the caller emits the ERROR log). Inside `test_author_plug_filter.py` — only assert that the helpers themselves do not raise on malformed input.

### Integration tests

Three additions to `tests/test_fallback_publish_paths.py`:

1. **Mock-wiring smoke (covers AC9 / AC10 / AC11):** patch `news_bot.author_plug_filter.strip_author_plugs` / `strip_in_paragraphs` / `strip_in_blocks` with mocks. Run `_fallback_publish` once on the LLM-success branch and once on the `is_fallback_active`=True branch; assert each helper invoked once with the expected `ru_*` argument. Same assertion on the `ClaudeOutageError` degraded-mode branch (third path that produces `ru_*`).
2. **Behavioural anchor (covers Solution + AC11 end-to-end):** real call (no mock of `strip_*`) — feed a `pending_articles` row whose LLM-mocked output `ru_paragraphs` contains the canonical leak phrase «(подписывайтесь на меня в Instagram @diecast215 )»; run `_fallback_publish`; assert the final argument passed to `telegraph_publisher.publish_article` no longer contains the plug. Litmus-test against silent no-op: a stub `strip_*` returning input unchanged would FAIL this test.
3. **Caplog assertion (covers AC9 from caller side):** `caplog.set_level(logging.INFO, logger='news_bot')`, run the same behavioural scenario as #2; assert exactly one record with shape `[author_plug] stripped from ...: ...` per stripped fragment.

Also one addition to `tests/test_no_token_leak_in_logs.py` per security review medium #2:
- Inject a synthetic Google-API-key shape (`AIzaSy...`) into a `ru_paragraphs` element; run a behavioural strip + log; assert the captured INFO line contains `***` (redacted), not the raw key. Defence-in-depth verification of `_TokenRedactingFilter` coverage.

### E2E tests

None — feature is pure-functional regex layer; full pipeline already covered by existing integration tests in `tests/test_fallback_publish_paths.py`. Live-environment verification handled by post-deploy QA task.

### ReDoS guard

A pytest-timeout sentinel test under `tests/test_author_plug_filter.py` runs each variant-B regex against a 50K-char adversarial input (lots of nested parens, repeated cue verbs, dense `@handle`-shaped tokens) and asserts the call returns within 1 s. Defence against future regex changes that could introduce catastrophic backtracking — the existing patterns are linear-time per security-review info #1, but adding a runtime guard prevents regression.

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
| Variant B over-strips a real-content sentence | Patterns anchor on cue verbs paired with target/platform OR mandatory `@handle` — never on bare platform mention or bare cue verb; INFO log per strip exposes false positives quickly; rollback `git revert HEAD && git push` ~3 min. |
| Bumping `_MAX_BOILERPLATE_LEN` 80→120 breaks existing test pinned to literal 80 | Replace literal with `from boilerplate_filter import _MAX_BOILERPLATE_LEN` (constant import); fixture is 113 chars — survives bump unchanged. |
| New module forgotten in `deploy.sh` / `deploy.yml` FILES — server hits ImportError on next cron tick | Explicit task in Wave 3 (Task 5) requires updating BOTH FILES arrays; pre-deploy QA reads both and verifies. |
| Empty paragraph after strip leaves visible blank line in Telegraph | Decision 10 — drop empty paragraphs / empty `paragraph`-blocks from `ru_paragraphs` / `ru_blocks` lists. |
| Variant B regex bug crashes publish loop | `try/except Exception` wrapper at the call site (Decision 8); ERROR log + original RU returned; channel keeps publishing. |
| Architecture change makes `hw_review publish` route through `_fallback_publish` in future | Tech-spec documents the via_review boundary in Decision 2; reviewer of any such future architecture change must re-evaluate this feature's behaviour on the manual path. |
| Log-injection through `link` in INFO line | Both `link` and `frag` are passed through `repr()` (`!r`) per Decision 9; control characters / terminal escapes neutralised. |
| Future regex change introduces catastrophic backtracking (CPU-bind, NOT raise — `try/except` would not catch) | ReDoS sentinel test in `tests/test_author_plug_filter.py` runs each pattern against 50K-char adversarial input with `pytest-timeout` → guards future changes. |
| Corporate plug «Follow Mattel on Instagram, X, and Facebook» reaches the channel | Intentional per AC11 / AC16 — out of scope for this feature; will be addressed by a separate press-release-boilerplate feature. |
| Per-block guard on `text`/`caption` non-string values | `strip_in_blocks` does `isinstance(text, str)` per block before applying regex; malformed block degrades only itself (no whole-call try/except trip). |

## User-Spec Deviations

None.

## Acceptance Criteria

Technical criteria complementing user-spec AC1–AC17:

- [ ] All 682 existing tests still pass.
- [ ] New tests in `tests/test_boilerplate_filter.py` and `tests/test_author_plug_filter.py` are green; new smoke test in `tests/test_fallback_publish_paths.py` is green.
- [ ] Test count grows by ~38 (~15 variant-A: 10 platform positives + 5 negatives/AC16 + assertion fix; ~17 variant-B unit cases including B1 anchor variants, block-type cases, empty-disposal, None-safety, full 7-fixture negative battery; 3 integration tests in `test_fallback_publish_paths.py` (mock-wiring + behavioural + caplog); 1 redaction test in `test_no_token_leak_in_logs.py`; 1 ReDoS sentinel test).
- [ ] No new pip dependencies in `requirements.txt`.
- [ ] `deploy.sh` and `.github/workflows/deploy.yml` FILES arrays both contain `author_plug_filter.py`.
- [ ] `git diff main...HEAD` shows ~250 lines added across 10 files; ~3 removed.
- [ ] `news_bot.py` imports `author_plug_filter` at module-import block (top of file).
- [ ] `boilerplate_filter._MAX_BOILERPLATE_LEN == 120` after the change.
- [ ] `boilerplate_filter._BOILERPLATE_PATTERNS` no longer contains the legacy `^follow us on \w+` pattern.
- [ ] `ux-guidelines.md` — "разрешённые дропы → (a)" list includes a new bullet naming parenthesised plugs and cue-verb-anchored shapes.
- [ ] No regex in either module uses catastrophic-backtracking shapes (`(.+)+`, `(a|a)*`).

## Implementation Tasks

### Wave 1 (parallel — independent pre-LLM defences)

#### Task 1: Variant A — extend boilerplate filter

- **Description:** Implement variant A per Decisions 1, 7 — add patterns A1–A5 to `_BOILERPLATE_PATTERNS`, remove legacy `^follow us on \w+`, bump `_MAX_BOILERPLATE_LEN` 80→120, update `tests/test_boilerplate_filter.py` accordingly (constant-import for the length-threshold test plus the AC1–AC5 positives and AC16 corporate-plug negative).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_boilerplate_filter.py tests/test_lamley_source.py tests/test_mattel_news_source.py tests/test_autoevolution_source.py -v` → all green
- **Files to modify:** `boilerplate_filter.py`, `tests/test_boilerplate_filter.py`
- **Files to read:** `lamley_source.py`, `autoevolution_source.py`, `mattel_news_source.py`, `work/author-plug-filter/code-research.md`

#### Task 2: ux-guidelines + patterns.md prose updates

- **Description:** Append one bullet to `.claude/skills/project-knowledge/references/ux-guidelines.md` under existing "разрешённые дропы → (a) Author social links" naming inline parenthesised plugs and cue-verb-anchored shapes (Decision 6). Update the "Length-bounded at 80 chars" line in `.claude/skills/project-knowledge/references/patterns.md` so the prose matches the bumped 120-char constant from Task 1.
- **Skill:** prompt-master
- **Reviewers:** prompt-reviewer
- **Files to modify:** `.claude/skills/project-knowledge/references/ux-guidelines.md`, `.claude/skills/project-knowledge/references/patterns.md`
- **Files to read:** `.claude/skills/project-knowledge/references/ux-guidelines.md`, `work/author-plug-filter/code-research.md`

### Wave 2 (depends on Wave 1)

#### Task 3: Create author_plug_filter.py module

- **Description:** Implement variant B per Decisions 3, 4, 10, 11 — new pure-stdlib module exposing `strip_author_plugs` / `strip_in_paragraphs` / `strip_in_blocks`. Cover the full Testing Strategy section's variant-B unit list including the 7-fixture negative battery and ReDoS sentinel.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_author_plug_filter.py -v` → all green; `python3 -c "import author_plug_filter; print(author_plug_filter.strip_author_plugs('(подписывайтесь на меня в Instagram @x )')[0])"` → empty-or-whitespace
- **Files to modify:** `author_plug_filter.py` (new), `tests/test_author_plug_filter.py` (new)
- **Files to read:** `_llm_common.py`, `boilerplate_filter.py`, `work/author-plug-filter/code-research.md`

### Wave 3 (parallel — both depend on Wave 2)

#### Task 4: Integrate variant B into _fallback_publish

- **Description:** Wire variant B into the auto-publish path per Decisions 2, 8, 9 — single call site at the post-translation convergence point in `_fallback_publish` (covers all three engine paths: LLM-success, `is_fallback_active`, `ClaudeOutageError` degraded mode). Add the three integration tests from Testing Strategy (mock-wiring + behavioural + caplog) and the redaction-filter test in `tests/test_no_token_leak_in_logs.py`.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -m pytest tests/test_fallback_publish_paths.py tests/test_no_token_leak_in_logs.py tests/test_integration.py -v` → all green
- **Files to modify:** `news_bot.py`, `tests/test_fallback_publish_paths.py`, `tests/test_no_token_leak_in_logs.py`
- **Files to read:** `author_plug_filter.py`, `news_bot.py`, `work/author-plug-filter/code-research.md`

#### Task 5: Add author_plug_filter.py to deploy FILES

- **Description:** Add `author_plug_filter.py` to the `FILES=(...)` array in BOTH `deploy.sh` and `.github/workflows/deploy.yml` (the `patterns.md` deploy invariant — both arrays must mirror).
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

- **Description:** Live-environment checks AFTER `news_bot.service` restart on prod, following the procedure in Agent Verification Plan → Verification approach. Operator-driven (uses `bash` / `curl` / `ssh`).
- **Skill:** post-deploy-qa
- **Reviewers:** none
