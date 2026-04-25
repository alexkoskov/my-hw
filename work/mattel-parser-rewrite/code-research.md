# Code Research: mattel-parser-rewrite

Date: 2026-04-24. Research pass 1 (fresh file).

---

## 1. Function contract (current)

File: `/workspaces/debian-2/my-hw/mattel_news_source.py` (217 lines total).

### Module constants
- `NEWS_URL = "https://corporate.mattel.com/news"` (line 21)
- `ARTICLE_URL_PREFIX = "https://corporate.mattel.com/news/"` (line 22)
- `USER_AGENT` — Chrome/120 desktop (lines 23-26)
- `REQUEST_TIMEOUT = 15` (line 27)
- `MAX_RESPONSE_SIZE = 5 * 1024 * 1024` — 5 MB guard (line 28)
- `_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.DOTALL)` (lines 30-33)
- `class MattelNewsError(Exception)` (line 36)

### `fetch_mattel_news(url=NEWS_URL, session=None, notifier=None) -> List[Dict]`
Lines 92-131. Signature:
```python
def fetch_mattel_news(
    url: str = NEWS_URL,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
```

Flow:
1. HTTP GET with UA + 15s timeout. `raise_for_status()`. Refuse > 5 MB.
2. `_extract_entries(response.text)` — regex-match `__NEXT_DATA__`, `json.loads`, return `data["props"]["pageProps"]["page"]["data"]["state"]["article2"]["entries"]`.
3. Filter each raw via `_is_hotwheels` (line 40-43): `"hot wheels" in title.lower() or "hot-wheels" in handle.lower()`.
4. Build entry via `_build_entry` (lines 46-71).

Returned entry dict keys (verbatim, line 65-71):
```
{
    "link":             ARTICLE_URL_PREFIX + handle,   # str
    "title":            raw["title"],                   # str
    "summary":          excerpt or title,               # str; excerpt.get("text") if excerpt is dict
    "published_parsed": time.strptime(date, "%Y-%m-%d") or None,  # time.struct_time | None
    "feed_url":         NEWS_URL,                       # str
}
```

Error behavior:
- `requests.RequestException` → `_notify(...)` with `"Mattel news HTTP error: …"` → return `[]`.
- `MattelNewsError` → `_notify(...)` with `"Mattel news parsing error: …"` or `"Response too large: …"` → return `[]`.
- `_notify` catches any notifier exception itself (line 138-141) — caller never sees a raise.

### `fetch_mattel_article(link, session=None, notifier=None) -> Optional[Dict]`
Lines 144-216. Signature matches above.

Flow:
1. HTTP GET + 5 MB guard.
2. Regex `_NEXT_DATA_RE`, `json.loads`, read `data["props"]["pageProps"]["contentArticle"]`.
3. If `contentArticle is None` → `MattelNewsError("contentArticle is null (article unavailable or 404)")` → return `None`.
4. `article = content_article["result"]`. Parse `article["body"]` HTML with `BeautifulSoup("html.parser")`. Collect paragraphs from tags `["p", "li", "h1", "h2", "h3", "h4"]` via `get_text(" ", strip=True)`.
5. Thumbnail-only image policy: `images = [content_article["thumbnail"]["url"]]` if present. `download_media` ignored by design (lines 192-204 comment).
6. `excerpt = article.get("excerpt") or ""` (dict → `.get("text")` fallback, lines 207-209).

Returned dict (lines 211-216):
```
{
    "title":      article.get("title", ""),   # str
    "subtitle":   excerpt_str,                # str (from excerpt field)
    "paragraphs": List[str],                  # parsed from body HTML
    "images":     List[str],                  # 0 or 1 url (thumbnail only)
}
```

Error umbrella (line 178-181): `requests.RequestException | json.JSONDecodeError | KeyError | TypeError | MattelNewsError` → `_notify` + return `None`.

### Shape summary for the rewrite
- `fetch_mattel_news` contract is FIVE keys: `link`, `title`, `summary`, `published_parsed`, `feed_url`. The rewrite MUST preserve them.
- `fetch_mattel_article` contract is FOUR keys: `title`, `subtitle`, `paragraphs`, `images`. The rewrite MUST preserve them.
- Both functions must still accept `session` and `notifier` kwargs (used by tests and by `news_bot.job()`).

---

## 2. Downstream usage

### news_bot.py registry and dispatch
- `/workspaces/debian-2/my-hw/news_bot.py:24` imports `fetch_mattel_news, fetch_mattel_article`.
- `_fetch_mattel_entries(notifier=None)` at `news_bot.py:1037-1047` — thin wrapper that stamps `item['source_name'] = 'mattel'` on each entry. Calls `fetch_mattel_news(notifier=notifier) or []`.
- `SOURCES = [_fetch_rss_entries, _fetch_mattel_entries]` at `news_bot.py:1053-1056` — iterated in `job()` at `news_bot.py:1164`.
- `fetch_full_article(entry)` at `news_bot.py:955-976` dispatches by domain: `'corporate.mattel.com' in domain → fetch_mattel_article(link, notifier=send_admin_notification)`.

### Source-name resolution
- `NETLOC_TO_SOURCE` at `news_bot.py:448-454` maps `'corporate.mattel.com' → 'mattel'`.
- `_resolve_source_name(link)` at `news_bot.py:457-472` — fallback mechanism when an entry arrives without a `source_name`. Returns `'mattel'` for any `corporate.mattel.com` URL. Used at line 883 (overflow fast-track) and line 1238 (prep insert).
- Hashtag (`news_bot.py:424-433`): `_source_hashtag("https://corporate.mattel.com/news/foo")` → `#mattel` (TLD-stripping from netloc).
- `SOURCE_EMOJI['mattel'] = '🟣'` and `SOURCE_LABEL['mattel'] = 'mattel'` at lines 482, 487.
- `_ADMIN_PING_ORDER = ('autoevolution', 'mattel', 'lamley')` at line 494.

### Entry fields actually consumed downstream
Looking at `job()` lines 1236-1246 which builds the row dict for `insert_pending`:
```
row = {
    'link':         link,                                       # required
    'source_name':  entry.get('source_name') or _resolve_source_name(link),
    'feed_url':     entry.get('feed_url'),
    'title':        article.get('title') or entry.get('title') or '',
    'subtitle':     article.get('subtitle') or '',
    'paragraphs':   article.get('paragraphs') or [],
    'images':       article.get('images') or [],
    'blocks':       article.get('blocks'),                     # None for Mattel
    'pub_date':     entry.get('published') or entry.get('pub_date') or '',
}
```

- `entry['link']` — used as dedup key (`filter_new_entries` at line 287-297) and as `pending_articles.link` PRIMARY KEY.
- `entry['title']` — fallback if `article.get('title')` is empty.
- `entry['summary']` — **NOT** consumed anywhere in `job()`. Grep confirms: only `fetch_mattel_news` writes it; `news_bot` reads only `title`, `link`, `published`, `feed_url`, `source_name`. The rewrite could set `summary = ""` and downstream doesn't care — but keeping the contract intact avoids surprising the unit tests and any future consumer.
- `entry['published_parsed']` — **NOT** consumed by `job()`. `pub_date` is built from `entry.get('published')` (string, from feedparser), not `published_parsed` (struct_time). Only `get_latest_article.py:42` and `post_latest_news.py:84` read `published_parsed`, and both are archive/one-shot scripts, not cron-driven. So the Mattel rewrite could drop `published_parsed` without breaking production — but the unit test at `tests/test_mattel_news_source.py:76` and `:131` asserts on it, so keep it to avoid test churn.
- `entry['feed_url']` — stored but only used for log context (`news_bot.py:1026`).

### Downstream fallback when `fetch_mattel_article` returns None
`news_bot.py:1231-1234`:
```python
article = fetch_full_article(entry)
if not article or not article.get('paragraphs'):
    logger.warning(f"No article data for {link}, skipping")
    continue
```
Entry is silently skipped for this tick. Next tick re-fetches from source and retries (not dedup-filtered until successfully inserted into `pending_articles` OR `processed_news`). So returning `None` is safe but wasteful.

### pending_articles_repo
`/workspaces/debian-2/my-hw/pending_articles_repo.py:166-199` `insert_pending` — writes columns `(link, source_name, feed_url, title, subtitle, paragraphs, images, blocks, pub_date)`. JSON-serializes `paragraphs`/`images`/`blocks` via `json.dumps(..., ensure_ascii=False)`. No read of `published_parsed` or `summary`.

---

## 3. Existing tests

Test runner: `pytest` (9.0.3 cached bytecode). Two files touch Mattel:

### `/workspaces/debian-2/my-hw/tests/test_mattel_news_source.py` (337 lines)

Fixture: `tests/fixtures/mattel_news.html` loaded once per module at line 29-32 via `@pytest.fixture(scope="module")`.

Mock pattern: `_make_response(text, status_code, raise_exc)` at lines 35-44 returns a `MagicMock(spec=requests.Response)`. Session is injected via `session=MagicMock()`, `session.get.return_value = resp`. This bypasses `requests` entirely — rewriting the parser logic will not affect these mocks.

Tests (per class):

**TestIsHotwheels** (lines 47-61) — 5 tests; pure unit on `_is_hotwheels({title, handle})`. Survives rewrite IF `_is_hotwheels` keeps its (dict) → bool shape. Safe to keep or rename as an internal helper.

**TestBuildEntry** (lines 64-92) — 5 tests; tests `_build_entry` produces the 5-key dict (line 73-77), including `published_parsed == time.strptime(...)`. If the rewrite keeps `_build_entry` with the same signature + output, these pass untouched. If we inline entry construction or remove `published_parsed`, these tests must be rewritten.

**TestExtractEntries** (lines 95-116) — 4 tests; target `_extract_entries(html)` and specifically reference `__NEXT_DATA__` in error strings (line 104, 108, 114). **These will break on the rewrite** — the new parser will not look for `__NEXT_DATA__`. Tests must be updated to reference RSC flight structure (e.g. "flight payload not found", "article2.entries not found").

**TestFetchMattelNews** (lines 119-221) — 9 tests. High-level behavior: empty list on any error, single notifier call per error, `published_parsed is not None` on success (line 131), correct filter (line 126 — expects exactly 1 HW entry from fixture), oversized guard (line 202-213).
- **Survives the rewrite if** the new parser produces at least 1 HW entry from the fixture. The CURRENT fixture has HW content; the NEW fixture (live snapshot of 2026-04-24) does NOT — zero HW entries in today's listing. Either we (a) craft a fixture that embeds a synthetic HW entry, or (b) combine the real-listing fixture with a specific Wayback snapshot that had HW content.
- Error-path tests use hand-crafted bad HTML (e.g. `<script id="__NEXT_DATA__">{bad json}</script>`). They will break because the new parser won't react to the `__NEXT_DATA__` string. Must be rewritten to produce RSC-flight-shaped bad input.

**TestFetchMattelArticle** (lines 224-333) — 7 tests. `_article_page(...)` helper at lines 225-249 builds synthetic `__NEXT_DATA__` HTML inline. **Entire class must be rewritten** — new fixtures must produce flight-payload-shaped HTML. Tests that must still pass in spirit:
- parses paragraphs from `<p>`, `<li>` in body (line 268).
- thumbnail-only image policy (lines 251-270, 272-281) — critical, the dominant policy decision for this module.
- missing excerpt → empty subtitle (line 283-287).
- 404 / null article → `None` + readable notification (line 315-332). On the new flight site, a 404 does NOT contain `article2.entries` at all (verified live at `/workspaces/debian-2/my-hw` during research); the rewrite must detect this specifically.

### `/workspaces/debian-2/my-hw/tests/test_mattel_integration.py` (156 lines)

Integration via `unittest.TestCase`. `setUp` (line 31-46) creates a tempfile SQLite, patches `news_bot.DB_FILE`, and sets mock Telegram tokens. Key fixture: loads `tests/fixtures/mattel_news.html` into `self.fixture_html` at line 32-33.

Patches per test:
- `mattel_news_source.requests.get` (mocks HTTP at the source module level — so `fetch_mattel_news` actually runs its parser against the fixture).
- `news_bot.fetch_mattel_article` (mocks article fetch — tests don't load an article-page fixture).
- `news_bot.fetch_rss` and `news_bot.load_feeds` → return `[]` (RSS disabled).
- `news_bot.send_admin_notification`, `news_bot.telegraph_publisher.publish_article`, `news_bot.send_telegraph_teaser`, `news_bot.transcreate_text`.

Three tests:

**test_mattel_post_flows_into_pending_queue** (line 73-104) — Asserts `len(pending_articles) == 1`, `source_name == 'mattel'`, `'corporate.mattel.com/news/' in link`, `'hot-wheels' in link`. **Will break** when fixture is swapped to the live 2026-04-24 listing because that listing has zero HW entries. Must change fixture to one containing HW content (either synthetic, or a Wayback HW snapshot, or merged).

**test_mattel_http_failure_does_not_crash_job** (line 106-122) — Only asserts that `send_admin_notification` is called and no row is staged. This test is parser-agnostic and will survive untouched.

**test_mattel_duplicate_is_not_restaged** (line 124-151) — Also relies on the fixture producing 1 HW entry. Same fixture dependency as test 1.

### Other tests that touch `fetch_mattel_news`
- `tests/test_feed_iteration.py`, `tests/test_integration.py`, `tests/test_sources_registry.py` — all patch `news_bot.fetch_mattel_news` to `return_value=[]` so they don't hit the network. Parser-agnostic; no changes needed for the rewrite.

---

## 4. Fixtures

### `/workspaces/debian-2/my-hw/tests/fixtures/mattel_news.html`
- Size: 1,158,890 bytes (~1.10 MB), saved 2026-04-20.
- Format: OLD `<script id="__NEXT_DATA__" type="application/json">...</script>` — the site used this format as recently as 2026-04-21 (verified via Wayback).
- Content: 7 news entries in `article2.entries`, exactly 1 matching Hot Wheels filter (per the 2026-04-20 decisions log, Task 2 verification).
- No article-page fixture exists on disk. Tests build synthetic article-page HTML inline (see `_article_page` helper in `test_mattel_news_source.py:225-249`).

### What we need for the rewrite

**Listing fixture (flight format).**
- Save a fresh flight-HTML snapshot from the live site. Size ~1.27 MB.
- To guarantee HW content, we can either:
  1. Hand-craft a synthetic RSC-flight listing page with 1-2 HW entries encoded in the biggest `self.__next_f.push([1, "..."])` chunk — requires fully re-escaping JSON-in-JS-string which is fiddly but doable programmatically.
  2. Save the current 7-entry listing verbatim, then edit the decoded payload to rename one entry's `handle` / `title` to contain "hot-wheels" / "Hot Wheels" and re-encode.
  3. Wait for a live HW release (timing unknown — last HW entry on live site indexed by Wayback is 2025-03-19 "2025 Hot Wheels Legends Tour").
- Option 1 or 2 is the realistic path. The fixture is used by 3 different tests (unit extract_entries, integration post_flows, integration duplicate) so it must produce exactly 1 HW entry to keep assertions simple.

**Article fixture (flight format).**
- Currently synthetic (built by `_article_page` in the unit test). Rewrite should either (a) keep synthetic but rebuild the helper for flight structure, or (b) save one real article-page snapshot. Option (a) is more maintainable because flight-payload escaping is mechanical — the helper can serialize a Python dict the same way the live pages do.
- Must cover: body HTML parses to paragraphs; thumbnail → images[0]; `download_media` present but IGNORED; excerpt → subtitle.

**404 fixture.**
- Live 404 response (captured at `/tmp/mattel_404.html`, 8,361 bytes): returns HTTP 404 with a short Next.js "Not Found" page. Has 5 `self.__next_f.push` calls but NO `article2` and NO `contentArticle` in the flight. The rewrite must detect this shape specifically.

---

## 5. Live RSC listing — verified structure

**Source:** `/tmp/mattel_news.html`, saved 2026-04-24, 1,274,273 bytes.

### Push chunk inventory
- 49 `self.__next_f.push([1, "..."])` calls total.
- 10 largest chunk sizes (chars of the encoded string literal): `1,109,992`, `14,046`, `11,640`, `10,923`, `8,074`, `5,322`, `4,496`, `4,336`, `4,331`, `4,316`. The biggest dwarfs everything else; it carries the full listing data.
- Each chunk begins with a row-number prefix, e.g. `6:[[\"$\",\"div\",null,...` — the integer is the RSC row id, followed by `:` then payload. The biggest chunk is row `6`.

### How to get clean JSON text out of the biggest chunk
Verified working sequence (from live file):
```python
import re, json
pattern = re.compile(r'self\.__next_f\.push\(\[\s*1\s*,\s*"(.+?)"\s*\]\)', re.DOTALL)
pushes = pattern.findall(html)
big = max(pushes, key=len)                        # pick the largest; size ~1.1 MB
prefix = re.match(r'^(\d+):', big)                # the "6:"
body = big[prefix.end():]                         # drop "6:"
unescaped = json.loads('"' + body + '"')          # interpret JS string escapes
# unescaped now contains literal JSON text 975,046 chars long
```
After this, the unescaped blob contains the listing payload in plain JSON escaping (single backslashes). The anchor `"article2":{"entries":[` exists at byte offset 957,018 in the unescaped text.

### Extracting `article2.entries`
The entries array can be sliced by bracket-matching on `[` starting after the `"entries":` key:
```python
idx = unescaped.find('"article2":{"entries":[')
arr_start = unescaped.find('[', idx)
depth = 0
for i in range(arr_start, len(unescaped)):
    c = unescaped[i]
    if c == '[': depth += 1
    elif c == ']':
        depth -= 1
        if depth == 0:
            arr_end = i + 1; break
entries = json.loads(unescaped[arr_start:arr_end])
# → list of 7 dicts
```

### Entry shape — verified keys on 2026-04-24
```
['uid', 'locale', '_version', 'ACL', '_in_progress', 'body', 'category',
 'created_at', 'created_by', 'date', 'download_media', 'excerpt', 'handle',
 'seo_description', 'seo_title', 'tags', 'thumbnail', 'title', 'updated_at',
 'updated_by', 'url', 'publish_details']
```

- `handle` — lowercase-hyphen slug (e.g., `engage-for-good-names-mattel-2026-halo-corporation-of-the-year`). Current `_is_hotwheels` filter (`"hot-wheels" in handle.lower()`) is compatible.
- `title` — plain human string (UTF-8, includes fancy quotes like `“Masters of the Universe”`).
- `date` — `YYYY-MM-DD` (e.g., `2026-04-20`). `time.strptime(date_str, "%Y-%m-%d")` still works.
- `excerpt` — empty string on all 7 current entries. `_build_entry` falls back to `title` for `summary` (line 55 current code).
- `seo_description` — the actual user-facing description field. Note: old `__NEXT_DATA__` structure ALSO had `seo_description` (confirmed in Wayback HW article). Rename note: user-spec said `seo_desc` — the real field name is `seo_description`.
- `body` — `"$62"` etc — RSC reference to another row id. NOT inline HTML on the listing. Reflects that the listing does not hydrate article bodies. Listing entries contain ONLY metadata.
- `download_media` — present even in listing (empty list on most current entries; non-empty on the 2026-04-13 HALO entry).
- `thumbnail` — full asset dict (same structure as the old-format listing) with `url` field pointing at contentstack CDN.

### Handles in today's listing (2026-04-24)
7 unique, 0 Hot Wheels:
1. `mattels-masters-of-the-universe-elevates-desert-festival-billboards-with-first-ever-drone-display` — 2026-04-20
2. `engage-for-good-names-mattel-2026-halo-corporation-of-the-year` — 2026-04-13
3. `mattel-announces-first-quarter-2026-financial-results-and-conference-call-date` — 2026-04-09
4. `mattel-announces-departure-of-steve-totzke-president-and-chief-commercial-officer-and-promotion-of-sanjay-luthra-to-lead-global-commercial-organization` — 2026-04-07
5. `mattel-and-amazon-mgm-studios-debut-official-trailer-for-masters-of-the-universe-exclusively-in-theaters-june-5-2026` — 2026-03-31
6. `mattel-unveils-full-masters-of-the-universe-product-line-ahead-of-highly-anticipated-live-action-film` — 2026-03-24
7. `mattel-presents-at-2026-ubs-global-consumer-and-retail-conference-to-discuss-strategy-and-outlook` — 2026-03-13

(A duplicate of entry 1 appears as a "featured" card outside `article2.entries`, giving the raw handle count of 8. Pinning to `article2.entries` avoids this duplication.)

### Alternative regex (if someone wanted to skip bracket-matching)
The double-escaped patterns in the RAW HTML (before unescaping) ARE extractable directly:
- `\\"handle\\":\\"([a-z0-9-]+)\\"` — matches 8 handles.
- `\\"date\\":\\"(\\d{4}-\\d{2}-\\d{2})\\"` — matches 8 dates.
- `\\"title\\":\\"([^\\\\]+)\\"` — matches 21 titles (polluted by thumbnail asset titles like `Mattel_Logo.png`).
- `\\"excerpt\\":\\"([^\\\\]*)\\"` — matches 8.
- `\\"seo_description\\":\\"...\\"` — matches 8 as well.

The key problem with naive regex on the RAW HTML: titles like `Mattel_Logo.png` from thumbnail.title get caught, and binding fields to entries by proximity is fragile. **Recommended approach: unescape once, then walk JSON via `"article2":{"entries":[` anchor + bracket-match.**

---

## 6. Live RSC article page — verified structure

**Source:** `/tmp/mattel_article.html` (fetched live 2026-04-24 via curl of `engage-for-good-names-mattel-2026-halo-corporation-of-the-year`). Size 1,031,996 bytes. 45 `self.__next_f.push` calls.

### Biggest chunk is the page data
Row `6`, ~891 KB. After unescaping: 781,724 chars.
- Does NOT contain `contentArticle` (the old Pages-Router key). **Do not search for `contentArticle` — it no longer exists.**
- Contains `article2` with `entries` array (same as listing page — article pages still load the sidebar listing, and the specific article's metadata is one of the entries).
- Contains `"result":"$6:1:props:content:data:state:article"` (a reference to a different row in the stream — this is the React server "lazy" reference to the actual article body). The payload itself is NOT inline at this key.

### Locating the current article's metadata
The single article's listing-shaped entry IS present in `article2.entries` on the article page (verified: the `engage-for-good...` handle object with all 22 keys appears with `body: "$54"`). This gives us everything the listing gave us, PLUS a reference to the body row.

### Body HTML is in a separate RSC row (text row)
`body: "$54"` means "fetch row 54 from the flight stream." Row 54 is declared via:
```
54:T1c2a,
```
Where `T<hex>,` is the RSC text-row marker — `T` = text, `1c2a` = hex length `7210`, `,` = separator. The actual 7,210-char body text is streamed in SUBSEQUENT pushes, starting from the push that begins with `54:T1c2a,` and continuing chunk-by-chunk until the advertised length is reached.

Practical algorithm (verified working):
```python
# 1) unescape + concatenate ALL pushes (not just the biggest)
# 2) scan for row "54:T<hex>,<content>" — content may wrap to next push
# 3) read exactly <hex-length> chars of content; that's the body HTML
```
Reconstructed body for the test article contained valid HTML with `<p>`, `<strong>`, `<a href="...">`, `<img src="...">` — BeautifulSoup parsing works identically to the old approach: `for tag in soup.find_all(["p", "li", "h1", "h2", "h3", "h4"])` yields correct paragraphs.

Body head sample (verified): `<img src="..."/><p><strong>PALM SPRINGS, Calif. — April 13, 2026 —</strong> <a href="https://engageforgood.com/">Engage for Good</a> today named <a href="https://corporate.mattel.com/">Mattel, Inc</a>. ...</p>`.

### Thumbnail
Article page entry has `thumbnail.url = "https://images.contentstack.io/v3/assets/.../HALO_Award_.png"` — same structure as the old format. Thumbnail-only image policy remains applicable.

### download_media
Article page's entry has a non-empty `download_media` list (2 assets for the HALO example). The current thumbnail-only policy (mattel_news_source.py:192-204) explicitly ignores this field and should continue to do so.

### 404 signal
Live 404 response (`/tmp/mattel_404.html`, 8,361 bytes, HTTP 404):
- 5 `self.__next_f.push` chunks but tiny (small "Not Found" nextjs error page).
- No `article2` substring.
- No `contentArticle` substring.
- `"Not Found"` literal present in first 5 KB.

**New 404 signal for the rewrite:** `"article2"` not found in the unescaped flight payload, OR the handle's entry object cannot be located there. The old "`contentArticle is null`" signal is gone — the new 404 page simply omits the whole structure.

Side note: `response.raise_for_status()` will already raise `HTTPError` on status 404, which the current exception handler catches and produces a `"Mattel article fetch error ..."` notifier message. The rewrite can lean on `raise_for_status` for status-404 specifically; the "article not in flight" check is a belt-and-suspenders guard against 200-with-no-data which shouldn't happen on the new site but could on proxy/CDN edges.

---

## 7. Wayback viability for archival HW article snapshot

### CDX query results
- News listing URL has hundreds of Wayback snapshots from 2025-01 through 2026-04-21.
- Individual article URLs: only one HW article is archived — `corporate.mattel.com/news/2025-hot-wheels-legends-tour-opens-vehicle-submissions-in-the-global-hunt-for-the-next-hot-wheels-die-cast`. Seven snapshot timestamps in 2025 (earliest: `20250410060754`, latest: `20250523043424`).

### Format of Wayback snapshots
**Critical finding:** All Wayback snapshots of `corporate.mattel.com/news*` tested (2025-01 through 2026-04-21) serve OLD `__NEXT_DATA__` format — the App Router migration happened between 2026-04-21 (last Wayback crawl with `__NEXT_DATA__`) and 2026-04-24 (live site today serves `self.__next_f.push` with no `__NEXT_DATA__`).

Tested:
- `20250401082014/corporate.mattel.com/news` (1.22 MB) — `__NEXT_DATA__` present, 8 `handle` matches, 0 `next_f.push`.
- `20260119143048/corporate.mattel.com/news` (1.24 MB) — `__NEXT_DATA__` present, 8 handles, 0 `next_f.push`.
- `20260421065151/corporate.mattel.com/news` (1.20 MB) — `__NEXT_DATA__` present, 7 entries in `article2.entries`, same handles as live flight payload. No Hot Wheels.
- `20250501065732/corporate.mattel.com/news/2025-hot-wheels-legends-tour-...` (18,239 bytes) — `__NEXT_DATA__` present, `contentArticle` present, `result.body` is 7,312 chars of valid HTML with `<p><strong>EL SEGUNDO, Calif., March 19, 2025 —</strong>...`.

### Implication for fixtures
Wayback is NOT a source of flight-format HTML (at least not yet — wayback crawlers will presumably pick up the new format in days/weeks). It CAN provide:
- A snapshot of the old `__NEXT_DATA__` listing for regression tests if we want to keep a fallback parser for the old format. (Probably not worth it; user-spec says replace.)
- A snapshot of the HW article in OLD format. **NOT useful** as a fixture for the new parser — the new parser reads `self.__next_f.push`, not `__NEXT_DATA__`.

### Bottom line
We must construct the new fixtures from current live site HTML + hand-edited HW content, NOT from Wayback. The only value Wayback provides is cross-check that the CURRENT listing page schema (7-entry `article2.entries`, same field names) has been stable across the migration — it has. Field-level compatibility is confirmed.

---

## 8. Integration boundaries

### SOURCES registry
Verified via `tests/test_sources_registry.py:42-52`:
```python
assert set(NETLOC_TO_SOURCE) == {
    'www.autoevolution.com',
    'autoevolution.com',
    'lamleygroup.com',
    'www.lamleygroup.com',
    'corporate.mattel.com',
}
assert set(NETLOC_TO_SOURCE.values()) == {'autoevolution', 'lamley', 'mattel'}
```
The rewrite does NOT touch registry plumbing. As long as entries carry `link` starting with `https://corporate.mattel.com/news/`, `_resolve_source_name` resolves to `'mattel'` and the hashtag helper produces `#mattel`.

### Hashtag dispatch
`_source_hashtag(url)` at `news_bot.py:424-433` works on the NETLOC only (TLD-stripped). No change needed.

### pending_articles_repo DAO touchpoints
The rewrite only feeds `insert_pending` via `news_bot.job()`. Columns used: `link`, `source_name`, `feed_url`, `title`, `subtitle`, `paragraphs`, `images`, `blocks`, `pub_date`. The rewrite's only contribution is `title`/`subtitle`/`paragraphs`/`images` via `fetch_mattel_article`, plus `feed_url` via `fetch_mattel_news`. `pub_date` comes from `entry.get('published')` — which Mattel's `_build_entry` does NOT set, so it's empty string. Consistent with pre-migration behavior.

### External modules imported
`mattel_news_source.py` imports: `json`, `logging`, `re`, `time`, `typing`, `requests`, `bs4.BeautifulSoup`. All already in `requirements.txt` (`requests==2.32.3`, `beautifulsoup4==4.12.3`). **Zero new dependencies.**

### Load path for the module
Called from `news_bot.py:24` (top-level import). Not imported from anywhere else in production. Archive scripts don't import it. Safe to refactor internals as long as exports `fetch_mattel_news`, `fetch_mattel_article`, `MattelNewsError`, `NEWS_URL`, `ARTICLE_URL_PREFIX`, `MAX_RESPONSE_SIZE` are preserved (all of these are imported by the unit test file).

---

## 9. Historical constraints

### Thumbnail-only image policy
Source: `mattel_news_source.py:192-204` inline comment — anchored in the original review of the module. Rationale (paraphrased): `download_media` contains press-kit assets (often the same logo in multiple formats plus high-res press photos) that inflate the Telegraph page with imagery that isn't on the source article. Thumbnail matches the visible source layout 1:1. If inline imagery is ever added to Mattel articles, the right path is parsing `<img>` tags out of `body_html` — NOT surfacing `download_media`.

**Binding constraint:** The test `test_parses_paragraphs_and_uses_thumbnail_only` at `test_mattel_news_source.py:251-270` locks this behavior. The rewrite MUST continue to (a) read `thumbnail.url`, (b) ignore `download_media` entirely.

Note: the HALO article's body HTML on the live site contains one `<img src="..." alt="..." height="auto"/>` tag at the top. Under the current policy, this in-body image would be parsed OUT (only `["p", "li", "h1"-"h4"]` tags are walked for text), AND it would not appear in `images[]`. The only image surfaced is the thumbnail. Matches the existing policy — don't need to change anything.

### Other locked-in decisions (from `work/mattel-news-source/tech-spec.md`)
- **Decision 2: Filter logic** — substring match on `title` / `handle`. No NLP, no category filter. Kept in rewrite unchanged.
- **Decision 3: Feedparser-compatible entry format** — 5 specific keys. Kept in rewrite unchanged (even though `published_parsed` isn't consumed downstream in production, the test asserts on it).
- **Decision 4: Fail-soft with admin notification** — every error path calls `_notify` and returns empty/None. Kept.
- **Decision 5: No config file** — URL hardcoded. Kept.

### `work/mattel-parser-rewrite/decisions.md`
Empty (template comment only). No prior rewrite-specific decisions to honor.

---

## 10. Risks and unknowns

### Confirmed risks
1. **No current Hot Wheels content on the live listing.** Today's 7 entries are all non-HW. The unit-test `test_success_filters_hotwheels_only` (line 120-131) and both integration tests that assert `len(rows) == 1` REQUIRE the fixture to produce exactly 1 HW entry. We must either hand-edit a fixture to inject a synthetic HW entry, or construct a synthetic RSC-flight HTML from scratch. Hand-editing the flight payload is tedious because it's JSON escaped inside a JS string literal — every change requires re-escaping the whole entry. A Python fixture-builder helper is probably the cleanest path.

2. **RSC body reconstruction across push boundaries.** The body for one article is `T<len>,<text>` — text content MAY span multiple push chunks (verified: 7,210-char body needed concatenation of more than one push after the `T<len>,` marker). The rewrite must concatenate ALL pushes (or at least the sequence starting at the target row) before reading exactly `<len>` chars. A naive "pick the biggest push" heuristic works for the listing (metadata all in row 6) but NOT for article bodies (body text streams separately).

3. **Next.js may change chunk layout.** The push-number ordering, row IDs (`$62` etc.), and whether data is in the biggest chunk are all artifacts of this build. A future Next.js upgrade or app refactor could change how the data is split. The rewrite should anchor on semantic markers (`"article2":{"entries":[`, `handle`/`title`/`date` field names) rather than positional assumptions like "always row 6" or "always the biggest push".

4. **Empty `excerpt` on all current entries.** Current code falls back to `title` for `summary`. The new parser should preserve this. If the rewrite starts using `seo_description` as a richer alternative, downstream doesn't care (summary isn't consumed in production) — but that would be an intentional upgrade beyond the stated rewrite goal, adjacent to scope creep.

5. **Fixture file size.** The current fixture is 1.16 MB; the new flight-format HTML is 1.27 MB. Git-wise, not a concern (both are under 2 MB and both the old and new belong only on `dev`). Test load-time impact: `@pytest.fixture(scope="module")` already amortizes it across tests.

### Unknowns worth clarifying in Cycle 2 interview
1. Do we want the rewrite to emit `seo_description` as `summary`, or keep the original behavior of `excerpt or title` → `summary`? (The user-spec mentions `seo_desc` as an extractable field but doesn't say it replaces `excerpt`.)
2. Fixture strategy: hand-crafted-synthetic-flight vs. edited-real-snapshot. Both work; the former is more reproducible and lets us test edge cases (missing excerpt, null body, dict-shaped excerpt). Which does the user prefer?
3. Do we need a backward-compat parser for the OLD `__NEXT_DATA__` format as a safety net (e.g., in case the site flips back, or Wayback/mirror serves an older page)? Current live site gives 0% `__NEXT_DATA__`, so probably no — but worth an explicit call.
4. Should the rewrite replace `mattel_news_source.py` in place, or add a new module and switch the import? Same external contract either way; the former is simpler.
5. Article page: is reading the body HTML via RSC text-row reconstruction acceptable complexity, or does the user prefer a "tougher but simpler" approach like searching the entire concatenated unescaped flight for `<p>...</p>` runs that fall inside a known bounded region? The text-row approach is more correct; the brute-force scan is more robust against minor format drift.
6. What happens if a HW article's body ends up empty (e.g., Mattel publishes a pure-image press release)? Current code returns an article dict with `paragraphs=[]`, which `job()` then skips at line 1232 (`if not article or not article.get('paragraphs'): continue`). The rewrite will behave the same way. Is that the desired UX, or should we stage an image-only article with title+subtitle?

### Test pyramid impact estimate
- `tests/test_mattel_news_source.py`: ~40% of tests likely need updates (all of `TestExtractEntries` + ~half of `TestFetchMattelNews` error paths + all of `TestFetchMattelArticle`). The other ~60% (`TestIsHotwheels`, `TestBuildEntry`, success paths) survive unchanged if we keep the helper signatures.
- `tests/test_mattel_integration.py`: 2 of 3 tests need a new fixture; all 3 pass with minimal mocking changes.
- `tests/test_sources_registry.py`, `tests/test_feed_iteration.py`, `tests/test_integration.py`: zero changes expected.
