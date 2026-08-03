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
- **Boilerplate filter** (`boilerplate_filter.py`, added 2026-04-27) — every parser passes its paragraphs through `filter_boilerplate(...)` before returning. Autoevolution `blocks` are filtered first; the flat `paragraphs` list is rebuilt from filtered blocks so both forms stay consistent. What it drops and at what granularity: see § "Service-text stripping — three granularities" below.

### Transcreation, not plain translation

The publish path never calls an engine module directly — it goes through the
dispatcher `llm_transcreation.py`, which picks one of four interchangeable
engines. The engine inventory, the `LLM_PROVIDER` override and the selection
order live in **architecture.md § External Integrations**; do not restate them
here. What matters for writing code against this layer:

- **Write engine-agnostic, not Claude-specific.** `news_bot.py` imports the
  dispatcher under the legacy alias (`import llm_transcreation as
  claude_transcreation`), and many symbols still carry `claude` in the name
  (`transcreate_via_claude`, `ClaudeTranscreationError`, `ClaudeOutageError`).
  The names are backward compat, not a statement about which model runs. Prod
  currently runs OpenRouter, so a change made inside `claude_transcreation.py`
  alone will NOT reach production.
- **Shared behaviour lives in `_llm_common.py`, not in the engines.** System
  prompt assembly (`_build_system_prompt` — `ux-guidelines.md` body + JSON
  envelope), the user message, response parsing and validation
  (`_parse_response`) and every post-pass are there. Adding an engine means
  re-exporting that core, not reimplementing it — the checklist is in the
  `llm_transcreation.py` module docstring.
- **Token ceiling:** `_DEFAULT_MAX_TOKENS = 30000` in each `*_transcreation.py`,
  and it is the default of every engine's `max_tokens` parameter. Decision 13's
  original 8000 is superseded — the operator confirmed 30000 as intentional on
  2026-08-03 (long articles were being truncated). It is still the
  prompt-injection cost-amplification bound, just at the higher value. The engine
  docstrings deliberately do NOT restate the number any more — they used to say
  8000 and three reference docs copied it from there (corrected 2026-08-03). Read
  the constant.
- **The post-passes inside the LLM engine layer** are in `_llm_common.py`:
  `_decode_format_markers` (see § "Telegraph paragraph rendering with runs
  metadata"), `_is_mostly_russian` (rejects an English-leaking response),
  `_apply_emoji_safety_net` (re-inserts the title's content-aware emoji prefix
  from 🏆/🏎️/🚀/💎/🤝/📢/🚗/🔥 if the model dropped it — AC11) and
  `_truncate_paragraphs` (defensive 4000-char per-paragraph cap, WARNING on
  fire). That is the whole engine-layer list — but it is NOT the whole story:
  two further passes run on LLM output *after* the engine returns, in
  `news_bot.py` (see § "Service-text stripping — three granularities"):
  `_strip_plugs` over `ru_title` / `ru_subtitle` / `ru_paragraphs`
  (`news_bot.py:3207-3213`) and the RU-side boilerplate filter right below it
  (`news_bot.py:3214-3216`). So when a bad translation reaches the channel there
  are two levers, and picking the wrong one wastes a release: **wording, tone and
  terminology** are the prompt's job (`ux-guidelines.md`); **service text and
  promo tails** are the regex layer's. Corrected 2026-08-03 — this bullet
  previously claimed the engine list was exhaustive and that no regex touched LLM
  output, which would have sent anyone debugging a promo-tail leak to the prompt.
- **NOT a post-pass on LLM output: the HW glossary.** The 15-pattern
  `hw_glossary` dict (`сборка гаража` → `гаражный проект` etc.) is a local
  variable inside `news_bot.transcreate_text` (news_bot.py:2763) and only ever
  corrects **Google Translate** output. `transcreate_text` itself is DORMANT
  since the 2026-06-11 hold-and-wait change — the publish path no longer calls
  it, per-article LLM failures strike out and API-level outages HOLD the
  article. *Corrected 2026-08-03: this file previously claimed the glossary ran
  on LLM output. It never has. Adding a pattern there is a no-op for
  production.*
- **The 19-pattern bureaucratic regex was REMOVED** in the llm-transcreation
  feature, and the 4000-char whole-body truncation was removed from
  `transcreate_text`: an LLM does not produce канцелярит, Telegraph has no
  practical length cap, and the regex's false-positive rate on already-good
  output was not worth the maintenance.
- On per-article translation failure the row's `attempt_count` increments;
  after 3 strikes, `move_to_failed`. See § "Auto-publish path" for which
  exception classes strike vs. hold.

### Orangetrack rendering specifics (orangetrack-rendering-fixes, 2026-05-08)

- **`<li>` parsing.** `_walk` in `orangetrack_source.py` handles `<li>` children of `<ul>` and `<ol>` uniformly — emits a `list_item` block via `_runs_from_tag`. Empty `<li>` skipped. Bullet `«• »` is NOT inserted at parse time — it's prepended in `_build_content_from_blocks` AFTER the LLM has run, immune to LLM stripping or translation.
- **Heading dispatch.** `<h2>`/`<h3>`/`<h4>` → `type: heading, level: 3`. `<h5>` stays `type: paragraph` (preserves `babc67c` carve-out from SESSION-2026-05-06.md break 3 — h5 is used as in-paragraph section marker on orangetrack, big-bold rendering looked uneven). `<h1>`/`<h6>` ignored. Telegraph supports only `<h3>` and `<h4>`; we use a single visual treatment.
- **Inline format markers.** `_runs_from_tag` captures `formats: ["bold"|"italic"|"underline"|"strikethrough"]` for `<strong>`/`<b>`/`<em>`/`<i>`/`<u>`/`<s>`/`<del>` AND any element with a WordPress Gutenberg `has-*-color` class (color → bold mapping; Telegraph rejects color attributes, so we preserve the «обрати внимание» semantic).

### Telegraph paragraph rendering with runs metadata (orangetrack-rendering-fixes 2026-05-08; bold-marker round-trip 2026-07-28)

`_render_paragraph_with_runs(text, runs, source_url)` in `telegraph_publisher.py` is invoked from `_build_content_from_blocks` for `paragraph`/`heading`/`list_item` blocks. It walks `runs` metadata, finds each run's `text` substring inside (post-LLM) `block.text` via case-sensitive `str.find`, and wraps the matched substring in Telegraph nodes:

- **Inline links — disabled** (product decision 2026-05-13). `_render_paragraph_with_runs` always sets `href_val = None`. Rationale: subscribers reading the Russian translation shouldn't be hyperlinked to English source pages mid-prose; the page footer «Источник: …» still carries the original URL for readers who want it. `_is_same_site` and the href branch are preserved (dormant) so the planned cross-article-linking feature (mapping same-site hrefs to OUR Telegra.ph URLs when target is already published — see architecture.md "Cross-article linking") can flip this back on without re-implementing the machinery. `runs[].href` metadata still flows through the parser and survives translation untouched.
- **Inline formats**: wrapped in `<strong>`/`<i>`/`<u>`/`<s>` per `run.formats`. Multiple formats nest in deterministic order: bold > italic > underline > strikethrough.
- **Overlapping spans**: first-wrap-wins (sort by start position, drop overlapping later spans). The dropped span's text still appears in the rendered children as plain text (it's part of the original `block.text`, just not wrapped).
- **DoS bounds**: if `len(text) > 100000` or `len(runs) > 100`, fall through to plain text and log WARNING.
- **Empty/whitespace `run.text`**: skip BEFORE `str.find` (avoids zero-width wrap at position 0).
- **`run.text` not in `block.text` after translation** (LLM translated the phrase): silently drop the format wrap — text still appears unstyled. Since 2026-07-28 this is the *fallback* case only — see the marker round-trip below.

**Bold-marker round-trip (added 2026-07-28).** `str.find` cannot locate an EN run
inside RU text, so bold used to vanish on translation. Now bold survives the LLM
call as a literal markdown marker — `**` around the emphasised span:

- **Encode before the call.** `_llm_common._encode_format_markers` wraps each
  `formats: ['bold']` span in `**…**` before the paragraph goes into the request,
  and the system prompt's "Inline formatting markers" section (in
  `_llm_common._JSON_ENVELOPE`) tells the model to carry the markers onto the
  Russian words and to invent none. Only bold is encoded; italic/underline/
  strikethrough are still dropped across translation by design.
- **Decode at render, not at parse.** `telegraph_publisher._decode_bold_markers`
  runs on every text node the renderer touches — title, subtitle, captions, flat
  paragraphs and blocks. `_llm_common._decode_format_markers` also decodes on the
  block-patch path, but the publisher is the one place all routes converge, so it
  is the last line of defence. Existing source `runs` WIN over model-invented
  markers; the markers are stripped either way.
- **Hard invariant — a literal marker must never reach Telegra.ph.** This is not
  theoretical: subscribers saw raw asterisks around a headline in a published
  post before eaba4f6 (2026-07-28) moved the decode to render time, which is
  why that commit is titled "never publish them". An unbalanced marker matches
  nothing, so `_STRAY_MARKER_RE` deletes the stray asterisks rather than
  publishing them: losing an emphasis we cannot place beats showing punctuation
  the author never wrote.
- The publisher deliberately keeps its own copy of `_BOLD_MARKER_RE` instead of
  importing `_llm_common`'s — the renderer must not depend on the LLM-engine
  layer. Change one, change both.
- Filters that run BEFORE the renderer (see § "Service-text stripping") must
  therefore tolerate `**` inside the text they match.

### Service-text stripping — three granularities (2026-05-08, reworked 2026-07-29)

*Renamed 2026-08-03 from «Affiliate / promo line filter» — other docs
(`ux-guidelines.md`) may still point at the old title.*

Someone else's service text (share widgets, affiliate CTAs, shop ads, "click
here" cross-promo, pointers at a page layout that is not ours) is removed by
**three** filters working at three different scopes. **Pick the scope first —
this is the whole lesson of 2026-07-29.**

| Filter | Scope | Catches |
|---|---|---|
| `_is_promo_article` `[E035]` | whole article | the article IS an ad → dropped at intake, before translation |
| `boilerplate_filter.is_boilerplate` / `filter_boilerplate` / `filter_blocks` | whole paragraph, `^`-anchored | standalone UI labels and promo outros |
| `news_bot._strip_plugs` / `_strip_plugs_in_blocks` | one sentence | a plug or pointer sitting inside real prose |

**Why it matters** (`work/SESSION-2026-07-29.md`): the operator reported the same
class of leak on three consecutive articles. Each of the three filters missed for
the same reason — wrong granularity, not a missing pattern. The first instinct
was a fourth `^`-anchored paragraph pattern; that would have been whack-a-mole
with a fourth report already on the way. **When one class of leak arrives a third
time, look at the LEVEL the existing filters work at, not for another line.**

**Paragraph level — `boilerplate_filter.py`.** Two pattern families:

- `_BOILERPLATE_PATTERNS`, applied only to paragraphs ≤ `_MAX_BOILERPLATE_LEN`
  (120) so a long sentence that merely mentions "Share on Facebook" inline
  survives. Covers EN UI labels + RU equivalents, plus the affiliate CTAs
  (`*QUICK LINK!*` prefix — verb-gate dropped 2026-05-08 after
  `«*QUICK LINK!* Find … on eBay»` slipped through; `^find … on/at
  ebay|amazon|…` without the prefix).
- `_LONG_BOILERPLATE_PATTERNS`, which **bypass the length cap** for
  multi-sentence promo outros running 150–400 chars: t-hunted's PT
  "Saiba mais sobre …" / "Para ver mais novidades …" outro (incident
  2026-06-02), its "Clique aqui …" cross-promo (incident 2026-06-13) and its
  own shop's ad tail (incident 2026-07-29).
- RU mirrors of each are defence-in-depth for text that already went through the
  LLM: a post-LLM pass in `news_bot._fallback_publish` runs immediately after
  `_strip_plugs` and drops matching `ru_paragraphs` and `ru_blocks` of type
  `paragraph`/`lead`/`heading`/`list_item`.

**Sentence level — `_strip_plugs` (news_bot.py:1285).** Removes one sentence and
leaves the paragraph standing. Covers author social plugs (parenthesised
`@handle`, RU/EN "подписывайтесь / follow me on …"), cross-promo CTAs
("Нажмите здесь …", "Вы можете посмотреть всё, что мы публиковали … по этой
ссылке") and **dangling page-layout pointers** ("подробнее в видео ниже", "на
фото выше") — the model writes for a page it cannot see, and our re-layout makes
«ниже» unverifiable even when a video survives.

**The design rule for every new pattern here: deliberately NARROW.** "Drop any
sentence containing the word ссылка" would cost facts — «Цена — $28, подробнее по
ссылке» would lose the price. So:

- the imperative must sit at the **START** of the sentence
  (`нажмите/кликните/жмите` + `сюда/здесь/по ссылке`);
- the "посмотреть … по ссылке" shape requires **BOTH** markers — a viewing verb
  **and** the link phrase — in the same sentence;
- likewise the shop-tail pattern anchors on the shop name **plus a selling
  verb**, never the name alone: t-hunted is affiliated with that shop and may
  legitimately report news about it.

A sentence that carries a fact is handled by the **prompt** (allowed-drop
category (d) in `ux-guidelines.md`: rewrite to lose only the pointer), never by
the filter. Negative controls are mandatory and live in the tests: a news story
about the shop, ordinary prose containing "ссылка", an honest mention of a video
that does exist.

Also required of any new pattern: `^`-anchored where it is paragraph-scoped,
bounded quantifiers only (ReDoS-safe on uncapped input — no nested greedy
quantifiers), and tolerance of `**` markers, since these filters run BEFORE the
renderer decodes them.

### Admin-ping format (multi-line columnar Russian, 2026-05-08)

Admin-bound notifications use multi-line column layout for readability:

- **Plan-of-day busy** — `🟢 План на сегодня` + `Принято свежих:` / `Всего в очереди:` / `Слоты сегодня:` / `Перенесено на завтра:`.
- **Plan-of-day quiet** — single-line `🟢 Бот сработал, новых статей нет.`
- **Idempotency-guard hit** — `⚠️ Пропущен дубль публикации` + `Ссылка:` / `Что произошло:` / `Что сделать:`.
- **Idempotency-guard cleanup failure** — `⚠️ Не удалось снять зомби-строку` + `Ссылка:` / `Ошибка:` / `Что сделать:`.
- **Outage state-machine pings** — older single-line format, untouched (separate domain).

Operator preference: pure Russian, no English mixing in operational pings.

### Channel post format (locked 2026-04-21, auto-marker relocated 2026-04-27, `#news` tag added 2026-04-24)
- **Channel teaser is byte-identical for every path** — single-line `#<source> #news` (e.g. `#autoevolution #news`, `#lamleygroup #news`). The source hashtag is derived from the source URL's second-level domain by `news_bot._source_hashtag`, with a `SOURCE_HASHTAG_OVERRIDE` map for outlets where that yields the hoster instead of the brand (`t-hunted.blogspot.com` → `#thunted`, dash-stripped because Telegram hashtags accept only `[a-zA-Z0-9_]`); the trailing `#news` is a static tag hardcoded in `send_telegraph_teaser` (NOT derived from source) so subscribers can filter the channel by topic. Decision 14 (manual-review-workflow tech-spec) holds at the visible-feed level: subscribers see no difference between auto-LLM publishes and the dormant manual path (if revived). Edge case: when `_source_hashtag` returns the bare `#` (unknown / malformed `source_url`), `send_telegraph_teaser` falls back to the legacy bare hashtag and skips the `#news` append — emitting a lone `#news` would lose source attribution without compensating value.
- **Telegra.ph article body — `↳ автоперевод` marker is no longer emitted (since the 2026-06-11 hold-and-wait change).** It was originally a manual-vs-auto path differentiator, then a Google-fallback quality warning. With the Google-fallback publish branch gone, `_fallback_publish` always calls `publish_article(..., auto_marker=False)` — every published page is LLM-translated and carries no marker. The `auto_marker` kwarg is retained on `publish_article` but is dead from the cron path.
- **One `send_message` call, no `send_photo`.** Text = the hashtag line; the Telegra.ph page is surfaced via `LinkPreviewOptions(url=telegraph_url, show_above_text=True, prefer_large_media=True)` (`send_telegraph_teaser`, news_bot.py:2950). Telegram renders a full-width Instant View card above the hashtag, carrying domain label, title, excerpt, hero image and the ⚡ INSTANT VIEW button; the raw URL stays hidden inside the options. A separate `send_photo` was dropped — it duplicated the IV image without adding value once `prefer_large_media` started working again.
- **`prefer_large_media=True` has a history.** It was reverted once because it killed the IV button on iOS, then re-enabled 2026-04-30 after an iOS field test confirmed the regression no longer reproduces with the `show_above_text` layout. If the IV-button regression ever returns: drop **just that flag**, not the layout.
- **Rationale for the relocation (2026-04-27):** the original two-line teaser (commit `cc4cc8c`) added subscriber-facing noise to the channel feed. Moving the marker INTO the article keeps the feed clean (Decision 14 byte-equality preserved) while still letting operators and curious readers diagnose path inside the article — the marker sits right above the source link where attribution context naturally lives.
- Wiring: `telegraph_publisher.publish_article(..., auto_marker: bool = False)` controls the node insertion. Since 2026-06-11 `_fallback_publish` always passes `auto_marker=False` (no Google-fallback branch remains), so the node is never inserted from the cron path. `hw_review.cmd_publish` never passes the flag → defaults False (path is archived anyway). `send_telegraph_teaser` no longer accepts `auto_marker` (single-line only). Full spec: `work/telegraph-pipeline/post-format.md`.

### Auto-publish path (added with llm-transcreation feature)

The auto-publish path is the cron-side route that lands articles in the channel WITHOUT operator intervention. Replaces the legacy auto-fallback throttle + overflow fast-track + inline idle-fallback (all removed in this feature).

- **Publish loop.** `news_bot.job()` fires once daily, computes today's slots from the pending count and sleeps in-process between them, publishing one article per slot. Slot mechanics — three fixed times, the daily ceiling, carry-over and the crash-loop guard — are canonical in **architecture.md § Data Flow**; do not restate or re-derive them here. *Corrected 2026-08-03: this bullet described the dynamic even-spread `compute_publish_slots` with `posts_today ≈ min(N, 7)`. That function has been DORMANT since the 2026-06-13 operator pacing change; production calls `compute_fixed_slots`.*

- **Outage state machine** (`outage_state.py`). Five states — `no_outage`, `ping_1_sent`, `ping_2_sent`, `google_fallback_active`, `recovery_pending`. State persists in `bot_state` SQLite table (survives container restart). `record_outage_event(now)` and `record_recovery_event(now)` are atomic via `BEGIN IMMEDIATE`; `PRAGMA busy_timeout=5000` absorbs typical contention. Pings: #1 immediately on first outage (E010), #2 after 1 h (E011), #3 after 2 h (E012 — "still down 2 h, posts held"). Since the 2026-06-11 hold-and-wait change the machine drives operator pings only — it no longer switches the bot to Google; the `google_fallback_active` label / `fallback_active` flag are retained but DORMANT (not read by the publish path). Recovery ping (E013) fires on the next slot where the LLM succeeds again.

- **Per-article vs API-level error classification** (Decision 5). API-level errors (advance the state machine, HOLD the article — hold-and-wait): `APIConnectionError`, `APITimeoutError`, `RateLimitError` (429), `InternalServerError` (5xx), `AuthenticationError` (401), `PermissionDeniedError` (403), `NotFoundError` (model 404). Per-article errors (no state change; bump `attempt_count`, 3 strikes → `failed_articles`): `BadRequestError` (400), `UnprocessableEntityError` (422), unrecognized `APIStatusError` codes, `ClaudeTranscreationError` (refusal or malformed JSON). Misclassification would either fire a false outage (one weird article needlessly holds the queue) or hide a real outage (auth failure quietly strikes out every article instead of holding + alerting).

- **Output validation.** Every engine caps the response at `_DEFAULT_MAX_TOKENS = 30000` (Decision 13 — bounds the cost-amplification surface from prompt-injection in source article bodies; the value was raised from 8000, confirmed intentional by the operator 2026-08-03). The response contract itself — what raises `ClaudeTranscreationError` (a per-article strike) and what is only warned about — is enforced in one place, `_llm_common._parse_response`; read it there rather than trusting a copy in this file. Its rules have been softened at least twice by real incidents, so a restatement here rots fast.

- **Token redaction (3-layer defense, Decision 12).** Do NOT enumerate the key shapes here — they live in news_bot.py:330-380 (`_BOT_TOKEN_RE`, `_OPENROUTER_KEY_RE`, `_ANTHROPIC_KEY_RE`, `_OPENAI_KEY_RE`, `_GEMINI_KEY_RE`) and now cover Telegram plus all four LLM providers, not just Anthropic. **Pattern order is a contract:** the prefix-specific `sk-or-…` and `sk-ant-…` patterns must be scrubbed BEFORE the broader OpenAI `sk-…` pattern, or the broad one wins on an already-replaced substring — see the `_redact_text` docstring. Adding a fifth provider therefore means adding a pattern **and** placing it correctly in that order. The three layers:
  1. `_TokenRedactingFilter` regex on Python's logging pipeline (covers the SDK's own `logger.exception(...)` calls — the filter is attached to `anthropic`, `anthropic._client`, `anthropic._base_client` loggers at import time).
  2. `_SECRET_ENV_NAMES` (news_bot.py:229) — every provider key name (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` + alias) plus the Telegram/Telegraph ones, so any env-var-value verbatim replace path strips them.
  3. `_redact_text(text)` pure helper — used by both the logging filter AND `send_admin_notification` so admin-ping payloads (which travel OUTSIDE Python's logging machinery) are also redacted. The admin-ping template uses `type(exc).__name__` not `str(exc)` for user-visible messages on outage paths; full exception text only goes to redacted logs.

### Sibling-brand relevance filter

`_is_hot_wheels_relevant(entry)` (news_bot.py:1530) runs at fetch time, before
a row enters `pending_articles`. The channel is Hot Wheels-only and several
feeds carry other diecast brands: autoevolution cross-tags them under
`tag-Hot+Wheels+News.xml`, t-hunted covers the whole hobby.

It is **source-agnostic** and **label-driven**, not an autoevolution title
check. *Corrected 2026-08-03: this file described a one-element sibling-brands
tuple `('matchbox',)`; that construct does not exist.* The six-step decision
order is written out in the function's own docstring — read it there. The four
lexicons it consults, and what each is for:

- `_SIBLING_BRAND_LABELS` (11 brands) — exact match against the entry's
  `labels` (Blogger "Labels:" / "Marcadores:", carried in RSS as `<category>`).
  This is the **authoritative** rejection: the source's own taxonomy beats any
  title heuristic. Added 2026-06-24 after a Matchbox "Moving Parts" post
  slipped past the title-only filter — "Moving Parts" reads like a HW line.
- `_HW_SERIES_LABELS` — the mirror image: an HW series label keeps a post whose
  title names neither "Hot Wheels" nor the series (e.g. a Portuguese-only
  Pop Culture Porsche headline).
- `_HW_SERIES_SIGNALS` — substring match on the TITLE, the fallback for sources
  with no usable labels. Multi-word HW-specific names only; single common words
  risk false includes. Note "moving parts" is deliberately absent — it is a
  Matchbox line.
- `_BROAD_DIECAST_NETLOCS` (currently t-hunted) — sources that cover the whole
  hobby. For these the default flips to **reject** when no HW signal was found;
  every other source keeps the default-include policy.

When extending: prefer adding a label to the lexicons over adding a title
keyword — labels are authoritative, titles are guesses. Default-include is the
policy for HW-focused sources (over-publishing beats dropping a real
cross-over story); default-reject applies only inside
`_BROAD_DIECAST_NETLOCS`. Adding a netloc there is the change with the largest
false-negative risk. Regression tests: `tests/test_relevance_filter.py`.

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
- **🟡 `mattel_news_source`** — **DISABLED, see below**. Kept for the record: **thumbnail only**. The `download_media` field on Mattel's Contentstack CMS is a *press-kit* downloadable-assets field (logo in multiple formats, hi-res variants for journalists), NOT in-page visuals. Surfacing it on Telegraph produces figures that don't exist on the source article page and wastes mobile screen. Only the entry's `thumbnail.url` was used. If Mattel is ever revived and an article relies on true inline imagery, the correct fix is to parse `<img>` out of `body_html` — don't be tempted to re-add `download_media`. Regression test: `tests/test_mattel_news_source.py::TestFetchMattelArticle::test_parses_paragraphs_and_uses_thumbnail_only`.

### Mattel source — DISABLED 2026-05-24 (2026-04-25 RSC parser retained)

**Not a live source.** `_fetch_mattel_entries` is commented out of the `SOURCES`
registry (news_bot.py:3599-3611; the commented-out entry is :3609). Reason, per
the comment block just above it:
corporate.mattel.com moved to Astro/Netlify, the article body is rendered
client-side from a JS bundle and is unreachable via any JSON endpoint (the
listing API returns only handle/title/date/thumbnail). Full recovery would need
a headless browser, and **over its whole lifetime Mattel produced zero Hot
Wheels articles**, so it was switched off without a replacement. If it is ever
worth reviving: uncomment the line and rewrite the parser for the new API.
`mattel_news_source.py` and its tests stay in the tree. (Same reasoning class as
the 2026-07-13 decision to reject Facebook/Instagram as sources — headless
browser required, poor yield.)

The parser mechanics (RSC streaming payload, `body: "$<row-id>"` →
`<row-id>:T<hex-len>` text rows, the public surface) are in the
`mattel_news_source.py` module docstring. What is NOT recorded there and must
survive here:

- **Anti-drift hedge:** the parser anchors on field names + section keys
  (`article2`, `entries`, `handle`, `title`, `date`, `body`, `thumbnail`), never
  on positional row-IDs or a "biggest push" heuristic. Structural break →
  `MattelNewsError` → admin notifier.
- **Security boundary** (`fetch_mattel_article` is reachable from external link
  sources; the code marks these only as terse "Decision 8 control N"): SSRF guard
  rejects any `link` that doesn't start with `ARTICLE_URL_PREFIX`, and the link
  is NOT echoed in the notifier message; the payload regex is linear-time
  string-aware (no `(.+?)` backtracking trap); all `json.loads` calls catch
  `(JSONDecodeError, RecursionError, ValueError)`; bracket-match is
  depth-and-string-literal aware; advertised hex lengths > `MAX_RESPONSE_SIZE`
  are treated as content-empty; both `requests.get` calls pass
  `allow_redirects=False`; notifier messages format only exception type + safe
  scalars, never raw `str(exc)`. **Any revival must re-establish all seven.**
- **Test fixtures** are synthesised in `tests/fixtures/mattel_flight_builder.py`
  via `_make_flight_listing(entries)` and `_make_flight_article(entry,
  body_html, body_chunks, truncate)` — they anchor on the same semantic markers
  the parser reads, so builder and parser fail together if Mattel changes
  format. That coupling is the point; keep it if the fixtures are ever rebuilt.
- **Anti-drift smoke tests** (`tests/test_mattel_news_source.py:610-629`) parse
  `/tmp/mattel_news.html` and `/tmp/mattel_article.html` if the operator captures
  them live first, and `pytest.skip` otherwise — so a fresh capture turns them on
  without any code change. This is the only mechanism that would catch a Mattel
  format change while the source is disabled; it survives the disabling.
- **Known wayback gap:** Wayback snapshots ≤ 2026-04-21 still serve the old
  `__NEXT_DATA__`. Wayback is not a fallback source for new-format HTML —
  captures must be taken live.

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
- One daily fixed-time in-process tick via `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)` in `news_bot.main()`. The `tz=` argument requires `pytz` — `schedule==1.2.1` rejects stdlib `zoneinfo.ZoneInfo` with `ScheduleValueError` (verified via `inspect.getsource(schedule.Job.at)`). `pytz>=2024.1` is a hard runtime dep.
- After fetch the tick computes today's publish slots and sleeps between them in-process via `time.sleep`. **The slot algorithm, the fixed daily times and the daily ceiling are canonical in architecture.md § Data Flow** — deliberately not repeated here (the old duplicate is why one stale `min(N, 7)` claim survived in three paragraphs across two files until 2026-08-03).
- **Pending order** (`list_pending`): two-tier — today's freshly-fetched batch first (in fetch order), then carry-over backlog drained oldest-first. SQL: `ORDER BY CASE WHEN date(fetched_at) = date('now') THEN 0 ELSE 1 END, fetched_at ASC`.
- Container restart mid-window: `news_bot.main()` triggers `job()` immediately (existing pattern). Crash-loop guard kicks in if needed. The schedule is recomputed for the rest of the day from the remaining pending count — no migration of old slots. Already-published rows are skipped via Decision 9 idempotency from manual-review-workflow (telegraph_url presence).
- The script runs indefinitely (`while True: schedule.run_pending(); time.sleep(60)`) when started interactively.
- **Prod is the only instance:** a Docker container (`restart: unless-stopped`) on the Moscow VPS. There is no staging and no test bot — see deployment.md. *Corrected 2026-08-03: this line claimed a test instance ran as a `systemd` service on the NL server; NL was decommissioned 2026-07-25.* The in-process `schedule` loop is the design — Docker only restarts on process exit, so the alive-but-stuck class is caught by `watchdog.sh` instead (deployment.md § Health Checks).
- **Consequence for deploys:** a restart resets the single in-process daily schedule, so redeploys must land OUTSIDE the publishing window. Rule and procedure: deployment.md.

### Logging
- Logging is configured at INFO level, with timestamps and module names.
- Critical steps (new entries found, translation, Telegraph publish,
  channel post) are logged at INFO, errors at ERROR.
- **Per-LLM-call observability** (AC19): every transcreation call logs the model, `input_tokens`, `output_tokens` and `latency_ms` at INFO, prefixed with the engine name, so the operator can cross-check the provider's console without extra instrumentation. These are also the numbers to measure against before anyone proposes shortening the prompt to save tokens.
- **Token redaction** — full design in § "Auto-publish path → Token redaction" above. One line only here: it covers Telegram plus all four LLM providers, and the pattern list lives in code (news_bot.py:330-380), not in this file.

### Multiple RSS Feeds Configuration
- Feed URLs are read from `feeds.json` (JSON array of up to 5 strings) or fall back to the default RSS URL.
- The `load_feeds()` function validates URLs and ensures the list length does not exceed 5.
- If the configuration file is missing, malformed, or contains invalid URLs, the script falls back to the default RSS URL, logs a warning, and notifies the admin via Telegram.

### Error Isolation
- Each feed is processed independently inside a try‑catch block; failures in one feed do not stop processing of other feeds.
- Source-level failures (Lamley, autoevolution scrape, orangetrack, t-hunted) are isolated the same way, with admin notifications on hard failures.
- *Removed 2026-08-03: this section claimed a global `limit=3` fetch cap applied across all sources. No such constant exists in `news_bot.py` or any source module — the per-day ceiling is a publish-side property (architecture.md § Data Flow), not a fetch-side one.*

### Operator-facing inbound path (review buttons)

Until 2026-07-25 the bot only ever SENT to Telegram. `_run_review_listener` in
[news_bot.py](../../../../news_bot.py) is the single inbound path: a daemon
thread long-polling `get_updates(allowed_updates=['callback_query'])`. Rules that
hold for anything added here:

- **A fresh `Bot` per call, never a long-lived one.** PTB 21.10 binds its httpx
  client to the event loop that created it, so a `Bot` reused across successive
  `asyncio.run(...)` calls fails on the second poll. Mirrors the per-call style
  `send_admin_notification` already used.
- **Decision logic stays pure and separate from I/O** (`resolve_dedup_callback`,
  `resolve_hold_callback` — no Telegram calls inside), so every branch is unit-
  testable without sockets or threads. The listener only parses, dispatches and
  renders.
- **Admin check first, before any DB read.** `_is_admin_press` is the single
  fail-closed comparison shared by both resolvers; a non-admin press performs
  zero queries.
- **Callback grammar is exact and kind-scoped:** `dd:<c|k>:<token>` (dedup),
  `hd:<a|r>:<token>` (hold). Tokens live in `bot_state` as `<kind>|<link>`, and a
  resolver refuses a token of the wrong kind **without consuming it** — otherwise
  a cross-grammar press silently burns the only button an article has.
- **Three exception layers** (per update / per poll cycle / outer) with backoff;
  a `Conflict` (409) means a second poller exists on the shared token. Nothing in
  the listener may ever reach the publish loop.

### Content gates on intake

Four gates run before staging — checklist, promo `[E035]`, hold `[E036]`, genre
`[E037]` — all before translation, so a rejected article costs zero LLM tokens.
Conventions any new gate must follow:

- **Decide on the title (+ URL slug), not the body.** Body scanning is what
  produced every serious false positive during review; the subject of a post
  lives in its headline.
- **Asymmetric cost drives the action.** A wrong hold costs the operator one tap;
  a wrong drop loses a real story permanently. Only unambiguous branches drop;
  anything evidence-based holds. The branch→action policy is a single table
  (`_GENRE_BRANCH_ACTION`) pinned by a guard test, so re-pointing a branch without
  updating the test fails.
- **Never single-keyword.** Scoring or two-signal rules only; the grammatical
  discriminator that survived review is *genre word + function word = the post's
  subject* vs *genre word + finite verb = a news clause*.
- Wiring mirrors the promo gate exactly: fail-open `try/except` around the
  detector, funnel counter, `processed_news` pin on drops (never on holds — a held
  row must stay releasable), best-effort alert, `continue`.
- **Regression corpus is mandatory.** Any marker change must be re-run against the
  25-headline false-positive corpus in
  [work/content-gate-review/](../../../../work/content-gate-review/); three review
  rounds found five distinct FP classes there.

### Held articles

`pending_articles.hold_reason` freezes a row: the exclusion lives in **SQL**
(`WHERE hold_reason IS NULL` in `list_pending`, `count_pending` and the other list
helpers), not in application logic, so the slot loop cannot see a held row even by
accident. `get_pending` deliberately does *not* filter — the intake guard must
still see held rows or the article re-stages daily. Silence never publishes: there
is no timer, reminder or auto-drop by operator decision.

### Schema column migrations

`_COLUMN_MIGRATIONS` + `_ensure_column` in
[pending_articles_repo.py](../../../../pending_articles_repo.py): check via
`pragma_table_info` → `ALTER` only if missing → **re-read and verify**. Only
`duplicate column name` is tolerated; anything else (notably `database is locked`)
raises `SchemaMigrationError`. The earlier broad-`except` version could swallow a
lock error and leave the column absent, after which every queue query crash-loops
the tick with no clue to the cause.

---

## Git Workflow

### Branch Structure

- **`main`** – what production runs. Only merge from `dev` after verification. Deployment is **manual**: the operator pulls and rebuilds on the server (procedure and the publishing-window restriction are in deployment.md). Both GitHub Actions deploy workflows are disarmed (`if: false`).
- **`dev`** – active development, default branch. All feature branches are merged here. There is no staging environment to deploy to.

### Testing Requirements

*Rewritten 2026-08-03 — the previous text said there were no automated tests and
no secret scanning. Both have existed for a long time; that text predates them.*

- **On commit:** pre-commit hooks run automatically (see below). Run the suite
  yourself before pushing: `python3 -m pytest -q` (1628 tests as of 2026-08-03,
  a few seconds to collect).
- **On push / PR to `dev` or `main`:** `.github/workflows/ci.yml` installs
  `requirements.txt` + `requirements-dev.txt` on Python 3.13 and runs
  `python -m pytest tests/ -v`. Note the `check-skip` job: a change touching only
  `*.md`, `*.txt`, `.claude/`, `.spec/` or `docs/` skips the test job entirely —
  so a green check on a docs-only PR does not mean the suite ran.
- **On merge to `main`:** green CI is the gate, plus the deploy-window rule from
  deployment.md. Verify production env vars separately — CI does not see them.

### Security & Quality Gates

- **Pre-commit** (`.pre-commit-config.yaml`, one-time setup per clone:
  `pip install pre-commit && pre-commit install`):
  - `gitleaks` v8.21.4 — API keys, tokens, secrets in staged files. This is the
    repo's primary control: the whole threat model here is a leaked provider key.
  - `detect-private-key`, `check-added-large-files` (`--maxkb=1000`),
    `check-merge-conflict`, `check-yaml`, `trailing-whitespace`,
    `end-of-file-fixer`.
- **`git commit --no-verify` is not a normal workflow step.** If a hook blocks a
  commit, fix the finding; bypassing must be explained in the commit message.
- **Pre-push:** no automated code review — review is a human/agent step (see the
  `code-reviewing` skill).

---

## Testing & Verification

### Test Infrastructure

pytest suite lives in `tests/` — **1628 tests across 47 files** as of 2026-08-03.
Run with `python3 -m pytest -q`; collection takes a couple of seconds and the
whole suite is fast enough to run on every change. Fixtures and synthetic-payload
builders are in `tests/fixtures/`.

*Rewritten 2026-08-03: the hand-maintained file-by-file inventory that used to
sit here claimed ~500 tests and listed 17 of the 47 files, and it described the
Mattel tests as covering `__NEXT_DATA__` parsing — which they have not since the
2026-04 rewrite. `ls tests/` is accurate and self-describing; a stale copy of it
in this file caused agents to conclude a behaviour was untested and write a
duplicate suite.*

Find coverage with `ls tests/` — filenames mirror module names
(`test_<module>.py`). Only the non-obvious ones are worth naming here:

- `test_deploy_files_invariant.py` — asserts that every first-party module
  `news_bot.py` imports also appears in the deploy scripts' FILES arrays. A
  module added without it would ImportError on the server with no CI signal.
- `test_sources_registry.py` — pins `NETLOC_TO_SOURCE`, `_resolve_source_name`
  and the shape of the `SOURCES` registry. Read it before adding a source.
- `test_ux_guidelines_structure.py` — structural assertions on
  `ux-guidelines.md`, which is the runtime system prompt. It pins rules that a
  later refactor must not silently drop; content-anchored, not line-coupled.
- `tests/fixtures/mattel_flight_builder.py` — a payload *builder*, not a test
  module; see § "Mattel source — DISABLED".
- Suites that exercise a whole tick rather than one module:
  `test_integration.py`, `test_feed_iteration.py`,
  `test_distributed_schedule_integration.py` and `test_fallback_publish_paths.py`
  (the last pins the per-article-strike vs API-outage-HOLD contract).
- **Historical:** `test_overflow.py` and `test_idle_fallback.py` were deleted by
  the llm-transcreation feature together with the code they covered
  (`_overflow_fast_track` and the inline idle-fallback). Recorded so their absence
  reads as intentional rather than as a coverage gap.

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
