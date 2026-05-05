---
created: 2026-05-05
status: draft
branch: dev
size: M
---

# Tech Spec: orangetrack-source

## Solution

Add a 4th news source — `orangetrackdiecast.com` (Brad Bannach's solo Hot Wheels blog, WordPress.com hosted, EN, Hot Wheels-only). Implementation:

- New module **`orangetrack_source.py`** (~270-320 LoC) — feed-first parser. Primary path: extract article body from RSS `<content:encoded>` (BS4 walk by HTML tag, not Gutenberg classes). Fallback path: bounded-streaming HTTP GET of the article URL when `content:encoded` is missing/empty, with `allow_redirects=False`. Returns `{title, subtitle, paragraphs, images, blocks}` — `blocks` list preserves original ordering of paragraphs / images / YouTube videos / h5 headings, mirroring the autoevolution dual-contract.
- **SSRF allowlist** (`_is_allowed_orangetrack_url`) inside `orangetrack_source.py` — exact host match against `('orangetrackdiecast.com', 'www.orangetrackdiecast.com')`. Mandatory first check before any HTTP fetch (mirrors lamley pattern at [`lamley_source.py:212-225`](../../../lamley_source.py#L212-L225)).
- New entry in **`SOURCES` registry** (`_fetch_orangetrack_entries` in `news_bot.py`) — performs the FULL parse cycle in one place: fetches feed, parses every entry's content:encoded (or HTTP-scrapes article URL on miss) inside the function. Returns enriched entries with body fields (`title`, `subtitle`, `paragraphs`, `images`, `blocks`) already populated. Owns the orangetrack-specific admin-ping aggregator instance for one cron tick — collects (code, link) tuples during fetch + parse, emits a single aggregated admin-ping at end of `_fetch_orangetrack_entries` if the bag is non-empty. **No module-level singleton, no global state — aggregator lives only in the function's stack frame.**
- **`fetch_full_article` for orangetrack URLs** is a pass-through — reads pre-populated body fields from the entry dict (already filled by `_fetch_orangetrack_entries`) and returns the canonical contract dict. No second HTTP fetch.
- **Boilerplate filter extension** — 1-3 patterns appended to `_BOILERPLATE_PATTERNS` in `boilerplate_filter.py` for standalone short affiliate lines (`*QUICK LINK!* Buy from <store> now.` shape, length ≤ 120 chars). All new patterns anchored at start (`^`), no nested greedy quantifiers (ReDoS-safe). Inline affiliate sentences inside real paragraphs are out of scope (deferred per user-spec Q5 = I).
- **Routing wiring** in `news_bot.py` — `NETLOC_TO_SOURCE` adds the two host variants (apex + www), `SOURCE_EMOJI` adds 🔵, `SOURCE_LABEL` adds the label, `fetch_full_article` dispatcher gets a new domain branch (pass-through, see above).
- **Deploy plumbing** — `deploy.sh` and `.github/workflows/deploy.yml` FILES list both add `orangetrack_source.py` (project INVARIANT — drift between the two breaks the production cron tick on next restart). `feeds.json` is **NOT modified** — orangetrack feed URL lives as a module-level constant inside `orangetrack_source.py` (mirrors mattel_news_source's hardcoded URL convention). This keeps `_fetch_rss_entries` untouched and avoids a duplicate-fetch bug.
- **Tests** — new `tests/test_orangetrack_source.py` (~40-50 unit cases including aggregator lifecycle, SSRF allowlist, security guards), additions to `tests/test_boilerplate_filter.py` for affiliate patterns (~3-5 cases), one-line update to `tests/test_sources_registry.py` set assertion.

The feature is purely additive at the runtime path level: existing autoevolution / lamley / mattel parsers and the shared `_fetch_rss_entries` are untouched. Telegraph publisher's existing block-or-flat renderer auto-selects blocks-path when present — no changes there. LLM transcreation goes through the existing `ux-guidelines.md` system prompt unchanged. The existing `claude_transcreation._translate_block_strings` (Variant B+) and `_patch_text_with_ru_paragraphs` already handle blocks-shaped output → ru_blocks for the channel — verified at [`claude_transcreation.py:159, 436`](../../../claude_transcreation.py#L159).

## Architecture

### What we're building/modifying

- **`orangetrack_source.py` (new)** — pure parser module. Public API: `fetch_orangetrack_article(entry, notifier=None)` returns the canonical dict (or None on hard failure). Internal helpers: `_parse_content_encoded(html_str)`, `_fetch_article_html(url, notifier, link)`, `_is_allowed_orangetrack_url(url)`, `_video_embed_url(youtube_url)` (5-line copy from autoevolution + hostname allowlist gate), `OrangetrackPingAggregator` class (inside this module — not in news_bot.py).
- **`news_bot.NETLOC_TO_SOURCE` (modify)** — add `'orangetrackdiecast.com': 'orangetrack'` and `'www.orangetrackdiecast.com': 'orangetrack'` keys.
- **`news_bot.SOURCE_EMOJI` / `SOURCE_LABEL` (modify)** — add `'orangetrack': '🔵'` / `'orangetrack': 'orangetrack'`.
- **`news_bot.fetch_full_article` (modify)** — add domain branch for `orangetrackdiecast.com`. Branch is a pass-through that reads pre-populated fields (`title`, `subtitle`, `paragraphs`, `images`, `blocks`) from the entry dict and returns the canonical dict. NO second HTTP fetch.
- **`news_bot._fetch_orangetrack_entries` (new function)** — entry in `SOURCES` registry. Constructs a fresh `OrangetrackPingAggregator` instance, fetches the feed, iterates entries, calls `orangetrack_source.fetch_orangetrack_article(entry, notifier=aggregator.add)` for each entry, attaches the parsed body fields back onto the entry dict, emits aggregator at end via `aggregator.emit(send_admin_notification)` if bag non-empty. Whole function wrapped in try/finally so aggregator is GC'd cleanly even on exceptions.
- **`news_bot.SOURCES` (modify)** — append `_fetch_orangetrack_entries` to the registry list.
- **`boilerplate_filter._BOILERPLATE_PATTERNS` (modify)** — append 1-3 affiliate-line regexes. All anchored at `^`, no nested greedy groups.
- **`feeds.json` (NOT modified)** — orangetrack feed URL stays out of `feeds.json`. Lives as module-level constant `_FEED_URL = 'https://orangetrackdiecast.com/feed/'` in `orangetrack_source.py`. This mirrors `mattel_news_source`'s hardcoded URL and avoids modifying the shared `_fetch_rss_entries` iteration path (Decision 4 resolves the OR-branch in favor of a constant).
- **`deploy.sh` FILES list (modify)** — append `orangetrack_source.py`.
- **`.github/workflows/deploy.yml` FILES list (modify)** — append `orangetrack_source.py`. Must mirror `deploy.sh` byte-for-byte.
- **`tests/test_orangetrack_source.py` (new)** — unit coverage including SSRF allowlist guard, aggregator lifecycle, primary path, fallback path, edge cases.
- **`tests/test_boilerplate_filter.py` (modify)** — add affiliate-pattern positive/negative cases including 120-char boundary.
- **`tests/test_sources_registry.py` (modify)** — update set assertion to include `'orangetrack'`.

### How it works

```
Daily cron tick at 10:00 МСК (news_bot.job)
  │
  ▼
Step (b1) — fetch + filter + insert
  │
  ▼
Iterate SOURCES = [_fetch_rss_entries, _fetch_mattel_entries, _fetch_orangetrack_entries]
  │
  ├── _fetch_rss_entries     ◀── unchanged; iterates feeds.json (which still has 3 URLs — autoevolution × 2 + lamley)
  ├── _fetch_mattel_entries  ◀── unchanged
  └── _fetch_orangetrack_entries  ◀── NEW; URL = module-level constant orangetrack_source._FEED_URL
        │
        ▼
        aggregator = OrangetrackPingAggregator(instance_label=os.getenv('INSTANCE_LABEL'))
        try:
          ▼
          response = requests.get(orangetrack_source._FEED_URL,
                                  timeout=REQUEST_TIMEOUT,
                                  allow_redirects=False,
                                  stream=False)  # feed is small; no streaming
          │
          ├── HTTP error / timeout / connection refused
          │     → aggregator.add('FEED_HTTP_<status>' or 'FEED_TIMEOUT', feed_url)
          │     → return [] silently to caller
          │
          ├── Malformed XML (feedparser raises)
          │     → aggregator.add('FEED_XML_PARSE', feed_url)
          │     → return []
          │
          └── 200 + valid XML, entries = [...]
              ▼
              For each entry:
                if not _is_allowed_orangetrack_url(entry['link']):
                    aggregator.add('ENTRY_HOST_REJECTED', entry['link'])
                    continue  # skip this entry, never reaches pending_articles
                article = orangetrack_source.fetch_orangetrack_article(
                    entry, notifier=aggregator.add
                )
                if article is None: skip this entry (notifier already called)
                else: attach article fields to entry dict, append to results
              ▼
              For each result entry: stamp source_name='orangetrack', feed_url, etc.
        finally:
          ▼
          if not aggregator.is_empty():
              aggregator.emit(send_admin_notification)
        │
        ▼
        return results to SOURCES iterator
  │
  ▼
Step (b3) — for each entry: article = fetch_full_article(entry)
  │
  ▼
For orangetrack URL: pass-through reads pre-populated body fields:
  article = {
      'title': entry['title'], 'subtitle': entry['subtitle'],
      'paragraphs': entry['paragraphs'], 'images': entry['images'],
      'blocks': entry['blocks'],
  }
  │
  ▼
Step (b4) — pending_articles_repo.insert_pending(...)
```

Per-article parse path inside `orangetrack_source.fetch_orangetrack_article(entry, notifier)`:

```
entry has non-empty content:encoded?
  ├── YES → parse it via BS4 (walk by tag) → return canonical dict
  │           (silent — successful primary path)
  │
  └── NO/empty → check _is_allowed_orangetrack_url(entry['link'])
        ├── NOT allowed → notifier('ART_FALLBACK_HOST_REJECTED', link) → return None
        │
        └── allowed → bounded-stream HTTP GET, allow_redirects=False, MAX 5MB
              ├── 3xx redirect → notifier('ART_FALLBACK_REDIRECT_<status>', link) → None
              ├── 5xx/4xx → notifier('ART_FALLBACK_HTTP_<status>', link) → None
              ├── timeout → notifier('ART_FALLBACK_TIMEOUT', link) → None
              ├── body > 5MB → notifier('ART_FALLBACK_TOO_LARGE', link) → None
              ├── 200 + parse extracts content → return canonical dict (silent)
              └── 200 + parse extracts nothing → notifier('ART_FALLBACK_PARSE_EMPTY', link) → None

Any unexpected exception → notifier('ART_PARSE_EXCEPTION', link) → re-raise (existing news_bot.fetch_full_article try/except catches)
```

Telegraph publisher auto-selects blocks-path renderer when `pending_articles.blocks` is non-null — no changes needed in telegraph or `_fallback_publish`. The existing `claude_transcreation._translate_block_strings` (Variant B+) translates block text/captions; `_patch_text_with_ru_paragraphs` splices translated paragraphs back into blocks structure → `ru_blocks` populated automatically for orangetrack rows the same way it is for autoevolution.

### Shared resources

| Resource | Owner (creates) | Consumers | Instance count |
|----------|----------------|-----------|----------------|
| `OrangetrackPingAggregator` | `news_bot._fetch_orangetrack_entries` (function-local) | `news_bot._fetch_orangetrack_entries` (creates + emits), `orangetrack_source.fetch_orangetrack_article` (calls `add()` via notifier callback parameter) | 1 per cron tick (lifetime: stack frame of `_fetch_orangetrack_entries` call; GC'd via try/finally) |

The aggregator is the only stateful resource introduced. **Function-local, no module-level singleton** — eliminates concurrency risk and state-leak risk.

## Decisions

### Decision 1: Hybrid feed-first parser with bounded fallback HTTP
**Decision:** Primary path parses RSS `<content:encoded>` in-memory. Fallback path makes ONE bounded-stream HTTP GET (`stream=True`, `iter_content`, ≤ 5 MB cap, `allow_redirects=False`, `timeout=REQUEST_TIMEOUT`) of the article URL when `content:encoded` is missing/empty. Caller checks `_is_allowed_orangetrack_url()` allowlist before issuing GET (Decision 13).
**Rationale:** WordPress.com sends the full article body in `<content:encoded>` for orangetrackdiecast (verified by code research §3 with live capture of post 35264). Parsing the feed avoids a per-article HTTP call. Defense-in-depth via fallback covers the rare case (≤1/week expected) of empty `content:encoded`. Streaming + size cap prevents memory-exhaustion DoS even from a lying server (lamley's `len(response.content) > MAX_RESPONSE_SIZE` check materializes full body first — known weakness; we improve on it). `allow_redirects=False` prevents redirect-bypass SSRF (mattel pattern from patterns.md).
**Alternatives considered:**
- (A) Feed-only, no fallback. Rejected — operator explicitly required fallback (user-spec Q7).
- (B) Full HTTP scrape every time. Rejected — adds ~7 daily HTTP requests for no benefit.
- (C) `response.content` post-download size check (lamley pattern). Rejected — vulnerable to lying servers; streaming is strictly better.

**Supports user-spec:** "Как должно работать" steps 2-3, AC3, R5.

### Decision 2: Blocks-path output with safe href/src filtering
**Decision:** Parser emits both flat fields (`title, subtitle, paragraphs, images`) AND a `blocks` list. The `blocks` list contains typed entries: `paragraph`, `image`, `video`, `heading` (h5 only). Anchor `href` attributes are filtered to schemes `(http, https, mailto)` — other schemes (`javascript:`, `data:`, `file:`, scheme-relative `//`) drop the href and the anchor becomes plain text. Image `<img src>` URLs must start with `http://` or `https://` — `data:`, `file:`, relative URIs are dropped from images and blocks.
**Rationale:** Brad's posts often interleave YouTube embeds (his unboxing videos) between paragraphs. Flat shape `{paragraphs, images}` has no place to put a video iframe. Blocks-path preserves position. Telegraph publisher already auto-selects blocks renderer when present. Defense-in-depth: parser must not propagate dangerous URI schemes from arbitrary blogpost HTML to Telegraph nodes — relying solely on Telegraph's filter is brittle.
**Alternatives considered:**
- (A) Flat-path only. Rejected — drops video.
- (B) Blocks-only without flat fields. Rejected — `paragraphs` is the gating field at [`news_bot.py:1510`](../../../news_bot.py#L1510); flat fields preserved for that gate.
- (C) Trust Telegraph's filter for href/src sanitization. Rejected — defense-in-depth.

**Supports user-spec:** AC3, AC4, AC5, Q9.

### Decision 3: Walk DOM by HTML tag, not by Gutenberg class
**Decision:** Parser uses BS4 `find_all` on raw HTML tags (`<p>`, `<figure>`, `<img>`, `<iframe>`, `<h5>`). Ignores `wp-block-*` classes entirely.
**Rationale:** Gutenberg classes are version-specific and theme-dependent. Tags are stable across all WP variants and editor migrations.
**Alternatives considered:** Parse by class names. Rejected — brittle.

**Supports user-spec:** AC14, R1.

### Decision 4: Separate `_fetch_orangetrack_entries` doing FULL parse cycle in one place; URL is a module constant (NOT in feeds.json)
**Decision:** Add a new fetcher function `_fetch_orangetrack_entries` to the `SOURCES` registry list. It fetches the feed AND parses every entry's body inside the same function call. Returns enriched entries with `title`, `subtitle`, `paragraphs`, `images`, `blocks` already populated. **Feed URL is a module-level constant `_FEED_URL = 'https://orangetrackdiecast.com/feed/'` in `orangetrack_source.py`** — it does NOT appear in `feeds.json`. `_fetch_rss_entries` is not touched in any way; `feeds.json` keeps its 3 existing URLs unchanged.
**Rationale:** The admin-ping aggregator (Decision 5) needs to see BOTH feed-level events (FEED_*) AND per-article events (ART_*). If we only fetch entries here and let per-article parsing happen later in step b3 via `fetch_full_article`, the aggregator's emit() would fire too early and ART_* events would be silently lost. By doing the full parse cycle inside `_fetch_orangetrack_entries`, the aggregator stays alive for the whole event window and emits exactly once at the end. As a side effect, `fetch_full_article` for orangetrack URLs becomes a trivial pass-through (entry already has body fields). This is also the smallest change at the integration boundary and preserves the existing per-source isolation (one source's failure doesn't block others).

**Why `_FEED_URL` is a constant, not in `feeds.json`:** Adding the URL to `feeds.json` would make `_fetch_rss_entries` fetch it as well (it iterates `load_feeds()` unconditionally — no per-URL filter capability), causing a duplicate fetch and producing shadow entries with no body fields that get silently dropped at `news_bot.py:1510` paragraphs gate. Adding a filter to `_fetch_rss_entries` touches the shared function used by 3 existing sources (Alternative A, rejected). Constant-in-module mirrors the `mattel_news_source` pattern — Mattel's `NEWS_URL` is hardcoded too. If Brad ever changes the feed URL, operator edits one constant + redeploys. Same operational pattern as for any other source.
**Alternatives considered:**
- (A) Modify shared `_fetch_rss_entries` with optional aggregator parameter. Rejected — touches shared function used by 3 existing sources.
- (B) Module-level `_active_aggregator` singleton spanning b1 → b3. Rejected — global mutable state, concurrency risk, cleanup-on-exception fragility.
- (C) Defer the aggregator emit to end of `news_bot.job()` via a hook. Rejected — couples news_bot.job() to source-specific logic.

**Note:** This contradicts code-research §2's recommendation ("NOT needed: SOURCES registry — orangetrack arrives via existing `_fetch_rss_entries`"). Code-research was written before the admin-ping aggregator requirement was introduced (user-spec Q11) and before the lifecycle timing problem was surfaced by validators.

**Supports user-spec:** AC1 (per-feed isolation preserved), AC6 (aggregated single-ping per cron tick).

### Decision 5: `OrangetrackPingAggregator` design and bounds
**Decision:** Class `OrangetrackPingAggregator` lives inside `orangetrack_source.py` (function-local instance per cron tick). API: `add(code, link)`, `is_empty()`, `format_summary()`, `emit(send_fn)`.

State: dict-of-(code → list[link])-with-counts; per-code link list deduplicated and order-preserving. Bounds: per-code link list capped at **50 entries** (further entries replaced with `… N more truncated`); total `add()` calls capped at **500** (subsequent calls are silent no-ops, not raises). `format_summary()` truncates the rendered string to **≤ 3500 chars** (margin for Telegram's 4096 limit).

Format spec (matches user-spec AC6):
```
[<INSTANCE_LABEL>] orangetrack: N issues this tick
  • <CODE> (<count>×) — <link1>[, <link2>...]
  • <CODE> (<count>×) — <link>
```
- N is the number of distinct (code, link) pairs (not raw add() count).
- Full code taxonomy: `FEED_HTTP_<status>`, `FEED_TIMEOUT`, `FEED_XML_PARSE` (feed-level); `ENTRY_HOST_REJECTED` (Decision 13 entry-level guard, primary-path SSRF defense); `ART_FALLBACK_HTTP_<status>`, `ART_FALLBACK_TIMEOUT`, `ART_FALLBACK_HOST_REJECTED` (Decision 13 fallback-level guard), `ART_FALLBACK_REDIRECT_<status>` (Decision 1 — redirect rejected), `ART_FALLBACK_TOO_LARGE` (Decision 1 — body exceeded 5 MB cap), `ART_FALLBACK_PARSE_EMPTY`, `ART_PARSE_EXCEPTION`.
- Codes ordered: `FEED_*` group before `ENTRY_*` before `ART_*`; alphabetical within each group (with status-suffix codes treated as full strings — `FEED_HTTP_429` comes before `FEED_HTTP_503` alphabetically and both are distinct entries from `FEED_TIMEOUT`).
- `<INSTANCE_LABEL>` prefix omitted when env var `INSTANCE_LABEL` is unset / empty.

**Sanitization:** `add()` runs each `code` and `link` through a `_safe_for_ping(s)` helper. Order of operations: (1) strip ASCII control chars (`\r`, `\n`, `\t`, `\x00`-`\x1f`); (2) replace any remaining non-printable bytes with `?`; (3) truncate to 200 chars (with `…` suffix when truncated). Strict order so a malicious string can't smuggle a control byte by being just under 200 chars before sanitization. Prevents log/admin-ping spoofing via crafted feed link strings.

`emit(send_fn)`: calls `send_fn(format_summary())`. If `send_fn` raises, the exception is **logged at ERROR level via the module logger** (so journalctl shows the failure with full traceback) and then **swallowed** — do not propagate. Admin-ping send failure must not break the cron tick. The ERROR log line is the operator's only signal of admin-ping delivery failure (the channel still publishes; the user-spec already accepts that admin-ping failure is non-fatal).

**Rationale:** Operator wants ONE message per tick with codes for fast triage (Q11). Bounds prevent feed-bug-induced ping flood and Telegram message-size overflow. Sanitization prevents attacker-controlled link strings from spoofing fake ping lines. Emit-swallows-error keeps the cron tick robust.
**Alternatives considered:**
- Per-event admin-ping. Rejected — flood risk.
- Silent + log-only. Rejected — operator wants visibility.
- Reuse existing `'⚠️ Source failed: ...'` pattern at [`news_bot.py:1471`](../../../news_bot.py#L1471). Rejected — that pattern fires per failure (no aggregation) and lacks structured codes.

**Supports user-spec:** AC6, AC7, Q11.

### Decision 6: NO throttle / Retry-After (alignment with user-spec, NOT a deviation)
**Decision:** Orangetrack parser does NOT implement Lamley-style request throttle, 429 Retry-After honoring, or `MIN_REQUEST_INTERVAL_S`.
**Rationale:** User-spec R4 explicitly states "Специальные защиты (как у Lamley с throttle и 429-retry) не делаем — у Lamley они появились после реальных 429 в проде, у нас нет основания их предрассчитывать." Tech-spec aligns. The aggregator code `ART_FALLBACK_HTTP_429` will surface any actual 429 immediately so a follow-up can add throttle if observed. `[TECHNICAL]` decision in the sense that no test/AC enforces "throttle exists" — but it's already what user-spec asks for.
**Alternatives considered:** Lamley-pattern throttle. Rejected — user-spec position.

**Supports user-spec:** R4 mitigation (verbatim).

### Decision 7: NO new pip dependencies
**Decision:** Use existing `requests`, `feedparser`, `beautifulsoup4`, `re`, `urllib.parse`, `logging`. No `curl_cffi`, no `lxml` explicit, no others.
**Rationale:** No Cloudflare bypass needed. BS4 with default parser handles content:encoded body. Minimum surface.

**Supports user-spec:** "Ограничения" — "Нет новых зависимостей".

### Decision 8: YouTube embed wrapper with hostname allowlist
**Decision:** Replicate the YouTube ID regex + `https://telegra.ph/embed/youtube?url=<urlencoded>` wrapper inside `orangetrack_source.py`. **Critical addition over the autoevolution copy:** before applying the YouTube ID regex, the iframe `src` attribute is parsed via `urlparse()` and its hostname must be in the allowlist `('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtube-nocookie.com', 'www.youtube-nocookie.com', 'youtu.be')`. (Note: `youtu.be` is the canonical short-link host with no www variant. `youtube-nocookie.com` is YouTube's privacy-enhanced embed domain and is commonly used by WordPress YouTube plugins.) Iframes from other hostnames (e.g. attacker-controlled URL containing the substring `youtube.com/embed/`) are dropped from the blocks list entirely.
**Rationale:** Cross-importing private-prefixed `_video_embed_url` from autoevolution is a private-API violation. Five lines duplication is cheaper than introducing a shared module. The hostname allowlist gate is the security upgrade — autoevolution's `.search()` matches anywhere in the string, so a non-YouTube URL containing the substring would be falsely wrapped. Adding the allowlist closes the content-spoofing primitive identified by security audit.
**Alternatives considered:**
- (A) Import `autoevolution_source._video_embed_url`. Rejected — private-API + same security gap.
- (B) Extract to shared `media_helpers.py`. Rejected — premature DRY.

**Supports user-spec:** AC5; security-defense-in-depth.

### Decision 9: Test fixtures use lamley-pattern inline `SAMPLE_HTML` constants
**Decision:** `tests/test_orangetrack_source.py` uses module-level `SAMPLE_*_HTML` string constants inline.
**Rationale:** WordPress block markup is short and simple; ~40-50 cases fit comfortably as inline constants. Mattel's `tests/fixtures/mattel_flight_builder.py` exists because RSC flight payloads are complex synthetic structures — not the case here.

**Supports user-spec:** AC11 testing scope.

### Decision 10: source_name=`orangetrack`, hashtag=`#orangetrackdiecast`, emoji=`🔵`
**Decision:**
- `NETLOC_TO_SOURCE` keys: `orangetrackdiecast.com` → `orangetrack`, `www.orangetrackdiecast.com` → `orangetrack`.
- `SOURCE_EMOJI['orangetrack'] = '🔵'`, `SOURCE_LABEL['orangetrack'] = 'orangetrack'`.
- `_source_hashtag(source_url)` is unchanged — it derives `#orangetrackdiecast` from the netloc directly.

**Rationale:** Short internal label matches existing `'lamley'` / `'mattel'`. Channel hashtag stays operator-visible as the netloc form. Blue circle is the 4th free color.
**Alternatives considered:** Same-name everywhere or custom hashtag. Rejected — convention break.

**Supports user-spec:** "Технические решения".

### Decision 11: Update `tests/test_sources_registry.py` set assertion in same commit as registry change
**Decision:** Set-assertion in `tests/test_sources_registry.py` updated to `{'autoevolution', 'lamley', 'mattel', 'orangetrack'}` in the same commit as the registry changes.
**Rationale:** Otherwise pytest fails immediately on the change. Code-research §2 flagged this as a known break-on-add point.

**Supports user-spec:** AC11, AC12.

### Decision 12: Boilerplate filter scope = standalone short paragraphs only, ReDoS-safe patterns
**Decision:** Affiliate patterns added to `_BOILERPLATE_PATTERNS`. They match standalone paragraphs ≤ 120 chars. **All new patterns are anchored at `^` AND must NOT contain nested greedy quantifiers** (no `(.+)+` shape). Inline affiliate sentences inside real paragraphs are NOT addressed (per user-spec Q5 = I).

**Rationale:** Variant A boilerplate filter shape is the same as existing `'Share on Facebook'` / `'^follow us on'` patterns. Anchoring + no-nested-greedy guarantees no catastrophic backtracking even on pathological inputs. Length-bound at 120 chars further bounds worst case.
**Alternatives considered:**
- (B) Extend `_strip_plugs` in `news_bot.py` with affiliate cues. Rejected — operator-deferred (Q5).
- (C) Pre-LLM stripper inside `orangetrack_source.py`. Rejected — operator-deferred.

**Supports user-spec:** Q5 = I, AC8, AC10.

### Decision 13: SSRF allowlist guard `_is_allowed_orangetrack_url` (TWO call sites)
**Decision:** Inside `orangetrack_source.py`, function `_is_allowed_orangetrack_url(link: str) -> bool` performs an exact-host-match check against `_ALLOWED_HOSTS = ('orangetrackdiecast.com', 'www.orangetrackdiecast.com')`. Returns False for non-http(s) schemes, malformed URLs, or hosts that aren't an exact allowlist match.

**Two call sites:**
1. **Entry-level guard in `_fetch_orangetrack_entries`** — called for EACH entry's `link` field BEFORE the entry enters parsing. If rejected: notifier called with `'ENTRY_HOST_REJECTED'` code; entry skipped (not added to results, never reaches `pending_articles`). Closes content-spoofing risk: a poisoned feed entry with `link='https://attacker.example/x'` cannot be published to the channel even via the primary content:encoded path.
2. **Fallback-HTTP guard in `fetch_orangetrack_article`** — called BEFORE issuing the fallback HTTP GET when content:encoded is missing/empty. If rejected: notifier called with `'ART_FALLBACK_HOST_REJECTED'` code; return None.

Mirrors the lamley pattern at [`lamley_source.py:212-225`](../../../lamley_source.py#L212-L225). `news_bot.fetch_full_article` dispatcher continues to use substring `in` for routing (existing convention) — but the parser MUST do its own exact-host check before HTTP, since the dispatcher's substring match is exploitable (e.g. `https://orangetrackdiecast.com.attacker.example/payload` would route to orangetrack despite being attacker-controlled).

**Rationale:** Without this guard, an attacker who can inject a `<link>` field in an RSS entry (or in the future, who compromises orangetrackdiecast itself) could direct the bot to issue server-side HTTP requests against arbitrary URLs (cloud metadata, internal services). The same flaw was closed in lamley (`_is_allowed_lamley_url` at line 212-225) and mattel (`ARTICLE_URL_PREFIX` startswith). Defense-in-depth from the parser side is the project-wide convention per `patterns.md` ‘Security boundary’ note.
**Alternatives considered:**
- Trust the dispatcher's substring match. Rejected — known SSRF gap.
- Tighten the dispatcher to exact match. Rejected — wider blast radius (touches existing autoevolution + lamley + mattel handling); adding a parser-side guard is the smaller, project-conventional fix.

**Supports user-spec:** R4 (extended — covers SSRF pivot risk not explicitly in user-spec but implicit in "WordPress.com hosting, no Cloudflare/antibot" assumption).

### Decision 14: ru_blocks pipeline already handled by existing LLM transcreation
**Decision:** No changes to `claude_transcreation` / `_llm_common` / other engine modules. The orangetrack parser's `blocks` output lands in `pending_articles.blocks`, picked up by existing pipeline:
- `_translate_block_strings` (Variant B+) at [`claude_transcreation.py:159`](../../../claude_transcreation.py#L159) — translates captions/text in EN-fallback blocks via a focused second-pass call.
- `_patch_text_with_ru_paragraphs` at [`claude_transcreation.py:436`](../../../claude_transcreation.py#L436) — splices Russian paragraphs back into EN block scaffold when LLM didn't return matching blocks.

This is the same pipeline used by autoevolution today. Verified by grep + read of claude_transcreation.

**Rationale:** Eliminates a (false) gap that completeness validator flagged. Documenting this here so future readers don't re-investigate.
**Alternatives considered:** None — this is verification of existing behavior, not a design choice.

**Supports user-spec:** AC4 (ru_blocks correctness).

### Decision 15: H5 heading goes to blocks-path only, NOT flat paragraphs
**Decision:** When parser encounters `<h5>` inside content:encoded, it emits a `{type: 'heading', level: 5, text: ...}` entry in the `blocks` list. The heading text does NOT appear in the flat `paragraphs` list.
**Rationale:** Mirrors autoevolution's heading handling — `paragraphs` is the LLM-translation-input list and should only contain prose paragraphs. Heading text is preserved in blocks, where Telegraph renders it as an `<h3>` (Telegraph's heading level mapping). LLM gets a cleaner translation context without heading boilerplate cluttering the paragraphs list.
**Alternatives considered:** Include heading text in paragraphs (treated as an extra paragraph). Rejected — clutters translation, and Telegraph would render a paragraph-style heading without the `<h3>` styling.

**Supports user-spec:** AC3 (block types include `heading`).

## Data Models

No DB schema changes. Existing tables and columns stay as-is.

- `pending_articles.source_name` gains a new value `'orangetrack'`.
- `pending_articles.blocks` already nullable JSON; orangetrack will write a populated blocks list.
- Other tables unaffected.

Module-level data structure introduced in `orangetrack_source.py`:

```python
class OrangetrackPingAggregator:
    """Collects (code, link) tuples during a cron tick. Emits one
    aggregated admin-ping at end via send_admin_notification.

    Bounds: per-code link list capped at 50; total add() calls capped
    at 500; format_summary() truncates to ≤ 3500 chars.
    """
    def __init__(self, instance_label: str | None = None) -> None: ...
    def add(self, code: str, link: str) -> None: ...
    def is_empty(self) -> bool: ...
    def format_summary(self) -> str: ...
    def emit(self, send_fn: Callable[[str], None]) -> None: ...
        # Catches and logs any exception from send_fn — never propagates.
```

## Dependencies

### New packages

None.

### Using existing (from project)

- **`requests`** — feed fetch + bounded-stream fallback HTTP scrape.
- **`feedparser`** — RSS parsing.
- **`beautifulsoup4`** — content:encoded HTML parse + fallback HTML scrape parse.
- **`re`** — affiliate-line patterns + YouTube embed regex.
- **`urllib.parse`** — URL parsing for image dedup, host allowlist, YouTube wrap.
- **`logging`** — module-level logger.
- **`boilerplate_filter.filter_boilerplate`** — applied per-parser.
- **`news_bot.send_admin_notification`** — passed to aggregator's `emit()`.

## Testing Strategy

**Feature size:** M

### Unit tests

In `tests/test_orangetrack_source.py` (~40-50 cases, organized into classes mirroring lamley/mattel test conventions):

**`TestPrimaryPath`** (content:encoded parse):
- Standard post with title + subtitle + N paragraphs + M images.
- Post with YouTube iframe interleaved → blocks list contains `video` entry at right position; flat `paragraphs` omits the video position.
- Post with h5 case-section heading → blocks list has `heading` entry at right position; flat `paragraphs` does NOT include heading text (Decision 15).
- Post with carousel gallery (nested `<figure>`) → all images flattened, dedup by URL pre-`?`, capped at IMAGE_LIMIT=10.
- Post with image dedup across srcset suffixes (`?w=300`, `?w=600`) → one entry retained.
- Post with affiliate "QUICK LINK!" line as standalone short paragraph → boilerplate strips it.
- Post with affiliate sentence INLINE in real paragraph → kept verbatim (Q5 = I).
- Post with empty paragraphs after boilerplate filter → `paragraphs=[]`.
- Post with no images → `images=[]`, blocks list has no `image` entries.
- Post with only YouTube iframe and no text → `paragraphs=[entry.title]` synthesized, blocks has `video` entry.
- Multiple `<iframe>` in same paragraph block → blocks list contains 2 `video` entries in order.
- Non-ASCII title (curly quotes, em-dash, unicode model names) → preserved without mojibake.
- `<a href="javascript:alert(1)">click</a>` → kept as plain text "click", no anchor href in blocks.
- `<img src="data:image/svg+xml,...">` → dropped from images and blocks.
- Scheme-relative href (`//evil.example/x`) → href dropped, plain text only.

**`TestSSRFAllowlist`** (Decision 13):
- `_is_allowed_orangetrack_url('https://orangetrackdiecast.com/post')` → True.
- `_is_allowed_orangetrack_url('https://www.orangetrackdiecast.com/post')` → True.
- `_is_allowed_orangetrack_url('https://orangetrackdiecast.com.attacker.example/payload')` → False (subdomain attack).
- `_is_allowed_orangetrack_url('http://169.254.169.254/latest/meta-data/')` → False (cloud metadata).
- `_is_allowed_orangetrack_url('javascript:alert(1)')` → False (non-http scheme).
- `_is_allowed_orangetrack_url('not a url')` → False (malformed).
- `_is_allowed_orangetrack_url('//evil.example/x')` → False (scheme-relative).
- Entry-level guard: feed has entry with `link='https://attacker.example/x'` (poisoned but content:encoded valid) → `_fetch_orangetrack_entries` calls `aggregator.add('ENTRY_HOST_REJECTED', ...)` and skips entry; entry NEVER reaches `pending_articles`. Verifies the primary-path SSRF guard.
- Fallback-level guard: `fetch_orangetrack_article(entry={'link': 'https://attacker.example/x', ...content:encoded missing})` → notifier `('ART_FALLBACK_HOST_REJECTED', ...)`; returns None; `requests.get` NOT called.

**`TestFallbackPath`** (HTTP scrape):
- content:encoded missing → HTTP GET 200 + parse → returns canonical dict (silent — successful fallback).
- HTTP GET 503 → notifier `('ART_FALLBACK_HTTP_503', link)`; returns None.
- HTTP GET 404 → notifier `('ART_FALLBACK_HTTP_404', link)` (verifies status placeholder substitution).
- HTTP GET timeout → notifier `('ART_FALLBACK_TIMEOUT', link)`.
- HTTP GET 302 redirect (with Location to internal IP) → notifier `('ART_FALLBACK_REDIRECT_302', link)`; second HTTP request NOT issued.
- HTTP GET 200 + body 6 MB → bounded-stream cuts off after 5 MB, notifier `('ART_FALLBACK_TOO_LARGE', link)`.
- HTTP GET 200 but body has no extractable content → notifier `('ART_FALLBACK_PARSE_EMPTY', link)`.
- Connection refused → notifier `('ART_FALLBACK_TIMEOUT', link)` or `('ART_FALLBACK_HTTP_<...>', link)` per `requests` exception type — single test with documented mapping.
- Unexpected exception in parser → notifier `('ART_PARSE_EXCEPTION', link)`; exception propagates to caller.
- UTF-8 response with non-ASCII body → paragraphs decode correctly.

**`TestWPBlockDriftMitigation`** (Decision 3):
- HTML body without ANY `wp-block-*` classes (synthesized minimal: just `<p>` / `<figure>` / `<img>`) → parser still extracts paragraphs and images.

**`TestYouTubeEmbedWrapping`** (Decision 8):
- iframe `src='https://www.youtube.com/embed/abc123?...&...'` → blocks `video.src` is `https://telegra.ph/embed/youtube?url=<urlencoded watch URL>`.
- iframe `src='https://attacker.example/redirect?u=youtube.com/embed/abc'` → no `video` entry in blocks (host allowlist rejects); iframe dropped silently.
- iframe `src='https://youtu.be/abc123'` → wrapped correctly.
- iframe `src='https://vimeo.com/123'` → no wrap (Vimeo not in allowlist for this feature).

**`TestOrangetrackPingAggregator`**:
- Empty bag → `is_empty()=True`, `emit()` no-op.
- 3 events (1× FEED_HTTP_503, 2× ART_FALLBACK_HTTP_404 with different links) → `format_summary()` produces the documented format with grouping.
- Same code, same link, called twice → count=2, link listed once.
- Distinct status codes: FEED_HTTP_503 + FEED_HTTP_429 → two separate bullet lines.
- Codes ordered: FEED_* before ART_*; alphabetical within category — TWO separate tests (one for category ordering, one for alphabetical).
- `instance_label='test'` → output starts with `[test] orangetrack: ...`.
- `instance_label=None` or `''` → output omits bracketed prefix.
- N count semantic: 5 add() calls with 2 dups → header reads `3 issues` (count of distinct (code, link) pairs).
- Per-code link list capped at 50 → 60 add() calls of same code with distinct links → bullet list shows first 50 + `... 10 more truncated`.
- Total add() calls capped at 500 → 600 calls → `len(internal state)` reflects cap, no exception.
- format_summary() output capped at 3500 chars → 600 add() calls of long links → output ≤ 3500 chars.
- Sanitization: `add('FEED_HTTP_503', 'https://x.example/\n[prod] orangetrack: 0 issues this tick')` → format_summary contains the link with newline replaced/escaped, no fake summary line.
- `emit(send_fn)` swallows exception: send_fn raises requests.HTTPError → emit() does not propagate; logs at ERROR level.
- `_fetch_orangetrack_entries` lifecycle: aggregator instance is created at start, emitted at end via try/finally; if fetcher itself raises, finally clause still attempts to emit (or guard against partial state).

**`TestDispatcherIntegration`** (in `tests/test_orangetrack_source.py` or `tests/test_news_bot.py` — TBD):
- `news_bot.fetch_full_article({'link': 'https://orangetrackdiecast.com/post', 'paragraphs': ['p1'], ...})` → returns canonical dict via pass-through (no HTTP).
- `news_bot.fetch_full_article({'link': 'https://orangetrackdiecast.com.attacker.example/x', ...})` → routed through orangetrack but parser's allowlist rejects, returns None (defense-in-depth).
- Apex vs www domain routing: both `orangetrackdiecast.com/post` and `www.orangetrackdiecast.com/post` route to orangetrack pass-through.

In `tests/test_boilerplate_filter.py` (~3-5 new cases, additive — placed in existing `TestIsBoilerplatePositive` / `TestIsBoilerplateNegative` parametrize lists where natural):

- `'*QUICK LINK!* Buy from 1 Stop Diecast now.'` → filtered (positive).
- `'Buy now from 1 Stop Diecast'` → filtered (positive).
- `'I\'d buy that for $1'` → survives (negative).
- Long content paragraph >120 chars containing 'buy' → survives.
- Affiliate pattern at exactly 120 chars → filtered (boundary).
- Affiliate pattern at 121 chars → preserved (boundary +1).
- ReDoS-safety: input `'a' * 60 + 'buy' * 20 + ' shop now'` → `is_boilerplate(s)` returns within 100ms.

In `tests/test_sources_registry.py`:

- Set-assertion updated: `{'autoevolution', 'lamley', 'mattel', 'orangetrack'}`.

### Integration tests

None new. Orangetrack passes through the same `fetch_full_article` → `pending_articles_repo.insert_pending` → `_fallback_publish` → Telegraph + Telegram pipeline as existing 3 sources. Coverage exists via `test_distributed_schedule_integration.py` which doesn't hardcode source identity.

### E2E tests

None. M-size feature; pre-deploy QA on `dev` branch via `deploy_test.yml` + visual check on test channel `@myhwchannel123` provides practical end-to-end coverage.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

Layer 1 — automated (pytest, runs before commit + in CI):
- All existing tests + new unit cases.
- Pre-commit hooks (gitleaks, whitespace, EOF, merge conflicts).

Layer 2 — pre-deploy smoke (manual REPL, before pushing to dev):
- Import check: `python3 -c "import orangetrack_source; print(orangetrack_source.fetch_orangetrack_article)"` → confirms module loads + symbol present.
- Aggregator format spot-check: `python3 -c "from orangetrack_source import OrangetrackPingAggregator; a = OrangetrackPingAggregator('test'); a.add('FEED_HTTP_503', 'https://example/x'); print(a.format_summary())"` → prints expected format.

Layer 3 — post-deploy on dev (test instance):
- After CI green and `deploy_test.yml` succeeds, `journalctl -u news_bot_test.service -n 100 --since "5 minutes ago"` confirms bot started without `ImportError` and orangetrack feed fetched on the next cron tick.
- Wait for next slot in 10:00–20:00 МСК window. If orangetrack article in queue, it publishes to test channel.
- Operator visually inspects test channel `@myhwchannel123`: hashtag, IV preview, Telegraph blocks-path render, RU translation, no affiliate-line leak.

Layer 4 — production after main merge:
- 24h `journalctl -u news_bot.service` review.
- 1-week channel review for tone consistency.

### Tools required

- **bash** — pytest, journalctl, git operations.
- **Telegram MCP** (or operator-visual) — test channel inspection.
- **curl** (optional) — manual feed fetch during smoke layer 2 if Python REPL not available.

## Risks

| Risk | Mitigation |
|------|-----------|
| WordPress block-class drift breaks parser | Decision 3 (walk by tag). Unit test covers minimal HTML without `wp-block-*` classes. `ART_PARSE_EXCEPTION` aggregator code surfaces breakage. |
| Inline affiliate sentences leak to channel | Out of scope per Decision 12 / user-spec Q5 = I. |
| Forgotten FILES list update (deploy.sh / .github/workflows/deploy.yml drift) | Manual diff review of both files in PR. |
| `test_sources_registry.py` set assertion breaks | Update in same commit (Decision 11). |
| orangetrackdiecast rate-limits feed fetch | Decision 6 — no preemptive throttle. `ART_FALLBACK_HTTP_429` admin-ping surfaces real cases. |
| Site down >5-7 days continuously AND >10 posts published | Out of scope — natural RSS recovery covers ≤5-day outages with Brad's cadence. |
| Brad changes feed URL or plugin | `FEED_*` admin-ping codes surface; operator updates `feeds.json`. |
| YouTube wrapper false-positive (non-YouTube URL wrapped) | Decision 8 hostname allowlist gate before regex. |
| SSRF via crafted feed link to internal IP | Decision 13 `_is_allowed_orangetrack_url` exact-host allowlist; `allow_redirects=False`; bounded-stream HTTP. |
| Memory exhaustion via lying server returning 500 MB body | Bounded-stream `iter_content` + 5 MB cap (Decision 1). |
| Telegraph node injection via `javascript:` href / `data:` src | Decision 2 scheme filtering at parser layer. |
| Aggregator memory exhaustion from feed bug producing 10000+ errors | Decision 5 bounds: 50 links/code, 500 total adds, 3500-char output cap. |
| Admin-ping spoofing via control chars in attacker-controlled link | Decision 5 `_safe_for_ping` sanitization (strip `\r` `\n` `\t` etc.). |
| ReDoS in new affiliate boilerplate patterns | Decision 12 — anchored at `^`, no nested greedy quantifiers; ReDoS-safety unit test. |

## User-Spec Deviations

- **Extends user-spec AC1 (where orangetrack feed is fetched + how the URL is configured):** user-spec implies orangetrack feed URL goes into `feeds.json` and is fetched through shared `_fetch_rss_entries`. Tech-spec instead: (a) routes orangetrack through a NEW `_fetch_orangetrack_entries` in the `SOURCES` registry (Decision 4), and (b) hardcodes the feed URL as a module-level constant `_FEED_URL` in `orangetrack_source.py` rather than adding it to `feeds.json`. Reasons: (a) admin-ping aggregator (AC6) requires lifecycle isolation that shared `_fetch_rss_entries` can't provide; (b) putting the URL in `feeds.json` would make `_fetch_rss_entries` fetch it as well (no per-URL filter exists), causing duplicate fetch + silent-drop bug. Mirrors the `mattel_news_source.NEWS_URL` precedent (Mattel's URL is also hardcoded, not in feeds.json). Operationally identical from operator's perspective: if Brad ever changes the feed URL, operator edits one constant + redeploys (same as if the URL were in feeds.json). Existing autoevolution / lamley / mattel paths untouched; per-feed isolation preserved. → **[PENDING USER APPROVAL]**

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [ ] `orangetrack_source.py` создан с публичной функцией `fetch_orangetrack_article(entry, notifier=None)` и module-level constant `_FEED_URL`.
- [ ] `_is_allowed_orangetrack_url` реализован, ВЫЗЫВАЕТСЯ В ДВУХ МЕСТАХ — на entry-level в `_fetch_orangetrack_entries` (отбраковка poisoned link до парсинга) и перед HTTP fallback. Тестирован на subdomain-attack, cloud metadata IP, malformed URL, non-http scheme, scheme-relative URL.
- [ ] Парсер возвращает `{title, subtitle, paragraphs, images, blocks}`. `blocks` непустой для не-empty постов; `paragraphs` непустой (синтез из `entry.title` для video-only).
- [ ] H5 в `blocks` как `heading` (Decision 15), не в `paragraphs`.
- [ ] href с `javascript:` / `data:` / scheme-relative — drop с сохранением plain text (Decision 2).
- [ ] `<img src=>` принимается только `http://` / `https://`.
- [ ] HTTP fallback с `allow_redirects=False`, bounded streaming, 5 MB cap.
- [ ] YouTube wrapper с hostname allowlist (`youtube.com` / `www.youtube.com` / `m.youtube.com` / `music.youtube.com` / `youtube-nocookie.com` / `www.youtube-nocookie.com` / `youtu.be`).
- [ ] `news_bot.NETLOC_TO_SOURCE`, `SOURCE_EMOJI`, `SOURCE_LABEL` дополнены ключами orangetrack.
- [ ] `news_bot.fetch_full_article` имеет ветку для orangetrack (pass-through).
- [ ] `news_bot._fetch_orangetrack_entries` создан, добавлен в `news_bot.SOURCES`, владеет `OrangetrackPingAggregator` (function-local).
- [ ] `OrangetrackPingAggregator` в `orangetrack_source.py` реализован с bounds (50/500/3500), `_safe_for_ping` sanitization, swallow-on-emit-error.
- [ ] `boilerplate_filter._BOILERPLATE_PATTERNS` дополнен 1-3 ReDoS-safe affiliate-паттернами (анкера `^`, без `(.+)+`).
- [ ] `feeds.json` НЕ модифицирован (остаётся 3 URL — autoevolution × 2 + lamley). Orangetrack feed URL — module-level constant `_FEED_URL` в `orangetrack_source.py`.
- [ ] `deploy.sh` FILES list содержит `orangetrack_source.py`.
- [ ] `.github/workflows/deploy.yml` FILES list содержит `orangetrack_source.py` и побайтно совпадает с `deploy.sh` FILES.
- [ ] `tests/test_orangetrack_source.py` создан, ~40-50 тестов в классах `TestPrimaryPath`, `TestSSRFAllowlist`, `TestFallbackPath`, `TestWPBlockDriftMitigation`, `TestYouTubeEmbedWrapping`, `TestOrangetrackPingAggregator`, `TestDispatcherIntegration` — все зелёные.
- [ ] `tests/test_boilerplate_filter.py` дополнен 5-7 кейсами (positive + negative + boundary + ReDoS-safety).
- [ ] `tests/test_sources_registry.py` set-assertion обновлён: `{'autoevolution', 'lamley', 'mattel', 'orangetrack'}`.
- [ ] `pytest tests/` зелёный.
- [ ] Pre-commit hooks зелёные.
- [ ] После push на `dev`: `news_bot_test.service` стартует без `ImportError`; первая orangetrack-публикация на test channel рендерится корректно.

## Implementation Tasks

### Wave 1 (независимые)

#### Task 1: Create `orangetrack_source.py` parser module
- **Description:** Создать `orangetrack_source.py` с публичной функцией `fetch_orangetrack_article(entry, notifier=None)`, классом `OrangetrackPingAggregator`, helper-ами `_is_allowed_orangetrack_url`, `_video_embed_url` (с hostname allowlist), `_safe_for_ping`. Module-level constant `_FEED_URL = 'https://orangetrackdiecast.com/feed/'`. Реализует Decisions 1, 2, 3, 5, 8, 13, 15. Unit-тесты в `tests/test_orangetrack_source.py` по классам из Testing Strategy.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "import orangetrack_source as o; print(o.fetch_orangetrack_article, o.OrangetrackPingAggregator); a = o.OrangetrackPingAggregator('test'); a.add('FEED_HTTP_503', 'https://x/y'); print(a.format_summary())"` → prints non-None symbols and aggregator format
- **Files to modify:** `orangetrack_source.py` (new), `tests/test_orangetrack_source.py` (new)
- **Files to read:** `lamley_source.py`, `autoevolution_source.py`, `boilerplate_filter.py`, `tests/test_lamley_source.py`, `work/orangetrack-source/code-research.md`, `work/orangetrack-source/user-spec.md`

#### Task 2: Extend `boilerplate_filter.py` with affiliate-line patterns
- **Description:** Добавить 1-3 ReDoS-safe паттерна в `_BOILERPLATE_PATTERNS` (Decision 12). Тесты в существующих `TestIsBoilerplatePositive` / `TestIsBoilerplateNegative` parametrize lists + новый класс `TestAffiliateLengthBound` для boundary-кейсов.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `boilerplate_filter.py`, `tests/test_boilerplate_filter.py`
- **Files to read:** `boilerplate_filter.py`, `tests/test_boilerplate_filter.py`

### Wave 2 (зависит от Wave 1)

#### Task 3: Wire orangetrack into `news_bot.py`
- **Description:** Добавить в `news_bot.py`: записи в `NETLOC_TO_SOURCE`, `SOURCE_EMOJI`, `SOURCE_LABEL`; новую функцию `_fetch_orangetrack_entries` (по Decision 4 — owns aggregator instance, performs full parse cycle, attaches body to entries); ветку pass-through в `fetch_full_article` для orangetrack-домена; добавить `_fetch_orangetrack_entries` в `SOURCES`. Важно: shared `_fetch_rss_entries` НЕ итерирует orangetrack URL (фильтр или отдельный source URL constant).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "import news_bot; assert news_bot.NETLOC_TO_SOURCE.get('orangetrackdiecast.com') == 'orangetrack'; assert any(f.__name__ == '_fetch_orangetrack_entries' for f in news_bot.SOURCES); print('OK')"`
- **Files to modify:** `news_bot.py`
- **Files to read:** `news_bot.py`, `orangetrack_source.py` (Task 1 result), `work/orangetrack-source/code-research.md`

#### Task 4: sources registry test update
- **Description:** Обновить `tests/test_sources_registry.py` set-assertion на 4 источника (Decision 11). `feeds.json` НЕ модифицируется (Decision 4 — orangetrack URL живёт как module-level constant).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files to modify:** `tests/test_sources_registry.py`
- **Files to read:** `tests/test_sources_registry.py`

#### Task 5: Deploy FILES list synchronization
- **Description:** Добавить `orangetrack_source.py` в FILES list **обоих** файлов: `deploy.sh` и `.github/workflows/deploy.yml`. INVARIANT: побайтное совпадение между двумя файлами. Без этого прод падает с ImportError при следующем рестарте.
- **Skill:** deploy-pipeline
- **Reviewers:** code-reviewer, security-auditor, deploy-reviewer
- **Verify-smoke:** `diff <(grep -E '^\s*orangetrack_source\.py' deploy.sh) <(grep -E '^\s*orangetrack_source\.py' .github/workflows/deploy.yml)` → no output (both files have the entry, identical text)
- **Files to modify:** `deploy.sh`, `.github/workflows/deploy.yml`
- **Files to read:** `deploy.sh`, `.github/workflows/deploy.yml`, `.claude/skills/project-knowledge/references/deployment.md`

### Audit Wave

<!-- Full-feature audit: 3 auditors review all code in parallel. -->

#### Task 6: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified in Tasks 1-5. Review holistically for cross-component issues: duplicate logic with autoevolution/lamley parsers, aggregator class boundaries, naming consistency, error-handling completeness, dead code. Write audit report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 7: Security Audit
- **Description:** Full-feature security audit. Read all source files created/modified. Verify: SSRF allowlist guard active before HTTP, `allow_redirects=False` enforced, bounded streaming on body read, scheme filtering on hrefs/srcs, YouTube hostname allowlist, ReDoS-safety on regex, aggregator size bounds, control-char sanitization on adversary-controlled strings. OWASP Top 10 across components.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 8: Test Audit
- **Description:** Full-feature test quality audit. Read `tests/test_orangetrack_source.py` + new tests in `tests/test_boilerplate_filter.py` + updated `tests/test_sources_registry.py`. Verify all behavioral assertions are meaningful (no `assert True`-style placeholders). Verify SSRF / aggregator lifecycle / scheme-filter / size-cap edge cases all have tests. Test pyramid balance.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

<!-- QA mandatory; Deploy applicable; Post-deploy applicable. -->

#### Task 9: Pre-deploy QA
- **Description:** Acceptance testing. Run `pytest tests/` — all green. Verify all user-spec ACs and tech-spec ACs are met by reading code + running smoke checks. Verify pre-commit hooks pass on staged changes. Verify FILES list invariant (manual diff `deploy.sh` vs `.github/workflows/deploy.yml`).
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 10: Deploy via dev push (CI auto-fires test deploy)
- **Description:** `git push origin dev` → CI on dev runs `ci.yml` (pytest) → on green, `deploy_test.yml` SCPs FILES list to `/home/hwbot/bot_test/`, restarts `news_bot_test.service`. Verify GitHub Actions UI shows both workflows green.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 11: Post-deploy verification on test channel
- **Description:** Live environment verification on test instance:
  - Verify `journalctl -u news_bot_test.service -n 100 --since "5 minutes ago"` (operator-side) shows bot started without `ImportError` and orangetrack feed was fetched. Tool: bash + journalctl.
  - Wait for first orangetrack publication on test channel `@myhwchannel123`. Verify hashtag is `#orangetrackdiecast #news`, IV preview card renders with hero image and INSTANT VIEW button, Telegra.ph page has Russian title with emoji prefix, subtitle, paragraphs interleaved with images and embedded YouTube video (if applicable) in original order. Tool: Telegram MCP or operator-visual.
  - Verify no admin-ping flood; if any pings appeared, decode the codes per Decision 5 taxonomy and confirm they're sensible. Tool: Telegram MCP for admin pings, journalctl for backend logs.
  Tools: bash, journalctl, Telegram MCP (or operator-visual).
- **Skill:** post-deploy-qa
- **Reviewers:** none
