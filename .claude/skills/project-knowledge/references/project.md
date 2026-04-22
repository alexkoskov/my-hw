# Project Context

## Purpose
This file provides high-level project overview for AI agents. Helps agents understand WHAT we're building and WHY.

---

## Project Overview

**Name:** Hot Wheels News Bot

**Description:** A Python script that automatically collects Hot Wheels news
from multiple sources (autoevolution.com RSS + scrape, corporate.mattel.com,
lamleygroup.com), translates and adapts each article to Russian, publishes
the full body to Telegra.ph, and posts a hashtag-attributed channel card
with an Instant View preview in Telegram.

This bot runs on a schedule (daily) and handles the entire pipeline from
source fetching to Telegraph publishing and Telegram posting, eliminating
manual work for news aggregation and translation.

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

- **Multi-source aggregation** – Fetches from a list of RSS feeds
  (`feeds.json`, up to 5) plus `corporate.mattel.com` (via `__NEXT_DATA__`).
- **Per-source article fetchers** – Each domain owns its parser
  (autoevolution via Cloudflare-bypass scrape with `curl_cffi`, Mattel via
  `__NEXT_DATA__`, Lamley via HTML scrape).
- **Duplicate detection** – Uses SQLite to track processed articles by URL.
- **Translation + transcreation** – Google Translate + a post-processing
  pass that replaces bureaucratic Russian, fixes Hot Wheels jargon, flips
  a few passive constructions, and prepends a content-aware emoji to titles.
- **Telegraph publishing** – Full Russian translation posted to Telegra.ph
  with hero image, decorated subtitle, body paragraphs, interleaved images,
  and a source footer. Autoevolution additionally preserves
  image/video/heading positions via ordered content blocks.
- **Telegram channel card** – Minimal one-line post (`#{source_label}`) with
  `LinkPreviewOptions(show_above_text=True)` so Telegram renders the
  Telegra.ph page as an Instant View preview card with the ⚡ button.
- **Admin notifications** – Source failures are delivered to a separate
  admin chat via the same bot.
- **Scheduling** – Runs daily at 12:00 local time via the `schedule` library.

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
- Multiple RSS feeds via `feeds.json` (completed — see
  `work/completed/multiple-rss-feeds/`)
- Mattel corporate news source (completed — see
  `work/mattel-news-source/`)
- Lamley source (completed as part of telegraph-pipeline)
- Cloudflare bypass for autoevolution via `curl_cffi`
- Telegra.ph publishing with Instant View preview + locked channel post
  format (see `work/telegraph-pipeline/`)
- Transcreation pass (plain Russian, HW glossary, deterministic emoji
  prefix for titles)

**Near-term enhancements (Planned)**
- Cross-article linking: map `runs[].href` in autoevolution blocks to our
  own Telegra.ph pages when we've already published the target
  (Phase 2 — placeholder lives in `telegraph_publisher._build_content_from_blocks`)
- Health monitoring and per-source error reporting beyond admin messages

**Future ideas (Backlog)**
- Web dashboard for configuration and monitoring
- LLM-powered transcreation (higher-quality Russian than Google + rules)
- Extended translation options (DeepL, Yandex.Translate)
- Support for additional news sources beyond the current three
