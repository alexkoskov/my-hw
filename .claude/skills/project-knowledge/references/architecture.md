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
│                              google_fallback_active → recovery_pending) driving the
│                              operator-ping cadence (ping #1 now, #2 +1h, #3 +2h). Since
│                              the 2026-06-11 hold-and-wait change it no longer switches the
│                              bot to Google — outages HOLD posts; the google_fallback_active
│                              label/fallback_active flag are dormant. State
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

  All four are listed as a single "cron-side files added by llm-transcreation feature" checklist for operator + future devs: deploy bundle MUST contain all four after deploy, otherwise the bot crashes on startup (missing `*.py`) or, with `ux-guidelines.md` missing, every LLM call fails its prompt load and (since the 2026-06-11 hold-and-wait change) all posts are held until the file is restored.
- **Shared local + cron**: `requirements.txt`, `feeds.json`, `.env.example`, `news.db` (SQLite — cron-only data, never overwritten on deploy).

---

## Key Dependencies

**Critical packages:**

- `feedparser` – Parses RSS feeds to extract article entries.
- `requests` – Generic HTTP for Telegra.ph API, Mattel, Lamley.
- `curl_cffi` – Chrome-impersonating HTTP client for autoevolution
  (bypasses Cloudflare's `HTTP 403` on plain `requests`).
- `beautifulsoup4` – Extracts title, body, images, and inline links from HTML.
- `deep-translator` – Google Translate engine wrapped by `transcreate_text`
  in `news_bot.py`. **DORMANT since the 2026-06-11 hold-and-wait change** —
  no longer wired into the publish path (outages now hold posts instead of
  falling back to Google). Code + the HW glossary safety net + emoji prefix
  are kept for possible revival; bureaucratic regex post-processing was
  removed in the llm-transcreation feature.
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
  blocks?}`. Per-article failures (refusal, malformed JSON, 4xx) bump
  `attempt_count` and strike the article out after 3 (→ `failed_articles`);
  API-level outages (auth, rate-limit, network, 5xx) trigger the 2-ping
  protocol and HOLD the article in the queue (hold-and-wait, 2026-06-11) —
  nothing published, retried next slot/day until the LLM recovers.
- **Auth method:** `ANTHROPIC_API_KEY` env var (obtained from
  https://console.anthropic.com → API Keys → Create Key, format
  `sk-ant-api03-…`). The key is redacted from logs by
  `_TokenRedactingFilter` (pattern `sk-ant-[A-Za-z0-9_=.-]{16,}`) and
  from admin Telegram pings by the shared `_redact_text` helper.
- **Default model:** `claude-haiku-4-5` (override via `ANTHROPIC_MODEL`).
  Cost ≈ $3/month at 10 articles/day. Sonnet 4.6 ≈ $15/month for higher
  quality.

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

The pipeline is the **cron prep + distributed-publish phase** (no operator, daily at 10:00 МСК) — auto-LLM transcreation → Telegra.ph → Telegram. Production uses OpenRouter (`openai/gpt-5.4-mini`) as the sole translator. On an API-level LLM outage the article is held in the queue (hold-and-wait, 2026-06-11); per-article LLM failures strike out after 3.

A second loop — the **manual review loop** (`hw_review.py` CLI in operator's Claude Code session) — is documented below for completeness but **archived as of 2026-04-30**: the auto path produces 100 % of channel posts in production. The manual loop's code is preserved (740 tests stay green) so it can be revived ad-hoc for one-off articles.

### Cron prep + distributed-publish phase — `news_bot.job()` daily at 10:00 МСК

1. **Crash-loop guard.** Read `MAX(published_at)` from `published_articles`. If `now - last_published < MIN_INTERVAL_MINUTES (90)`, sleep until that gap elapses before continuing. Protects the channel from burst posting under systemd/Docker rapid-restart loops.
2. **Fetch** all sources via `SOURCES` registry; each entry gets `source_name` via `_resolve_source_name(link)` → netloc → `autoevolution` / `mattel` / `lamley` / `other`. Boilerplate filter, image policy, dedup unchanged.
3. **Dedup + relevance.** Filter against `processed_news` AND `pending_articles` (no re-fetch of seen links). Also drop sibling-brand articles via `_is_hot_wheels_relevant` — autoevolution cross-tags Matchbox / Mega Bloks under "Hot Wheels", the channel is HW-only. After `fetch_full_article` runs, drop bare-checklist posts via `_is_text_only_checklist` (title contains whole-word "checklist" AND paragraph body < 500 chars — typical orangetrack list-of-cars-with-no-prose pattern).
   - **Promo/ad filter (2026-07-25, `[E035]`):** immediately after the checklist drop and BEFORE the dedup gate, `_is_promo_article(entry, article)` scans title + URL slug + first 8 paragraphs (each capped at 2000 chars, accent-stripped and word-bounded) for promo markers and returns the matched list. Three marker tiers: SELLER-voice (`nossa loja` / `em nossa loja` / `our store` / `our shop`), CTA-DIRECT (reader-addressed imperatives: `compre já`, `garanta o seu`, `não perca`, `shop now`, `buy now`, `use code`, …), CTA-OFFER (offer nouns: `cupom`, `frete grátis`, `promoção`, `coupon code`, `discount code`, `free shipping`, …). **Block iff** SELLER-voice AND (≥1 CTA-DIRECT OR ≥2 CTA of any tier), **or** ≥3 distinct CTA markers. WEAK commerce words (`loja`, `store`, `in stock`, `discount`, slug tokens `url:loja`…) never affect the verdict — they are reported in the ping for the operator, with hits inside a matched marker's span suppressed. Rationale: an ad tells the reader to act, journalism does not. Seller voice alone is not enough because interviews and community posts quote owners and fans in the first person («Our store has always focused on…», «our store finally got it back in stock»); a CTA alone is not enough because retail news reports offers factually («the revamped shop now offers free shipping»). Both thresholds sit one notch above the intuitive ones for exactly those two cases. On a hit: `funnel['dropped_promo']`, `[E035]` log line with the markers, `mark_processed` pin (E015 precedent — no daily re-fetch/re-alert), `admin_alerts.alert_promo_blocked` ping, `continue`. The article never reaches `pending_articles`, so a dropped ad costs zero LLM tokens. Wrapped fail-open like the dedup gate: a filter crash logs and treats the article as not-promo rather than killing the tick. Prod incident: t-hunted published a pure store ad («…na loja Universo Hot Wheels») and the bot translated + posted it.
   - **Content gate (2026-07-25, `[E036]` / `[E037]`):** immediately after the promo filter and BEFORE the dedup gate. Three post genres the operator does not want published automatically, split across two verdicts. Detection is **subject-anchored**: both detectors scan the title + the URL slug ONLY (slugs on our sources are generated from the title, so they add no independent false-positive surface but survive a truncated feed title); the body is never scanned. Matching reuses the promo filter's `_promo_fold` (accent-strip, lowercase, word-bounded) and `_promo_scan_input` (non-str → `''`, 2000-char cap), so `poster` never hits `posterior` and a pathological title cannot stall intake.
     - **HOLD** — `_hold_for_review_reason(entry, article)` returns matched markers for a poster / catalog / packaging post (`poster`, `cartaz`, `catálogo`/`catalog`/`catalogue`, `embalagem`/`packaging`, `cartela`/`blister`, `cardback`/`card art`/`box art`, + plurals). **One marker holds** — deliberately strict: a false hold costs one button press, a false publish is irreversible. The article IS staged, but with `pending_articles.hold_reason` set, which makes it invisible to `list_pending`/`count_pending`. `funnel['held_for_review']`, `[E036]` log + ping with the approve/reject keyboard. **No answer = it never publishes** — no timer, no auto-publish, no auto-drop. Not pinned in `processed_news` (the row is still live); the b2 `get_pending` filter is what stops it being re-staged daily.
     - **DROP** — `_is_rejected_genre(entry, article)` returns `(genre, markers)` for a video review or an event announcement. Wired exactly like the promo drop: `funnel['dropped_genre']`, `[E037]` log + ping, `mark_processed` pin (E015 precedent), `continue`.
     - **Video detection requires TWO signals** (review round 1, F1/F2 — the single-marker version dropped genuine car reveals that merely used the word: "Mattel drops video revealing the 2027 Corvette Z06", "Vídeo revela o novo Porsche 911 GT3 RS", "Unboxing surprises us: … Supra revealed"; a DROP is permanent). Three branches, each reported by name in `GenreVerdict.branch`:
       - **`video_lead` (A1)** — the genre word HEADS the title, immediately followed by a separator (`Vídeo: …`, `Watch: …`). Unambiguous headline convention, unconditional.
       - **`video_np` (A2)** — the genre word heads a NOUN PHRASE (`Unboxing da caixa J …`, `Assista ao …`), i.e. it is followed by a determiner/preposition from `_GENRE_VIDEO_NP_HEADS`, **and no finite verb from `_GENRE_HEADLINE_VERBS` appears in the title**. The verb condition is review round 2 (R1): "Video **of the** 2027 Corvette Z06 reveal **leaked** online" parses exactly like the required-drop "Unboxing **da** caixa J de 2026" — genre word + genitive + noun phrase — so narrowing the NP-head list to bare determiners would have broken the required case without fixing the template. A label has no predicate; a news clause does. (PT `é` is deliberately absent from the verb list — it accent-folds to `e`, "and".)
       - **`video_signals` (B)** — a genre word (`vídeo`/`video`, `unboxing`, `assista`, `youtube`) plus a review co-marker (`review`, `análise`, `assortment`, `full case`, `abrimos`, `we open`, …) or a second distinct genre word. Round 2 (R2) removed `hands-on` and `first impressions`: both are PREVIEW-journalism vocabulary ("First hands-on video of the 2027 Corvette Z06 STH"), and lexical proximity to "video" is not proof of genre.
       `watch` counts ONLY via the `video_lead` separator form. It is an imperative VERB rather than a noun heading a labelled headline, so as an anywhere-marker it would eat "watch out for these five Treasure Hunts" and via the noun-phrase branch it would eat every "Watch {this|these|our|my|all|every} …" sentence. The two lead regexes are therefore built from DIFFERENT marker sets — `_GENRE_VIDEO_LEAD_MARKERS` for the separator form, `_GENRE_VIDEO_MARKERS` for the noun-phrase form — so this is enforced by construction, not by a word list. Reveal language (`revealing`/`revela`/`first look`) is deliberately NOT a negative signal: it cannot apply to branch (A1) — the operator's required case `Watch: first look at …` must still drop — and on (B) it would punch a hole through `Video review: … reveals …`, a genuine video review.
     - **Branch → action is policy, held in one place.** The detector only reports WHICH rule fired; `_GENRE_BRANCH_ACTION` maps each branch to `'drop'` or `'hold'`, and `job()` looks the action up there. Today every branch drops. If the operator decides the softer video branches should instead go through the poster HOLD path (a wrong hold costs one tap; a wrong drop is irreversible), re-pointing is `'video_np': 'hold', 'video_signals': 'hold'` — `video_lead` stays a drop either way. The hold route is not speculative: an integration test patches the map and verifies the full path (staged with `hold_reason`, out of the publishable queue, one `[E036]` with buttons, no `[E037]`, link NOT pinned).
     - **Event detection requires TWO signals** — an event NAME (`convention`/`convenção`, `expo`, `feira`, `encontro`, `meetup`, `nationals`, `swap meet`, `legends tour`) **AND** an organizational word (`datas`, `ingressos`, `inscrições`, `programação`, `credenciamento`, `acontece`; `dates`, `tickets`, `registration`, `will be held`, `schedule`, `venue`). This is the critical false-positive guard: a convention-exclusive CAR REVEAL ("Hot Wheels Convention 2026 exclusive Datsun revealed", "exclusivo da convenção") is legitimate model news, and a convention name says WHERE a casting was announced, not what the post is about. Symmetrically, org words alone are ordinary release language ("2026 mainline release dates").
     - **Precedence: HOLD beats DROP.** `job()` evaluates the hold detector first and skips the genre check on a hit — the incident post is a poster post that also says «no vídeo abaixo» and must reach the operator rather than be silently binned.
     - Both detectors are wrapped fail-open like the promo filter and the dedup gate (Decision 12 / AC9, audit SEC-PROMO-1): a crash logs and treats the article as ordinary news — it must neither kill the tick nor silently park articles. Prod incident: t-hunted published «As fotos do último poster da Hot Wheels» — four sentences, 12 images, an unembeddable video — and the bot translated + posted it.
4. **Insert** accepted entries into `pending_articles` via `pending_articles_repo.insert_pending` (JSON-serialised paragraphs/images/blocks, plus `hold_reason` when the content gate held the article). Staged rows from prior days remain in the queue as carry-over. On a held insert, `[E036]` is sent AFTER the row is committed (so the button token can never point at a missing row); the ping is best-effort but the hold is not — a send/mint failure leaves the article parked and visible in the daily «На утверждении» line.
5. **Compute schedule.** `compute_publish_slots(N=count_pending(), now, window_start=10:00 МСК, window_end=20:00 МСК, min_interval_min=90)` → `(slots, carry_over)`. `posts_today ≈ min(N, 7)`. `count_pending()` counts PUBLISHABLE rows only — content-gate holds are excluded, so a parked article never buys the day a slot it can never fill.
6. **Admin ping** (per tick, always fires — operator heartbeat) to `TELEGRAM_ADMIN_ID`. Busy day → `admin_alerts.alert_plan_of_day(inserted, queue_size, slots, carry_over)` (`[E008]` schedule); quiet day (`queue_size==0 and inserted==0`) → `alert_quiet_day()` (`[E009]` «Бот сработал, новых статей нет»). Additional backlog warning when `queue_size > 50` (AC20) — also held-free, since the queue cannot drain held rows on its own.
   - **Intake-funnel diagnostic (pipeline-diagnostic-watchdog, 2026-07-13):** step (b) accumulates a per-tick funnel of plain-int counters (`sources_fetched`/`sources_failed` → `new_count` → `dropped_no_article`/`checklist`/`promo`/`genre`/`dedup_block`, `dedup_degraded`, `held_for_review` → `staged`), emitted as one `[funnel]` log line and rendered into the tick ping. `[E009]` now shows WHERE intake collapsed («Где схлопнулось: …») instead of a flat line; `[E008]` gets a compact intake summary. Counts only (no untrusted title/link/error text), fail-safe — degrades to the legacy line on any error and can never break the tick. Scope = intake/staging; translation/post happen later in the publish loop (step 7), so the funnel shows those as `—`.
   - **Held backlog line (content-gate, 2026-07-25):** both `[E008]` and `[E009]` take a keyword-only `held_count` (`len(list_held())`) and render «На утверждении: N». Held rows are absent from «Всего в очереди» and from the slot computation, and `[E036]` has no timer or reminder by design — this line is the ONLY place a forgotten hold ever resurfaces, which is why it is on the quiet-day ping too (a held article can be the only thing in the DB). Distinct from the funnel's `held_for_review`, which counts THIS tick's holds («• придержано на утверждение: N») and is deliberately excluded from the «отсеяно» sum — a hold is a deferred decision, not a drop.
7. **Publish loop.** For each slot in `slots`:
   - Window-end guard: if `slot > 20:00 МСК`, break (excess becomes carry-over).
   - `time.sleep((slot - now).total_seconds())` until slot arrives.
   - Pop next row via `list_pending()` two-tier ordering: today's freshly-fetched batch first (in fetch order), then carry-over backlog drained oldest-first.
   - **Idempotency guard (publish-idempotency-fix, 2026-05-07):** at the very top of `_fallback_publish`, BEFORE any side effect, check `pending_articles_repo.get_published(link)`. If non-`None`, the pending row is a zombie (some prior `move_to_published` left an inconsistent state, or fetch loop re-staged a published link). Log INFO with `[idempotency-guard]` tag, send admin-ping `«⚠️ Пропущен дубль публикации»`, call `skip_pending(link)` (atomic `INSERT OR IGNORE → processed_news` + `DELETE → pending_articles`), return `True`. Slot loop treats as success — no `attempt_count` strike, no `move_to_failed`. If `skip_pending` itself raises (DB-level), log ERROR + send a second admin-ping `«⚠️ Не удалось снять зомби-строку»` and STILL return `True` (subscriber-visible duplicate prevention is the primary contract).
   - Call `_fallback_publish` via `_publish_with_retries` (LLM-only — no Google branch since 2026-06-11). The wrapper returns `'published' | 'held' | 'failed'`.
   - `'held'` — `ClaudeOutageError` (API-level outage): `_fallback_publish` already HELD the article (nothing published) and advanced the operator-ping state machine. NOT retried (holding is desired; a retry would re-translate). Do NOT count a publish and do NOT strike — the row stays at the queue head and the next slot/day retries the LLM. Continue to next slot.
   - `'failed'` — per-article LLM failure (`ClaudeTranscreationError`) strikes IMMEDIATELY (deterministic, no retry); a transient publish-side error (Telegra.ph/Telegram/repo network timeout) is retried in-slot up to `PUBLISH_RETRY_ATTEMPTS`×`PUBLISH_RETRY_DELAY_SECONDS` (4×10 min, in-slot-retry 2026-06-17) so a one-off blip doesn't cost the slot until the next day. Either way, once `'failed'` the slot loop runs the standard 3-strike counter → `move_to_failed` (exactly one strike per slot, not per retry).
   - **Publish recap (publish-recap-diagnostic, 2026-07-15):** after the slot loop, `job()` tallies the outcomes (`published`/`held`/`failed`/`moved_to_failed` + a capped, de-duped list of `(link, sanitized reason)` for failures) and sends `admin_alerts.alert_publish_recap` → `[E034]` — 🟢 `опубликовано N/N` when all published, else 🟡 with held («Claude недоступна») + the failed reasons («снято после 3 промахов»). Surfaces `failed`-article reasons that were previously log-only (the intake funnel + this recap together cover the whole pipeline's "where + why"). Sent only when publishing was attempted (skips quiet ticks). Reasons double-scrubbed (`sanitize_error_message` + `_redact_text`), plain-text, fail-safe (runs after all publishing — cannot affect posts).

### Inbound review path — `_run_review_listener()` (prod-only, gated by `REVIEW_BUTTONS_ENABLED`)

**The project's first inbound Telegram path** (dedup-review-buttons feature, 2026-07). Until this feature the bot was strictly send-only; now the `[E014]` «Похож на дубль» admin ping carries two inline buttons — «🚫 Не публиковать» / «👍 Оставить» — and a background listener receives the operator's press. Since the content gate (2026-07-25) a **second** keyboard rides the same path: `[E036]` «На утверждение» with «✅ Опубликовать» / «🚫 Не публиковать» (see below).

- **Send side.** In `job()`'s soft-flag branch (and ONLY there — no other alert carries buttons), when the flag is on: mint `token = secrets.token_urlsafe(9)`, persist `review_token:<token> → link` in `bot_state`, attach `admin_alerts.build_dedup_review_keyboard(token)` to the E014 `send_admin_notification` call. Flag off → no token, no buttons (pre-feature behavior). The same send passes `buttons_enabled=kb is not None` into `alert_cross_source_dupe`, so the alert's «Что сделать» block matches what is actually rendered: buttons on → «нажми кнопку под этим сообщением…»; buttons off → «ничего — убрать её на этом инстансе нечем» (there is genuinely no operator action; the archived `hw_review.py` CLI is not deployed and must never be advertised in an alert).
- **Listen side.** `main()` starts `_run_review_listener()` as a **daemon thread** next to the blocking `schedule`/publish loop. The thread opens its own `Bot` and long-polls `get_updates(offset, timeout=30, allowed_updates=['callback_query'])`. For each callback query it parses the `callback_data` grammar `dd:<c|k>:<token>` (`c` = cancel, `k` = keep; anything else is ignored) and calls the pure `resolve_dedup_callback(action, token, from_user_id)`:
  - non-admin `from_user_id` → ignored, no state change (numeric-admin auth);
  - unknown/stale token → «⚠️ Кнопка устарела»;
  - keep → «👍 Оставлено» (no state change);
  - cancel with the row still pending → `skip_pending(link)` → «✅ Отменено оператором»;
  - cancel after the slot already published it → «⚠️ Уже опубликовано, отменить нельзя» (race-honest: queue state at press time is the source of truth, no timer);
  - cancel with the row gone (failed/held) → «⚠️ Статья уже недоступна».

  Terminal outcomes delete the token, then the listener does `edit_message_text` (append the status line, drop the keyboard — buttons become un-pressable) + `answer_callback_query`. The loop is wrapped so a listener error never kills the publish loop.
- **Second grammar — content-gate hold (2026-07-25).** `[E036]` uses `hd:<a|r>:<token>` (`a` = approve, `r` = reject), a DIFFERENT prefix on purpose: a shared one would route an approve press into the dedup resolver, which would answer «устарела» and quietly lose the decision. `_parse_review_callback_data` accepts both grammars (rejecting cross-grammar letters like `dd:a:…`) and still returns the same `(action, token)` pair — the action WORDS are unique across grammars (`cancel`/`keep`/`approve`/`reject`), so `_handle_review_update` dispatches on the word alone and the `[E014]` round-trip contract is unchanged. Grammar separation alone is NOT sufficient, though — the callback_data prefix says which BUTTON was pressed, not which keyboard minted the TOKEN, so each resolver additionally verifies the stored token `kind` (see `bot_state` § `review_token:` above, audit SEC-CG-2). Dispatch names `resolve_dedup_callback` / `resolve_hold_callback` directly rather than via a lookup table: a table captures the function objects at import time and would silently defeat `patch('news_bot.resolve_dedup_callback')`. `resolve_hold_callback` mirrors the dedup resolver exactly (pure, same `_is_admin_press` fail-closed gate FIRST, same `(status, answer)` / `(None, "")` contract, token consumed only on terminal outcomes): approve → `clear_hold` → «✅ Одобрено — выйдет в ближайший слот»; reject → `skip_pending` → «🚫 Не будет опубликовано»; row gone → «⚠️ Статья уже недоступна»; stale token → «⚠️ Кнопка устарела». **Doing nothing needs no branch** — an unpressed hold simply stays out of `list_pending` forever. The `[E036]` send site uses the same `_review_listener_enabled()` gate as E014 (SEC-A8-1) and derives the alert's `buttons_enabled` from the SAME keyboard object it attaches; with the gate closed the article is STILL held (the safe direction) and the text says plainly that this instance has no way to release it. Both text variants state outright that no answer means the article is NEVER published — a two-button prompt otherwise reads as "it goes out unless I stop it".
- **Gate (fail-closed).** The thread starts only when `REVIEW_BUTTONS_ENABLED` is truthy AND `TELEGRAM_BOT_TOKEN` is non-empty AND `TELEGRAM_ADMIN_ID` is numeric (`_review_listener_gate_reason()`); a missing token or non-numeric admin id disables the feature with a startup warning naming the broken knob (a `@username` can never match the numeric `from_user.id`; an empty token would otherwise error-loop every poll). The SAME full gate (`_review_listener_enabled()`, not the bare flag — audit SEC-A8-1) gates keyboard rendering at the E014 send site, so an instance that can't listen neither mints tokens nor shows buttons. **Prod-only:** the bot token is shared prod+test and Telegram allows one `get_updates` consumer — a second poller gets HTTP 409. See deployment.md § Feature rollout: dedup-review-buttons for the operator procedure.
- **Publish loop slot selection is unchanged** — it re-reads `list_pending()` each slot, so a row cancelled before its slot simply never publishes. A cancel racing that row's own in-flight publish is covered by a two-sided guard (audit CA-1): `_fallback_publish` re-checks the pending row right before the Telegram teaser (row gone → abort, no channel post, no strike), and `move_to_published` WARN-logs + defensively dozapis the `published_articles` row from its explicit args in the residual teaser→move window, so a completed publish is never absent from the audit table.

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
- The `↳ автоперевод` marker is no longer emitted. Since the 2026-06-11 hold-and-wait change there is no Google-fallback publish branch, so `_fallback_publish` always passes `auto_marker=False` and every published page is LLM-translated without a marker. The `auto_marker` kwarg on `telegraph_publisher.publish_article` is retained but always False from the cron path. See `patterns.md` § "Channel post format" for wiring.

---

## Data Model

**Database:** SQLite 3 (single file `news.db`). DDL owned by `pending_articles_repo.init_schema(conn)` (called from `news_bot.init_db()`); `processed_news` remains owned by `news_bot` itself.

**Column migrations (hardened 2026-07-25, audit SEC-CG-1).** SQLite has no `ADD COLUMN IF NOT EXISTS`. Every added column lives in `_COLUMN_MIGRATIONS` as `(table, column, DDL literal)` and goes through `_ensure_column`: read `SELECT name FROM pragma_table_info(?)` → `ALTER` only if genuinely missing → **re-read and confirm**. The only tolerated `sqlite3.OperationalError` is `duplicate column name` (another process won the race between the check and the ALTER); anything else is logged at ERROR and raised as `SchemaMigrationError`. The previous `try: ALTER / except OperationalError: pass` could not tell "already migrated" from "the ALTER failed" — a writer holding an IMMEDIATE lock past the busy timeout raises `database is locked`, also an `OperationalError`, so the migration reported success with the column absent. That mattered once `hold_reason` existed: `list_pending` / `count_pending` / `insert_pending` name it unconditionally, so a silently-absent column turns into `no such column` inside `job()` on every tick and restart. Failing loudly at startup is strictly better than a silent crash-loop. `pragma_table_info` is used as a table-valued function specifically so the table name binds through a `?` placeholder — no SQL string interpolation anywhere in the module (`TestSqlAudit`).

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
- **HELD state** (`hold_reason` TEXT NULL, content-gate 2026-07-25): NULL = publishable; a non-NULL value is the human-readable matched-marker list (e.g. `poster, url:poster`) that parked the row awaiting the operator's «✅ Опубликовать». Added by the shared column-migration path in `init_schema` — nullable with no default, so it is safe on the live prod DB and every pre-migration row reads back as publishable. **The exclusion is enforced in SQL, not in application logic:** `list_pending`, `count_pending`, `list_pending_stale`, `list_pending_for_eviction` and `list_notified_overdue` all carry `WHERE hold_reason IS NULL`. Since the slot loop's row source IS `list_pending`, that predicate is the whole guarantee behind the operator's rule «нет ответа = не публикуем» — there is no timer anywhere in the path. `get_pending` deliberately does NOT filter (by-PK accessor: the b2 intake filter must keep seeing held rows or the article would be re-staged and re-alerted daily, and both button resolvers look rows up by link). `list_held()` is the exact complement; `clear_hold(link)` releases a hold and returns `True` only if a row was actually held, so a double press cannot report a second approval. Approving is not publishing — `clear_hold` touches neither `processed_news` nor `published_articles`. Rejecting reuses `skip_pending` (DELETE + `processed_news` pin). The archived `hw_review list` CLI does not show held rows (it reads `list_pending`) — accepted, the CLI is not deployed.
- **Block types** (in `blocks` / `ru_blocks` JSON): `paragraph`, `lead`, `heading` (with `level: 3` or `4`), `image`, `video`, and `list_item` (added 2026-05-08 by orangetrack-rendering-fixes — children of `<ul>`/`<ol>` from orangetrack content). Text-bearing types (`paragraph`/`lead`/`heading`/`list_item`) all carry an optional `runs` field with inline metadata: `[{text, [href], [formats]}]` where `formats` is a list of `bold`/`italic`/`underline`/`strikethrough` markers. `_PATCHED_TEXT_BLOCK_TYPES` in `_llm_common.py` lists the 4 patchable types — `_patch_text_with_ru_paragraphs` rewrites their `text` from LLM output and preserves `type`, `runs`, `level`.

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
