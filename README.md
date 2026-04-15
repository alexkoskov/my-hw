# Hot Wheels News Bot

A Python script that automatically collects Hot Wheels news from autoevolution.com, translates them to Russian, summarizes, and posts to a Telegram channel.

## Features

- **RSS monitoring**: Fetches the latest articles from `https://www.autoevolution.com/rss/tag-Hot+Wheels.xml`.
- **Duplicate detection**: Uses SQLite to track already processed news.
- **Article scraping**: Extracts title, full text, and images from each article.
- **Translation**: Translates title and text from English to Russian using Google Translate.
- **Summarization**: Creates a short summary (3–5 sentences) of the translated text.
- **Telegram posting**: Sends formatted posts with images to a Telegram channel via Bot API.
- **Scheduling**: Runs daily at 12:00 local time (configurable).

## Project Structure

```
my-hw/
├── news_bot.py          # Main script
├── requirements.txt     # Python dependencies
├── news.db              # SQLite database (created automatically)
├── .env.example         # Example environment variables
└── README.md            # This file
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
     ```
   - Get a Telegram bot token from [@BotFather](https://t.me/BotFather).
   - Ensure your bot is an administrator of the target channel.

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

You can adjust the following constants at the top of `news_bot.py`:

- `RSS_URL` – RSS feed URL.
- `DB_FILE` – SQLite database filename.
- `TRANSLATOR_SERVICE` – translation backend (currently Google).
- `LIMIT` – number of new articles to process per run (default 3).

## How It Works

1. **Fetch RSS** – The script downloads the RSS feed and extracts new entries.
2. **Filter duplicates** – Each entry’s link is checked against the SQLite database.
3. **Scrape article** – For each new entry, the script downloads the article page and extracts title, text, and images.
4. **Translate** – Title and text are translated into Russian.
5. **Summarize** – The translated text is shortened to 3–5 sentences.
6. **Post to Telegram** – A formatted message (with images) is sent to the configured channel.
7. **Mark as processed** – The entry is stored in the database to avoid re‑posting.

## Dependencies

See `requirements.txt` for exact versions.

- `feedparser` – RSS parsing
- `requests` – HTTP requests
- `beautifulsoup4` – HTML parsing
- `deep-translator` – translation (Google Translate)
- `python-telegram-bot` – Telegram Bot API wrapper
- `schedule` – in‑process job scheduling

## Troubleshooting

- **No new articles found** – Check the RSS feed URL; the site might have changed its structure.
- **Article scraping fails** – The HTML selectors in `fetch_article()` may need updating.
- **Translation errors** – Google Translate may block frequent requests; consider using a paid API or a different translator.
- **Telegram posting fails** – Verify the bot token and channel ID, and ensure the bot has permission to post in the channel.

## Future Improvements

- Add support for multiple RSS feeds.
- Implement more sophisticated summarization (e.g., using NLP libraries).
- Add image caching/download to avoid hotlinking.
- Create a web dashboard for monitoring and manual posting.
- Dockerize the application for easier deployment.

## License

This project is provided as-is for educational and personal use.

## Checkpoint

If you are resuming work after a pause, refer to `work/checkpoint.md` for the latest status and next steps.
