# Patterns & Conventions

Coding conventions, development workflow, and project-specific practices.
For universal coding standards, see `~/.claude/skills/code-writing/references/universal-patterns.md`.

---

## Project-Specific Code Patterns

### SQLite Duplicate Detection
- The `processed_news` table uses `link` as PRIMARY KEY to guarantee uniqueness.
- Before processing any RSS entry, `is_processed(link)` checks the database; if present, the entry is skipped.
- The table also stores `title` and `pub_date` for reference, but only `link` is essential for deduplication.

### Publish-path idempotency (publish-idempotency-fix, 2026-05-07/08)

Three-layer defense against duplicate Telegram posts on stale-state:

1. **Function-entry guard** in `_fallback_publish` — read-only check
   `pending_repo.get_published(link)` BEFORE any side effect (Telegraph
   create, Telegram teaser, repo writes). On hit: log INFO, admin-ping,
   `skip_pending`, return True. Slot loop counts the row as a successful
   slot — no `attempt_count` strike. The guard dominates all 4 entry
   conditions of `_fallback_publish` (LLM success, per-article failure,
   ClaudeOutageError hold). (Since the 2026-06-11 hold-and-wait change there
   is no Google-fallback / `is_fallback_active()` shortcut branch anymore.)

2. **Idempotent `move_to_published`** — Step 1 uses `INSERT OR IGNORE
   INTO published_articles` (NOT plain `INSERT`). Defense-in-depth: if
   guard misses or a future caller bypasses it, a second move on the
   same link is a no-op rather than `UNIQUE constraint failed`. Steps 2
   (processed_news) and 3 (DELETE pending) execute unconditionally.
   Original values from first publish are preserved.

3. **Post-commit defensive dozapis** — after the main transaction commits,
   `move_to_published` re-queries `processed_news`. If missing, dozapis
   with `INSERT OR IGNORE` and emits a WARNING log. Closes a historical
   anomaly where `published_articles` had rows but `processed_news` did
   not — root cause unrecoverable from logs but the code is now
   self-healing regardless of how the inconsistency arose.

**Cleanup-failure semantics:** if `skip_pending` itself raises during
guard activation (DB-level error), the guard logs ERROR + sends a SECOND
admin-ping `«⚠️ Не удалось снять зомби-строку»` + STILL returns `True`.
Subscriber-visible duplicate prevention is the primary contract;
cleanup-failure is degraded mode the operator must investigate.

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
  `ClaudeTranscreationError` — a per-article failure that bumps
  `attempt_count` (3 strikes → `failed_articles`). Defensive
  per-paragraph 4000-char truncation as belt-and-braces; logs a warning
  if it fires.
- **Google engine — DORMANT (since 2026-06-11):** `transcreate_text` wraps
  Google Translate but is no longer wired into the publish path. The
  hold-and-wait change removed both former call sites (per-article
  fallback and global-outage fallback): per-article LLM failures now
  strike out, API-level outages HOLD the article in the queue. The
  function + its safety nets are kept in code for possible revival.
- **HW glossary safety net** (14 patterns) runs as a post-pass on the LLM
  output (e.g. `сборка гаража` → `гаражный проект`).
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

### Orangetrack rendering specifics (orangetrack-rendering-fixes, 2026-05-08)

- **`<li>` parsing.** `_walk` in `orangetrack_source.py` handles `<li>` children of `<ul>` and `<ol>` uniformly — emits a `list_item` block via `_runs_from_tag`. Empty `<li>` skipped. Bullet `«• »` is NOT inserted at parse time — it's prepended in `_build_content_from_blocks` AFTER the LLM has run, immune to LLM stripping or translation.
- **Heading dispatch.** `<h2>`/`<h3>`/`<h4>` → `type: heading, level: 3`. `<h5>` stays `type: paragraph` (preserves `babc67c` carve-out from SESSION-2026-05-06.md break 3 — h5 is used as in-paragraph section marker on orangetrack, big-bold rendering looked uneven). `<h1>`/`<h6>` ignored. Telegraph supports only `<h3>` and `<h4>`; we use a single visual treatment.
- **Inline format markers.** `_runs_from_tag` captures `formats: ["bold"|"italic"|"underline"|"strikethrough"]` for `<strong>`/`<b>`/`<em>`/`<i>`/`<u>`/`<s>`/`<del>` AND any element with a WordPress Gutenberg `has-*-color` class (color → bold mapping; Telegraph rejects color attributes, so we preserve the «обрати внимание» semantic).

### Telegraph paragraph rendering with runs metadata (orangetrack-rendering-fixes, 2026-05-08)

`_render_paragraph_with_runs(text, runs, source_url)` in `telegraph_publisher.py` is invoked from `_build_content_from_blocks` for `paragraph`/`heading`/`list_item` blocks. It walks `runs` metadata, finds each run's `text` substring inside (post-LLM) `block.text` via case-sensitive `str.find`, and wraps the matched substring in Telegraph nodes:

- **Inline links — disabled** (product decision 2026-05-13). `_render_paragraph_with_runs` always sets `href_val = None`. Rationale: subscribers reading the Russian translation shouldn't be hyperlinked to English source pages mid-prose; the page footer «Источник: …» still carries the original URL for readers who want it. `_is_same_site` and the href branch are preserved (dormant) so the planned cross-article-linking feature (mapping same-site hrefs to OUR Telegra.ph URLs when target is already published — see architecture.md "Cross-article linking") can flip this back on without re-implementing the machinery. `runs[].href` metadata still flows through the parser and survives translation untouched.
- **Inline formats**: wrapped in `<strong>`/`<i>`/`<u>`/`<s>` per `run.formats`. Multiple formats nest in deterministic order: bold > italic > underline > strikethrough.
- **Overlapping spans**: first-wrap-wins (sort by start position, drop overlapping later spans). The dropped span's text still appears in the rendered children as plain text (it's part of the original `block.text`, just not wrapped).
- **DoS bounds**: if `len(text) > 100000` or `len(runs) > 100`, fall through to plain text and log WARNING.
- **Empty/whitespace `run.text`**: skip BEFORE `str.find` (avoids zero-width wrap at position 0).
- **`run.text` not in `block.text` after translation** (LLM translated the phrase): silently drop the format wrap — text still appears unstyled.

### Affiliate / promo line filter (boilerplate_filter.py, expanded 2026-05-08)

`is_boilerplate(s)` drops short standalone paragraphs (≤ 120 chars) that match known UI/affiliate patterns. Applied at the parser level on EN content via `filter_blocks` and `filter_boilerplate`.

- **Aff1**: `^*?quick\s+link[!:]` prefix — drops `*QUICK LINK!*`-prefixed lines unconditionally (verb-gate dropped 2026-05-08 after `«*QUICK LINK!* Find ... on eBay»` slipped through).
- **Aff3**: `^find ... on/at (ebay|amazon|aliexpress|mattel|walmart)` — direct CTA without QUICK LINK prefix.
- **RU defense-in-depth** (added 2026-05-08): patterns for `«Быстрая ссылка»` and `«Найти/Купить ... на eBay/Amazon/...»`. Activated via post-LLM filter pass in `news_bot._fallback_publish` immediately after `_strip_plugs`. Drops matching `ru_paragraphs` and `ru_blocks` of type `paragraph`/`lead`/`heading`/`list_item`.

When extending: keep length-bounded (`_MAX_BOILERPLATE_LEN = 120`) and anchored at `^`. ReDoS-safe — no nested greedy quantifiers.

### Admin-ping format (multi-line columnar Russian, 2026-05-08)

Admin-bound notifications use multi-line column layout for readability:

- **Plan-of-day busy** — `🟢 План на сегодня` + `Принято свежих:` / `Всего в очереди:` / `Слоты сегодня:` / `Перенесено на завтра:`.
- **Plan-of-day quiet** — single-line `🟢 Бот сработал, новых статей нет.`
- **Idempotency-guard hit** — `⚠️ Пропущен дубль публикации` + `Ссылка:` / `Что произошло:` / `Что сделать:`.
- **Idempotency-guard cleanup failure** — `⚠️ Не удалось снять зомби-строку` + `Ссылка:` / `Ошибка:` / `Что сделать:`.
- **Outage state-machine pings** — older single-line format, untouched (separate domain).

Operator preference: pure Russian, no English mixing in operational pings.

### Channel post format (locked 2026-04-21, auto-marker relocated 2026-04-27, `#news` tag added 2026-04-24)
- **Channel teaser is byte-identical for every path** — single-line `#<source> #news` (e.g. `#autoevolution #news`, `#mattel #news`, `#lamleygroup #news`). The source hashtag is derived from the source URL's second-level domain by `news_bot._source_hashtag`; the trailing `#news` is a static tag hardcoded in `send_telegraph_teaser` (NOT derived from source) so subscribers can filter the channel by topic. Decision 14 (manual-review-workflow tech-spec) holds at the visible-feed level: subscribers see no difference between auto-LLM publishes and the dormant manual path (if revived). Edge case: when `_source_hashtag` returns the bare `#` (unknown / malformed `source_url`), `send_telegraph_teaser` falls back to the legacy bare hashtag and skips the `#news` append — emitting a lone `#news` would lose source attribution without compensating value.
- **Telegra.ph article body — `↳ автоперевод` marker is no longer emitted (since the 2026-06-11 hold-and-wait change).** It was originally a manual-vs-auto path differentiator, then a Google-fallback quality warning. With the Google-fallback publish branch gone, `_fallback_publish` always calls `publish_article(..., auto_marker=False)` — every published page is LLM-translated and carries no marker. The `auto_marker` kwarg is retained on `publish_article` but is dead from the cron path.
- The Telegra.ph page is surfaced via `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` — Telegram renders the Instant View card above the hashtag, carrying domain label, title, excerpt, hero image, and ⚡ INSTANT VIEW button.
- **Rationale for the relocation (2026-04-27):** the original two-line teaser (commit `cc4cc8c`) added subscriber-facing noise to the channel feed. Moving the marker INTO the article keeps the feed clean (Decision 14 byte-equality preserved) while still letting operators and curious readers diagnose path inside the article — the marker sits right above the source link where attribution context naturally lives.
- Wiring: `telegraph_publisher.publish_article(..., auto_marker: bool = False)` controls the node insertion. Since 2026-06-11 `_fallback_publish` always passes `auto_marker=False` (no Google-fallback branch remains), so the node is never inserted from the cron path. `hw_review.cmd_publish` never passes the flag → defaults False (path is archived anyway). `send_telegraph_teaser` no longer accepts `auto_marker` (single-line only). Full spec: `work/telegraph-pipeline/post-format.md`.

### Auto-publish path (added with llm-transcreation feature)

The auto-publish path is the cron-side route that lands articles in the channel WITHOUT operator intervention. Replaces the legacy auto-fallback throttle + overflow fast-track + inline idle-fallback (all removed in this feature).

- **Distributed-publish loop.** `news_bot.job()` fires once daily at 10:00 МСК. After fetch, `compute_publish_slots(N, now, 10:00, 20:00, min_interval_min=90)` returns `(slots, carry_over)` with `posts_today ≈ min(N, 7)`. The publish loop then calls `time.sleep((slot - now).total_seconds())` between iterations and publishes one article per slot. Window-end guard (Decision 15) breaks before scheduling past 20:00 МСК — excess slots become carry-over to the next day. Crash-loop guard (Decision 9) at the start of `job()` reads `MAX(published_at)` and waits for `last_published + MIN_INTERVAL_MINUTES` before resuming, protecting the channel from burst posting under rapid-restart loops.

- **Outage state machine** (`outage_state.py`). Five states — `no_outage`, `ping_1_sent`, `ping_2_sent`, `google_fallback_active`, `recovery_pending`. State persists in `bot_state` SQLite table (survives container restart). `record_outage_event(now)` and `record_recovery_event(now)` are atomic via `BEGIN IMMEDIATE`; `PRAGMA busy_timeout=5000` absorbs typical contention. Pings: #1 immediately on first outage (E010), #2 after 1 h (E011), #3 after 2 h (E012 — "still down 2 h, posts held"). Since the 2026-06-11 hold-and-wait change the machine drives operator pings only — it no longer switches the bot to Google; the `google_fallback_active` label / `fallback_active` flag are retained but DORMANT (not read by the publish path). Recovery ping (E013) fires on the next slot where the LLM succeeds again.

- **Per-article vs API-level error classification** (Decision 5). API-level errors (advance the state machine, HOLD the article — hold-and-wait): `APIConnectionError`, `APITimeoutError`, `RateLimitError` (429), `InternalServerError` (5xx), `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (model 404). Per-article errors (no state change; bump `attempt_count`, 3 strikes → `failed_articles`): `BadRequestError` (400), `UnprocessableEntityError` (422), unrecognized `APIStatusError` codes, `ClaudeTranscreationError` (refusal or malformed JSON). Misclassification would either fire a false outage (one weird article needlessly holds the queue) or hide a real outage (auth failure quietly strikes out every article instead of holding + alerting).

- **Output validation.** `claude_transcreation` enforces `max_tokens=8000` on every API call (Decision 13 — bounds the cost-amplification surface from prompt-injection in source article bodies). Parsed responses are validated: `paragraphs` length must equal input length, otherwise `ClaudeTranscreationError` (per-article failure → `attempt_count` strike, no Google fallback since 2026-06-11). Defensive 4000-char per-paragraph cap with WARNING log if it fires.

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

### Bare-checklist filter
- `_is_text_only_checklist(entry, article)` rejects articles whose
  title contains the whole word "checklist" / "check list" /
  "check-list" AND whose body paragraph text totals < 500 chars.
  Two-condition rule: a real review article that mentions a
  checklist in its title but has substantive body content stays in;
  only bare bullet-list posts (orangetrack's typical "Q3 mainline
  checklist" template — title + image grid + no prose) are dropped.
- Runs in `job()` step (b3) AFTER `fetch_full_article` because the
  body content is the second condition. On a True return the row
  never enters `pending_articles`, also saving the LLM-translation
  API call. Source-agnostic.

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

### Autoevolution quirks
- **Invalid HTML — `<h2>` nested inside `<p>`.** Section titles (e.g. "BMW M1 Procar") are emitted as `<p><h2 class="bold dispblock">…</h2>…body…</p>`. `_scrape_article_page` detects this in the `<p>` branch via `child.find_all([h2,h3,h4])` and emits each nested heading as its own `heading` block before reading the paragraph's residual runs. Without the detach the heading text leaked into the paragraph prefix on Telegraph and the structural `<h3>` was lost (verified live 2026-05-13, article 269773). Regression test: `tests/test_autoevolution_source.py::test_extracts_heading_nested_inside_paragraph`.
- **JS-rendered brand links are NOT captured.** Autoevolution auto-links brand mentions ("Porsches", "BMWs") client-side via JavaScript after page load. `curl_cffi` returns raw HTML without running JS, so only the links that the editor inserted manually as `<a href>` (e.g. "Audi") survive into our `runs` metadata. Out-of-scope by design: subscribers still get a readable article; the alternative (Playwright + chromium) costs 200 MB deps + 5–15 s per article. If a future article loses load-bearing context because of this, the right fix is a local brand-auto-linker (small regex map "BMW → /bmw/", "Audi → /audi/", etc. applied post-parse) rather than full JS rendering.

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
- `test_fallback_publish_paths.py` — Claude-success branch (asserts `via_review=False`, `auto_marker=False`, telegraph_url persisted before Telegram send), per-article-failure branch (re-raises so the slot loop strikes), and the API-level outage HOLD contract (`ClaudeOutageError` → article held, nothing published, no Google) — added by llm-transcreation feature, reworked by the 2026-06-11 hold-and-wait change.

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
