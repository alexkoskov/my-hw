# Code Research: orangetrack-source

Research date: 2026-05-04
Author: research agent (Cycle 1 input for user-spec)
Scope: Adding `orangetrackdiecast.com` as a fourth news source. Architecture = FEED-ONLY parser (no article-page scrape). RSS `<content:encoded>` already carries the full HTML body.

---

## 1. Existing source-parser contract

### Three reference parsers compared

| Source | Public function | Returns on success | Returns on failure |
|---|---|---|---|
| `lamley_source.fetch_lamley_article(link, session=None, notifier=None)` | `dict` | `None` |
| `autoevolution_source.fetch_autoevolution_article(entry, fetcher=None)` | `dict` | `None` (only via the `enrich_entry` final fallback; the scrape stage may degrade to RSS but never yields `None` if the entry has any title/summary) |
| `mattel_news_source.fetch_mattel_article(link, session=None, notifier=None)` | `dict` | `None` |

### Return-dict shape (the de-facto interface)

All three return a dict with the same four keys; autoevolution adds a fifth:

```python
{
    "title": str,            # always present (may be "")
    "subtitle": str,         # always present (may be "")
    "paragraphs": list[str], # always present (may be empty per Mattel AC9)
    "images": list[str],     # always present (may be empty)
    "blocks": list[dict],    # autoevolution ONLY (optional key)
}
```

`blocks` is an autoevolution-specific extension carrying typed children
(`{"type": "lead"|"paragraph"|"heading"|"image"|"video", ...}`).
`news_bot.py:1522` already passes it through verbatim:
```python
'blocks': article.get('blocks'),   # None for sources that don't emit blocks
```
The DB column `pending_articles.blocks` is nullable. **For orangetrack
the simplest thing is to NOT emit `blocks`** (let the consumer fall back
to flat `paragraphs` + `images`). Adding `blocks` is only worth it if
Telegraph rendering of typed image/video positioning matters.

### Key caller-visible contracts

- **`paragraphs` is the gating field** — `news_bot.py:1510`:
  ```python
  if not article or not article.get('paragraphs'):
      logger.warning(f"No article data for {link}, skipping")
      continue
  ```
  Returning a non-None dict with empty `paragraphs` causes the entry to
  be skipped (and logs WARNING — not ERROR). For orangetrack that means
  if a feed item has only a video embed and zero paragraphs we MUST
  synthesize at least one paragraph or accept that the article gets
  dropped. (Mattel's AC9 explicitly accepts the empty-paragraph drop.)

- **`subtitle` empty-string convention** — when the source has no
  dedicated subtitle field, return `""` (not `None`). The Telegraph
  publisher then skips the decorated lead-paragraph + `<hr>` rendering
  (`autoevolution_source.py:360` calls this out explicitly).

- **`title` empty-string convention** — `news_bot.py:1518` falls back
  to `entry.get('title')` when `article.get('title')` is empty:
  ```python
  'title': article.get('title') or entry.get('title') or '',
  ```
  So returning `""` for title is safe — RSS `<title>` becomes the
  canonical title via fallback.

- **`images` may be empty** — `news_bot.py:1521`: `article.get('images') or []`.
  No image is fine.

### Edge cases each parser handles

| Case | lamley | autoevolution | mattel |
|---|---|---|---|
| HTTP error → `notifier` + `None` | yes (`lamley_source.py:319`) | n/a (returns RSS-fallback dict, never None for valid entry) | yes (`mattel_news_source.py:469`) |
| Missing body → `None` | yes (`lamley_source.py:339`) | yes (`autoevolution_source.py:160`) | n/a (returns dict with empty paragraphs per AC9) |
| Empty-paragraphs-after-filter → `None` | no (returns `paragraphs=[]`) | yes (`autoevolution_source.py:296`) | no (returns empty per AC9) |
| Missing images → `[]` | yes | yes | yes |
| Hostname allowlist (SSRF) | yes (`lamley_source.py:212-225`) | n/a (entry-driven) | yes (article-prefix check, `mattel_news_source.py:450`) |
| Boilerplate filter applied | yes (`lamley_source.py:349`) | yes (`autoevolution_source.py:293`) | yes (`mattel_news_source.py:488`) |

### Recommended orangetrack contract

```python
# orangetrack_source.py
def fetch_orangetrack_article(entry: dict) -> Optional[Dict]:
    """Parse content:encoded body from a feedparser entry into the
    canonical {title, subtitle, paragraphs, images} shape.

    Pure-parse, no HTTP — the entry already has the full body inline.
    Returns None only on structural errors (no content:encoded field,
    BS4 parse failure). An empty body returns
    {title, subtitle="", paragraphs=[entry.title], images=[]} so the
    pending-articles gate (which requires non-empty paragraphs) accepts
    it — synthesise from title rather than skip silently.
    """
```

No HTTP, so no allowlist, no notifier, no rate-limit. Signature is
`(entry: dict)` not `(link, session, notifier)` — this is the autoevolution
shape, not the lamley shape.

---

## 2. Integration points map (file-by-file checklist)

A complete touch-list, in execution order:

### 2.1 `feeds.json` — add the feed URL

Current contents (3 lines):
```json
[
    "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml",
    "https://www.autoevolution.com/rss/tag-Hot+Wheels+News.xml",
    "https://lamleygroup.com/category/hot-wheels/feed/"
]
```
Add:
```json
"https://orangetrackdiecast.com/feed/"
```
**Cap awareness**: `news_bot.load_feeds()` truncates with `data[:5]`
(`news_bot.py:176`). Adding a 4th URL is safe; a 6th would be silently
dropped.

### 2.2 `news_bot.NETLOC_TO_SOURCE` (line 745–751)

Add two entries (mirroring the lamley pattern — both bare host and `www.`):
```python
NETLOC_TO_SOURCE = {
    'www.autoevolution.com': 'autoevolution',
    'autoevolution.com':     'autoevolution',
    'lamleygroup.com':       'lamley',
    'www.lamleygroup.com':   'lamley',
    'corporate.mattel.com':  'mattel',
    'orangetrackdiecast.com':     'orangetrack',  # new
    'www.orangetrackdiecast.com': 'orangetrack',  # new
}
```

### 2.3 `news_bot.SOURCE_EMOJI` and `SOURCE_LABEL` (lines 777–786)

Add the new key in both dicts:
```python
SOURCE_EMOJI = {
    'autoevolution': '\U0001F7E0',  # orange circle
    'mattel':        '\U0001F7E3',  # purple circle
    'lamley':        '\U0001F7E2',  # green circle
    'orangetrack':   '\U0001F535',  # blue circle (4th colour, free)
}
SOURCE_LABEL = {
    'autoevolution': 'autoevolution',
    'mattel':        'mattel',
    'lamley':        'lamley',
    'orangetrack':   'orangetrack',
}
```
Note: `SOURCE_EMOJI` is consumed by `hw_review.py:198`
(`emoji = news_bot.SOURCE_EMOJI.get(row['source_name'], '•')`).

### 2.4 `news_bot.fetch_full_article` dispatcher (lines 1265–1286)

Add a domain branch BEFORE the catch-all warning:
```python
if 'orangetrackdiecast.com' in domain:
    return orangetrack_source.fetch_orangetrack_article(entry)
```
Plus a top-level import beside `import lamley_source` at line 36:
```python
import orangetrack_source
```

### 2.5 `news_bot.SOURCES` registry (lines 1300–1366) — **NO CHANGE NEEDED**

The orangetrack feed enters via `_fetch_rss_entries`, which already
loops `load_feeds()` and stamps `source_name` via `_resolve_source_name`.
Once `feeds.json` and `NETLOC_TO_SOURCE` are updated, the registry
picks the new feed up automatically. **Do NOT add a separate
`_fetch_orangetrack_entries` function** — that would duplicate work.

### 2.6 `boilerplate_filter._BOILERPLATE_PATTERNS` (lines 51–111)

Add affiliate-line regex(es). Recommended addition:
```python
# Orangetrack — affiliate "QUICK LINK!" plugs as standalone paragraphs.
# Variant A only — Brad always wraps these in their own
# <p class="has-text-align-center wp-block-paragraph"><strong><a>...
# so they always arrive as a standalone paragraph (≤ 120 chars).
re.compile(r'^quick link[!:\s].*\b(buy|order|get|grab|shop)\b', re.I),
```
Variants observed in the live feed:
- `QUICK LINK! Buy the Hot Wheels Car Culture / Team Transport K case from 1 Stop Diecast now.` (line 83 in feed)
- `QUICK LINK! Order a Hot Wheels Pop Culture "Q" case from 1 Stop Diecast` (line 220)
- `QUICK LINK! Buy the new 2025 Hot Wheels "H" case from Jcar Diecast.` (line 513)

**Length bound is 120 chars** (`_MAX_BOILERPLATE_LEN = 120` —
`boilerplate_filter.py:37`, raised from 80 in author-plug-filter).
Sample lengths above are 95–110 chars, so they fit. A safe upper-bound
test: the longest is ~108 chars including punctuation → confirmed under cap.

### 2.7 Variant B post-translation filter — **NOT in scope for this feature**

`work/author-plug-filter/` is a feature **planned but NOT yet executed**
(`work/author-plug-filter/decisions.md` is empty boilerplate). The
docstring `boilerplate_filter.py:69–70` referencing a "variant B
(post-LLM) in `author_plug_filter.py`" is forward-looking. The file
`author_plug_filter.py` does NOT exist yet. So:

- This feature touches ONLY the pre-LLM `boilerplate_filter._BOILERPLATE_PATTERNS`
  for standalone-paragraph affiliate lines.
- Inline affiliate sentences embedded in real paragraphs (e.g.
  "...Use the link below to get yours!") are **out of scope** — they
  would need either (a) waiting for author-plug-filter feature to ship,
  then adding orangetrack patterns to its variant-B regex, or (b) a
  per-source pre-LLM substring strip (rejected here as scope creep).

### 2.8 `deploy.sh` FILES list (lines 37–57) and `.github/workflows/deploy.yml` FILES list (lines 117–137)

**INVARIANT** documented at `deploy.sh:22` and `.github/workflows/deploy.yml:103`:
both arrays must be byte-for-byte equivalent. Add `"orangetrack_source.py"`
to both, in the same alphabetical-ish position (after `lamley_source.py`):
```bash
"orangetrack_source.py"
```
Forgetting either side → `ImportError` on the next cron tick on the
production server with no CI signal beforehand.

### 2.9 `tests/` — new test file

New file: `tests/test_orangetrack_source.py`
- Naming pattern matches `test_lamley_source.py`, `test_mattel_news_source.py`,
  `test_autoevolution_source.py`.
- Existing fixture pattern (Mattel) uses **synthesized minimal HTML**
  via a builder helper (`tests/fixtures/mattel_flight_builder.py`). Lamley
  uses an **inline SAMPLE_HTML constant** at the top of the test file.
  Recommendation: **inline SAMPLE_HTML constant** for orangetrack —
  the WordPress-block markup is simpler than Mattel's RSC stream;
  no need for a builder. Optional: capture a one-off real
  `<content:encoded>` body to `tests/fixtures/orangetrack_sample.xml`
  for an end-to-end "real feed parse" smoke test.
- `tests/conftest.py` already inserts repo root onto `sys.path` (lines
  1–14) — no new conftest needed.
- Update `tests/test_sources_registry.py::TestNetlocToSource` if its
  hard-coded set assertion (lines 41–55) is to keep passing — it
  checks `set(NETLOC_TO_SOURCE) == {5 keys}` and
  `set(NETLOC_TO_SOURCE.values()) == {3 source names}`. Both must be
  expanded.

### 2.10 Anything else (logging filters, admin-ping, env vars)

Audited the codebase:

- `news_bot.send_admin_notification` is the only notifier; nothing
  source-specific.
- No env vars per source (each parser is self-contained).
- No logging filters per source — module-level loggers only.
- No CI config beyond `deploy.yml` / `ci.yml`. `ci.yml` runs the unit
  test suite — adding `tests/test_orangetrack_source.py` is automatic.
- `requirements.txt` — already has `feedparser` and `beautifulsoup4`.
  No new dependency needed.
- `pending_articles_repo.py` — schema-agnostic to `source_name` value
  (it's a TEXT column). No migration needed.

---

## 3. WordPress.com RSS quirks — orangetrackdiecast vs lamley

### Orangetrack's content:encoded carries the FULL article body

Live fetch, 2026-05-04: top item "Hot Wheels 2026 Car Culture / Team Transport
'K' CASE REPORT", `<content:encoded>` is ~85 lines of HTML containing:
- Multiple `<p class="wp-block-paragraph">` for body prose
- `<h5 class="wp-block-heading has-text-align-center">` for section labels (case numbers)
- `<figure class="wp-block-embed is-type-rich is-provider-embed-handler ... wp-embed-aspect-16-9">` wrapping `<iframe class="youtube-player" ... src="https://www.youtube.com/embed/...">` for video embeds
- `<figure class="wp-block-image size-large"><a href=full><img src=...?w=1024 srcset="... 1024w, ... 150w, ... 300w, ... 768w, ... 1200w" sizes="..."/></a></figure>` for images
- `<figure class="wp-block-gallery has-nested-images columns-default ...">` wrapping multiple `<figure class="wp-block-image">` children for galleries
- `<hr class="wp-block-separator has-alpha-channel-opacity"/>` as section dividers
- `<p class="has-text-align-center wp-block-paragraph">` (extra class) for centered/affiliate paragraphs

### Lamley's content:encoded carries ONLY a short excerpt

Live fetch, 2026-05-04: `<content:encoded><![CDATA[<p>Alex Winson talks to Hot Wheels designer Bryan Zhao...</p>]]></content:encoded>` — ONE `<p>`, ~100 chars. The full body lives only on the article page. **This is why `lamley_source.py` does an HTTP scrape — orangetrack does NOT need that.**

### Implication: cannot reuse `lamley_source` parsing helpers

`lamley_source.py:325–369` (BS4-walk over `div.entry-content`)
operates on a **full article-page** DOM with `<h1 class="entry-title">`,
`<div class="entry-content">`, etc. Orangetrack's `<content:encoded>`
is **just the body fragment** — no h1, no entry-content wrapper. So:
- Lamley's selector `soup.find("h1", class_="entry-title")` → won't
  match anything in `<content:encoded>`. Title comes from RSS
  `<title>` (which feedparser exposes as `entry.title`).
- Lamley's `soup.find("div", class_="entry-content")` → no match.
  Just walk `soup` directly (or wrap as `soup = BeautifulSoup(html, "html.parser")`
  and walk `soup` as the body root).
- Lamley's `body.find_all(["p", "li", "h2", "h3", "h4", "blockquote"])`
  → adapt to `["p", "li", "h2", "h3", "h4", "h5", "blockquote"]` — note
  **h5 is significant** in orangetrack (case-section labels).
- Lamley's image walk `body.find_all("img")` is essentially reusable
  but won't pick up the `?w=1024` srcset preference — see § 5.

**Recommendation: write `orangetrack_source.py` as a fresh module**
sized similarly to lamley (~150-200 LoC), copying the docstring +
boilerplate pattern but NOT importing helpers from `lamley_source`.
The Cloudflare bypass (`curl_cffi`), throttle, WAF protection, and
URL allowlist are all irrelevant for orangetrack (no HTTP).

### feedparser RSS-quirk specifics

- `entry.content` is a list of `{"type": "text/html", "value": "<HTML>"}` dicts in feedparser. The convenient accessor is `entry.content[0].value`.
- `entry.title` is the un-CDATA'd RSS `<title>`.
- HTML entities (`&#8220;`, `&#8217;`, `&#8212;`) come through escaped
  in the raw feed but feedparser DOES NOT unescape them inside
  `content[0].value`. BS4's `.get_text()` will. Use BS4 — never raw
  string regex.

---

## 4. YouTube embed handling

`autoevolution_source._video_embed_url` (lines 83–101) is the canonical
helper:

```python
YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/watch\?v=|youtube\.com/embed/)"
    r"([A-Za-z0-9_-]+)"
)

def _video_embed_url(href):
    m = YOUTUBE_ID_RE.search(href)
    if m:
        watch = f"https://www.youtube.com/watch?v={m.group(1)}"
        return f"https://telegra.ph/embed/youtube?url={urllib.parse.quote(watch, safe='')}"
    ...
```

Why the wrapping is required: Telegra.ph's `iframe@src` validator
accepts ONLY URLs under its own `/embed/<provider>?url=…` proxy. Raw
`youtube.com/embed/<id>` URLs are stripped to an empty `/embed/`,
which makes the page fail Instant View.

### Orangetrack iframes

Sample (line 88 of feed):
```html
<iframe class="youtube-player" width="1300" height="732"
  src="https://www.youtube.com/embed/ZaDCHaa9MnM?version=3&#038;rel=1&#038;..."
  allowfullscreen="true" sandbox="..."></iframe>
```

**Yes — orangetrack iframes need the EXACT same wrapper.** The regex
`YOUTUBE_ID_RE` above DOES match the `youtube.com/embed/<id>` form
(third alternative in the alternation). So options:

1. **Reuse**: `from autoevolution_source import _video_embed_url`
   (private — but small underscore-prefixed helpers are imported by
   convention in this codebase; cf. `mattel_news_source` exports
   `_extract_entries` for tests). Stylistically risky — violates
   private-prefix convention.
2. **Recommended — copy 5 lines** of `_video_embed_url` + `YOUTUBE_ID_RE`
   into `orangetrack_source.py`. Tiny duplication, full module isolation,
   no cross-source import.
3. **Refactor** — promote the helper to a shared `_telegraph_embeds.py`.
   Out of scope for this S-size feature; would require changing
   autoevolution too.

If orangetrack emits `blocks` (autoevolution-style typed-block list),
the video block shape is `{"type": "video", "src": <wrapped URL>}` —
identical to autoevolution `autoevolution_source.py:242`. If orangetrack
emits flat `paragraphs` only, video iframes are silently dropped
(no Telegraph-block representation in flat-paragraph parsers).

**Per Cycle-1 description**: orangetrack should wrap iframes via
telegra.ph proxy. That implies `blocks`-mode output (since flat
paragraphs have no place for a video). **Decision pending in user-spec.**

---

## 5. Image extraction patterns

### Lamley's pattern (`lamley_source.py:357–369`)
```python
images = []
seen_bases = set()
for img in body.find_all("img"):
    src = img.get("src") or ""
    if not src.startswith("http"):
        continue
    base = src.split("?", 1)[0]
    if base in seen_bases:
        continue
    seen_bases.add(base)
    images.append(src)
    if len(images) >= IMAGE_LIMIT:
        break
```

- `IMAGE_LIMIT = 10` (line 77).
- Dedup key is the URL **before** the `?` query string (so
  `img.jpg?w=1024` and `img.jpg?w=300` are the same image; first one
  wins).
- No srcset preference — just takes whatever `<img src>` is set.
- `http`-only filter blocks `data:` URIs and relative paths.

### Orangetrack-specific complications

1. **Srcset preference for `?w=1024`** — orangetrack always renders
   `<img src="...?w=1024" srcset="... 1024w, ... 150w, ... 300w,
   ... 768w, ... 1200w">`. Lamley's logic above already grabs the
   `src` attribute (`?w=1024` in orangetrack's case), so the `?w=1024`
   preference falls out for free. **No srcset parsing needed if you
   trust `src`.** If you want a higher-res override
   (`?w=1200` from srcset), parse `srcset` separately. Recommended:
   **don't** — keep it simple, `src` already gives a Telegraph-friendly
   1024px image.

2. **Galleries** — orangetrack wraps gallery items in
   `<figure class="wp-block-gallery">` containing nested
   `<figure class="wp-block-image">` children (sample at lines
   105–115 of the feed). Each child has its own `<img>`. Lamley's
   flat `body.find_all("img")` walks them all, so dedup-by-base
   handles galleries correctly **for the image list** — but loses
   positional information vs. surrounding paragraphs (which is fine
   for flat `paragraphs`+`images` output; matters only if emitting
   `blocks`).

3. **Image links wrap each `<img>`** — orangetrack's pattern is
   `<figure><a href="full.jpg"><img src="full.jpg?w=1024" srcset="..."/></a></figure>`.
   The `<a href>` points to the un-resized original; the `<img src>`
   is the resized 1024px version. Telegraph generally wants the
   1024px version (smaller payload, pre-resized). Sticking with
   `img['src']` is the right call.

4. **WordPress Jetpack `?ssl=1` and CDN variants** — none observed in
   the orangetrack sample. If they appear, `split("?", 1)[0]` dedup
   handles them.

5. **Zero-image articles** — possible (e.g. case report posts that
   are mostly text + a single video). `images=[]` is acceptable.

**Recommended logic for orangetrack**:
```python
images = []
seen_bases = set()
for img in soup.find_all("img"):
    src = img.get("src") or ""
    if not src.startswith("http"):
        continue
    base = src.split("?", 1)[0]
    if base in seen_bases:
        continue
    seen_bases.add(base)
    images.append(src)
    if len(images) >= IMAGE_LIMIT:
        break
```
Identical to lamley's, applied to `soup` (root of `<content:encoded>`)
rather than `body`.

---

## 6. Boilerplate filter extension points

### Structure (boilerplate_filter.py:51–111)
- `_BOILERPLATE_PATTERNS` — flat `list[re.Pattern]`. Each pattern is
  matched against the whole stripped paragraph (`pat.search(s)`),
  case-insensitive (`re.I`). Match → drop.
- `_MAX_BOILERPLATE_LEN = 120` (line 37). Paragraphs longer than 120
  chars are NEVER classified as boilerplate, even if they match a
  pattern. Rationale: real prose that happens to start with "Share on
  Facebook" stays.
- **Verified**: 120-char threshold; bumped from 80 in the
  author-plug-filter feature (per the comment on line 35).
- `_PLUG_PLATFORMS` tuple (line 43) is the single source of platform
  names used across patterns A1, A2, A3 — keep alternation in sync.

### Where to add affiliate patterns

Append to `_BOILERPLATE_PATTERNS` after the existing English block
(after line 65, before the Russian section). Suggested patch:

```python
re.compile(r'^quick link[!:\s].*\b(buy|order|get|grab|shop)\b', re.I),
```

Sample affiliate paragraphs from the live feed (all under 120 chars):
- `QUICK LINK! Buy the Hot Wheels Car Culture / Team Transport K case from 1 Stop Diecast now.` — 95 chars
- `QUICK LINK! Order a Hot Wheels Pop Culture "Q" case from 1 Stop Diecast` — 71 chars
- `QUICK LINK! Buy the new 2025 Hot Wheels "H" case from Jcar Diecast.` — 67 chars
- `QUICK LINK! Buy a case of Hot Wheels Car Culture / Modern Classics from 1 Stop Diecast now` — 91 chars

**Important**: the BS4 `get_text()` of the wrapped paragraph drops
the `<em>` tags, so input to the matcher is the plain text shape
`"QUICK LINK! Buy ... now."` — anchor on the `^QUICK LINK` prefix.

### Variant B (post-translation) — out of scope for this feature

See §2.7 above. The author-plug-filter feature is planned but not
shipped. Even when it ships, `author_plug_filter.py` is for **author
social plugs** (Instagram/Twitter/etc.); affiliate-shop URLs are a
different category. Adding orangetrack affiliate patterns to a future
variant-B module would be a follow-up feature, not part of this one.

---

## 7. Test fixture patterns

### `test_lamley_source.py` (recommended pattern for orangetrack)
- Lines 50–68: **inline `SAMPLE_HTML` constant** — synthesized minimal
  HTML that exercises the parser:
  ```python
  SAMPLE_HTML = """
  <html>
  <body>
  <h1 class="entry-title">Sample Hot Wheels Post</h1>
  ...
  </body>
  </html>
  """
  ```
- Lines 37–47: `_make_response(text, status, raise_exc, content, headers)`
  helper that builds a `MagicMock(spec=requests.Response)`.
- Network mocked via `session = MagicMock(); session.get.return_value = ...`
  passed as the `session=` kwarg (`fetch_lamley_article(link, session=session)`).
- Throttle and WAF state reset in `_no_real_sleep` autouse fixture
  (lines 12–24). Not relevant for orangetrack (no throttle).

### `test_mattel_news_source.py` (more elaborate — reference, not template)
- Uses `tests/fixtures/mattel_flight_builder.py` (a 2-function builder
  module: `_make_flight_listing`, `_make_flight_article`) because the
  RSC payload format is fiddly to hand-write.
- Real captured HTML samples are NOT under `tests/fixtures/` — only
  the builder.

### Recommended pattern for `test_orangetrack_source.py`

**Inline `SAMPLE_CONTENT_ENCODED` constant** at the top of the test
file. WordPress-block markup is simpler than Mattel's RSC stream — no
builder needed. Example skeleton:

```python
SAMPLE_CONTENT_ENCODED = """
<p class="wp-block-paragraph">First paragraph.</p>
<h5 class="wp-block-heading has-text-align-center">#89 Mercedes-Benz</h5>
<p class="wp-block-paragraph">Second paragraph with detail.</p>
<figure class="wp-block-image"><a href="https://example.com/img.jpg"><img src="https://example.com/img.jpg?w=1024" srcset="..." /></a></figure>
<figure class="wp-block-embed is-provider-youtube"><div class="wp-block-embed__wrapper">
<iframe src="https://www.youtube.com/embed/abc123?rel=1"></iframe>
</div></figure>
<p class="has-text-align-center wp-block-paragraph"><strong><a href="https://shop.example.com/x"><em>QUICK LINK!</em> Buy the K case from 1 Stop Diecast now.</a></strong></p>
"""

def _make_entry(content_encoded=SAMPLE_CONTENT_ENCODED, title="Test Post"):
    return {
        "title": title,
        "link": "https://orangetrackdiecast.com/2026/05/02/test-post/",
        "content": [{"type": "text/html", "value": content_encoded}],
        "summary": "...",
        "published": "Sat, 02 May 2026 15:30:00 +0000",
    }
```

Tests to write (8–12 cases is right):
- `test_extracts_title_from_rss_entry` — title comes from `entry.title`.
- `test_extracts_paragraphs_from_p_blocks` — only `wp-block-paragraph` paragraphs.
- `test_extracts_h5_headings_as_paragraphs` — h5 case labels are kept.
- `test_subtitle_is_first_paragraph` — same lead-extraction convention as lamley.
- `test_images_dedup_by_base_url` — the `?w=1024` and `?w=300` collapse.
- `test_youtube_iframe_wrapped_with_telegra_ph_proxy` — full URL test.
- `test_affiliate_quick_link_paragraph_filtered_out` — pre-LLM boilerplate filter integration.
- `test_carousel_gallery_flattens_to_image_list` — nested `<figure>` children.
- `test_video_only_post_returns_paragraphs_from_title` — fallback path.
- `test_missing_content_encoded_returns_none` — structural error.
- `test_html_entities_unescaped` — `&#8217;`-style.
- `test_image_limit_applied` — 20 imgs → 10 (`IMAGE_LIMIT`).

Optional second file `tests/fixtures/orangetrack_sample.xml` — a real
captured feed for end-to-end regression. Not strictly needed.

### Test mocking style — single-import, no network
Unlike lamley/mattel, **orangetrack does no HTTP** in its parser. Tests
just call `fetch_orangetrack_article(entry)` with a hand-built dict.
No `MagicMock(session)`, no patching `requests.get`, no `_no_real_sleep`
autouse fixture. Pure-functional testing.

---

## 8. Risks and gotchas

1. **WordPress Gutenberg block markup version drift**. The current
   classes are `wp-block-paragraph`, `wp-block-heading`, `wp-block-image`,
   `wp-block-embed`, `wp-block-gallery`, `wp-block-separator`. WP 6.x is
   stable on these — but orangetrack is on `wordpress.com` (per
   `<generator>http://wordpress.com/</generator>` in the feed XML), so
   the platform may auto-upgrade. **Mitigation**: don't rely on exact
   class names; walk by tag (`p`, `h2`–`h5`, `figure`, `iframe`) and
   **fall back to bare-tag selectors** (`<p>` without class). Lamley's
   parser style — agnostic to class names — is the model.

2. **Inline affiliate sentences in real paragraphs** — lines 79, 216, 718
   of the feed:
   - `"1 Stop Diecast is sold out of these cases! Fortunately, I was able to unbox..."` (line 79) — this is **contextual content**, NOT a plug. Should be kept.
   - `"...Use the link below to get yours!"` (line 216) — soft inline
     plug, sentence-final. Only catchable by variant-B-style mid-paragraph
     surgery.
   - `"...Check the link below to order your own Pop Culture 'Q' case from 1 Stop Diecast."` — same.
   **Recommendation per cycle-1 description**: **keep the whole
   paragraph as-is** for inline plugs in this feature. Variant A
   pattern only matches standalone `^QUICK LINK!` paragraphs. Inline
   plugs are tolerated; if they become a problem, they're a follow-up
   for the author-plug-filter variant B (when that lands).

3. **Carousel/gallery flattening** — orangetrack wraps galleries:
   ```html
   <figure class="wp-block-gallery has-nested-images">
     <figure class="wp-block-image"><a><img src="...?w=1024"/></a></figure>
     <figure class="wp-block-image"><a><img src="...?w=1024"/></a></figure>
     <figure class="wp-block-image"><a><img src="...?w=1024"/></a></figure>
   </figure>
   ```
   BS4's `soup.find_all("img")` recursively walks **into** nested
   figures, so the flat image list is correct. **No special handling
   needed** for `wp-block-gallery`.

4. **Video-only / minimal-paragraph posts** — possible. Sample top
   item has 8+ paragraphs; older posts might be just 1 paragraph + 1
   video. `news_bot.py:1510` skips entries with empty `paragraphs`,
   so a post that resolves to `paragraphs=[]` is silently dropped.
   **Mitigation**: synthesize `paragraphs=[entry.title]` when body
   yields zero paragraphs.

5. **'Case Report' cross-references** — the live sample has
   `<a href="...category/case-report/">Case Report</a>` linking back
   to a category page. This anchor lives **inside** a normal
   paragraph and adds no noise — `BS4.get_text()` flattens to plain
   prose. **No action needed**.

6. **Orphan paragraphs** — `<p class="has-text-align-center wp-block-paragraph"></p>`
   (line 133 of feed: empty body). BS4 `get_text(strip=True)` returns
   `""`. The current lamley pattern (line 343) skips empties via
   `if text and text != title:` — adopt the same.

7. **Title from RSS** — `entry.title` from feedparser is **already
   HTML-entity decoded** (`&#8220;` → `"`). No extra `html.unescape`
   needed for the title.

8. **`<hr class="wp-block-separator">`** — not a `<p>` and not in any
   selector list, so it's silently ignored. Good.

9. **Bare brand mentions** — paragraphs like "1 Stop Diecast" or
   "Jcar Diecast" appearing as plain text (not links). Not boilerplate,
   real content. Boilerplate filter doesn't touch them.

10. **Feed XML namespace prefixes** — `<content:encoded>` is namespaced.
    feedparser exposes it as `entry.content` (a list). Don't try to
    parse the raw XML manually — let feedparser handle namespaces.

11. **RSS feed staleness / outage** — the `_fetch_rss_entries` fetcher
    already has per-URL try/except (`news_bot.py:1318–1322`). One feed
    failing doesn't abort the rest. **No new error handling needed**
    at the registry level. Internal `fetch_orangetrack_article`
    structural failures (no `entry.content`, BS4 parse error) should
    return `None` so the entry is skipped per `news_bot.py:1510`.

12. **`<dc:creator>` is always "Brad"** — solo blog. Useful as a
    sanity-check assertion in tests but no production impact.

13. **`SOURCE_EMOJI` / `SOURCE_LABEL` ordering** — both dicts are
    plain dicts; insertion order is preserved (Python 3.7+) but no
    code depends on it. Adding `'orangetrack'` at the end is safe.

14. **`tests/test_sources_registry.py::TestNetlocToSource::test_has_exactly_the_five_keys`**
    will **break** when orangetrack adds 2 keys (it asserts on a
    closed set). Must be updated to 7 keys.

---

## 9. Sample article anatomy (live capture, 2026-05-04)

Source: top item of `https://orangetrackdiecast.com/feed/` (post ID
35264, "Hot Wheels 2026 Car Culture / Team Transport 'K' CASE
REPORT", 2026-05-02). Saved to `/tmp/orangetrack_feed.xml` during
research. Representative `<content:encoded>` body (40 lines, abridged):

```html
<p class="wp-block-paragraph">Hot Wheels Car Culture / <a href="https://orangetrackdiecast.com/tag/car-culture-team-transport/" target="_blank" rel="noreferrer noopener">Team Transport</a> debuts three new sets with the K case (2026 mix 2). Check out my thoughts below and/or feel free to watch the unboxing which is also below.</p>

<h5 class="wp-block-heading has-text-align-center"><mark style="background-color:rgba(0, 0, 0, 0)" class="has-inline-color has-luminous-vivid-orange-color"><strong>#89</strong> Mercedes-Benz 300 SLR / Mercedes-Benz Renntransporter</mark></h5>

<p class="wp-block-paragraph">New casting alert! The Mercedes-Benz 300 SLR looks stellar in silver and features some amazing details &#8212; specifically in the interior...</p>

<h5 class="wp-block-heading has-text-align-center"><mark ...><strong>#90</strong> &#8217;24 Ford Mustang RTR Spec 5-FD / Aero Lift</mark></h5>

<p class="wp-block-paragraph">RTR is back in Team Transport!...</p>

<p class="wp-block-paragraph"><a href="https://www.1stopdiecast.com/" ...>1 Stop Diecast</a> is sold out of these cases! Fortunately, I was able to unbox one of these 4-count cases on the <a href="https://www.youtube.com/channel/...">OTD YouTube Channel</a>...</p>

<p class="has-text-align-center wp-block-paragraph"><strong><a href="https://www.1stopdiecast.com/HOT-WHEELS-2026-CC-TEAM-TRANSPORT-RELEASE-K-..." target="_blank" rel="noreferrer noopener"><em>QUICK LINK!</em> Buy the Hot Wheels Car Culture / Team Transport K case from 1 Stop Diecast now.</a></strong></p>

<figure class="wp-block-embed is-type-rich is-provider-embed-handler wp-block-embed-embed-handler wp-embed-aspect-16-9 wp-has-aspect-ratio"><div class="wp-block-embed__wrapper">
<iframe class="youtube-player" width="1300" height="732" src="https://www.youtube.com/embed/ZaDCHaa9MnM?version=3&#038;rel=1&#038;showsearch=0&#038;showinfo=1..." allowfullscreen="true" style="border:0;" sandbox="..."></iframe>
</div></figure>

<hr class="wp-block-separator has-alpha-channel-opacity" />

<p class="wp-block-paragraph">Hot Wheels <a href="https://orangetrackdiecast.com/tag/car-culture-team-transport/" ...>#Car Culture / Team Transport</a> cases (FLF56) contain 4 sets (8 vehicles)...</p>

<p class="wp-block-paragraph">#89 &#8212; <strong>Mercedes-Benz 300 SLR</strong> / <strong>Mercedes-Benz Renntransporter</strong> (x1)<br>#90 &#8212; <strong>&#8217;24 Ford Mustang RTR Spec 5-FD</strong> / <strong>Aero Lift</strong> (x2)<br>#91 &#8212; <strong>&#8217;66 Chevrolet Corvair Yenko Stinger</strong> / <strong>&#8217;72 Chevy Ramp Truck</strong> (x1)</p>

<figure data-carousel-extra='...' class="wp-block-gallery has-nested-images columns-default is-cropped wp-block-gallery-1 is-layout-flex">
<figure class="wp-block-image size-large"><a href="https://orangetrackdiecast.com/wp-content/uploads/2025/11/jhx90_..._classicmercedes.jpg"><img width="1024" height="837" data-attachment-id="34108" src="https://orangetrackdiecast.com/wp-content/uploads/2025/11/jhx90_..._classicmercedes.jpg?w=1024" srcset="...?w=1024 1024w, ...?w=150 150w, ...?w=300 300w, ...?w=768 768w, ... 1200w" sizes="(max-width: 1024px) 100vw, 1024px"/></a></figure>
<figure class="wp-block-image size-large"><a href="..."><img src="...?w=1024" srcset="..."/></a></figure>
<figure class="wp-block-image size-large"><a href="..."><img loading="lazy" src="...?w=1024" srcset="..."/></a></figure>
</figure>

<hr class="wp-block-separator has-alpha-channel-opacity" />

<p class="has-text-align-center wp-block-paragraph">To catch up with the previous mixes of Team Transport for 2026, check out the videos below.</p>

<figure class="wp-block-embed is-type-video is-provider-youtube wp-block-embed-youtube wp-embed-aspect-16-9 wp-has-aspect-ratio"><div class="wp-block-embed__wrapper">
<iframe loading="lazy" src="https://www.youtube.com/embed/3X4QRRziJxU?..."></iframe>
</div></figure>

<p class="has-text-align-center wp-block-paragraph"></p>
```

Header structure (item-level):
- `<title>` — Hot Wheels 2026 Car Culture / Team Transport "K" CASE REPORT
- `<link>` — https://orangetrackdiecast.com/2026/05/02/...
- `<dc:creator>` — Brad
- `<pubDate>` — Sat, 02 May 2026 15:30:00 +0000
- `<category>` (multiple, e.g. "Case Report", "2026 Hot Wheels", "Car Culture")
- `<description>` — short excerpt (mirrors `<title>` + first paragraph)
- `<content:encoded>` — full body HTML (above)

---

## 10. Test hooks needed

### Libraries
- `feedparser` — already in `requirements.txt`. Build entry dicts
  manually OR call `feedparser.parse(<XML string>)` and pluck the
  first entry. The existing test pattern (`test_sources_registry.py:173`
  uses `feedparser.parse(tiny_rss)`) is reusable.
- `bs4.BeautifulSoup` — already in `requirements.txt`. Used inside
  the parser; in tests, asserted indirectly via output dict.
- `requests` — **NOT used** in the orangetrack parser (no HTTP).
  No mocking needed. (Tests for `_fetch_rss_entries` integration with
  orangetrack feed are already covered in
  `test_sources_registry.py::TestFetchRssEntries`.)

### Project mocking patterns to reuse
- `tests/conftest.py:1-14` — auto-injects repo root into `sys.path`.
  Just `import orangetrack_source` at the top of the new test file.
- `_make_response` helper from `test_lamley_source.py:37-47` —
  **NOT needed** (no HTTP).
- `_make_entry(content_encoded=..., title=..., link=...)` — local
  test helper, build it inline. No project precedent.
- `MagicMock(spec=requests.Response)` — **NOT needed**.
- `monkeypatch` — **NOT needed** for parser tests. Only relevant if
  testing the `news_bot` registry integration end-to-end (orangetrack
  feed in `feeds.json`, dispatcher routing through
  `fetch_full_article`); that's already covered by existing
  `test_sources_registry.py` patterns.

### Skipped concerns (not needed for this feature)
- No `time.sleep` mock — no throttle.
- No WAF state reset — no WAF.
- No SSRF allowlist test — no HTTP.
- No retry/backoff test — no transport layer.

### Recommended test imports
```python
import pytest
from unittest.mock import MagicMock  # only if testing fetcher injection — likely not needed

import orangetrack_source
from orangetrack_source import fetch_orangetrack_article  # main public API
```

The whole test suite for orangetrack should fit in ~150-200 LoC.

---

## Summary checklist (for implementer)

- [ ] Create `orangetrack_source.py` with `fetch_orangetrack_article(entry)` (~150 LoC).
- [ ] Add feed URL to `feeds.json`.
- [ ] Edit `news_bot.py`:
  - [ ] line 36 area — `import orangetrack_source`
  - [ ] lines 745–751 — extend `NETLOC_TO_SOURCE` (+2 keys)
  - [ ] lines 777–786 — extend `SOURCE_EMOJI` and `SOURCE_LABEL` (+1 key each)
  - [ ] lines 1265–1286 — add domain branch in `fetch_full_article`
- [ ] Edit `boilerplate_filter.py:51-65` — add affiliate-line regex.
- [ ] Edit `deploy.sh:37-57` — add `"orangetrack_source.py"`.
- [ ] Edit `.github/workflows/deploy.yml:117-137` — add `"orangetrack_source.py"` (mirror).
- [ ] Create `tests/test_orangetrack_source.py` (~150-200 LoC).
- [ ] Update `tests/test_sources_registry.py::TestNetlocToSource` set assertions (5→7 keys, 3→4 source-name values).
- [ ] No changes to: `SOURCES` registry, `pending_articles_repo`, DB migrations, env vars, CI config beyond `deploy.yml`, ux-guidelines.md.
