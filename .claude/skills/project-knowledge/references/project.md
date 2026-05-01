# Project Context

## Purpose
This file provides high-level project overview for AI agents. Helps agents understand WHAT we're building and WHY.

---

## Project Overview

**Name:** Hot Wheels News Bot

**Description:** A Hot Wheels news pipeline. A **daily cron tick at 10:00 МСК** on a VPS fetches from autoevolution.com, lamleygroup.com, and corporate.mattel.com, dedups, and stages articles into a SQLite queue, then a **distributed-publish loop** (10:00–20:00 МСК window, ≥90 min between publishes, ~7 articles/day) translates each article via an LLM (default OpenRouter `openai/gpt-5.4-mini`; Anthropic Claude / Google Gemini / OpenAI as alternates — engine selected by `LLM_PROVIDER` or by which API key is configured) and publishes to Telegra.ph + Telegram. A **Google Translate fallback** covers per-article LLM failures and global LLM outages so the channel never goes dark. A separate **manual review loop** (operator in Claude Code session) is available for cases the operator wants to review before publishing.

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
- **Two-tier translation** — primary: configured LLM. Per-article fallback: Google Translate (when the LLM refuses or returns malformed output, no state change). Global fallback: Google Translate after the 2-ping + 2 h grace outage protocol exhausts.
- **Manual review path (optional)** — operator in a Claude Code session uses `hw_review.py` CLI (`list / show / stage / skip / preview / publish / take / retry`) to translate articles style-pinned to `ux-guidelines.md`. Strict 1:1 transcreation; 2-3 alt titles per article.
- **Local HTML preview** — `preview_renderer` renders the proposed Telegraph node tree into a sandboxed HTML file under `~/.cache/hw-review/` (CSP meta, tag/URL allowlists, path guard). Operator opens in browser before publish.
- **Multi-source aggregation** — 3 sources via `SOURCES` registry: autoevolution (RSS + Cloudflare-bypass scrape), lamley (RSS + HTML scrape), mattel (RSC flight-payload parser, see patterns.md "Mattel RSC flight-payload parser").
- **5-table state model** — `processed_news` (dedup), `pending_articles` (queue, no hard cap; admin warning at >50), `published_articles` (audit with `via_review` flag), `failed_articles` (dead letter after 3 strikes), `bot_state` (outage state machine k/v).
- **Idempotent publishing** — Decision 9: Telegraph URL persisted before Telegram send, so retries after teaser failure reuse the same Telegraph page (no orphan pages on the account).
- **Outage state machine** — 5 states (`no_outage` / `ping_1_sent` / `ping_2_sent` / `google_fallback_active` / `recovery_pending`) with 2-ping protocol + 2 h grace before flipping the bot to global Google Translate. Recovery ping fires on the next slot where the LLM succeeds again.
- **Telegram channel card** — single-line `#{source_hashtag} #news` with `LinkPreviewOptions(show_above_text=True, prefer_large_media=True)` triggers Instant View preview card with ⚡ button. Hashtag identical across manual/auto paths (Decision 14 — `_source_hashtag`, TLD-stripped form). Auto-publishes inject a `↳ автоперевод` marker INSIDE the Telegra.ph page (above source footer); manual publishes don't.
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
- Legacy Gemini-based transcreation (now used only by `_fallback_publish`)
- **Manual-review-workflow** — see `work/completed/manual-review-workflow/`. Split pipeline into cron prep + operator-driven review CLI. 10 coding tasks + 3 audits + pre/post-deploy QA + ~5 ad-hoc fixes landed during live QA. 407 pytest tests.
- **Mattel-parser-rewrite** — see `work/completed/mattel-parser-rewrite/`. Replaced `__NEXT_DATA__` extraction with RSC flight-payload parser after Mattel migrated to Next.js App Router. 1 atomic implementation task + 3 audits + pre-deploy QA. 44 Mattel tests, 0 critical findings, 0 new dependencies.

**Near-term enhancements (Planned)**
- LLM-powered transcreation for the auto-fallback path — closes the style drift vs manual path (archived in `work/archived/llm-transcreation-deferred/`)
- Cross-article linking (`runs[].href` → our own Telegraph URLs when already published)
- Production observability beyond admin pings (uptime, failure digest)

**Future ideas (Backlog)**
- Web dashboard for configuration and monitoring
- Extended translation options (DeepL, Yandex.Translate)
- Support for additional news sources beyond the current three
