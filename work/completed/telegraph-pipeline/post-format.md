# Post Format Standard

Locked 2026-04-21, message-body form revised 2026-04-22 (hashtag replaces
`🔗 [domain](url)`). Visual reference: channel post msg 35 in
`@myhwchannel123` for the preview card / Telegraph page; current message
body is `#{source_label}`. Applies to every source the bot publishes
(autoevolution, Mattel, Lamley, and any future site).

---

## Telegram channel post

One line of text + a Telegraph preview card above it. That's it — no title, no
teaser, no extra links in the message body. The preview card carries the
visible content and Telegram renders the ⚡ INSTANT VIEW button on it natively.

**Message body (Markdown):**
```
#{source_label}
```

`source_label` is the second-level domain of the source URL (no TLD, no
`www.`). Derived in [news_bot._source_hashtag](../../news_bot.py):
`autoevolution.com` → `#autoevolution`, `corporate.mattel.com` → `#mattel`,
`lamleygroup.com` → `#lamleygroup`.

**Send parameters:**
```python
await bot.send_message(
    chat_id=CHANNEL_ID,
    text=text,
    parse_mode='Markdown',
    link_preview_options=LinkPreviewOptions(
        url=telegraph_url,
        show_above_text=True,
    ),
)
```

### Rejected variants

Do not add these (tried and rejected during the 2026-04-21 iteration):

- Duplicated title and teaser below the preview card — preview already shows them.
- `📖 Читать полностью` link in the body — the card IS the "read more" affordance.
- `🔗 [domain](url)` linked-domain form — replaced by the source hashtag
  (commit `b7256de`), which makes the attribution tappable as a channel filter.
- Per-article topic tags (`#HotWheels`, `#release`, `#legends`) — only the
  source hashtag.
- Decorative separators (`──────────`, `• • •`).
- Inline keyboard buttons (including `t.me/iv?url=...&rhash=11` buttons).
- `prefer_large_media=True` — changes layout in a way we decided not to keep.
- `send_photo` with caption — 1024-char cap kills long content and disables
  the link preview.

---

## Telegraph page

Built via `api.telegra.ph/createPage`. Two rendering paths live in
[telegraph_publisher.py](../../telegraph_publisher.py):

**Block path** (`_build_content_from_blocks`, preferred) — used when the
source parser returns an ordered `blocks` list so image/video positions
from the source article are preserved. Autoevolution uses this path.

**Flat path** (`_build_content`, fallback) — used when the parser only
emits flat `paragraphs` + `images` lists. Mattel, Lamley, and the
autoevolution RSS fallback use this.

Node order (flat path):

1. **Hero image** — `figure(first_image)`. Becomes the preview card thumbnail.
2. **Decorated subtitle** — `p(italic("💬 «{translated_subtitle}»"))`.
   - The subtitle is the editorial lead from the source site
     (autoevolution: `div.mgtop_10.mgbot_10.fsz19`; Mattel: `excerpt` in
     `__NEXT_DATA__`; Lamley: first `<p>` in `.entry-content`).
   - The `💬` emoji + Russian guillemets («») survive plain-text flattening
     in the preview card excerpt, where `<i>`/`<b>` tags are stripped.
3. **Horizontal separator** — `hr`. Visible rule between the lead and the body.
4. **Body paragraphs** — `p(translated_paragraph)` in original order.
   Every 3rd paragraph interleaves the next image from the source as `figure`.
5. **Trailing images** — any remaining source images appended as `figure`.
6. **Source footer** — `p("Источник: ", a(source_url, source_url))`.

Block path adds:

- **Lead block** — `p(bold(text))` for source-highlighted intro paragraphs
  (autoevolution `div.sanscond.fsz22.bold`).
- **Headings** — `h3`/`h4` preserved from the source.
- **Inline images with captions** — `figure(img, figcaption(caption))` where
  the source provides one (autoevolution `div.ch_pic_crd`).
- **Video embeds** — `iframe` pointing at `telegra.ph/embed/youtube?url=…`
  or `…/embed/vimeo?url=…` (raw YouTube/Vimeo URLs are rejected by
  Telegra.ph's iframe validator; the proxy form is mandatory).

Full Russian translation only — no article-level summary, no generated
abstract. The subtitle IS the editorial lead.

---

## Preview excerpt behavior

Telegraph has no `description` or `meta` field. Telegram derives the preview
card excerpt from the Telegraph page's first ~300 characters of text,
skipping `figure` and `hr` nodes and concatenating `<p>` content with spaces.
Whatever you put at the top of the Telegraph content appears in both the
page AND the preview card.

Consequences:
- Control the preview by controlling the first paragraph's content and length.
- Markdown/HTML formatting (italic, bold) in the excerpt is flattened to
  plain text — use visible character markers (emoji, guillemets) to create
  visual structure that survives flattening.
- A decorated subtitle (`💬 «...»`) both hooks readers and visually separates
  the lead from the body text that follows in the excerpt.

---

## Required source-parser contract

Every per-source parser (`fetch_*_article`) must return this shape:

```python
{
    'title': str,                 # source article headline
    'subtitle': str,              # editorial lead from the site; '' if none
    'paragraphs': list[str],      # body in reading order, already stripped
    'images': list[str],          # absolute URLs in reading order
    'blocks': list[dict],         # OPTIONAL — ordered content blocks
}
```

Sources without a native subtitle: fall back to an empty string — the
Telegraph page will skip step 2 and step 3 (no decorated lead, no separator).

`blocks` is optional but preferred. When present, `telegraph_publisher`
uses `_build_content_from_blocks` to render media at its source positions;
when absent, it falls back to `_build_content` with the flat
`paragraphs`/`images` lists. Block shapes:

```python
{'type': 'paragraph', 'text': str, 'runs': list[dict]}   # text + [{text, href?}] metadata
{'type': 'lead', 'text': str, 'runs': list[dict]}        # bold intro
{'type': 'heading', 'text': str, 'level': 3|4, 'runs': list[dict]}
{'type': 'image', 'src': str, 'caption': str}
{'type': 'video', 'src': str}                            # telegra.ph/embed/… proxy URL
```

Only autoevolution emits `blocks` today. Mattel and Lamley use flat
`paragraphs`/`images` — the RSS fallback also uses the flat form.
