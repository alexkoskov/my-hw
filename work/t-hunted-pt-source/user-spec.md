---
created: 2026-05-25
status: draft
type: feature
size: M
---

# User Spec: подключение t-hunted.blogspot.com как 4-го источника новостей (PT)

## Что делаем

Подключаем **t-hunted.blogspot.com** — бразильский блог про Hot Wheels — четвёртым источником в существующий pipeline. Используем тот же pipeline что и для остальных источников (RSS fetch → article scrape → boilerplate filter → LLM transcreation → Telegraph → Telegram). Единственное структурное отличие: язык оригинала — **португальский** (PT), а не английский. Для подписчика канала результат идентичен: статья на Telegra.ph + хештег-карточка в Telegram с Instant View превью.

## Зачем

Сейчас у канала 3 EN-источника: autoevolution, lamley, orangetrack. Они хорошо покрывают глобальную HW-сцену, но **пропускают бразильскую перспективу**:

- HW Brasil exclusives — линейки и эксклюзивы для бразильского рынка, которые часто появляются раньше глобального релиза (например, Pop Culture в Бразилии раньше, чем в США).
- Фото-обзоры региональных выпусков, не освещённые англоязычными блогерами.
- Другой редакторский угол на те же события мировой HW-сцены.

Цель — **расширение охвата контента**, а не просто увеличение частоты публикаций. Подписчик получает более полную картину сцены.

## Как должно работать

Сценарий с точки зрения подписчика канала:

1. Бразильский блогер публикует на t-hunted пост вида «Mais fotos da série Pop Culture 2026».
2. В ближайший cron-тик (10:00 МСК) бот по RSS-фиду t-hunted замечает новый URL.
3. Парсер скачивает HTML-страницу статьи, вытаскивает title, тело, картинки (3-5 типично).
4. Boilerplate-фильтр сносит Blogger-шаблонный мусор: `Marcadores:`, `Compartilhar no Facebook`, навигацию.
5. LLM (по умолчанию OpenRouter `openai/gpt-5.4-mini`) переводит PT → RU, применяя системный промпт с расширенной поддержкой PT, per-source style note для t-hunted и PT-EN-RU глоссарий HW-терминологии.
6. Telegra.ph публикует страницу с hero-картинкой и переводом. Telegram постит в канал карточку: `#thunted #news` + Instant View превью.
7. Подписчик видит превью в ленте, идентичное другим источникам по форме, и открывает Telegra.ph для чтения. Никакой пометки про PT-источник не делается — атрибуция через хештег.

## Критерии приёмки

- [ ] **AC1.** RSS-фид `https://t-hunted.blogspot.com/feeds/posts/default?alt=rss` добавлен в `feeds.json`. После добавления `feeds.json` содержит 4 записи (под cap=5), `feedparser` парсит фид без ошибок, t-hunted entries появляются в `_fetch_rss_entries` output.
- [ ] **AC2.** Новый модуль `t_hunted_source.py` экспортирует функцию `fetch_t_hunted_article(link, session=None, notifier=None) -> Optional[Dict]`, возвращающую `{title, subtitle, paragraphs, images}` (тот же контракт что у `fetch_lamley_article`) или `None` при ошибке.
- [ ] **AC3.** SSRF-allowlist в `t_hunted_source` жёстко ограничен `('t-hunted.blogspot.com',)`. Запрос на любой другой netloc возвращает `None` и шлёт админ-алерт.
- [ ] **AC4.** Article dispatcher в `news_bot.fetch_full_article` (строки 1414-1439) маршрутизирует через ветку `'blogspot.com' in domain` к `t_hunted_source.fetch_t_hunted_article`.
- [ ] **AC5.** `_resolve_source_name` возвращает `'t-hunted'` для netloc `t-hunted.blogspot.com`. `NETLOC_TO_SOURCE` расширена. `source_name='t-hunted'` доходит до LLM payload (`_llm_common._build_user_message`) без правок в `*_transcreation.py`.
- [ ] **AC6.** Telegram channel post для t-hunted-статей эмитит ровно `#thunted #news`. `_source_hashtag` патчится спец-кейсом для blogspot-поддомена либо через source-name override map. Регрессионный тест `tests/test_telegram.py::test_t_hunted_teaser_appends_news_tag` фиксирует формат.
- [ ] **AC7.** `ux-guidelines.md` обновлён в трёх местах: (а) строка 22 расширяет input-language assertion на «английский или португальский»; (б) новый блок `### 🟤 t-hunted` в секции «Per-source style notes» с заметками о тоне, голосе, типичной длине; (в) новая секция `## Glossary — PT/EN/RU` между «Per-source style notes» и «Red flags» с базовым словарём из 10 HW-терминов (Caça → Hunt, Super-T → Super Treasure Hunt, etc.).
- [ ] **AC8.** Boilerplate-фильтр расширен PT-секцией (~10 паттернов: `Compartilhar no`, `Marcadores:`, `Postado por`, `Leia mais`, `Postagens mais antigas`, etc.) в `_BOILERPLATE_PATTERNS`. Length-bound 120 chars + `^`-anchored regex обеспечивают защиту от false positives на EN/RU.
- [ ] **AC9.** Три новых admin-алерта в `admin_alerts.py`: `alert_t_hunted_host_rejected` (E031), `alert_t_hunted_fetch_error` (E032), `alert_t_hunted_no_body` (E033). Unit-тесты в `tests/test_admin_alerts.py`.
- [ ] **AC10.** Все три деплой-FILES-списка обновлены: `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml` — каждый содержит `"t_hunted_source.py"`. Pre-deploy QA вручную проверяет лоск-сtep.
- [ ] **AC11.** После 7 дней работы на test-инстансе оператор субъективно подтверждает: RU-перевод t-hunted-постов не хуже autoevolution baseline (на 5-10 примерах), нет [E031-E033] алертов в админ-чате, нет cross-language багов в выводе.

## Ограничения

**Хардовые «нет»:**

- **Не вводить headless browser** (Playwright / Selenium). Урок Mattel (PR #9): ~300MB зависимостей, замедление cron-тика, ради источника с редким HW-контентом — не оправдано.
- **Не вводить новых ENV-переменных.** Текущий `.env` уже плотный; вся конфигурация должна работать на существующем наборе.
- **Не перетряхивать структуру `SOURCES` registry в `news_bot.py`.** Только новая ветка в article dispatcher.
- **LLM-бюджет:** текущий ~$3/мес через OpenRouter (`openai/gpt-5.4-mini`). Допустимый рост от добавления t-hunted — до +25% (~$0.75/мес).
- **Без новых схем БД.** SQLite-таблицы (`processed_news`, `pending_articles`, `published_articles`, `failed_articles`, `bot_state`) остаются нетронутыми.
- **Без новых зависимостей.** Используем уже установленные `requests`, `beautifulsoup4`, `feedparser`.

**Известные ограничения, выходящие за scope:**

- **Cross-source content dedup** (одна и та же модель Hot Wheels освещена t-hunted и autoevolution в одну неделю) — не обрабатывается в этой фиче. Запланирована отдельная фича `cross-source-content-dedup` с собственным user-spec.

## Риски

- **Риск R1 (HIGH): Telegram отбрасывает дефис в хештеге** — `#t-hunted` отрендерится как `#t` + plain text `-hunted`. **Митигация:** хештег зафиксирован как `#thunted` в AC6, регрессионный тест `tests/test_telegram.py::test_t_hunted_teaser_appends_news_tag` лочит формат.

- **Риск R2 (HIGH): `_source_hashtag` по дефолту возвращает `#blogspot`** для netloc `t-hunted.blogspot.com` (берёт второй сегмент справа). **Митигация:** patch функции спец-кейсом для blogspot-поддомена либо source-name override map. Реализуется как часть AC6.

- **Риск R3 (MED): дедуп картинок** — Blogger хранит size variants в path (`=s1600` vs `=s640`), а lamley-логика дедупит только по query-string. **Митигация:** wait-and-see — стартуем с lamley-логикой, патчим если дубли появятся в первых 10 постах.

- **Риск R7 (MED): забыли модуль в одном из трёх FILES lists** — `news_bot.service` упадёт с ImportError на следующем cron-тике без CI-сигнала. **Митигация:** INVARIANT-комментарии в `deploy.sh` уже предупреждают; pre-deploy чеклист в AC10.

- **Риск R8 (MED): PT-EN-RU глоссарий — baseline-угадайка** до первых реальных публикаций. **Митигация:** code-researcher предложил 10-pattern starter; оператор уточняет после первых 5-10 переводов (как отдельный мини-PR), не блокирует выход фичи.

- **Риск R4 (LOW): длинные PT-эссе** в одном параграфе могут упереться в 4000-char per-paragraph cap. **Митигация:** существующий `_truncate_paragraphs` пишет WARN, не падает; мониторим первые 10 постов.

- **Риск R5 (LOW): PT-паттерны в boilerplate применяются глобально** (ко всем источникам) — нет language tagging в `_BOILERPLATE_PATTERNS`. **Митигация:** length-bound 120 chars + `^`-anchored regex дают защиту от false positives — точно та же модель что у RU-патернов с 2026-05-08.

## Технические решения

- **Хештег зафиксирован как `#thunted`**, потому что Telegram не поддерживает дефис в хештеге (рендерится как два отдельных токена), а `#tHunted` неудобен на мобильной клавиатуре, и `#t_hunted` выбивается из стиля канала (`#autoevolution`, `#lamleygroup`).

- **Парсер пишем по шаблону `lamley_source.py`** (минус WAF/throttle/cooldown apparatus), потому что HTML-структура Blogger ближе всего к lamley (`<div class="post-body entry-content">`), и Blogger не использует Cloudflare против скрейпинга. Получится ~150 строк против lamley'евских 380. autoevolution overkill (Cloudflare bypass + blocks generation для JS-DOM); orangetrack тяжелее (700+ строк, aggregator pattern, WP-block-embed handling).

- **Эмодзи для t-hunted = 🟤** — единственный «тёплый» круг, не конфликтующий с уже используемыми цветами (🟠 autoevolution / 🔵 lamley / 🟡 mattel / 🟣 orangetrack / 🟢 prod-info / 🔴 error / etc.). Используется только в архивном `hw_review.py`, cosmetic.

- **PT-паттерны добавляем глобально в `_BOILERPLATE_PATTERNS`** (без language tagging), потому что length-bound 120 + `^`-anchored regex даёт ту же защиту от false positives что у RU-патернов с 2026-05-08, и операторская точка ввода более легковесная.

- **Не вводим language tagging в `feeds.json`.** `_resolve_source_name` инфериует source из netloc, а LLM сам определяет PT-язык из контента — единственное место где захардкожен EN-input, это `ux-guidelines.md:22`, и оно расширяется.

- **Три dedicated admin-алерта (E031-E033)**, а не один generic `[E002] alert_source_fetch_failed`. Lamley имеет 4 (host/size/fetch/no-body) — три достаточно для триажа без перебора, у нас нет size guard (Blogger не отдаёт огромные страницы) поэтому E028-аналог не нужен.

## Тестирование

**Unit-тесты:** делаются всегда, не обсуждаются. Конкретно:
- Новый файл `tests/test_t_hunted_source.py` (~80 строк, mirror lamley's `TestFetch* + TestHostAllowlist`).
- Расширение `tests/test_sources_registry.py` (`NETLOC_TO_SOURCE` + `_resolve_source_name`).
- Расширение `tests/test_telegram.py` (`test_t_hunted_teaser_appends_news_tag` — лочит `#thunted`).
- Расширение `tests/test_boilerplate_filter.py` (`test_portuguese_patterns_filtered` параметризованный класс).
- Расширение `tests/test_admin_alerts.py` (builders E031-E033).

**Интеграционные тесты:** делаем — smoke pipeline тик с одним EN + одним PT entry в одном `job()` run, проверяет dedup, slot ordering, что обе публикации выходят без cross-language interference.

**E2E тесты:** не делаем — оператор визуально верифицирует первые публикации на test-канале `@myhwchannel123` через стандартный `git push dev` → `deploy_test.yml` цикл. Размер фичи M, нет critical user flows которые требуют автоматизации браузерных проверок.

## Как проверить

### Агент проверяет (pre-deploy gate)

| Шаг | Инструмент | Ожидаемый результат |
|-----|-----------|-------------------|
| 1. Unit-тесты для t-hunted-парсера | `pytest tests/test_t_hunted_source.py -v` | Все зелёные: TestFetch (parse_title, parse_body, image_limit, http_error) + TestHostAllowlist |
| 2. Все остальные тесты по-прежнему зелёные | `pytest tests/ -q` | 933+ passed (текущий baseline) + новые тесты |
| 3. `feedparser` парсит t-hunted RSS | `python -c "import feedparser; print(len(feedparser.parse('https://t-hunted.blogspot.com/feeds/posts/default?alt=rss').entries))"` | ≥ 1 entry, нет critical exception |
| 4. SSRF-allowlist жёсткий | `pytest tests/test_t_hunted_source.py::TestHostAllowlist -v` | Запрос на `evil.example.com` возвращает None + ping |
| 5. Хештег зафиксирован | `pytest tests/test_telegram.py::test_t_hunted_teaser_appends_news_tag` | Зелёный, формат `#thunted #news` |
| 6. Все три FILES-списка содержат модуль | `grep -l "t_hunted_source.py" deploy.sh .github/workflows/deploy.yml .github/workflows/deploy_test.yml` | Все три файла в выводе |
| 7. CI на PR зелёный | GitHub Actions / CI workflow | Зелёный статус CI на PR в `dev` |

### Пользователь проверяет (post-deploy на test-канале)

- **Hero-картинка в превью** — открыть Telegram, найти первую t-hunted публикацию в `@myhwchannel123`, убедиться что preview card отрисован с фотографией. (Зачем: визуальная проверка не покрывается автотестами — Telegram-side рендеринг.)
- **Хештег рендерится как `#thunted`** (без обрезания) — кликабельный, открывает фильтр по тегу. (Зачем: Telegram-side validation хештег-формата.)
- **Telegraph-страница на чистом русском** — открыть `⚡ INSTANT VIEW` карточку, прочитать body, убедиться что нет PT-фраз в выводе. (Зачем: real-world проверка LLM PT→RU транскреации, нельзя автоматизировать без человеческого языкового восприятия.)
- **Emoji prefix на title** — должен быть один из 🏆 / 🏎️ / 🚀 / 💎 / 🤝 / 📢 / 🚗 / 🔥. (Зачем: safety net в `_apply_emoji_safety_net` — визуальная проверка что fallback не сбросил префикс.)
- **Через 7 дней работы:** оператор спот-чекает 5-10 первых t-hunted переводов против оригиналов на t-hunted.blogspot.com, оценивает RU-качество не хуже autoevolution baseline. (Зачем: AC11 — subjective quality gate перед promotion в prod.)
- **Нет [E031-E033] алертов** за 7 дней в админ-чате `@sunny413x`. (Зачем: AC11 — стабильность парсера на реальном feed'е.)
