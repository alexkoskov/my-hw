# Hot Wheels News Bot

A Python script that automatically collects Hot Wheels news from multiple
sources (autoevolution.com, corporate.mattel.com, lamleygroup.com),
translates and adapts them to Russian, publishes the full body to
Telegra.ph, and posts a hashtag-attributed channel card with an Instant
View preview in Telegram.

## Features

- **Multi-source aggregation**: RSS feeds from `feeds.json` (up to 5) plus
  `corporate.mattel.com` (via `__NEXT_DATA__`). Default RSS is autoevolution
  at `https://www.autoevolution.com/rss/tag-Hot+Wheels.xml`.
- **Per-source parsers**: autoevolution (Cloudflare bypass via `curl_cffi`),
  Mattel (`__NEXT_DATA__`), Lamley (HTML scrape on `.entry-content`).
- **Duplicate detection**: SQLite tracks processed articles by URL.
- **Transcreation**: Google Translate + rule-based post-processing
  (bureaucratic → plain Russian, Hot Wheels glossary, deterministic
  content-aware emoji on titles).
- **Telegra.ph publishing**: Full translated article rendered as a
  Telegra.ph page with hero image, decorated subtitle lead, body
  paragraphs, interleaved images, and a source footer. Autoevolution
  additionally preserves image/video/heading positions via ordered blocks.
- **Channel post**: Single-line `#{source_label}` with
  `LinkPreviewOptions(show_above_text=True)` so Telegram renders the
  Telegra.ph page as an Instant View preview card with the ⚡ button.
- **Admin notifications**: Source failures are delivered to a separate
  admin chat.
- **Scheduling**: Runs daily at 12:00 local time via the `schedule` library.

## Project Structure

```
my-hw/
├── news_bot.py              # Entry point: scheduler, pipeline, Telegram posting
├── telegraph_publisher.py   # Telegra.ph API client + page builder
├── autoevolution_source.py  # RSS + Cloudflare-bypass scrape
├── mattel_news_source.py    # corporate.mattel.com via __NEXT_DATA__
├── lamley_source.py         # lamleygroup.com HTML scrape
├── feeds.json               # List of up to 5 RSS URLs (optional)
├── requirements.txt         # Python dependencies
├── news.db                  # SQLite database (created automatically)
├── .env.example             # Example environment variables
├── tests/                   # pytest suite + fixtures
├── work/                    # Feature development logs (decisions.md per feature)
└── README.md                # This file
```

## Quick Start

1. **Clone the repository** (if applicable) and navigate into the project folder.

2. **Create a virtual environment** and install dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Set up environment variables**:
   - Create a `.env` file (copy from `.env.example`) and fill in your credentials:
     ```
     TELEGRAM_BOT_TOKEN=your_bot_token_here
     TELEGRAM_CHANNEL_ID=@your_channel_username
     TELEGRAM_ADMIN_ID=@your_admin_username
     ```
   - Get a Telegram bot token from [@BotFather](https://t.me/BotFather).
   - Ensure your bot is an administrator of the target channel.
   - `TELEGRAPH_ACCESS_TOKEN` is auto-provisioned on first run via
     `telegraph_publisher.ensure_access_token` and persisted back into
     `.env`.

4. **Test the script**:
   ```bash
   python news_bot.py
   ```
   The script will run once (and schedule itself for daily execution). Press `Ctrl+C` to stop.

5. **Production deployment**:
   - For a server, run the script as a systemd service or use a cron job:
     ```cron
     0 12 * * * cd /path/to/my-hw && /path/to/venv/bin/python news_bot.py
     ```
   - Alternatively, keep the script running with `schedule` (as implemented) inside a screen/tmux session.

## Configuration

Edit `feeds.json` to change the RSS feed list (up to 5 URLs). Other knobs
are constants at the top of `news_bot.py`:

- `RSS_URL` – default RSS feed (fallback when `feeds.json` is missing/invalid).
- `DB_FILE` – SQLite database filename.
- `TRANSLATOR_SERVICE` – translation backend (currently Google).
- The pipeline's `limit` (default 3) lives in `process_new_articles`.

## How It Works

1. **Load config** – `load_feeds()` reads `feeds.json` or falls back to the default RSS.
2. **Telegraph account** – `ensure_access_token()` loads or creates a Telegra.ph token.
3. **Fetch sources** – RSS feeds + Mattel's corporate news page.
4. **Filter duplicates** – Each entry's link is checked against SQLite.
5. **Per-source article fetch** – Domain dispatcher picks the right parser and returns `{title, subtitle, paragraphs, images[, blocks]}`.
6. **Transcreate** – Google Translate + glossary / plain-Russian rewrites.
7. **Publish to Telegra.ph** – Full article with hero, subtitle lead, body, interleaved images, source footer.
8. **Post to Telegram** – Single hashtag line with Instant View preview card above.
9. **Mark as processed** – The entry is stored in the database.

## Dependencies

See `requirements.txt` for exact versions.

- `feedparser` – RSS parsing
- `requests` – HTTP (Telegra.ph API, Mattel, Lamley)
- `curl_cffi` – Chrome-impersonating HTTP for autoevolution (Cloudflare bypass)
- `beautifulsoup4` – HTML parsing
- `deep-translator` – translation (Google Translate)
- `python-telegram-bot` – Telegram Bot API wrapper
- `schedule` – in‑process job scheduling

## Troubleshooting

- **No new articles found** – Check `feeds.json` and the RSS URLs; the site might have changed its structure.
- **Autoevolution returns HTTP 403** – Cloudflare tightened its fingerprinting; `curl_cffi` may need an updated impersonation profile.
- **Mattel returns no entries** – The `__NEXT_DATA__` path may have changed; inspect the page source.
- **Translation errors** – Google Translate may block frequent requests; consider switching to a paid API or LibreTranslate.
- **Telegram posting fails** – Verify the bot token and channel ID, and ensure the bot has permission to post in the channel.
- **Telegra.ph publish fails** – Check `TELEGRAPH_ACCESS_TOKEN` in `.env`; delete the line to force re-provisioning on next run.

## Future Improvements

- Cross-article linking on Telegra.ph (Phase 2 of the block pipeline).
- LLM-powered transcreation for higher-quality Russian.
- Image caching/download to avoid hotlinking.
- Web dashboard for monitoring and manual posting.
- Dockerize the application for easier deployment.

## License

This project is provided as-is for educational and personal use.

## Development Logs

Active and completed features have per-folder logs in `work/`:

- `work/telegraph-pipeline/` — locked post format and Telegraph decisions.
- `work/mattel-news-source/` — Mattel source rollout.
- `work/completed/` — shipped features (multiple-rss-feeds).
- `work/archived/` — retired experiments (facebook-hotwheels-source).
