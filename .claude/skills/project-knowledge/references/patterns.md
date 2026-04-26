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
- **Boilerplate filter** (`boilerplate_filter.py`, added 2026-04-27) — every parser passes its paragraphs through `filter_boilerplate(...)` before returning. Strips short standalone UI labels like "Share on Facebook", "Tweet", "Subscribe", "Related articles" + Russian equivalents ("Поделиться на Facebook", "Твитнуть", "Читайте также", "Теги: ..."). Length-bounded at 80 chars, so long sentences mentioning these terms inline as content are preserved. Autoevolution `blocks` are filtered first; the flat `paragraphs` list is rebuilt from filtered blocks so both forms stay consistent. Goal: clean Telegraph article body + skip Google-Translate calls on UI text.

### Transcreation, not plain translation
- `transcreate_text` wraps Google Translate with a regex post-processing
  pass: bureaucratic Russian → plain Russian, passive → active, Hot Wheels
  glossary fixes (e.g. `сборка гаража` → `гаражный проект`).
- Titles get a deterministic content-aware emoji prefix (🏆, 🏎️, 🚀, 💎,
  🤝, 📢, 🚗, or 🔥 fallback).
- Body output is truncated at 4000 chars on a sentence boundary.
- On translator failure, the original English text is returned so the
  pipeline keeps going.

### Channel post format (locked 2026-04-21, auto-marker relocated 2026-04-27, `#news` tag added 2026-04-24)
- **Channel teaser is byte-identical for both paths** — single-line `#<source> #news` (e.g. `#autoevolution #news`, `#mattel #news`, `#lamleygroup #news`). The source hashtag is derived from the source URL's second-level domain by `news_bot._source_hashtag`; the trailing `#news` is a static tag hardcoded in `send_telegraph_teaser` (NOT derived from source) so subscribers can filter the channel by topic. Decision 14 (manual-review-workflow tech-spec) holds at the visible-feed level: subscribers see no difference between manual and auto posts. Edge case: when `_source_hashtag` returns the bare `#` (unknown / malformed `source_url`), `send_telegraph_teaser` falls back to the legacy bare hashtag and skips the `#news` append — emitting a lone `#news` would lose source attribution without compensating value.
- **Telegra.ph article body** carries the path differentiator: auto-fallback (`_fallback_publish` with `via_review=False` — overflow / idle-fallback) injects a plain `<p>` paragraph node `↳ автоперевод` (U+21B3 + label) IMMEDIATELY BEFORE the `Источник:` footer. Manual `hw_review publish` (`via_review=True`) doesn't add it.
- The Telegra.ph page is surfaced via `LinkPreviewOptions(url=telegraph_url, show_above_text=True)` — Telegram renders the Instant View card above the hashtag, carrying domain label, title, excerpt, hero image, and ⚡ INSTANT VIEW button.
- **Rationale for the relocation (2026-04-27):** the original two-line teaser (commit `cc4cc8c`) added subscriber-facing noise to the channel feed. Moving the marker INTO the article keeps the feed clean (Decision 14 byte-equality preserved) while still letting operators and curious readers diagnose path inside the article — the marker sits right above the source link where attribution context naturally lives.
- Wiring: `telegraph_publisher.publish_article(..., auto_marker: bool = False)` controls the node insertion. `_fallback_publish` calls it with `auto_marker=not via_review`. `hw_review.cmd_publish` never passes the flag → defaults False. `send_telegraph_teaser` no longer accepts `auto_marker` (single-line only). Full spec: `work/telegraph-pipeline/post-format.md`.

### Auto-fallback throttle (added 2026-04-26)
- `_overflow_fast_track` and the idle-fallback loop sleep `FALLBACK_THROTTLE_SECONDS` (default 3600 = 1h) BETWEEN consecutive `_fallback_publish` calls — skip-first pattern, so 1 publish = no wait, N publishes = (N-1) waits.
- Rationale: prevents burst-spam in the channel when overflow evicts many articles or many idle rows fire fallback in one tick. 5 articles → 4 hours of cron-tick instead of ~2 minutes of back-to-back posts.
- Cron-tick can therefore exceed 12h in pathological cases; `schedule` library queues the next tick (sequential by default), no overlap concern.
- Set `FALLBACK_THROTTLE_SECONDS=0` to disable (used by tests and for manual emergency-publish scenarios).

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
- The `schedule` library runs `job()` **every 12 hours** via `schedule.every(12).hours.do(job)` in `news_bot.main()` (operator rule 2026-04-24; was hourly originally). Pick: 12h is slow enough that articles rarely reach the idle-fallback window before the operator can review, and queue pressure doesn't accumulate across many ticks.
- The script runs indefinitely (`while True: schedule.run_pending(); time.sleep(60)`) when started interactively.
- For production, a systemd service or cron job is recommended instead of relying on the in‑process scheduler.

### Overflow fast-track — "newest-10 window" rule (operator rule 2026-04-24 refined)

- **Queue is a sliding window of the newest `QUEUE_CAP` (10) rows across the combined pool of pending + incoming new entries.** Everything that doesn't fit goes to Gemini auto-publish, oldest first.
- Formula: `excess = count_pending() + len(new_entries) - QUEUE_CAP`. If `excess ≤ 0`, all new enter queue, no eviction. If `excess > 0`, publish `excess` oldest rows (old pending ru-NULL first, then oldest-indexed new entries) via `_fallback_publish` (Gemini).
- **Staged rows always protected** — rows with `ru_paragraphs IS NOT NULL` never evict, regardless of age. The queue-window size for non-staged content effectively shrinks by the staged count.
- **Under-cap still triggers eviction** if the pool exceeds cap. Example: 8 pending + 28 new = 36 pool, excess 26 → 8 old + 18 new auto-publish; 10 newest new stay in queue.
- New-entry auto-publish flow: `fetch_full_article(entry)` → `insert_pending(row)` → `_fallback_publish(full_row)` (which moves it to `published_articles`). Brief transit through pending preserves the `_fallback_publish` invariant of DB-resident rows.
- Admin ping format: `"Queue pressure: auto-published {total} ({old} old + {new} new)"` + optional `", {N} staged rows protected"` + `", fast-track failed for {K}"` suffixes.
- Rationale: 1:1-at-cap rule (interim) under-fired on large fetches — after 12h cron interval a 30-item burst wouldn't fit until multiple ticks. Newest-window rule matches the operator's mental model: queue always holds the freshest 10 items; the rest goes to Gemini automatically so nothing blocks.
- Key tests: `test_overflow_users_example_8_old_plus_28_new` (the canonical scenario), `test_overflow_partial_protection`, `test_overflow_full_protection`, `test_overflow_staged_protected_forces_new_autopub`.

### Logging
- Logging is configured at INFO level, with timestamps and module names.
- Critical steps (new entries found, translation, Telegraph publish,
  channel post) are logged at INFO, errors at ERROR.

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

pytest suite lives in `tests/`:

- `test_autoevolution_source.py` — scrape success/failure, RSS fallback, block ordering, video embed wrapping.
- `test_mattel_news_source.py` + `test_mattel_integration.py` — `__NEXT_DATA__` parsing, Hot Wheels filter, notifier contract, DB persistence.
- `test_lamley_source.py` — entry-content parsing, image dedup.
- `test_telegraph_publisher.py` — account lifecycle, node tree for flat and block renderers, subtitle/hr rules, figcaption, iframe wrapping, source footer.
- `test_telegram.py` — `send_telegraph_teaser` hashtag format + `LinkPreviewOptions`.
- `test_feed_iteration.py`, `test_integration.py` — end-to-end pipeline with mocks.
- `test_config_loader.py`, `test_database.py`, `test_translation.py` — unit coverage.

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
