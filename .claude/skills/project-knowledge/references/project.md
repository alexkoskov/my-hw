# Project Context

## Purpose
This file provides high-level project overview for AI agents. Helps agents understand WHAT we're building and WHY.

---

## Project Overview

**Name:** Hot Wheels News Bot

**Description:** A Hot Wheels news pipeline. A **daily tick at 10:00 МСК** (in-process scheduler inside the Docker container — there is no cron) fetches from autoevolution.com, lamleygroup.com, t-hunted.blogspot.com and orangetrackdiecast.com (corporate.mattel.com is configured but disabled — see Key Features), dedups, and stages articles into a SQLite queue, then a **fixed-slot publish loop** (10:00 / 15:00 / 19:30 МСК, at most one post per slot → **≤3/day**, `news_bot.MAX_DAILY_POSTS`; surplus carries over to the next tick) translates each article via an LLM (default OpenRouter `openai/gpt-5.4-mini`; Anthropic Claude / Google Gemini / OpenAI as alternates — engine selected by `LLM_PROVIDER` or by which API key is configured) and publishes to Telegra.ph + Telegram. On an **API-level LLM outage** the article is **held in the queue** (hold-and-wait, 2026-06-11) — nothing is published, no machine translation — and retried on the next slot/day until the LLM recovers — bounded since 2026-08-04 by the **hold cap**: after `HOLD_CAP` consecutive holds the article yields the queue head for 24 h so the rest of the queue keeps publishing (it is not lost, it returns by itself). Per-article LLM failures strike out (3 → `failed_articles`). The auto-LLM loop is the **primary and currently sole production path**. An operator-driven manual review path (`hw_review.py` CLI) exists in code and tests but has been archived since 2026-04-30 — kept dormant for revival, not actively used.

---

## Target Audience

**Primary users:** Hot Wheels enthusiasts and collectors who want to stay updated with the latest news in Russian.

**Use case:** Users subscribe to a Telegram channel where they receive Hot Wheels news transcreated into Russian in full — not summarised — saving time and overcoming language barriers.

---

## Core Problem

Manually monitoring Hot Wheels news across websites is time‑consuming, and many enthusiasts are not comfortable reading English content. This results in missed updates and delayed information.

Currently users have to regularly check multiple sites, translate articles themselves, and manually share them. This is slow and inconsistent because it relies on manual effort. We solve this by automating the entire process: RSS monitoring, article scraping, full-text LLM transcreation into Russian, Telegra.ph publication and Telegram posting.

---

## Key Features

- **Auto-publish path (default)** — daily tick at 10:00 МСК fetches → stages → publishes at the three fixed slots 10:00 / 15:00 / 19:30 МСК (`compute_publish_slots.DAILY_PUBLISH_TIMES`, operator pacing 2026-06-13), one post per slot, hard cap `MAX_DAILY_POSTS = 3` (news_bot.py:172). The dynamic even-spread `compute_publish_slots()` (≥90 min interval, module defaults `WINDOW_START`/`WINDOW_END` = 13:00–20:00, compute_publish_slots.py:41-43) is kept DORMANT for reference — superseded 2026-06-13 by `compute_fixed_slots()`, see compute_publish_slots.py:111-114. LLM transcreation (style-pinned to `ux-guidelines.md`, role: ведущий редактор/локализатор) → Telegra.ph → Telegram channel card.
- **Pluggable LLM engines** — `claude_transcreation`, `gemini_transcreation`, `openai_transcreation`, `openrouter_transcreation` share `_llm_common.py` (prompt loading skeleton, JSON envelope, response parsing, emoji safety net, EN-leak guard). Engine selected by `LLM_PROVIDER` env var or by which API key is set; new engines plug in by mirroring the existing public API. Default engine: `openrouter` (model `openai/gpt-5.4-mini`).
- **Hold-and-wait on outage (2026-06-11)** — single-engine translation via the configured LLM. On an API-level LLM outage the article is HELD in `pending_articles` (nothing published, no machine translation) and retried on the next slot/day until the LLM recovers. **Hold cap (2026-08-04):** a hold never strikes and the slot loop always re-reads the queue head, so one permanently-failing article used to block the channel forever and silently; past `HOLD_CAP` consecutive holds the row is parked for 24 h ([E038]) and the queue moves on. Per-article LLM failures (refusal / malformed output) bump `attempt_count` and strike out after 3 (→ `failed_articles`). The Google Translate helper (`transcreate_text`) is kept in code but DORMANT — no longer wired into the publish path.
- **Manual review path (archived 2026-04-30)** — `hw_review.py` CLI (`list / show / stage / skip / preview / publish / take / retry`) for operator-driven transcreation of single articles. Code preserved + 740 tests stay green; not used in production. May be revived ad-hoc if a specific article needs hand-crafting.
- **Local HTML preview (archived 2026-04-30)** — `preview_renderer` renders the proposed Telegraph node tree into a sandboxed HTML file under `~/.cache/hw-review/` (CSP meta, tag/URL allowlists, path guard). Used only by the dormant `hw_review preview` flow.
- **Multi-source aggregation** — 4 live sites behind a 2-callable `SOURCES` registry (news_bot.py:3607-3611): the shared RSS fetcher walks `feeds.json` (autoevolution ×2 tags, lamleygroup, t-hunted.blogspot.com) and `_fetch_orangetrack_entries` handles orangetrackdiecast.com from its own feed constant. Per-site body parsers are dispatched by hostname in `fetch_full_article` (news_bot.py:3380-3425): autoevolution (RSS + Cloudflare-bypass scrape), lamley (HTML scrape), t-hunted (Blogspot HTML), orangetrack (RSS `content:encoded`, parsed at fetch time). **mattel is disabled 2026-05-24** — commented out of `SOURCES`; parser and tests retained, rationale at news_bot.py:3599-3606 (site moved to client-side rendering; zero Hot Wheels articles in its whole history). The RSC flight-payload parser is still described in patterns.md § "Mattel source — DISABLED 2026-05-24 (2026-04-25 RSC parser retained)".
- **5-table state model** — `processed_news` (dedup), `pending_articles` (queue, no hard cap; admin warning at >50), `published_articles` (audit with `via_review` flag), `failed_articles` (dead letter after 3 strikes), `bot_state` (outage state machine k/v).
- **Cross-source dedup** (cross-source-dedup 2026-06, dedup-model-series 2026-07, subject-aware broad precision 2026-09) — the same car covered by two of the four sites is caught by CONTENT, not by link. `model_extractor` builds a fingerprint per article (brand+model tokens, Hot Wheels series/line names, and `model|series|tier` pair keys). A shared *distinctive* pair still hard-blocks the newcomer (`[E015]`, irreversible). A shared *broad* pair is eligible for `[E014]` only when its canonical line is present in the effective original title of both articles; a rejected comparison is non-terminal, so later candidates and the unchanged set-overlap backstop still run. `[E014]` has three explicit reasons: title-qualified broad subject (`broad_subject`), ordinary `[30%, 50%)` overlap (`overlap`), and a ≥50% overlap whose block was capped after subject rejection (`overlap_capped`); every flag keeps the existing cancel button and 24 h defer. `dedup_subject_suppressed` counts each affected incoming article once per tick, not rejected pairs or candidate rows, and is informational rather than a drop. Fail-open is unchanged: any crash in the gate stages the article without a fingerprint and pings `[E016]`. Full flow and invariants: architecture.md § Data Flow; change conventions: patterns.md § Cross-source dedup precision.
- **Idempotent publishing** — Decision 9: Telegraph URL persisted before Telegram send, so retries after teaser failure reuse the same Telegraph page (no orphan pages on the account).
- **Outage state machine** — 5 internal states (`no_outage` / `ping_1_sent` / `ping_2_sent` / `google_fallback_active` / `recovery_pending`) driving the operator-ping cadence: ping #1 immediately, #2 after 1 h, #3 after 2 h. Since the 2026-06-11 hold-and-wait change the machine no longer switches the bot to Google Translate — posts stay held; the `google_fallback_active` label and `fallback_active` flag are retained as state names but are DORMANT (not read by the publish path). Recovery ping fires on the next slot where the LLM succeeds again.
- **Telegram channel card** — single-line `#{source_hashtag} #news` with `LinkPreviewOptions(show_above_text=True, prefer_large_media=True)` triggers Instant View preview card with ⚡ button. Hashtag identical across all paths (Decision 14 — `_source_hashtag`, TLD-stripped form). The `↳ автоперевод` marker is no longer emitted: since the 2026-06-11 hold-and-wait change there is no Google-fallback publish branch, so `_fallback_publish` always passes `auto_marker=False`. Everything that publishes is LLM-translated and carries no marker.
- **Admin pings** — plan-of-day, backlog warnings, outage transitions, error digests go to `TELEGRAM_ADMIN_ID` (operator's personal chat), NOT the public channel.
- **Scheduling** — one daily fixed-time tick at 10:00 МСК via `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)` (news_bot.py:4641). The process is long-lived and schedules itself; no crontab is involved. A container restart re-runs `job()` immediately — which is why deploys are barred inside the 10:00–20:00 МСК publish window.

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
- Mattel corporate news source — see `work/completed/mattel-news-source/` (original) + `work/completed/mattel-parser-rewrite/` (RSC flight-payload rewrite, 2026-04-25)
- Lamley source, Cloudflare bypass for autoevolution, locked channel-post format — see `work/completed/telegraph-pipeline/`
- Legacy Gemini-based transcreation (now one of the pluggable LLM engines; the old Google-Translate fallback it once backed is dormant since the 2026-06-11 hold-and-wait change)
- **Manual-review-workflow** — see `work/completed/manual-review-workflow/`. Split pipeline into cron prep + operator-driven review CLI. 10 coding tasks + 3 audits + pre/post-deploy QA + ~5 ad-hoc fixes landed during live QA. 407 pytest tests. **Path archived 2026-04-30** — superseded by auto-LLM transcreation in production; code + tests preserved for ad-hoc revival.
- **Mattel-parser-rewrite** — see `work/completed/mattel-parser-rewrite/`. Replaced `__NEXT_DATA__` extraction with RSC flight-payload parser after Mattel migrated to Next.js App Router. 1 atomic implementation task + 3 audits + pre-deploy QA. 44 Mattel tests, 0 critical findings, 0 new dependencies.
- **LLM-transcreation-and-distributed-publishing** (2026-04-26) — primary translator for the auto-publish path is now an LLM (default OpenRouter `openai/gpt-5.4-mini`) reading `ux-guidelines.md` as system prompt; same prompt as the (now-archived) manual path so style drift is closed. Distributed-publish loop (10:00–20:00 МСК window, ≥90 min between slots, ~7/day) replaced the legacy idle-fallback + overflow fast-track. Outage state machine + 2-ping protocol. (Originally shipped a global Google fallback after a 2 h grace; that fallback was removed by the 2026-06-11 hold-and-wait change — outages now hold posts.) See `work/completed/llm-transcreation-and-distributed-publishing/`.
- **External uptime watch** (2026-07-31) — `.github/workflows/uptime.yml`, a GitHub-Actions-hosted probe running roughly every 30 min. It exists because `watchdog.sh` runs ON the prod host and is silent by construction when the host itself stops serving. Operational detail (what it probes, its known limits, the secrets it needs) belongs in deployment.md.
- **Author-plug-filter** (2026-05-04) — variant A (5 patterns in `boilerplate_filter.py` for standalone author plugs) + variant B (`_strip_plugs` / `_strip_plugs_in_blocks` in `news_bot.py`, called from `_fallback_publish` post-translation) for inline plugs. 10 platforms covered (instagram/twitter/x/tiktok/youtube/facebook/reddit/patreon/discord/linktree). Single commit `695b201`. See `work/completed/author-plug-filter/`.

**Near-term enhancements (Planned)**
- Cross-article linking (`runs[].href` → our own Telegraph URLs when already published)
- Production observability beyond admin pings — the **uptime** half shipped 2026-07-31 (see Delivered → External uptime watch), and per-tick failures are already reported by the [E034] publish recap ping (`admin_alerts.alert_publish_recap`, shipped 2026-07-15). What is still missing: any failure view OUTSIDE the admin chat (a persisted/aggregated digest an operator can read after the fact).
- Per-source tone calibration verification — `_build_user_message` already passes `source_name` to the LLM, and `ux-guidelines.md` already carries the per-source notes block (Autoevolution / Lamley / Mattel tone dials). What's not verified: whether the production LLM actually applies the right dial per article. Spot-check a sample of recent publishes per source against the prompt's per-source notes; if drift exists, tighten the prompt (e.g. by labelling the source-name field more explicitly: `source_brand_voice: "autoevolution_blog"`).

**Future ideas (Backlog)**
- Web dashboard for configuration and monitoring
- Extended translation options (DeepL, Yandex.Translate)
- Support for additional news sources beyond the current four
