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
