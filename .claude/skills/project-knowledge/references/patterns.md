# Patterns & Conventions

Coding conventions, development workflow, and project-specific practices.
For universal coding standards, see `~/.claude/skills/code-writing/references/universal-patterns.md`.

---

## Project-Specific Code Patterns

### SQLite Duplicate Detection
- The `processed_news` table uses `link` as PRIMARY KEY to guarantee uniqueness.
- Before processing any RSS entry, `is_processed(link)` checks the database; if present, the entry is skipped.
- The table also stores `title` and `pub_date` for reference, but only `link` is essential for deduplication.

### Translation Fallback
- If Google Translate fails (e.g., network error, quota limit), the original English text is used as a fallback.
- The translation function `transcreate_text` catches exceptions and logs the error, returning the input text unchanged.

### Image Handling
- Up to three images are extracted from the article HTML, but only the first image is sent to Telegram (due to Telegram's single‑photo‑with‑caption limitation).
- Images are hot‑linked (original URLs) – no local download or caching is performed.

### Scheduling
- The `schedule` library is used to run the main job daily at 12:00 local time.
- The script runs indefinitely (`while True: schedule.run_pending(); time.sleep(60)`) when started interactively.
- For production, a systemd service or cron job is recommended instead of relying on the in‑process scheduler.

### Logging
- Logging is configured at INFO level, with timestamps and module names.
- Critical steps (new entries found, translation, posting) are logged at INFO, errors at ERROR.

### Multiple RSS Feeds Configuration
- Feed URLs are read from `feeds.json` (JSON array of up to 5 strings) or fall back to the default RSS URL.
- The `load_feeds()` function validates URLs and ensures the list length does not exceed 5.
- If the configuration file is missing, malformed, or contains invalid URLs, the script falls back to the default RSS URL and logs a warning.

### Error Isolation
- Each feed is processed independently inside a try‑catch block; failures in one feed do not stop processing of other feeds.
- Feed‑specific errors (network timeouts, invalid XML, etc.) are logged with the feed URL for debugging.
- The global limit (`limit=3`) is applied across all feeds to prevent overloading external services.

---

## Git Workflow

### Branch Structure

- **`main`** – Production‑ready code (protected). Only merge from `dev` after verification. Triggers production deployment (if configured).
- **`dev`** – Active development. All feature branches are merged here. Triggers staging deployment (if configured).

### Testing Requirements

- **On commit:** No automated tests are currently set up. Manual verification is required.
- **On merge to dev:** Run the script manually to ensure RSS fetching, translation, and Telegram posting still work.
- **On merge to main:** Same as dev; additionally verify that environment variables are correctly set for production.

### Security & Quality Gates

- **Pre‑commit:** No automated secret scanning is configured; developers must ensure no secrets are committed.
- **Pre‑push:** No automated code review; changes should be manually reviewed.

---

## Testing & Verification

### Test Infrastructure

No test suite is currently implemented. Verification is performed manually by:

1. Setting up a test Telegram bot and channel with appropriate environment variables.
2. Running the script locally (`python news_bot.py`) and observing logs.
3. Checking that a test article is fetched, translated, summarized, and posted correctly.

### Agent Verification Methods

**Telegram Bot Posting**
- **Method:** Use the Telegram MCP (if available) to read the last message in the target channel.
- **Setup:** Bot must be running, test channel configured.
- **Verification:** Confirm that the posted message contains the expected title and summary.

**RSS Feed Parsing**
- **Method:** Manually inspect the RSS feed URL to ensure it returns entries.
- **Verification:** Compare entries count with script output.

### User Verification Methods

**Visual Check of Telegram Post**
- **What to check:** Message formatting, image presence, translation quality.
- **How:** Open the Telegram channel and inspect the latest post.
- **Why agent can't:** No visual rendering capability.

---

## Business Rules

*No complex business rules – this is a straightforward automation script.*
