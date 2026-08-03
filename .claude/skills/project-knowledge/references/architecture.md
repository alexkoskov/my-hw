# Architecture

## Purpose
Technical architecture overview for AI agents. Helps agents understand HOW the system is built.

---

## Tech Stack

**Frontend:** None (CLI script with no user interface)

**Backend:** Standalone Python script (no web framework). In prod it runs inside a **Docker container** on the Moscow VPS, egress routed through a non-RU VPN — see deployment.md for topology.

**Database:** SQLite (`news.db`; path via `DB_FILE`, prod = `/data/news.db` on a mounted volume)
- **Why:** Lightweight, file‑based, zero‑configuration; suits the simple duplicate‑tracking requirement.

**Runtime:** Python 3.8+
- **Why:** Wide library support, ease of scripting, and compatibility with required packages.

---

## Project Structure

Rebuilt from an actual repo listing 2026-08-03. One line per module — for what a module
does in detail, read its docstring; this tree only says where to look.

```
my-hw/
├── news_bot.py              # Entry point + orchestrator (~220 KB, the bulk of the project).
│                              main() (:4555) arms one daily 10:00 МСК tick (:4641); job() runs
│                              the crash-loop guard, fetch, the intake gates (relevance,
│                              checklist, promo [E035], content gate [E036]/[E037], cross-source
│                              dedup), staging, the plan-of-day ping and the publish loop.
│                              Also owns: SOURCES registry, NETLOC_TO_SOURCE / SOURCE_EMOJI /
│                              SOURCE_LABEL / SOURCE_HASHTAG_OVERRIDE (:2841-2906), secret
│                              redaction (:336-380), _strip_plugs (:1285), the inbound review
│                              listener, and the DORMANT Google-path transcreate_text (:2740).
├── llm_transcreation.py     # LLM dispatcher — selects ONE of the four engines below at import.
│                              THE startup-critical translator module: news_bot.py:77 imports it
│                              as `claude_transcreation`, so that alias no longer means the
│                              Anthropic engine. See § External Integrations.
├── _llm_common.py           # Provider-agnostic core shared by all four engines: system-prompt
│                              build (_build_system_prompt), JSON envelope, response parse +
│                              validation, `**bold**` marker round-trip, emoji safety net,
│                              paragraph truncation, block patching, shared exception classes.
├── claude_transcreation.py  # Engine: Anthropic SDK.
├── openrouter_transcreation.py # Engine: OpenRouter gateway — the engine production runs.
├── openai_transcreation.py  # Engine: OpenAI SDK.
├── gemini_transcreation.py  # Engine: Google GenAI SDK.
├── compute_publish_slots.py # Scheduling maths, pure, no external deps. `compute_fixed_slots`
│                              is the production scheduler (three fixed МСК times — see Data
│                              Flow step 5); the dynamic even-spread `compute_publish_slots`
│                              (:111) is DORMANT, retained only for its unit tests.
├── outage_state.py          # SQLite-backed outage state machine (llm-transcreation feature).
│                              5-state protocol (no_outage → ping_1_sent → ping_2_sent →
│                              google_fallback_active → recovery_pending) driving the
│                              operator-ping cadence (ping #1 now, #2 +1h, #3 +2h). Since
│                              the 2026-06-11 hold-and-wait change it no longer switches the
│                              bot to Google — outages HOLD posts; the google_fallback_active
│                              label/fallback_active flag are dormant. State
│                              persists in bot_state table; BEGIN IMMEDIATE for atomicity;
│                              PRAGMA busy_timeout=5000.
├── pending_articles_repo.py # DAO owning all SQLite tables (DDL + CRUD + transactional moves);
│                              init_schema() now also creates bot_state(key, value) idempotently.
├── admin_alerts.py          # Single catalogue of EVERY operator-facing message: `alert_*`
│                              builders + their `[E0XX]` codes + the review keyboards. (The
│                              old `news_bot.build_admin_ping` was deleted 2026-05-01; this
│                              module replaced it.)
├── model_extractor.py       # Cross-source dedup fingerprints: brand/model lexicon,
│                              SERIES_LEXICON with distinctive/broad tiers, pair keys,
│                              guarded Jaccard similarity. Pure, no I/O.
├── boilerplate_filter.py    # Paragraph-granularity UI/promo boilerplate drop (`is_boilerplate`,
│                              `filter_boilerplate`), called by each source parser before return.
├── backfill_fingerprints.py # One-shot/top-up backfill of `model_fingerprint` on historical
│                              rows. Operator-run, not part of the tick.
├── telegraph_publisher.py   # Telegra.ph API client + public preview_nodes() node-tree builder
├── autoevolution_source.py  # autoevolution.com — RSS + Cloudflare-bypass scrape (curl_cffi)
├── orangetrack_source.py    # orangetrackdiecast.com (WordPress.com) — RSS `content:encoded`
│                              first, bounded-streaming page GET as fallback
├── lamley_source.py         # lamleygroup.com HTML scrape
├── t_hunted_source.py       # t-hunted.blogspot.com (Blogger, Portuguese) scrape
├── mattel_news_source.py    # corporate.mattel.com — DISABLED 2026-05-24, see § External
│                              Integrations. Code + tests kept in case Mattel ever publishes
│                              Hot Wheels content again.
├── hw_review.py             # Operator-facing CLI (list/show/stage/skip/preview/publish/take/retry).
│                              Archived 2026-04-30 — code + tests preserved, dormant in production.
├── preview_renderer.py      # Local HTML preview builder (CSP + tag/URL allowlists).
│                              Archived 2026-04-30 — used only by the dormant hw_review flow.
├── feeds.json               # RSS URLs — 4 entries: 2 autoevolution + lamley + t-hunted.
│                              `load_feeds()` reads at most the first 5 (news_bot.py:297).
├── Dockerfile               # Production runtime image
├── docker-compose.yml       # Prod service + VPN route sidecar + ./data bind-mount — deployment.md
├── watchdog.sh              # ON-HOST stuck-bot watchdog (host cron → `docker exec`)
├── deploy.sh                # Historical scp bundle deploy — superseded, see below
├── requirements.txt / requirements-dev.txt
├── news.db                  # SQLite: processed_news + pending_articles + published_articles +
│                              failed_articles + bot_state (key/value, llm-transcreation outage)
├── .env / .env.example      # .env.example is the annotated env-var reference — read it first
├── .github/workflows/       # ci.yml (pytest on push/PR to main+dev); uptime.yml (external
│                              watchdog, every 30 min, live since 2026-07-31);
│                              deploy.yml + deploy_test.yml — both DISARMED (`if: false`)
├── tests/                   # pytest suite — 1626 tests across 47 files (2026-08-03; 1628 before
│                            # test_deploy_files_invariant.py was rewritten 6 shallow → 4 real tests)
├── scripts/ archive/ logs/  # Helper scripts, retired code, local run logs
├── work/
│   ├── completed/           # Finalized features (manual-review-workflow lands here)
│   └── archived/            # Deferred features
└── .claude/skills/project-knowledge/references/  # This doc tree
    └── ux-guidelines.md     # Editorial transcreation prompt — operator-side AND cron-side
                              # runtime dependency (see below). Loaded as the LLM system
                              # prompt by every engine via _llm_common._build_system_prompt.
```

**What ships to production.** The whole repo. Since the move to Docker the redeploy is
`git pull && docker compose up -d --build` on the prod host (deployment.md), so there is no
per-file bundle to keep in sync any more. `deploy.sh` and the two GitHub Actions deploy
workflows are historical: both workflows are hard-guarded `if: false`. Three facts from the
scp era still matter:

- **Archived 2026-04-30, dormant but kept on disk:** `hw_review.py`, `preview_renderer.py`.
  Meant for the operator's local Claude Code session; the auto-LLM path replaced them in
  production. Files + tests stay green so the path can be revived ad-hoc for one article.
- **`ux-guidelines.md` is a RUNTIME dependency, not just an editorial doc** (architectural
  shift closing AC28): every LLM engine loads it as its system prompt. If it is missing, every
  LLM call fails its prompt load and — since the 2026-06-11 hold-and-wait change — all posts
  are held until the file is restored. Decision 8 of the `llm-transcreation-and-distributed-publishing`
  tech-spec covers the layout: `_load_prompt` tries the subdir path first
  (`_llm_common._PROMPT_PATH_DEFAULT_PARTS`), then falls back to a flat filename next to the
  module — a leftover from the flattening `scp` bundle, harmless under Docker.
- **The three `FILES` arrays** in `deploy.sh` / `deploy.yml` / `deploy_test.yml` are pinned by
  `tests/test_deploy_files_invariant.py`. Keep them in step when adding a first-party module,
  so a revived scp deploy cannot ImportError.

`news.db` is cron-side data only and is never overwritten by a deploy — under Docker it lives
on the `./data` bind-mount.

---

## Key Dependencies

**Critical packages:**

- `feedparser` – Parses RSS feeds to extract article entries.
- `requests` – Generic HTTP for the Telegra.ph API and for every source
  except autoevolution (lamley, orangetrack, t-hunted).
- `curl_cffi` – Chrome-impersonating HTTP client for autoevolution
  (bypasses Cloudflare's `HTTP 403` on plain `requests`).
- `beautifulsoup4` – Extracts title, body, images, and inline links from HTML.
- `deep-translator` – Google Translate engine wrapped by `transcreate_text`
  in `news_bot.py`. **DORMANT since the 2026-06-11 hold-and-wait change** —
  no longer wired into the publish path (outages now hold posts instead of
  falling back to Google). Code + the HW glossary safety net + emoji prefix
  are kept for possible revival; bureaucratic regex post-processing was
  removed in the llm-transcreation feature.
- `anthropic>=0.45.0,<0.46.0` – Anthropic Python SDK, used by the `claude`
  engine. Pinned to lock the exception class hierarchy referenced in the
  per-article vs API-level classifier.
- `openai>=2.0.0,<3.0.0` – OpenAI SDK. Used by BOTH the `openai` engine and
  the `openrouter` engine (OpenRouter speaks the OpenAI wire protocol at a
  different `base_url`), so it is a hard runtime dependency in production.
- `google-genai>=1.70.0,<2.0.0` – Google GenAI SDK, used by the `gemini` engine.

  All four engine SDKs are installed unconditionally: the dispatcher imports
  only the selected engine (llm_transcreation.py `_select_engine`), but which
  one that is depends on env vars, so the image must be able to satisfy any
  of them. See § External Integrations.
- `pytz>=2024.1` – IANA timezone library. Required by `schedule==1.2.1` for
  `Job.at(time_str, tz=...)`; stdlib `zoneinfo.ZoneInfo` is rejected by the
  scheduler with `ScheduleValueError` (verified). Used for the daily
  10:00 МСК in-process tick registered in `news_bot.main()`.
- `python-telegram-bot==21.10` – Posts the channel card with
  `LinkPreviewOptions(url=telegraph_url, show_above_text=True,
  prefer_large_media=True)` for a full-width INSTANT VIEW preview,
  and delivers admin failure notifications.
- `schedule==1.2.1` – In-process job scheduling. One daily fixed-time tick
  (`every().day.at("10:00", tz=pytz.timezone("Europe/Moscow"))`). **There is no
  crontab for the bot** — the process is long-lived and schedules itself. The only
  real crontabs on the host are the DB backup and `watchdog.sh` (deployment.md).

---

## External Integrations

**LLM transcreation — one dispatcher, four interchangeable engines**

This is the single most important runtime dependency: no LLM, no posts.

- **Shape.** `llm_transcreation.py` is a dispatcher that picks exactly ONE engine
  at import time and re-exports its public API (`transcreate_via_claude`,
  `health_check`, `is_outage_error`, `is_per_article_error`). `news_bot.py:77`
  imports it as `import llm_transcreation as claude_transcreation` — the alias is
  backward compatibility for the bound name in the code and in tests, NOT a
  statement about which provider runs. **`llm_transcreation.py` is the
  startup-critical module**; the four `<engine>_transcreation.py` files are
  imported lazily, only the selected one.
- **Engines:** `claude` (Anthropic SDK), `openai`, `gemini` (Google GenAI),
  `openrouter` (multi-model gateway). Each is thin SDK glue: client lifecycle,
  exception classification, the API call. Everything provider-agnostic —
  prompt build, JSON envelope, response parsing/validation, safety nets — lives
  once in `_llm_common.py`; adding a fifth engine is documented in its docstring.
- **Selection order** (`_select_engine`, llm_transcreation.py:1-30): explicit
  `LLM_PROVIDER` wins; otherwise auto-select by API-key presence in the order
  `OPENAI_API_KEY` → `ANTHROPIC_API_KEY` → `GEMINI_API_KEY` → `OPENROUTER_API_KEY`;
  with no key at all it falls back to `claude` so the startup health check fails
  with a readable admin ping instead of an import error.
- **Production runs OpenRouter.** Prod pins `LLM_PROVIDER=openrouter` explicitly
  so key presence does not decide (OpenRouter is LAST in the auto-order — relying
  on auto-selection here would be fragile). Default model
  `openai/gpt-5.4-mini` (`_DEFAULT_MODEL`, openrouter_transcreation.py:80),
  override via `OPENROUTER_MODEL`. Per-engine model overrides: `ANTHROPIC_MODEL`
  (default `claude-haiku-4-5`), `OPENAI_MODEL`, `GEMINI_MODEL`. `.env.example`
  documents every knob including the `[E019]` low-balance alert.
- **Response cap:** `_DEFAULT_MAX_TOKENS` in each engine — **30000**, confirmed
  intentional by the operator 2026-08-03. (Decision 13 of the llm-transcreation
  tech-spec originally set 8000 as a prompt-injection cost bound; the docs
  carried 8000 until 2026-08-03. The constant in the code is authoritative —
  claude_transcreation.py's own module docstring still quotes the stale 8000.)
- **Failure model** (identical whichever engine is selected — exception classes
  are shared from `_llm_common`): per-article failures (refusal, malformed JSON,
  4xx) bump `attempt_count` and strike the article out after 3 →
  `failed_articles`; API-level outages (auth, rate-limit, network, 5xx) trigger
  the 2-ping protocol and HOLD the article in the queue (hold-and-wait,
  2026-06-11) — nothing published, retried next slot/day until the LLM recovers.
- **Auth:** one env var per provider (see above). All five secret shapes the bot
  can hold — Telegram bot token, OpenRouter, Anthropic, OpenAI, Gemini — are
  scrubbed from logs by `_TokenRedactingFilter` and from admin pings by the
  shared `_redact_text`; the patterns and their **order contract** (specific
  prefixes must match before the broad OpenAI `sk-…`) live at news_bot.py:336-380.
  Anyone adding a sixth provider must read that ordering note before adding a
  pattern.

**Google Translate (via deep-translator) — DORMANT since 2026-06-11**
- **Purpose:** Formerly the fallback translator (per-article + global outage).
  Unwired from the publish path by the hold-and-wait change — outages now
  hold posts, no machine translation is published. `transcreate_text` is
  kept in code for possible revival but is not called by `_fallback_publish`.
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
  (`TELEGRAM_CHANNEL_ID`); admin chat id in `TELEGRAM_ADMIN_ID`. All stored
  as environment variables. `TELEGRAM_ADMIN_ID` must be the NUMERIC chat id:
  it falls back to the placeholder `'@sunny413x'` (news_bot.py:106), which the
  Bot API cannot resolve for a private chat — the bot then starts and sends no
  admin pings at all — and the review-button listener fail-closes on any
  non-numeric value. `.env.example` spells out the failure mode.

**News sources.** Three are live, one is disabled. All are unauthenticated. The
registry that decides which fetchers run is `news_bot.SOURCES` (news_bot.py:3607);
the netloc→`source_name` map, the ping emoji/label vocabulary and the channel
hashtag override map are at news_bot.py:2841-2906. Adding a source means touching
all of them plus `feeds.json` — see patterns.md for the recipe.

**autoevolution.com (behind Cloudflare)** — primary RSS source + full article
scrape. Scraping requires `curl_cffi` with Chrome TLS impersonation; plain
`requests` returns HTTP 403. `source_name` = `autoevolution`.

**orangetrackdiecast.com** — Brad Bannach's solo Hot Wheels blog on WordPress.com.
Full body arrives in the RSS `<content:encoded>`; a bounded-streaming page GET is
the fallback. Fetched via its own entry in `SOURCES` (not via `feeds.json`).
`source_name` = `orangetrack`.

**lamleygroup.com** — enthusiast blog. Plain `requests` + BeautifulSoup on
`.entry-content`. `source_name` = `lamley`.

**t-hunted.blogspot.com** — Blogger-hosted, **Portuguese-language**. The Atom feed
carries title + ~150-char excerpt only, so the body is scraped from
`<div class="post-body">`. `source_name` = `t-hunted`; the channel hashtag is
`#thunted` via `SOURCE_HASHTAG_OVERRIDE` (the default netloc rule would emit
`#blogspot`, the platform rather than the source, and Telegram hashtags cannot
contain a dash). Being PT-language and enthusiast-run, this source generates most
of the intake-gate incidents narrated below.

**corporate.mattel.com — DISABLED 2026-05-24.** Commented out of `SOURCES`
(news_bot.py:3609) with the reason at news_bot.py:3599-3606: Mattel moved to
Astro/Netlify, article bodies now render client-side and are unreachable from any
JSON endpoint (the listing API returns handle/title/date/thumbnail only). Restoring
it would need a headless browser, and across its whole life the source yielded zero
Hot Wheels articles — so it was disabled without a replacement. `mattel_news_source.py`
(RSC flight-payload parser, written 2026-04 when Mattel was on the Next.js App
Router) and its tests are kept on disk, and `mattel` remains in `NETLOC_TO_SOURCE`
for historical rows. Do not debug "no Mattel posts" as a parser bug.

---

## Data Flow

The pipeline is the **cron prep + distributed-publish phase** (no operator, daily at 10:00 МСК) — auto-LLM transcreation → Telegra.ph → Telegram. The LLM is the sole translator — which engine, see § External Integrations. On an API-level LLM outage the article is held in the queue (hold-and-wait, 2026-06-11); per-article LLM failures strike out after 3.

A second loop — the **manual review loop** (`hw_review.py` CLI in the operator's Claude Code session) — is **archived as of 2026-04-30**: the auto path produces 100 % of channel posts in production. See § Manual review loop below.

Below, each stage says what it does, what it emits into the intake funnel, which `[E0XX]` alert it raises and where to read the rules. **The branch-by-branch rationale is NOT restated here** — it lives in `news_bot.py`'s own `#:` comments (they explain why each marker/branch exists, and they change whenever a false positive shows up) and in `work/content-gate-review/`.

### Cron prep + distributed-publish phase — `news_bot.job()` daily at 10:00 МСК

1. **Crash-loop guard.** Read `MAX(published_at)` from `published_articles`. If `now - last_published < MIN_INTERVAL_MINUTES (90)`, sleep until that gap elapses before continuing. Protects the channel from burst posting under Docker rapid-restart loops.
2. **Fetch** all sources via the `SOURCES` registry; each entry gets `source_name` via `_resolve_source_name(link)` → netloc → one of `autoevolution` / `orangetrack` / `lamley` / `t-hunted` / `mattel` (historical) / `other` on a miss (news_bot.py:2841). Each parser applies `boilerplate_filter` and the image policy before returning.
3. **Dedup + relevance.** Filter against `processed_news` AND `pending_articles` (no re-fetch of seen links). Also drop sibling-brand articles via `_is_hot_wheels_relevant` (news_bot.py:1530) — the channel is Hot-Wheels-only, and feeds cross-tag sibling diecast brands under "Hot Wheels". It is source-agnostic and label-driven: a six-step decision ladder over four lexicons, ending in "broad-diecast source with no HW signal → reject". The ladder and the lexicons are documented in its docstring; the rules for extending it are in patterns.md. After `fetch_full_article` runs, drop bare-checklist posts via `_is_text_only_checklist` (title contains whole-word "checklist" AND paragraph body < 500 chars — typical orangetrack list-of-cars-with-no-prose pattern).
   - **Promo/ad filter (2026-07-25, `[E035]`)** — `_is_promo_article(entry, article)`, run right after the checklist drop and BEFORE the dedup gate. Scans title + URL slug + the first 8 paragraphs for promo markers in three tiers (SELLER-voice / CTA-DIRECT / CTA-OFFER) and blocks on a combination, never on a single word; the marker lists and the exact thresholds are in news_bot.py with the reasoning inline. **The principle behind the thresholds:** an ad tells the reader to act, journalism does not — seller voice alone is not enough (interviews quote shop owners in the first person) and a CTA alone is not enough (retail news reports offers factually). On a hit: `funnel['dropped_promo']`, `[E035]` log + `admin_alerts.alert_promo_blocked` ping, `mark_processed` pin (E015 precedent — no daily re-fetch/re-alert), `continue`. The article never reaches `pending_articles`, so a dropped ad costs zero LLM tokens. Fail-open: a crash treats the article as not-promo rather than killing the tick. Prod incident that prompted it: t-hunted published a pure store ad («…na loja Universo Hot Wheels») and the bot translated + posted it.
   - **Content gate (2026-07-25, `[E036]` HOLD / `[E037]` DROP)** — three genres the operator does not want auto-published: poster/catalog/packaging posts, video reviews, event announcements. Two detectors, `_hold_for_review_reason` and `_is_rejected_genre`, both **subject-anchored**: they read the title + URL slug ONLY, never the body (slugs are generated from the title, so they add no independent false-positive surface but survive a truncated feed title). Marker lists, the video branches (`video_lead` / `video_np` / `video_signals`), the event two-signal rule and the per-marker rationale are all in news_bot.py — that is where they get amended when a false positive shows up, and `work/content-gate-review/` holds the two review rounds. What is policy rather than implementation:
     - **Branch → action is a lookup, held in one place.** The detectors report only WHICH rule fired; `_GENRE_BRANCH_ACTION` maps a branch to `'drop'` or `'hold'` and `job()` obeys it. Operator decision 2026-07-25 — «очевидные резать, спорные спрашивать»: `video_lead` (`Vídeo:` / `Watch:` + separator) and `event` (event name AND organisational word) **drop**; `video_np` and `video_signals` **hold**.
     - **Rationale — asymmetric cost of error.** A wrong HOLD costs the operator one button press; a wrong DROP is unrecoverable (the link is pinned in `processed_news` and never seen again). So only branches that need no judgement may drop. `video_np` / `video_signals` are *evidence, not proof* — both review rounds found genuine car reveals in exactly those shapes — so they ask instead of deciding. The same asymmetry is why a poster hold fires on ONE marker.
     - **A hold is staged, not dropped.** The row goes into `pending_articles` with `hold_reason` set, which hides it from `list_pending`/`count_pending` (see § Data Model). **No answer = it never publishes** — no timer, no auto-publish, no auto-drop. It is deliberately NOT pinned in `processed_news`; the intake `get_pending` filter is what stops it being re-staged daily.
     - **A hold is not a drop in the funnel.** Genre holds increment `held_for_review`, never `dropped_genre`, and are excluded from the «отсеяно» sum — otherwise the daily ping would report an article as discarded while it is waiting for the operator.
     - **The hold reason travels with the article** (`poster` vs `video`) into `alert_held_for_review`, which picks the matching «Что произошло» wording — a suspected video review and a poster photo-dump need visibly different judgement calls. Unknown reason → poster wording, so the block is never blank.
     - **Precedence: HOLD beats DROP.** The hold detector runs first and skips the genre check on a hit — the incident post was a poster post that also said «no vídeo abaixo» and had to reach the operator rather than be silently binned.
     - Both detectors are fail-open (Decision 12 / AC9, audit SEC-PROMO-1): a crash treats the article as ordinary news — it must neither kill the tick nor silently park articles. Prod incident: t-hunted published «As fotos do último poster da Hot Wheels» — four sentences, 12 images, an unembeddable video — and the bot translated + posted it.
   - **Cross-source dedup gate (`_check_cross_source_dedup`, cross-source-dedup + dedup-model-series)** — the last post-fetch gate before row assembly. `model_extractor.extract_fingerprint(article)` yields `{strict, brands, series, pairs}`: `strict` = `"brand model"` tokens from the brand lexicon, `series` = canonical Hot Wheels line/event/franchise names from `SERIES_LEXICON`, `pairs` = `"<model>|<series>|<tier>"` keys. Two rules run in order against ONE 30-day candidate fetch (pending + published):
     - **(1) Tiered pair rule.** A shared `|D` (distinctive) key **hard-blocks** any-source — `[E015]`, article dropped and pinned in `processed_news`, **irreversible**. A shared `|B` (broad) key **soft-flags** — `[E014]` with a per-link-pair 7-day alert rate-limit. Both verdicts are terminal.
     - **(2) Set-overlap backstop.** The legacy guarded Jaccard over `strict` (≥0.50 block, `[0.30,0.50)` flag), 7-day window, cross-source only, reached only when rule 1 passes.
     - Gated by `DEDUP_SERIES_ENABLED` (default ON) and fail-open: any crash → `[E016]` + the article publishes with `model_fingerprint=NULL`.
     - **A soft-flagged article is DEFERRED 24h** (`_DEDUP_DEFER_HOURS`, 2026-07-28): staged with a future `pending_articles.publish_after`, which hides it from `list_pending`/`count_pending` exactly like `hold_reason` — so it buys no slot it cannot fill — and it then publishes by itself when the stamp elapses. **Silence means PUBLISH here**; the delay exists only so the `[E014]` cancel button has a usable window. Before it, intake and the first slot both ran at 10:00: on 2026-07-28 the ping went out at 10:00:10, the article published at 10:00:17, and the operator's cancel at 10:14 got «Уже опубликовано, отменить нельзя». The defer is set regardless of the alert rate-limit — the rate-limit protects the operator's notifications, not the article. Contrast with the content gate's `hold_reason`, which never expires: «нет ответа = не публикуем» there, «нет ответа = публикуем» here.
     - **Tier polarity is fail-safe:** `|D` requires a lexicon-tagged-`distinctive` series AND a concrete model; anything untagged/unknown defaults to broad (`_SERIES_DEFAULT_TIER`), so a new franchise can never silently hard-block.
       - **Round-up rule for tagging a series (2026-07-28):** a series that can turn up in listicle / round-up coverage CANNOT be `distinctive`. A round-up names many castings, so any one of them can collide with a programme mentioned in passing — and `|D` is an irreversible hard block. **Super Treasure Hunt and Red Line Club were retagged `broad`** on 2026-07-28 after a false `[E015]` dropped a t-hunted RLC Audi Quattro post against an autoevolution «10 affordable cars» round-up that merely listed an Audi Sport and mentioned Red Line Club in passing (`audi sport|red line club|D` on both sides). Both are continuously-shipping Hot Wheels programmes, not one-off franchises. `distinctive` is for one-off franchises/events only — see the tier table and its incident note in model_extractor.py:138-172. Broad ⇒ soft flag ⇒ the operator decides, and nothing is dropped irreversibly.
     - **Theme-only keys (`"*|<series>|B"`, no concrete model) — franchises/events ONLY, since 2026-07-28.** A broad recurrent line (pop culture, car culture, boulevard, zamac, monster trucks, team transport) or a recurring release program (`_RECURRING_PROGRAMS`: super treasure hunt, red line club) yields NO key without a model. Prod incident: a t-hunted «lote da série Pop Culture» post (Lotus → brand-only token) and an autoevolution «Super Treasure Hunt … Is a Lincoln» article (Lincoln is outside the brand lexicon) both degraded to `*|pop culture|B` and false-flagged each other — and autoevolution's «pop culture» was ordinary prose, not a line name. Extraction is unchanged (`series` still lists both); only MATCHING narrowed. Side effect: an article can now reach the backstop with non-empty `series` but EMPTY `pairs`, which is a guaranteed no-op there (`similarity()` = 0.0 on empty `strict`).
4. **Insert** accepted entries into `pending_articles` via `pending_articles_repo.insert_pending` (JSON-serialised paragraphs/images/blocks, plus `hold_reason` when the content gate held the article). Staged rows from prior days remain in the queue as carry-over. On a held insert, `[E036]` is sent AFTER the row is committed (so the button token can never point at a missing row); the ping is best-effort but the hold is not — a send/mint failure leaves the article parked and visible in the daily «На утверждении» line.
5. **Compute schedule — THE production scheduler.** `compute_fixed_slots(count_pending(), now_msk)` → `(slots, carry_over)` (news_bot.py:4285). Operator pacing decision 2026-06-13: publish at most once per **fixed** daily time — **10:00 / 15:00 / 19:30 МСК** (`DAILY_PUBLISH_TIMES`, compute_publish_slots.py:49). So `posts_today = min(N, number of fixed times still eligible today)` and the hard ceiling is **3 posts/day** — the same three times the operator's «no deploy inside 10:00–20:00 МСК» rule is built on (deployment.md). `news_bot.MAX_DAILY_POSTS = 3` still exists but is now redundant for trimming: the cap falls out of the three fixed times. Surplus becomes `carry_over` and waits for the next tick.
   - A slot stays eligible for `grace_minutes` (default 5) after its wall-clock time, because the 10:00 cron tick reaches slot planning seconds-to-minutes after 10:00:00. **The grace must stay small** — a deploy restart well after a fixed time must NOT re-fire that slot, which would break the 3/day guarantee. This is the mechanical reason redeploys are forbidden inside the publishing window.
   - `count_pending()` counts PUBLISHABLE rows only — content-gate holds and 24h-deferred dedup soft-flags are excluded, so a parked article never buys the day a slot it can never fill.
   - **The dynamic even-spread `compute_publish_slots(N, now, window_start, window_end, min_interval_min)` is DORMANT** (compute_publish_slots.py:111) — superseded 2026-06-13, kept only so its unit tests stay green. Editing it changes nothing in production.
6. **Admin ping** (per tick, always fires — operator heartbeat) to `TELEGRAM_ADMIN_ID`. Busy day → `admin_alerts.alert_plan_of_day(inserted, queue_size, slots, carry_over)` (`[E008]` schedule); quiet day (`queue_size==0 and inserted==0`) → `alert_quiet_day()` (`[E009]` «Бот сработал, новых статей нет»). Additional backlog warning when `queue_size > 50` (AC20) — also held-free, since the queue cannot drain held rows on its own.
   - **Intake-funnel diagnostic (pipeline-diagnostic-watchdog, 2026-07-13):** step (b) accumulates a per-tick funnel of plain-int counters (`sources_fetched`/`sources_failed` → `new_count` → `dropped_no_article`/`checklist`/`promo`/`genre`/`dedup_block`, `dedup_degraded`, `held_for_review` → `staged`), emitted as one `[funnel]` log line and rendered into the tick ping. `[E009]` now shows WHERE intake collapsed («Где схлопнулось: …») instead of a flat line; `[E008]` gets a compact intake summary. Counts only (no untrusted title/link/error text), fail-safe — degrades to the legacy line on any error and can never break the tick. Scope = intake/staging; translation/post happen later in the publish loop (step 7), so the funnel shows those as `—`.
   - **Held backlog line (content-gate, 2026-07-25):** both `[E008]` and `[E009]` take a keyword-only `held_count` (`len(list_held())`) and render «На утверждении: N». Held rows are absent from «Всего в очереди» and from the slot computation, and `[E036]` has no timer or reminder by design — this line is the ONLY place a forgotten hold ever resurfaces, which is why it is on the quiet-day ping too (a held article can be the only thing in the DB). Distinct from the funnel's `held_for_review`, which counts THIS tick's holds («• придержано на утверждение: N») and is deliberately excluded from the «отсеяно» sum — a hold is a deferred decision, not a drop.
7. **Publish loop.** For each slot in `slots`:
   - Window-end guard: if `slot > 20:00 МСК`, break (excess becomes carry-over).
   - `time.sleep((slot - now).total_seconds())` until slot arrives.
   - Pop next row via `list_pending()` two-tier ordering: today's freshly-fetched batch first (in fetch order), then carry-over backlog drained oldest-first.
   - **Idempotency guard (publish-idempotency-fix, 2026-05-07):** at the very top of `_fallback_publish`, BEFORE any side effect, check `pending_articles_repo.get_published(link)`. If non-`None`, the pending row is a zombie (some prior `move_to_published` left an inconsistent state, or fetch loop re-staged a published link). Log INFO with `[idempotency-guard]` tag, send admin-ping `«⚠️ Пропущен дубль публикации»`, call `skip_pending(link)` (atomic `INSERT OR IGNORE → processed_news` + `DELETE → pending_articles`), return `True`. Slot loop treats as success — no `attempt_count` strike, no `move_to_failed`. If `skip_pending` itself raises (DB-level), log ERROR + send a second admin-ping `«⚠️ Не удалось снять зомби-строку»` and STILL return `True` (subscriber-visible duplicate prevention is the primary contract).
   - Call `_fallback_publish` via `_publish_with_retries` (LLM-only — no Google branch since 2026-06-11). The wrapper returns `'published' | 'held' | 'failed'`.
   - **Bold survives the LLM as literal `**…**` markers, and must never reach Telegra.ph.** Inline formatting is carried in each block's `runs` metadata (see § Data Model). Before the request, `_llm_common._encode_format_markers` (:128) rewrites the runs into `**bold**` inside the paragraph text so the model keeps the emphasis while translating; at patch time `_decode_format_markers` (:187) turns the markers back into RU runs, and `telegraph_publisher._decode_bold_markers` (:201) is the render-time backstop that also strips unbalanced markers (`_STRAY_MARKER_RE`). Both fixes are 2026-07-28; the invariant is in the commit title — *"decode `**bold**` markers at render — never publish them"*. Any change to the paragraph-patching path must preserve it, or raw `**` leaks into a published page. `_render_paragraph_with_runs`' `str.find` fallback now only handles runs the LLM did not preserve. Rendering rules: patterns.md.
   - **Service text is stripped at THREE granularities** — mixing them up is a documented recurring bug (commit 5bbc9d4, 2026-07-29, after three operator reports on three consecutive t-hunted articles): whole ARTICLE = `_is_promo_article` `[E035]` (step 3); whole PARAGRAPH = `boilerplate_filter`, `^`-anchored and length-bounded; single SENTENCE = `_strip_plugs` / `_strip_plugs_in_blocks` (news_bot.py:1285-1322), applied here to the RU title, subtitle, paragraphs and blocks — author social plugs, cross-promo CTAs and dangling page-layout pointers («в видео ниже», «на фото выше»). **Before adding a fourth `^`-anchored paragraph pattern, check whether the offending text is a sentence inside a paragraph** — that was the wrong instinct each of the three times. Pattern-writing rules: patterns.md.
   - `'held'` — `ClaudeOutageError` (API-level outage): `_fallback_publish` already HELD the article (nothing published) and advanced the operator-ping state machine. NOT retried (holding is desired; a retry would re-translate). Do NOT count a publish and do NOT strike — the row stays at the queue head and the next slot/day retries the LLM. Continue to next slot.
   - `'failed'` — per-article LLM failure (`ClaudeTranscreationError`) strikes IMMEDIATELY (deterministic, no retry); a transient publish-side error (Telegra.ph/Telegram/repo network timeout) is retried in-slot up to `PUBLISH_RETRY_ATTEMPTS`×`PUBLISH_RETRY_DELAY_SECONDS` (4×10 min, in-slot-retry 2026-06-17) so a one-off blip doesn't cost the slot until the next day. Either way, once `'failed'` the slot loop runs the standard 3-strike counter → `move_to_failed` (exactly one strike per slot, not per retry).
   - **Publish recap (publish-recap-diagnostic, 2026-07-15):** after the slot loop, `job()` tallies the outcomes (`published`/`held`/`failed`/`moved_to_failed` + a capped, de-duped list of `(link, sanitized reason)` for failures) and sends `admin_alerts.alert_publish_recap` → `[E034]` — 🟢 `опубликовано N/N` when all published, else 🟡 with held («Claude недоступна») + the failed reasons («снято после 3 промахов»). Surfaces `failed`-article reasons that were previously log-only (the intake funnel + this recap together cover the whole pipeline's "where + why"). Sent only when publishing was attempted (skips quiet ticks). Reasons double-scrubbed (`sanitize_error_message` + `_redact_text`), plain-text, fail-safe (runs after all publishing — cannot affect posts).

### Inbound review path — `_run_review_listener()` (prod-only, gated by `REVIEW_BUTTONS_ENABLED`)

**The project's first inbound Telegram path** (dedup-review-buttons feature, 2026-07). Until this feature the bot was strictly send-only; now the `[E014]` «Похож на дубль» admin ping carries two inline buttons — «🚫 Не публиковать» / «👍 Оставить» — and a background listener receives the operator's press. Since the content gate (2026-07-25) a **second** keyboard rides the same path: `[E036]` «На утверждение» with «✅ Опубликовать» / «🚫 Не публиковать» (see below).

- **Send side.** Only two alerts ever carry a keyboard: `[E014]` (dedup soft-flag) and `[E036]` (content-gate hold). Each mints `token = secrets.token_urlsafe(9)`, persists `review_token:<token> → <kind>|<link>` in `bot_state` (see § Data Model) and attaches the keyboard from `admin_alerts`. The alert's «Что сделать» text is derived from the SAME keyboard object that is attached, so it can never promise a button that was not rendered; with buttons off it says there is no action available on this instance — the archived `hw_review.py` CLI is not deployed and must never be advertised in an alert.
- **Listen side.** `main()` starts `_run_review_listener()` as a **daemon thread** beside the blocking `schedule`/publish loop; it opens its own `Bot` and long-polls `get_updates(offset, timeout=30, allowed_updates=['callback_query'])`. Each press goes through a PURE resolver (`resolve_dedup_callback` / `resolve_hold_callback`) that returns `(status, answer)`; the thread then edits the message (append the status line, drop the keyboard) and answers the callback. Outcomes: cancel → `skip_pending`; keep → nothing; approve → `clear_hold`; reject → `skip_pending`; already published → «Уже опубликовано, отменить нельзя» (race-honest — queue state at press time is the source of truth, there is no timer); row gone or stale token → «недоступна» / «устарела». A listener error can never kill the publish loop.
- **Two grammars, deliberately different prefixes** — `dd:<c|k>:<token>` for `[E014]`, `hd:<a|r>:<token>` for `[E036]`. A shared prefix would route an approve press into the dedup resolver, which would answer «устарела» and quietly lose the decision. Prefix separation alone is NOT sufficient: the prefix says which BUTTON was pressed, not which keyboard minted the TOKEN — so each resolver also verifies the stored token `kind` (audit SEC-CG-2; the two exploitable directions are written up in § Data Model under `review_token:`). Dispatch calls the two resolvers by name rather than through a lookup table: a table captures function objects at import time and would silently defeat `patch('news_bot.resolve_dedup_callback')` in tests.
- **Doing nothing needs no branch.** An unpressed hold simply stays out of `list_pending` forever. Both `[E036]` text variants say outright that no answer means the article is NEVER published — a two-button prompt otherwise reads as "it goes out unless I stop it".
- **Gate (fail-closed).** The thread starts only when `REVIEW_BUTTONS_ENABLED` is truthy AND `TELEGRAM_BOT_TOKEN` is non-empty AND `TELEGRAM_ADMIN_ID` is numeric (`_review_listener_gate_reason()`); anything else disables the feature with a startup warning naming the broken knob (a `@username` can never match the numeric `from_user.id`; an empty token would error-loop every poll). The SAME full gate — `_review_listener_enabled()`, not the bare flag (audit SEC-A8-1) — also gates keyboard rendering at the send sites, so an instance that cannot listen neither mints tokens nor shows buttons. With the gate closed a held article is STILL held (the safe direction). **Prod-only:** Telegram allows one `get_updates` consumer per token, so a second poller gets HTTP 409. See deployment.md § Feature rollout: dedup-review-buttons.
- **Publish loop slot selection is unchanged** — it re-reads `list_pending()` each slot, so a row cancelled before its slot simply never publishes. A cancel racing that row's own in-flight publish is covered by a two-sided guard (audit CA-1): `_fallback_publish` re-checks the pending row right before the Telegram teaser (row gone → abort, no channel post, no strike), and `move_to_published` WARN-logs + defensively dozapis the `published_articles` row from its explicit args in the residual teaser→move window, so a completed publish is never absent from the audit table.

### Manual review loop — `hw_review.py` (archived 2026-04-30)

> **Status:** dormant since 2026-04-30, when the operator declared the auto path production-ready. `hw_review.py` + `preview_renderer.py` + their tests are preserved verbatim and still work if revived for a single article: `list → show → stage → preview → publish` (plus `skip` / `take` / `retry`), with `ux-guidelines.md` loaded into the operator's Claude Code session as the translation prompt. `publish` is idempotent per Decision 9 (Telegra.ph page persisted BEFORE the Telegram send, so a retry never creates a second page). Full walkthrough: `hw_review.py`'s own command docstrings and `work/completed/manual-review-workflow/`. The CLI reads `list_pending`, so it does not show content-gate-held rows — accepted, it is not deployed.

### Channel post output (identical for all paths)

- Message body: `#{source_hashtag}` per `_source_hashtag` (news_bot.py:2806) — `#autoevolution`, `#lamleygroup`, `#orangetrackdiecast`, `#thunted` (override; the default TLD-strip would say `#blogspot`), `#mattel` historically. Single-line, byte-identical regardless of which translation engine produced the RU text (Decision 14). Holds even if the archived manual path is revived.
- `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` triggers Instant View card.
- Telegra.ph page: hero figure + italic subtitle with `💬 «…»` + bold lead + body blocks (paragraphs, images, iframes) + `Источник:` footer.
- The `↳ автоперевод` marker is no longer emitted. Since the 2026-06-11 hold-and-wait change there is no Google-fallback publish branch, so `_fallback_publish` always passes `auto_marker=False` and every published page is LLM-translated without a marker. The `auto_marker` kwarg on `telegraph_publisher.publish_article` is retained but always False from the cron path. See `patterns.md` § "Channel post format" for wiring.

---

## Data Model

**Database:** SQLite 3 (single file `news.db`). DDL owned by `pending_articles_repo.init_schema(conn)` (called from `news_bot.init_db()`); `processed_news` remains owned by `news_bot` itself.

**Column migrations (hardened 2026-07-25, audit SEC-CG-1).** SQLite has no `ADD COLUMN IF NOT EXISTS`. Every added column lives in `_COLUMN_MIGRATIONS` as `(table, column, DDL literal)` and goes through `_ensure_column`: read `SELECT name FROM pragma_table_info(?)` → `ALTER` only if genuinely missing → **re-read and confirm**. The only tolerated `sqlite3.OperationalError` is `duplicate column name` (another process won the race between the check and the ALTER); anything else is logged at ERROR and raised as `SchemaMigrationError`. The previous `try: ALTER / except OperationalError: pass` could not tell "already migrated" from "the ALTER failed" — a writer holding an IMMEDIATE lock past the busy timeout raises `database is locked`, also an `OperationalError`, so the migration reported success with the column absent. That mattered once `hold_reason` existed: `list_pending` / `count_pending` / `insert_pending` name it unconditionally, so a silently-absent column turns into `no such column` inside `job()` on every tick and restart. Failing loudly at startup is strictly better than a silent crash-loop. `pragma_table_info` is used as a table-valued function specifically so the table name binds through a `?` placeholder — no SQL string interpolation anywhere in the module (`TestSqlAudit`).

### Tables

**processed_news** — dedup
- `link` (TEXT PRIMARY KEY), `title`, `pub_date`, `processed_at`
- Written whenever a link is published (manual OR fallback) or skipped. Acts as the "seen" blacklist for future fetches.

**pending_articles** — WIP publish queue. No hard cap on the QUEUE: the fixed-slot scheduler caps PUBLISHES at 3/day (Data Flow step 5) and everything else becomes carry-over; an admin warning fires at `len(pending) > 50`.
- `link` PRIMARY KEY, `source_name`, `feed_url`, `title`, `subtitle`
- EN content: `paragraphs`, `images`, `blocks` (JSON, `ensure_ascii=False`)
- RU content (NULL until staged): `ru_title`, `ru_subtitle`, `ru_paragraphs`, `ru_blocks`
- Publish state: `telegraph_url`, `telegraph_path`, `preview_html_path`
- Bookkeeping: `fetched_at`, `notified_at`, `attempt_count`, `last_error`, `pub_date`
- **HELD state** (`hold_reason` TEXT NULL, content-gate 2026-07-25): NULL = publishable; a non-NULL value is the human-readable matched-marker list (e.g. `poster, url:poster`) that parked the row awaiting the operator's «✅ Опубликовать». Added by the shared column-migration path in `init_schema` — nullable with no default, so it is safe on the live prod DB and every pre-migration row reads back as publishable. **The exclusion is enforced in SQL, not in application logic:** `list_pending`, `count_pending`, `list_pending_stale`, `list_pending_for_eviction` and `list_notified_overdue` all carry `WHERE hold_reason IS NULL`. Since the slot loop's row source IS `list_pending`, that predicate is the whole guarantee behind the operator's rule «нет ответа = не публикуем» — there is no timer anywhere in the path. `get_pending` deliberately does NOT filter (by-PK accessor: the b2 intake filter must keep seeing held rows or the article would be re-staged and re-alerted daily, and both button resolvers look rows up by link). `list_held()` is the exact complement; `clear_hold(link)` releases a hold and returns `True` only if a row was actually held, so a double press cannot report a second approval. Approving is not publishing — `clear_hold` touches neither `processed_news` nor `published_articles`. Rejecting reuses `skip_pending` (DELETE + `processed_news` pin). The archived `hw_review list` CLI does not show held rows (it reads `list_pending`) — accepted, the CLI is not deployed.
- **Block types** (in `blocks` / `ru_blocks` JSON): `paragraph`, `lead`, `heading` (with `level: 3` or `4`), `image`, `video`, and `list_item` (added 2026-05-08 by orangetrack-rendering-fixes — children of `<ul>`/`<ol>` from orangetrack content). Text-bearing types (`paragraph`/`lead`/`heading`/`list_item`) all carry an optional `runs` field with inline metadata: `[{text, [href], [formats]}]` where `formats` is a list of `bold`/`italic`/`underline`/`strikethrough` markers. `_PATCHED_TEXT_BLOCK_TYPES` in `_llm_common.py` lists the 4 patchable types — `_patch_text_with_ru_paragraphs` rewrites their `text` from LLM output and preserves `type`, `runs`, `level`. Since 2026-07-28 `runs` bold survives translation as a literal `**bold**` marker in the text; the encode/decode round-trip and the "markers must never reach Telegra.ph" invariant are in Data Flow step 7.

**published_articles** — audit of real publishes
- `link` PK, `title`, `telegraph_url`, `telegraph_path`, `via_review` (1=manual, 0=auto-LLM), `published_at`. Since the manual path was archived on 2026-04-30 every new row carries `via_review=0`; existing `via_review=1` rows in `published_articles` are historical (manual-review-workflow era).
- **Insert is idempotent** (publish-idempotency-fix, 2026-05-07): `move_to_published` step 1 uses `INSERT OR IGNORE` so a second move with the same `link` is a no-op rather than `IntegrityError`. Original values (telegraph_url, via_review, published_at) are preserved from the first move. Step 2 (`processed_news`) and step 3 (`DELETE pending_articles`) execute unconditionally.
- **Post-commit defensive verification** (2026-05-08, after a prod incident where `published_articles` had rows but `processed_news` did not): `move_to_published` re-queries `processed_news` after the main commit. If the entry is missing, dozapis with `INSERT OR IGNORE` and emits a WARNING log. Self-healing — closes the zombie-row recurrence loop where `is_processed(link)` returns False on next fetch and re-stages an already-published link.

**failed_articles** — dead letter after 3 failed publish attempts
- `link` PK, `title`, `last_error`, `attempt_count`, `failed_at`

**bot_state** — small key/value store for cross-tick state (added by llm-transcreation feature)
- `key` (TEXT PRIMARY KEY), `value` (TEXT)
- Active keys (all values stored as ISO-8601 strings or `'0'`/`'1'`/`'2'` for counters/flags):
  - `outage_started_at` — ISO timestamp when first Claude outage error fired. NULL = no active outage.
  - `last_ping_sent_at` — ISO timestamp of the last admin ping (#1, #2, or recovery).
  - `ping_count` — `'1'` after ping #1, `'2'` after ping #2, `'3'` after the still-down (2 h) ping.
  - `fallback_active` — legacy flag (`'1'` once 2 h grace elapsed, else NULL). DORMANT since the 2026-06-11 hold-and-wait change — still written by the state machine but no longer read by the publish path (posts are held, not routed to Google).
  - `last_health_check_at` — ISO timestamp of the most recent Claude probe attempt during recovery_pending state. Rate-limits probes.
  - `review_token:<token>` — value = `<kind>|<link>`; `<token> = secrets.token_urlsafe(9)`. Maps a review keyboard's callback_data back to an article (the URL itself would overflow Telegram's 64-byte callback_data limit). Lifecycle: written at the send, read on button press, deleted after a terminal outcome. Stale tokens (bot restarted, row already gone, double press) are harmless — the handler resolves them to «устарела/недоступна»; no janitor needed. Accessors: `put_review_token(token, link, kind=…)` / `get_review_token(token) → (kind, link)` / `get_review_token_link` / `delete_review_token`.
    - **`kind` scoping** (`dedup` = `[E014]` `dd:<c|k>:`, `hold` = `[E036]` `hd:<a|r>:`; audit SEC-CG-2). The store is one flat namespace and the listener dispatches by action word, so without a kind a token minted by one keyboard could be redeemed by the other resolver. Both directions were reproducible: a dedup token redeemed as `hold`/`reject` silently `skip_pending`s a NON-held article; a hold token redeemed as `dedup`/`keep` consumes the token with no state change, leaving the held article **permanently orphaned** (still frozen, no live button, no re-mint path). Each resolver now checks the kind and answers «⚠️ Кнопка устарела` on a mismatch **without consuming the token or touching state**, so the legitimate button still works afterwards. The kind lives in the VALUE, not the key, so the change needs no migration and no janitor: pre-change values are a bare link and read back as `dedup` — which is what they are, since the hold keyboard did not exist then. Parsing splits on the FIRST separator and only for a known kind, so a link containing `|` round-trips intact.
    - **Known limitation:** if the operator deletes the `[E036]` message in Telegram, the token and the held row both survive but there is no button left to press and no re-mint path. Reminders/timeouts were explicitly rejected by the operator, so nothing is added; the daily «На утверждении: N» line is the mitigation, and releasing such a row needs a DB edit.
- DDL: `CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT);`. Owned by `pending_articles_repo.init_schema()`. `outage_state.py` provides typed accessors and state-machine helpers (`record_outage_event`, `record_recovery_event`) wrapped in `BEGIN IMMEDIATE` for atomicity. A 5 s busy-timeout absorbs typical contention — `outage_state` executes `PRAGMA busy_timeout=5000`, while `pending_articles_repo._connect()` pins the equivalent connect-time parameter `sqlite3.connect(..., timeout=5.0)` (same 5 s contention absorption, different mechanism — chosen deliberately, see its docstring). Since dedup-review-buttons it also serialises the second in-process writer, the review-listener thread, against the publish loop.

### Transactions owned by repo

`pending → published` (on successful publish), `pending → failed` (on 3rd strike), `failed → pending` (retry), `pending → skipped` (operator skip). Each transition is a single SQLite transaction; `processed_news` written as part of published/skipped moves.

### Sensitive Data

**PII fields:** No PII is stored in the database.

**Secrets:** The Telegram bot token (`TELEGRAM_BOT_TOKEN`), channel ID
(`TELEGRAM_CHANNEL_ID`), admin chat ID (`TELEGRAM_ADMIN_ID`), the
Telegra.ph access token (`TELEGRAPH_ACCESS_TOKEN`, auto-provisioned on
first run) and the LLM provider keys (`OPENROUTER_API_KEY` in prod;
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` for the other
engines) are stored in `.env` — never committed. The `.env` file is
git-ignored. None of these are persisted to the database. Every one of those
key shapes is scrubbed from logs and from admin pings — patterns and their
match-ordering contract at news_bot.py:336-380 (§ External Integrations).

---

## Planned Enhancements

**Cross-article linking** (Phase 2 of blocks pipeline)
- `block["runs"]` already carries external `<a href>` metadata. Future pass maps them to our own Telegra.ph URLs when the linked target is already published.

**Observability**
- ~~uptime checks~~ **shipped 2026-07-31** — `.github/workflows/uptime.yml` probes the host and
  the publish outcome every 30 min from outside the server. Still open: a failure digest and
  maybe a read-only dashboard.
