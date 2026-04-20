# Code Research: Facebook Hot Wheels source integration

## Current Codebase Analysis

### File: `news_bot.py`
- **Main script** orchestrates RSS fetching, article processing, translation, summarization, and Telegram posting.
- **Architecture**: Monolithic script with functions for each step; supports multiple RSS feeds via `feeds.json`.
- **Database**: SQLite `news.db` with `processed_news` table (link primary key).
- **Configuration**: Environment variables for Telegram credentials; RSS feed URLs loaded from `feeds.json` (list of strings).
- **External Dependencies**:
  - `feedparser` – RSS parsing
  - `requests` – HTTP fetching
  - `beautifulsoup4` – HTML parsing
  - `deep_translator` – translation via Google Translate
  - `schedule` – in‑process scheduling
  - `python‑telegram‑bot` – Telegram API client

### File: `feeds.json`
- Simple JSON array of RSS feed URLs (strings). Currently contains two Hot Wheels‑related feeds from autoevolution.com.
- Loaded by `load_feeds()` which validates URLs and falls back to a hardcoded default.

### File: `requirements.txt`
Lists the six dependencies above; no Facebook‑specific libraries.

### Pipeline Overview
1. `job()` calls `load_feeds()` to obtain list of RSS URLs.
2. For each URL, `fetch_rss(url)` retrieves entries (list of dicts) using `feedparser`.
3. Each entry is augmented with `feed_url` and aggregated into a single list.
4. `filter_new_entries()` removes entries whose `link` already exists in the database.
5. `process_new_articles()` processes up to `limit` new entries:
   - Calls `get_article_data()` which attempts `fetch_article()` (full‑article scraping) or falls back to RSS summary.
   - Summarizes text with `summarize_text_with_limit()`.
   - Translates title and summary with `transcreate_text()` (Google Translate + stylistic adaptations).
   - Posts to Telegram via `send_to_telegram()`.
   - Marks the entry as processed in the database.

### Error Handling
- `fetch_rss` catches exceptions and returns an empty list.
- `load_feeds` sends admin notifications on configuration errors.
- The loop over feeds in `job()` does not isolate errors per feed, but `fetch_rss` already catches exceptions.

## Key Functions

1. `load_feeds()` – reads `feeds.json`, validates URLs, returns list of strings.
2. `fetch_rss(url)` – fetches and parses RSS feed; returns list of entry dicts.
3. `filter_new_entries(entries)` – filters out duplicates using the database.
4. `fetch_article(url)` – scrapes article title, text, images from a web page.
5. `transcreate_text(text, is_title)` – translates and stylizes text.
6. `summarize_text_with_limit(text, char_limit)` – extractive summarization.
7. `send_to_telegram(title, summary, images, link)` – posts to Telegram channel.
8. `process_new_articles(entries, limit)` – core processing pipeline.
9. `job()` – daily job that orchestrates the whole workflow.

## Limitations for Facebook Integration

- **Source‑type inflexibility**: The pipeline expects RSS feeds; there is no abstraction for other source types (Facebook, Graph API, HTML scraping of social media pages).
- **Configuration format**: `feeds.json` only accepts URLs as strings; no fields for source type, authentication tokens, or filtering rules.
- **Entry format assumption**: `fetch_rss` returns entries with fields `link`, `title`, `published`, `summary`, `description`. Facebook posts may have different field names (e.g., `message`, `created_time`, `permalink_url`).
- **Article scraping**: `fetch_article` is tailored to news‑article HTML (h1, article, div.article‑content). Facebook page HTML is highly dynamic and requires different selectors.
- **Keyword filtering**: No built‑in filtering of posts based on keywords; needed to exclude event/announcement posts.
- **Rate‑limiting and token management**: No infrastructure for respecting API rate limits (Graph API limit 200 requests/hour) or handling authentication tokens.
- **Error isolation per source**: The current error handling in `job()` is per‑feed but assumes RSS failures are silent; Facebook failures could be more complex (token expiry, HTML structure changes) and should not block other sources.
- **No fallback strategy**: The pipeline does not support trying multiple methods (RSS → Graph API → HTML) for a single source.

## Opportunities for Extension

- **Extend configuration format** to support objects with `type`, `url`, `params`, `enabled`, `filter_keywords`, etc., while maintaining backward compatibility with string‑only lists.
- **Create a source‑type dispatcher** – a new function `fetch_source(source_config)` that calls the appropriate fetcher (RSS, Facebook Graph API, Facebook HTML) and returns entries in a uniform format.
- **Reuse existing processing pipeline** – once entries are produced in the same shape as RSS entries, the rest of the pipeline (filtering, translation, summarization, posting) can be reused unchanged.
- **Leverage existing HTML parsing** – `fetch_article` can be adapted or a new `parse_facebook_html` function can be built using BeautifulSoup.
- **Add keyword filtering** as a step in `process_new_articles` or inside the Facebook‑specific fetcher.
- **Implement token‑based authentication** via environment variables (e.g., `FACEBOOK_ACCESS_TOKEN`) and rate‑limiting with `time.sleep()`.
- **Isolate Facebook‑source errors** with try‑catch blocks that log and continue, preventing a single source from breaking the entire job.

## Database Schema

Table `processed_news` already suitable for Facebook posts:

```sql
CREATE TABLE IF NOT EXISTS processed_news
    (link TEXT PRIMARY KEY,
     title TEXT,
     pub_date TEXT,
     processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
```

- `link` can store the Facebook post permalink.
- `title` can store the post message (first N characters) or a generated title.
- `pub_date` can store the post creation time.

No schema changes required.

## Configuration Format Proposal

Extend `feeds.json` to allow either strings (legacy RSS) or objects with explicit fields. Example:

```json
[
  "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml",
  {
    "type": "facebook",
    "id": "hotwheels",
    "url": "https://www.facebook.com/hotwheels/",
    "methods": ["rss", "graph", "html"],
    "filter_keywords": ["event", "announcement", "coming soon", "glow-n-fire"],
    "enabled": true,
    "limit_per_run": 2
  }
]
```

Alternatively, introduce a separate configuration file `sources.json` that lists all sources with their types, leaving `feeds.json` for RSS only. The latter is simpler and avoids breaking existing tests.

Proposal: create `facebook_source.json` with Facebook‑specific settings and have `job()` read it if present. This keeps the change minimally invasive.

## Required Changes

1. **New module `facebook_source.py`** containing:
   - `fetch_facebook_rss(page_url)` – attempt to fetch RSS via Facebook's RSS endpoint.
   - `fetch_facebook_graph(page_id, access_token)` – use Graph API to retrieve posts.
   - `fetch_facebook_html(page_url)` – fallback HTML scraping.
   - `fetch_facebook_posts(config)` – orchestrator that tries methods in order and returns uniform entries.
   - `filter_posts_by_keywords(posts, keywords)` – exclude posts containing any keyword.

2. **Configuration loading** – add `load_facebook_config()` that reads `facebook_source.json` (or a section in `feeds.json`).

3. **Integration into `job()`** – after loading RSS feeds, load Facebook config; if enabled, call `fetch_facebook_posts`, extend the entries list (adding `feed_url` as the Facebook page URL).

4. **Error isolation** – wrap Facebook fetching in try‑catch, log errors, and continue.

5. **Rate‑limiting** – add `time.sleep()` between Graph API requests if needed.

6. **Environment variables** – add `FACEBOOK_ACCESS_TOKEN` for Graph API (optional).

7. **Update `requirements.txt`** – add `facebook‑sdk` (optional) or rely on `requests` only.

8. **Tests** – create unit tests for the new module and update existing tests to accommodate config changes.

## Integration Points

- **`job()`** (lines 460–482) – add a block after loading RSS feeds that fetches Facebook posts and merges entries.
- **`load_feeds()`** – could be extended to read object configs, but for simplicity we can keep it unchanged and add a separate loader.
- **`filter_new_entries()`** – works on any entries with a `link` field, no change needed.
- **`process_new_articles()`** – expects entries with `link`, `title`, `published`; Facebook fetcher must provide these fields.
- **`fetch_article()`** – could be used for scraping individual Facebook posts if we need full text (but Facebook posts are usually short). Not required for basic integration.
- **Admin notifications** – use existing `send_admin_notification()` for Facebook‑specific errors.

## Potential Risks

1. **Facebook API changes** – RSS endpoint may be deprecated; Graph API version may sunset. **Mitigation**: implement HTML parsing as fallback and monitor errors.

2. **Rate limiting** – Graph API limits 200 requests/hour; fetching posts every 24 hours is well within limits, but repeated retries could exceed. **Mitigation**: implement request counting and sleep.

3. **HTML parsing fragility** – Facebook’s page structure changes frequently, breaking scrapers. **Mitigation**: use Graph API as primary, RSS as secondary, HTML as last resort; send admin alerts when parsing fails.

4. **Token security** – storing Facebook access token in environment variables is safe, but token may expire. **Mitigation**: document token renewal and optionally implement token‑refresh logic.

5. **Keyword filtering false positives** – may filter out legitimate news. **Mitigation**: use configurable regex patterns and allow manual override.

6. **Increased runtime** – adding another source extends job duration. **Mitigation**: run Facebook fetching concurrently with RSS (threading) or keep sequential with timeouts.

7. **Backward compatibility** – changes to `feeds.json` format could break existing deployments. **Mitigation**: support both string and object formats, maintain existing tests.

## Recommendations

- **Implement Facebook source as a separate module** to keep concerns isolated.
- **Use Graph API as primary method** because it is official, stable, and provides structured data. RSS as secondary, HTML as fallback.
- **Add keyword filtering at the source level** (inside Facebook fetcher) to avoid processing unwanted posts.
- **Respect rate limits** by adding a delay between Graph API requests (if fetching multiple posts).
- **Keep configuration simple** – start with a single `facebook_source.json` file containing page ID, token (optional), and filter keywords.
- **Maintain backward compatibility** – do not change `load_feeds` behavior unless necessary; introduce a new config file.
- **Enhance error logging** – log Facebook‑specific errors with enough detail for debugging, and notify admin only on persistent failures.
- **Write integration tests** with mocked Facebook responses to verify the pipeline works before production.

## Test Strategy

- **Unit tests** for each new function in `facebook_source.py` (mocked HTTP responses).
- **Integration test** that runs `fetch_facebook_posts` with a mock Graph API endpoint and verifies entries are correctly formatted.
- **Test keyword filtering** with sample posts containing/not containing keywords.
- **Test fallback chain** – simulate RSS failure, ensure Graph API is tried, then HTML.
- **Update existing tests** – ensure `job()` still works when Facebook config is absent.
- **End‑to‑end test** (optional) with a real Facebook page (e.g., a test page) in a controlled environment.

## Next Steps

Proceed with technical specification that details:

1. Exact schema of `facebook_source.json`.
2. Implementation of `facebook_source.py` with all three fetching methods.
3. Modifications to `job()` to integrate the new source.
4. Environment variable setup (`FACEBOOK_ACCESS_TOKEN`).
5. Updates to `requirements.txt` (add `facebook‑sdk` if needed).
6. Test plan and new test files.
7. Deployment instructions (how to add Facebook source to existing bot).