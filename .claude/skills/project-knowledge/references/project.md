# Project Context

## Purpose
This file provides high-level project overview for AI agents. Helps agents understand WHAT we're building and WHY.

---

## Project Overview

**Name:** Hot Wheels News Bot

**Description:** A Hot Wheels news pipeline. A **daily cron tick at 10:00 МСК** on a VPS fetches from autoevolution.com, lamleygroup.com, and corporate.mattel.com, dedups, and stages articles into a SQLite queue, then a **distributed-publish loop** (10:00–20:00 МСК window, ≥90 min between publishes, ~7 articles/day) translates each article via an LLM (default OpenRouter `openai/gpt-5.4-mini`; Anthropic Claude / Google Gemini / OpenAI as alternates — engine selected by `LLM_PROVIDER` or by which API key is configured) and publishes to Telegra.ph + Telegram. On an **API-level LLM outage** the article is **held in the queue** (hold-and-wait, 2026-06-11) — nothing is published, no machine translation — and retried on the next slot/day until the LLM recovers; per-article LLM failures strike out (3 → `failed_articles`). The auto-LLM loop is the **primary and currently sole production path**. An operator-driven manual review path (`hw_review.py` CLI) exists in code and tests but has been archived since 2026-04-30 — kept dormant for revival, not actively used.

---

## Target Audience

**Primary users:** Hot Wheels enthusiasts and collectors who want to stay updated with the latest news in Russian.

**Use case:** Users subscribe to a Telegram channel where they receive automated, translated summaries of Hot Wheels news, saving time and overcoming language barriers.

---

## Core Problem

Manually monitoring Hot Wheels news across websites is time‑consuming, and many enthusiasts are not comfortable reading English content. This results in missed updates and delayed information.

Currently users have to regularly check multiple sites, translate articles themselves, and manually share them. This is slow and inconsistent because it relies on manual effort. We solve this by automating the entire process: RSS monitoring, article scraping, translation, summarization, and Telegram posting.

---

## Key Features

- **Auto-publish path (default)** — daily cron tick at 10:00 МСК fetches → stages → distributes publishes across the 10:00–20:00 МСК window (≥90 min between posts, ~7/day). LLM transcreation (style-pinned to `ux-guidelines.md`, role: ведущий редактор/локализатор) → Telegra.ph → Telegram channel card.
- **Pluggable LLM engines** — `claude_transcreation`, `gemini_transcreation`, `openai_transcreation`, `openrouter_transcreation` share `_llm_common.py` (prompt loading skeleton, JSON envelope, response parsing, emoji safety net, EN-leak guard). Engine selected by `LLM_PROVIDER` env var or by which API key is set; new engines plug in by mirroring the existing public API. Default engine: `openrouter` (model `openai/gpt-5.4-mini`).
- **Hold-and-wait on outage (2026-06-11)** — single-engine translation via the configured LLM. On an API-level LLM outage the article is HELD in `pending_articles` (nothing published, no machine translation) and retried on the next slot/day until the LLM recovers. Per-article LLM failures (refusal / malformed output) bump `attempt_count` and strike out after 3 (→ `failed_articles`). The Google Translate helper (`transcreate_text`) is kept in code but DORMANT — no longer wired into the publish path.
- **Manual review path (archived 2026-04-30)** — `hw_review.py` CLI (`list / show / stage / skip / preview / publish / take / retry`) for operator-driven transcreation of single articles. Code preserved + 740 tests stay green; not used in production. May be revived ad-hoc if a specific article needs hand-crafting.
- **Local HTML preview (archived 2026-04-30)** — `preview_renderer` renders the proposed Telegraph node tree into a sandboxed HTML file under `~/.cache/hw-review/` (CSP meta, tag/URL allowlists, path guard). Used only by the dormant `hw_review preview` flow.
- **Multi-source aggregation** — 3 sources via `SOURCES` registry: autoevolution (RSS + Cloudflare-bypass scrape), lamley (RSS + HTML scrape), mattel (RSC flight-payload parser, see patterns.md "Mattel RSC flight-payload parser").
- **5-table state model** — `processed_news` (dedup), `pending_articles` (queue, no hard cap; admin warning at >50), `published_articles` (audit with `via_review` flag), `failed_articles` (dead letter after 3 strikes), `bot_state` (outage state machine k/v).
- **Idempotent publishing** — Decision 9: Telegraph URL persisted before Telegram send, so retries after teaser failure reuse the same Telegraph page (no orphan pages on the account).
- **Outage state machine** — 5 internal states (`no_outage` / `ping_1_sent` / `ping_2_sent` / `google_fallback_active` / `recovery_pending`) driving the operator-ping cadence: ping #1 immediately, #2 after 1 h, #3 after 2 h. Since the 2026-06-11 hold-and-wait change the machine no longer switches the bot to Google Translate — posts stay held; the `google_fallback_active` label and `fallback_active` flag are retained as state names but are DORMANT (not read by the publish path). Recovery ping fires on the next slot where the LLM succeeds again.
- **Telegram channel card** — single-line `#{source_hashtag} #news` with `LinkPreviewOptions(show_above_text=True, prefer_large_media=True)` triggers Instant View preview card with ⚡ button. Hashtag identical across all paths (Decision 14 — `_source_hashtag`, TLD-stripped form). The `↳ автоперевод` marker is no longer emitted: since the 2026-06-11 hold-and-wait change there is no Google-fallback publish branch, so `_fallback_publish` always passes `auto_marker=False`. Everything that publishes is LLM-translated and carries no marker.
- **Admin pings** — plan-of-day, backlog warnings, outage transitions, error digests go to `TELEGRAM_ADMIN_ID` (operator's personal chat), NOT the public channel.
- **Scheduling** — daily fixed-time cron at 10:00 МСК via `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)`.

---
## Out of Scope

- No mobile app version.
- No web dashboard or admin panel.
- No multi‑language support beyond Russian.
- No real‑time notifications outside scheduled runs.
- No user authentication or personalization.

---

## Development Roadmap

**Delivered**
- Multiple RSS feeds via `feeds.json` — see `work/completed/multiple-rss-feeds/`
- Mattel corporate news source — see `work/mattel-news-source/` (original) + `work/completed/mattel-parser-rewrite/` (RSC flight-payload rewrite, 2026-04-25)
- Lamley source, Cloudflare bypass for autoevolution, locked channel-post format — see `work/telegraph-pipeline/`
- Legacy Gemini-based transcreation (now one of the pluggable LLM engines; the old Google-Translate fallback it once backed is dormant since the 2026-06-11 hold-and-wait change)
- **Manual-review-workflow** — see `work/completed/manual-review-workflow/`. Split pipeline into cron prep + operator-driven review CLI. 10 coding tasks + 3 audits + pre/post-deploy QA + ~5 ad-hoc fixes landed during live QA. 407 pytest tests. **Path archived 2026-04-30** — superseded by auto-LLM transcreation in production; code + tests preserved for ad-hoc revival.
- **Mattel-parser-rewrite** — see `work/completed/mattel-parser-rewrite/`. Replaced `__NEXT_DATA__` extraction with RSC flight-payload parser after Mattel migrated to Next.js App Router. 1 atomic implementation task + 3 audits + pre-deploy QA. 44 Mattel tests, 0 critical findings, 0 new dependencies.
- **LLM-transcreation-and-distributed-publishing** (2026-04-26) — primary translator for the auto-publish path is now an LLM (default OpenRouter `openai/gpt-5.4-mini`) reading `ux-guidelines.md` as system prompt; same prompt as the (now-archived) manual path so style drift is closed. Distributed-publish loop (10:00–20:00 МСК window, ≥90 min between slots, ~7/day) replaced the legacy idle-fallback + overflow fast-track. Outage state machine + 2-ping protocol. (Originally shipped a global Google fallback after a 2 h grace; that fallback was removed by the 2026-06-11 hold-and-wait change — outages now hold posts.) See `work/llm-transcreation-and-distributed-publishing/`.
- **Author-plug-filter** (2026-05-04) — variant A (5 patterns in `boilerplate_filter.py` for standalone author plugs) + variant B (`_strip_plugs` / `_strip_plugs_in_blocks` in `news_bot.py`, called from `_fallback_publish` post-translation) for inline plugs. 10 platforms covered (instagram/twitter/x/tiktok/youtube/facebook/reddit/patreon/discord/linktree). Single commit `695b201`. See `work/author-plug-filter/`.

**Near-term enhancements (Planned)**
- Cross-article linking (`runs[].href` → our own Telegraph URLs when already published)
- Production observability beyond admin pings (uptime, failure digest)
- Per-source tone calibration verification — `_build_user_message` already passes `source_name` to the LLM, and `ux-guidelines.md` already carries the per-source notes block (Autoevolution / Lamley / Mattel tone dials). What's not verified: whether the production LLM actually applies the right dial per article. Spot-check a sample of recent publishes per source against the prompt's per-source notes; if drift exists, tighten the prompt (e.g. by labelling the source-name field more explicitly: `source_brand_voice: "autoevolution_blog"`).

**Future ideas (Backlog)**
- Web dashboard for configuration and monitoring
- Extended translation options (DeepL, Yandex.Translate)
- Support for additional news sources beyond the current three
