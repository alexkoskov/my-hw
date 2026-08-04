# Decisions Log: telegraph-pipeline

Work spanning the Telegraph integration, per-source article fetchers,
Cloudflare bypass for autoevolution, and the final locked post format.
Entries are written by the agent that executed the task.

---

## Task 1: Telegraph publishing + Instant View preview

**Status:** Done
**Commit:** b291799
**Agent:** main agent
**Summary:** Added `telegraph_publisher` module (account lifecycle, page
builder with hero + paragraphs + interleaved images + source footer).
Replaced old `send_to_telegram` flow (photo caption, 1024-char cap) with
`send_telegraph_teaser` that sends a text message and uses
`LinkPreviewOptions(url=telegraph_url, show_above_text=True)` — Telegram
renders the Telegraph page as an Instant View preview card with a native
⚡ INSTANT VIEW button. `news_bot.process_new_articles` now translates the
full body paragraph-by-paragraph and publishes it.

**Verification:**
- 13 unit tests for `telegraph_publisher`.
- Integration tests updated to mock `publish_article` + `send_telegraph_teaser`.

---

## Task 2: Per-source article fetchers

**Status:** Done
**Commit:** 1544d93
**Agent:** main agent
**Summary:** Each news source now owns its full-article parsing via a
domain dispatcher (`news_bot.fetch_full_article`):
- `mattel_news_source.fetch_mattel_article(link)` parses
  `__NEXT_DATA__.contentArticle.result.body` for paragraphs + thumbnail +
  `download_media` images.
- `autoevolution_source.enrich_entry(entry)` turns an RSS entry into
  `{title, paragraphs, images}` (RSS-only — Cloudflare blocks scraping at
  this stage).
- `lamley_source.fetch_lamley_article(link)` scrapes the `entry-content`
  body + up to 10 deduped images from lamleygroup.com.
- Removed dead `fetch_article`, `get_article_data`, and
  `summarize_text*` helpers; deleted their tests.

**Verification:**
- 15 new unit tests across the 3 sources + updated integration tests.
- Smoke-tested Mattel and Lamley; autoevolution used RSS-only path here.

---

## Task 3: Cloudflare bypass for autoevolution

**Status:** Done
**Commit:** 7d4bc2f
**Agent:** main agent
**Summary:** Autoevolution article pages were returning HTTP 403 to stock
`requests`. Switched to `curl_cffi` impersonating Chrome's TLS fingerprint
and now scrape the full article body (`div.newstext`) plus gallery images
filtered by the article ID in the URL. Added
`autoevolution_source.fetch_autoevolution_article(entry)` that tries the
scrape first and falls back to `enrich_entry` if anything fails. Added
`curl_cffi==0.15.0` to `requirements.txt`.

**Verification:**
- 6 new unit tests (scrape success, HTTP error, missing body, exception,
  scrape→RSS fallback, image source preference).
- Smoke test: 13 paragraphs + 2 article images vs. 1 truncated RSS paragraph.

---

## Task 4: Lock the channel post format

**Status:** Done (spec only; code implementation pending)
**Commit:** (pending — documentation-only commit)
**Agent:** main agent
**Summary:** After iterating through ~15 test posts (msg 23–35 in
`@myhwchannel123`), we locked the final visual format. Key decisions:

1. **Minimal message body** — a single line `🔗 [domain](url)`. No title,
   no teaser, no "Читать полностью" link duplication.
2. **Preview card via `LinkPreviewOptions(url=telegra_ph, show_above_text=True)`**
   — Telegram renders domain label + title + excerpt + hero image + ⚡ INSTANT
   VIEW button natively. No `prefer_large_media`, no inline keyboard buttons,
   no `t.me/iv?url=...` rewrites (the last one opened in external browser on
   some clients).
3. **Decorated subtitle as Telegraph lead** — first text paragraph on the
   Telegraph page is `<p><i>💬 «{subtitle}»</i></p>`. The emoji + Russian
   guillemets survive plain-text flattening in the preview excerpt (where
   `<i>`/`<b>` tags are stripped).
4. **`<hr>` separator** between subtitle and body on Telegraph — visually
   divides the lead from the article on the page (the preview excerpt
   skips `<hr>` and flattens text, so the subtitle still flows into the
   body excerpt, but emoji+guillemets keep it distinct).
5. **Preview excerpt is not independently editable** — Telegraph lacks a
   `description`/`meta` field; Telegram derives the excerpt from the first
   ~300 chars of page content. Control the preview by controlling the
   first paragraph.

**Deviations from earlier iteration:** article-level summary (≤4000 chars,
originally requested) was rejected in favor of the shorter editorial
subtitle from the source site. "INSTANT VIEW" inline keyboard buttons were
tried and rejected (opened in browser on some clients, duplicated native
card button).

**Verification:**
- Reference post: msg 35 in `@myhwchannel123`.
- Spec documented in `work/telegraph-pipeline/post-format.md`.
- Feedback memory updated: `feedback_telegram_longread.md`.

---

## Task 5: Source-parser contract alignment

**Status:** Done
**Commit:** 21b616f
**Agent:** main agent
**Summary:**
- `telegraph_publisher.publish_article(..., subtitle='')` now prepends the
  decorated lead `p(italic("💬 «{subtitle}»"))` + `hr` before the body when
  a subtitle is provided; empty subtitle skips both.
- `news_bot.send_telegraph_teaser(telegraph_url, source_url)` emits the
  minimal one-line body `🔗 [{domain}]({url})` with
  `LinkPreviewOptions(url=telegraph_url, show_above_text=True)`. Dropped
  title, teaser, and `📖 Читать полностью` duplication. Removed the
  `make_teaser` helper — unused after the signature change.
- All three source parsers now include `subtitle` in their output dict:
  - Mattel: from `contentArticle.result.excerpt` in `__NEXT_DATA__`.
  - Autoevolution scrape: from `div.mgtop_10.mgbot_10.fsz19`.
  - Autoevolution RSS fallback: empty (no subtitle field in RSS).
  - Lamley: first `<p>` of `.entry-content`, lifted out of the body so it
    doesn't repeat below the decorated lead.
- Tests updated for the new signature and shape; new cases cover
  subtitle rendering and the empty-subtitle skip path.

**Verification:**
- `pytest tests/` — 96 passed.
- Smoke test through `news_bot.process_new_articles` on autoevolution
  Super Treasure Hunt: subtitle extracted + 13 translated paragraphs + 2
  images, Telegraph posted, channel card in the locked format.

---

## Reference posts for the locked format

- msg 35 — `@myhwchannel123/35`: final format, autoevolution Super
  Treasure Hunt. Subtitle `💬 «Прости меня, отец...»` + `<hr>` separator +
  full translated body + 2 inline images. Minimal channel body
  (`🔗 autoevolution.com`).

---

## Закрытие — 2026-08-04

Фича выкачена и работает; папка перенесена в `work/completed/`.

Первая работающая связка «статья → Telegra.ph → карточка в Telegram». Всё, что от неё осталось живого, давно перенесено в `telegraph_publisher.py` и описано в architecture.md § Data Flow; отдельного знания папка больше не несёт.
