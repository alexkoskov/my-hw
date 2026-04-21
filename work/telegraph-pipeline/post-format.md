# Post Format Standard

Locked 2026-04-21. Visual reference: channel post msg 35 in `@myhwchannel123`.
Applies to every source the bot publishes (autoevolution, Mattel, Lamley, and
any future site).

---

## Telegram channel post

One line of text + a Telegraph preview card above it. That's it — no title, no
teaser, no extra links in the message body. The preview card carries the
visible content and Telegram renders the ⚡ INSTANT VIEW button on it natively.

**Message body (Markdown):**
```
🔗 [{source_domain}]({source_url})
```

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
- Hashtags (`#autoevolution`, `#HotWheels`, etc.) or topic tags.
- Decorative separators (`──────────`, `• • •`).
- Inline keyboard buttons (including `t.me/iv?url=...&rhash=11` buttons).
- `prefer_large_media=True` — changes layout in a way we decided not to keep.
- `send_photo` with caption — 1024-char cap kills long content and disables
  the link preview.

---

## Telegraph page

Built via `api.telegra.ph/createPage`. Node order is:

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
6. **Source footer** — `p(italic("Источник: "), a(source_url, source_url))`.

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
}
```

Sources without a native subtitle: fall back to an empty string — the
Telegraph page will skip step 2 and step 3 (no decorated lead, no separator).
