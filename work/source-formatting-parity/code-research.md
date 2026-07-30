# Code Research: source-formatting-parity

**Date:** 2026-07-28 · **deepened 2026-07-30** (see [Part II](#part-ii--updated-2026-07-30))
**Repo:** `/workspaces/debian-2/my-hw` (branch `dev`, HEAD `5b19f0d`)
**Scope:** bring `t_hunted_source.py`, `lamley_source.py`, `mattel_news_source.py` from flat
`paragraphs: list[str]` up to the `blocks` + `runs` contract already used by
`orangetrack_source.py` and `autoevolution_source.py`.

Facts and structure only — no design proposals.

> ### ⚠️ Read Part II first — Part I is partly stale
>
> Part I was written **before** the approved user-spec and before the 2026-07-28/29 fixes landed.
> Scope has narrowed and four Part-I findings are now resolved.
>
> | Part I section | Status as of 2026-07-30 |
> |---|---|
> | §1–§4 line numbers in `news_bot.py` | **SHIFTED +59** (`fetch_full_article` 3321 → **3380**). Other files unchanged. |
> | §3.3, §6.2 (mattel rows), §7.3 (mattel) | **OUT OF SCOPE** — user-spec drops mattel |
> | §5.5 (literal `**` leak) | **FIXED & OUT OF SCOPE** — `eaba4f6`, see II-0 |
> | §7.2 (`preview_renderer` deletes `<strong>`) | **FIXED** — verified live, II-0 |
> | §7.9 (bold bleeds onto preceding text) | **FIXED** — `a509722`, verified live, II-0 |
> | §7.10 (run-join double-spaces `text`) | **FIXED** — `_text_from_runs`, but the *replacement* diverges from `get_text` in a NEW way, II-6 |
> | §2.3 "bottom line" seam estimate | **SUPERSEDED** by the exhaustive AST map in II-1 |
> | §6.2/§6.3/§6.4 "assertions at risk" | **MOSTLY REFUTED** — full suite is green and stays green, II-8 |
> | §7.1 (off-by-one), §7.7 (image layout) | **CONFIRMED AND QUANTIFIED on real articles**, II-3 / II-4 |
> | §9 "new module must be checked against `test_deploy_files_invariant`" | **WRONG** — that test is blind to new files; prod is Docker `COPY . .`, II-8 |
> | everything else in §1–§10 | **re-verified, still accurate** |

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

---
---

# Part II — Updated: 2026-07-30

Implementation-level research for the **approved** user-spec. Scope: `t_hunted_source`,
`lamley_source`, `autoevolution_source` gain `blocks`+`runs`; the orangetrack walker is
**extracted** into a shared module that orangetrack then imports; orangetrack output stays
byte-identical. mattel excluded. Literal-`**` fix done.

Method note: **network is available from this workspace.** t-hunted (Blogger) and lamley
(WordPress, via `curl_cffi` impersonation) both fetch fine; **autoevolution article pages return
HTTP 403** (its RSS returns 200). So the measurements below use **14 real articles** — 10 t-hunted,
4 lamley — cached under
`/tmp/claude-1000/-workspaces-debian-2/45eede25-ca2d-4b98-a382-455e2a943626/scratchpad/{th,lam}/`,
with scripts `measure_th.py`, `measure_lam.py`, `lam_why.py`, `measure_heading.py`,
`heur_risk.py`, `inv_img.py`, `diff_th.py` in the same directory. autoevolution is covered from
test fixtures only.

Baseline: `python3 -m pytest tests/ -q` → **1628 passed, 441 subtests passed**, 60s. Suite green.

---

## II-0. Re-verification of the four "changed since Part I" items

| Claim | Verdict | Evidence |
|---|---|---|
| `_runs_from_tag` bold-bleed reordered, pre-flush gated on `opens_format` | **CONFIRMED** | `orangetrack_source.py:406-408` computes `opens_format`; `flush()` at **420**, *before* the pushes at 422/425. Live: `<p>Plain <strong>bold</strong> tail.</p>` → `[{'text':'Plain '}, {'text':'bold','formats':['bold']}, {'text':' tail.'}]` — the prefix run has **no** `formats` key. Part I §7.9 resolved. |
| `_text_from_runs` helper replaced four divergent joins | **CONFIRMED** | New module-level fn `orangetrack_source.py:270-283`, `re.sub(r"\s+"," ", "".join(...)).strip()`. Called at **577, 605, 628, 789** (paragraph plain / paragraph `<br>`-segment / heading / list_item). Part I §7.10's double-space defect is gone. **But see II-6 — the replacement diverges from `get_text(" ", strip=True)` in the opposite direction.** |
| `preview_renderer._ALLOWED_TAGS` gained `strong`/`u`/`s` | **CONFIRMED** | `preview_renderer.py:58-62`. Live end-to-end: a bold paragraph block renders `<p>Hello <strong>bold</strong> world</p>`. Part I §7.2 resolved. Note `render_html`'s real signature is `render_html(nodes: list, title: str)` — Part I §7.2 wrote it backwards. |
| `telegraph_publisher._decode_bold_markers` + `_BOLD_MARKER_RE`/`_STRAY_MARKER_RE` | **CONFIRMED** | Helper `telegraph_publisher.py:201-248`; regexes 195/198. Call sites: `_build_content_from_blocks` hero caption **409**, subtitle **413**, paragraph **426**, lead **431**, heading **433**, list_item **440**, image caption **446**; `_build_content` subtitle **504**, paragraph **511**; `publish_article` page title **612**. Part I §5.5 is closed — **out of scope**. |
| `model_extractor`: STH / RLC retagged `'broad'`, `_theme_only_eligible` added | **CONFIRMED** | `model_extractor.py:161-164` (`'broad'`), `224` `_RECURRING_PROGRAMS`, `244` `_theme_only_eligible`. No interaction with this feature. |
| `pending_articles_repo` `publish_after` column + predicate | **CONFIRMED** | `insert_pending` now inserts **12** columns (`pending_articles_repo.py:373-393`), `publish_after` at 391. Part I §4.2's round-trip conclusion is unaffected: `blocks` is still `_dumps(entry.get('blocks'))` at **387**, NULL-preserving. |
| `boilerplate_filter._LONG_BOILERPLATE_PATTERNS` shop-outros; `news_bot._PLUG_PATTERNS` sentence-level | **CONFIRMED** | `boilerplate_filter.py:54` (uncapped long patterns, bypass `_MAX_BOILERPLATE_LEN = 120` at 37); `news_bot.py:1195`. Relevant only as an input to II-6. |

---

## II-1. Extraction plan for the walker — exhaustive symbol map

### II-1.1 What `_runs_from_tag` depends on

`orangetrack_source.py:300-443`. AST-derived, complete:

| Symbol | Where | Nature | Verdict |
|---|---|---|---|
| `re` | stdlib | whitespace normalisation (439) | move |
| `List`, `Dict` | `typing` | annotations | move |
| `_INLINE_FORMAT_TAGS` | **259-267** | `strong/b→bold, em/i→italic, u→underline, s/del→strikethrough` | **move verbatim — pure generic** |
| `_safe_href` | **208-230** | scheme allowlist `_ALLOWED_HREF_SCHEMES` (79) | **move — pure, zero site knowledge** |
| `_has_color_class` | **286-297** | `any("-color" in c for c in node.get("class"))` → `"bold"` | **WordPress/Gutenberg-specific — must become injectable** |

Internal closures (`current_formats` 320, `flush` 328, `walk` 342, `collect` 356) and locals
(`runs`, `buf`, `fmt_stack`, `inner_buf`, `inner_fmts`) are self-contained — they move with the
function body, nothing to parameterise.

**`_has_color_class` is the only seam in `_runs_from_tag`.** Two call sites: **362** (inside
`collect`, for anchor children) and **400** (the main inline branch). Both feed the same
`color_fmt = "bold" if … else None`. Injecting it as a keyword parameter with a default of
"never colored" makes the function fully generic; passing orangetrack's version keeps orangetrack
byte-identical.

Note the color-class path is **not** WordPress-exclusive in practice: **lamley is WordPress too**
(`div.entry-content`, JetPack chrome — see II-6), so lamley likely wants the same predicate.
t-hunted (Blogger) and autoevolution do not.

### II-1.2 What `_walk` depends on

`_walk` is a **closure at `orangetrack_source.py:704-801`**, nested inside
`_parse_content_encoded` (527-861). It is not importable today. AST-derived free variables:

| Free variable | Defined at | Kind |
|---|---|---|
| `blocks` | **553** (`List[Dict] = []`) | **mutable accumulator, shared by all five emitters** |
| `handled_tags` | **670-680** | local `set` literal — *not* a module constant |
| `_has_chrome_class` | **695-702** | closure, reads `_CHROME_CLASS_MARKERS` |
| `_emit_paragraph` | **561-608** | closure → `blocks`, `_runs_from_tag`, `_text_from_runs`, `BeautifulSoup` |
| `_emit_heading` | **610-644** | closure → `blocks`, `_runs_from_tag`, `_text_from_runs` |
| `_emit_image` | **646-657** | closure → `blocks`, **`seen_image_bases`** (554), `_best_img_src` |
| `_emit_iframe` | **659-664** | closure → `blocks`, `_video_embed_url` |
| `_runs_from_tag`, `_text_from_runs` | 300 / 270 | module-level, used directly in the `li` branch (786, 789) |

Second-order module-level dependencies pulled in through the emitters:

| Symbol | Where | Site-specific? |
|---|---|---|
| `_CHROME_CLASS_MARKERS` | **690-693**, a LOCAL inside `_parse_content_encoded` | `sharedaddy, sd-, taxonomies, jp-related, post-comments, comment-form` — **WordPress/JetPack.** Needed by lamley too. |
| `_best_img_src` | **451-487** | srcset parser is generic; the "prefer largest ≤1024" pick (478-480) is tuned to WordPress.com's 300/600/1024 ladder |
| `_safe_img_src` + `_ALLOWED_IMG_SCHEMES` | **233-248**, 84 | pure |
| `_video_embed_url` | **138-169** | **orangetrack-specific**: `_YOUTUBE_HOSTS` (52-60) allowlist + `_YOUTUBE_ID_RE` (88-91) + `telegra.ph/embed/youtube` wrap. No Vimeo (autoevolution has its own at `autoevolution_source.py:106-124` *with* Vimeo and *without* a host allowlist) |
| `BeautifulSoup` | 31 | used at **594** to re-parse each `<br>` segment as a fresh `<p>` |
| `filter_blocks`, `filter_boilerplate`, `IMAGE_LIMIT` | 33, 73 | used in the **post-pass** (809/827/850), i.e. *after* `_walk`, not inside it |

### II-1.3 Which behaviours are orangetrack policy, not mechanism

These live in `_walk`/`_emit_*` and are **decisions**, pinned by orangetrack's own tests:

| Behaviour | Line | Pinned by |
|---|---|---|
| h2/h3/h4 all normalise to `level=3` | **631-637** | `test_orangetrack_source.py:1084-1095` |
| h5 → `type: "paragraph"` (carve-out `babc67c`) | **639-644**, dispatch 734-740 | `:145-164`, `:1097-1106` |
| h1/h6 dropped from body | **741-743** | `:1108-1120` |
| one `<p>` split into N paragraph blocks at each `<br>` | **583-608** | `:1028-1051` |
| `<li>` gets **no** bullet at parse time | **776-796** | `:975-1082` (`"•" not in block["text"]`) |
| `ul`/`ol` excluded from `handled_tags` so recursion reaches `<li>` | **670-680** | same |
| `<p>` holding only an iframe → video blocks, text suppressed | **715-720** | — |
| mixed `<p>`: text first, then nested iframes, then non-figure imgs | **721-729** | — |
| image dedup key = `src.split("?",1)[0]` | **650** | — |
| `subtitle = ""` hardcoded | **839** | `:115` |
| flat list = `paragraph|heading|list_item` text, DOM order | **823-826** | `:1122-1142` |

Directly conflicting with the three new sources:

- **level normalisation.** autoevolution keeps the real tag number (`int(tag.name[1])` at
  `autoevolution_source.py:297`, `312` → `level` can be **2**), pinned by
  `test_autoevolution_source.py:216-226`. orangetrack forces 3. A shared emitter cannot do both
  unconditionally.
- **image dedup key.** orangetrack uses `split("?")`; **t-hunted needs
  `_BLOGGER_SIZE_SUFFIX_RE.sub('', src.split("?",1)[0])`** (`t_hunted_source.py:64`, applied 244)
  because Blogger encodes size in the *path*. lamley uses plain `split("?")`
  (`lamley_source.py:420`) even though it has the same Blogger-lightbox shape.
- **image src selection.** t-hunted/lamley lift the wrapping `<a href>` when
  `_is_blogger_image_url(href)` (`t_hunted_source.py:239-243`, `lamley_source.py:415-419`);
  orangetrack's `_best_img_src` knows nothing about that.
- **video.** orangetrack: YouTube only, host-allowlisted. autoevolution: YouTube + Vimeo,
  regex-only.

### II-1.4 The minimal seam that exists in the code today

The mechanism/policy boundary that already exists, stated as facts:

1. **Fully portable as-is, zero parameters:** `_INLINE_FORMAT_TAGS` (259-267), `_safe_href`
   (208-230), `_safe_img_src` (233-248) + their two `frozenset`s (79, 84), `_text_from_runs`
   (270-283). ~55 lines.
2. **Portable with one injected predicate:** `_runs_from_tag` (300-443, 144 lines) — inject
   `_has_color_class`.
3. **Portable with the emitters injected:** `_walk` (704-801, 98 lines). Its body contains **no**
   site strings at all — every site-specific decision is reached through one of the six free
   names in the table above. The dispatch table `handled_tags` (670-680) is data.
4. **Not portable:** `_video_embed_url`, `_best_img_src`, `_CHROME_CLASS_MARKERS`, the
   `seen_image_bases` dedup key, the h5/h-level policy, `subtitle=""`, the video-only title
   synthesis (845-846), `IMAGE_LIMIT` (73).

The single hardest structural fact: **`blocks` and `seen_image_bases` are mutable state captured
by all five emitters plus `_walk`.** Any extraction must decide who owns that state; today it is
`_parse_content_encoded`'s frame.

---

## II-2. The heading heuristic

### II-2.1 Where each parser decides paragraph vs heading TODAY

| Parser | Decision site | What it does |
|---|---|---|
| **t_hunted_source** | **196** | `body.find_all(["p","li","h2","h3","h4","blockquote"])` → `get_text(" ", strip=True)`. **There is no heading branch at all.** An `<h2>` becomes an ordinary string in `paragraphs`, indistinguishable from prose. |
| **lamley_source** | **388** | byte-identical loop, same six tags, same absence of a branch |
| **autoevolution_source** | **290-300** and **305-314** | Two branches. (a) `<h2>/<h3>/<h4>` **nested inside a `<p>`** — detached and emitted before the paragraph's residual text (this is autoevolution's invalid-HTML workaround, comment 277-289). (b) top-level `<h2>/<h3>/<h4>` children of `div.newstext`. Both set `level = int(tag.name[1])`, so **level 2 is emitted**. |
| orangetrack (reference) | `_walk` **731-740** → `_emit_heading` **610-644** | h2/h3/h4 → `level=3`; h5 → `paragraph`; h1/h6 dropped |

**No bold-paragraph→heading heuristic exists anywhere in the repo.** Verified by grep across all
`.py` (excluding `tests/`, `archive/`): zero hits for a length threshold near a heading decision,
zero for `endswith('.')`.

### II-2.2 Real-tag headings must keep working — and they do exist

Measured on the 14 real articles:

| Source | real `<h2>/<h3>/<h4>` in body | `heading` blocks the walker produced |
|---|---|---|
| t-hunted (10 articles) | **0** in every article | 0 |
| lamley `lamley-awards-2025…` | 25 | **22** |
| lamley `…interview-bryan-zhao…` | 3 | **0** |
| lamley `quick-look-how-do-bburagos…` | 3 | **0** |
| lamley `recalibrating-alex-winsons…` | 3 | **0** |

The 3-vs-0 gap is not a bug: those three `<h>` tags sit inside JetPack chrome (`Share this:`,
`Like this:`, `Related`) that `_has_chrome_class` (695-702) discards. So **`_CHROME_CLASS_MARKERS`
is load-bearing for lamley**, not orangetrack trivia.

Consequence: **t-hunted has literally zero real heading tags** across 10 articles, so for t-hunted
the bold-paragraph rule is the *only* possible source of headings. lamley needs **both** paths.

### II-2.3 The rule measured against real data

Implemented as specified (whole paragraph bold + `len ≤ 80` + does not end with `.`), applied to
all **286 paragraph-type blocks** across the 14 articles:

**24 paragraphs would become headings.** Every one reads as a genuine subheading:

```
[t-hunted] len= 33 'Todo Hot Wheels antigo é valioso?'      ← the "?" case from AC2
[t-hunted] len= 37 'Treasure Hunts e Super Treasure Hunts'
[t-hunted] len= 12 'Referências:'                            ← bold label, benign
[lamley]   len= 42 'Highly commended: Hot Wheels Lotus Cortina'
[lamley]   len= 73 'Trax Ford Cortina GT (Bob Jane/Harry Firth, Armstrong 500, Bathurst 1963)'
… 19 more
```

**The user-spec's threshold evidence reproduces exactly for t-hunted.**
`o-que-faz-um-hot-wheels-aumentar-de.html`: **12 headings, 18–37 chars; 34 non-heading paragraphs,
81–207 chars.** Nothing between 38 and 80. (The user-spec says "44 обычных абзаца" — the actual
count is 34; 12 + 34 = 46 total paragraph blocks. The *length range* 81–207 is exact.)

**But the gap is t-hunted-only.** Across all 14 articles only **two** whole-bold paragraphs are
rejected, and both are false negatives:

| Rejected | Reason | Judgement |
|---|---|---|
| `'Lionel Racing NASCAR Authentics Ford Mustang Dark Horse, #12 Ryan Blaney, Team P…'` (**85 chars**) | `len > 80` | genuine lamley subheading, **missed by 5 chars** |
| `'Also ran….'` | ends with `.` | genuine lamley subheading, missed |

So on real data the rule scores **24 / 26 = 92 %**, with the longest accepted heading at **73
chars** (7 chars of headroom) and the shortest rejection at **85**. There are **38** non-whole-bold
paragraphs in the 38–80 char band, but none can be misclassified: they are not whole-bold. **Length
alone is never a false-positive risk; only whole-bold + length is.** Both errors are misses, not
false headings — matching user-spec Risk 7 ("ошибка косметическая и обратимая").

### II-2.4 What "whole paragraph bold" has to mean mechanically

Not "one run". `<p><strong>Part</strong> and <strong>two</strong></p>` yields
`[{'Part',bold}, {' and '}, {'two',bold}]` — three runs, the middle one plain. The predicate that
matched real data is: locate every bold run via `text.find(run_text)`, merge adjacent/overlapping
spans, require exactly **one** merged span, and require `text[:start].strip() == ""` and
`text[end:].strip() == ""`.

This needs `(text, runs)` — i.e. it can only run **after** `_runs_from_tag`. The only place in the
current architecture where both exist together and a block type is still being chosen is the
paragraph emitter, `orangetrack_source.py:561-608` (both the no-`<br>` branch at 573-581 and the
per-segment branch at 590-608 pick `{"type": "paragraph"}`).

**AC10 constraint:** orangetrack's tests pin its exact block-type sequences
(`test_orangetrack_source.py:145-164`, `1084-1095`, `1122-1142`). If the shared emitter applied the
rule unconditionally, any orangetrack paragraph that is entirely bold and short would flip
`paragraph`→`heading` and those tests would go red. So the rule has to be gated per source.
Whether orangetrack has such paragraphs today is unmeasured (its feed was not fetched) — the tests
are the binding constraint regardless.

---

## II-3. The subtitle-lift alignment

### II-3.1 Exact lift sites

| Parser | Lines | Code | Guard |
|---|---|---|---|
| `t_hunted_source.py` | **216-220** | `if len(paragraphs) >= 2: subtitle = paragraphs[0]; paragraphs = paragraphs[1:]` / `else: subtitle = ""` | conditional; rationale comment **207-215** |
| `lamley_source.py` | **400-401** | `subtitle = paragraphs[0] if paragraphs else ""` / `paragraphs = paragraphs[1:]` | **unconditional** |
| autoevolution | **180-185** | `subtitle` comes from a separate DOM node (`div.mgtop_10.mgbot_10.fsz19`) | never touches `paragraphs` — **no alignment problem** |

Both lifts run **after** `filter_boilerplate` (t-hunted **205**, lamley **395**) — the ordering
marked CRITICAL at `t_hunted_source.py:201-204` and pinned by
`tests/test_t_hunted_source.py:309` (asserts `out["paragraphs"] == ["Second content paragraph."]`
at **336**).

The `len(paragraphs) >= 2` guard exists because t-hunted's dominant format is a photo-gallery post
with one intro paragraph; lifting it would leave `paragraphs == []` and
`news_bot.py:3827` (`if not article or not article.get('paragraphs')`) would drop the post. Pinned
by `tests/test_t_hunted_source.py:300` (`out["subtitle"] == ""`) and `:302-304`.
Corroborated at the LLM layer by `tests/test_llm_common.py:3-7` + `TestSanityFloorRelaxation`
(265-329), whose docstring names this exact incident as the reason the 30-char sanity floor is
skipped for 1-paragraph articles.

### II-3.2 The invariant that must hold, and by how much it breaks

The consumer is positional in **both** directions:

- **encode** — `_llm_common._build_user_message` **223-241**: walks `blocks`, and for each block
  whose `type` is in `_PATCHED_TEXT_BLOCK_TYPES` takes `next(para_iter)`. Mismatch is *tolerated
  silently*: `except StopIteration: break` at **236-237**, then `marked.extend(para_iter)` at 240.
- **decode** — `_llm_common._patch_text_with_ru_paragraphs` **466-491**: same sequential
  consumption; short by one ⇒ `except StopIteration: pass` at **488-489** ⇒ **the trailing block
  keeps its English text and is published in English.**

`_PATCHED_TEXT_BLOCK_TYPES = ("lead","paragraph","heading","list_item")` — `_llm_common.py:115`.
So the invariant is:

```
count(b for b in blocks if b['type'] in ('lead','paragraph','heading','list_item'))
    == len(paragraphs)
```

**Measured** (script `inv_img.py`): building blocks with the orangetrack walker while keeping each
parser's lift exactly as written today:

| Source | articles | aligned | mismatch |
|---|---|---|---|
| t-hunted | 10 | **1** (`novidades-muito-interessantes-da-m2` — 1 paragraph, guard suppresses the lift) | 9, each off by exactly **1** |
| lamley | 4 | **0** | 4, each off by exactly **1** |

**13 of 14 real articles (93 %) break the invariant by exactly one.** This is not a corner case;
it is the default outcome. It matches the SESSION-2026-05-06 incident that made orangetrack
hardcode `subtitle = ""` (`orangetrack_source.py:839`, comment 829-838).

The photo-gallery single-paragraph case is the *only* naturally aligned shape, and only because
the guard fires.

Second, independent divergence source in t-hunted/lamley — the **title-dedup filter**:
`t_hunted_source.py:198` and `lamley_source.py:390` both skip a tag whose text equals `title`
(`if text and text != title`). The orangetrack walker has no such check. If a blocks build omits
it, a title-repeating `<p>` becomes a block with no matching flat entry. (Part I §7.4 framed the
two filter passes as the second divergence source; in fact `filter_blocks`
(`boilerplate_filter.py:318-362`) and `filter_boilerplate` (313-315) call the **same**
`is_boilerplate` on the same string for pure-text blocks, so for `paragraph`/`heading`/`list_item`
they agree — orangetrack's `filter_boilerplate` at **827** is effectively a no-op after
`filter_blocks` at **809**. The real second source is the title-dedup filter, and it is
parser-local.)

### II-3.3 Where the AC8 runtime guard can sit

Three candidate sites exist. Facts about each:

| # | Call site | Sees | Covers | Notes |
|---|---|---|---|---|
| **A** | end of each parser, immediately before the `return` dict — `t_hunted_source.py:252-257`, `lamley_source.py:428-433`, `autoevolution_source.py:359-365` | its own `blocks` + `paragraphs` | only that parser | 3 edits; `logger` already bound in all three (`t_hunted_source.py:37`, `lamley` module logger, `autoevolution` logger). Dropping `blocks` here means downstream never learns blocks existed. |
| **B** | `news_bot.py` row assembly, **4173-4193**, immediately before `pending_repo.insert_pending(row)` at **4195** | `article.get('blocks')` at **4181** and `article.get('paragraphs')` at **4179** for **every** source | all five sources incl. orangetrack | 1 edit, single choke point. `logger` in scope. This is the last point before the article is persisted; the `blocks` column is written by `insert_pending` at `pending_articles_repo.py:387`. |
| **C** | inside `pending_articles_repo.insert_pending`, **347-401** | the same dict | all sources | The repo's stated contract is "the repo owns all JSON serialisation" (`news_bot.py:3816`) — it currently contains **no** content validation, only a `sqlite3.IntegrityError` catch (396-399). Adding policy here would be new responsibility for that layer. |
| — | `_llm_common._build_user_message` **223** | `blocks` + `paragraphs` | all | **Cannot work as the guard**: it only builds the request string. Each engine independently reads `blocks_in = article.get("blocks")` and sets `expected_block_count = len(blocks_in)` (claude **375**, gemini **306**, openai **308**, openrouter **408**), then uses `blocks_in` at the variant-B branch (claude 439, gemini 370, openai 373, openrouter 480). Neutralising blocks here would not stop that. |

What the guard must do, per AC8: compare
`sum(1 for b in blocks if isinstance(b, dict) and b.get('type') in ('lead','paragraph','heading','list_item'))`
against `len(paragraphs)`; on inequality set `blocks` to `None` (**not** `[]` — the distinction is
live and tested: `tests/test_pending_articles_repo.py:276-287` `test_blocks_empty_list_vs_null_distinguished`,
and `telegraph_publisher.preview_nodes:562` branches on truthiness so `[]` already falls back to
the flat path, but `hw_review`'s parity gate at `hw_review.py:396-404` treats `[]` as "has blocks")
and `logger.warning(...)` with link + both counts. Fail-open is already the house pattern
(`news_bot.py:3853-3865` promo filter, `4165-4171` dedup bookkeeping).

Note site **B** would also start guarding orangetrack. orangetrack satisfies the invariant by
construction (flat list derived from blocks at **823-826**, `filter_boilerplate` a no-op after
`filter_blocks`, `subtitle=""`), and its tests assert it explicitly
(`test_orangetrack_source.py:124-130`, `162-165`) — so the guard would be inert there, which is
consistent with AC10 but means the guard cannot be *tested* through orangetrack.

---

## II-4. Image limits on the blocks path (AC9)

### II-4.1 PROVEN: non-empty `blocks` makes `images` entirely dead

The chain, line by line:

1. `news_bot._fallback_publish` **3274-3282** calls
   `telegraph_publisher.publish_article(title=…, paragraphs=ru_paragraphs, images=row.get('images') or [], …, blocks=ru_blocks)`.
2. `publish_article` **614-622** forwards everything to `preview_nodes`.
3. `preview_nodes` **562-569**:
   ```python
   if blocks:
       return _build_content_from_blocks(subtitle, blocks, source_url, auto_marker=auto_marker)
   return _build_content(subtitle, paragraphs or [], images or [], source_url, auto_marker=auto_marker)
   ```
   `images` is **not a parameter** of `_build_content_from_blocks` (signature **358-363**).
4. `_build_content_from_blocks` **400-454** emits one `figure_img` per `image` block: hero at
   **406-410**, the rest at **444-447**. **There is no cap, no slice, no counter.**

Docstring **592-593** states it outright: *"When provided, `paragraphs` and `images` are ignored."*
Pinned by `tests/test_telegraph_publisher.py:455` (blocks path) and `:543`
(`test_empty_blocks_falls_back_to_flat`). **Part I §7.7 / user-spec Risk 4 confirmed: today's caps
become dead code the moment these sources emit blocks.**

### II-4.2 Where the caps are applied today

| Parser | Constant | Where applied | Mechanism |
|---|---|---|---|
| `t_hunted_source.py` | `_IMAGE_LIMIT = 30` (**49**) | **249-250** | `if len(images) >= _IMAGE_LIMIT: break` — inside the `body.find_all("img")` loop, so it caps the *flat* list only |
| `lamley_source.py` | `IMAGE_LIMIT = 10` (**93**) | **425-426** | identical `break` |
| `autoevolution_source.py` | `MAX_IMAGES = 10` (**39**) | **357** | `images = [b["src"] for b in blocks if b["type"]=="image"][:MAX_IMAGES]` — slices the derived flat list, **the blocks keep every image** |
| orangetrack (reference) | `IMAGE_LIMIT = 10` (**73**) | **848-850** | same slice-the-flat-list-only shape |

So **all four sources already leak past their cap on the blocks path** — orangetrack and
autoevolution do it in production today. The three new sources would join them.

### II-4.3 How bad, measured

`image`-type block counts from the walker on the 14 real articles:

| Source | cap | image blocks per article | over cap? |
|---|---|---|---|
| t-hunted | 30 | 1, 1, 3, 6, 8, 10, 15, 22, 24, **27** | 0 / 10 (closest: 27) |
| lamley | 10 | **14, 41, 48, 50** | **4 / 4 — every article, 1.4× to 5×** |

**Every single lamley article measured exceeds its 10-image cap on the blocks path**, by up to 5×.
t-hunted stays under 30 in all 10 but one article reaches 27. AC9 is an immediate,
100 %-of-lamley-publications problem, not a theoretical one.

The minimal change consistent with the existing code: the cap has to be applied **to the block
list**, not to the derived flat list — i.e. after the walk, drop `image`-type blocks beyond the
Nth. Two facts constrain where: (a) `_build_content_from_blocks` promotes the **first** `image`
block to hero (**400-403**), so dropping from the tail preserves the hero; (b) `filter_blocks`
(`boilerplate_filter.py:318-362`) runs before the flat derivation in both block-emitting parsers
(orangetrack **809**, autoevolution **347**) and keeps media blocks unconditionally, so capping
must come after it or the count is wrong.

---

## II-5. The kill switch (AC11)

### II-5.1 How the two existing flags are parsed — there is no helper

Both are **inline module-level expressions in `news_bot.py`**. No shared parser exists anywhere in
the repo (verified by grep for `_env_flag` / `_env_bool` / `_truthy`: zero hits).

```python
# news_bot.py:134-136  — default ON, off-words
DEDUP_SERIES_ENABLED = os.getenv(
    "DEDUP_SERIES_ENABLED", "1"
).strip().lower() not in ("0", "false", "no", "off")

# news_bot.py:149-151  — default OFF, on-words (deliberately INVERTED)
REVIEW_BUTTONS_ENABLED = os.getenv(
    "REVIEW_BUTTONS_ENABLED", ""
).strip().lower() in ("1", "true", "yes", "on")
```

| | `DEDUP_SERIES_ENABLED` | `REVIEW_BUTTONS_ENABLED` |
|---|---|---|
| default | **ON** (`"1"`) | **OFF** (`""`) |
| word set | off-words `0/false/no/off` | on-words `1/true/yes/on` |
| unset / blank | enabled | disabled |
| unrecognised value (`"maybe"`) | **enabled** | **disabled** |
| read once at | import time | import time |
| const name == env name | **yes, deliberately** — comments 125-127 and 139-141 both flag a const↔env drift as making a "dark" deploy silently no-op | yes |

Gating style (documented at 130-133 and 141-144): call sites read the **bare module global**, never
re-read `os.getenv`, so `unittest.mock.patch('news_bot.DEDUP_SERIES_ENABLED', False)` works and an
operator flips it with env + restart. Consult sites:

- `DEDUP_SERIES_ENABLED` → **one** site, `news_bot.py:2665` (`if DEDUP_SERIES_ENABLED and pairs:`)
- `REVIEW_BUTTONS_ENABLED` → **one** site, `news_bot.py:1075` (`if not REVIEW_BUTTONS_ENABLED: …`),
  which the docstring at 1072 explicitly presents as the single gate other code checks through
  rather than re-testing the flag. `tests/test_integration.py:3578-3601` pins that the send site
  must **not** re-read it.

Test convention: `@patch('news_bot.REVIEW_BUTTONS_ENABLED', True/False)` — 16 occurrences in
`tests/test_integration.py`; `with patch('news_bot.DEDUP_SERIES_ENABLED', False)` at
`:1773`, `:2607`. `tests/test_integration.py:3532` additionally asserts
`self.assertNotIn('REVIEW_BUTTONS_ENABLED', os.environ)` — the suite guards against the env var
leaking into the test process.

### II-5.2 The structural problem this feature has and the other two did not

Both existing flags gate code **inside `news_bot.py`**. This feature's behaviour lives in three
**separate parser modules** that do **not** import `news_bot` — and cannot: `news_bot.py` imports
them (`t_hunted_source`, `lamley_source`, `autoevolution_source`), so the reverse import would be
circular. `t_hunted_source.py` imports only `admin_alerts` + `boilerplate_filter` (34-35);
`lamley_source.py` only `admin_alerts` + `boilerplate_filter` (26); `autoevolution_source.py`
imports `boilerplate_filter` (26).

So a `news_bot.py`-hosted constant cannot gate the parsers directly. The places where a flag
**can** be consulted, as facts about the current call graph:

| Option | Site | Blast radius | Byte-identical when off? |
|---|---|---|---|
| flag lives in the **new shared module**, parsers read it | inside each parser's blocks build | exactly the three sources | yes if the parsers skip emitting `blocks` entirely; the flat `paragraphs` build must then also stay on today's `find_all` path, otherwise the flat text still changes (II-6) |
| flag lives in `news_bot.py`, consulted in **`fetch_full_article`** | **3380-3426**; the three branches are `lamleygroup.com` **3416-3417**, `autoevolution.com` **3418-3419**, `blogspot.com` **3420-3421** | the three sources; **must not touch** the orangetrack pass-through at **3396-3413** which forwards `entry['blocks']` at **3412** | strips `blocks` → publish falls back to `_build_content`. **But the flat `paragraphs` returned by the parser has already changed** (II-6), so the *rendered text* would not be byte-identical to today unless the parsers also keep the old flat build. |
| flag consulted at **row assembly** | `news_bot.py:4181` (`'blocks': article.get('blocks')`) | all five sources — would also strip orangetrack's blocks | same caveat |

The precise fact that decides this: "flag off ⇒ output byte-identical to today" is only achievable
if the flag suppresses **both** the `blocks` emission **and** the new flat-text derivation. A
switch placed downstream of the parser (`fetch_full_article` or row assembly) suppresses only the
first. See II-6 for exactly how much the flat text moves.

Also worth noting: prod restarts reset the in-process daily schedule
(`.claude/skills/project-knowledge/references/deployment.md:253`), which is why AC11 exists — but
an import-time constant still needs a restart to take effect. The two precedents accept that.

---

## II-6. Flat-text shrinkage (user-spec Risk 2) — MEASURED

### II-6.1 The premise is largely wrong on real articles

Nested `<li><p>` / `<blockquote><p>` occurrences:

| Corpus | `li > p` | `blockquote > p` |
|---|---|---|
| 10 real t-hunted articles | **0** | **0** |
| 4 real lamley articles | **0** | **0** |
| all 15 inline HTML fixtures in `tests/test_{t_hunted,lamley,autoevolution}_source.py` + `test_boilerplate_filter.py` | **0** | **0** (no `<blockquote>` appears anywhere in the test suite) |

The double-counting defect described in Part I §3.4 is **real as a mechanism** — a synthetic
fixture (lead + 4×`<li><p>` + 1×`<blockquote><p>`) shrinks **924 → 462 chars, exactly −50 %** for
t-hunted and **856 → 428, −50 %** for lamley, because each nested item's text is duplicated
exactly once. But it **does not fire on any real article or any existing fixture**.

`autoevolution_source.py` does not have the defect at all: it walks `body.children` in a single
pass (**231-314**) and has **zero** `<li>`/`<blockquote>` handling, so list content is currently
*dropped*, not duplicated.

### II-6.2 What the text actually does change by

| Source | articles | paragraph entries before → after | chars before → after | delta |
|---|---|---|---|---|
| **t-hunted** | 10 | identical in **all 10** | 23 043 → 23 020 | **−0.10 %** (worst single article −0.8 %) |
| **lamley** | 4 | 79→74, 26→22, 18→13, 45→40 | 35 206 → 34 790 | **−1.18 %** (worst −2.5 %) |

**t-hunted: the only differences are cosmetic whitespace.** Four articles differ at all, and every
diff is one of two shapes (script `diff_th.py`):

```
OLD 'modelos típicos da época do filme.\xa0Foram 13 modelos…'    → NEW '…do filme. Foram 13 modelos…'   (nbsp collapsed)
OLD 'Para saber mais sobre a série Boulevard, clique aqui . Para…' → NEW '…clique aqui. Para…'            (spurious space before '.' gone)
```

Both come from the `_text_from_runs` vs `get_text(" ", strip=True)` difference, not from
de-duplication. `get_text(" ")` **inserts** a separator between every text node (so `</a>` + `"."`
becomes `"aqui ."`), while `_text_from_runs` joins with nothing then collapses `\s+` (which in
Python's `re` also matches `\xa0`). The new text is strictly cleaner.

Full divergence table between the two flatteners (measured live, not inferred):

| HTML | `get_text(" ", strip=True)` | `_text_from_runs` |
|---|---|---|
| `<p>Plain <strong>bold</strong> tail.</p>` | `'Plain bold tail.'` | `'Plain bold tail.'` — same |
| `<p>See <a href=…>this link</a> now</p>` | `'See this link now'` | same |
| `<li><p>Item text</p></li>` | `'Item text'` | same |
| `<blockquote><p>Quote</p></blockquote>` | `'Quote'` | same |
| `<p>a<strong>b</strong>c</p>` | `'a b c'` | **`'abc'`** |
| `<p>Word<b>Joined</b></p>` | `'Word Joined'` | **`'WordJoined'`** |
| `<p>Multi   spaces   here</p>` | `'Multi   spaces   here'` | **`'Multi spaces here'`** |
| `<p>Line<br>Two</p>` | `'Line Two'` | **`'LineTwo'`** (moot for orangetrack — `_emit_paragraph` **573** splits on `<br>` into separate blocks) |

This **inverts** Part I §7.10, which said the join *added* spaces. After `_text_from_runs` the join
*removes* them where the source HTML had none between a text node and a tag.

**lamley: the count drop is WordPress chrome removal, not de-duplication.** The entries that
disappear (script `lam_why.py`) are:

```
'Share this:'
'Email a link to a friend (Opens in new window) Email'
'More'
'Like this:'
'Related'
```

These are JetPack `sharedaddy` / `jp-related` blocks that leak into lamley's flat `paragraphs`
**today** — `filter_boilerplate` does not catch them — and that the walker's `_has_chrome_class`
(695-702, markers 690-693) discards. Everything else in the lamley diff is the same
whitespace-only pair as t-hunted (`'( find X on eBay )'` → `'(find X on eBay)'`).

So for lamley the change is a **content improvement** (5 chrome strings per article stop reaching
the LLM, the fingerprint, and the promo filter), and `_CHROME_CLASS_MARKERS` is required, not
optional.

### II-6.3 The `_is_text_only_checklist` threshold

`news_bot.py:1597-1624`. Exact predicate — two independent triggers, either suffices:

```
A.  _CHECKLIST_URL_RE.search(entry['link'])                  # r'case-contents-checklist'  (1594)
B.  _CHECKLIST_TITLE_RE.search(title)                        # r'\bcheck[\s-]?list\b'      (1573)
    AND sum(len(p) for p in article['paragraphs'] if isinstance(p, str)) < 500   # 1582, 1623
```

`title` is `entry['title'] or article['title']` (**1619**). Only `article['paragraphs']` is summed
— `subtitle` and `blocks` are **not** (**1622-1623**). Called from `job()` step (b3) at
**3836**, right after the `not article.get('paragraphs')` guard at **3827**; a True return
`continue`s and the row never reaches `insert_pending`.

Measured body-char totals after the change, per article:

```
t-hunted:  280, 436, 515, 573, 611, 799, 882, 1310, 5022, 12592
lamley:    3500, 8516, 9326, 13448
```

Five t-hunted articles already sit **below** 500 today (280, 436) or near it (515, 573, 611) — but
that is their *current* state, not a consequence of this change: the measured delta on those
articles is 0.0 % to −0.8 %, i.e. **at most 4 characters**. No article crosses the floor. And none
of the 14 has a `checklist` title, so trigger B never arms for them regardless.

Restated precisely: **on real articles the shrinkage is ~0.1–1.2 % and cannot move anything across
a 500-char boundary.** The floor is only reachable by the synthetic 50 %-shrink shape — where
924 → 462 does cross it. That shape (a review with a bulleted spec list plus a pull quote) is
plausible but unobserved in 14 real articles and 15 fixtures.

Downstream consumers of the same flat text, for completeness:

| Consumer | Bound | Sensitivity to ~1 % |
|---|---|---|
| `_is_promo_article` `news_bot.py:1786` | `_PROMO_SCAN_MAX_PARAGRAPHS = 8`, `_PROMO_SCAN_MAX_CHARS = 2000` (declared 1633-1634) | The **paragraph count** matters more than chars: lamley loses 4-5 leading-or-trailing chrome entries, which shifts *which* paragraphs land in the first 8. On the four lamley articles the removed entries are trailing chrome, so the first 8 are unchanged — but this is not guaranteed. |
| `model_extractor.extract_fingerprint` → `_gather_text` | `title + subtitle + paragraphs` | Fingerprint text changes for lamley (chrome gone). Fail-open at `news_bot.py:4128+` (`[E016]`), never blocks publishing. |
| `news_bot.py:3827` staging guard | `not article.get('paragraphs')` | Only fires at zero. Non-issue: minimum measured is 1 entry. |

---

## II-7. Second LLM call / `list_item` (AC3)

### II-7.1 Confirmed: all four engines omit `list_item`

| Engine | local `_PATCHED_TEXT_BLOCK_TYPES` | line | value |
|---|---|---|---|
| `claude_transcreation.py` | local | **159** | `("lead", "paragraph", "heading")` |
| `gemini_transcreation.py` | local | **132** | `("lead", "paragraph", "heading")` |
| `openai_transcreation.py` | local | **129** | `("lead", "paragraph", "heading")` |
| `openrouter_transcreation.py` | local | **217** | `("lead", "paragraph", "heading")` |
| `_llm_common.py` (shared) | canonical | **115** | `("lead", "paragraph", "heading", "list_item")` |

`list_item` is missing from all four. **These four line numbers are the exact constants to change.**

### II-7.2 What it costs — the mechanism, in order

Using openrouter (the prod engine) as the trace; the other three are structurally identical:

1. **475** `if expected_block_count and not parsed.get("blocks"):` — the variant-B guard.
   `expected_block_count = len(blocks_in)` (**408**), so it is truthy for **any** article carrying
   blocks. The system envelope tells the model *"Do not output a `blocks` field"*
   (`_llm_common.py:99`), so `parsed["blocks"]` is normally absent ⇒ **this branch fires on
   essentially every blocks-carrying article.**
2. **480-482** `_patch_text_with_ru_paragraphs(blocks_in, parsed["paragraphs"])` — uses the
   **shared 4-tuple including `list_item`** (`_llm_common.py:475`), so every `list_item`'s `text`
   is now **Russian**, and its `runs` were rebuilt from `**bold**` markers (479-487).
3. **490-492** `_translate_block_strings(parsed["blocks"], client, model, timeout_s=…)` — a
   **second LLM call**. Its skip test at **256-258** is
   `if skip_patched_text and field == "text" and btype in _PATCHED_TEXT_BLOCK_TYPES` with the
   **local 3-tuple**. `list_item` is not in it ⇒ **every list item's already-Russian text is put in
   the numbered EN list and sent to the model again** (items built 250-261, prompt 266-267).

Cost, concretely:
- The second call happens per-article anyway (for image/video `caption` fields). `list_item` does
  not *add* a call; it **inflates that call's payload** by one line per list item, both directions.
- The system prompt for that call (`_BLOCK_TRANSLATE_SYSTEM`, `openrouter_transcreation.py:199-210`) instructs
  *"Return strictly JSON: {"translations": [...]}"* with translate-to-Russian glossary rules
  applied to text that is already Russian — a re-translation, i.e. the RU-degradation risk Part I
  §5.4 flagged.
- Failure mode is contained: a count mismatch or exception returns `blocks` unchanged
  (**283-297**), so the already-correct Russian survives. The waste is tokens and latency, not
  correctness.
- The skip comment at **212-216** states the intent explicitly ("*to avoid re-translating already-RU
  paragraphs (one wasted API call per long article)*") — the tuple simply was never extended when
  `list_item` was added to the shared one.

Blast radius of the fix: orangetrack list posts are the only current producer of `list_item`, so
changing the four constants changes orangetrack's **second-call payload**. It does not change the
published `blocks` on the happy path (the text was already Russian from step 2) — but it does
change what a *failing* second call leaves behind, and it changes token counts. `AC10`
(byte-identical orangetrack output) is satisfiable, but this is a real cross-source edit.

---

## II-8. Test inventory

Baseline: **1628 passed, 441 subtests passed** — suite fully green, nothing pre-broken.

### II-8.1 Will pass unchanged

| File | Why |
|---|---|
| `tests/test_t_hunted_source.py` (17) | **No fixture contains `<li>`, `<h2>`, `<h3>`, `<h4>` or `<blockquote>` inside the body.** (`<h3 class="post-title">` is the title element, outside `body.find_all(...)` scope.) The two exact-equality assertions Part I §6.2 flagged — `:302-304` `out["paragraphs"] == ["A loja Universo Hot Wheels recebeu mais um set incrível."]` and `:336` `out["paragraphs"] == ["Second content paragraph."]` — both operate on plain-`<p>` bodies whose flattening is unchanged. Membership checks `:111-114`, `:333` likewise. No test asserts the returned key-set or `"blocks" not in out`. |
| `tests/test_lamley_source.py` (26) | No exact-equality assertion on `paragraphs` exists in the file at all. `:81` `"Bullet one" in out["paragraphs"]` and `:82` `"A heading" in out["paragraphs"]` survive **iff** the new build keeps `heading`/`list_item` text in the flat list — which is exactly what orangetrack already does at `orangetrack_source.py:823-826` (12-line rationale comment 811-822). |
| `tests/test_integration.py` (114) | Zero calls to `fetch_t_hunted_article` / `fetch_lamley_article` / `_scrape_article_page` / `enrich_entry`. Every `t-hunted`/`lamley`/`autoevolution` hit is a `source_name` string label in a seeded row. |
| `tests/test_hw_review_cli.py` (46) | All tests use the synthetic `_sample_entry()` (**34-49**) with caller-supplied `blocks=` (default `None`) and only `{'type':'paragraph','text':…}` blocks. Never real parser output. The parity-gate tests `:375`, `:389`, `:401` are unaffected. |
| `tests/test_job_prep_phase.py` (10) | `_article_payload()` (**102-108**) has **no `blocks` key**; `job()` is driven through `fetch_full_article` mocks. |
| `tests/test_llm_common.py` (21) | Builds `article`/`blocks` dicts by hand; source-agnostic. This file is the **consumer contract** for the new format and already proves `_encode_format_markers` / `_decode_format_markers` / `_build_user_message` / `_patch_text_with_ru_paragraphs` handle `runs` with `formats: ['bold']`. |
| `tests/test_boilerplate_filter.py` (60) integration classes | `TestLamleyIntegration::test_share_paragraphs_stripped` (**676-703**) and `TestAutoevolutionIntegration::…` (**768-798**) use plain-`<p>` fixtures. |
| `tests/test_preview_renderer.py` (48) | Node-tree → HTML only, zero references to `blocks`/`runs`/`paragraphs`. |
| `tests/test_telegraph_publisher.py` (69) | Feeds hand-built blocks; the blocks path is already exercised. |

**Part I §6.2, §6.3 and §6.4 are therefore mostly refuted** — those assertions were listed as "at
risk" on the assumption the flat list would change shape. Measured (II-6), it does not for these
fixtures. §6.2's mattel rows are moot (out of scope).

### II-8.2 Must be verified deliberately

| File:line | Test | Assertion | Why it is the one at risk |
|---|---|---|---|
| `tests/test_autoevolution_source.py:180-184` | `test_parses_title_subtitle_and_ordered_blocks` | `porsche_para["runs"] == [{"text": "The rare Porsche is finally here. See "}, {"text": "Red Line Club", "href": "https://mattel.com/rlc"}, {"text": " for details."}]` | **Exact list equality with no `formats` key on any run.** Survives only if the shared `_runs_from_tag` keeps orangetrack's convention of adding `formats` **conditionally** (`orangetrack_source.py:336-338` `if fmts: run["formats"] = list(fmts)`). If the extraction ever sets `"formats": []` unconditionally, this breaks immediately. **Highest-risk assertion in the suite.** |
| `tests/test_autoevolution_source.py:216-226` | `test_extracts_heading_nested_inside_paragraph` | `text_blocks == [("heading","BMW M1 Procar",2), ("paragraph",…,None), …, ("heading","Inline H3 Section",3), …]` | Pins that autoevolution keeps the **real** heading level (2 and 3). orangetrack normalises everything to 3 (`orangetrack_source.py:631-637`, pinned by `test_orangetrack_source.py:1084-1095`). A shared emitter must not force orangetrack's normalisation onto autoevolution. |
| `tests/test_autoevolution_source.py:138-152` | same test | `types == ["image","lead","paragraph","paragraph","image","heading","paragraph","video","image","image"]` | Exact block-type sequence. Unaffected by bold runs, but any change to walk order / image handling shows up here first. |
| `tests/test_autoevolution_source.py:74, 93, 98, 113, 329, 341` | `TestEnrichEntry.*`, RSS-fallback tests | various `out["paragraphs"] == [...]` | These go through `enrich_entry` (**409-435**, returns **four** keys, no `blocks`) or the RSS-fallback branch — **never** `_scrape_article_page`. Safe as long as the RSS path is left alone. |

### II-8.3 `test_orangetrack_source.py` (94) — the AC10 checklist

Must stay green **without edits**. What it pins about the walker, i.e. the refactor's acceptance
surface:

| Invariant | Test lines |
|---|---|
| `out["subtitle"] == ""` always | 115 |
| `len(out["paragraphs"]) == count of paragraph-type blocks` | 124-130, 162-165 |
| h2/h3/h4 → `heading` with `level == 3` regardless of tag | 1084-1095 |
| h5 → `paragraph`, not `heading` (`babc67c`) | 145-164, 1097-1106 |
| h1 → `title` only; h6 → dropped | 1108-1120 |
| `<li>`/`<ol><li>` → `list_item` with `text`+`runs`, and `"•" not in block["text"]` | 975-1082 |
| flat `paragraphs` includes `list_item` + `heading` text in DOM order | 1122-1142 |
| `<br>` inside one `<p>` splits into multiple paragraph blocks | 1028-1051 |
| all four formats: bold (`strong`/`b`/WP color class), italic (`em`/`i`), underline (`u`), strikethrough (`s`) | 1152-1306 |
| `formats` key **omitted** on plain runs | ~1292-1295 |
| **no format bleed backward** onto preceding plain text (`a509722`) | 1173-1207 |
| flattened `text` has **no doubled spaces** from the run join (2026-07-28) | 1208-1221 |
| every run's `text` is a literal substring of the block's `text` (renderer uses `text.find`) | 1223-1234 |
| exact Telegraph node tree: `{"tag":"h3",…}` for any heading level; `{"tag":"p","children":["• ", …]}` for `list_item`; inline links render as plain text, href lives only in `runs` | 1323-1391 |

Run `pytest tests/test_orangetrack_source.py -q` after any extraction step and diff against this
table.

### II-8.4 Gaps that are not test breaks

| Gap | Location | Consequence |
|---|---|---|
| `hw_review._VALID_BLOCK_TYPES` lacks `list_item` | **`hw_review.py:104`** = `frozenset({'paragraph','lead','heading','image','video'})`; rejection at **304**; `_BLOCK_KEYS_BY_TYPE` **105-111** additionally rejects any extra key, so a `runs`-carrying block is refused outright | `tests/test_hw_review_cli.py:677-687` `test_valid_block_types` never exercises `list_item`, so nothing catches it. Once the three sources emit `list_item`, `hw_review stage N` for those rows is unusable. Mitigating fact: the manual path is **archived since 2026-04-30** (`architecture.md`, `ux-guidelines.md:11`); 100 % of posts go through `_fallback_publish`. Also already out of sync — `_validate_block` requires `heading.level in (3,4)` (**315-316**) while autoevolution emits level 2. |
| `filter_blocks` is never tested with a `list_item` block | `tests/test_boilerplate_filter.py:806-878` `TestFilterBlocks` builds block dicts by hand, none of type `list_item` | Nobody asserts a boilerplate list item gets stripped. `filter_blocks` (`boilerplate_filter.py:344-361`) treats it correctly as a pure-text block, but that is untested. |
| `_strip_plugs_in_blocks` drops empty blocks only for `('paragraph','lead','heading')` | **`news_bot.py:1325-1330`** vs the RU boilerplate re-filter 40 lines later at **3231** which uses `('paragraph','lead','heading','list_item')` | An emptied `list_item` survives `_strip_plugs_in_blocks` and renders as a bare `• `. Part I §7.8 item 1, re-verified, still present. |
| `_build_content_from_blocks` ignores `runs` for `lead` blocks | **`telegraph_publisher.py:431`** `nodes.append(p(b_(_decode_bold_markers(block["text"])[0])))` | autoevolution is the only `lead` producer; its bold inside a lead would not render. Part I §7.8 item 3, still present. |

### II-8.5 `test_deploy_files_invariant.py` — Part I §9 was wrong

The file's **docstring** claims "every new first-party module imported by `news_bot.py` must appear
in ALL THREE deploy FILES arrays". The **implementation** does not check that:

```python
EXPECTED_ENTRY = '"t_hunted_source.py"'          # tests/test_deploy_files_invariant.py:24
```

Three tests (**26-51**) do a plain substring search for that one literal in `deploy.sh`,
`.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml`; three more (**61-84**) do the
same for `watchdog.sh`. There is **no enumeration of the repo's modules**. A brand-new shared
walker module is **invisible** to this test — it stays green whether or not the module is listed.

What actually ships to prod, verified:

- **`Dockerfile` does `COPY . .`** (line 19). `.dockerignore` excludes `.git`, `.github`, `tests`,
  `work`, `logs`, `__pycache__`, `*.db`, `.env*` — and **not** `*.py`. So **a new module ships to
  prod automatically** with `git pull && docker compose up -d --build`.
- `deploy.sh` (**FILES array 37-60**) is the **legacy SCP fallback**;
  `deployment.md:182` labels it exactly that, and `:149` records `deploy.yml`'s prod path as
  **DISARMED**.
- `deployment.md:191` still states the FILES-array invariant as binding, and `:189` notes the SCP
  route is "the LEGACY route".

So: adding a new module needs no deploy-file change for the Docker prod path, but leaving the FILES
arrays un-updated silently breaks the documented emergency SCP fallback, and **no test will tell
you**.

---

## II-9. Corrections to Part I worth carrying forward

| Part I claim | Correction |
|---|---|
| §2.3 "`_walk` is ~80 % generic … not currently importable" | Accurate but imprecise. `_walk`'s **body contains zero site-specific strings**; all site knowledge arrives through 6 free names (II-1.2). `_CHROME_CLASS_MARKERS` was described as orangetrack/WordPress chrome — it is **required by lamley too** (II-6.2). |
| §7.4 "filters for flat text and for blocks are two DIFFERENT passes over two different lists, and this is the second source of length divergence" | `filter_blocks` and `filter_boilerplate` call the **same** `is_boilerplate` on the same string for pure-text blocks, so they agree; orangetrack's `filter_boilerplate` at **827** is a no-op after `filter_blocks` at **809**. The real second divergence source is the **title-dedup filter** `if text and text != title` (`t_hunted_source.py:198`, `lamley_source.py:390`), which the walker lacks. |
| §7.10 "copying the orangetrack pattern would insert double spaces" | Inverted since `_text_from_runs` landed: the join now **removes** separators `get_text(" ")` would have inserted (`a<strong>b</strong>c` → `'abc'` vs `'a b c'`). Measured net effect on real flat text: **−0.10 % (t-hunted) / −1.18 % (lamley)** (II-6.2). |
| §6.5 "the alignment invariant is already an explicit test" | Still true, and now quantified: keeping the lift breaks it on **13 of 14** real articles by exactly 1 (II-3.2). |
| §7.7 image-cap row | Confirmed and quantified: **4/4 lamley articles exceed their 10-image cap** on the blocks path (14/41/48/50); t-hunted max 27 vs cap 30 (II-4.3). |
| §9 "a new module must be checked against `test_deploy_files_invariant`" | That test cannot see new modules; prod is Docker `COPY . .` (II-8.5). |
| §1 line numbers for `news_bot.py` | All shifted **+59**. `fetch_full_article` **3380-3426**; dispatch **3396-3421**; staging guard **3827**; row assembly **4173-4193**; `insert_pending` call **4195**. |
