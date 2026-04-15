# Code Research: multiple RSS feeds support

## Current Codebase Analysis

### File: `news_bot.py`
- **Main script** that orchestrates RSS fetching, article processing, translation, summarization, and Telegram posting.
- **Architecture**: Monolithic script with functions for each step.
- **Database**: SQLite `news.db` with `processed_news` table (link primary key).
- **Configuration**: Hardcoded RSS_URL (`"https://www.autoevolution.com/rss/tag-Hot+Wheels.xml"`), DB_FILE, LOG_LEVEL.
- **External Dependencies**:
  - `feedparser` – RSS parsing
  - `requests` – HTTP fetching
  - `beautifulsoup4` – HTML parsing
  - `deep_translator` – translation via Google Translate
  - `schedule` – in‑process scheduling
  - `python‑telegram‑bot` – Telegram API client

### Key Functions
1. `fetch_rss(url)` – fetches single RSS feed, returns entries.
2. `filter_new_entries(entries)` – filters entries already present in DB.
3. `fetch_article(url)` – scrapes article title, text, images.
4. `translate_text(text)` – translates English → Russian with fallback.
5. `summarize_text(text)` – extractive summarization (first sentences).
6. `send_to_telegram(title, summary, images, link)` – posts to Telegram channel.
7. `process_new_articles(entries, limit=3)` – processes up to `limit` new articles.
8. `job()` – daily job that runs the pipeline.
9. `main()` – entry point with scheduler.

### Current Limitations for Multi‑Feed Support
- **Single RSS URL** hardcoded as `RSS_URL`.
- **No configuration management** – feeds cannot be added without editing code.
- **Processing limit** (`limit=3`) applies per job, not per feed.
- **Error handling** per feed is not isolated – an exception in one feed could stop the whole job (though `fetch_rss` catches exceptions, it returns empty list).
- **Database deduplication** works globally (by link) which is fine for multiple feeds.
- **No feed‑specific settings** (e.g., custom limits, enabled/disabled).

### Opportunities for Extension
- Replace `RSS_URL` with a list of URLs from a config file (`feeds.json`) or environment variable.
- Iterate over each feed in `job()` and call `fetch_rss` per feed.
- Aggregate entries across feeds, then deduplicate and process.
- Add per‑feed error handling (try‑catch around each feed) to prevent cascading failures.
- Consider parallel fetching (async) for performance (optional).
- Add feed‑specific metadata (name, weight, etc.) for logging.

### Database Schema
Table `processed_news` already suitable for multiple feeds because `link` is globally unique. No changes needed.

### Configuration File Format Proposal
```json
{
  "feeds": [
    {
      "url": "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml",
      "enabled": true,
      "limit_per_run": 2
    },
    {
      "url": "https://example.com/another-rss.xml",
      "enabled": true,
      "limit_per_run": 1
    }
  ],
  "global_limit": 5
}
```

Alternatively, a simple list of URLs in a JSON array or a newline‑separated text file.

### Required Changes
1. **Configuration loading** – read feeds from a JSON file or env var.
2. **Modify `fetch_rss` calls** – loop over feeds, aggregate entries.
3. **Error isolation** – wrap each feed fetch in try‑catch, log errors, continue.
4. **Adjust processing limit** – either per feed or global.
5. **Update logging** – include feed source in log messages.
6. **Testing** – ensure existing single‑feed behavior remains unchanged.

### Integration Points
- No changes to translation, summarization, or Telegram posting required.
- Database layer remains unchanged.
- Scheduler remains unchanged.

### Potential Risks
- **Increased runtime** – fetching multiple feeds sequentially may exceed timeouts. Consider timeouts per feed.
- **Rate limiting** – hitting external servers too frequently; need to add delays.
- **Memory** – storing all entries from all feeds before processing may increase memory usage (negligible for <5 feeds).
- **Configuration errors** – malformed URLs causing runtime errors; need validation.

### Recommendations
- Implement configuration via `feeds.json` placed in project root.
- Keep backward compatibility: if config missing, fallback to hardcoded RSS_URL.
- Add a `FEEDS_CONFIG` environment variable pointing to config file.
- Validate URLs at startup.
- Add per‑feed timeout and retry logic.
- Update documentation with example configuration.

### Test Strategy
- Unit tests for new configuration loader.
- Integration test with mock RSS feeds (using `feedparser` mock).
- End‑to‑end test with a small number of real feeds (optional).
- Ensure duplicate detection works across feeds.

### Next Steps
Proceed with technical specification detailing the above changes, tasks, and validation steps.