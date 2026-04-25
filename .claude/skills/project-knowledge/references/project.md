# Project Context

## Purpose
This file provides high-level project overview for AI agents. Helps agents understand WHAT we're building and WHY.

---

## Project Overview

**Name:** Hot Wheels News Bot

**Description:** A Hot Wheels news pipeline split into two halves. A **cron prep phase** (every 12h on a VPS) fetches from autoevolution.com, lamleygroup.com, and corporate.mattel.com, dedups, and stages articles into a SQLite review queue. A **manual review loop** (operator in Claude Code session) then translates articles via a style-pinned prompt and publishes them to Telegra.ph + Telegram. A **Gemini auto-fallback** covers idle-timeout and queue-overflow cases so the channel never blocks on operator absence.

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

- **Manual-review workflow** — operator in a Claude Code session uses `hw_review.py` CLI (`list / show / stage / skip / preview / publish / take / retry`) to translate articles style-pinned to `ux-guidelines.md` (role: ведущий редактор/локализатор). Strict 1:1 transcreation; 2-3 alt titles per article.
- **Local HTML preview** — `preview_renderer` renders the proposed Telegraph node tree into a sandboxed HTML file under `~/.cache/hw-review/` (CSP meta, tag/URL allowlists, path guard). Operator opens in browser before publish.
- **Multi-source aggregation** — 3 sources via `SOURCES` registry: autoevolution (RSS + Cloudflare-bypass scrape), lamley (RSS + HTML scrape), mattel (RSC flight-payload parser, see patterns.md "Mattel RSC flight-payload parser").
- **4-table state model** — `processed_news` (dedup), `pending_articles` (WIP queue ≤10, cap configurable), `published_articles` (audit with `via_review` flag), `failed_articles` (dead letter after 3 GT attempts).
- **Idempotent publishing** — Decision 9: Telegraph URL persisted before Telegram send, so retries after teaser failure reuse the same Telegraph page (no orphan pages on the account).
- **Two safety nets** — idle-fallback (auto-publish via Gemini after ~24h operator absence) and overflow fast-track (newest-10 window rule: anything exceeding queue cap goes through Gemini).
- **Telegram channel card** — one-line `#{source_hashtag}` with `LinkPreviewOptions(show_above_text=True)` triggers Instant View preview card with ⚡ button. Hashtag unchanged across manual/fallback paths (Decision 14 — uses `_source_hashtag`, TLD-stripped form).
- **Admin pings** — queue-pressure notifications, idle-fallback heads-ups, error digests go to `TELEGRAM_ADMIN_ID` (operator's personal chat), NOT the public channel.
- **Scheduling** — cron every 12h via `schedule.every(12).hours.do(job)`.

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
- Bug-fix for `tests/test_hw_review_retry.py::TestListFooter::test_list_footer_format_exact` — pre-existing list-footer order issue, not blocking

**Future ideas (Backlog)**
- Web dashboard for configuration and monitoring
- Extended translation options (DeepL, Yandex.Translate)
- Support for additional news sources beyond the current three
