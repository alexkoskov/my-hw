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
