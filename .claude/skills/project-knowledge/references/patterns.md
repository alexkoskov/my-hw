# Patterns & Conventions

Coding conventions, development workflow, and project-specific practices.
For universal coding standards, see `~/.claude/skills/code-writing/references/universal-patterns.md`.

---

## Project-Specific Code Patterns

### SQLite Duplicate Detection
- The `processed_news` table uses `link` as PRIMARY KEY to guarantee uniqueness.
- Before processing any RSS entry, `is_processed(link)` checks the database; if present, the entry is skipped.
- The table also stores `title` and `pub_date` for reference, but only `link` is essential for deduplication.

### Source-Parser Contract
- Every per-source module exposes a `fetch_*_article(link_or_entry)` function
  that returns `{title, subtitle, paragraphs, images}` — or `None` on failure.
- `subtitle` is the editorial lead from the source site. Empty string when
  the source has none (e.g. autoevolution RSS fallback); `telegraph_publisher`
  then skips the decorated `💬 «…»` lead + `<hr>` on the Telegraph page.
- Autoevolution additionally returns `blocks` — an ordered list preserving
  image/video/heading positions. When present, `telegraph_publisher` uses
  the block renderer; otherwise it falls back to the flat
  `paragraphs`/`images` renderer.
- `news_bot.fetch_full_article` dispatches to the right parser by URL
  domain and wraps each call in a try/except so one bad article doesn't
  stop the pipeline.
- **Boilerplate filter** (`boilerplate_filter.py`, added 2026-04-27) — every parser passes its paragraphs through `filter_boilerplate(...)` before returning. Strips short standalone UI labels like "Share on Facebook", "Tweet", "Subscribe", "Related articles" + Russian equivalents ("Поделиться на Facebook", "Твитнуть", "Читайте также", "Теги: ..."). Length-bounded at 120 chars, so long sentences mentioning these terms inline as content are preserved. Autoevolution `blocks` are filtered first; the flat `paragraphs` list is rebuilt from filtered blocks so both forms stay consistent. Goal: clean Telegraph article body + skip Google-Translate calls on UI text.

### Transcreation, not plain translation
- **Primary engine (auto-publish path):** `claude_transcreation.transcreate_via_claude` —
  Anthropic Claude API with `ux-guidelines.md` as system prompt + JSON
  output envelope. Default model `claude-haiku-4-5` (override via
  `ANTHROPIC_MODEL`). `max_tokens=8000` (Decision 13 cap against
  prompt-injection-driven cost amplification). Output validated against
  the input shape: `paragraphs` length must equal input length, otherwise
  `ClaudeTranscreationError` triggers per-article fallback. Defensive
  per-paragraph 4000-char truncation as belt-and-braces; logs a warning
  if it fires.
- **Fallback engine:** `transcreate_text` wraps Google Translate. Two
  call sites: (a) per-article fallback for THIS article only when Claude
  refuses or returns malformed output (no state change); (b) global
  fallback during API-level Claude outages (after the 2-ping protocol
  exhausts the 2 h grace window — see "Auto-publish path" below).
- **HW glossary safety net** (14 patterns) runs as a post-pass on the
  output of BOTH engines (e.g. `сборка гаража` → `гаражный проект`).
  Encodes brand-specific terminology that even Claude can miss. The
  19-pattern bureaucratic regex was REMOVED in the llm-transcreation
  feature — Claude does not produce канцелярит, and the regex's
  false-positive rate on already-good output isn't worth the
  maintenance.
- **4000-char body truncation REMOVED** from `transcreate_text` —
  Telegraph has no practical length cap, and the channel teaser is just
  a hashtag line. Claude responses are still capped per-paragraph at
  4000 chars defensively inside `claude_transcreation`.
- **Emoji prefix safety net.** Titles always carry a deterministic
  content-aware emoji prefix (🏆, 🏎️, 🚀, 💎, 🤝, 📢, 🚗, or 🔥 fallback).
  If Claude omits the prefix, the regex wrapper inserts it as a safety
  net (AC11).
- On translator failure of BOTH engines, the row's `attempt_count`
  increments; after 3 strikes, `move_to_failed`.

### Channel post format (locked 2026-04-21, auto-marker relocated 2026-04-27, `#news` tag added 2026-04-24)
- **Channel teaser is byte-identical for both paths** — single-line `#<source> #news` (e.g. `#autoevolution #news`, `#mattel #news`, `#lamleygroup #news`). The source hashtag is derived from the source URL's second-level domain by `news_bot._source_hashtag`; the trailing `#news` is a static tag hardcoded in `send_telegraph_teaser` (NOT derived from source) so subscribers can filter the channel by topic. Decision 14 (manual-review-workflow tech-spec) holds at the visible-feed level: subscribers see no difference between manual and auto posts. Edge case: when `_source_hashtag` returns the bare `#` (unknown / malformed `source_url`), `send_telegraph_teaser` falls back to the legacy bare hashtag and skips the `#news` append — emitting a lone `#news` would lose source attribution without compensating value.
- **Telegra.ph article body** carries the path differentiator: auto-publish (`_fallback_publish` with `via_review=False`) injects a plain `<p>` paragraph node `↳ автоперевод` (U+21B3 + label) IMMEDIATELY BEFORE the `Источник:` footer. Manual `hw_review publish` (`via_review=True`) doesn't add it. The marker is identical regardless of the auto-publish engine — Claude API and Google Translate fallback both produce the same `↳ автоперевод` text (AC18). The path differentiator is manual vs auto, NOT Claude vs Google.
- The Telegra.ph page is surfaced via `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` — Telegram renders the Instant View card above the hashtag, carrying domain label, title, excerpt, hero image, and ⚡ INSTANT VIEW button.
- **Rationale for the relocation (2026-04-27):** the original two-line teaser (commit `cc4cc8c`) added subscriber-facing noise to the channel feed. Moving the marker INTO the article keeps the feed clean (Decision 14 byte-equality preserved) while still letting operators and curious readers diagnose path inside the article — the marker sits right above the source link where attribution context naturally lives.
- Wiring: `telegraph_publisher.publish_article(..., auto_marker: bool = False)` controls the node insertion. `_fallback_publish` calls it with `auto_marker=not via_review`. `hw_review.cmd_publish` never passes the flag → defaults False. `send_telegraph_teaser` no longer accepts `auto_marker` (single-line only). Full spec: `work/telegraph-pipeline/post-format.md`.

### Auto-publish path (added with llm-transcreation feature)

The auto-publish path is the cron-side route that lands articles in the channel WITHOUT operator intervention. Replaces the legacy auto-fallback throttle + overflow fast-track + inline idle-fallback (all removed in this feature).

- **Distributed-publish loop.** `news_bot.job()` fires once daily at 10:00 МСК. After fetch, `compute_publish_slots(N, now, 10:00, 20:00, min_interval_min=90)` returns `(slots, carry_over)` with `posts_today ≈ min(N, 7)`. The publish loop then calls `time.sleep((slot - now).total_seconds())` between iterations and publishes one article per slot. Window-end guard (Decision 15) breaks before scheduling past 20:00 МСК — excess slots become carry-over to the next day. Crash-loop guard (Decision 9) at the start of `job()` reads `MAX(published_at)` and waits for `last_published + MIN_INTERVAL_MINUTES` before resuming, protecting the channel from burst posting under rapid-restart loops.

- **Outage state machine** (`outage_state.py`). Five states — `no_outage`, `ping_1_sent`, `ping_2_sent`, `google_fallback_active`, `recovery_pending`. State persists in `bot_state` SQLite table (survives container restart). `record_outage_event(now)` and `record_recovery_event(now)` are atomic via `BEGIN IMMEDIATE`; `PRAGMA busy_timeout=5000` absorbs typical contention. Pings: #1 immediately on first outage, #2 after 1 h, #3 ("switching to Google Translate") after 2 h. Recovery ping fires on the next slot where Claude succeeds again.

- **Per-article vs API-level error classification** (Decision 5). API-level errors (advance the state machine): `APIConnectionError`, `APITimeoutError`, `RateLimitError` (429), `InternalServerError` (5xx), `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (model 404). Per-article errors (single-article Google fallback, no state change): `BadRequestError` (400), `UnprocessableEntityError` (422), unrecognized `APIStatusError` codes, `ClaudeTranscreationError` (refusal or malformed JSON). Misclassification would either fire a false outage (one weird article kills the channel for 2 h) or hide a real outage (auth failure quietly serves Google translation forever).

- **Output validation.** `claude_transcreation` enforces `max_tokens=8000` on every API call (Decision 13 — bounds the cost-amplification surface from prompt-injection in source article bodies). Parsed responses are validated: `paragraphs` length must equal input length, otherwise `ClaudeTranscreationError` (per-article Google fallback). Defensive 4000-char per-paragraph cap with WARNING log if it fires.

- **Token redaction (3-layer defense, Decision 12).** Anthropic API key shape `sk-ant-[A-Za-z0-9_=.-]{16,}` is redacted by:
  1. `_TokenRedactingFilter` regex on Python's logging pipeline (covers anthropic SDK's own `logger.exception(...)` calls — the filter is attached to `anthropic`, `anthropic._client`, `anthropic._base_client` loggers at import time).
  2. `'ANTHROPIC_API_KEY'` added to `_SECRET_ENV_NAMES` so any env-var-name verbatim replace path strips it.
  3. `_redact_text(text)` pure helper — used by both the logging filter AND `send_admin_notification` so admin-ping payloads (which travel OUTSIDE Python's logging machinery) are also redacted. The admin-ping template uses `type(exc).__name__` not `str(exc)` for user-visible messages on outage paths; full exception text only goes to redacted logs.

### Sibling-brand relevance filter
- `_is_hot_wheels_relevant(entry)` rejects autoevolution entries whose
  title names a sibling Mattel brand without also naming "hot wheels".
  autoevolution cross-tags Matchbox / Mega Bloks / etc under
  `tag-Hot+Wheels+News.xml` — the channel is HW-only, so we skip these
  at fetch time (before they enter `pending_articles`).
- Sibling-brands tuple is currently `('matchbox',)`; extend
  conservatively. Default for any title without a known sibling-brand
  keyword is "include" — over-publishing is preferred to dropping
  legitimate cross-over articles.

### Channel post layout (single-message IV preview)
- One `send_message` call: text = hashtag line (`#source #news`),
  `LinkPreviewOptions(url=telegraph_url, show_above_text=True,
  prefer_large_media=True)`. Renders as a full-width INSTANT VIEW
  preview card above the tags. Raw URL stays hidden inside options.
- `prefer_large_media=True` was historically reverted (it killed the
  IV button on iOS). Re-enabled 2026-04-30 after iOS field test
  confirmed the regression no longer reproduces with the show_above_text
  layout. If the IV button regression returns: drop just that flag.
- No separate `send_photo` — it duplicated the IV image without adding
  value once `prefer_large_media` started working again.

### Image/Media Handling
- All images from the source are carried through to the Telegra.ph page
  (hero image first, then interleaved every 3rd paragraph on the flat
  path, or at their original positions on the block path).
- Images are hot-linked — no local download or caching.
- Videos (YouTube/Vimeo) must be wrapped in the `telegra.ph/embed/<provider>?url=…`
  proxy form; raw URLs fail Telegra.ph's iframe validator and break
  Instant View. See `autoevolution_source._video_embed_url`.

### Image extraction per source

Different source parsers take different paths to image URLs. Each is tuned to match what the *source page actually displays*, not everything the source's CMS exposes. The rule of thumb: a Telegraph figure should correspond to a visible figure on the source page.

- **🟠 `autoevolution_source`** — parses `<img>` tags out of the article body DOM (via `curl_cffi` + BeautifulSoup). Extracts hero image + gallery (10–30 images typical). Telegraph renders them in original order. See the `blocks` handling for image placement between paragraphs.
- **🔵 `lamley_source`** — parses `<img>` in body content. Typically 5–20 images per article.
- **🟡 `mattel_news_source`** — **thumbnail only**. The `download_media` field on Mattel's Contentstack CMS is a *press-kit* downloadable-assets field (logo in multiple formats, hi-res variants for journalists), NOT in-page visuals. Surfacing `download_media` on Telegraph produces figures that don't exist on the source article page and wastes mobile screen. Only the entry's `thumbnail.url` is used. If a future Mattel article relies on true inline imagery, the correct fix is to parse `<img>` out of `body_html` — don't be tempted to re-add `download_media`. Regression test: `tests/test_mattel_news_source.py::TestFetchMattelArticle::test_parses_paragraphs_and_uses_thumbnail_only`.

### Mattel RSC flight-payload parser (2026-04-25 rewrite)

`mattel_news_source` parses the embedded React Server Components streaming payload Mattel ships in its Next.js App Router HTML — `self.__next_f.push([1, "..."])` chunks. Listing entries are extracted from the largest chunk via the semantic anchor `"article2":{"entries":[` + bracket-match. Article bodies are referenced by `body: "$<row-id>"` and reconstructed from the separate text-row marker `<row-id>:T<hex-len>,<content>`, which can span multiple `__next_f.push` chunks (the parser concatenates all chunks before scanning).

- **Anti-drift hedge:** parser anchors on field names + section keys (`article2`, `entries`, `handle`, `title`, `date`, `body`, `thumbnail`), not positional row-IDs or "biggest push" heuristics. Survives Next.js layout drift as long as the field names stay the same. Structural break → `MattelNewsError` → admin notifier.
- **Security boundary** (`fetch_mattel_article` is reachable from external link sources): SSRF guard rejects any `link` that doesn't start with `ARTICLE_URL_PREFIX` (link NOT echoed in notifier message); regex is linear-time string-aware (no `(.+?)` backtracking trap); all `json.loads` calls catch `(JSONDecodeError, RecursionError, ValueError)`; bracket-match is depth-and-string-literal aware; advertised hex lengths > `MAX_RESPONSE_SIZE` are treated as content-empty; both `requests.get` calls pass `allow_redirects=False`; notifier messages format only exception type + safe scalars (no raw `str(exc)`).
- **Test fixtures** are synthesised in `tests/fixtures/mattel_flight_builder.py` via `_make_flight_listing(entries)` and `_make_flight_article(entry, body_html, body_chunks, truncate)` — anchors on the same semantic markers the parser reads, so builder/parser fail together if Mattel changes the format. Anti-drift smoke tests in `tests/test_mattel_news_source.py` parse `/tmp/mattel_news.html` and `/tmp/mattel_article.html` if the operator captures them locally before validation; `pytest.skip` in CI.
- **Known wayback gap:** all Wayback Machine snapshots ≤ 2026-04-21 still serve old `__NEXT_DATA__`. Wayback is not a fallback source for new-format HTML; operator captures live snapshots manually before deploy.

### Cloudflare bypass
- `autoevolution_source` uses `curl_cffi` with `impersonate="chrome"` for
  article-page fetches. Plain `requests` returns HTTP 403.
- If `curl_cffi` isn't installed or the scrape fails for any reason, the
  pipeline falls back to `enrich_entry` (RSS-only path) so we still post
  something — truncated is better than silent.

### Scheduling
- Daily fixed-time cron via `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)` in `news_bot.main()`. The `tz=` argument requires `pytz` — `schedule==1.2.1` rejects stdlib `zoneinfo.ZoneInfo` with `ScheduleValueError` (verified via `inspect.getsource(schedule.Job.at)`). `pytz>=2024.1` is a hard runtime dep.
- After fetch, `compute_publish_slots(N, now, window_start=10:00 МСК, window_end=20:00 МСК, min_interval_min=90)` returns `(slots, carry_over)` with `posts_today ≈ min(N, 7)`. The publish loop sleeps between slots via `time.sleep` (in-process). See "Auto-publish path" above for the full loop including window-end guard and crash-loop guard.
- **Pending order** (`list_pending`): two-tier — today's freshly-fetched batch first (in fetch order), then carry-over backlog drained oldest-first. SQL: `ORDER BY CASE WHEN date(fetched_at) = date('now') THEN 0 ELSE 1 END, fetched_at ASC`.
- Container restart mid-window: `news_bot.main()` triggers `job()` immediately (existing pattern). Crash-loop guard kicks in if needed. `compute_publish_slots(remaining_pending, now)` recomputes the schedule for the rest of the window — no migration of old slots. Already-published rows are skipped via Decision 9 idempotency from manual-review-workflow (telegraph_url presence).
- The script runs indefinitely (`while True: schedule.run_pending(); time.sleep(60)`) when started interactively.
- For production, a systemd service or cron job is recommended instead of relying on the in-process scheduler.

### Logging
- Logging is configured at INFO level, with timestamps and module names.
- Critical steps (new entries found, translation, Telegraph publish,
  channel post) are logged at INFO, errors at ERROR.
- **Per-Claude-API-call observability** (AC19): every Claude transcreation call logs `input_tokens`, `output_tokens`, `latency_ms`, `model_version` at INFO so the operator can cross-check Anthropic console usage without instrumentation.
- **Token redaction.** `_TokenRedactingFilter` redacts `sk-ant-[A-Za-z0-9_=.-]{16,}` and the legacy `TELEGRAM_BOT_TOKEN` shape across all log handlers and is also attached to anthropic SDK loggers at import time. The same redaction core (`_redact_text`) is reused by `send_admin_notification` so admin Telegram pings (non-logging path) cannot leak the API key. See "Auto-publish path → Token redaction" above for the full 3-layer design.

### Multiple RSS Feeds Configuration
- Feed URLs are read from `feeds.json` (JSON array of up to 5 strings) or fall back to the default RSS URL.
- The `load_feeds()` function validates URLs and ensures the list length does not exceed 5.
- If the configuration file is missing, malformed, or contains invalid URLs, the script falls back to the default RSS URL, logs a warning, and notifies the admin via Telegram.

### Error Isolation
- Each feed is processed independently inside a try‑catch block; failures in one feed do not stop processing of other feeds.
- Source-level failures (Mattel, Lamley, autoevolution scrape) are isolated the same way, with admin notifications on hard failures.
- The global limit (`limit=3`) is applied across all sources to prevent overloading external services.

---

## Git Workflow

### Branch Structure

- **`main`** – Production‑ready code (protected). Only merge from `dev` after verification. Triggers production deployment (if configured).
- **`dev`** – Active development. All feature branches are merged here. Triggers staging deployment (if configured).

### Testing Requirements

- **On commit:** No automated tests are currently set up. Manual verification is required.
- **On merge to dev:** Run the script manually to ensure RSS fetching, translation, and Telegram posting still work.
- **On merge to main:** Same as dev; additionally verify that environment variables are correctly set for production.

### Security & Quality Gates

- **Pre‑commit:** No automated secret scanning is configured; developers must ensure no secrets are committed.
- **Pre‑push:** No automated code review; changes should be manually reviewed.

---

## Testing & Verification

### Test Infrastructure

pytest suite lives in `tests/` (~500 tests after the llm-transcreation feature; baseline ~470 pre-feature, +~30 from the feature, –~28 from legacy auto-publish deletes).

- `test_autoevolution_source.py` — scrape success/failure, RSS fallback, block ordering, video embed wrapping.
- `test_mattel_news_source.py` + `test_mattel_integration.py` — `__NEXT_DATA__` parsing, Hot Wheels filter, notifier contract, DB persistence.
- `test_lamley_source.py` — entry-content parsing, image dedup.
- `test_telegraph_publisher.py` — account lifecycle, node tree for flat and block renderers, subtitle/hr rules, figcaption, iframe wrapping, source footer.
- `test_telegram.py` — `send_telegraph_teaser` hashtag format + `LinkPreviewOptions`.
- `test_feed_iteration.py`, `test_integration.py` — end-to-end pipeline with mocks.
- `test_config_loader.py`, `test_database.py`, `test_translation.py` — unit coverage.
- `test_compute_publish_slots.py` — distributed-publish algorithm edge cases (N=0..30, container restart mid-window, TZ-naive ValueError) — added by llm-transcreation feature.
- `test_claude_transcreation.py` — Anthropic SDK wrapper, mocked anthropic client, exception classification (per-article vs API-level outage), output validation — added by llm-transcreation feature.
- `test_outage_state.py` — state machine transitions, persistence across simulated container restart, `BEGIN IMMEDIATE` concurrency — added by llm-transcreation feature.
- `test_distributed_schedule_integration.py` — full cron tick → fetch → schedule → publishes; outage end-to-end; container restart end-to-end; manual-review preemption mid-window — added by llm-transcreation feature.
- `test_fallback_publish_paths.py` — Claude success branch + per-article Google fallback branch of `_fallback_publish`, both asserting `via_review=False`, `auto_marker=True`, telegraph_url persisted before Telegram send — added by llm-transcreation feature.

Removed in the llm-transcreation feature: `test_overflow.py`, `test_idle_fallback.py` (companions of `_overflow_fast_track` and inline idle-fallback, both deleted).

Run with `pytest tests/`. Fixtures (including the real Mattel page HTML) are in `tests/fixtures/`.

### Agent Verification Methods

**Telegram Bot Posting**
- **Method:** Use the Telegram MCP (if available) to read the last message in the target channel (`@myhwchannel123`).
- **Verification:** Confirm the message is the single-line hashtag form and the preview card renders correctly.

**Telegra.ph Page**
- **Method:** Fetch the Telegra.ph URL (printed in logs) and inspect the node tree.
- **Verification:** Hero image present, decorated subtitle lead (if the source has one), body paragraphs in reading order, source footer at the bottom.

**RSS Feed Parsing**
- **Method:** Manually inspect the RSS feed URL to ensure it returns entries.
- **Verification:** Compare entries count with script output.

### User Verification Methods

**Visual Check of Telegram Post**
- **What to check:** Preview card title/excerpt/hero, ⚡ INSTANT VIEW button, translation quality on the Telegra.ph page.
- **How:** Open the Telegram channel and tap the preview card.
- **Why agent can't:** No visual rendering capability.

---

## Business Rules

*No complex business rules – this is a straightforward automation script.*
