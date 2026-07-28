# Code Research: source-formatting-parity

**Date:** 2026-07-28
**Repo:** `/workspaces/debian-2/my-hw` (branch `dev`)
**Scope:** bring `t_hunted_source.py`, `lamley_source.py`, `mattel_news_source.py` from flat
`paragraphs: list[str]` up to the `blocks` + `runs` contract already used by
`orangetrack_source.py` and `autoevolution_source.py`.

Facts and structure only — no design proposals.

---

## 1. Entry Points

The three flat parsers are reached through one dispatcher and one pass-through.

| File | Function | Lines | Role |
|---|---|---|---|
| `news_bot.py` | `fetch_full_article(entry)` | 3321–3367 | Hostname dispatcher. `urlparse(link).hostname` → substring match per source. |
| `news_bot.py` | `job()` step (b3) | 3755–3771 | Calls `fetch_full_article`, hard-drops on `not article.get('paragraphs')`. |
| `news_bot.py` | `_fetch_orangetrack_entries` | 3449–3533 | Orangetrack parses at fetch time; `fetch_full_article` is a pass-through (3337–3354). |
| `news_bot.py` | `_fetch_rss_entries` | 3381–3433 | Feeds for autoevolution / lamley / t-hunted (`feeds.json`). Bodies fetched later. |

Dispatch table inside `fetch_full_article` (3337–3362):

```
'orangetrackdiecast.com' → pass-through, forwards entry['blocks']   (3348–3354)
'corporate.mattel.com'   → fetch_mattel_article(link, notifier=…)   (3356)
'lamleygroup.com'        → lamley_source.fetch_lamley_article(…)    (3358)
'autoevolution.com'      → autoevolution_source.fetch_autoevolution_article(entry) (3360)
'blogspot.com'           → t_hunted_source.fetch_t_hunted_article(…) (3362)
```

**Only the orangetrack branch forwards `blocks`** (3353). The other four branches return whatever
the parser returns verbatim — so autoevolution's `blocks` survives because its parser puts the key
in its own return dict, not because the dispatcher does anything.

**Mattel is disabled in production.** `news_bot.SOURCES` (3548–3551) has
`# _fetch_mattel_entries,  # disabled — see comment above`; the rationale is at 3540–3547
(corporate.mattel.com moved to Astro/Netlify, body renders client-side, zero Hot Wheels articles
ever produced). `mattel_news_source.py` and its 42 tests are kept for possible restoration.
Any change to the Mattel parser has **zero live effect** today.

Signatures:

```python
# t_hunted_source.py:126
def fetch_t_hunted_article(link, session=None, notifier=None) -> Optional[Dict]
# lamley_source.py:266
def fetch_lamley_article(link, session=None, notifier=None) -> Optional[Dict]
# mattel_news_source.py:442
def fetch_mattel_article(link, session=None, notifier=None) -> Optional[Dict]
# orangetrack_source.py:961
def fetch_orangetrack_article(entry: dict, notifier=None) -> Optional[Dict]
# autoevolution_source.py:368
def fetch_autoevolution_article(entry: dict, fetcher=None) -> Optional[Dict]
```

---

## 2. The Reference Implementations

### 2.1 `orangetrack_source.py` — the richer of the two

#### `_runs_from_tag(tag) -> List[Dict]` (L284–412)

Run dict shape — this is the canonical shape in the codebase:

```python
{'text': str}                                   # plain
{'text': str, 'href': str}                      # anchor (safe scheme only)
{'text': str, 'formats': ['bold', 'italic']}    # inline formatting
{'text': str, 'href': str, 'formats': [...]}    # both
```

Mechanism: a `buf`/`fmt_stack`/`flush()` walker (300–412).
- `_INLINE_FORMAT_TAGS` (259–267): `strong`/`b`→`bold`, `em`/`i`→`italic`, `u`→`underline`,
  `s`/`del`→`strikethrough`.
- Anchors are handled in a dedicated branch (332–380) that recurses into anchor children
  (`collect`, 340–358) and emits ONE run per anchor. Unsafe href → text is inlined as plain (377–379).
- Format tags push/pop on `fmt_stack` (383–400). The comment at 393–395 claims `flush()` is called
  before descending *"otherwise the unformatted prefix would incorrectly get this format attached"* —
  **but the flush at 396 happens AFTER the push at 387/390, so the prefix gets the format anyway.**
  See §7.9.
- Post-pass (406–412): `re.sub(r"\s+", " ", …)` per run, `lstrip` first / `rstrip` last, empty runs dropped.

#### `_parse_content_encoded(html_str, link)` (L496–830) — the block walker

Emitters:

| Helper | Lines | Emits |
|---|---|---|
| `_emit_paragraph` | 530–577 | `{'type':'paragraph','text':str,'runs':[...]}`; splits a `<p>` on `<br>` into one block per segment (542–577) |
| `_emit_heading` | 579–613 | h2/h3/h4 → `{'type':'heading','level':3,'text','runs'}`; h5 → `{'type':'paragraph',...}` |
| `_emit_image` | 615–626 | `{'type':'image','src':str[,'caption':str]}`, dedup on `src.split('?')[0]` |
| `_emit_iframe` | 628–633 | `{'type':'video','src':<telegra.ph embed proxy URL>}` |
| `<li>` branch inside `_walk` | 745–766 | `{'type':'list_item','text','runs'}` — bullet is NOT prepended here |

`_walk(node)` (673–770): iterates `node.children`, dispatches **by tag name** (`handled_tags`,
639–649), recurses into any unhandled wrapper tag (769–770). `ul`/`ol` deliberately excluded from
`handled_tags` so recursion reaches the `<li>` branch.

Post-processing (774–830):
1. `blocks = filter_blocks(blocks)` (778)
2. `paragraphs_flat = [b['text'] for b in blocks if b['type'] in ('paragraph','heading','list_item')]` (792–795)
3. `paragraphs_flat = filter_boilerplate(paragraphs_flat)` (796)
4. **`subtitle = ""` — hardcoded** (808), with a long comment (798–807) explaining the
   SESSION-2026-05-06 off-by-one incident (see §7.1)
5. video-only synthesis: `body_paragraphs = [title]` when no paragraphs + has video (813–815)
6. `images_flat = [b['src'] for b in blocks if b['type']=='image'][:IMAGE_LIMIT]` (817–819)

Return keys (824–830): `title`, `subtitle`, `paragraphs`, `images`, `blocks`.

### 2.2 `autoevolution_source.py` — the weaker of the two

#### `_runs_from_tag(tag)` (L64–103)

**Different, poorer shape.** Only `{'text'}` and `{'text','href'}` — no `formats` at all.
Docstring 68–70 states it explicitly: *"Nested formatting (`<strong>`, `<em>`) is flattened to
plain text."* Autoevolution bold therefore does **not** survive today either; only its
structural `lead`/`heading` blocks do.

#### `_scrape_article_page(link, fetcher=None)` (L141–365)

Walks `for child in body.children` where `body = soup.find("div", class_="newstext")` (187, 231).
Block types emitted: `lead` (239–244), `image` (251–260, 321–334), `video` (265–272),
`paragraph` (301–304), `heading` (290–299 nested-in-`<p>` case, 305–314 top-level case).
No `list_item`.

`heading.level` is `int(tag.name[1])` (298, 313) — so **level can be 2**, which
`telegraph_publisher` normalises to 3 (334–335) but `hw_review._validate_block` rejects (315–316).

Post: `filter_blocks(blocks)` (347); flat `paragraphs` from `lead|paragraph|heading` (353–356);
`images` (357). Return keys 359–365: same five.

The RSS-only fallback `enrich_entry` (409–435) returns **four** keys — no `blocks`.

### 2.3 How generic is the machinery? (quantified)

| Piece | Lines | Generic | Site-specific |
|---|---|---|---|
| `orangetrack._runs_from_tag` | 284–412 (129 L) | ~117 L — tag→format map, anchor handling, whitespace normalisation | `_has_color_class` calls (381–391, 344–353) — 2 call sites into a 12-line WordPress helper (270–281) |
| `orangetrack._safe_href` / `_safe_img_src` | 208–248 (41 L) | 41 L — pure scheme allowlist | none |
| `orangetrack._INLINE_FORMAT_TAGS` | 259–267 | all | none |
| `orangetrack._walk` | 673–770 (98 L) | ~78 L — tag-name dispatch, wrapper recursion, li/figure/img/iframe branches | `_has_chrome_class` + `_CHROME_CLASS_MARKERS` (659–671, 679–680) = WordPress/JetPack chrome; the h5→paragraph carve-out (703–708) |
| `orangetrack._emit_paragraph` | 530–577 (48 L) | ~12 L (the no-`<br>` path, 542–549) | ~36 L `<br>`-splitting for the WordPress checklist-in-one-`<p>` shape |
| `orangetrack._best_img_src` | 420–456 | ~30 L srcset parsing is generic | the "prefer ≤1024" heuristic is tuned to WordPress.com's 300/600/1024 ladder |
| `orangetrack._parse_content_encoded` post-pass | 774–830 | flat-list derivation + `filter_blocks`/`filter_boilerplate` | `subtitle=""` (808), video-only title synthesis (813–815) |
| `autoevolution._scrape_article_page` | 141–365 | ~0 | entangled throughout: `div.newstext`, `fsz22`/`sanscond`, `ch_pic`, `ch_pic_crd`, `newsgal2`, `mainpic`, `ad300`/`intext`/`clearfix`, `ARTICLE_SLUG_RE` |

**Bottom line:** `orangetrack._runs_from_tag` + `_safe_href` + `_safe_img_src` +
`_INLINE_FORMAT_TAGS` (~190 lines) are reusable essentially as-is. `_walk` is ~80 % generic but
lives inside `_parse_content_encoded` as a closure over `blocks`/`seen_image_bases` — it is not
currently importable. `autoevolution._scrape_article_page` is not reusable at all; its
`_runs_from_tag` is a strictly weaker duplicate of orangetrack's.

There is **no shared module** for any of this today — `_runs_from_tag` is defined twice
(orangetrack 284, autoevolution 64) with divergent contracts.

---

## 3. The Three Flat Parsers

All three build `paragraphs` with a **flat, recursive `find_all` + `get_text(" ", strip=True)`** —
which destroys inline markup *and* silently duplicates nested text.

### 3.1 `t_hunted_source.py` (257 lines)

| Concern | Lines | Detail |
|---|---|---|
| Title | 177–182 | `h3.post-title` → `h1.entry-title` → `h1` |
| **Body root** | 186–190 | `div.post-body` → `div.entry-content` → `article`; `None` → `alert_t_hunted_no_body` + return None |
| **paragraphs build** | 195–199 | `for tag in body.find_all(["p","li","h2","h3","h4","blockquote"])`: `text = tag.get_text(" ", strip=True)`; append if `text and text != title` |
| Boilerplate filter | 201–205 | `paragraphs = filter_boilerplate(paragraphs)` — comment 201–204 marks the ordering as **CRITICAL** |
| **Subtitle lift** | 206–220 | `if len(paragraphs) >= 2: subtitle = paragraphs[0]; paragraphs = paragraphs[1:]` else `subtitle = ""` |
| Images | 226–250 | separate `body.find_all("img")` pass; Blogger lightbox `<a href>` lift (239–243); `_BLOGGER_SIZE_SUFFIX_RE.sub('', src.split("?",1)[0])` dedup (244); `_IMAGE_LIMIT = 30` (49) |
| Return | 252–257 | `{'title','subtitle','paragraphs','images'}` — **no `blocks`** |

The `len(paragraphs) >= 2` guard rationale is spelled out at 206–215: t-hunted's dominant format is
a photo-gallery post with a single intro paragraph; lifting it would leave `paragraphs == []` and
`news_bot.py:3768` (`not article.get('paragraphs')`) would drop the post entirely.

### 3.2 `lamley_source.py` (433 lines)

| Concern | Lines | Detail |
|---|---|---|
| Title | 376–377 | `h1.entry-title` → `h1` |
| **Body root** | 379–382 | `div.entry-content` → `article` |
| **paragraphs build** | 387–391 | identical loop to t-hunted (same six tags, same `get_text`) |
| Boilerplate filter | 393–395 | `filter_boilerplate` before subtitle, same reason |
| **Subtitle lift** | 397–401 | **unconditional**: `subtitle = paragraphs[0] if paragraphs else ""`; `paragraphs = paragraphs[1:]` |
| Images | 403–426 | `body.find_all("img")`, lightbox lift (416–419), `src.split("?",1)[0]` dedup (420), `IMAGE_LIMIT = 10` (93) |
| Return | 428–433 | four keys, no `blocks` |

Everything above line 371 is WAF/throttle apparatus (curl_cffi impersonation, 429 backoff,
per-URL blacklist, process cool-down) — untouched by this feature but it means **lamley fetches are
throttled to one per 20 s** (`_MIN_REQUEST_INTERVAL_S`, 101), which matters for any test that
exercises the live path.

### 3.3 `mattel_news_source.py` (527 lines)

| Concern | Lines | Detail |
|---|---|---|
| Body source | 501–505 | Not HTML-scraped — resolved from the Next.js RSC stream: `entry['body'] == "$<row-id>"` → `_resolve_body_html(concat, body_ref)` (313–355) returns a raw HTML string |
| **paragraphs build** | 358–368 | `_paragraphs_from_body(body_html)`: `soup.find_all(["p","li","h1","h2","h3","h4"])` + `get_text(" ", strip=True)`. **Includes `h1`, excludes `blockquote`** — differs from the other two |
| Boilerplate filter | 506 | `filter_boilerplate(_paragraphs_from_body(body_html))` |
| **Subtitle** | 520 | `_excerpt_to_str(entry.get("excerpt"))` — comes from feed metadata, **no lift from paragraphs** |
| Images | 508–518 | thumbnail only (`entry['thumbnail']['url']`), by explicit policy (comment 508–512) |
| Return | 522–527 | four keys, no `blocks` |

`_resolve_body_html` returns `""` on every failure mode (docstring 315–322) → `paragraphs == []`
is a legitimate, non-error outcome (AC9 / ES9c).

### 3.4 Shared defect in all three: recursive `find_all` duplicates nested text

`Tag.find_all` is recursive and returns document order, so nested matches are emitted twice.
Verified:

```python
html = '<div><ul><li><p>Item text</p></li></ul><p>Intro <b>BOLD</b> tail</p>'
       '<blockquote><p>Quote</p></blockquote></div>'
[t.get_text(" ", strip=True) for t in body.find_all(["p","li","h2","h3","h4","blockquote"])]
# → ['Item text', 'Item text', 'Intro BOLD tail', 'Quote', 'Quote']
```

`<li><p>…</p></li>` and `<blockquote><p>…</p></blockquote>` each produce a duplicate paragraph
today. orangetrack's `_walk` cannot produce this (it dispatches per node, once).

---

## 4. Downstream Consumers — `paragraphs` vs `blocks`

### 4.1 Verdict table

| Consumer | File:lines | Reads | Effect if a flat source starts returning `blocks` |
|---|---|---|---|
| `fetch_full_article` | `news_bot.py:3355–3362` | returns parser dict verbatim | **No care** — extra key passes through untouched |
| staging guard | `news_bot.py:3768` | `article.get('paragraphs')` | **No care** — as long as `paragraphs` stays non-empty. Breaks the post if a redesign empties it |
| `_is_text_only_checklist` | `news_bot.py:1548–1575` | `article['paragraphs']` char total vs `_CHECKLIST_BODY_TEXT_FLOOR` | **Degrades** if the flat list's content changes (e.g. de-duplicated nested text shortens it below the floor → post silently dropped as a checklist) |
| `_is_promo_article` | `news_bot.py:1737–1815` (para read 1793–1799) | first 8 paragraphs, 2000 chars | **Degrades** — same sensitivity; changing paragraph order/content shifts which markers are in scan range |
| `_content_gate_subject` / `_is_rejected_genre` | `news_bot.py:2187–2215`, 2313 | title + URL path **only** | **No care** |
| `model_extractor.extract_fingerprint` → `_gather_text` | `model_extractor.py:580–592` | `title` + `subtitle` + `paragraphs` | **Degrades** — dedup fingerprint text changes if the flat list changes. Fail-open (`news_bot.py:4069+`, `[E016]`) so it never blocks publishing |
| row assembly | `news_bot.py:4102–4118` | `article.get('blocks')` at 4110 | **Already wired.** `'blocks': article.get('blocks')` is unconditional — a new key is persisted with no code change |
| `pending_articles_repo.insert_pending` | `pending_articles_repo.py:339–391` | `_dumps(entry.get('blocks'))` at 378 | **Already wired.** Column `blocks TEXT` (DDL 77), NULL-preserving |
| queue round-trip | `pending_articles_repo.py:141` | `_PENDING_JSON_COLS` includes `'blocks'`, `'ru_blocks'` | **Round-trips fine.** `json.dumps(ensure_ascii=False)` / `json.loads` — nested `runs` survive verbatim |
| `failed_articles` | `pending_articles_repo.py:106–121, 143, 926–948, 1051–1080` | `blocks TEXT` column + `_FAILED_JSON_COLS` | **Round-trips fine** on move-to-failed / retry |
| `_fallback_publish` | `news_bot.py:3007–3306` | `claude_result['blocks']` (3083), passes `blocks=ru_blocks` (3221), persists via `update_staged` (3252–3258) | **Already wired.** But behaviour flips from the flat branch to the blocks branch — see §6 |
| `_strip_plugs` | `news_bot.py:1236–1252` | plain string, regex sub + whitespace collapse | **No care.** Note: it does **not** know about `**` markers |
| `_strip_plugs_in_blocks` | `news_bot.py:1255–1282` | `text`/`caption` of each block | **Works**, but drops empty blocks only for `('paragraph','lead','heading')` — **`list_item` is missing** (1276) |
| RU boilerplate re-filter | `news_bot.py:3167–3176` | block types `('paragraph','lead','heading','list_item')` | Works; `list_item` IS present here (inconsistent with 1276) |
| `telegraph_publisher.publish_article` | `telegraph_publisher.py:498–554` | `blocks` kwarg | **Already wired.** `blocks` non-empty ⇒ `paragraphs`/`images` ignored entirely (docstring 517–519) |
| `telegraph_publisher.preview_nodes` | `telegraph_publisher.py:453–495` | branch at 488 `if blocks:` | Same branch point for preview and publish |
| `_build_content_from_blocks` | `telegraph_publisher.py:295–386` | renders `paragraph`/`lead`/`heading`/`list_item`/`image`/`video` | Renders `runs` for paragraph (361), heading (369), list_item (375). **`lead` ignores `runs`** (365: `p(b_(block["text"]))`) |
| `_render_paragraph_with_runs` | `telegraph_publisher.py:188–292` | `runs` | **hrefs are disabled** (`href_val = None`, 245) — only `formats` render today |
| `preview_renderer.render_html` | `preview_renderer.py:159–192` | node tree | **BROKEN for bold** — see §7.2 |
| `hw_review._n_paragraphs` | `hw_review.py:178–185` | prefers block count | Counts `paragraph|lead|heading` — **omits `list_item`** |
| `hw_review cmd_stage` parity gate | `hw_review.py:396–404` | `row['blocks'] is not None` | **BREAKS the manual path** — see §7.3 |
| `hw_review._validate_block` | `hw_review.py:300–320` | strict key/type allowlist | **Rejects `runs` and `list_item`** — see §7.3 |
| `hw_review cmd_preview` / `cmd_publish` | `hw_review.py:496–503`, 603–610 | `row['ru_blocks']` | Works once staging passes |
| `boilerplate_filter.filter_boilerplate` | `boilerplate_filter.py:270–272` | `list[str]` | Unchanged use — still needed for the flat list |
| `boilerplate_filter.filter_blocks` | `boilerplate_filter.py:275–319` | `list[dict]`, reads `text`/`caption`/`src`/`image_url` | Works on any block carrying `runs` (extra keys are ignored, blocks pass through by reference) |
| `backfill_fingerprints.py` | 247 | `article.get('paragraphs')` | **No care** |

### 4.2 `blocks` persistence — confirmed round-trip

- Column: `pending_articles.blocks TEXT` (`pending_articles_repo.py:77`), plus
  `ru_blocks TEXT` (81) and `failed_articles.blocks TEXT` (113).
- Write: `insert_pending` line 378, `_dumps(entry.get('blocks'))` — `None` → SQL NULL,
  a list → `json.dumps(..., ensure_ascii=False)`.
- Read: `_row_to_dict` (168–179) deserialises every name in `_PENDING_JSON_COLS` (141).
- RU side: `update_staged(link, ru_title, ru_subtitle, ru_paragraphs, ru_blocks)` (394–412).
- No schema change is needed for this feature. `runs` is nested inside the block dicts and
  survives the JSON round-trip untouched.

---

## 5. The Translation Path

### 5.1 Encode (EN → `**` markers)

`_llm_common._build_user_message(article)` (`_llm_common.py:210–249`):

```python
paragraphs = list(article.get("paragraphs") or [])
blocks = article.get("blocks")
if isinstance(blocks, list) and blocks and paragraphs:        # ← line 223, THE GATE
    para_iter = iter(paragraphs)
    for block in blocks:
        if block.get("type") not in _PATCHED_TEXT_BLOCK_TYPES: continue
        p = next(para_iter)
        marked.append(_encode_format_markers(p, block.get("runs") or []))
    marked.extend(para_iter)
    paragraphs = marked
payload = {"source_name", "title", "subtitle", "paragraphs"}   # blocks NEVER sent (243–248)
```

`_encode_format_markers` (128–177) only encodes runs whose `formats` contains `"bold"`
(line 158: `if "bold" not in formats: continue`). Italic/underline/strikethrough are **dropped
across translation by design** (docstring 133–139). Positions come from `text.find(run_text)`
(159) with first-wrap-wins overlap resolution (163–169).

`_PATCHED_TEXT_BLOCK_TYPES` = `("lead","paragraph","heading","list_item")` (`_llm_common.py:115`).

**The pairing at line 229–239 is positional:** block *i* (patchable) ↔ paragraph *i*.
Any length mismatch between `blocks`' patchable count and `len(paragraphs)` mis-assigns runs.

### 5.2 System prompt

`_build_system_prompt` (123–125) appends `_JSON_ENVELOPE` (80–109) **unconditionally**.
The envelope contains:
- line 99: *"Do not output a `blocks` field."*
- lines 101–108: the "## Inline formatting markers" section telling the model to preserve
  `**...**` and *"Do not add new `**...**` markers to spans that were not marked in the input."*

That section is sent **even for flat articles that were never marked** — the model is primed to
emit `**` on every article regardless.

### 5.3 Decode (`**` markers → runs)

`_decode_format_markers(text) -> (clean_text, runs)` (`_llm_common.py:187–207`),
regex `_BOLD_MARKER_RE = r"\*\*([^*\n]+?)\*\*"` (184).

**Its only non-test call site in the entire repo is `_llm_common.py:479`**, inside
`_patch_text_with_ru_paragraphs` (448–491), which is itself called only from the four engines'
variant-B branch.

### 5.4 The four engines

`_translate_block_strings` is **duplicated verbatim in all four engines**, not shared:

| Engine | `_translate_block_strings` | `transcreate_via_claude` | `blocks_in` / `expected_block_count` | `_parse_response` call | variant-B guard | `_patch_text_with_ru_paragraphs` |
|---|---|---|---|---|---|---|
| `claude_transcreation.py` | 162–238 | 333–450 | 372–375 | 430 | 438 | **439–441** |
| `gemini_transcreation.py` | 135–207 (no `timeout_s`) | 288–379 | 303–306 | 363 | 369 | **370–372** |
| `openai_transcreation.py` | 132–206 | 290–384 | 305–308 | 366 | 372 | **373–375** |
| `openrouter_transcreation.py` | 220–307 | 390–494 | 405–408 | 465 | 475 | **480–482** |

Common facts:
- `expected_block_count = len(blocks_in) if isinstance(blocks_in, list) else None`.
- Variant-B guard is `if expected_block_count and not parsed.get("blocks")`.
- The returned dict is built **only** at `_llm_common.py:387–393` — `blocks` is **always a key**,
  and is `None` for a flat article. No engine ever synthesizes blocks from paragraphs.
- `_translate_block_strings` translates only `text` and `caption`; it skips `text` for
  patchable types. It never touches `runs`, `src`, `href`, `type`.
- **Each engine defines its OWN local `_PATCHED_TEXT_BLOCK_TYPES = ("lead","paragraph","heading")`
  — WITHOUT `list_item`** (claude 159, gemini 132, openai 129, openrouter 217), diverging from the
  shared 4-tuple at `_llm_common.py:115`. Consequence: a `list_item`'s already-Russian `text` is
  re-sent to the second-pass translator (wasted tokens, possible RU degradation).

**Cost note:** the variant-B branch fires a SECOND LLM call (`_translate_block_strings`) per
article whenever `blocks` is present and the model returns no usable `blocks`. Giving three more
sources `blocks` puts them on that path.

### 5.5 CRITICAL trace — no `blocks`, LLM emits `**` anyway

Reproduced live in this repo:

```python
>>> _llm_common._build_user_message({'title':'t','subtitle':'s',
...     'paragraphs':['Hello **bold** x'],'source_name':'t_hunted'})
'{"source_name": "t_hunted", "title": "t", "subtitle": "s", "paragraphs": ["Hello **bold** x"]}'

>>> telegraph_publisher.preview_nodes(title='T', paragraphs=['Это **жирный** текст'],
...     images=[], source_url='https://x.com/a', subtitle='', blocks=None)
[{'tag': 'p', 'children': ['Это **жирный** текст']}, {'tag': 'p', 'children': ['Источник: ', …]}]
```

The exact path:

1. `article` has no `blocks` → `blocks_in = None`, `expected_block_count = None`
   (claude 372–375 / gemini 303–306 / openai 305–308 / openrouter 405–408).
2. `_build_user_message` gate at **`_llm_common.py:223`** is False → paragraphs sent raw.
   But `_build_system_prompt` (123–125) still appends the `**bold**` section (101–108) → **root cause**.
3. `_parse_response` (284–393) type/count/Cyrillic-checks only. **No text normalisation.**
   Markers pass into `list(paragraphs)` at **391**.
4. `_apply_emoji_safety_net` (396–427) — title only. `_truncate_paragraphs` (430–445) — slicing only.
5. Variant-B guard is False (`expected_block_count is None`) → `_patch_text_with_ru_paragraphs`
   never runs → `_decode_format_markers` **never invoked**.
6. Engine returns (claude 450 / gemini 379 / openai 384 / openrouter 494);
   `llm_transcreation.py:112` re-exports verbatim, no post-processing.
7. `news_bot.py:3082` `ru_paragraphs = list(claude_result.get('paragraphs') or [])`; `ru_blocks = None` (3083).
8. `news_bot.py:3148–3165`: `_strip_plugs` (1236–1252) is regex-substitution + whitespace collapse;
   `is_boilerplate` is a line filter. **Neither knows about `**`.**
9. `news_bot.py:3215–3223` → `publish_article(..., blocks=None)` → `preview_nodes` (453–495) →
   branch at **488** `if blocks:` is False → `_build_content` (400–450) →
   **439–440 `nodes.append(p(para))`** emits the raw string as a Telegraph text node.
10. Also persisted raw to SQLite at `news_bot.py:3252–3258` (`update_staged`).

**No code path anywhere strips or decodes `**` on the no-blocks path.** Literal asterisks reach
both the published Telegraph page and the DB.

Every place a fix could sit:

| Location | File:lines | Nature |
|---|---|---|
| Shared parse post-pass | `_llm_common.py` between 373 and 375, or the return literal at 391 | Single-point; strip/flatten markers when `expected_block_count is None` |
| New helper next to the decoder | `_llm_common.py:207` | e.g. `_strip_format_markers`, reusing `_BOLD_MARKER_RE` (184) |
| Per-engine, after `_truncate_paragraphs`, before the variant-B guard | claude 434↔438, gemini 365↔369, openai 368↔372, openrouter 467↔475 | 4 edits |
| Prompt-side | `_llm_common.py:101–108` + `_build_system_prompt` 123–125 | Make the marker section conditional on the article carrying runs |
| Render backstop (flat) | `telegraph_publisher.py:439–440` in `_build_content` | Catches markers from any source |
| news_bot post-LLM | `news_bot.py:3082–3083` (right after `ru_paragraphs` is read) | Outside the LLM modules |

Note the same leak exists in theory on the **blocks** path if a model ever returns a `blocks` list
of matching length (variant-B guard skipped → no decode); in practice the envelope forbids it, and
there the markered `paragraphs` are ignored by the renderer but still written to the DB.

### 5.6 Where a real Telegraph heading comes from

`telegraph_publisher._build_content_from_blocks` (295–386) → `heading(level, *children)` (333–335):
`lvl = level if level in (3, 4) else 3`, emits `{"tag": "h3"|"h4"}`. So a
`{'type':'heading','level':3,'text':…,'runs':…}` block is the only way to get a real heading —
there is **no path from a flat paragraph string to `<h3>`**. `_build_content` (400–450) emits only
`<p>`, `<figure>`, `<hr>`, `<i>` and the footer.

---

## 6. Existing Tests

Runner: plain `pytest`, **no config file at all** (no `pytest.ini` / `setup.cfg` /
`pyproject.toml` / `tox.ini`). `tests/conftest.py` (12 lines) only does
`sys.path.insert(0, <repo root>)` — no fixtures, no autouse hooks, no DB setup.
CI: `.github/workflows/ci.yml:42` → `python -m pytest tests/ -v` on Python 3.13.
Pre-commit runs gitleaks + whitespace hygiene only — no linter, no type checker, no test hook.
Style is mixed: pytest classes with bare `assert` (the parser files) and `unittest.TestCase`
(`test_llm_common.py`, `test_integration.py`, `test_mattel_integration.py`).

### 6.1 Counts

| File | Tests |
|---|---|
| `tests/test_t_hunted_source.py` | 17 |
| `tests/test_lamley_source.py` | 26 |
| `tests/test_mattel_news_source.py` | 42 |
| `tests/test_telegraph_publisher.py` | 69 |
| `tests/test_orangetrack_source.py` | 94 |
| `tests/test_autoevolution_source.py` | 21 |
| `tests/test_preview_renderer.py` | 48 |
| `tests/test_boilerplate_filter.py` | 60 |
| `tests/test_llm_common.py` | 21 |
| `tests/test_integration.py` | 114 |
| `tests/test_mattel_integration.py` | 3 |
| `tests/test_pending_articles_repo.py` | 104 |
| `tests/test_hw_review_cli.py` | 46 |
| `tests/test_hw_review_publish_flow.py` | 15 |
| `tests/test_job_prep_phase.py` | 10 |
| `tests/test_fallback_publish_paths.py` | 9 |
| `tests/test_sources_registry.py` | 24 |

**No test anywhere asserts the exact key-set of the dict returned by the three parsers, and no
test asserts `"blocks" not in result`.** Adding a `blocks` key is additively safe at the
dict-shape level.

### 6.2 Assertions at risk — exact list equality

| File:line | Test | Assertion |
|---|---|---|
| `test_t_hunted_source.py:302–304` | `test_single_paragraph_post_keeps_paragraph_in_body_with_empty_subtitle` | `out["paragraphs"] == ["A loja Universo Hot Wheels recebeu mais um set incrível."]` — the `len(paragraphs) >= 2` guard test |
| `test_t_hunted_source.py:336` | `test_boilerplate_filter_applied_before_subtitle_lift` | `out["paragraphs"] == ["Second content paragraph."]` — comment says *"any extra entry or reordering by filter/lift fails"* |
| `test_mattel_news_source.py:373` | `test_parses_paragraphs_and_uses_thumbnail_only` | `out["paragraphs"] == ["A paragraph.", "Second.", "Bullet"]` — fixture has `<ul><li>Bullet</li></ul>` |
| `test_mattel_news_source.py:503` | `test_body_split_across_multiple_flight_chunks` | `out["paragraphs"] == ["Alpha.", "Beta.", "Gamma."]` |
| `test_mattel_news_source.py:521` / `:542` | `test_body_absent_…` / `test_body_truncated_…` | `out["paragraphs"] == []` — forces the `blocks=[]` vs `blocks=None` decision |
| `test_mattel_news_source.py:560` | `test_article_falls_back_to_url_field_when_handle_mismatch` | `out["paragraphs"] == ["x"]` |

### 6.3 Assertions at risk — the subtitle lift

| File:line | Assertion | Pins |
|---|---|---|
| `test_t_hunted_source.py:105–107` | `out["subtitle"] == "Mais um lançamento…"` | conditional lift fires with 2+ paragraphs |
| `test_t_hunted_source.py:109–112` | `"Mais um lançamento…" not in out["paragraphs"]` | lifted lead is NOT duplicated in the body |
| `test_t_hunted_source.py:300` | `out["subtitle"] == ""` | guard suppresses lift at 1 paragraph |
| `test_lamley_source.py:78` | `out["subtitle"] == "First paragraph of the post."` | unconditional lift |
| `test_lamley_source.py:79` | `"First paragraph of the post." not in out["paragraphs"]` | same |
| `test_mattel_news_source.py:372, 404, 417` | subtitle from `excerpt` | Mattel is already 1:1 aligned |

### 6.4 Assertions at risk — flattened `<li>` / `<h3>`

| File:line | Assertion | Decision it forces |
|---|---|---|
| `test_lamley_source.py:81` | `"Bullet one" in out["paragraphs"]` | is a lamley `<li>` a `list_item` block or a `paragraph` block? |
| `test_lamley_source.py:82` | `"A heading" in out["paragraphs"]` | is a lamley `<h3>` a `heading` block? |
| `test_mattel_news_source.py:373` | `"Bullet"` is the 3rd flat entry | same, for mattel |

Note: orangetrack's flat list deliberately includes `heading` and `list_item` text
(`orangetrack_source.py:792–795`, with a 12-line comment at 780–791 explaining why omitting them
would starve `_patch_text_with_ru_paragraphs`). Any blocks build for these sources must keep the
same three types in the flat list.

### 6.5 The alignment invariant is already an explicit test

`tests/test_orangetrack_source.py:105–130` — `test_standard_post_with_paragraphs_and_image`:

```python
# Subtitle is empty by design — preserves alignment between `paragraphs` and
# the paragraph-type entries in `blocks` … See SESSION-2026-05-06.md.
assert out["subtitle"] == ""
...
block_paragraph_count = sum(1 for t in types if t == "paragraph")
assert len(out["paragraphs"]) == block_paragraph_count
```

Repeated at 162–165 for the h5 case. This is the invariant t-hunted/lamley's subtitle lift breaks
(§7.1).

### 6.6 Rendering tests (`test_telegraph_publisher.py`, 69 tests)

- `TestInlineFormats` (686–828, 12 tests) — exactly what a `runs`-emitting parser feeds:
  `test_bold_run_wraps_in_strong` (692), italic (702), underline (712), strikethrough (722),
  `test_combined_bold_italic_outer_strong_inner_i` (732), href+bold drops the link keeps the
  format (746, 763), unknown format ignored (789), run text not found → dropped (799),
  overlapping first-wins (806), empty formats skipped (821).
- `TestHeadingRendering` (894–916): `test_heading_block_renders_h3` (897) asserts
  `nodes == [{"tag": "h3", "children": ["Section"]}]`.
- `TestListItemRendering` (863–893): bullet prefix (866), href run → plain text (871),
  leading-bullet strip (887).
- `TestBuildContentFromBlocks` (137–200): `test_runs_are_metadata_not_rendered_inline` (168) —
  href-carrying runs are metadata only; `test_block_order_preserved_except_hero_promotion` (186)
  asserts `tags == ["figure", "p", "p", "figure"]`.
- `TestDoSBounds` (918–944): `_MAX_TEXT_FOR_RUNS` (921), `_MAX_RUNS_PER_BLOCK` (932).
- `TestPreviewNodes` (424–629) — pins the `if blocks:` branch:
  `test_flat_path_matches_build_content` (440), `test_blocks_path_matches_build_content_from_blocks`
  (455), **`test_empty_blocks_falls_back_to_flat` (543)**, parity tests (557, 577).
  `test_images_interleaved_every_third_paragraph` (104) pins the flat-path image rhythm that the
  blocks path abandons.

### 6.7 `test_preview_renderer.py` (48 tests)

**Zero references to `blocks` / `runs` / `paragraphs`** — it tests node-tree → HTML only.
`test_renders_allowed_container_tags` (57–62) is parametrised over
`["p", "figure", "figcaption", "h3", "h4", "i", "b"]` — **`strong`, `u`, `s` are absent from both
the parametrisation and the module's `_ALLOWED_TAGS`** (see §7.2).

### 6.8 `test_llm_common.py` (21 tests)

- `TestEncodeFormatMarkers` (27–78): 6 tests, incl. `test_non_bold_runs_are_ignored` (53) — pins
  that italic/underline are dropped across translation.
- `TestDecodeFormatMarkers` (80–127): 5 tests, incl. `test_round_trip_through_encode_and_decode` (114).
- `TestBuildUserMessageInlineFormatting` (129–200): **`test_no_blocks_passes_paragraphs_through`
  (134)** is the test that documents today's t-hunted/lamley/mattel behaviour at the LLM boundary.
  Stays green (it builds its own dict) but marks exactly what changes.
- `TestPatchTextDecodesBoldRuns` (201–264): 3 tests, incl. `test_ru_without_marker_drops_runs` (224).
- `TestSanityFloorRelaxation` (265–329): all four calls pass `expected_block_count=None`
  (280, 293, 305, 324); the docstring (3–7) cites the **t-hunted single-paragraph photo-gallery
  incident** as the reason the 30-char floor is skipped for 1-paragraph articles.

### 6.9 `test_boilerplate_filter.py` — the template for what to add

- `TestLamleyIntegration::test_share_paragraphs_stripped` (675–703) — asserts on `out["paragraphs"]` only.
- `TestMattelIntegration::test_share_paragraphs_stripped` (711–745) — same.
- **`TestAutoevolutionIntegration::test_share_paragraphs_stripped_from_paragraphs_and_blocks`
  (753–795)** — the model to copy: asserts the flat list AND
  `block_texts = [b.get("text","") for b in out["blocks"]]` (792).
- `TestFilterBlocks` (806–880, 7 tests).
- **No t-hunted class exists in this file.**

Import reality check: `filter_blocks` is imported today only by `autoevolution_source.py:26` and
`orangetrack_source.py:33`. `t_hunted_source.py:35`, `lamley_source.py:26` and
`mattel_news_source.py:36` import `filter_boilerplate` only.

### 6.10 Downstream tests that flip behaviour once EN rows carry `blocks`

| File:line | Test | Effect |
|---|---|---|
| `test_hw_review_cli.py:375` | `test_stage_rejects_ru_blocks_null_when_en_has_blocks` | asserts `'ru_blocks required'` — t-hunted/lamley/mattel rows would now hit this |
| `test_hw_review_cli.py:389` | `test_stage_rejects_ru_blocks_present_when_en_null` | the mirror |
| `test_hw_review_cli.py:328, 339, 351` | non-dict / unknown type / unknown key rejection | `runs` is an "unknown key" |
| `test_pending_articles_repo.py:276–287` | `test_blocks_empty_list_vs_null_distinguished` | `None`→NULL→`None`; `[]`→`'[]'`→`[]` |
| `test_pending_articles_repo.py:230, 237, 836, 858, 886, 928` | blocks round-trip, failed_articles, legacy raw-SQL rows | schema-level, unaffected |
| `test_job_prep_phase.py:331–333` | `test_empty_paragraphs_skipped` | the gate the t-hunted `>= 2` guard exists to avoid |

### 6.11 Integration fixtures (all mock the parser — they stay green, but they are the inventory)

- `test_integration.py`: `_seed_pending_row` (511–527) **already takes `blocks=None`**;
  `_seed_published` (1112–1114); `TestContentGateIntake` (4785–5150) is t-hunted-heavy with
  `paragraphs`-only fake articles; `TestResolveHoldCallback::_seed_held` (4128–4140) and
  `TestReviewCallbackGrammarsCoexist` (4445) use `'source_name': 't-hunted'` with `'blocks': None`;
  ~21 CrossSourceDedup fixtures with `paragraphs` and no `blocks`; `:339` asserts
  `row['paragraphs'] == ['First paragraph.', 'Second paragraph.']`.
- `test_mattel_integration.py` (3 tests): all build fake dicts with **no `blocks` key**
  (115–117, 186–187). Safe — `news_bot` reads `article.get('blocks')` (3522, 4110).
- `test_fallback_publish_paths.py`: `_seed_pending` (49–61) and `_llm_result` (67–76) already take
  `blocks=None`; `:187` asserts `kwargs['paragraphs'] == ['RU one.', 'RU two.']`.

---

## 7. Risks

### 7.1 Off-by-one between `paragraphs` and `blocks` — the subtitle lift (HIGH)

`t_hunted` (216–220) and `lamley` (400–401) lift `paragraphs[0]` into `subtitle`, leaving
`len(paragraphs) == K-1` while a naive `blocks` build would have `K` patchable blocks.

Both directions break:

- **Encode**: `_build_user_message` (229–239) pairs block *i* with paragraph *i*. With a one-off
  misalignment, `_encode_format_markers` receives the wrong paragraph; `text.find(run_text)`
  (159) then usually fails → markers silently dropped, occasionally wrong span bolded.
- **Decode**: `_patch_text_with_ru_paragraphs` (468–489) consumes `ru_paragraphs` sequentially per
  patchable block. Short by one ⇒ `StopIteration` at 488 ⇒ **the last block keeps its English text**
  and gets published in English.

This is the exact incident orangetrack hit; the fix there was to hardcode `subtitle = ""`
(`orangetrack_source.py:808`, with a 10-line comment at 798–807 pointing at SESSION-2026-05-06).
autoevolution avoids it a different way — its subtitle comes from a separate DOM node
(`div.mgtop_10.mgbot_10.fsz19`, 180–185), never from `paragraphs`.

So for t-hunted/lamley there is no free lunch: keep the lift and the flat list must be derived
*after* it in a way that stays in lockstep with the patchable-block count, or drop the lift and
lose the `💬 «…»` lead decoration.

### 7.2 `preview_renderer` drops `<strong>` — and its children (HIGH, pre-existing)

`preview_renderer._ALLOWED_TAGS` (49–52) is
`{p, figure, img, figcaption, iframe, h3, h4, hr, i, b, a}` — **`strong`, `u`, `s` are absent**.

`telegraph_publisher._FORMAT_TAGS` (163–168) maps `bold → "strong"`, `underline → "u"`,
`strikethrough → "s"`.

`_render_node` (130–156) returns `""` for an unknown tag **and does not render its children**
(deliberate, comment 137–140). Verified:

```
blocks: [{'type':'paragraph','text':'Hello bold world',
          'runs':[{'text':'bold','formats':['bold']}]}]
nodes:  [{'tag':'p','children':['Hello ', {'tag':'strong','children':['bold']}, ' world']}]
html:   <p>Hello  world</p>          ← the word "bold" is DELETED
```

This already affects orangetrack/autoevolution previews today. The docstring at
`preview_renderer.py:13–17` claims the allowlist "mirrors what `_build_content*` is allowed to
emit" — it no longer does. The comment at 47–48 (*"Must stay in lock-step with
`telegraph_publisher._build_content*`"*) is stale.

Telegra.ph's own API accepts `strong` — this is a preview-only defect. But any preview-based
verification of the new bold rendering will show the words missing.

### 7.3 `hw_review` staging cannot express `runs` or `list_item` (MEDIUM)

`hw_review._VALID_BLOCK_TYPES` (104) = `{paragraph, lead, heading, image, video}` — **no
`list_item`**.
`_BLOCK_KEYS_BY_TYPE` (105–111):

```python
'paragraph': {'type','text'}
'lead':      {'type','text'}
'heading':   {'type','text','level'}      # level must be 3 or 4 (315–316)
'image':     {'type','src','caption'}
'video':     {'type','src','caption'}
```

`_validate_block` (300–320) rejects any extra key (307–309) — so **a staged `ru_blocks` entry
carrying `runs` is rejected outright**.

And the parity gate at `cmd_stage` (396–404):

```python
if en_has_blocks and not ru_has_blocks:  reject 'ru_blocks required (EN has blocks)'
if ru_has_blocks and not en_has_blocks:  reject 'ru_blocks must be null (EN has no blocks)'
```

Consequence: the moment t-hunted/lamley/mattel rows carry `blocks`, `hw_review stage N` for those
rows **requires** `ru_blocks`, and those `ru_blocks` can never carry formatting. Existing operator
muscle memory (`"ru_blocks": null`) starts failing for these sources.

Mitigating fact: `architecture.md` and `ux-guidelines.md:11` record the manual `hw_review` path as
**archived since 2026-04-30** — 100 % of channel posts go through `_fallback_publish`. The CLI is
kept green by tests but is not the production path. Also note `hw_review._validate_block` already
rejects autoevolution's `level=2` headings, so this gate is already out of sync with reality.

### 7.4 Boilerplate filter ordering in t-hunted (MEDIUM)

`t_hunted_source.py:201–204` marks the order **CRITICAL**: `filter_boilerplate` must run before the
subtitle lift, or a Blogger footer (`"Marcadores: …"`) at the top of a title-only post floats into
`subtitle`. Pinned by `tests/test_t_hunted_source.py::test_boilerplate_filter_applied_before_subtitle_lift`
(line 309), which asserts `out["paragraphs"] == ["Second content paragraph."]` exactly (line 336).

orangetrack runs a **double** filter — `filter_blocks(blocks)` (778) then `filter_boilerplate` on
the derived flat list (796). Any blocks-based rewrite of t-hunted must keep both the block filter
and the flat filter in the pre-lift position.

### 7.5 DoS bounds in `_render_paragraph_with_runs` (LOW–MEDIUM)

`telegraph_publisher.py:156–157`: `_MAX_TEXT_FOR_RUNS = 100_000`, `_MAX_RUNS_PER_BLOCK = 100`.
Exceeding either logs one WARNING and falls through to `[text]` — formatting silently disappears
for that block (218–226). Per-block, so a normal paragraph is nowhere near 100 runs; a t-hunted
new-arrival post with a long `<br>`-separated model list inside one `<p>` could approach it if
segmentation is not applied.

Related: `_encode_format_markers` (`_llm_common.py:128–177`) has **no equivalent bound** — it
iterates all runs and does a `text.find` per run.

### 7.6 Silent output change for the two sources that already work

Things that would change published output for orangetrack/autoevolution if the "reuse the existing
machinery" refactor touches shared code:

| Change | Blast radius |
|---|---|
| Editing `orangetrack._runs_from_tag` (284–412) to make it importable/generic | orangetrack is the ONLY consumer; 94 tests in `test_orangetrack_source.py` |
| Editing `telegraph_publisher._FORMAT_TAGS` / `_wrap_with_formats` (163–185) | both live sources; 69 tests in `test_telegraph_publisher.py` |
| Editing `_llm_common._build_user_message` / `_patch_text_with_ru_paragraphs` | **all five sources + all four engines** |
| Adding `list_item` to the engines' local `_PATCHED_TEXT_BLOCK_TYPES` | orangetrack list posts (would stop double-translating `list_item.text`) |
| Editing `preview_renderer._ALLOWED_TAGS` (49–52) | preview only; 48 tests in `test_preview_renderer.py`, several byte-compare the CSP meta (76–81) |
| Adding `filter_blocks` before `filter_boilerplate` in the three parsers | changes which paragraphs survive → changes `_is_promo_article` / `_is_text_only_checklist` / fingerprint inputs |

### 7.7 Behavioural changes inherent to the flat→blocks switch

Even with zero shared-code edits, converting a source to `blocks` flips it from
`_build_content` (400–450) to `_build_content_from_blocks` (295–386):

| Aspect | flat path today | blocks path |
|---|---|---|
| Image placement | first image = hero, then one image every 3rd paragraph (441–442), remainder appended (444–445) | images render at their **DOM position** (378–379); first image block = hero (337–345) |
| Image cap | `_IMAGE_LIMIT` applied in the parser (t-hunted 30, lamley 10) | `blocks` carries **all** image blocks; `images` list is ignored entirely |
| Captions | none | `figcaption` from `<figure>` (326) |
| Videos | none | `<iframe>` blocks possible |
| Headings | rendered as ordinary `<p>` | real `<h3>`/`<h4>` |
| Nested-tag duplicates | present (§3.4) | absent |

For t-hunted's photo-gallery format (up to 30 images) this is a **visible layout change** for every
post — images would land wherever the Blogger DOM puts them rather than in the every-3rd-paragraph
rhythm.

### 7.8 Other inconsistencies found (not blockers, but they are landmines)

| # | Issue | Location |
|---|---|---|
| 1 | `_strip_plugs_in_blocks` drops empty blocks for `('paragraph','lead','heading')` but the RU boilerplate filter 40 lines later uses `(...,'list_item')` | `news_bot.py:1276` vs `3172` |
| 2 | Engines' local `_PATCHED_TEXT_BLOCK_TYPES` (3 types) ≠ shared (4 types) | claude 159, gemini 132, openai 129, openrouter 217 vs `_llm_common.py:115` |
| 3 | `_build_content_from_blocks` ignores `runs` for `lead` blocks | `telegraph_publisher.py:365` |
| 4 | Inline `<a href>` runs are collected by both parsers but **never rendered** (`href_val = None`) | `telegraph_publisher.py:245`, product decision 2026-05-13 (comment 230–237) |
| 5 | `hw_review._validate_block` requires `heading.level in (3,4)`; autoevolution emits `level=2` | `hw_review.py:315–316` vs `autoevolution_source.py:298` |
| 6 | `autoevolution._runs_from_tag` has no `formats` support at all — autoevolution bold is already lost | `autoevolution_source.py:64–103` |
| 7 | `preview_renderer` docstring/comment claim lock-step with the publisher; they diverged | `preview_renderer.py:13–17, 47–48` |

### 7.9 `_runs_from_tag` bleeds the format onto the preceding plain text (HIGH, pre-existing, live)

`orangetrack_source.py:383–400`:

```python
fmt = _INLINE_FORMAT_TAGS.get(name)
...
if fmt:
    fmt_stack.append(fmt)      # ← 387: push FIRST
    pushed.append(fmt)
...
if pushed:
    flush()                    # ← 396: flush AFTER the push
    walk(child)
    flush()
```

`flush()` (312–324) reads `current_formats()` off the already-pushed `fmt_stack`, so the pending
plain-text buffer inherits the format it was supposed to be flushed clear of. Verified live:

```
input:  <p>Plain <strong>bold</strong> tail.</p>
runs:   [{'text': 'Plain ', 'formats': ['bold']},     ← WRONG, should have no formats
         {'text': 'bold',   'formats': ['bold']},
         {'text': ' tail.'}]
telegraph nodes: [{"tag":"strong","children":["Plain "]}, " ",
                  {"tag":"strong","children":["bold"]}, "  tail."]
```

Every word before the first bold span in an orangetrack paragraph is published bold today.
Any reuse of this walker propagates the defect to three more sources. Fix is a one-line reorder
(flush before push).

### 7.10 Block `text` derived from runs ≠ `get_text(" ", strip=True)` (MEDIUM)

orangetrack derives block text as `" ".join(r["text"] for r in runs).strip()`
(`orangetrack_source.py:546`, 597, 758) and applies `re.sub(r"\s+", " ", …)` **only** in the
`<br>`-split branch (575), not in the plain branch. Because each run keeps its own leading/trailing
whitespace, the join inserts a second space at every run boundary:

```
get_text(" ", strip=True) → 'Plain bold tail.'
" ".join(run texts)       → 'Plain  bold  tail.'      ← double spaces
```

The three flat parsers all use `get_text(" ", strip=True)` today (`t_hunted_source.py:197`,
`lamley_source.py:389`, `mattel_news_source.py:365`). Copying the orangetrack pattern verbatim
would silently change production flat text for every paragraph containing inline markup — while
the existing exact-equality tests (§6.2) stay green, because none of their fixtures contain
inline markup.

Second-order effect: `_render_paragraph_with_runs` locates runs via `text.find(run_text)`
(`telegraph_publisher.py:253`), so `text` and `runs` must stay byte-consistent. Collapsing
whitespace in `text` without collapsing it in the runs breaks the lookup and silently drops all
formatting for that block.

---

## 8. Shared Utilities

| Utility | Location | What it does |
|---|---|---|
| `filter_boilerplate(paragraphs) -> list[str]` | `boilerplate_filter.py:270–272` | Drops UI-label paragraphs, order preserved |
| `is_boilerplate(text) -> bool` | `boilerplate_filter.py:240–267` | Long-form patterns first (uncapped, 54–77), then short-form under `_MAX_BOILERPLATE_LEN = 120` (37) |
| `filter_blocks(blocks) -> list[dict]` | `boilerplate_filter.py:275–319` | Block-level mirror. Media blocks (`src`/`image_url`) are kept unless caption is boilerplate AND `text` is empty (305–309). Extra keys like `runs` pass through untouched |
| `_safe_href` / `_safe_img_src` | `orangetrack_source.py:208–248` | Scheme allowlists (`http`/`https`/`mailto`; `http`/`https`) |
| `_is_blogger_image_url` | `t_hunted_source.py:76–88`, `lamley_source.py:238–250` | **Duplicated verbatim** in both — Blogger CDN host suffix check |
| `_notify(notifier, message)` | `t_hunted_source.py:91–103`, `lamley_source.py:214–221`, `mattel_news_source.py:376–383` | **Triplicated** — log at ERROR + best-effort notifier call |
| `_render_paragraph_with_runs(text, runs, source_url)` | `telegraph_publisher.py:188–292` | runs → Telegraph children; first-wrap-wins, DoS-bounded |
| `_wrap_with_formats(child_text, formats)` | `telegraph_publisher.py:171–185` | Nests format tags outer→inner per `_FORMAT_TAGS` |
| `_encode_format_markers` / `_decode_format_markers` | `_llm_common.py:128–177` / `187–207` | runs ⇄ `**bold**` |
| `_patch_text_with_ru_paragraphs(blocks_in, ru_paragraphs)` | `_llm_common.py:448–491` | Splices RU text into EN blocks, decodes markers into `runs`, deletes stale EN runs (484–487) |
| `admin_alerts.alert_t_hunted_*` / `alert_lamley_*` / `alert_mattel_*` | `admin_alerts.py` | E031–E033 and friends |

---

## 9. Constraints & Infrastructure

- **Python / deps:** `requirements.txt` + `requirements-dev.txt`; BeautifulSoup4 (`html.parser`
  everywhere — no lxml), `requests`, `curl_cffi` (optional, lamley + autoevolution Cloudflare
  bypass), `feedparser`.
- **Tests:** plain `pytest` from repo root. **No `pytest.ini` / `setup.cfg` / `pyproject.toml` /
  `tox.ini`.** `tests/conftest.py` (12 lines) only inserts the repo root on `sys.path`.
  Mixed style: `unittest.TestCase` classes and bare pytest functions coexist.
- **Deploy:** Docker on the Moscow prod host, branch `main`, `git pull && docker compose up -d --build`.
  `tests/test_deploy_files_invariant.py` (84 lines) pins which files ship — a **new module** added
  for shared walker code must be checked against it.
- **Publish window:** 10:00–20:00 МСК (slots 10:00 / 15:00 / 19:30). Deploys restart the in-process
  schedule, so promotion must happen outside the window.
- **Telegraph API:** accepts `strong`, `i`, `u`, `s`, `a`, `p`, `h3`, `h4`, `figure`, `img`,
  `figcaption`, `iframe`, `hr`. `iframe.src` must be a `https://telegra.ph/embed/<provider>?url=…`
  proxy URL or it is silently stripped (`telegraph_publisher` docstrings; `orangetrack._video_embed_url`
  138–169).
- **Env:** `TELEGRAPH_ACCESS_TOKEN`, `ANTHROPIC_API_KEY` / engine keys, `INSTANCE_LABEL`,
  `DEDUP_SERIES_ENABLED`, `REVIEW_BUTTONS_ENABLED`.
- **Rate limits:** lamley `_MIN_REQUEST_INTERVAL_S = 20.0` (`lamley_source.py:101`), 429 backoff,
  per-URL 24 h blacklist, 1 h process cool-down after 5 consecutive 429s (105–137).
  t-hunted `_MAX_BYTES = 2_000_000` (52), `_TIMEOUT_SECONDS = 15` (44).
  mattel `MAX_RESPONSE_SIZE = 5 MB` (48).
- **Docs to update on any contract change:**
  `.claude/skills/project-knowledge/references/architecture.md` — line **309** is the canonical
  block-type/`runs` contract; lines 232, 285, 304–305 also describe the pipeline.

---

## 10. External Libraries

No new external library is implicated. BeautifulSoup4 `html.parser` is already the sole HTML
backend in all five parsers; `Tag.children` / `Tag.find_all` / `NavigableString` are the only APIs
the walker uses. `Tag.find_all` recursion semantics are the source of §3.4.
