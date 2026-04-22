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
├── news_bot.py              # Entry point: scheduler, pipeline, Telegram posting
├── telegraph_publisher.py   # Telegra.ph API client + page builder
├── autoevolution_source.py  # RSS + Cloudflare-bypass scrape (curl_cffi)
├── mattel_news_source.py    # corporate.mattel.com via __NEXT_DATA__
├── lamley_source.py         # lamleygroup.com HTML scrape
├── feeds.json               # List of up to 5 RSS URLs (optional)
├── requirements.txt         # Python dependencies
├── news.db                  # SQLite database (created automatically)
├── .env.example             # Example environment variables
├── README.md                # Project documentation
├── CLAUDE.md                # AI agent context
├── tests/                   # pytest suite + fixtures
├── work/                    # Development tracking (feature folders)
│   ├── mattel-news-source/
│   ├── telegraph-pipeline/
│   ├── completed/
│   └── archived/
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
- `requests` – Generic HTTP for Telegra.ph API, Mattel, Lamley.
- `curl_cffi` – Chrome-impersonating HTTP client for autoevolution
  (bypasses Cloudflare's `HTTP 403` on plain `requests`).
- `beautifulsoup4` – Extracts title, body, images, and inline links from HTML.
- `deep-translator` – Translates English to Russian via Google Translate
  (no auth). Wrapped by `transcreate_text` in `news_bot.py`, which also
  rewrites bureaucratic phrasing, applies a Hot Wheels glossary, and
  prepends a deterministic emoji to titles.
- `python-telegram-bot` – Posts the channel card with
  `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` to trigger
  the Instant View preview, and delivers admin failure notifications.
- `schedule` – In‑process job scheduling for daily runs.

---

## External Integrations

**Google Translate (via deep-translator)**
- **Purpose:** Translate article titles, subtitles, and body paragraphs
  from English to Russian.
- **Auth method:** No authentication required (public Google Translate API).

**Telegra.ph API (`api.telegra.ph`)**
- **Purpose:** Publish the full translated article as a Telegra.ph page so
  Telegram can render it as an Instant View preview card with the ⚡ button.
- **Auth method:** Anonymous account access token, created on first run
  via `createAccount` and persisted to `.env` as `TELEGRAPH_ACCESS_TOKEN`
  by `telegraph_publisher.ensure_access_token`.

**Telegram Bot API (`python-telegram-bot`)**
- **Purpose:** Post the channel card (hashtag + Instant View preview) and
  deliver admin failure notifications.
- **Auth method:** Bot token (`TELEGRAM_BOT_TOKEN`) and channel ID
  (`TELEGRAM_CHANNEL_ID`); admin chat id in `TELEGRAM_ADMIN_ID` (defaults
  to `@sunny413x`). All stored as environment variables.

**corporate.mattel.com**
- **Purpose:** Source for Mattel PR / announcement articles.
- **Auth method:** None. Parsed via the `__NEXT_DATA__` JSON embedded in
  the HTML.

**autoevolution.com (behind Cloudflare)**
- **Purpose:** Primary RSS source + full article scrape.
- **Auth method:** None. Scraping requires `curl_cffi` with Chrome TLS
  impersonation; plain `requests` returns HTTP 403.

**lamleygroup.com**
- **Purpose:** Enthusiast blog source for Hot Wheels releases.
- **Auth method:** None. Plain `requests` + BeautifulSoup on
  `.entry-content`.

---

## Data Flow

1. **Configuration load** – `news_bot.load_feeds()` reads `feeds.json`
   (list of up to 5 RSS feed URLs) or falls back to the default
   autoevolution Hot Wheels RSS URL. Invalid config triggers an admin
   notification and the fallback.
2. **Telegraph account** – `telegraph_publisher.ensure_access_token()`
   loads `TELEGRAPH_ACCESS_TOKEN` from env, or calls `createAccount` and
   persists the result to `.env`.
3. **RSS fetch** – For each feed URL, `fetch_rss` downloads and parses
   the feed. Errors in individual feeds are isolated and logged.
4. **Mattel fetch** – `fetch_mattel_news` pulls `corporate.mattel.com/news`
   and filters `__NEXT_DATA__` entries by Hot Wheels mention. Failures
   notify the admin but don't stop other sources.
5. **Duplicate filter** – Each entry's `link` is checked against the
   SQLite `processed_news` table (global deduplication across sources).
6. **Article fetch (per source)** – `news_bot.fetch_full_article` dispatches
   by domain: Mattel → `fetch_mattel_article`, Lamley →
   `fetch_lamley_article`, autoevolution →
   `fetch_autoevolution_article` (Cloudflare-bypass scrape with RSS-only
   fallback). Each returns `{title, subtitle, paragraphs, images}` plus
   an optional ordered `blocks` list when the source preserves media
   positions.
7. **Transcreation** – Title (with content-aware emoji prefix), subtitle,
   and every body paragraph are translated + adapted by
   `transcreate_text` (Google Translate + plain-Russian rewrites + HW
   glossary). Body is truncated at 4000 chars on a sentence boundary.
8. **Telegraph publish** – `telegraph_publisher.publish_article` renders
   hero figure + decorated subtitle lead + `<hr>` + body paragraphs with
   interleaved images + source footer. When `blocks` is provided, the
   block renderer preserves image/video/heading positions and emits
   `figcaption` for captioned images.
9. **Telegram channel post** – `send_telegraph_teaser` sends one line
   `#{source_label}` with `LinkPreviewOptions(url=telegraph_url,
   show_above_text=True)`. Telegram renders the Telegra.ph page as an
   Instant View preview card.
10. **Storage** – The entry is recorded in SQLite to avoid reprocessing.

The pipeline runs once per day (12:00 local time, configurable) via the
`schedule` library. A global limit (default 3) restricts the total
number of articles processed per run across all sources.

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

**Secrets:** The Telegram bot token (`TELEGRAM_BOT_TOKEN`), channel ID
(`TELEGRAM_CHANNEL_ID`), admin chat ID (`TELEGRAM_ADMIN_ID`), and the
Telegra.ph access token (`TELEGRAPH_ACCESS_TOKEN`, auto-provisioned on
first run) are stored in `.env` — never committed. The `.env` file is
git-ignored. None of these are persisted to the database.

---

## Planned Enhancements

**Cross-article linking (Phase 2 of the block pipeline)**
- Autoevolution parser already records external `<a href>` anchors in
  `block["runs"]` as metadata. A future pass will map those hrefs to our
  own Telegra.ph URLs when we've already published the linked target, and
  emit them as `<a>` nodes on the page (currently stripped — see
  `telegraph_publisher._build_content_from_blocks` comment from commit
  a984505).

**LLM-powered transcreation**
- Replace the regex-based post-processing pass with an LLM that produces
  higher-quality Russian (preserves Hot Wheels jargon, tone, brand names).

**Health Monitoring & Error Reporting**
- Beyond the current per-source admin notifications, add uptime checks
  and a failure digest.

**Web Dashboard (Future)**
- Flask/FastAPI dashboard to view processed articles, monitor status, and
  manage feeds — read-only view of logs and statistics.
