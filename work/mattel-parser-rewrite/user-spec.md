---
created: 2026-04-25
status: draft
type: refactoring
size: S
---

# User Spec: mattel-parser-rewrite

## Что делаем

Переписываем извлечение данных в `mattel_news_source.py`. Текущий код ищет `<script id="__NEXT_DATA__">` на `https://corporate.mattel.com/news` и страницах статей. После миграции Mattel на Next.js App Router (RSC-стриминг) этого тега больше нет — парсер тихо возвращает `[]` / `None`. Заменяем механизм на парсинг RSC flight payload (`self.__next_f.push([1, "..."])`), сохраняя внешний контракт функций 1:1, чтобы `news_bot.job()` работал без изменений.

## Зачем

В проде тихий регресс: `fetch_mattel_news()` всегда возвращает `[]`, подписчики канала `@myhwchannel123` не видят пресс-релизы Mattel о Hot Wheels. Каждые 12 часов оператор получает в админ-чат «Mattel news parsing error: __NEXT_DATA__ script tag not found in HTML» — шум, который скрывает настоящие инциденты. Восстановление существовавшей фичи + удаление ложного админ-сигнала. Импакт на канал низкий (Mattel редко выпускает HW-посты, autoevolution и lamley работают), но это P1 — устраняем тихий регресс и не накапливаем шум в нотификациях.

## Как должно работать

1. Каждые 12 часов `news_bot.job()` вызывает `_fetch_mattel_entries(notifier=...)`, который зовёт `fetch_mattel_news()`.
2. `fetch_mattel_news()` делает `GET https://corporate.mattel.com/news` (15s timeout, 5 MB guard, Chrome UA), достаёт RSC flight payload через `self.__next_f.push([1, "..."])`, разворачивает JSON-экранирование, находит anchor `"article2":{"entries":[`, читает массив через bracket-match, фильтрует на Hot Wheels (по `title` или `handle`), и возвращает feedparser-совместимые dict'ы — те же 5 ключей, что и раньше (`link`, `title`, `summary`, `published_parsed`, `feed_url`).
3. Для каждой HW-записи `news_bot.fetch_full_article()` диспетчит на `fetch_mattel_article(link)`, который снова парсит flight payload страницы статьи: находит запись с нужным `handle` в `article2.entries`, читает `body: "$N"`, реконструирует HTML тела через RSC text-row маркер `N:T<hex-len>,<content>` (с конкатенацией pushes), парсит body BeautifulSoup'ом → возвращает `{title, subtitle, paragraphs, images}` где `images = [thumbnail.url]` (download_media по-прежнему игнорим — patterns.md image policy).
4. Дальше — обычный pipeline: запись в `pending_articles`, ручной обзор через `hw_review`, публикация на Telegra.ph + канальная карточка с `#mattel`.
5. Перед deploy и сразу после оператор запускает `python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())"` для smoke-проверки — должен вернуть `[]` (нет HW сегодня) или валидный список без админ-уведомлений в STDERR.

## Критерии приёмки

- [ ] AC1. `fetch_mattel_news()` против HTML-фикстура с N HW-записями в `article2.entries` возвращает список длины N.
- [ ] AC2. Каждый возвращённый entry имеет ровно 5 ключей: `link` (`ARTICLE_URL_PREFIX + handle`), `title` (str из flight), `summary` (str, `excerpt or title`), `published_parsed` (`time.struct_time` из `date` формата `YYYY-MM-DD`, или `None`), `feed_url` (`NEWS_URL`).
- [ ] AC3. `fetch_mattel_news()` против HTML без HW-записей возвращает `[]` И не вызывает notifier (отсутствие HW ≠ ошибка парсинга).
- [ ] AC4. `fetch_mattel_news()` против HTML без `self.__next_f.push` или без anchor `"article2":{"entries":[` вызывает notifier один раз с осмысленным сообщением (например «Mattel news parsing error: article2.entries not found in flight payload») и возвращает `[]`.
- [ ] AC5. `fetch_mattel_article(link)` против HTML-фикстура HW-статьи возвращает dict с 4 ключами: `title`, `subtitle` (`excerpt or ""`, dict-form поддерживается), `paragraphs` (`List[str]` из BeautifulSoup-обхода body по тегам `p/li/h1-h4`), `images` (`List[str]` длины 0 или 1 — только `thumbnail.url`).
- [ ] AC6. `fetch_mattel_article(link)` против HTTP 404 (через `raise_for_status`) или 200-без-`article2` возвращает `None` и вызывает notifier один раз.
- [ ] AC7. RSC body reconstruction: при `body="$N"` парсер находит row N с маркером `N:T<hex-len>,<content>`, читает ровно `<hex-len>` символов контента, корректно конкатенируя pushes если контент перекрывает их границы.
- [ ] AC8. Image policy «thumbnail only» сохранена: `download_media` в выходе отсутствует даже если непуст в payload.
- [ ] AC9. Импорты `news_bot.py:24` (`from mattel_news_source import fetch_mattel_news, fetch_mattel_article`) продолжают работать; `MattelNewsError`, `NEWS_URL`, `ARTICLE_URL_PREFIX`, `MAX_RESPONSE_SIZE` остаются экспортированы.
- [ ] AC10. `tests/test_mattel_integration.py` — 3 теста зелёные с обновлённым фикстуром.
- [ ] AC11. `pytest tests/` — все тесты зелёные.
- [ ] AC12. Manual smoke против live `https://corporate.mattel.com/news` не вызывает notifier; возврат либо `[]`, либо валидный список dict'ов.

## Ограничения

- **Совместимость контракта 1:1.** Функции `fetch_mattel_news` и `fetch_mattel_article` сохраняют сигнатуры (`url=NEWS_URL, session=None, notifier=None`) и форму выхода (5 ключей и 4 ключа соответственно). Внешние символы модуля (`MattelNewsError`, `NEWS_URL`, `ARTICLE_URL_PREFIX`, `MAX_RESPONSE_SIZE`) сохраняются.
- **Без новых зависимостей.** Используем `re`, `json`, `time`, `logging`, `requests`, `beautifulsoup4` — всё уже в `requirements.txt`. Playwright, selenium, curl_cffi (последний есть в стеке для autoevolution, но Mattel сейчас отдаёт 200 OK на plain `requests`) не подключаем.
- **Image policy thumbnail-only.** Только `thumbnail.url` уходит в `images`. `download_media` (press-kit активы — логотипы в нескольких форматах, hi-res пресс-фото) игнорируется по дизайну (`patterns.md` Image Extraction Per Source). Регрессионный тест `test_parses_paragraphs_and_uses_thumbnail_only` остаётся в силе.
- **Fail-soft с админ-уведомлением.** Любая структурная ошибка → `MattelNewsError` → `_notify(...)` → возврат `[]` / `None`. Падение notifier'а само поглощается внутри `_notify` (caller never raises). Существующее поведение модуля сохранено.
- **Backward-compat для `__NEXT_DATA__` не держим.** Live сайт мигрирован, ретроспективная поддержка не требуется. Если откатят — отдельная мини-фича.
- **HTTP guards.** `REQUEST_TIMEOUT=15s`, `MAX_RESPONSE_SIZE=5 MB`, hardcoded Chrome UA — без изменений.
- **Без миграций.** `news.db` schema, существующие `pending_articles`/`published_articles` — не трогаем.

## Риски

- **Риск 1: будущая смена layout RSC.** Mattel/Next.js может перетасовать row-IDs, переименовать `article2`, изменить chunk-разбиение. **Митигация:** anchor на семантические маркеры (`"article2":{"entries":[`, имена полей `handle`/`title`/`date`), а не позицию push'а или конкретный row-id. При структурном сломе сработает админ-уведомление — оператор зафиксирует регресс и заведёт следующую итерацию.
- **Риск 2: расхождение фикстуры с реальным форматом.** Hand-crafted фикстура может разойтись с тем, как Mattel реально стримит RSC, и тесты будут зелёными при сломе на проде. **Митигация:** строим фикстуру через Python helper'ы (`_make_flight_listing`, `_make_flight_article`) которые имитируют реальное обрамление push'ей, проверенное в code-research §5–6 на live HTML 2026-04-24. Manual smoke (AC12) перед/после deploy ловит расхождение, которое тесты пропустили.
- **Риск 3: RSC body reconstruction для крайних случаев.** Тело статьи может перекрывать > 2 push'а или иметь нетривиальные escape-последовательности. **Митигация:** алгоритм работает с конкатенацией ВСЕХ pushes до начала поиска row-marker — границы pushes не имеют значения. Покрываем юнит-тестом «body across 3 pushes».
- **Риск 4: Cloudflare interstitial у Mattel.** Mattel может включить Cloudflare bot-protection (как у autoevolution), и `requests` начнёт получать 403. **Митигация:** не делаем upfront (YAGNI — сейчас 200 OK через plain requests). Если случится — переключимся на `curl_cffi` отдельной мини-фичей; модуль уже в стеке.
- **Риск 5: Wayback не сохраняет RSC-формат.** Code-research §7 показал: все Wayback snapshots ≤ 2026-04-21 — старый `__NEXT_DATA__`. Если live сайт сломается надолго, ретроспективно дебажить через snapshot не получится. **Митигация:** перед деплоем сохраняем live snapshot в `/tmp/mattel_news_snapshot.html` локально оператором — single-shot страховка. Принимаем как остаточный риск.

## Технические решения

- Мы решили **парсить RSC flight payload через regex + JSON unescape + bracket-match anchor**, потому что данные доказуемо присутствуют в самом большом `self.__next_f.push([1, "..."])` (live verified 2026-04-24, code-research §5), не требует новых зависимостей и не привязан к позиционным предположениям.
- Мы решили **не использовать playwright/headless-браузер**, потому что +100 МБ на VPS и больше движущихся частей оператор не приветствует, а данные доступны статически.
- Мы решили **не разведывать undocumented Builder.io/Contentstack API**, потому что выходит за scope и ломкий — Mattel может заблокировать non-browser-source.
- Мы решили **переписать `mattel_news_source.py` in-place** (не новый модуль), потому что внешний контракт сохраняется, alias никому не нужен.
- Мы решили **не держать backward-compat для `__NEXT_DATA__`**, потому что live сайт мигрирован, retrospective parsing — мёртвый код. Если откатят — добавим отдельной фичей.
- Мы решили **строить body HTML через RSC text-row reconstruction** (находить `body="$N"`, потом `N:T<hex-len>,<content>`, читать ровно `<hex-len>` символов через все pushes), потому что brute-force скан `<p>` тегов поймает sidebar-контент (на article-странице тоже рендерится listing).
- Мы решили **оставить `summary = excerpt or title`** (не апгрейдить на `seo_description`), потому что `summary` не потребляется downstream в проде (`news_bot.job()` использует только `link`/`title`/`feed_url`).
- Мы решили **сохранить downstream-skip для image-only HW** (`paragraphs=[]` → `news_bot.py:1231` пропускает row), потому что pipeline не поддерживает image-only посты, а HW-релизы Mattel без текста — крайне редкий edge.
- Мы решили **строить фикстуры через Python helper'ы** (`_make_flight_listing`, `_make_flight_article`), потому что ручное редактирование двойного-JSON-escape мучительно и плохо поддерживается, а helper даёт детерминированные edge-cases для CI.

## Тестирование

**Unit-тесты:** делаются всегда, не обсуждаются. Расширяем `tests/test_mattel_news_source.py`. Сохраняем `TestIsHotwheels` и `TestBuildEntry` (helper-сигнатуры не меняются). Переписываем `TestExtractEntries` под flight payload (новые сообщения ошибок: «article2.entries not found in flight payload» вместо «`__NEXT_DATA__` not found»). Переписываем `TestFetchMattelArticle` целиком — `_article_page` помощник теперь строит flight HTML. Переписываем error-path тесты в `TestFetchMattelNews` под новые сообщения. Старый фикстур `tests/fixtures/mattel_news.html` (`__NEXT_DATA__`) удаляем; добавляем helper-builder'ы для синтетических flight HTML (1+ HW entry для happy-path, 0 entries для AC3, no-`article2` для AC4, body-across-pushes для AC7). Покрытие: AC1–AC8 + регрессионный `test_parses_paragraphs_and_uses_thumbnail_only` сохранён.

**Интеграционные тесты:** делаем — обновляем `tests/test_mattel_integration.py`. 3 существующих теста (`test_mattel_post_flows_into_pending_queue`, `test_mattel_http_failure_does_not_crash_job`, `test_mattel_duplicate_is_not_restaged`) остаются; меняем фикстуру на flight-сборку с инжектированной HW-записью так, чтобы `len(pending_articles) == 1` ассерт оставался валиден. Это границы интеграции source→registry→repo, важные для гарантии AC9–AC10.

**E2E тесты:** не делаем. Live-hit к `corporate.mattel.com` flaky (Mattel может блокировать; HW-gaps по неделям нормальны). Manual smoke (AC12) и post-deploy verification покрывают то, что E2E пытались бы.

## Как проверить

### Агент проверяет

| Шаг | Инструмент | Ожидаемый результат |
|-----|-----------|---------------------|
| 1. Запустить полный тестовый набор | `pytest tests/ -q` | Все тесты зелёные (407+ существующих + новые/обновлённые в `test_mattel_news_source.py` и `test_mattel_integration.py`). |
| 2. Прицельно прогнать Mattel-юниты | `pytest tests/test_mattel_news_source.py tests/test_mattel_integration.py -v` | Все Mattel-тесты зелёные, нет skip'нутых. |
| 3. Manual smoke против live Mattel | `python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())"` | Возврат `[]` (если HW=0 на странице сегодня) ИЛИ валидный список dict'ов. STDERR пустой (нет «parsing error»). |
| 4. Smoke-парсинг конкретной HW-статьи (если в листинге есть HW) | `python3 -c "from mattel_news_source import fetch_mattel_article; print(fetch_mattel_article('<HW link>'))"` | Возврат dict с непустым `paragraphs` и `images=[thumb]` ИЛИ `None` без падения. |
| 5. Импорт-проверка | `python3 -c "from mattel_news_source import fetch_mattel_news, fetch_mattel_article, MattelNewsError, NEWS_URL, ARTICLE_URL_PREFIX, MAX_RESPONSE_SIZE; print('ok')"` | Печатает `ok` — все экспортируемые символы на месте (AC9). |

### Пользователь проверяет

- **Что:** отсутствие «Mattel news parsing error» в админ-чате после deploy. **Как:** оператор смотрит чат с ботом в Telegram спустя 12+ часов после прода (один cron-tick). **Зачем:** Подтверждает AC3 в проде — отсутствие HW сегодня не должно генерить шум.
- **Что:** в течение 7 дней (или к моменту следующего HW-релиза от Mattel) убедиться, что HW-пост попадает в pending-очередь и оператор получает обычный queue-pressure ping. **Как:** наблюдение за `hw_review list` или прямой `sqlite3 news.db "select link from pending_articles where source_name='mattel'"`. **Зачем:** Подтверждает AC1 + AC5 + AC10 в живом окружении (синтетические фикстуры могут разойтись с реальным форматом — риск R2).
- **Что:** запустить smoke-команду на VPS после `bash deploy.sh`. **Как:** `ssh ... "cd /home/.../bot && python3 -c 'from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())'"`. **Зачем:** Подтверждает что обновлённый код действительно скопирован и импортируется на сервере (deploy.sh FILES list уже включает `mattel_news_source.py`).
