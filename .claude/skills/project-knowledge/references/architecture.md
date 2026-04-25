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
├── news_bot.py              # Cron entry point: job() prep-phase (fetch + stage + admin ping),
│                              _fallback_publish for idle/overflow, SOURCES registry,
│                              build_admin_ping, sanitize_error_message, transcreate_text
├── pending_articles_repo.py # DAO owning all 4 SQLite tables (DDL + CRUD + transactional moves)
├── preview_renderer.py      # Local HTML preview builder (CSP + tag/URL allowlists)
├── hw_review.py             # Operator-facing CLI (list/show/stage/skip/preview/publish/take/retry).
│                              Runs in Claude Code session, NOT on production cron server.
├── telegraph_publisher.py   # Telegra.ph API client + public preview_nodes() node-tree builder
├── autoevolution_source.py  # RSS + Cloudflare-bypass scrape (curl_cffi)
├── mattel_news_source.py    # corporate.mattel.com via RSC flight payload (Next.js App Router)
├── lamley_source.py         # lamleygroup.com HTML scrape
├── feeds.json               # List of RSS URLs (3 entries: 2 autoevolution + 1 lamley)
├── deploy.sh                # SCP-based deploy to VPS; FILES list excludes operator-only modules
├── requirements.txt
├── news.db                  # SQLite: processed_news + pending_articles + published_articles + failed_articles
├── .env / .env.example
├── tests/                   # pytest suite (407 tests)
├── work/
│   ├── completed/           # Finalized features (manual-review-workflow lands here)
│   └── archived/            # Deferred features (llm-transcreation)
└── .claude/skills/project-knowledge/references/  # This doc tree
```

**Operator-side vs cron-side split:** `hw_review.py` + `preview_renderer.py` run in the operator's Claude Code session only (local machine). `news_bot.py` + sources + `telegraph_publisher.py` + `pending_articles_repo.py` run on the VPS cron.

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
- **Auth method:** None. Parsed via the embedded RSC flight payload
  (`self.__next_f.push([1, "..."])`) — Mattel migrated to Next.js App
  Router in 2026-04, so the legacy `__NEXT_DATA__` script tag is gone.
  Listing entries are extracted from the largest push under the anchor
  `"article2":{"entries":[`; article bodies are reconstructed from a
  separate text-row marker `<row-id>:T<hex-len>,<content>` referenced
  by `body: "$<row-id>"`.

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

The pipeline is split into a **cron prep phase** (no operator, 12h interval) and a **manual review loop** (operator in Claude Code session). Both paths converge on the same `publish_article` + `send_telegraph_teaser` output. An auto-fallback via Gemini activates if the operator is absent too long or the queue overflows.

### Cron prep phase — `news_bot.job()` every 12h

1. **Idle-fallback pass** (two steps, both against `pending_articles`):
   (a) Find rows with `fetched_at` older than `IDLE_TIMEOUT_HOURS` AND `notified_at IS NULL` → send ONE consolidated admin ping to `TELEGRAM_ADMIN_ID` + stamp `notified_at`.
   (b) Find rows with `notified_at` older than `GRACE_WINDOW_HOURS` AND `ru_paragraphs IS NULL` → call `_fallback_publish(row, via_review=False)` on each (Gemini pipeline).
2. **Fetch** all sources via `SOURCES` registry; each entry gets `source_name` via `_resolve_source_name(link)` → netloc → `autoevolution` / `mattel` / `lamley` / `other`.
3. **Dedup** against `processed_news` AND `pending_articles` (no re-fetch of seen links).
4. **Overflow fast-track** (newest-10 window rule — see patterns.md): if `count_pending() + len(new) > QUEUE_CAP` (10), publish `excess` oldest rows via Gemini. Old pending ru-NULL first, then oldest-indexed new entries. Staged rows always protected.
5. **Insert** accepted entries into `pending_articles` via `pending_articles_repo.insert_pending` (JSON-serialised paragraphs/images/blocks).
6. **Admin ping** via `build_admin_ping(rows)` to `TELEGRAM_ADMIN_ID` — only when queue non-empty.

### Manual review loop — `hw_review.py` (operator + Claude Code session)

1. Operator opens Claude Code → `hw_review list` shows the queue + `⚠️` failed-footer.
2. Claude loads `ux-guidelines.md` (mandatory), reads `hw_review show N`.
3. Claude proposes title + alts + subtitle + paragraphs to operator; operator signs off.
4. `hw_review stage N --ru-title ... --ru-subtitle ... < translation.json` — RU fields persisted to the pending row.
5. `hw_review preview N` — `telegraph_publisher.preview_nodes` → `preview_renderer.render_html` → file in `~/.cache/hw-review/` (mode `0700`, path guard). `webbrowser.open` on the resolved path.
6. `hw_review publish N` — idempotent per Decision 9:
   - If `telegraph_url` already set (retry after prior Telegram-send fail), skip `createPage`.
   - Else: `publish_article` → `mark_telegraph_published(link, url, path)` (persist BEFORE Telegram).
   - `send_telegraph_teaser(telegraph_url, row['link'])` — hashtag derived from source URL via `_source_hashtag` (Decision 14).
   - On both success: `move_to_published(link, via_review=True)` (single repo transaction: INSERT published + INSERT OR IGNORE processed + DELETE pending) + `_cleanup_preview_html`.
7. `hw_review skip N` (with y/N prompt if staged) / `hw_review take N` (clear_notified) / `hw_review retry N` (re-queue from failed).

### Channel post output (identical for manual and fallback paths)

- Message body: `#{source_hashtag}` — `autoevolution` / `mattel` / `lamleygroup` per `_source_hashtag`.
- `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` triggers Instant View card.
- Telegra.ph page: hero figure + italic subtitle with `💬 «…»` + bold lead + body blocks (paragraphs, images, iframes) + `Источник:` footer.

---

## Data Model

**Database:** SQLite 3 (single file `news.db`). DDL owned by `pending_articles_repo.init_schema(conn)` (called from `news_bot.init_db()`); `processed_news` remains owned by `news_bot` itself.

### Tables

**processed_news** — dedup
- `link` (TEXT PRIMARY KEY), `title`, `pub_date`, `processed_at`
- Written whenever a link is published (manual OR fallback) or skipped. Acts as the "seen" blacklist for future fetches.

**pending_articles** — WIP review queue (cap `QUEUE_CAP`, default 10)
- `link` PRIMARY KEY, `source_name`, `feed_url`, `title`, `subtitle`
- EN content: `paragraphs`, `images`, `blocks` (JSON, `ensure_ascii=False`)
- RU content (NULL until staged): `ru_title`, `ru_subtitle`, `ru_paragraphs`, `ru_blocks`
- Publish state: `telegraph_url`, `telegraph_path`, `preview_html_path`
- Bookkeeping: `fetched_at`, `notified_at`, `attempt_count`, `last_error`, `pub_date`

**published_articles** — audit of real publishes
- `link` PK, `title`, `telegraph_url`, `telegraph_path`, `via_review` (1=manual, 0=fallback), `published_at`

**failed_articles** — dead letter after 3 GT attempts
- `link` PK, `title`, `last_error`, `attempt_count`, `failed_at`

### Transactions owned by repo

`pending → published` (on successful publish), `pending → failed` (on 3rd strike), `failed → pending` (retry), `pending → skipped` (operator skip). Each transition is a single SQLite transaction; `processed_news` written as part of published/skipped moves.

### Sensitive Data

**PII fields:** No PII is stored in the database.

**Secrets:** The Telegram bot token (`TELEGRAM_BOT_TOKEN`), channel ID
(`TELEGRAM_CHANNEL_ID`), admin chat ID (`TELEGRAM_ADMIN_ID`), and the
Telegra.ph access token (`TELEGRAPH_ACCESS_TOKEN`, auto-provisioned on
first run) are stored in `.env` — never committed. The `.env` file is
git-ignored. None of these are persisted to the database.

---

## Planned Enhancements

**LLM-powered transcreation fallback** (closes style drift between manual path and auto-fallback)
- Current `_fallback_publish` uses `transcreate_text` (Google Translate + regex). Manual path uses Claude via `ux-guidelines.md`. Styles diverge visibly. Future: route auto-fallback through an LLM call with the same prompt.

**Cross-article linking** (Phase 2 of blocks pipeline)
- `block["runs"]` already carries external `<a href>` metadata. Future pass maps them to our own Telegra.ph URLs when the linked target is already published.

**Observability**
- Beyond per-row admin pings: uptime checks, failure digest, maybe a read-only dashboard.
