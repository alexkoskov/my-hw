# Architecture

## Purpose
Technical architecture overview for AI agents. Helps agents understand HOW the system is built.

---

## Tech Stack

**Frontend:** None (CLI script with no user interface)

**Backend:** Standalone Python script (no web framework)

**Database:** SQLite (local file `news.db`)
- **Why:** Lightweight, file‑based, zero‑configuration; suits the simple duplicate‑tracking requirement.

**Runtime:** Python 3.8+
- **Why:** Wide library support, ease of scripting, and compatibility with required packages.

---

## Project Structure

```
my-hw/
├── news_bot.py              # Main script
├── requirements.txt         # Python dependencies
├── news.db                  # SQLite database (created automatically)
├── .env.example             # Example environment variables
├── README.md                # Project documentation
├── CLAUDE.md                # AI agent context
├── work/                    # Development tracking
│   └── checkpoint.md
└── .claude/                 # AI skill definitions and project knowledge
    └── skills/project-knowledge/references/
        ├── project.md
        ├── architecture.md
        ├── patterns.md
        ├── deployment.md
        └── ux-guidelines.md
```

---

## Key Dependencies

**Critical packages:**

- `feedparser` – Parses RSS feeds to extract article entries.
- `requests` – Fetches HTML content of articles.
- `beautifulsoup4` – Extracts title, text, and images from HTML.
- `deep-translator` – Translates English text to Russian using Google Translate.
- `python-telegram-bot` – Sends messages and images to Telegram channels via Bot API.
- `schedule` – In‑process job scheduling for daily runs.

---

## External Integrations

**Google Translate (via deep-translator)**
- **Purpose:** Translate article titles and text from English to Russian.
- **Auth method:** No authentication required (public Google Translate API).

**Telegram Bot API**
- **Purpose:** Post formatted news summaries with images to a Telegram channel.
- **Auth method:** Bot token (`TELEGRAM_BOT_TOKEN`) and channel ID (`TELEGRAM_CHANNEL_ID`) stored as environment variables.

---

## Data Flow

1. **RSS fetch** – The script downloads the Hot Wheels RSS feed and extracts new entries.
2. **Duplicate filter** – Each entry’s link is checked against the SQLite `processed_news` table.
3. **Article scraping** – For each new entry, the script downloads the article page and extracts title, text, and up to three images.
4. **Translation** – Title and text are translated from English to Russian using Google Translate.
5. **Summarization** – The translated text is shortened to 3–5 sentences (simple extractive method).
6. **Telegram posting** – A formatted message (with images) is sent to the configured Telegram channel.
7. **Storage** – The entry is recorded in the SQLite table to avoid future reprocessing.

The entire pipeline runs once per day (configurable) via the `schedule` library.

---

## Data Model

**Database:** SQLite 3 (single file `news.db`)

### Main Tables/Collections

**processed_news**
- Purpose: Stores links of already processed news articles to prevent duplicates.
- Key fields:
  - `link` (TEXT, PRIMARY KEY) – Unique article URL.
  - `title` (TEXT) – Original article title (for reference).
  - `pub_date` (TEXT) – Publication date from RSS feed.
  - `processed_at` (TIMESTAMP) – When the article was processed (default CURRENT_TIMESTAMP).
- Relationships: None (standalone table).

### Key Constraints

- **Unique constraints:** `link` is PRIMARY KEY (enforced by SQLite).
- **Foreign keys:** None.
- **Required fields:** `link` is NOT NULL.

### Migration Strategy

**Tool:** Not applicable – the database is created automatically on first run via `init_db()`.

**Process:** The script calls `init_db()` at startup, which creates the table if it does not exist. No manual migration steps are needed.
### Sensitive Data

**PII fields:** No PII is stored in the database.

**Secrets:** The Telegram bot token and channel ID are stored as environment variables (never committed). They are required for the bot to operate but are not persisted in the database.

---

## Planned Enhancements

**Multiple RSS Feeds**
- Support for multiple RSS feed URLs via a configuration file (`feeds.json` or environment variable)
- Each feed can be individually enabled/disabled
- Deduplication across all feeds using the same SQLite database

**Improved Summarization**
- Replace simple extractive summarization with more advanced methods (e.g., `sumy` library for extractive summarization)
- Future option to integrate LLM-based summarization (OpenAI API, local Ollama) for higher quality

**Health Monitoring & Error Reporting**
- Add logging of failures with retry mechanisms
- Optional notification of errors via Telegram (separate admin channel)
- Basic uptime monitoring via scheduled self‑checks

**Configuration Management**
- Move hard‑coded settings (RSS URL, schedule time, limit) to a config file or environment variables
- Support for runtime configuration updates without code changes

**Web Dashboard (Future)**
- Simple Flask/FastAPI dashboard to view processed articles, monitor status, and manage feeds
- Authentication via basic auth or token
- Read‑only view of logs and statistics
