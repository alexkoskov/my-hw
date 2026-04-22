---
created: 2026-04-22

status: draft

type: feature

size: M
---

# User Spec: manual-review-workflow

## Что делаем

Разделяем пайплайн бота на две независимые фазы. **Prep-фаза** (автоматическая, на cron) фетчит источники, дедуплицирует и складывает каждую новую статью в очередь на ревью — в канал ничего не публикует. **Review + publish фаза** (ручная, через Claude Code + новый CLI `hw_review.py`) даёт оператору прочитать английский оригинал, написать русский пересказ вместе с Claude Code, увидеть превью на Telegraph и по команде опубликовать пост в канал в уже залоченном формате. Для случаев, когда оператор недоступен, сохраняется деградированный автоматический путь через существующий `transcreate_text` (Google Translate).

## Зачем

Текущий полностью автоматический флоу печатает в канале машинный русский (Google Translate + regex-замены): кальки, неправильные согласования, неточно переведённые термины Hot Wheels. В живом прогоне 22 апреля 2026 на Mattel-статье это выглядело как «Тур Legends», «великий чемпион увидит, как его творение будет создано», «в основной линейки». Оператор хочет ревьюить каждый пост с качеством, которое Claude Code даёт прямо в интерактивной сессии, — при этом без оплаты OpenAI API (нет желания платить за токены сегодня) и без смены стека (бот остаётся Python-ом, никакого n8n). Max-подписка Claude, которой пользуется оператор, уже покрывает переводчик. Бот из end-to-end автомата превращается в подготовщика очереди и публикатора благословлённого контента, с страховочной сеткой на случай отлучки оператора.

## Как должно работать

**Утренний happy-path:**
1. В 12:00 cron запускает `job()`. Prep-фаза фетчит autoevolution RSS + Mattel corporate + Lamley, дедуплицирует по `processed_news`, для каждой новой ссылки зовёт `fetch_full_article` и кладёт результат в новую таблицу `pending_articles` с пустыми `ru_*`-полями.
2. Если очередь после прогона непустая, бот шлёт админу в Telegram пинг в жёстком формате: `"3 ждут review: 🟠 autoevolution ×2, 🟣 mattel ×1"`. Источники с нулём новых записей в строку не попадают.
3. Оператор в удобный момент открывает Claude Code в папке `my-hw`, говорит триггер-фразу **«посмотрим очередь»**. Claude запускает `hw_review list`, показывает пронумерованные строки (источник, заголовок, число параграфов, возраст в часах). Если в `failed_articles` что-то накопилось — внизу вывода появляется футер `⚠️ 2 неопубликованных в failed: [titles]. hw_review retry N чтобы переподнять.`.
4. Оператор выбирает запись; Claude зовёт `hw_review show N` — дампит английский оригинал, читает, пишет русский пересказ прямо в чате. Оператор даёт правки («сократи», «оставь корпоративку», «добавь ироничности»); Claude переписывает.
5. По готовности Claude сохраняет перевод: `hw_review stage N` получает финальные `ru_title`, `ru_subtitle`, `ru_paragraphs` (и `ru_blocks` для autoevolution) и обновляет строку.
6. Claude запускает `hw_review preview N` — это создаёт draft-страницу на Telegra.ph и возвращает URL. Оператор открывает ссылку в браузере, проверяет вёрстку (hero-картинка, subtitle с `💬 «…»`, параграфы, блоки).
7. Оператор говорит «публикуй». Claude зовёт `hw_review publish N`. Команда: (а) создаёт финальную Telegraph-страницу через `createPage`; (б) шлёт в канал `@myhwchannel123` пост с хэштегом источника и Instant View превью; (в) если `preview_path` был сохранён — обнуляет draft через `editPage`; (г) переносит строку из `pending_articles` в `published_articles` с флагом `via_review=true`; (д) пишет ссылку в `processed_news` (dedup).
8. Цикл повторяется для остальных записей в очереди.

**Fallback-путь (если оператор не пришёл):**
- Запись простояла в `pending_articles` >48 часов. На ближайшем cron-tick fallback-пасс шлёт админу `"Will auto-publish in <grace>: [titles]. Intercept via hw_review take N"` и проставляет `notified_at`.
- Если за окно grace оператор вызвал `hw_review take N`, `notified_at` снимается, статья идёт через обычный review.
- Если нет — бот зовёт `transcreate_text` на original paragraphs, публикует через тот же публиковочный код (Telegraph + Telegram), переносит в `published_articles` с `via_review=false`.

**Overflow-путь (очередь забита):**
- Prep-фаза обнаружила: `len(pending_articles) + new_count > 10`. Отбирает самые старые pending-записи, прогоняет их через `transcreate_text` + полный publish-цикл, освобождает слоты. Админу пинг: `"Queue pressure: auto-published 2, added 3 new"`. Освобождённые уходят в `published_articles` с `via_review=false`.

**Retry-путь (ручное спасение failed):**
- Оператор видит футер `failed_articles` в `hw_review list`.
- Вызывает `hw_review retry N` — строка возвращается из `failed_articles` в `pending_articles` с `attempt_count=0`, `status='pending'`, свежим `fetched_at`.
- Следующий cron-tick прогоняет её через fallback-пасс. Если GT снова падает три раза подряд — снова в `failed_articles`.

## Критерии приёмки

- [ ] Prep-фаза в `job()` больше не публикует в канал напрямую — ни одного вызова `send_telegraph_teaser` по ходу prep-цикла.
- [ ] Каждая новая запись из каждого источника (autoevolution RSS, Mattel corporate, Lamley) сохраняется в `pending_articles` с заполненными `title`, `subtitle`, `paragraphs-JSON`, `images-JSON`, `blocks-JSON` (если источник отдаёт), `pub_date`, `fetched_at`.
- [ ] Админ-пинг отправляется **только когда очередь непустая**, в формате `"N ждут review: 🟠 autoevolution ×K, 🟣 mattel ×M, 🟢 lamley ×L"`. Источники с нулём новых в строку не попадают.
- [ ] `hw_review publish N` генерирует канальный пост визуально идентичный текущему автоматическому флоу (тот же locked post format из `work/telegraph-pipeline/post-format.md` — хэштег в теле + Instant View preview).
- [ ] `hw_review skip N` при непустом `ru_*` требует явного подтверждения y/N. Скипнутая ссылка пишется в `processed_news` и больше не всплывает при новых прогонах RSS.
- [ ] Запись, простоявшая >48h без действия оператора, получает админ-пинг heads-up и (после окна grace) автопубликацию через `transcreate_text`. Запись уходит в `published_articles` с `via_review=false`.
- [ ] Очередь никогда не превышает 10 pending-записей. При переполнении самые старые graduated автоматически через GT-path, перед добавлением новых.
- [ ] Telegraph drafts, созданные в ходе `hw_review preview`, обнуляются через `editPage` при финальной публикации или явном `skip`. `hw_review cleanup-drafts` вручную подчищает то, что не удалось в моменте.
- [ ] `hw_review list` всегда показывает футер с `failed_articles` (если непустая): `"⚠️ K неопубликованных в failed: [titles]. hw_review retry N чтобы переподнять."`.
- [ ] `hw_review retry N` возвращает строку из `failed_articles` в `pending_articles` с `attempt_count=0`, удаляет исходную failed-строку.
- [ ] Все 106 существующих тестов зелёные после рефакторинга `job()`.
- [ ] Новый test suite покрывает: CRUD для трёх новых таблиц, парсинг аргументов CLI, формат admin-ping, prep-фазу end-to-end с мок-источниками, publish-фазу end-to-end с мок-Telegraph/Telegram, миграцию (пустая БД → все 4 таблицы), idle-fallback с grace-окном, overflow с освобождением слотов, skip с `ru_*` и retry из failed.
- [ ] CI-гейт на каждом PR прогоняет `pytest tests/` включая новый `test_migration.py`, который на tempfile-БД после `init_db()` проверяет существование всех 4 таблиц с ожидаемыми колонками.

## Ограничения

- Бот остаётся на Python. Миграция в n8n отвергнута 2026-04-22 из-за стоимости переписывания Cloudflare-bypass для autoevolution и трёх существующих парсеров.
- В этой фиче **нет** внешних LLM-API — ни OpenAI, ни Anthropic API. Переводчик — это Claude Code-сессия оператора под Max-подпиской. Feature `llm-transcreation` (заменитель Google Translate на OpenAI) вынесена в будущую работу, `work/archived/llm-transcreation-deferred/`.
- Locked post format из `work/telegraph-pipeline/post-format.md` не изменяется: тот же хэштег источника, тот же `LinkPreviewOptions(show_above_text=True)`, та же структура Telegraph (hero → italic subtitle → `<hr>` → параграфы → футер с источником).
- Существующая таблица `processed_news` не меняет схемы; её семантика расширяется с «что опубликовано» до «что видели» (any link that was published OR skipped OR historically processed).
- Cron продолжает бежать как минимум раз в сутки в 12:00 (базовая частота). Возможный bump до часовой гранулярности — осознанный 1-строчный follow-up, решение отложено до tech-spec.
- Queue hard cap — 10 одновременных записей в `pending_articles`. Жёсткое ограничение.
- Idle timeout — 48 часов от `fetched_at` до heads-up админ-пинга. Окно grace до auto-публикации — точное значение определится в tech-spec (зависит от выбранной частоты cron).
- Триггер для Claude Code — фраза «посмотрим очередь». Intercept команда — `hw_review take N`.
- Preview — Telegra.ph draft создаётся **только при явном `hw_review preview N`**. Не при stage, не при INSERT. Если preview пропущен, publish не делает лишнего `editPage`.
- Discarded drafts обнуляются через `editPage` (Telegra.ph API не имеет `deletePage`).
- Никаких новых зависимостей в `requirements.txt`. CLI на stdlib `argparse`, БД на stdlib `sqlite3`.
- `news_bot.main()` сохраняет немедленный прогон `job()` при старте ([news_bot.py:461](../../news_bot.py#L461)). Побочный эффект в день деплоя: два админ-пинга (один сразу, второй по расписанию) — осознанная простота вместо специальной логики подавления.

## Риски

- **Рефакторинг `job()` ломает 8 существующих auto-publish тестов** в `test_integration.py`, `test_mattel_integration.py`, `test_feed_iteration.py`. **Митигация:** переписываем в том же PR; green-run по всему tests/ перед merge.
- **Bump частоты cron с дневного на часовой увеличивает число запросов к RSS/Mattel.** **Митигация:** дефолт остаётся дневной, bump делаем только по явному решению; `fetch_full_article` cached внутри одного прогона; `curl_cffi` для autoevolution не меняется.
- **Telegra.ph `editPage` API может измениться или исчезнуть.** **Митигация:** `cleanup-drafts` ловит ошибки и алертит админу; фолбэк-поведение — «оставить draft как есть, пусть оператор подчистит руками».
- **`publish_article` меняет возвращаемый тип с `str` на `dict {url, path}`, что ломает `test_telegraph_publisher`.** **Митигация:** фикс тестов в том же PR; внешних потребителей модуля нет.
- **Оператор отсутствует >48h и `failed_articles` накапливается из-за хиккапов Google Translate.** **Митигация:** футер в `hw_review list` на каждом вызове делает backlog видимым; `hw_review retry N` — прямой путь к ручному разрешению.
- **`news.db` повреждается (редкий crash SQLite или ошибка в обращении).** **Митигация:** RSS при следующем прогоне повторно поднимает все необработанные записи; потеря только на `ru_*` черновики — переписать заново из Claude Code дешево.
- **Бот переезжает из локального Docker на VPS в середине разработки.** **Митигация:** фича hosting-agnostic — код одинаково работает в любом окружении с `news.db` на файловой системе.

## Технические решения

- Мы решили **держать `transcreate_text` как legacy-fallback**, потому что он закрывает idle-таймаут и overflow-очистку без добавления LLM-зависимости. Удалять рано.
- Мы решили **завести три новых таблицы (`pending_articles`, `published_articles`, `failed_articles`)** вместо расширения одной `processed_news`, потому что семантика разная: очередь, аудит, мёртвый ящик — у каждой свои колонки и запросы.
- Мы решили **хранить скипнутые ссылки только в `processed_news`**, не в `published_articles`, потому что скип — не публикация; аудит публикаций должен оставаться чистым.
- Мы решили **переместить Mattel-источник в реестр `SOURCES`** вместе с RSS-фидами, потому что сейчас он зашит в `job()` и это раздражает при добавлении новых источников; сделать реестр дешевле, чем поддерживать hardcoded вызов.
- Мы решили **создавать Telegraph draft только в `hw_review preview N`**, не на prep или stage, потому что (а) не каждая статья требует превью, (б) экономим количество Telegraph-страниц, (в) draft живёт минимально короткий промежуток — от preview до publish/skip.
- Мы решили **не подавлять немедленный `job()` на старте контейнера**, потому что один лишний админ-пинг в день деплоя — приемлемая цена за отсутствие специальной логики startup-режима.
- Мы решили **не делать отдельного staging-окружения**, потому что фича в момент деплоя перестаёт автопубликовать — риск «залить канал мусором при rollout» структурно отсутствует.
- Мы решили **добавить `hw_review retry N`, который перекладывает строку из failed в pending**, а не запускает GT синхронно в CLI, потому что команда должна быть быстрой и не блокировать на сетевых вызовах.
- Мы решили **изменить возврат `publish_article` с `str` на `dict {url, path}`**, потому что `editPage` требует `path` (парсить path из URL хрупко — slug генерируется с date-суффиксом).
- Мы решили **не делать отдельную retention/auto-purge для `published_articles` и `failed_articles` в этой фиче**, потому что объём низкий (до 100 записей в месяц) и ручной `sqlite3` с DELETE WHERE failed_at < date('now', '-90 day') достаточен.

## Тестирование

**Unit-тесты:** делаются всегда, не обсуждаются.

**Интеграционные тесты:** делаем. Фича затрагивает пайплайн end-to-end и переворачивает поведение 8 существующих интеграционных тестов (из `test_integration.py`, `test_mattel_integration.py`, `test_feed_iteration.py`). Нужно подтвердить новыми тестами: prep-фаза end-to-end с мок-источниками (только staging в БД, никаких вызовов в Telegram/Telegraph), publish-фаза end-to-end с мок-Telegraph/Telegram, idle-fallback симуляция (стэйлим `fetched_at`, запускаем cron-tick), overflow (заполняем очередь до 10, следующий prep прогоняет старые через GT).

**E2E тесты:** не делаем отдельно. Smoke-проверка после деплоя — публикация одной статьи через полный review flow с визуальным сравнением с msg 35 в `@myhwchannel123` — закрывает E2E без автоматического фреймворка.

## Как проверить

### Агент проверяет

| Шаг | Инструмент | Ожидаемый результат |
|-----|-----------|-------------------|
| 1. Прогнать `pytest tests/` | bash | 106 старых + все новые тесты зелёные, 0 failures |
| 2. Запустить prep-фазу на тестовой БД | bash `python -m news_bot` в контейнере с мок-env | В `pending_articles` N строк; `processed_news` пополнен; ни одного вызова `send_telegraph_teaser` |
| 3. Прочитать последнее сообщение в админ-чате | Telegram MCP | Формат `"N ждут review: 🟠 autoevolution ×K, 🟣 mattel ×M, 🟢 lamley ×L"`, без нулевых источников |
| 4. Пройти review-flow на одной записи через CLI | bash `python hw_review.py list` → `show` → `stage` → `preview` → `publish` | preview-URL корректный, после `publish` запись исчезает из `pending_articles`, появляется в `published_articles` с `via_review=true`, draft через GET возвращает «(draft discarded)» |
| 5. Симулировать idle >48h на тестовой записи | bash + SQL: `UPDATE pending_articles SET fetched_at=date('now','-3 day')` → вручную вызвать fallback-пасс | Админ-пинг пришёл с heads-up, через grace-окно запись авто-опубликована, строка в `published_articles` с `via_review=false` |
| 6. Симулировать переполнение | SQL: заполнить `pending_articles` до 10, запустить prep | Перед INSERT новых — старые прогнаны через GT + publish, место освобождено, админ-пинг `"Queue pressure: auto-published K"` |
| 7. Проверить migration test | bash `pytest tests/test_migration.py` | На пустой tempfile-БД после `init_db()` существуют 4 таблицы с колонками из спецификации |
| 8. Сравнить визуал последнего канального поста с msg 35 | Telegram MCP + ручной просмотр | Хэштег, preview card, Telegraph-контент совпадают с golden-reference (msg 35) |

### Пользователь проверяет

- Открыть `@myhwchannel123` после первой боевой публикации — пост в locked формате, Instant View preview работает, хэштег корректный (`#autoevolution`/`#mattel`/`#lamleygroup`). **Зачем руками:** визуальная валидация не автоматизируется без чего-то вроде Playwright.
- Открыть Telegraph draft-URL в браузере на шаге `preview` — hero-картинка, subtitle с `💬 «…»`, `<hr>`-сепаратор, параграфы в правильном порядке. **Зачем руками:** верифицировать вёрстку Instant View, которая не отрисовывается в headless-чекапах.
