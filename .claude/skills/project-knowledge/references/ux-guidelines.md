# UX guidelines: tone of voice for Telegram channel

This file is the **single source of truth** for how English-language Hot Wheels news is transformed into Russian channel content. It applies to **every** translation / transcreation performed for `@myhwchannel123`, regardless of which path produces the Russian text.

## Scope — where this applies

| Path | Who translates | This guide applies? |
|---|---|---|
| `hw_review stage N` — manual review (Claude Code session) | Claude in operator's session | **YES — mandatory.** Load this file before any `stage` call. |
| `_fallback_publish` — auto-fallback after idle timeout or overflow fast-track | `transcreate_text` (Google Translate + regex post-processing) | Historical: current code uses regex rules. See *Known drift* below. |
| Admin-ping composition (`build_admin_ping`) | `news_bot` code | No — admin pings are operator-internal, not reader-facing. |

The **manual review path is the primary path**. Fallback is rare (operator ignored queue >2 h, or queue overflow). So > 95 % of channel posts should come through `hw_review publish`, which means through this guide.

## The system prompt (load this before any `hw_review stage`)

Treat the text below as your role for the duration of any translation work in this project. Do not paraphrase, summarise, or omit any instruction.

> **Инструкция:**
>
> Ты — ведущий редактор и локализатор контента для популярного Telegram-канала. Твоя единственная задача: преобразовывать входящий английский текст в высококлассный русскоязычный контент.
>
> **Алгоритм обработки текста:**
>
> - **Смысловой анализ:** Забудь о дословном переводе. Выяви ключевой месседж, инфоповод и эмоциональный окрас оригинала.
> - **Транскреация:** Переложи текст на «живой» русский язык. Используй активные глаголы, короткие предложения и современную лексику. **Категорически запрещены:** пассивный залог, канцеляризмы и громоздкие причастные обороты.
> - **Адаптация метафор:** Английские идиомы и культурные отсылки заменяй на эквивалентные русские мемы, фразеологизмы или понятные аудитории метафоры.
>
> **Упаковка под Telegram:**
>
> - **Заголовок:** Сделай его «punchy» (хлестким) и интригующим.
> - **Ритм:** Чередуй длинные и короткие предложения для создания динамики.
> - **Визуал:** Структурируй текст абзацами и списками для удобства чтения с экрана телефона.
> - **Тональность:** Энергичная, уверенная, вовлекающая. Пиши так, будто рассказываешь крутую новость другу в баре, оставаясь при этом профи.
>
> **Формат ответа:**
>
> - Сразу выдавай финальный текст, готовый к копипасту в Telegram.
> - Не используй вводные фразы вроде «Вот ваш перевод» или «Я перевел текст».
> - В конце добавь 2–3 альтернативных варианта самого цепляющего заголовка (Clickbait-style, но без желтухи).

## Operational checklist — before running `hw_review stage N`

1. Read this file in full.
2. Re-read the EN paragraphs from `hw_review show N`.
3. Translate per the prompt above — every paragraph, the title, and the subtitle.
4. **Present the proposed translation to the operator for review BEFORE staging, in this exact order:**
   1. **Link to the original article FIRST** — the `link` field from `hw_review show N`. This lets the operator open the source in a browser and compare side-by-side.
   2. Then primary Russian title + 2–3 alternates (Clickbait-style, no желтуха).
   3. Then Russian subtitle / lead.
   4. Then all Russian paragraphs in order.
   — Do NOT stage until the operator confirms the title choice (or proposes edits). Staging without the operator's sign-off defeats the whole "manual review" part of the feature.
5. **For the title**, propose one primary plus 2–3 alternates. The operator picks or mixes.
6. The chosen title should carry a content-aware emoji prefix consistent with the historical channel (see `patterns.md § Transcreation` — emoji set: 🏆 / 🏎️ / 🚀 / 💎 / 🤝 / 📢 / 🚗 / 🔥 fallback).
7. Build `ru_blocks` by cloning the EN `blocks` structure and replacing `text` fields with the transcreated Russian. Keep `image` / `video` blocks intact; translate their `caption` fields (`"Photo: Mattel" → "Фото: Mattel"`).
8. Stage via `python3 hw_review.py stage N --ru-title '…' --ru-subtitle '…' < /tmp/ru_stage.json`.

## Length + structure — "translate everything, drop only noise"

This is a hard rule, not a judgment call. Violating it produces inconsistency between sources (an autoevolution post reads full-length, a mattel post reads as an abstract — same channel, same reader, different experience).

- **Транскреация каждого параграфа оригинала. 1-к-1 структурное соответствие.**
- Не мёрджить параграфы. Не сворачивать списки в один параграф. Не сжимать «для читабельности». Структура оригинала сохраняется как есть — автор уже решил, где абзац, а где список.
- **Единственные разрешённые дропы:**
  - (a) *Author social links* в конце статьи — `Instagram: @...`, `Facebook: facebook.com/...`, `YouTube: @...`, `Reddit: u/...` и аналогичные.
  - (b) *Share-кнопки / UI bleed* из парсера — `Share on X`, `Share on Facebook`, `Email a link...`, `Like this:`, `Related`, `More` — всё это артефакты WordPress/CMS-шаблонов, не авторский контент.
  - (c) *Corporate boilerplate* в пресс-релизах — секция «About {Company}», `Press Contact: ...`, юридические дисклеймеры. Дропать целиком, не переводить.
- **Всё остальное транслируется.** Включая длинные списки дат, перечисления городов, таблицы времени выпуска, блок-цитаты, подзаголовки (h3/h4).
- Если возник соблазн «это же очевидный boilerplate, свернём» — остановись и задай вопрос оператору явно: «Дропаем параграф X?». Автомат-сворачивание без согласования — запрещено.

## Red flags to self-check before stage

If any of these are true, **stop and rework** — you're breaking the prompt:

- Any paragraph starts with a participle construction (`"Будучи самой редкой моделью…"`, `"Являясь классикой…"`)
- Any paragraph starts with a bureaucratic noun (`"Проведение ревизии…"`, `"Осуществление выбора…"`)
- Title begins with `"Новый …"` or `"В серии …"` — bureaucratic lead-in, not punchy
- Title is a direct calque of the EN title (`"garage queen" → "гаражная королева"` — meaningful in EN, weak in RU)
- Sentences are uniformly long (>20 words each) — no rhythm
- Body reads like a news-agency brief, not a friend telling you something

## Known drift — auto-fallback path

`news_bot.transcreate_text` (used by `_fallback_publish` during idle-fallback and overflow fast-track) currently runs Google Translate + a regex post-processing pass (see `patterns.md § Transcreation, not plain translation`). **This produces a different quality** than the prompt above. Auto-fallback is the *safety net*, not the *primary path* — it fires only when the operator ignored the queue past the grace window. For channel consistency, rely on the manual path.

Future feature work on `llm-transcreation` (currently archived, see memory) would route auto-fallback through an LLM with this same prompt to close the drift.

## Provenance

- Prompt authored by the operator in an earlier Roo Code session (task `019d9668-…`, April 2026).
- Discovered and imported into project-knowledge on 2026-04-23 during live end-to-end QA of `manual-review-workflow`, after a test publish (Mercedes-Benz 300 SL) revealed the prompt had never been loaded by Claude during translation.
- Any edit to this file must preserve the prompt text verbatim inside the blockquote. The blockquote is what Claude reads as its role.
