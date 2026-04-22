# Patterns & Conventions

Coding conventions, development workflow, and project-specific practices.
For universal coding standards, see `~/.claude/skills/code-writing/references/universal-patterns.md`.

---

## Project-Specific Code Patterns

### SQLite Duplicate Detection
- The `processed_news` table uses `link` as PRIMARY KEY to guarantee uniqueness.
- Before processing any RSS entry, `is_processed(link)` checks the database; if present, the entry is skipped.
- The table also stores `title` and `pub_date` for reference, but only `link` is essential for deduplication.

### Source-Parser Contract
- Every per-source module exposes a `fetch_*_article(link_or_entry)` function
  that returns `{title, subtitle, paragraphs, images}` — or `None` on failure.
- `subtitle` is the editorial lead from the source site. Empty string when
  the source has none (e.g. autoevolution RSS fallback); `telegraph_publisher`
  then skips the decorated `💬 «…»` lead + `<hr>` on the Telegraph page.
- Autoevolution additionally returns `blocks` — an ordered list preserving
  image/video/heading positions. When present, `telegraph_publisher` uses
  the block renderer; otherwise it falls back to the flat
  `paragraphs`/`images` renderer.
- `news_bot.fetch_full_article` dispatches to the right parser by URL
  domain and wraps each call in a try/except so one bad article doesn't
  stop the pipeline.

### Transcreation, not plain translation
- `transcreate_text` wraps Google Translate with a regex post-processing
  pass: bureaucratic Russian → plain Russian, passive → active, Hot Wheels
  glossary fixes (e.g. `сборка гаража` → `гаражный проект`).
- Titles get a deterministic content-aware emoji prefix (🏆, 🏎️, 🚀, 💎,
  🤝, 📢, 🚗, or 🔥 fallback).
- Body output is truncated at 4000 chars on a sentence boundary.
- On translator failure, the original English text is returned so the
  pipeline keeps going.

### Channel post format (locked 2026-04-21)
- Message body is a single source hashtag (`#autoevolution`, `#mattel`,
  `#lamleygroup`). Derived from the source URL's second-level domain by
  `news_bot._source_hashtag`.
- The Telegra.ph page is surfaced via
  `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` — Telegram
  renders the Instant View card above the hashtag line, carrying the
  domain label, title, excerpt, hero image, and ⚡ INSTANT VIEW button.
- Full spec: `work/telegraph-pipeline/post-format.md`.

### Image/Media Handling
- All images from the source are carried through to the Telegra.ph page
  (hero image first, then interleaved every 3rd paragraph on the flat
  path, or at their original positions on the block path).
- Images are hot-linked — no local download or caching.
- Videos (YouTube/Vimeo) must be wrapped in the `telegra.ph/embed/<provider>?url=…`
  proxy form; raw URLs fail Telegra.ph's iframe validator and break
  Instant View. See `autoevolution_source._video_embed_url`.

### Cloudflare bypass
- `autoevolution_source` uses `curl_cffi` with `impersonate="chrome"` for
  article-page fetches. Plain `requests` returns HTTP 403.
- If `curl_cffi` isn't installed or the scrape fails for any reason, the
  pipeline falls back to `enrich_entry` (RSS-only path) so we still post
  something — truncated is better than silent.

### Scheduling
- The `schedule` library is used to run the main job daily at 12:00 local time.
- The script runs indefinitely (`while True: schedule.run_pending(); time.sleep(60)`) when started interactively.
- For production, a systemd service or cron job is recommended instead of relying on the in‑process scheduler.

### Logging
- Logging is configured at INFO level, with timestamps and module names.
- Critical steps (new entries found, translation, Telegraph publish,
  channel post) are logged at INFO, errors at ERROR.

### Multiple RSS Feeds Configuration
- Feed URLs are read from `feeds.json` (JSON array of up to 5 strings) or fall back to the default RSS URL.
- The `load_feeds()` function validates URLs and ensures the list length does not exceed 5.
- If the configuration file is missing, malformed, or contains invalid URLs, the script falls back to the default RSS URL, logs a warning, and notifies the admin via Telegram.

### Error Isolation
- Each feed is processed independently inside a try‑catch block; failures in one feed do not stop processing of other feeds.
- Source-level failures (Mattel, Lamley, autoevolution scrape) are isolated the same way, with admin notifications on hard failures.
- The global limit (`limit=3`) is applied across all sources to prevent overloading external services.

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

pytest suite lives in `tests/`:

- `test_autoevolution_source.py` — scrape success/failure, RSS fallback, block ordering, video embed wrapping.
- `test_mattel_news_source.py` + `test_mattel_integration.py` — `__NEXT_DATA__` parsing, Hot Wheels filter, notifier contract, DB persistence.
- `test_lamley_source.py` — entry-content parsing, image dedup.
- `test_telegraph_publisher.py` — account lifecycle, node tree for flat and block renderers, subtitle/hr rules, figcaption, iframe wrapping, source footer.
- `test_telegram.py` — `send_telegraph_teaser` hashtag format + `LinkPreviewOptions`.
- `test_feed_iteration.py`, `test_integration.py` — end-to-end pipeline with mocks.
- `test_config_loader.py`, `test_database.py`, `test_translation.py` — unit coverage.

Run with `pytest tests/`. Fixtures (including the real Mattel page HTML) are in `tests/fixtures/`.

### Agent Verification Methods

**Telegram Bot Posting**
- **Method:** Use the Telegram MCP (if available) to read the last message in the target channel (`@myhwchannel123`).
- **Verification:** Confirm the message is the single-line hashtag form and the preview card renders correctly.

**Telegra.ph Page**
- **Method:** Fetch the Telegra.ph URL (printed in logs) and inspect the node tree.
- **Verification:** Hero image present, decorated subtitle lead (if the source has one), body paragraphs in reading order, source footer at the bottom.

**RSS Feed Parsing**
- **Method:** Manually inspect the RSS feed URL to ensure it returns entries.
- **Verification:** Compare entries count with script output.

### User Verification Methods

**Visual Check of Telegram Post**
- **What to check:** Preview card title/excerpt/hero, ⚡ INSTANT VIEW button, translation quality on the Telegra.ph page.
- **How:** Open the Telegram channel and tap the preview card.
- **Why agent can't:** No visual rendering capability.

---

## Business Rules

*No complex business rules – this is a straightforward automation script.*
