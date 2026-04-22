# code-research.md — manual-review-workflow

Research target: splitting the existing auto-posting cron into a prep phase
(fetch+translate+stage into `pending_articles`) and a user-triggered review/publish
phase via a new CLI (`hw_review.py`). All file:line refs below target current HEAD.

---

## 1. Integration points

### `news_bot.job()` — current shape
File: `news_bot.py:423-449`. Line-by-line:
- `423` signature `def job():`, no args, returns nothing.
- `426` `feed_urls = load_feeds()` reads `feeds.json` (validates scheme/netloc, caps at 5).
- `427-429` fallback: if empty, use `[RSS_URL]` (hardcoded autoevolution tag feed, `news_bot.py:33`).
- `431-441` loop over `feed_urls`: calls `fetch_rss(url)` (`news_bot.py:143-152`); exceptions inside the loop are caught locally; each entry gets `entry['feed_url']=url`; results appended to `all_entries`.
- `443-445` calls `fetch_mattel_news(notifier=send_admin_notification)` and extends `all_entries`. This is the second source — not driven by `feeds.json`, hardcoded here.
- `447` `new_entries = filter_new_entries(all_entries)` — dedupes within the run and against `processed_news` via `is_processed()` (`news_bot.py:154-164`).
- `448` `process_new_articles(new_entries, limit=3)` — the hot path that fetches full article, translates, publishes to Telegraph, posts teaser, marks processed.
- `449` final log.

### `process_new_articles` and where `fetch_full_article` fits
File: `news_bot.py:360-420`.
- `364` iterates `entries[:limit]` (limit=3 today).
- `371` `article = fetch_full_article(entry)` — dispatcher at `news_bot.py:336-357`.
  - Domain match: `corporate.mattel.com` → `fetch_mattel_article(link, notifier=send_admin_notification)` (`mattel_news_source.py:144`).
  - `lamleygroup.com` → `lamley_source.fetch_lamley_article(link, notifier=send_admin_notification)` (`lamley_source.py:37`).
  - `autoevolution.com` → `autoevolution_source.fetch_autoevolution_article(entry)` (`autoevolution_source.py:303`).
  - No handler → `logger.warning(...); return None`.
  - Blanket `except Exception` at line 353 returns `None`.
- `372-374` guard: `None` or no `paragraphs` → skip (no DB write).
- `376-400` translation pass: title, subtitle, paragraphs, and optional `blocks` (only autoevolution yields blocks).
- `402-413` call `telegraph_publisher.publish_article(...)`; `TelegraphError`/`RequestException` → skip, DO NOT mark processed.
- `415-419` `send_telegraph_teaser(telegraph_url, link)` → success marks processed; failure just logs.

### `send_admin_notification`
File: `news_bot.py:38-52`. Signature `send_admin_notification(message)`; returns `bool`.
- Reads module-level `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ADMIN_ID` (`news_bot.py:29-31`). `TELEGRAM_ADMIN_ID` defaults to `'@sunny413x'` if env var not set.
- Wraps `Bot(token).send_message(chat_id=ADMIN, text=message, parse_mode='Markdown')` inside `asyncio.run(_send())` — each call spins up a new event loop.
- Error path: `TelegramError` caught, returns `False`. No rate limiting; no retry. Every caller catches its own exceptions or no-ops — see `load_feeds` (`news_bot.py:63,71,81,89,98`), `_notify` helpers in `mattel_news_source.py:134-141` and `lamley_source.py:27-34`.
- Consumers pass it as `notifier=send_admin_notification` into source fetchers (`news_bot.py:348,350,443`).

### `telegraph_publisher.publish_article`
File: `telegraph_publisher.py:225-266`.
Signature:
```
publish_article(title, paragraphs=None, images=None, source_url=None,
                subtitle="", blocks=None, access_token=None,
                author_name=DEFAULT_AUTHOR_NAME, session=None) -> str
```
- Reads token from arg or env (`ENV_TOKEN_KEY = "TELEGRAPH_ACCESS_TOKEN"` at line 23); raises `TelegraphError` if missing.
- Builds node tree via `_build_content_from_blocks` (when `blocks` given) or `_build_content` (flat lists).
- Calls `_api_call("createPage", {...})` (`telegraph_publisher.py:255`) — returns `result["url"]`.
- There is **no `editPage` call path today**. No `path` is persisted; `result["path"]` is explicitly discarded (only `url` is returned).

### Telegra.ph API wrapper pattern
- `_api_call(method, data, session=None)` at `telegraph_publisher.py:30-37`: POSTs `https://api.telegra.ph/{method}`, 15s timeout, raises `TelegraphError` on `ok: false`.
- `ensure_access_token(env_path=".env", session=None)` at `telegraph_publisher.py:74-89`: returns cached env var or calls `create_account` (which calls `_api_call("createAccount", …)`) and persists the token via `_save_token_to_env`.
- Adding `editPage` would follow the same shape: `_api_call("editPage", {"access_token": …, "path": …, "title": …, "content": …, …})`. Requires `path` — the publisher currently drops it, so the caller must start storing it.

### `send_telegraph_teaser` inputs
File: `news_bot.py:303-333`. Signature `(telegraph_url, source_url) -> bool`.
- Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHANNEL_ID`; returns `False` if missing.
- Body text is derived solely from `source_url` via `_source_hashtag` (`news_bot.py:291-300`, e.g. `#mattel`, `#autoevolution`, `#lamleygroup`).
- `Bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode='Markdown', link_preview_options=LinkPreviewOptions(url=telegraph_url, show_above_text=True))`.
- Error path: `TelegramError` caught → `False`. `asyncio.run(_send())` per call.

---

## 2. Database

### Current `init_db()` schema
File: `news_bot.py:113-121`.
```sql
CREATE TABLE IF NOT EXISTS processed_news (
    link TEXT PRIMARY KEY,
    title TEXT,
    pub_date TEXT,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```
DB file: `news.db` (module-level `DB_FILE`, `news_bot.py:34`). Connection opened/closed per call (no pool, no `sqlite3` context manager).

### Migration mechanism
There is no migration framework. Every function uses `sqlite3.connect(DB_FILE)` directly (`news_bot.py:115,125,134`). The only schema evolution pattern used is `CREATE TABLE IF NOT EXISTS`. Adding `pending_articles` would mean appending a second `CREATE TABLE IF NOT EXISTS ...` inside `init_db()` (or a dedicated init in a new repo module).

### `is_processed` / `mark_processed`
- `is_processed(link) -> bool` at `news_bot.py:123-130`.
- `mark_processed(link, title, pub_date) -> None` at `news_bot.py:132-140`. `processed_at` is auto-filled by the DEFAULT.
- Both are used in `filter_new_entries` (`news_bot.py:160`) and `process_new_articles` (`news_bot.py:416`). Tests `tests/test_database.py:15-97` assert exact SQL text — changing the literal will break those tests.

---

## 3. Source fetchers contract

### Return shapes
| Source | Function (file:line) | title | subtitle | paragraphs | images | blocks |
|---|---|---|---|---|---|---|
| Mattel corp | `mattel_news_source.py:206-211` | yes | yes (excerpt) | list[str] | list[str] | — |
| Lamley | `lamley_source.py:103-108` | yes | yes (first body para lifted out) | list[str] | list[str] (cap 10) | — |
| Autoevolution (scraped) | `autoevolution_source.py:294-300` | yes | yes | list[str] | list[str] (cap 10) | **list[dict]** |
| Autoevolution (RSS fallback) | `autoevolution_source.py:346-351` | yes | `""` | list[str] | list[str] | — |

Deviations:
- Only autoevolution's scraped branch returns `blocks` (ordered paragraph/image/video/heading/lead — see shapes in `telegraph_publisher.py:99-110`). `process_new_articles` checks `article.get('blocks')` before translating them (`news_bot.py:386-400`).
- All four shapes include `title`, `subtitle`, `paragraphs`, `images`. `subtitle` may be `""`. `paragraphs` may be empty (e.g., if scrape found nothing) — `process_new_articles:372` uses `not article.get('paragraphs')` as a skip signal.
- `fetch_full_article` returns `None` on unknown domain or caught exception — callers must handle (`news_bot.py:353-357`).

### Dispatch logic
`news_bot.py:336-357`. Domain-based string matching on `urlparse(link).netloc.lower()`. No registry — new sources need to edit this function.

---

## 4. Existing tests

11 test files (not 106 — there are 11) under `tests/`:

| File | Lines | Covers | Affected by split |
|---|---|---|---|
| `tests/test_autoevolution_source.py` | 249 | scrape + RSS fallback + video embeds | no (source-local) |
| `tests/test_config_loader.py` | 98 | `load_feeds` validation, fallback | no |
| `tests/test_database.py` | 100 | init/is_processed/mark_processed, literal SQL asserted | **yes — add new table tests; existing ones still pass** |
| `tests/test_feed_iteration.py` | 144 | `job()` iterates feeds, error isolation, `limit=3` | **yes — asserts `process_new_articles(..., limit=3)` call shape** |
| `tests/test_integration.py` | 220 | full pipeline, DB writes, teaser failure paths | **yes — asserts auto-publish flow end-to-end** |
| `tests/test_lamley_source.py` | 93 | HTML parse, limits, notifier | no |
| `tests/test_mattel_integration.py` | 119 | HTTP fixture → `job()` → SQLite → mocked Telegram | **yes — asserts auto-publish on every `job()` run** |
| `tests/test_mattel_news_source.py` | 318 | Mattel source unit tests | no |
| `tests/test_telegram.py` | 112 | `_source_hashtag`, `send_telegraph_teaser` | no |
| `tests/test_telegraph_publisher.py` | 250 | `_api_call`, `ensure_access_token`, `_build_content*`, `publish_article` | **yes — new `editPage` wrapper tests** |
| `tests/test_translation.py` | 77 | `translate_text` | no |

Tests that currently assert auto-publish (each call of `job()` ends in a `publish_article` + `send_telegraph_teaser` call + `mark_processed` row) — these will need to flip to "no auto-publish, only stage into `pending_articles`":
- `tests/test_integration.py:54` — `test_full_pipeline_with_multiple_feeds` asserts `mock_publish.call_count == 3`, `mock_send_teaser.call_count == 3`, and that rows appear in `processed_news`.
- `tests/test_integration.py:115` — `test_duplicate_skipping` asserts publish count.
- `tests/test_integration.py:146` — `test_error_isolation` same.
- `tests/test_integration.py:178` — `test_telegraph_failure_skips_teaser_and_db` expects publish attempt.
- `tests/test_integration.py:205` — `test_no_article_data_skips_publish`.
- `tests/test_feed_iteration.py:35` — asserts `process_new_articles.assert_called_once_with(..., limit=3)`.
- `tests/test_mattel_integration.py:59` — `test_mattel_post_flows_into_telegram_and_db` expects teaser sent + row in `processed_news`.
- `tests/test_mattel_integration.py:104` — `test_mattel_duplicate_is_not_reposted` runs `job()` twice expecting only 1 teaser.

### Fixtures/mocks
- HTML fixture: `tests/fixtures/mattel_news.html` (only one real fixture on disk).
- Telegram mocks: `unittest.mock.AsyncMock` on `Bot.send_message` (pattern at `tests/test_telegram.py:47-67`).
- Telegraph mocks: `MagicMock(spec=requests.Response)` factory `_make_response` reused in `tests/test_telegraph_publisher.py:15-20` and `tests/test_mattel_news_source.py:35-44`.
- Source fetch mocks: `patch('news_bot.fetch_full_article', ...)` and `patch('news_bot.fetch_rss', ...)` and `patch('news_bot.fetch_mattel_news', return_value=[])` (all in `tests/test_integration.py`).
- DB fixture: `tempfile.mkstemp(suffix='.db')` + `patch('news_bot.DB_FILE', ...)` + `news_bot.init_db()` (pattern at `tests/test_integration.py:20-26`, `tests/test_mattel_integration.py:22-30`).
- Representative signatures:
  - `def test_full_pipeline_with_multiple_feeds(self, mock_load_feeds, mock_fetch_rss, mock_fetch_article, mock_transcreate, mock_publish, mock_send_teaser)` — `tests/test_integration.py:54`.
  - `def test_mattel_post_flows_into_telegram_and_db(self, mock_get, mock_transcreate, mock_article, mock_feeds, mock_fetch_rss, mock_publish, mock_tg)` — `tests/test_mattel_integration.py:59`.

---

## 5. Configuration and secrets

| Var | Defined | Read at | Used in |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | env | `news_bot.py:29` | `send_admin_notification` (`:40`), `send_telegraph_teaser` (`:309`) |
| `TELEGRAM_CHANNEL_ID` | env | `news_bot.py:30` | `send_telegraph_teaser` (`:309`) |
| `TELEGRAM_ADMIN_ID` | env, default `@sunny413x` | `news_bot.py:31` | `send_admin_notification` (`:40,46`) |
| `TELEGRAPH_ACCESS_TOKEN` | env (persisted to `.env`) | `telegraph_publisher.py:23,79,247` | `ensure_access_token`, `publish_article` |

Module-level reads mean any test must patch `news_bot.TELEGRAM_BOT_TOKEN` etc. directly — see pattern at `tests/test_integration.py:26-29`. `.env` file at `/workspaces/debian-2/my-hw/.env` contains all four values today; `.env.example` only lists the first three.

### Schedule
`news_bot.py:458` — `schedule.every().day.at("12:00").do(job)`. `schedule` library (v1.2.1, `requirements.txt:5`) supports `every(X).minutes`, `every(X).hours`, etc. Bumping to e.g. hourly prep is a 1-line change. Note `news_bot.py:461` also runs `job()` immediately on start. Loop at `:464-466` polls every 60s.

---

## 6. `transcreate_text` as fallback

File: `news_bot.py:177-289`.
Signature: `transcreate_text(text, source='auto', target='ru', is_title=False) -> str`.

Behavior:
- Calls `GoogleTranslator(source, target).translate(text)` (`:190`). On exception, uses original text.
- If translated is empty/whitespace, returns original (`:195-196`).
- Applies bureaucratic → plain Russian substitutions (`:201-223`).
- Passive → active substitutions (`:226-229`).
- Hot Wheels glossary (`:234-252`).
- If `is_title=True`: prepends a deterministic emoji based on regex-matched keywords (`:256-273`).
- If body: truncates to 4000 chars on sentence boundary (`:276-287`).

Pure-fallback viability: the function has **no external state** beyond `GoogleTranslator` (stateless per call) and the regex constants. It does not read DB, env, or files. Calling it again on paragraphs that have already passed through it would re-apply bureaucratic/glossary passes (idempotent for most patterns) and translate already-Russian text (`source='auto'`, so Google returns it unchanged for Russian input). The title-emoji prefix would duplicate if called twice on a title — so for the stale-fallback path operating on cached `paragraphs` (not cached `ru_*` fields), it's safe. The feature's plan stores pre-translated `ru_*` columns, so the fallback only runs when `ru_*` is still `NULL` — clean.

---

## 7. Risks and landmines

### Splitting `job()` into prep + publish
- **Concurrency on cron overlap.** Current loop (`news_bot.py:464-466`) single-threaded, but the prep phase could still exceed its interval if sources hang (autoevolution via `curl_cffi` with 20s timeout per page, up to 3 articles = 60s worst case, plus Telegraph-less now). `init_db` is not idempotent-safe in concurrent workers because SQLite connections are per-call with no WAL mode set. Any new `pending_articles` writes need careful UNIQUE constraints on `link` to avoid duplicate inserts if two preps overlap.
- **Partial state between fetch and insert.** `process_new_articles` today only calls `mark_processed` AFTER Telegraph publish + teaser succeed (`news_bot.py:416`). If the new prep fetches full article (`fetch_full_article`) but crashes before INSERT into `pending_articles`, nothing is recorded — on the next run, `filter_new_entries` (`news_bot.py:154-164`) still says "new" and re-fetches. That's actually safe, just wasteful. If prep crashes AFTER insert but before translation, the row is there with `ru_*` NULL — the stale-fallback path needs to handle that.
- **Lost entries on pre-translate crash.** Current pipeline translates in-memory then publishes. New pipeline: the plan has translation at prep time (stored in `ru_*` columns). If Google Translate hiccups mid-loop, some rows land with partial `ru_*` — need nullable columns and a "needs translation" state or just re-run translation lazily.
- **`fetch_mattel_news` is hardcoded inside `job()`** at `news_bot.py:443`. The split needs to preserve it — it's not in `feeds.json`.

### "Publish happens right after fetch" assumptions
- **Autoevolution gallery images.** Images come from `s1.cdn.example`-style URLs — real URLs are `s1.cdn.autoevolution.com`. These CDN URLs are long-lived, not hotlink-tokened — safe to cache for 48h.
- **Mattel `download_media` and `thumbnail.url`.** These are served from `corporate.mattel.com` CDN via signed-ish URLs but no observed expiry. Not proven to last 48h; worth spot-checking during implementation.
- **Lamley WordPress images.** Stable WP CDN; no expiry.
- **Google Translate quota.** Translation is done at prep time now, once per article. If a stale-fallback re-runs it, that's a second call per article — not expected to hit quota.
- **Telegraph `createPage` is called once per article today.** No retry loop. Preview URL needed for admin ping must come from a separate call (e.g. a draft page) OR be constructed after publish. Plan's `preview_url` column implies creating a draft-view early — that's a second `createPage` call up front.

### `editPage` on Telegra.ph
- Not used today. The Telegra.ph public API docs list `editPage(access_token, path, title, content, author_name?, author_url?, return_content?)` — returns the edited page object. Requires `path` (not URL).
- Quirks to watch for when wiring `editPage` through the existing wrapper:
  - `_api_call` POSTs `data=data` — `data` is form-encoded and `content` must be JSON-stringified exactly as `createPage` does (`telegraph_publisher.py:261`). Same serialization rules apply (array of node dicts).
  - `access_token` is per-account — the one stored in `.env` will work only for pages created by that same account. If the token is ever rotated, older pages become un-editable. Current `ensure_access_token` only creates on miss (`telegraph_publisher.py:82`) — not a direct risk unless `.env` is wiped.
  - The node-tree validator runs for both `createPage` and `editPage` — same constraints. Unknown tags are silently dropped. Current `_build_content_from_blocks` emits only `p/figure/img/figcaption/iframe/h3/h4/hr/i/b/a` — all supported.
  - `publish_article` currently discards `result["path"]` (only `url` is returned). To support `editPage` you must either change `publish_article`'s return type or read `path` from the URL (the path segment of the Telegraph URL matches the API `path`, e.g. `https://telegra.ph/Test-04-20` → `Test-04-20`).

---

## 8. Feasibility notes

### Telegra.ph `editPage`
Publicly documented at https://telegra.ph/api#editPage (referenced by existing wrapper's API_BASE `telegraph_publisher.py:19`). Parameters:
- `access_token` (required)
- `path` (required, no leading slash)
- `title` (required, 1–256 chars)
- `content` (required, JSON array of Nodes/NodeElements)
- `author_name` (optional)
- `author_url` (optional)
- `return_content` (optional bool, default false)

No delete endpoint exists — the plan's workaround of `editPage` to blank/placeholder content for `cleanup-drafts` is the documented workaround.

### CLI scaffolding in repo
No existing CLI framework (no click/typer in `requirements.txt`). Existing "scripts" use bare `sys.argv` indexing (e.g. `update_interview.py:5`) or `if __name__ == "__main__": main()` patterns with positional `argv` parsing (e.g. `get_latest_article.py:54`, `post_latest_news.py:96`). A new `hw_review.py` with `argparse` (stdlib, no new dep) would be the first proper CLI in the repo.

### Repository/DAO pattern
There is no existing repository layer. All DB access is 3 functions directly in `news_bot.py` (`init_db`, `is_processed`, `mark_processed`). A new `pending_articles_repo.py` would be the first dedicated data-access module. Naming convention and placement to match existing sources: top-level `*_source.py` style → `pending_articles_repo.py` at repo root fits the pattern.

### Other useful context
- Python 3.13 on CI (`.github/workflows/ci.yml:32`).
- `curl_cffi==0.15.0` (`requirements.txt:7`) is used only by autoevolution scraping; any new code path can stick to stock `requests`.
- `schedule` library supports sub-daily cadence directly — bumping prep cadence to hourly is a one-liner at `news_bot.py:458`.
- No pre-commit hooks configured in repo (no `.pre-commit-config.yaml`). CI runs pytest only, and only if non-doc files changed (`.github/workflows/ci.yml:20-24`).
- No WAL-mode or concurrent-writer SQLite handling; single-threaded assumption baked in.
- `schedule.run_pending()` + `time.sleep(60)` loop at `news_bot.py:464-466` is the only long-running process; the CLI would run out-of-process against the same `news.db`.

---

## 9. Implementation-level details

Appended 2026-04-22. Deepens §1–8 with concrete shapes for tech-spec. Refs: `news_bot.py`, `telegraph_publisher.py`, `tests/*.py`, `work/manual-review-workflow/user-spec.md`.

### 9.1 DDL for the three new tables

Conventions: SQLite + stdlib `sqlite3`, `CREATE TABLE IF NOT EXISTS` only (matches `news_bot.py:113-121`). UTF-8 by default — safe for Cyrillic. JSON fields stored as `TEXT` via `json.dumps(..., ensure_ascii=False)` on write, `json.loads` on read.

**`pending_articles`** — WIP queue, one row per article awaiting review.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link` | TEXT | PRIMARY KEY | dedup / UNIQUE by virtue of PK (acceptance criterion line 56) |
| `source_name` | TEXT | NOT NULL | one of `'rss'`, `'mattel'`, `'lamley'` — drives admin-ping counting (see 9.4) |
| `feed_url` | TEXT | | original `entry['feed_url']` if source is RSS; NULL for mattel |
| `title` | TEXT | NOT NULL | English original, from source fetcher |
| `subtitle` | TEXT | NOT NULL DEFAULT '' | empty string for sources without subtitle (autoevolution RSS fallback) |
| `paragraphs` | TEXT | NOT NULL | `json.dumps(list[str])` |
| `images` | TEXT | NOT NULL DEFAULT '[]' | `json.dumps(list[str])` |
| `blocks` | TEXT | | `json.dumps(list[dict])` or NULL — only autoevolution scraped branch populates |
| `ru_title` | TEXT | | NULL until operator runs `stage` |
| `ru_subtitle` | TEXT | | NULL until staged (may be empty string once staged) |
| `ru_paragraphs` | TEXT | | `json.dumps(list[str])` or NULL. Empty list serialized as `'[]'` — distinguishes "empty" (valid staged) from NULL (not staged). See 9.13 for serialization concern. |
| `ru_blocks` | TEXT | | `json.dumps(list[dict])` or NULL (only set when source had blocks) |
| `telegraph_url` | TEXT | | NULL until first successful `createPage`. Populated for publish-retry reuse (9.9). |
| `telegraph_path` | TEXT | | NULL until first successful `createPage`. Path component for potential future `editPage`. |
| `fetched_at` | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP | age driver for idle-fallback (>48h) and overflow eviction order |
| `notified_at` | TIMESTAMP | | NULL until heads-up ping sent. `take` clears it back to NULL. |
| `attempt_count` | INTEGER | NOT NULL DEFAULT 0 | incremented on idle-fallback failure and overflow-fast-track failure (user-spec L70) |
| `last_error` | TEXT | | last exception message for `hw_review show N` |
| `pub_date` | TEXT | | mirrors `processed_news.pub_date` — kept for move_to_published |

Indexes: PK on `link` suffices. Table bounded at 10 rows (user-spec L84) → scans are cheap, no secondary indexes.

**`published_articles`** — audit trail, one row per article that actually reached the channel.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link` | TEXT | PRIMARY KEY | source article URL (also present in `processed_news`) |
| `title` | TEXT | NOT NULL | EN original at publish time |
| `ru_title` | TEXT | NOT NULL | RU final |
| `telegraph_url` | TEXT | NOT NULL | publish result URL |
| `telegraph_path` | TEXT | | path component parsed from URL (may be NULL if parse fails) |
| `source_name` | TEXT | NOT NULL | `'rss'` / `'mattel'` / `'lamley'` |
| `published_at` | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP | server time, not pub_date |
| `via_review` | INTEGER | NOT NULL | 1 = operator approved via CLI; 0 = idle-fallback or overflow-fast-track auto-published (user-spec L36,L68) |

Skipped rows never land here — skip writes only to `processed_news` (AC L50, L65).

**`failed_articles`** — dead letter after 3 attempts (user-spec L70).

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `link` | TEXT | PRIMARY KEY | |
| `title` | TEXT | NOT NULL | for footer listing in `hw_review list` (AC L71) |
| `source_name` | TEXT | NOT NULL | |
| `paragraphs` | TEXT | NOT NULL | preserved so `retry N` can re-queue without re-fetching source (AC L72) |
| `images` | TEXT | NOT NULL DEFAULT '[]' | |
| `blocks` | TEXT | | |
| `subtitle` | TEXT | NOT NULL DEFAULT '' | |
| `pub_date` | TEXT | | |
| `feed_url` | TEXT | | |
| `last_error` | TEXT | | |
| `failed_at` | TIMESTAMP | NOT NULL DEFAULT CURRENT_TIMESTAMP | |
| `original_fetched_at` | TIMESTAMP | | fetched_at from the pending row, kept for forensics |

EN fields are kept on `failed_articles` so `retry N` re-queues without re-fetching source (user-spec L44, L72).

`processed_news` (existing): semantics extends per user-spec L82 — every published-or-skipped link (AC L74). No schema change; only callers expand.

### 9.2 `pending_articles_repo.py` — function signatures

New module at repo root (matches `*_source.py` style, §8). Plain-dict return shape (no dataclass), consistent with existing `entry = {...}` conventions across `news_bot.py`, `mattel_news_source.py:206-211`, `lamley_source.py:103-108`.

All functions read `news_bot.DB_FILE` at call time (shares the tempfile patch in tests — pattern at `tests/test_integration.py:23`).

```python
def init_schema(conn: sqlite3.Connection) -> None
    # Creates the 3 new tables if missing. Called by news_bot.init_db().

def insert_pending(entry: dict) -> bool
    # INSERT OR IGNORE. entry keys: link, source_name, feed_url, title, subtitle,
    # paragraphs (list), images (list), blocks (list|None), pub_date.
    # JSON fields serialized internally. True on insert, False on UNIQUE conflict (§7 race).

def get_pending(link: str) -> dict | None
    # SELECT * WHERE link=?. Deserializes paragraphs/images/blocks/ru_paragraphs/ru_blocks.

def list_pending() -> list[dict]
    # ORDER BY fetched_at ASC. Backs CLI list + admin-ping counter.

def list_pending_stale(hours: int = 48) -> list[dict]
    # fetched_at < now-hours AND notified_at IS NULL — heads-up candidates.

def list_notified_overdue(grace_hours: int = 2) -> list[dict]
    # notified_at < now-grace AND ru_paragraphs IS NULL — GT-fallback due.

def list_pending_for_eviction() -> list[dict]
    # ru_paragraphs IS NULL ORDER BY fetched_at ASC (user-spec L39, L109).

def update_staged(link, ru_title, ru_subtitle, ru_paragraphs, ru_blocks) -> bool
    # UPDATE ru_*. False if link no longer pending (AC L63).

def mark_notified(link) -> None       # SET notified_at = CURRENT_TIMESTAMP
def clear_notified(link) -> None      # SET notified_at = NULL — `take N` (AC L66)
def increment_attempt(link, error) -> int  # returns new count (3-strike check)

def move_to_published(link, telegraph_url, telegraph_path, via_review) -> None
    # Single txn: INSERT published + INSERT OR IGNORE processed_news + DELETE pending.

def move_to_failed(link, last_error) -> None
    # Single txn: INSERT failed (copy EN fields) + DELETE pending.

def retry_from_failed(link) -> bool
    # Single txn: INSERT pending (fresh fetched_at, attempt_count=0, ru_* NULL) + DELETE failed.

def list_failed() -> list[dict]         # ORDER BY failed_at DESC — footer in `list`
def count_pending() -> int
def mark_telegraph_published(link, telegraph_url, telegraph_path) -> None
    # UPDATE pending. Called between createPage OK and send_telegraph_teaser (AC L62).

def skip_pending(link) -> None
    # Single txn: INSERT OR IGNORE processed_news + DELETE pending. No published row (AC L74).
```

Connection handling: mirror `news_bot.is_processed` pattern (`news_bot.py:123-130`) — `sqlite3.connect(news_bot.DB_FILE)` per call. Multi-statement ops (`move_to_published`, `move_to_failed`, `skip_pending`) wrap in one `conn`, `commit()` on success, `rollback()` on exception.

### 9.3 `hw_review.py` CLI surface

Stdlib `argparse`, subparser pattern (user-spec L89: no new deps). Exit 0 on success, 1 on any error. Uses `news_bot.logger` (AC L75). Human output to stdout, errors to stderr.

`N` is a 1-based index into the current `list` output. Indexes are not stable across prep runs — every command re-resolves via a fresh `list_pending()`.

**`hw_review list`**
- Args: none.
- Output: if `count_pending() == 0` prints `"queue is empty"`. Else one line per row:
  `"{i}. [{source_emoji}] {title} ({n_paragraphs}п, {age_hours}h)"`.
- Footer (always on, regardless of queue): if `list_failed()` non-empty, emit to stdout:
  `"⚠️ {K} неопубликованных в failed: [title1, title2, ...]. hw_review retry N чтобы переподнять."` (AC L71).
- Exit: 0.

**`hw_review show N`**
- Args: positional `n: int`.
- Output: EN title, subtitle, paragraphs (truncated to ~200 chars each for readability),
  images count, blocks count, `ru_*` state (filled or NULL per field),
  `telegraph_url` if set, `last_error` if set, `attempt_count`, `fetched_at`, `notified_at`.
- Exit: 0 if found, 1 if index out of range.

**`hw_review stage N`**
- Args: positional `n: int`; optional flags `--ru-title TEXT`, `--ru-subtitle TEXT`.
  Paragraphs and blocks come as **JSON on stdin** (simplest working option):
  ```
  echo '{"ru_paragraphs": ["абзац 1", "абзац 2"], "ru_blocks": null}' | hw_review stage 3 --ru-title "🔥 Заголовок" --ru-subtitle ""
  ```
  Rationale: Claude Code emits JSON naturally, avoids shell-escaping Russian/newlines, handles null blocks cleanly. Repeated flags lose paragraph order; a temp file adds cleanup burden.
- Validation (AC L59): `ru_title`, `ru_subtitle`, `ru_paragraphs` always required; `ru_blocks` required only when pending row's `blocks` is non-NULL. No confirmation prompt.
- Exit: 0 on success, 1 on partial staging / invalid JSON (stderr: `"staging rejected: missing ru_paragraphs"`).

**`hw_review preview N`**
- Args: positional `n: int`; optional `--no-open` (skips `webbrowser.open`, prints path only — useful for tests and headless env; see 9.13).
- Precondition: row must have staged ru_* (all non-NULL). Else stderr `"nothing to preview — stage Russian text first"`, exit 1 (AC L60).
- Output: stdout prints the HTML file path; if not `--no-open`, calls `webbrowser.open(f"file://{path}")`.
- Exit: 0 / 1.

**`hw_review publish N`**
- Args: positional `n: int`.
- Precondition: row must have staged ru_* (else stderr `"nothing to publish — stage Russian text first"`, exit 1).
- Flow: see 9.9.
- Exit: 0 on channel post success; 1 on any failure. On partial success (Telegraph OK, Telegram fail), exit 1 with stderr `"telegram send failed: {err}. Telegraph URL saved — rerun publish {n} to retry send."`.

**`hw_review skip N`**
- Args: positional `n: int`. (UPDATED: `--yes` bypass removed per tech-spec Decision — skip confirmation is always interactive.)
- If row has `ru_paragraphs IS NOT NULL`, prompt via stdin: `"В записи {n} сохранён русский. Точно скипнуть? [y/N]: "`. Read single line; proceed only on `y`/`Y` (AC L49, L65). Any other input → exit 0 with `"skip cancelled"`.
- Action: `skip_pending(link)` (writes to `processed_news`, removes from `pending_articles`, no `published_articles` row per AC L74).
- Exit: 0.

**`hw_review take N`**
- Args: positional `n: int`.
- If row in `pending_articles` and `notified_at IS NOT NULL` → `clear_notified(link)`, stdout: `"took {link} — resumed normal review"`.
- If row no longer in pending (evicted/auto-published) → stderr `"{n} already auto-published"`, exit 1 (AC L67).
- Exit: 0 / 1.

**`hw_review retry N`**
- `N` indexes `list_failed()` output (ORDER BY `failed_at DESC`) — matches the numbered failed footer in `list`.
- Action: `retry_from_failed(link)`. Exit 0 / 1 (out of range).

### 9.4 Admin-ping construction

Format: `"N ждут review: 🟠 autoevolution ×K, 🟣 mattel ×M, 🟢 lamley ×L"` (user-spec L25, AC L57). Zero-count sources omitted.

Source ID comes from `pending_articles.source_name` (9.1) — no URL parsing. Existing `_source_hashtag` at `news_bot.py:291-300` is unrelated (drives channel post hashtag, not the ping).

**UPDATED 2026-04-22 per tech-spec Decision 4:** keys are now `'autoevolution'` / `'mattel'` / `'lamley'` — no `'rss'` key. Lamley also arrives via RSS, so keying by netloc-derived label keeps the two outlets distinguished.

```python
# In news_bot.py.
SOURCE_EMOJI = {
    'autoevolution': '🟠',
    'mattel':        '🟣',
    'lamley':        '🟢',
}
SOURCE_LABEL = {
    'autoevolution': 'autoevolution',
    'mattel':        'mattel',
    'lamley':        'lamley',
}

def build_admin_ping(rows: list[dict]) -> str | None:
    """Return ping text or None if queue is empty (AC L54, L57)."""
    if not rows:
        return None
    from collections import Counter
    counts = Counter(r['source_name'] for r in rows)
    parts = [f"{SOURCE_EMOJI[k]} {SOURCE_LABEL[k]} ×{counts[k]}"
             for k in ('autoevolution', 'mattel', 'lamley') if counts.get(k)]
    return f"{len(rows)} ждут review: " + ", ".join(parts)
```

Placement: `news_bot.py` next to `_source_hashtag`. Optional extraction into `admin_ping.py` if preferred for test isolation.

### 9.5 Source registry

Replace hardcoded `fetch_mattel_news(...)` at `news_bot.py:443-445` and RSS loop at `news_bot.py:432-441`:

```python
def _fetch_rss_entries(notifier) -> list[dict]:
    """Iterate load_feeds() + fetch_rss(); stamp feed_url + source_name."""
    entries = []
    feed_urls = load_feeds() or [RSS_URL]
    for url in feed_urls:
        try:
            raw = fetch_rss(url)
        except Exception as e:
            logger.error(f"Failed to fetch feed {url}: {e}")
            raw = []
        for entry in raw:
            # feedparser returns FeedParserDict; normalize to plain dict subset.
            item = dict(entry) if not isinstance(entry, dict) else entry
            item['feed_url'] = url
            item['source_name'] = 'rss'
            entries.append(item)
    return entries

def _fetch_mattel_entries(notifier) -> list[dict]:
    items = fetch_mattel_news(notifier=notifier) or []
    for item in items:
        item['source_name'] = 'mattel'
        # Mattel today sets entry['feed_url'] = NEWS_URL; keep that for the row.
    return items

def _fetch_lamley_entries(notifier) -> list[dict]:
    # lamley_source currently lacks a list-fetcher — see code-research §3.
    # If tech-spec scopes lamley-listing in, it lives here. If not, return [].
    items = lamley_source.fetch_lamley_news(notifier=notifier) if hasattr(lamley_source, 'fetch_lamley_news') else []
    for item in items:
        item['source_name'] = 'lamley'
    return items

SOURCES = [
    _fetch_rss_entries,
    _fetch_mattel_entries,
    _fetch_lamley_entries,
]
```

Refinements vs the lambda sketch:
- Each fetcher returns plain dicts with mandatory `source_name` (drives admin ping §9.4).
- RSS loop stays inside `_fetch_rss_entries` — `load_feeds()` returns multiple URLs needing per-URL error isolation (`news_bot.py:432-441`).
- Named functions (not lambdas) keep stack traces readable (`news_bot.py:354`).
- `notifier` param is uniform so each fetcher can surface its own errors (matches `news_bot.py:348,443`).

New source: one `_fetch_X_entries` + one `SOURCES` entry (AC L73).

### 9.6 Local HTML preview

Telegraph node tree: `telegraph_publisher._build_content*` (`telegraph_publisher.py:92-222`), shape `{'tag': str, 'attrs': dict?, 'children': list?}`. Tag set: `p / figure / img / figcaption / iframe / h3 / h4 / hr / i / b / a`.

New module `preview_renderer.py` at repo root (symmetric to `telegraph_publisher.py`) exposing `render_html(nodes, title) -> str`:

```python
_VOID_TAGS = {'img', 'hr', 'br'}
_ALLOWED = {'p', 'figure', 'img', 'figcaption', 'iframe', 'h3', 'h4',
            'hr', 'i', 'b', 'a'}

def render_html(nodes, title):
    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        f'<title>{escape(title)}</title>',
        '<style>body{font:17px/1.6 -apple-system,sans-serif;max-width:700px;margin:2em auto;padding:0 1em}',
        'figure{margin:1em 0}img{max-width:100%;height:auto}',
        'iframe{width:100%;aspect-ratio:16/9}hr{border:none;border-top:1px solid #ccc}</style>',
        '</head><body>',
        f'<h1>{escape(title)}</h1>',
    ]
    for node in nodes:
        parts.append(_render_node(node))
    parts.append('</body></html>')
    return ''.join(parts)

def _render_node(n):
    if isinstance(n, str):
        return escape(n)
    tag = n.get('tag')
    if tag not in _ALLOWED:
        return ''  # match Telegraph's silent-drop behavior (§7)
    attrs = n.get('attrs') or {}
    attr_str = ''.join(f' {k}="{escape(v)}"' for k, v in attrs.items())
    if tag in _VOID_TAGS:
        return f'<{tag}{attr_str}>'
    inner = ''.join(_render_node(c) for c in (n.get('children') or []))
    return f'<{tag}{attr_str}>{inner}</{tag}>'
```

`render_html` is called by `hw_review preview` like so:
```python
nodes = telegraph_publisher._build_content_from_blocks(sub, blocks, link) \
        if blocks else \
        telegraph_publisher._build_content(sub, paragraphs, images, link)
html = preview_renderer.render_html(nodes, ru_title)
```

`_build_content*` are underscore-private. Tech-spec should add a public `preview_nodes(...)` wrapper in `telegraph_publisher.py` that returns the node tree without `createPage` — keeps render logic inside the publisher module.

**Images**: hot-link source URLs. Autoevolution CDN (s1.cdn.autoevolution.com), Mattel corporate CDN, and Lamley WP all load cross-origin without hotlink protection on sampled URLs. Broken preview image only affects local viewing (Telegraph fetches server-side); operator can re-stage if needed. Data-URI inlining rejected: new HTTP dep, timeouts, MB-sized HTML files.

**File location**: `/tmp/hw-review-{md5(link)[:12]}.html`. `/tmp` writable on macOS / Linux / Docker. Overwritten on repeat preview; deleted on `publish` / `skip`. Abandoned rows leak until reboot (≤10 files × ~50KB = 500KB — acceptable).

### 9.7 Grace window

User-spec L34-36, L83-85: `idle_timeout=48h` fixed, `grace_window=2h` default (tunable).

Current cron: `schedule.every().day.at("12:00")` (`news_bot.py:458`). At daily cadence 2h grace is meaningless (next tick is 24h out).

**Proposed: bump to hourly** — one-line change to `schedule.every().hour.do(job)`. Justification:
1. User-spec L83 explicitly scopes this as a 1-line follow-up.
2. Hourly gives the 2h grace default real meaning.
3. Cost: 3 sources × hourly = 72 fetches/day (from 3/day). All three sources are cheap (RSS + 2 HTML pages) — well under any rate limit.
4. `filter_new_entries` dedupes vs `processed_news` + `pending_articles`, so hourly ticks with no new content cost just the HTTP fetches.
5. Admin ping suppressed when queue empty (AC L57) — no spam.

Config constants at `news_bot.py` module level (env-overridable):
```python
IDLE_TIMEOUT_HOURS = int(os.getenv('IDLE_TIMEOUT_HOURS', '48'))
GRACE_WINDOW_HOURS = int(os.getenv('GRACE_WINDOW_HOURS', '2'))
QUEUE_CAP = int(os.getenv('QUEUE_CAP', '10'))
```
Grace check lives at the top of `job()` — see §9.10 step (1).

### 9.8 Overflow semantics (CLI-cron race safe)

User-spec L38-40, L69-70, L109. Pseudocode for the fast-track pass inside `job()`:

```
def _overflow_fast_track(new_entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Mutates pending_articles (evictions), returns (accepted_new, fast_track_errors)."""
    cap = QUEUE_CAP  # 10
    current = pending_repo.count_pending()
    slots_free = cap - current

    if len(new_entries) <= slots_free:
        return new_entries, []  # happy path — no eviction needed

    needed = len(new_entries) - slots_free
    eviction_candidates = pending_repo.list_pending_for_eviction()
    # Already ORDER BY fetched_at ASC; all have ru_paragraphs IS NULL.
    to_evict = eviction_candidates[:needed]
    staged_protected = needed - len(to_evict)

    fast_track_errors = []
    evicted_count = 0
    for row in to_evict:
        try:
            _fallback_publish(row)  # translates via transcreate_text + publish_article
            evicted_count += 1
        except Exception as exc:
            # increment attempt_count; if hit 3, move_to_failed
            new_attempts = pending_repo.increment_attempt(row['link'], str(exc))
            if new_attempts >= 3:
                pending_repo.move_to_failed(row['link'], str(exc))
                evicted_count += 1  # slot freed via move_to_failed
            fast_track_errors.append(row['title'])

    # Now re-check slots: we freed `evicted_count` slots, plus whatever was
    # already free. But new_entries may still not all fit if (a) staged_protected > 0
    # or (b) fast-track failed mid-loop.
    slots_free_after = cap - pending_repo.count_pending()
    accepted = new_entries[:slots_free_after]
    deferred = new_entries[slots_free_after:]

    if deferred or staged_protected or fast_track_errors:
        send_admin_notification(
            f"Queue pressure: auto-published {evicted_count}, "
            f"{len(deferred)} new deferred, "
            f"{staged_protected} staged rows protected"
            + (f", fast-track failed for {len(fast_track_errors)}" if fast_track_errors else "")
        )
    return accepted, fast_track_errors
```

Race analysis: between `count_pending()` and `insert_pending`, operator may run `stage` / `publish` / `skip` / `take`. `publish` and `skip` reduce the count (harmless — we overestimated usage); `stage` and `take` don't touch count. The post-eviction re-count + `INSERT OR IGNORE` (9.2) keep it correct under overlapping prep runs.

AC mapping:
- L64: 10 pending + 0 new → `0 <= 0` early-returns, no fast-track.
- L69: eviction only touches `ru_paragraphs IS NULL` rows.
- L70: 3 cumulative strikes → `failed_articles`.

### 9.9 Telegraph page reuse on publish retry

AC L62. Pending row gains `telegraph_url` and `telegraph_path` columns (9.1). Publish flow in CLI:

```python
def cmd_publish(link: str) -> int:
    row = pending_repo.get_pending(link)
    if not row:
        logger.error(f"{link} no longer pending (evicted/published/failed)")
        return 1
    if row['ru_paragraphs'] is None:
        print("nothing to publish — stage Russian text first", file=sys.stderr)
        return 1

    # Step 1: Telegraph — skip if already done.
    tg_url = row.get('telegraph_url')
    tg_path = row.get('telegraph_path')
    if not tg_url:
        try:
            tg_url = telegraph_publisher.publish_article(
                title=row['ru_title'],
                subtitle=row['ru_subtitle'],
                paragraphs=row['ru_paragraphs'],
                images=row['images'],
                blocks=row['ru_blocks'],
                source_url=row['link'],
            )
        except (TelegraphError, requests.RequestException) as exc:
            logger.error(f"Telegraph publish failed: {exc}")
            return 1
        tg_path = urlparse(tg_url).path.lstrip('/')
        pending_repo.mark_telegraph_published(link, tg_url, tg_path)

    # Step 2: Telegram teaser — retried independently.
    if not send_telegraph_teaser(tg_url, link):
        print(f"telegram send failed. Telegraph URL saved — rerun publish {n} to retry send.",
              file=sys.stderr)
        return 1

    # Step 3: move to published. All-or-nothing txn (§9.2).
    pending_repo.move_to_published(link, tg_url, tg_path, via_review=True)
    _cleanup_preview_html(link)  # §9.6
    print(f"Published: {tg_url}")
    return 0
```

`publish_article` returns only URL (`telegraph_publisher.py:266`); path derived via `urlparse(tg_url).path.lstrip('/')`. `telegraph_path` is future-facing (editPage) but cheap to store now.

### 9.10 `job()` rewrite

Replaces `news_bot.py:423-449`. Same signature, same placement:

**UPDATED 2026-04-22 per tech-spec Decisions 11 (sanitize_error_message), 12 (batched heads-up ping), 13 (shared attempt_count), 4 (no `'rss'` fallback for source_name).**

```python
def job():
    logger.info("Starting prep-phase cron tick...")
    init_db()  # idempotent CREATE TABLE IF NOT EXISTS (9.11)

    # (1a) Idle heads-up — ONE consolidated ping for all stale rows (Decision 12).
    stale_rows = pending_repo.list_pending_stale(hours=IDLE_TIMEOUT_HOURS)
    if stale_rows:
        titles = ", ".join(row['title'] for row in stale_rows)
        send_admin_notification(
            f"Will auto-publish in ~{GRACE_WINDOW_HOURS}h: {titles}. "
            f"Intercept via hw_review take N"
        )
        for row in stale_rows:
            pending_repo.mark_notified(row['link'])

    # (1b) Overdue auto-publish — shared attempt_count (Decision 13).
    overdue_rows = pending_repo.list_notified_overdue(grace_hours=GRACE_WINDOW_HOURS)
    for row in overdue_rows:
        try:
            _fallback_publish(row, via_review=False)
        except Exception as exc:
            safe = sanitize_error_message(exc)  # Decision 11
            new_ct = pending_repo.increment_attempt(row['link'], safe)
            if new_ct >= 3:
                pending_repo.move_to_failed(row['link'], safe)
            logger.error(f"Fallback publish failed for {row['link']}: {safe}")

    # (2) Fetch all sources.
    all_entries = []
    for fetcher in SOURCES:  # §9.5
        try:
            all_entries.extend(fetcher(notifier=send_admin_notification))
        except Exception as exc:
            safe = sanitize_error_message(exc)
            logger.error(f"Source fetcher {fetcher.__name__} failed: {safe}")
            send_admin_notification(f"⚠️ {fetcher.__name__} raised: {safe}")

    # (3) Filter against processed_news + pending_articles.
    new_entries = filter_new_entries_extended(all_entries)

    # (4) Overflow fast-track.
    accepted, _ = _overflow_fast_track(new_entries)  # §9.8

    # (5) Fetch full article + INSERT into pending (no publish!).
    inserted = 0
    for entry in accepted:
        article = fetch_full_article(entry)
        if not article or not article.get('paragraphs'):
            logger.warning(f"No article data for {entry.get('link')}, skipping")
            continue
        row = {
            'link': entry['link'],
            # Decision 4: source_name MUST be set by fetcher; no 'rss' fallback.
            'source_name': entry['source_name'],
            'feed_url': entry.get('feed_url'),
            'title': article.get('title') or entry.get('title') or '',
            'subtitle': article.get('subtitle') or '',
            'paragraphs': article['paragraphs'],
            'images': article.get('images') or [],
            'blocks': article.get('blocks'),
            'pub_date': entry.get('published', ''),
        }
        if pending_repo.insert_pending(row):
            inserted += 1

    # (6) Admin ping (non-empty queue only, AC L57).
    rows = pending_repo.list_pending()
    ping = build_admin_ping(rows)  # §9.4
    if ping:
        send_admin_notification(ping)

    logger.info(f"Prep-phase done. Inserted {inserted}, queue size {len(rows)}.")
```

`_fallback_publish(row, via_review=False)` helper = `transcreate_text` on EN paragraphs → `publish_article` → `send_telegraph_teaser` → `move_to_published(via_review=False)`. Shares retry-safe Telegraph-URL reuse with `cmd_publish` (9.9) — factor a common internal helper.

`process_new_articles` (`news_bot.py:360-420`) is **deleted**. Its work is split: prep fetches + INSERTs, CLI publish reads staged rows + calls `publish_article` + `send_telegraph_teaser`. `limit=3` is replaced by the queue cap of 10. Tests citing `process_new_articles(..., limit=3)` flip (see §9.12).

### 9.11 Changes to `init_db()`

`news_bot.py:113-121`:

```python
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS processed_news
                 (link TEXT PRIMARY KEY, title TEXT, pub_date TEXT,
                  processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    pending_articles_repo.init_schema(conn)  # creates pending_articles,
                                             # published_articles, failed_articles
    conn.commit()
    conn.close()
    logger.info("Database initialized.")
```

`tests/test_database.py:19-39` pins the literal `processed_news` CREATE — unchanged, test still passes. New CREATEs live in `pending_articles_repo.init_schema(conn)` and get their own assertions in `test_migration.py` (9.12).

### 9.12 Test file plan

New files (follow existing mix: integration = unittest, telegraph_publisher = pytest):

| File | Purpose | Key test signatures |
|---|---|---|
| `tests/test_pending_articles_repo.py` | CRUD unit tests: insert, get, list*, update_staged, mark_notified, clear_notified, move_to_published, move_to_failed, retry_from_failed, list_pending_for_eviction, count_pending, skip_pending. Mocks `sqlite3.connect` or uses tempfile DB. | `def test_insert_pending_serializes_paragraphs_json`, `def test_insert_pending_unique_link_returns_false`, `def test_list_pending_for_eviction_skips_staged`, `def test_move_to_published_is_transactional`. |
| `tests/test_hw_review_cli.py` | argparse parsing, exit codes, stage validation, skip confirmation prompt, retry indexing. Mocks repo module. | `def test_stage_rejects_partial_ru`, `def test_skip_with_staged_prompts_confirmation`, `def test_publish_nonexistent_returns_clean_error`. |
| `tests/test_migration.py` | On empty tempfile DB, `init_db()` → assert all 4 tables exist with expected columns (user-spec L125, test-plan row 9). | `def test_all_tables_created`, `def test_pending_articles_has_expected_columns`. |
| `tests/test_job_prep_phase.py` | End-to-end prep: mock sources → assert `pending_articles` populated, zero calls to `send_telegraph_teaser`/`publish_article` (AC L54). | `def test_prep_phase_does_not_publish`, `def test_admin_ping_format_matches_spec`, `def test_admin_ping_suppressed_on_empty_queue`. |
| `tests/test_hw_review_publish_flow.py` | End-to-end publish via CLI: stage → preview → publish. Mock Telegraph + Telegram. Assert row moves pending → published. | `def test_publish_happy_path`, `def test_publish_retry_reuses_telegraph_url`. |
| `tests/test_idle_fallback.py` | Stale `fetched_at` → heads-up ping; overdue → GT fallback + auto-publish with `via_review=False`; `take N` clears `notified_at`. | `def test_stale_row_gets_heads_up`, `def test_overdue_auto_publishes_via_gt`, `def test_take_clears_notification`. |
| `tests/test_overflow.py` | Queue full + new entries → evict oldest unstaged; staged rows never touched; fast-track failure defers new entries with admin ping. | `def test_overflow_evicts_unstaged`, `def test_overflow_protects_staged`, `def test_overflow_fast_track_failure_defers`. |
| `tests/test_preview_renderer.py` | `render_html` produces valid HTML, handles all allowed tags, escapes Cyrillic safely. | `def test_renders_figure_with_caption`, `def test_escapes_html_special_chars_in_ru_text`. |
| `tests/test_admin_ping.py` | `build_admin_ping` format, zero-source omission, empty-queue None return. | `def test_omits_zero_sources`, `def test_returns_none_for_empty_queue`. |

Existing tests to flip (auto-publish → stage-only in prep):
- `tests/test_integration.py:54,115,146,178,205` — drop `mock_publish.call_count == 3`; assert `pending_repo.count_pending() == 3` + `mock_send_teaser.assert_not_called()`.
- `tests/test_mattel_integration.py:59,104` — flip to "row in pending_articles, no Telegram call". Duplicate test at L113-115 still holds (UNIQUE PK).
- `tests/test_feed_iteration.py:35,108-122` — drop `limit=3` assertion; replace `process_new_articles` with `insert_pending`.
- `tests/test_database.py:19-39` — unchanged. Optionally add `test_init_db_also_creates_pending_tables` cross-check.
- `tests/test_telegraph_publisher.py` — no required changes (only add tests if tech-spec wires `editPage`).

### 9.13 Edge cases

1. **Empty `ru_paragraphs` JSON.** `json.dumps([])` → `'[]'` (non-NULL). "Is staged?" must check `ru_paragraphs IS NOT NULL`, not truthiness. Empty list is a valid stage. If prohibiting empty: enforce in `update_staged` with explicit check + non-zero CLI exit.

2. **Cyrillic in SQLite.** UTF-8 by default — safe, no PRAGMA needed. Already verified by existing `processed_news` usage with Russian-titled fixtures. Serialize JSON with `ensure_ascii=False` so CLI DB inspection (`sqlite3 news.db "SELECT ru_title FROM pending_articles"`) shows readable Cyrillic.

3. **`publish N` on vanished row (AC L63).** `get_pending` returns None → check `get_published(link)` and `get_failed(link)` (new repo helpers), print current state, exit 1. SELECT-before-UPDATE throughout avoids PK-collision stack traces:
   ```python
   row = pending_repo.get_pending(link)
   if not row:
       if pub := pending_repo.get_published(link):
           print(f"{link} already published at {pub['telegraph_url']}", file=sys.stderr)
       elif fail := pending_repo.get_failed(link):
           print(f"{link} in failed: {fail['last_error']}", file=sys.stderr)
       else:
           print(f"{link} not found", file=sys.stderr)
       return 1
   ```

4. **`webbrowser.open` cross-platform.** Stdlib: macOS (`open`), Linux (`xdg-open`/`sensible-browser`), Windows (`start`). WSL/headless Docker: may fail without `BROWSER` env — the dev env here is headless Debian Docker. Mitigation: `preview --no-open` flag (9.3); `preview` always prints the HTML path to stdout; `preview` returns 0 even if `webbrowser.open` returns False (HTML was produced, launch is best-effort). Log launch failures to stderr so tests can assert them.

5. **feedparser `FeedParserDict`.** Not a real dict — `dict(entry)` includes internal attrs. In `_fetch_rss_entries` (§9.5) build the dict explicitly: `{'link': entry.get('link'), 'title': entry.get('title'), 'published': entry.get('published', ''), 'summary': entry.get('summary', ''), 'feed_url': url, 'source_name': 'rss'}`.
