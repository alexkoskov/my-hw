# Code research — author-plug-filter

Two-tier filter for author social-media plugs in source articles (variant A: source-side EN paragraph filter, variant B: post-translation RU sentence stripper). Trigger: 2026-05-02 14:40 publication leaked `(подписывайтесь на меня в Instagram @diecast215 )` to `@myhwchannel123`.

---

## 1. `boilerplate_filter.py` — current shape

File: `/workspaces/debian-2/my-hw/boilerplate_filter.py` (136 lines).

### Module constants

- `boilerplate_filter.py:34` — `_MAX_BOILERPLATE_LEN = 80`. Length-bound applied in `is_boilerplate`: a stripped paragraph longer than this is treated as real prose even if a trigger phrase appears at the start.
- `boilerplate_filter.py:38–65` — `_BOILERPLATE_PATTERNS = [...]` — list of `re.compile(..., re.I)` objects. Conventions:
  - All patterns case-insensitive (`re.I`).
  - Most are `^…`-anchored (start-of-paragraph). The "tags / categories" patterns also anchor `^`.
  - Matched against the **whole stripped paragraph** via `pat.search(s)` after `s = text.strip()`.
  - English block (autoevolution / lamley / mattel are all EN sources) is primary; RU block is defence in depth.
  - Existing handles: `share on/via/to {fb,twitter,x,linkedin,pinterest,whatsapp,telegram,email,reddit}`, `tweet`, `pin it/pin on pinterest`, `email this(article)?`, `copy link/url/article url`, `subscribe(to(our)?newsletter)?`, `follow us on \w+`, `(related articles?|see also|you may also like)`, `read more[:\s]*$`, `(tags?|filed under|categories?):`, `comments?$`.

### Functions

- `boilerplate_filter.py:68` — `def is_boilerplate(text: str) -> bool`. Single-paragraph predicate. Returns `False` for non-`str`, empty/whitespace, and any `len(s) > _MAX_BOILERPLATE_LEN`. Otherwise iterates `_BOILERPLATE_PATTERNS` and returns on first match.
- `boilerplate_filter.py:87` — `def filter_boilerplate(paragraphs: Iterable[str]) -> List[str]`. List comprehension over `is_boilerplate`; preserves order. Accepts iterables (generators OK — see test `test_handles_iterables`).
- `boilerplate_filter.py:92` — `def filter_blocks(blocks: Iterable[dict]) -> List[dict]`. Mirror for autoevolution structured blocks (lead/paragraph/heading/image/video). Decision rules:
  - Non-dict / empty → drop.
  - `has_media = bool(block.get("src") or block.get("image_url"))`. Media block kept regardless of caption unless caption is boilerplate AND `text` is empty.
  - Pure-text block → drop if `is_boilerplate(text)`.

### Public API import surface

```
from boilerplate_filter import filter_boilerplate
from boilerplate_filter import filter_blocks, filter_boilerplate, is_boilerplate
```

Both names are part of the parser-side ABI — already imported across three source modules.

---

## 2. Source-parser call sites (variant A entry points)

All three runs `filter_*(...)` **before** returning the article dict; output flows into `pending_articles_repo.add_pending` (paragraphs/blocks columns). Filter therefore runs on raw EN paragraphs at the parser exit.

| File | Import | Call site | Runs on |
|---|---|---|---|
| `/workspaces/debian-2/my-hw/autoevolution_source.py:25` | `from boilerplate_filter import filter_blocks, filter_boilerplate, is_boilerplate` | `autoevolution_source.py:293` (`blocks = filter_blocks(blocks)`) and `:356` (`paragraphs = filter_boilerplate(paragraphs)` in `enrich_entry`) | structured blocks list before flat-list rebuild; RSS-only fallback in `enrich_entry` |
| `/workspaces/debian-2/my-hw/lamley_source.py:26` | `from boilerplate_filter import filter_boilerplate` | `lamley_source.py:349` (inside `fetch_lamley_article`, after `<p>/<li>/<h*>` walk, BEFORE picking subtitle) | `paragraphs: List[str]` |
| `/workspaces/debian-2/my-hw/mattel_news_source.py:36` | `from boilerplate_filter import filter_boilerplate` | `mattel_news_source.py:488` (`paragraphs = filter_boilerplate(_paragraphs_from_body(body_html))` — wraps directly) | post-`_paragraphs_from_body` |
| `/workspaces/debian-2/my-hw/openrouter_transcreation.py:420` | (comment only — relies on autoevolution-side `filter_blocks` already running) | n/a | n/a |

**Confirmation:** in all three parsers, the filter runs on the raw EN paragraph list inside the parser, BEFORE the article dict is returned. Variant A pattern additions to `_BOILERPLATE_PATTERNS` (and, if needed, `_MAX_BOILERPLATE_LEN`) flow through every parser exit automatically — no per-source changes required.

**Lamley note:** `lamley_source.py:349` — filter runs BEFORE the subtitle pick (`subtitle = paragraphs[0] ...`). If a leading author-plug ever became P0 (rare but possible), variant A would prevent it from polluting the subtitle.

---

## 3. `_fallback_publish` — variant B integration point

File: `/workspaces/debian-2/my-hw/news_bot.py`.

### Imports + binding

- `news_bot.py:55–60` — `import llm_transcreation as claude_transcreation`, `from llm_transcreation import (transcreate_via_claude, ClaudeTranscreationError, ClaudeOutageError)`. Tests patch `news_bot.transcreate_via_claude` directly (per the comment at `:53–54`).

### Function under analysis

- `news_bot.py:838` — `def _fallback_publish(row, via_review=False):`. Both auto-publish (via `job()`) and operator-driven `hw_review publish` flow through it.

### Variables holding RU output (the variant B target)

After the engine call resolves, four locals hold the canonical RU payload regardless of which path was taken (Claude success, per-article Google fallback, or degraded-mode Google):

| Variable | Type | Source path |
|---|---|---|
| `ru_title` | `str` | Claude branch: `news_bot.py:935`. Google: `news_bot.py:878` (within `_google_translate`, returned at `:910` and unpacked at `:927` / `:1004`). |
| `ru_subtitle` | `str` | Claude branch: `:936`. Google: `:879` / `:910`. |
| `ru_paragraphs` | `list[str]` | Claude branch: `:937` (`list(claude_result.get('paragraphs') or [])`). Google: `:880` / `:910`. |
| `ru_blocks` | `list[dict] \| None` | Claude branch: `:938` (`claude_result.get('blocks')`). Google: `:881–898` / `:910`. May be `None` for non-block sources (lamley, mattel). |

### Where variant B should be invoked

The single canonical post-translation cleanup point is **after** the engine pivot finishes (Claude success OR per-article Google OR degraded Google) and **before** the Telegraph upload + DB persist:

- After `news_bot.py:1005` (last unpack of `ru_*` from `_google_translate`).
- Before `news_bot.py:1007` (Step 2 comment: "Telegraph — reuse saved URL").
- Specifically, before either `telegraph_publisher.publish_article(...)` at `:1027` (renders `ru_paragraphs` / `ru_blocks` to Telegraph) or `pending_repo.update_staged(...)` at `:1055` (persists the four RU fields).

This is "single call site, no per-engine branching" as the spec requires — it captures Claude, per-article Google, and degraded-mode Google output in one cleanup step.

### Google-fallback path coverage

Variant B should ALSO run on Google-translated RU. The state-machine `outage_state.is_fallback_active()` branch at `news_bot.py:923–928` calls `_google_translate()` and unpacks into the same `ru_*` locals — placing variant B after `:1005` covers it for free. No duplicated invocation needed.

### `update_staged` signature (downstream sink)

- `pending_articles_repo.py:243` — `def update_staged(link: str, ru_title: str, ru_subtitle: str, ru_paragraphs: list, ru_blocks: Optional[list]) -> bool`. Filters applied to the locals BEFORE `:1055` automatically affect what's persisted to the DB (so manual `hw_review publish` retries replay the cleaned text).

---

## 4. Block-level path — what variant B must traverse

### What renders to Telegraph

- `telegraph_publisher.py:298` — `publish_article(title, paragraphs=..., blocks=..., subtitle=..., images=..., source_url=..., auto_marker=...)`.
- `telegraph_publisher.py:288` — branching: when `blocks` is non-empty → `_build_content_from_blocks(...)`; else → `_build_content(...)` (paragraphs + images).
- `telegraph_publisher.py:114–186` — `_build_content_from_blocks` consumes `block["text"]` for `paragraph`/`lead`/`heading` and `block.get("caption", "")` inside `figure_img` for image blocks. Paragraphs (the flat list) are NOT consulted on the block path — only `ru_blocks` content reaches the page.

**Implication:** if `ru_blocks is not None` (autoevolution articles), variant B regex MUST be applied to each block's `text` field AND each image-block `caption` — otherwise an inline plug inside `text` survives to Telegraph despite `ru_paragraphs` being clean. For lamley/mattel (no `blocks`, falls through to `_build_content`) the flat `ru_paragraphs` is enough.

### Where the block list comes from in `_fallback_publish`

- Claude branch — `news_bot.py:938` — `ru_blocks = claude_result.get('blocks')`. May be `None` when LLM returned `null` for blocks (the `_patch_text_with_ru_paragraphs` fallback at `_llm_common.py:321` then splices `ru_paragraphs` into the EN block scaffold inside `claude_transcreation.py:435`).
- `_llm_common.py:106` — `_PATCHED_TEXT_BLOCK_TYPES = ("lead", "paragraph", "heading")`. These are the block types that carry text needing variant B treatment. Image/video blocks carry `src` (skip) and `caption` (apply variant B).

### Variant B traversal contract

- Apply regex to `ru_title`, `ru_subtitle`, every entry of `ru_paragraphs`.
- If `ru_blocks` is a non-empty list, iterate each block:
  - Block types in `("lead", "paragraph", "heading")` → clean `text` field.
  - Block types in `("image", "video")` → clean `caption` field if present.
  - Other block types / non-dict entries → pass through.
- Behaviour is "remove the matched sentence only, leave the rest verbatim" — the regex must match a sentence-shape (period/parenthetical bound), not the whole paragraph. If a paragraph is *only* a plug, the result should be empty string; downstream `update_staged` accepts empty entries (no NOT-NULL constraint on `ru_paragraphs` items, only the column itself).

---

## 5. Existing tests — fixture shape for new tests

File: `/workspaces/debian-2/my-hw/tests/test_boilerplate_filter.py` (390 lines, ~14 tests in 7 classes).

### Test conventions

- pytest classes with no inheritance (no `unittest.TestCase`), method names `test_*`.
- Heavy use of `@pytest.mark.parametrize("text", [...])` for positive/negative pattern lists (`tests/test_boilerplate_filter.py:23`, `:62`, `:92`).
- Imports at the top of the module: `from boilerplate_filter import filter_blocks, filter_boilerplate, is_boilerplate` (`:14`).
- Per-parser integration tests synthesise HTML, mock a `requests`-style session via the local `_make_response` helper at `:173`, and call `fetch_lamley_article` / `fetch_mattel_article` / `_scrape_article_page` directly.
- Mattel integration uses `tests/fixtures/mattel_flight_builder.py` → `_make_flight_article(entry, body_html=...)` to assemble realistic Flight CMS payloads (`tests/test_boilerplate_filter.py:222`).

### Relevant test classes (slot for new variant A tests)

| Class | Line | Slot for new tests |
|---|---|---|
| `TestIsBoilerplatePositive` | `:22` | EN author-plug positives (`Follow me on Instagram`, `My Twitter is @x`, `^Instagram: @diecast215`, `^@handle$`, etc.) |
| `TestIsBoilerplateNegative` | `:91` | Long inline-mention negatives (anti-false-positive) |
| `TestLengthThreshold` | `:121` | If `_MAX_BOILERPLATE_LEN` bumps to 120 — adjust `assert len(...) > 80` to whatever new bound; see Risk #1 below |
| `TestFilterBoilerplate` | `:138` | Multi-paragraph filter behaviour |
| `TestLamleyIntegration` / `TestMattelIntegration` / `TestAutoevolutionIntegration` | `:183`/`:219`/`:261` | End-to-end smoke through each parser, including `(подписывайтесь на меня в Instagram @diecast215)` regression |
| `TestFilterBlocks` | `:314` | Block-level handling of plug paragraphs |

### Per-source regression tests for filter

`grep` confirms `test_autoevolution_source.py`, `test_lamley_source.py`, `test_mattel_news_source.py` do NOT call `filter_boilerplate` / `filter_blocks` / `is_boilerplate` directly. The only filter-specific test coverage lives in `test_boilerplate_filter.py`. Variant A regression tests for the new patterns should therefore be added to `test_boilerplate_filter.py`, not the per-source files.

### Variant B test slot

No existing test file covers `_fallback_publish`'s post-translation cleanup. Suggested options:
- New file `tests/test_author_plug_filter_b.py` for unit tests of the variant B helper itself (call it directly with synthetic `ru_title` / `ru_paragraphs` / `ru_blocks`).
- Integration coverage may slot into `tests/test_fallback_publish_paths.py` (already exists per the test directory listing) — confirm by reading that file before deciding.

---

## 6. Risk patterns

### Risk #1 — bumping `_MAX_BOILERPLATE_LEN` 80 → 120

**Search result:** the only test pinned to literal `80` is `tests/test_boilerplate_filter.py:129` (`assert len(long_text) > 80`) inside `TestLengthThreshold.test_long_paragraph_with_trigger_preserved`. The synthetic string at `:125–128` is comfortably > 120 chars (`"Share on Facebook with all your friends and family for the rest of your life and even beyond, share share share."`) — so the existing assertion `is_boilerplate(...) is False` holds at the new bound, but the literal `80` in the assert message becomes misleading. Update the assertion to compare against the new constant (or import `_MAX_BOILERPLATE_LEN`).

No other code or doc pins to `80` — `boilerplate_filter.py:34` and `:79` are the only definition sites; `patterns.md` mentions "Length-bounded at 80 chars" descriptively (`/workspaces/debian-2/my-hw/.claude/skills/project-knowledge/references/patterns.md:28`) and would need a one-line update.

### Risk #2 — broad `^Instagram:` pattern false-positives

UX guidelines (`/.claude/skills/project-knowledge/references/ux-guidelines.md:65, :88, :90`) explicitly classify `Instagram: @...`, `Facebook: facebook.com/...`, `YouTube: @...`, `Reddit: u/...` as the canonical author-plug shape that must be dropped — tail-of-article social links. They are listed as "allowed drops" in the manual transcreation flow, meaning operators have already validated this exact pattern as never-real-content. Risk of a legitimate sentence starting with `"Instagram: ..."` is therefore very low in this domain (HW news), and the length bound (≤ 120 with the new max) further constrains hits.

No test fixture under `tests/fixtures/` contains `Instagram:` as legitimate body content. The only `Instagram` test reference is `tests/test_boilerplate_filter.py:44` (`"Follow us on Instagram"`) — a positive case for the existing `^follow us on \w+` pattern. No regression on adding stricter Instagram patterns.

### Risk #3 — variant B regex false-positives on RU content

Search for `написал.*Instagram` / `запостил.*Instagram` / `написали в Instagram` returned **zero hits** across the codebase and tests — these phrasings have never appeared in real translated content. Still, the variant B regex must be authored to match plug **sentences** (cue + handle / cue + parenthetical), not bare mentions of `Instagram` / `Facebook` / `YouTube` etc. Specifically:
- Anchor on `подписывайтесь`, `подписаться`, `следите за нами`, `следите за мной`, `мой Instagram`, `наш Instagram`, `(.*Instagram\s*@\w+.*)`-shaped parentheticals.
- Do **not** match bare brand mentions ("опубликовал в Instagram", "написал в Twitter") — those are legitimate body content.
- Apply the same "sentence-shape" boundary the spec calls for: cut the matched sentence (period-to-period or parenthetical), don't collapse the whole paragraph.

### Risk #4 — existing `^follow us on \w+` already covers some new patterns

Pattern at `boilerplate_filter.py:49` (`r'^follow us on \w+'`) overlaps with the proposed `(follow|check) (me|us) on (instagram|...)`. New patterns must add the `me` axis (and the parenthetical `(подписывайтесь на меня …)` shape) — not just rephrase what's already filtered. Worth deduplicating: collapse the existing `^follow us on \w+` line into the new broader `^(follow|check)\s+(me|us)\s+on\s+(instagram|twitter|x|tiktok|youtube|facebook|reddit)\b` so we don't have two patterns serving the same purpose.

---

## 7. Constraints & infrastructure

### Deployment

- `boilerplate_filter.py` is a deployed cron-side module:
  - `deploy.sh:52` — listed in the `FILES=(...)` array.
  - `.github/workflows/deploy.yml:132` — mirrored in the workflow's `FILES=(...)` array (per the byte-for-byte comment at `:103–105`).
  - No additional file lists touched.

### CI

- `.github/workflows/ci.yml:42` — `python -m pytest tests/ -v`. The full `tests/` directory is in CI; new tests in `tests/test_boilerplate_filter.py` (or a sibling) run automatically. No CI config change needed.

### DB schema

- `pending_articles_repo.py:52, 88` — schema for `pending_articles` and `published_articles` shows `paragraphs TEXT NOT NULL`, `blocks TEXT`, `ru_paragraphs TEXT`, `ru_blocks TEXT`. Both variants store cleaned text into the SAME columns the parser already writes — no schema change needed. JSON serialisation is encapsulated in `_dumps` (`pending_articles_repo.py:118`).

### Secrets / env vars

- No new env-var or secret introduced. Variant B helper is pure-regex.

### Dependencies

- `re` (stdlib). No new third-party dependency.

### Pre-commit hooks

- None visible at the project root that affect this change scope (no `.pre-commit-config.yaml` referenced in the file listing of `/workspaces/debian-2/my-hw/`).

---

## 8. Adjacent context (not in scope but worth knowing)

- `claude_transcreation.py:435` — `_patch_text_with_ru_paragraphs` is called when the LLM returns `null` blocks; it splices `ru_paragraphs` into the EN block scaffold. This means even with `null`-blocks LLMs, `ru_blocks` may be reconstructed before `_fallback_publish` sees `claude_result`. Variant B applied at the `news_bot.py:1005` cleanup point still catches everything regardless of which reconstruction path produced the blocks.
- `openrouter_transcreation.py:420` — comment refers to autoevolution-side `filter_blocks` already running. Variant A naturally extends this guarantee to author plugs without any additional engine-side change.
- UX guidelines (`ux-guidelines.md:64–69`) define the canonical "allowed drops" taxonomy: (a) author social links, (b) share-button UI bleed, (c) corporate boilerplate. Variant A + B together cover (a). Existing filter covers (b). (c) is out of scope for this feature ("About Mattel" / "Press Contact" — operator decision, mentioned in spec as future feature).
- Test infrastructure: `tests/conftest.py` exists (per directory listing) — confirm fixture sharing patterns there before adding new fixtures. `tests/fixtures/mattel_flight_builder.py:_make_flight_article` is the existing helper for synthesising Mattel test HTML.

---

## Tech-Spec Deepening (round 2) — 2026-04-30

Implementation-grade detail for tech-spec authoring. Sections 1–8 above remain the canonical first-pass map; this section drills into exact regex sources, line-by-line insertion sites, and contract traces.

### 9. Variant A — exact `re.compile(...)` proposals

Each pattern below is the **proposed** new entry to `_BOILERPLATE_PATTERNS` at `boilerplate_filter.py:38–65`. Order within the list matters only for readability (each is `pat.search(s)` against the whole stripped paragraph; first match wins). Length-bound `_MAX_BOILERPLATE_LEN` (currently `80`, proposed bump to `120`) gates everything.

#### A1 — umbrella "follow|check|subscribe me/us on <platform>"

Replaces the existing narrow pattern.

```python
re.compile(
    r'^(follow|check|subscribe to)\s+(me|us)\s+on\s+'
    r'(instagram|twitter|x|tiktok|youtube|facebook|reddit|patreon|discord|linktree)\b',
    re.I,
),
```

Covers AC1 ("(follow me on Instagram @diecast215)" — note: leading `(` would prevent match; see A2 for parenthesised umbrella).
Also covers AC4 ("Follow me on Instagram for the next reveal").

**Removal — `boilerplate_filter.py:49`:** legacy line `re.compile(r'^follow us on \w+', re.I),` is fully shadowed by A1 (any `follow us on Instagram` matches A1 too) AND by the platform-list constraint (avoids broad `\w+` accidental matches like "follow us on board"). User-spec calls for this collapse explicitly. Single-line delete.

#### A2 — parenthesised umbrella with mandatory `@handle`

```python
re.compile(
    r'^\(\s*(follow|check|subscribe to|join)\s+(me|us)\s+on\s+'
    r'(instagram|twitter|x|tiktok|youtube|facebook|reddit|patreon|discord|linktree)'
    r'\s+@\w+\s*\)$',
    re.I,
),
```

Covers AC1 specifically: `(follow me on Instagram @diecast215)` — the trigger-leak shape.

#### A3 — `Platform: handle` shape (with or without `@`)

Covers AC3 ("Instagram: diecast215") and the manual-review canonical form (`ux-guidelines.md:65`).

```python
re.compile(
    r'^(instagram|twitter|x|tiktok|youtube|facebook|reddit|patreon|discord|linktree)'
    r'\s*:\s*@?[\w./_-]+\s*$',
    re.I,
),
```

#### A4 — orphan handle on its own line

Covers AC2 (`@diecast215` standalone). Trivial pattern, no platform anchor — depends on length-bound + standalone-paragraph context to avoid false positives (no real prose paragraph is just `@handle`).

```python
re.compile(r'^@\w{2,30}$', re.I),
```

**Shadowing check on existing list:** none. The existing `_BOILERPLATE_PATTERNS` has zero `^@`-anchored patterns. Confirmed by re-reading lines 38–65 — every existing pattern starts with literal English/Russian words.

#### A5 — "subscribe to my channel / my newsletter" (author-form, distinct from A1)

```python
re.compile(
    r'^subscribe\s+to\s+my\s+'
    r'(channel|newsletter|patreon|youtube|page|feed)\b',
    re.I,
),
```

Distinct from existing `^subscribe( to (our )?newsletter)?$` at `boilerplate_filter.py:48` (that one is `our`, this one is `my` and adds platform vocabulary). Existing pattern stays — a different shape.

**Order rationale:** A1, A2, A3, A4, A5 proposed insertion BETWEEN existing line 49 (after deletion of `^follow us on \w+`) and line 50. They are all EN-side and stay grouped with the EN block. No RU additions for variant A — Russian is variant B's job.

### 10. Variant B — exact `re.compile(...)` + `re.sub(...)` set

New module: `/workspaces/debian-2/my-hw/author_plug_filter.py`. Pure-stdlib (`re`, `logging`). Public API:

```python
def strip_author_plugs(text: str) -> tuple[str, list[str]]: ...
def strip_in_blocks(blocks: list[dict]) -> tuple[list[dict], list[str]]: ...
```

Each returns `(cleaned, removed_fragments)` — the second element drives the AC9 INFO log. Caller in `_fallback_publish` discards `removed_fragments` after logging or accumulates them across all RU fields and emits one INFO line per strip.

#### B1 — cue-verb-anchored sentence (RU)

Mandatory cue verb anchor. Sentence boundary: from the cue back to the previous `.`/`!`/`?`/start-of-string, forward to the next `.`/`!`/`?` or end-of-string.

```python
_CUE_RU = re.compile(
    r'(?:'
    r'подпиш[иу]тесь|подпис[ыа]вайтесь|подписаться|'
    r'следите\s+за\s+(?:нами|мной)|'
    r'(?:мой|наш|моего|нашего)\s+(?:Instagram|Twitter|X|TikTok|YouTube|Facebook|Reddit|канал|канала)'
    r')',
    re.I,
)

# Sentence-extent regex: capture from sentence start through cue to terminal punct.
_RU_SENTENCE_WITH_CUE = re.compile(
    r'(?:(?<=[\.\!\?])\s*|^)'                      # start: after sentence-end OR string start
    r'[^\.\!\?]*?'                                  # any chars (non-greedy) up to cue
    + _CUE_RU.pattern +
    r'[^\.\!\?]*?'                                  # any chars (non-greedy) up to terminal punct
    r'(?:[\.\!\?]+|$)\s*',                          # terminal punct OR end of string
    re.I,
)
```

Replacement: `re.sub(_RU_SENTENCE_WITH_CUE, '', text)` then `re.sub(r'\s+', ' ', cleaned).strip()` to collapse stray whitespace introduced at sentence boundaries.

Covers AC6, AC7, AC8 (the `re.sub` removes ALL matches in one pass).

#### B2 — parenthesised umbrella with mandatory `@handle` (zonal pattern)

Covers AC12 (catches "(подпишитесь… в Instagram @x)" / "(посмотрите меня в Twitter @y)") AND its EN equivalent. AC15-mandated `@handle` requirement removes "(см. фото в Instagram)" risk.

```python
_PARENTHETICAL_PLUG = re.compile(
    r'\s*\(\s*[^()]*?'
    r'(?:Instagram|Twitter|X|TikTok|YouTube|Facebook|Reddit|Patreon|Discord|Linktree)'
    r'\s*@\w{2,30}'
    r'[^()]*?\)\s*',
    re.I,
)
```

Replacement: `re.sub(_PARENTHETICAL_PLUG, ' ', text)` (one-space replacement preserves word boundaries) → trailing `re.sub(r'\s+', ' ', cleaned).strip()`.

Note this also catches the leak trigger `(подписывайтесь на меня в Instagram @diecast215 )` — both B1 (cue verb) and B2 (parenthetical + @handle) match it; first-pass B2 wins because the regex consumes the entire parenthetical including outer whitespace.

#### B3 — orphan-handle in RU output

Defensive: covers a `@handle`-only output paragraph that slipped through variant A.

```python
_ORPHAN_HANDLE = re.compile(r'^\s*@\w{2,30}\s*$')
```

Used as predicate (`pat.fullmatch(text)`) — when an entire paragraph matches, the paragraph becomes empty string.

#### Strip pipeline for a single string

```python
def strip_author_plugs(text: str) -> tuple[str, list[str]]:
    if not isinstance(text, str) or not text:
        return text, []
    removed: list[str] = []
    cleaned = text
    # 1. Strip parenthesised plugs first (greedy outer match removes whole parens).
    for m in _PARENTHETICAL_PLUG.finditer(cleaned):
        removed.append(m.group(0).strip())
    cleaned = _PARENTHETICAL_PLUG.sub(' ', cleaned)
    # 2. Strip cue-verb-anchored sentences (RU + EN).
    for m in _RU_SENTENCE_WITH_CUE.finditer(cleaned):
        removed.append(m.group(0).strip())
    cleaned = _RU_SENTENCE_WITH_CUE.sub('', cleaned)
    # 3. Orphan handle → empty.
    if _ORPHAN_HANDLE.fullmatch(cleaned.strip()):
        removed.append(cleaned.strip())
        cleaned = ''
    # 4. Whitespace cleanup.
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned, removed
```

(Tech-spec is free to reshape — this is a working sketch to anchor task decomposition.)

#### Negative-fixture battery (mandatory tests)

These MUST pass through `strip_author_plugs` unchanged (anti-false-positive regression):

```python
NEGATIVES = [
    # AC13 — real Instagram mention, no cue verb, no @handle parenthetical.
    "The collector posted his find to Instagram and gathered 50K likes.",
    # AC14 — RU body content, no cue verb anchor.
    "Коллекционер написал в Instagram, что нашёл редкий Chase.",
    # AC15a — journalistic parenthetical, no @handle.
    "Хорошие фото можно найти в источнике (см. фото в Instagram).",
    # AC15b — broadcast parenthetical, no @handle.
    "Анонс прошёл вчера (трансляция шла на YouTube).",
    # AC16 — corporate plug, out of scope.
    "Follow Mattel on Instagram, X, and Facebook for more news.",
    # Defensive — bare brand mention on its own line, no handle, not orphan.
    "Instagram",
    # Defensive — full-prose sentence that happens to start with a cue verb but no platform.
    "Подписывайтесь на новости индустрии через RSS-агрегаторы — это удобно.",
]
```

Note the last fixture: `подписывайтесь` alone (without platform anchor / `@handle` / parenthetical) does NOT match because `_CUE_RU` requires either an inflected `подпиш[иу]тесь`-style token (which IS bare) followed by content matching the parent `_RU_SENTENCE_WITH_CUE` envelope. **This is a known soft-edge** — tech-spec must decide whether bare `подписывайтесь` should match or not. Recommendation: require either `(на|за)\s+(меня|нас|нами|мной)` OR a platform name within the sentence to qualify, so generic "subscribe to RSS" survives.

### 11. `_MAX_BOILERPLATE_LEN` 80 → 120 — exact assertion change

Test pinned to literal `80`: `tests/test_boilerplate_filter.py:129`:

```
129:        assert len(long_text) > 80
```

The synthetic `long_text` at lines 125–128 is 113 chars — `>80` holds today, `>120` would FAIL with the bumped constant.

**Recommended fix per tech-spec:** import the constant and assert against it directly:

```python
from boilerplate_filter import _MAX_BOILERPLATE_LEN
...
assert len(long_text) > _MAX_BOILERPLATE_LEN
```

OR extend the synthetic string until `>120`. Constant-import is more robust against future bumps. The test's *intent* (exceed-bound → not boilerplate) survives unchanged.

**Other literal-`80` sites:** none in code. Documentation hit at `.claude/skills/project-knowledge/references/patterns.md:28` ("Length-bounded at 80 chars") — operator-facing prose, update to "120 chars" in the same wave.

### 12. Variant B insertion site — exact 10-line diff window in `news_bot.py`

Around `news_bot.py:1004–1012`:

```
1004                ru_title, ru_subtitle, ru_paragraphs, ru_blocks = _google_translate()
1005                used_google_fallback = True
1006
1007        # Step 2: Telegraph — reuse saved URL per Decision 9 idempotency.
1008        # Done BEFORE persisting RU so a Telegraph failure keeps
1009        # ``ru_paragraphs IS NULL`` on the pending row — the next slot
1010        # in the distributed-publish loop will pull it again and the
1011        # attempt loop can retry. Once Telegraph succeeds the URL is written via
1012        # ``mark_telegraph_published`` (a dedicated txn) so a Telegram
```

**Insertion: blank line between current line 1005 and line 1007.** New block (~12 lines) sits there:

```python
# Variant B — strip author plugs from RU output before render + persist.
# Single call site; covers Claude/OpenRouter/OpenAI/Gemini and Google
# fallback. Wrapped in try/except (publish-something > publish-nothing).
try:
    ru_title, removed_t = author_plug_filter.strip_author_plugs(ru_title)
    ru_subtitle, removed_s = author_plug_filter.strip_author_plugs(ru_subtitle)
    ru_paragraphs, removed_p = author_plug_filter.strip_in_paragraphs(ru_paragraphs)
    ru_blocks, removed_b = author_plug_filter.strip_in_blocks(ru_blocks)
    for frag in (*removed_t, *removed_s, *removed_p, *removed_b):
        logger.info(f"[author_plug] stripped from {link}: {frag!r}")
except Exception as plug_err:  # noqa: BLE001 — never block publish on filter
    logger.error(
        f"[author_plug] strip failed for {link}: "
        f"{sanitize_error_message(plug_err)} — using original RU"
    )
```

**Ordering relative to `_maybe_record_recovery()`:** that call sits at `news_bot.py:942` INSIDE the try-block of the Claude branch — far above the convergence point. Variant B sits AFTER both branches converge. Confirmed: only ONE call site needed.

**Ordering relative to Telegraph upload:** strictly BEFORE `news_bot.py:1027` (`telegraph_publisher.publish_article(...)`) — ensures clean text reaches Telegraph.

**Ordering relative to `update_staged`:** strictly BEFORE `news_bot.py:1055` — ensures clean text persists to DB so any operator retry via `hw_review publish` replays cleaned text. Insertion at line 1006 satisfies both invariants.

**`outage_signal` path:** unaffected. Variant B runs in degraded-mode (post `_google_translate()` at line 1004) too, satisfying AC11.

**Import to add:** at the import block near `news_bot.py:55–60`:

```python
import author_plug_filter
```

### 13. Block-traversal contract table

From `telegraph_publisher.py:114–186` (`_build_content_from_blocks`):

| Block type | Field rendered | Eligible for variant B strip? |
|---|---|---|
| `paragraph` | `block["text"]` (line 173) | YES — `text` |
| `lead` | `block["text"]` wrapped in `<b>` (line 175) | YES — `text` |
| `heading` | `block["text"]` wrapped in `<h{level}>` (line 177) | YES — `text` |
| `image` | `block["src"]` + optional `block.get("caption", "")` (lines 178–179, via `figure_img`) | YES — `caption` only (NEVER `src`) |
| `video` | `block["src"]` only via `iframe(...)` (line 181) — caption NOT rendered | NO — neither `src` nor `caption` reaches Telegraph |
| (other) | not rendered (no `elif` clause) | N/A — pass-through |

`strip_in_blocks` should mirror this table: for `paragraph`/`lead`/`heading` clean `text`; for `image` clean `caption`; for `video` and unknown types pass through unchanged. NB: matches the pre-existing translation table at `_llm_common.py:106` (`_PATCHED_TEXT_BLOCK_TYPES = ("lead", "paragraph", "heading")`) — re-use the constant if cross-module import is acceptable; otherwise mirror it.

### 14. Empty-paragraph / empty-block handling

#### Empty `ru_paragraphs[i]`

Path: `_build_content` at `telegraph_publisher.py:239–242`:

```
239:    for i, para in enumerate(paragraphs):
240:        nodes.append(p(para))
241:        if remaining and (i + 1) % 3 == 0:
242:            nodes.append(figure_img(remaining.pop(0)))
```

An empty `para` becomes `{"tag": "p", "children": [""]}`. Telegraph's API accepts the empty `<p>` and renders it as a near-invisible blank line. **Decision required by tech-spec:** keep the empty `<p>` (visible spacer, low harm) OR drop empty paragraphs in the strip helper. Recommendation: drop them at the `strip_in_paragraphs` boundary — `[p for p in cleaned if p.strip()]` — both for cosmetics and to avoid "phantom paragraphs" from over-strip.

#### Empty `block["text"]` for `paragraph` block

Path: `telegraph_publisher.py:172–173`:

```
172:        if t == "paragraph":
173:            nodes.append(p(block["text"]))
```

Identical behaviour: empty string produces an empty `<p>`. Telegraph renders blank.

#### Dropping a block from the list

`telegraph_publisher.py:149–150` selects `first_image_idx` BEFORE the iteration. If `strip_in_blocks` removes a `paragraph`-type block (because its only content was a plug), the first-image index calculation is unaffected (image blocks are never dropped by this helper). Downstream invariants:

- `pending_articles_repo.update_staged` (line 244) accepts any list — no length invariant against `paragraphs` or `en_blocks`.
- `_translate_block_strings` (claude_transcreation.py:443) is upstream of the strip — runs on EN blocks; not invoked again post-strip.
- `_patch_text_with_ru_paragraphs` (`_llm_common.py:321`) is also upstream of the strip — invoked at `claude_transcreation.py:436`, BEFORE `_fallback_publish` returns from `transcreate_via_claude`. By the time variant B sees `ru_blocks`, that splice has already happened.

**Verdict:** dropping a now-empty `paragraph` block is safe. Recommendation for the strip helper: keep blocks whose text became empty as zero-length-text blocks (preserves order, simpler contract) and let Telegraph render the empty `<p>` — operator-visible signal that something was stripped. Alternative: drop the block. Tech-spec authoritative call.

#### LLM paragraph-count validator

`_llm_common.py:224–231` — `_parse_response` warns on count mismatch but does NOT raise (soft-divergence; the comment at lines 225–226 explicitly relaxes the constraint). Variant B running AFTER parse means count divergence introduced by stripping is not re-validated — safe.

### 15. `_patch_text_with_ru_paragraphs` interaction with variant B

Source: `_llm_common.py:321–348` (round-1 research already documented the function shape).

**Call order (verified):**
1. `transcreate_via_claude` → `_parse_response` → returns `parsed` dict.
2. Inside `claude_transcreation.py:435–445` — IF `expected_block_count and not parsed.get("blocks")`, splice `ru_paragraphs` into EN block scaffold via `_patch_text_with_ru_paragraphs`, THEN translate captions via `_translate_block_strings`.
3. Function returns `{title, alts, subtitle, paragraphs, blocks}`.
4. Back in `news_bot.py:934` — `claude_result = transcreate_via_claude(row)`; `ru_paragraphs` and `ru_blocks` are unpacked from this dict.
5. **Variant B runs HERE (proposed insertion at line 1006).**

So by the time variant B sees `ru_blocks`, every text-bearing block already carries spliced RU text from `ru_paragraphs`. **Iterating both `ru_paragraphs` AND `ru_blocks[*].text` independently is redundant — but safe.** Idempotent regex-strip: applying the same regex to a string that has no match is a no-op. Cost: one extra `re.sub` per paragraph that's already clean. Order-of-magnitude trivial.

**However:** the splice copies STRINGS BY REFERENCE in Python — no, strings are immutable, so `_patch_text_with_ru_paragraphs` rebuilds each block's `text` field via `new_block["text"] = ru` (line 344). Hence `ru_paragraphs[i]` and `ru_blocks[k]["text"]` (where block `k` is the `i`-th `lead`/`paragraph`/`heading`) are independent string objects with the same content. Variant B mutating `ru_paragraphs[i]` does NOT affect `ru_blocks[k]["text"]` and vice versa — both must be cleaned independently.

**Path NOT exercising splice:** when LLM returns matching blocks (count == expected), `parsed["blocks"]` is the model's output directly, and the `_patch_text_with_ru_paragraphs` branch is skipped. In that case `ru_blocks[k]["text"]` is the model-translated RU and may diverge from `ru_paragraphs[i]` (the model can phrase the same source paragraph differently in the flat list vs. the block list). Variant B still cleans both — necessary and not redundant.

### 16. Existing tests to verify before changes (regression baseline)

Tests that must stay green:

| File | Tests | Why |
|---|---|---|
| `tests/test_boilerplate_filter.py` | All ~14 (lines 22–388) | Variant A extends `_BOILERPLATE_PATTERNS`; existing positives + negatives + integration must still pass. `TestLengthThreshold.test_long_paragraph_with_trigger_preserved` (line 122) needs the assertion fix from §11. |
| `tests/test_lamley_source.py` | All | Confirms parser exit shape unchanged |
| `tests/test_mattel_news_source.py` | All | Same |
| `tests/test_mattel_integration.py` | All | Per-source integration |
| `tests/test_autoevolution_source.py` | All | Per-source integration |
| `tests/test_fallback_publish_paths.py` | 5 tests at lines 133, 228, 287, 318, 370 | Confirms `_fallback_publish` end-to-end shape: Claude path, per-article failure, English-leak rejection, already-in-fallback, outage-error degraded path. Variant B's insertion at line 1006 must not perturb any of these — they all assert ordering of `mark_telegraph_published` / `send_telegraph_teaser` / `move_to_published`, which sit AFTER variant B. Wave 2 spot-check anchor. |
| `tests/test_openrouter_transcreation.py:317, 387, 413` | `test_patch_text_with_ru_paragraphs_*` | Confirms `_patch_text_with_ru_paragraphs` shape unchanged (variant B is separate, doesn't touch this helper). |

Representative test signatures:

```python
# tests/test_boilerplate_filter.py:122
def test_long_paragraph_with_trigger_preserved(self):

# tests/test_fallback_publish_paths.py:133
def test_fallback_publish_claude_path(self):

# tests/test_openrouter_transcreation.py:317
def test_patch_text_with_ru_paragraphs_splices_in_order(self):
```

`tests/test_fallback_publish_paths.py` should grow ONE smoke test per user-spec "Тестирование": "variant B вызывается на canonical-пути и не вызывается дважды". Sketch: `with patch('news_bot.author_plug_filter.strip_author_plugs') as mock: ... self.assertEqual(mock.call_count, expected_count)`.

### 17. UX-guidelines.md prompt edit — wave-ordering guidance

File: `/workspaces/debian-2/my-hw/.claude/skills/project-knowledge/references/ux-guidelines.md` (122 lines).

**Edit point:** line 64–69, the existing "Единственные разрешённые дропы" list. Sub-item (a) already names "Author social links" with the canonical shapes. Tech-spec extension is a one-or-two-line clarification: "Включая встроенные плаги в скобках вида `(подписывайтесь на меня в Instagram @handle)` или с глагольным якорем — удалять без согласования". The prompt body itself (lines 19–40, blockquote) does NOT need changes — instruction is about scope, not voice.

**Deploy path:** `deploy.sh:56` and `.github/workflows/deploy.yml:136` both list this exact path; deployed flat as `$DEPLOY_PATH/ux-guidelines.md` (Decision 8 fallback documented at `deploy.sh:30` and `deploy.yml:110–113`). No additional deploy work.

**Wave ordering:** prompt edit is an LLM-side soft-defence; code-side filters (variant A + B) are hard defences. **They are independent — no wave-order coupling.** The prompt edit can land in:

- The same commit as variant A (low risk, tightly scoped).
- A separate prep commit before variant A (CI runs once on the prompt edit, no test impact since CI's `check-skip` job at `.github/workflows/ci.yml:18–25` skips `.md`-only changes — so the edit-only commit triggers no test run, and the next code commit re-runs CI in full).
- A follow-up commit after both variants land (also fine).

Recommendation: bundle the prompt edit with variant A (Wave 1) — single user-visible change, "extend the boilerplate-filter list AND tell the LLM about it".

### 18. Logging style match (AC9)

`news_bot.py` uses **f-strings** for context-rich logs and **printf-style `%s`** for parametrised structured logs. Sample of fallback-path style:

- `news_bot.py:924`: `logger.info(f"[fallback] is_fallback_active=True — routing {link} via Google")`
- `news_bot.py:951`: `logger.warning(f"[fallback] Claude per-article failure for {link}: {type(exc).__name__}: {sanitize_error_message(exc)} — slot strike (next slot retries this row)")`
- `news_bot.py:1017`: `logger.info(f"[fallback] reusing stored telegraph_url for {link}: {telegraph_url}")`
- `news_bot.py:1085`: `logger.info(f"[fallback] Published {link} via_review={via_review} url={telegraph_url}")`

Pattern: `[<bracket-tag>]` prefix + free-form context with `{var}` interpolation. Use `[author_plug]` as the bracket tag for variant B (consistent with `[fallback]`, `[recovery]`).

Proposed AC9 INFO line shape:

```python
logger.info(f"[author_plug] stripped from {link}: {frag!r}")
```

`{frag!r}` (repr) preserves whitespace/quotes — operator can copy-paste the fragment from `journalctl` directly into a regex test if a false-positive is suspected.

### 19. Pre-commit hooks / CI gates

`.pre-commit-config.yaml` (full content):

| Hook | Repo | Effect on this feature |
|---|---|---|
| `gitleaks` | gitleaks/gitleaks v8.21.4 | None — no secrets in regex/test fixtures. |
| `trailing-whitespace`, `end-of-file-fixer` | pre-commit/pre-commit-hooks v5.0.0 | Standard hygiene; new files must end in `\n`. |
| `check-merge-conflict`, `detect-private-key`, `check-yaml` | same | None impacted. |
| `check-added-large-files` (`--maxkb=1000`) | same | None — new module is < 5KB. |

**No** `ruff` / `black` / `mypy` / `pytest` pre-commit hook configured. Linting / type-checking happens manually if at all.

**No** catastrophic-backtracking scanner in pre-commit. Tech-spec should manually audit each new regex for nested quantifiers — the proposed patterns above use only:
- `[^\.\!\?]*?` (non-greedy, bounded char class — safe)
- `\w{2,30}` (bounded repetition — safe)
- `\s+` / `\s*` (linear — safe)

No pattern uses `(a+)+` or `(a|a)*` shapes — backtracking-safe.

`.github/workflows/ci.yml` (43 lines):
- `check-skip` job at lines 10–25: skips test job if all changed files match `\.(md|txt)$|^\.claude/|^\.spec/|^docs/`. Variant A/B code commits will trigger tests; ux-guidelines.md-only commits will skip tests (intentional — saves CI minutes on prose edits). Note: the path-based skip means a commit that ONLY edits `.claude/skills/project-knowledge/references/ux-guidelines.md` counts as a skip (file is under `.claude/`).
- `test` job at lines 27–42: `python -m pytest tests/ -v` against Python 3.13. New tests in `tests/test_boilerplate_filter.py` and the new `tests/test_author_plug_filter.py` are picked up automatically.

`.github/workflows/deploy.yml`: runs after CI on `main`. Mirror file list at line 132 (`boilerplate_filter.py`) and line 136 (`ux-guidelines.md`) — to add `author_plug_filter.py` to the deployment, both `deploy.sh:52` and `deploy.yml:132` need the new entry. **Not optional — without it the new module never reaches the server.**

### 20. Risk drift from round-1 research — spot-check

Re-verified line-anchors that round-1 cited:

| Round-1 anchor | Status |
|---|---|
| `boilerplate_filter.py:34` (`_MAX_BOILERPLATE_LEN = 80`) | Still at line 34 |
| `boilerplate_filter.py:38–65` (`_BOILERPLATE_PATTERNS`) | Still spans 38–65 |
| `boilerplate_filter.py:49` (`^follow us on \w+`) | Still at line 49 — variant A removes this |
| `boilerplate_filter.py:68` (`is_boilerplate`) | Still at line 68 |
| `lamley_source.py:349` (`paragraphs = filter_boilerplate(paragraphs)`) | Still at line 349 |
| `autoevolution_source.py:25, 293, 356` | Still at 25, 293, 356 |
| `mattel_news_source.py:36, 488` | Still at 36 (verified); 488 not re-verified but no recent edits in git log |
| `news_bot.py:838` (`_fallback_publish`) | Still at line 838 |
| `news_bot.py:1004` (`_google_translate` unpack) | Still at line 1004 |
| `news_bot.py:1027` (`telegraph_publisher.publish_article`) | Still at line 1027 |
| `news_bot.py:1055` (`pending_repo.update_staged`) | Still at line 1055 |
| `tests/test_boilerplate_filter.py:129` (assert > 80) | Still at line 129 |
| `_llm_common.py:106` (`_PATCHED_TEXT_BLOCK_TYPES`) | Still at line 106 |
| `_llm_common.py:321` (`_patch_text_with_ru_paragraphs`) | Still at line 321 |
| `claude_transcreation.py:436` (splice call) | Still at line 436 |
| `telegraph_publisher.py:114–186` (`_build_content_from_blocks`) | Still spans 114–186 |
| `pending_articles_repo.py:243` (`update_staged`) | Now at line 244 (1-line drift — docstring or comment edit, signature unchanged) |

**Drift verdict: negligible.** Single one-line drift in `pending_articles_repo.py:243→244` does not affect any contract. Round-1 research is current to 2026-04-30; tech-spec can rely on round-1 anchors.

### 21. Quick reference — files the tech-spec will touch

| File | Change kind | Wave |
|---|---|---|
| `boilerplate_filter.py` | Add 5 patterns; remove 1; bump `_MAX_BOILERPLATE_LEN` | 1 (variant A) |
| `tests/test_boilerplate_filter.py` | Add positives + negatives per AC1–AC5; fix line 129 assertion | 1 |
| `.claude/skills/project-knowledge/references/ux-guidelines.md` | Extend "разрешённые дропы" list | 1 (bundleable) |
| `.claude/skills/project-knowledge/references/patterns.md:28` | Update `80` → `120` | 1 |
| `author_plug_filter.py` | Create new module | 2 (variant B) |
| `tests/test_author_plug_filter.py` | Create test file | 2 |
| `news_bot.py` | Add import; insert ~12-line strip block at line 1006 | 2 |
| `tests/test_fallback_publish_paths.py` | Add ONE smoke test for variant B invocation | 2 |
| `deploy.sh:52` | Add `author_plug_filter.py` to FILES array | 2 |
| `.github/workflows/deploy.yml:132` | Add `author_plug_filter.py` to FILES array | 2 |

Total expected diff: ~250 lines added across 10 files; ~3 lines removed.
