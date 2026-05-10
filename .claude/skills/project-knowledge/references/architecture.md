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
├── news_bot.py              # Daily 10:00 МСК cron entry point: job() runs crash-loop guard,
│                              fetch + stage + admin ping, computes distributed-publish slots
│                              (10:00–20:00 МСК window, ≥90 min interval, ~7/day) and
│                              publishes via _fallback_publish in a sleep-between-slots loop.
│                              SOURCES registry, build_admin_ping, sanitize_error_message,
│                              transcreate_text (HW glossary safety net + emoji prefix only —
│                              bureaucratic regex and 4000-char truncation removed in
│                              llm-transcreation feature).
├── claude_transcreation.py  # Anthropic Claude API wrapper for the auto-publish path
│                              (llm-transcreation feature). Loads ux-guidelines.md as
│                              system prompt (subdir-then-flat fallback, Decision 8),
│                              composes JSON envelope, max_tokens=8000, validates
│                              response shape (paragraph-count match, defensive 4000-char
│                              per-paragraph cap), classifies anthropic SDK exceptions
│                              into outage vs per-article. Imported by news_bot.py at
│                              startup — without this module, ImportError on cron tick.
├── compute_publish_slots.py # Pure-functional distributed-publish algorithm (llm-transcreation
│                              feature). compute_publish_slots(N, now, window_start, window_end,
│                              min_interval_min=40) -> (slots, carry_over). No external deps.
│                              Imported by news_bot.py at startup.
├── outage_state.py          # SQLite-backed outage state machine (llm-transcreation feature).
│                              5-state protocol (no_outage → ping_1_sent → ping_2_sent →
│                              google_fallback_active → recovery_pending) with 2-ping +
│                              2h grace window before global Google fallback. State
│                              persists in bot_state table; BEGIN IMMEDIATE for atomicity;
│                              PRAGMA busy_timeout=5000. Imported by news_bot.py and
│                              claude_transcreation.py at startup.
├── pending_articles_repo.py # DAO owning all SQLite tables (DDL + CRUD + transactional moves);
│                              init_schema() now also creates bot_state(key, value) idempotently.
├── preview_renderer.py      # Local HTML preview builder (CSP + tag/URL allowlists)
│                              Archived 2026-04-30 — used only by the dormant hw_review preview flow.
├── hw_review.py             # Operator-facing CLI (list/show/stage/skip/preview/publish/take/retry).
│                              Archived 2026-04-30 — code + tests preserved, dormant in production.
│                              Runs in Claude Code session, NOT on production cron server.
├── telegraph_publisher.py   # Telegra.ph API client + public preview_nodes() node-tree builder
├── autoevolution_source.py  # RSS + Cloudflare-bypass scrape (curl_cffi)
├── mattel_news_source.py    # corporate.mattel.com via RSC flight payload (Next.js App Router)
├── lamley_source.py         # lamleygroup.com HTML scrape
├── feeds.json               # List of RSS URLs (3 entries: 2 autoevolution + 1 lamley)
├── deploy.sh                # SCP-based deploy to VPS; FILES list excludes operator-only modules
├── requirements.txt
├── news.db                  # SQLite: processed_news + pending_articles + published_articles +
│                              failed_articles + bot_state (key/value, llm-transcreation outage)
├── .env / .env.example
├── tests/                   # pytest suite (~500 tests after llm-transcreation feature)
├── work/
│   ├── completed/           # Finalized features (manual-review-workflow lands here)
│   └── archived/            # Deferred features
└── .claude/skills/project-knowledge/references/  # This doc tree
    └── ux-guidelines.md     # Editorial transcreation prompt — operator-side AND cron-side
                              # runtime dependency since llm-transcreation feature (see split
                              # below). Read by claude_transcreation._load_prompt as Claude
                              # API system prompt.
```

**Operator-side vs cron-side split:**
- **Operator-side only — archived 2026-04-30** (code preserved, dormant): `hw_review.py`, `preview_renderer.py`. Never deployed to the VPS; was meant for the operator's local Claude Code session. The auto-LLM path replaced this in production. Files stay on disk + tests remain green so the path can be revived ad-hoc.
- **Cron-side only** (deployed to VPS): `news_bot.py`, `pending_articles_repo.py`, `telegraph_publisher.py`, source parsers (`autoevolution_source.py`, `mattel_news_source.py`, `lamley_source.py`), and the four files added by the `llm-transcreation-and-distributed-publishing` feature:
  - `claude_transcreation.py` — Anthropic SDK wrapper, imported by `news_bot.py`.
  - `compute_publish_slots.py` — distributed-publish algorithm, imported by `news_bot.py`.
  - `outage_state.py` — outage state machine, imported by `news_bot.py` and `claude_transcreation.py`.
  - `.claude/skills/project-knowledge/references/ux-guidelines.md` — Claude API system prompt. **Architectural shift (closes AC28):** previously operator-side-only (loaded into the operator's Claude Code session); now ALSO a cron-side runtime dependency. Read by `claude_transcreation._load_prompt`. The deploy bundle ships it via `scp` (without `-r`), which flattens subdirs — on the server the file lands at `$DEPLOY_PATH/ux-guidelines.md`. Decision 8 of the `llm-transcreation-and-distributed-publishing` tech-spec covers the layout: `_load_prompt` tries the subdir path first, then falls back to the flat filename.

  All four are listed as a single "cron-side files added by llm-transcreation feature" checklist for operator + future devs: deploy bundle MUST contain all four after deploy, otherwise the bot crashes on startup (missing `*.py`) or sits in Google fallback all day (missing `ux-guidelines.md`).
- **Shared local + cron**: `requirements.txt`, `feeds.json`, `.env.example`, `news.db` (SQLite — cron-only data, never overwritten on deploy).

---

## Key Dependencies

**Critical packages:**

- `feedparser` – Parses RSS feeds to extract article entries.
- `requests` – Generic HTTP for Telegra.ph API, Mattel, Lamley.
- `curl_cffi` – Chrome-impersonating HTTP client for autoevolution
  (bypasses Cloudflare's `HTTP 403` on plain `requests`).
- `beautifulsoup4` – Extracts title, body, images, and inline links from HTML.
- `deep-translator` – Google Translate fallback engine. Used by `transcreate_text`
  in `news_bot.py` for per-article fallback (Claude refused/malformed) and
  for global API-level outage fallback. Bureaucratic regex post-processing
  removed in the llm-transcreation feature; HW glossary safety net + emoji
  prefix retained.
- `anthropic>=0.45.0,<0.46.0` – Anthropic Python SDK. Primary translator for
  the auto-publish path (llm-transcreation feature). Used by `claude_transcreation`
  with `ux-guidelines.md` as system prompt + JSON envelope. Pinned to lock
  the exception class hierarchy referenced in the per-article vs API-level
  classifier.
- `pytz>=2024.1` – IANA timezone library. Required by `schedule==1.2.1` for
  `Job.at(time_str, tz=...)`; stdlib `zoneinfo.ZoneInfo` is rejected by the
  scheduler with `ScheduleValueError` (verified). Used for the daily
  10:00 МСК cron trigger in `news_bot.main()`.
- `python-telegram-bot` – Posts the channel card with
  `LinkPreviewOptions(url=telegraph_url, show_above_text=True,
  prefer_large_media=True)` for a full-width INSTANT VIEW preview,
  and delivers admin failure notifications.
- `schedule==1.2.1` – In-process job scheduling. Daily fixed-time cron
  (`every().day.at("10:00", tz=pytz.timezone("Europe/Moscow"))`).

---

## External Integrations

**Anthropic Claude API (`api.anthropic.com`)**
- **Purpose:** Primary translator/transcreator for the auto-publish path
  (llm-transcreation feature). Loads `ux-guidelines.md` as the system
  prompt + a JSON envelope, sends the EN article as the user message,
  parses the Claude response into `{title, alts[2-3], subtitle, paragraphs,
  blocks?}`. Per-article failures (refusal, malformed JSON, 4xx) fall
  through to Google Translate for that article only; API-level outages
  (auth, rate-limit, network, 5xx) trigger the 2-ping protocol + 2 h
  grace before global Google fallback.
- **Auth method:** `ANTHROPIC_API_KEY` env var (obtained from
  https://console.anthropic.com → API Keys → Create Key, format
  `sk-ant-api03-…`). The key is redacted from logs by
  `_TokenRedactingFilter` (pattern `sk-ant-[A-Za-z0-9_=.-]{16,}`) and
  from admin Telegram pings by the shared `_redact_text` helper.
- **Default model:** `claude-haiku-4-5` (override via `ANTHROPIC_MODEL`).
  Cost ≈ $3/month at 10 articles/day. Sonnet 4.6 ≈ $15/month for higher
  quality.

**Google Translate (via deep-translator)**
- **Purpose:** Fallback translator. Per-article fallback when Claude
  refuses or produces malformed output; global fallback during Claude
  API outages (after the 2-ping protocol exhausts the 2 h grace window).
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

The pipeline is the **cron prep + distributed-publish phase** (no operator, daily at 10:00 МСК) — auto-LLM transcreation → Telegra.ph → Telegram. Production uses OpenRouter (`openai/gpt-5.4-mini`) as the primary translator with Google Translate as per-article + global fallback.

A second loop — the **manual review loop** (`hw_review.py` CLI in operator's Claude Code session) — is documented below for completeness but **archived as of 2026-04-30**: the auto path produces 100 % of channel posts in production. The manual loop's code is preserved (740 tests stay green) so it can be revived ad-hoc for one-off articles.

### Cron prep + distributed-publish phase — `news_bot.job()` daily at 10:00 МСК

1. **Crash-loop guard.** Read `MAX(published_at)` from `published_articles`. If `now - last_published < MIN_INTERVAL_MINUTES (90)`, sleep until that gap elapses before continuing. Protects the channel from burst posting under systemd/Docker rapid-restart loops.
2. **Fetch** all sources via `SOURCES` registry; each entry gets `source_name` via `_resolve_source_name(link)` → netloc → `autoevolution` / `mattel` / `lamley` / `other`. Boilerplate filter, image policy, dedup unchanged.
3. **Dedup + relevance.** Filter against `processed_news` AND `pending_articles` (no re-fetch of seen links). Also drop sibling-brand articles via `_is_hot_wheels_relevant` — autoevolution cross-tags Matchbox / Mega Bloks under "Hot Wheels", the channel is HW-only. After `fetch_full_article` runs, drop bare-checklist posts via `_is_text_only_checklist` (title contains whole-word "checklist" AND paragraph body < 500 chars — typical orangetrack list-of-cars-with-no-prose pattern).
4. **Insert** accepted entries into `pending_articles` via `pending_articles_repo.insert_pending` (JSON-serialised paragraphs/images/blocks). Staged rows from prior days remain in the queue as carry-over.
5. **Compute schedule.** `compute_publish_slots(N=count_pending(), now, window_start=10:00 МСК, window_end=20:00 МСК, min_interval_min=90)` → `(slots, carry_over)`. `posts_today ≈ min(N, 7)`.
6. **Admin ping.** `build_admin_ping(rows, slots, carry_over)` to `TELEGRAM_ADMIN_ID` — schedule for the day. Suppressed when `N=0`. Additional warning ping when `len(pending) > 50` (AC20).
7. **Publish loop.** For each slot in `slots`:
   - Window-end guard: if `slot > 20:00 МСК`, break (excess becomes carry-over).
   - `time.sleep((slot - now).total_seconds())` until slot arrives.
   - Pop next row via `list_pending()` two-tier ordering: today's freshly-fetched batch first (in fetch order), then carry-over backlog drained oldest-first.
   - **Idempotency guard (publish-idempotency-fix, 2026-05-07):** at the very top of `_fallback_publish`, BEFORE any side effect, check `pending_articles_repo.get_published(link)`. If non-`None`, the pending row is a zombie (some prior `move_to_published` left an inconsistent state, or fetch loop re-staged a published link). Log INFO with `[idempotency-guard]` tag, send admin-ping `«⚠️ Пропущен дубль публикации»`, call `skip_pending(link)` (atomic `INSERT OR IGNORE → processed_news` + `DELETE → pending_articles`), return `True`. Slot loop treats as success — no `attempt_count` strike, no `move_to_failed`. If `skip_pending` itself raises (DB-level), log ERROR + send a second admin-ping `«⚠️ Не удалось снять зомби-строку»` and STILL return `True` (subscriber-visible duplicate prevention is the primary contract).
   - If `outage_state.is_fallback_active()`, route directly to Google Translate; otherwise call `_fallback_publish` (Claude primary, Google per-article fallback).
   - On `OutageError` (state-machine signal): the state machine already recorded the event and routed this article through Google. Continue to next slot.
   - On unexpected exception: standard 3-strikes attempt counter → `move_to_failed`.

### Manual review loop — `hw_review.py` (archived 2026-04-30)

> **Status:** dormant. Operator declared production ready on 2026-04-30 EOD and stopped exercising this path. `hw_review.py` + tests are preserved verbatim — the workflow below still works if revived for a specific article.

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

### Channel post output (identical for all paths)

- Message body: `#{source_hashtag}` — `autoevolution` / `mattel` / `lamleygroup` per `_source_hashtag`. Single-line, byte-identical regardless of which translation engine produced the RU text (Decision 14). Holds even if the archived manual path is revived.
- `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` triggers Instant View card.
- Telegra.ph page: hero figure + italic subtitle with `💬 «…»` + bold lead + body blocks (paragraphs, images, iframes) + `Источник:` footer.
- The `↳ автоперевод` marker on the Telegra.ph page (plain `<p>` IMMEDIATELY BEFORE the `Источник:` footer) marks a quality warning for readers, **not a path differentiator**. It fires only when `_fallback_publish` translated the article via Google Translate (per-article LLM failure or global outage fallback) — wired as `auto_marker=used_google_fallback and not via_review` in [`news_bot.py:1195`](../../../../news_bot.py#L1195). LLM-translated auto publishes don't carry it. The dormant manual path also wouldn't carry it (`via_review=True`). See `patterns.md` § "Channel post format" for rationale and wiring.

---

## Data Model

**Database:** SQLite 3 (single file `news.db`). DDL owned by `pending_articles_repo.init_schema(conn)` (called from `news_bot.init_db()`); `processed_news` remains owned by `news_bot` itself.

### Tables

**processed_news** — dedup
- `link` (TEXT PRIMARY KEY), `title`, `pub_date`, `processed_at`
- Written whenever a link is published (manual OR fallback) or skipped. Acts as the "seen" blacklist for future fetches.

**pending_articles** — WIP review queue (no hard cap since the llm-transcreation feature; the distributed-publish algorithm caps at 11 publishes/day with carry-over, and an admin-warning fires at `len(pending) > 50`)
- `link` PRIMARY KEY, `source_name`, `feed_url`, `title`, `subtitle`
- EN content: `paragraphs`, `images`, `blocks` (JSON, `ensure_ascii=False`)
- RU content (NULL until staged): `ru_title`, `ru_subtitle`, `ru_paragraphs`, `ru_blocks`
- Publish state: `telegraph_url`, `telegraph_path`, `preview_html_path`
- Bookkeeping: `fetched_at`, `notified_at`, `attempt_count`, `last_error`, `pub_date`
- **Block types** (in `blocks` / `ru_blocks` JSON): `paragraph`, `lead`, `heading` (with `level: 3` or `4`), `image`, `video`, and `list_item` (added 2026-05-08 by orangetrack-rendering-fixes — children of `<ul>`/`<ol>` from orangetrack content). Text-bearing types (`paragraph`/`lead`/`heading`/`list_item`) all carry an optional `runs` field with inline metadata: `[{text, [href], [formats]}]` where `formats` is a list of `bold`/`italic`/`underline`/`strikethrough` markers. `_PATCHED_TEXT_BLOCK_TYPES` in `_llm_common.py` lists the 4 patchable types — `_patch_text_with_ru_paragraphs` rewrites their `text` from LLM output and preserves `type`, `runs`, `level`.

**published_articles** — audit of real publishes
- `link` PK, `title`, `telegraph_url`, `telegraph_path`, `via_review` (1=manual, 0=auto-LLM), `published_at`. Since the manual path was archived on 2026-04-30 every new row carries `via_review=0`; existing `via_review=1` rows in `published_articles` are historical (manual-review-workflow era).
- **Insert is idempotent** (publish-idempotency-fix, 2026-05-07): `move_to_published` step 1 uses `INSERT OR IGNORE` so a second move with the same `link` is a no-op rather than `IntegrityError`. Original values (telegraph_url, via_review, published_at) are preserved from the first move. Step 2 (`processed_news`) and step 3 (`DELETE pending_articles`) execute unconditionally.
- **Post-commit defensive verification** (2026-05-08, after a prod incident where `published_articles` had rows but `processed_news` did not): `move_to_published` re-queries `processed_news` after the main commit. If the entry is missing, dozapis with `INSERT OR IGNORE` and emits a WARNING log. Self-healing — closes the zombie-row recurrence loop where `is_processed(link)` returns False on next fetch and re-stages an already-published link.

**failed_articles** — dead letter after 3 GT attempts
- `link` PK, `title`, `last_error`, `attempt_count`, `failed_at`

**bot_state** — small key/value store for cross-tick state (added by llm-transcreation feature)
- `key` (TEXT PRIMARY KEY), `value` (TEXT)
- Active keys (all values stored as ISO-8601 strings or `'0'`/`'1'`/`'2'` for counters/flags):
  - `outage_started_at` — ISO timestamp when first Claude outage error fired. NULL = no active outage.
  - `last_ping_sent_at` — ISO timestamp of the last admin ping (#1, #2, or recovery).
  - `ping_count` — `'1'` after ping #1, `'2'` after ping #2, `'3'` after the fallback-switch ping.
  - `fallback_active` — `'1'` if Google Translate is the active engine (post-grace), else NULL.
  - `last_health_check_at` — ISO timestamp of the most recent Claude probe attempt during recovery_pending state. Rate-limits probes.
- DDL: `CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT);`. Owned by `pending_articles_repo.init_schema()`. `outage_state.py` provides typed accessors and state-machine helpers (`record_outage_event`, `record_recovery_event`) wrapped in `BEGIN IMMEDIATE` for atomicity. `PRAGMA busy_timeout=5000` absorbs typical contention with `hw_review` CLI writers.

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

**Cross-article linking** (Phase 2 of blocks pipeline)
- `block["runs"]` already carries external `<a href>` metadata. Future pass maps them to our own Telegra.ph URLs when the linked target is already published.

**Observability**
- Beyond per-row admin pings: uptime checks, failure digest, maybe a read-only dashboard.
