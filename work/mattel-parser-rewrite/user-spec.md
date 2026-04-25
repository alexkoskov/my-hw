---
created: 2026-04-25
status: draft
type: refactoring
size: S
---

# User Spec: mattel-parser-rewrite

## Что делаем

Переписываем извлечение данных в `mattel_news_source.py`. Текущий код ищет `<script id="__NEXT_DATA__">` на `https://corporate.mattel.com/news` и страницах статей. После миграции Mattel на Next.js App Router (RSC-стриминг) этого тега больше нет — парсер тихо возвращает `[]` / `None`. Заменяем источник: вычитываем те же данные из встроенного React/Next.js streaming-payload, который Mattel теперь использует для гидрации страницы. Внешний контракт обеих функций модуля сохраняется 1:1, чтобы `news_bot.job()` работал без изменений.

## Зачем

В проде тихий регресс: `fetch_mattel_news()` всегда возвращает `[]`, подписчики канала `@myhwchannel123` не видят пресс-релизы Mattel о Hot Wheels. Каждые 12 часов оператор получает в админ-чат «Mattel news parsing error: __NEXT_DATA__ script tag not found in HTML» — шум, который скрывает настоящие инциденты. Восстановление существовавшей фичи + удаление ложного админ-сигнала. Импакт на канал низкий (Mattel редко выпускает HW-посты, autoevolution и lamley работают), но это P1 — устраняем тихий регресс и не накапливаем шум в нотификациях.

## Как должно работать

1. Каждые 12 часов `news_bot.job()` зовёт `fetch_mattel_news()`.
2. `fetch_mattel_news()` делает HTTP GET на listing-страницу (15s timeout, 5 MB guard, Chrome UA), извлекает встроенный streaming-payload, находит массив news-entries, фильтрует на Hot Wheels (по `title` или `handle`) и возвращает feedparser-совместимые словари с теми же 5 ключами, что и до миграции (`link`, `title`, `summary`, `published_parsed`, `feed_url`).
3. Для каждой HW-записи `news_bot.fetch_full_article()` диспетчит на `fetch_mattel_article(link)`. Эта функция парсит payload страницы статьи, находит запись по `handle` (fallback по полю `url`, если `handle` не совпал; если ни тот ни другой не нашли — ошибка с админ-уведомлением + `None`), достаёт HTML тела статьи, извлекает текстовые параграфы BeautifulSoup'ом и возвращает `{title, subtitle, paragraphs, images}` с одной картинкой-thumbnail. Press-kit ассеты из `download_media` игнорируются (image-policy зафиксирована в `patterns.md`).
4. Дальше — обычный pipeline: запись в `pending_articles`, ручной обзор через `hw_review`, публикация на Telegra.ph + канальная карточка с `#mattel`.
5. Перед deploy и сразу после оператор запускает smoke-команду — должна вернуть `[]` (нет HW сегодня) или валидный список без админ-уведомлений в STDERR.

## Критерии приёмки

- [ ] AC1. `fetch_mattel_news()` против HTML-фикстура с N HW-записями возвращает список длины N.
- [ ] AC2. Каждый возвращённый entry имеет ровно 5 ключей: `link` (полный URL статьи), `title` (str), `summary` (str — `excerpt` или `title` как fallback), `published_parsed` (`time.struct_time` из `date` формата `YYYY-MM-DD`, или `None` если формат не парсится), `feed_url` (`NEWS_URL`).
- [ ] AC3. `fetch_mattel_news()` против HTML без HW-записей возвращает `[]` И не вызывает notifier (отсутствие HW ≠ ошибка парсинга).
- [ ] AC4. `fetch_mattel_news()` против HTML, где встроенный streaming-payload отсутствует или его секция с news-entries не найдена, вызывает notifier один раз с осмысленным сообщением и возвращает `[]`.
- [ ] AC5. `fetch_mattel_news()` при ответе сервера > `MAX_RESPONSE_SIZE` (5 MB) возвращает `[]` и вызывает notifier один раз.
- [ ] AC6. `fetch_mattel_article(link)` против HTML-фикстура HW-статьи возвращает dict с 4 ключами: `title`, `subtitle` (из `excerpt` — поддерживается строковая и dict-form `{text: ...}`; пустая строка если поле отсутствует), `paragraphs` (`List[str]` — текст параграфов и заголовков из body), `images` (`List[str]` длины 0 или 1 — только thumbnail).
- [ ] AC7. `fetch_mattel_article(link)` возвращает `None` и вызывает notifier один раз в трёх случаях: HTTP 404; payload без news-entries-секции; entry с заданным handle/url не найден внутри секции.
- [ ] AC8. Когда HTML тела статьи разбит на несколько streaming-чанков payload'а, `paragraphs` содержит ровно те же текстовые блоки и в том же порядке, что и для эквивалентного тела, помещающегося в один чанк (без пропусков и без дубликатов).
- [ ] AC9. `fetch_mattel_article(link)` для статьи с пустым/отсутствующим body, либо с обрезанным/неразрешимым body-референсом (заявленная длина > доступного контента, или ссылочный row не объявлен), возвращает dict с `paragraphs=[]` без вызова notifier — это контентная пустота, не парс-ошибка; downstream сам пропустит row.
- [ ] AC10. Image policy «thumbnail only» сохранена: `images` содержит максимум один URL (thumbnail); `download_media` в выходе отсутствует, даже если непуст в payload.
- [ ] AC11. Импорт `from mattel_news_source import fetch_mattel_news, fetch_mattel_article` продолжает работать в `news_bot.py`; экспорты `MattelNewsError`, `NEWS_URL`, `ARTICLE_URL_PREFIX`, `MAX_RESPONSE_SIZE` сохранены.
- [ ] AC12. `pytest tests/` — все тесты зелёные, включая обновлённые `tests/test_mattel_news_source.py` и `tests/test_mattel_integration.py`.
- [ ] AC13. Manual smoke против live `https://corporate.mattel.com/news` не вызывает notifier; возврат либо `[]`, либо валидный список dict'ов.

## Ограничения

- **Совместимость контракта 1:1.** Функции `fetch_mattel_news` и `fetch_mattel_article` сохраняют сигнатуры (`url=NEWS_URL, session=None, notifier=None`) и форму выхода (5 ключей и 4 ключа соответственно). Внешние символы модуля (`MattelNewsError`, `NEWS_URL`, `ARTICLE_URL_PREFIX`, `MAX_RESPONSE_SIZE`) сохраняются.
- **Без новых зависимостей.** Используем `re`, `json`, `time`, `logging`, `requests`, `beautifulsoup4` — всё уже в `requirements.txt`. Playwright, selenium и curl_cffi (последний есть в стеке для autoevolution, но Mattel сейчас отдаёт 200 OK на plain `requests`) не подключаем.
- **Image policy thumbnail-only.** Только thumbnail попадает в `images`. `download_media` (press-kit ассеты — логотипы в нескольких форматах, hi-res пресс-фото) игнорируется по дизайну (`patterns.md` Image Extraction Per Source). Регрессионный тест `test_parses_paragraphs_and_uses_thumbnail_only` остаётся в силе.
- **Fail-soft с админ-уведомлением.** Любая структурная ошибка → `MattelNewsError` → notifier → возврат `[]` / `None`. Падение notifier'а само поглощается — caller никогда не видит исключений из этого модуля. Существующее поведение сохранено.
- **Backward-compat для `__NEXT_DATA__` не держим.** Live сайт мигрирован, ретроспективная поддержка не требуется. Если откатят — отдельная мини-фича.
- **HTTP guards.** `REQUEST_TIMEOUT=15s`, `MAX_RESPONSE_SIZE=5 MB`, hardcoded Chrome UA — без изменений.
- **Без миграций.** `news.db` schema, существующие `pending_articles`/`published_articles` — не трогаем.
- **Silent-zero не алармим.** Возврат `[]` от `fetch_mattel_news()` не различает «листинг распарсен, но 0 HW» (нормально) и «парсер сломался, но фейлим без ошибки» (плохо) — различие пойдёт по линии notifier'а. Дополнительный automatic alert при N тиках подряд с пустым результатом не делаем; покрываем 7-дневным операторским наблюдением (см. «Пользователь проверяет»). Если в будущем потребуется — заведём отдельную фичу.

## Риски

- **Риск 1: будущая смена layout streaming-payload.** Mattel/Next.js может перетасовать внутренние идентификаторы, переименовать ключевые секции или поля. **Митигация:** анкер парсинга — на семантические маркеры (имена ключей `article2`, `handle`, `title`, `date`), а не на позиционные предположения. При структурном сломе сработает админ-уведомление — оператор зафиксирует регресс и заведёт следующую итерацию.
- **Риск 2: расхождение синтетической фикстуры с реальным форматом.** Synthetic-фикстура может разойтись с тем, что Mattel реально отдаёт, и unit-тесты будут зелёными при сломе на проде. **Митигация:** фикстуру строим через helper-функции, чьё обрамление выверено по live HTML 2026-04-24 (см. code-research §5–6). Manual smoke (AC13) перед/после deploy ловит расхождение, которое тесты пропустили. Принимаем как остаточный риск — рекомендуем при первом deploy сохранить live snapshot для будущей сверки.
- **Риск 3: тело статьи в крайних случаях.** Контент тела может перекрывать несколько чанков payload'а или иметь длинный заявленный размер при коротком фактическом. **Митигация:** AC8 фиксирует корректную реконструкцию через границы; AC9 — корректное поведение при пустом/обрезанном теле. Покрываем юнит-тестами с фикстурами, имитирующими каждый случай.
- **Риск 4: Cloudflare interstitial у Mattel.** Mattel может включить bot-protection (как у autoevolution), и `requests` начнёт получать 403. **Митигация:** не делаем upfront (YAGNI — сейчас 200 OK). Если случится — переключимся на `curl_cffi` отдельной мини-фичей; модуль уже в стеке.
- **Риск 5: Wayback не сохраняет новый формат.** Все Wayback snapshots ≤ 2026-04-21 — старый `__NEXT_DATA__` (code-research §7). Если live сайт сломается надолго, ретроспективно дебажить через snapshot не получится. **Митигация:** перед деплоем оператор сохраняет live snapshot в `/tmp` локально как страховку. Принимаем как остаточный риск.

## Технические решения

- Мы решили **парсить встроенный streaming-payload**, потому что данные доказуемо там присутствуют (live verified 2026-04-24, code-research §5), не требуется новых зависимостей и нет привязки к позиционным предположениям.
- Мы решили **не использовать playwright/headless-браузер**, потому что +100 МБ на VPS и больше движущихся частей оператор не приветствует, а данные доступны статически.
- Мы решили **не разведывать undocumented Builder.io/Contentstack API**, потому что выходит за scope и фрагильно — Mattel может заблокировать non-browser-source.
- Мы решили **переписать модуль in-place** (не новый файл), потому что внешний контракт сохраняется и alias никому не нужен.
- Мы решили **не держать backward-compat для `__NEXT_DATA__`**, потому что live сайт мигрирован, retrospective parsing — мёртвый код. Если откатят — добавим отдельной фичей.
- Мы решили **извлекать тело статьи только из секции тела, а не сканировать весь HTML на `<p>`-теги**, потому что на article-странице рендерится также sidebar listing — brute-force скан подцепит чужой контент.
- Мы решили **оставить `summary = excerpt or title`** (не апгрейдить на `seo_description`), потому что `summary` не потребляется downstream в проде.
- Мы решили **сохранить downstream-skip для image-only HW** (если `paragraphs=[]`, `news_bot.job()` пропускает row), потому что pipeline не поддерживает image-only посты, а такие HW-релизы Mattel — крайне редкий edge.
- Мы решили **строить фикстуры через Python helper-функции в тестах**, потому что ручное редактирование вложенного JSON-escape мучительно и плохо поддерживается, а helper даёт детерминированные edge-cases для CI.

## Тестирование

**Unit-тесты:** делаются всегда, не обсуждаются. Расширяем `tests/test_mattel_news_source.py`. Тесты на внутреннюю фильтрацию HW и сборку entry-словаря из сырых данных сохраняются в текущем виде (их сигнатуры не меняются). Переписываем тесты, проверявшие старое извлечение entries, под новый payload и его сообщения ошибок. Полностью обновляем тесты `fetch_mattel_article` — синтетические HTML-фикстуры теперь имитируют новый формат. Старый файл-фикстуру `tests/fixtures/mattel_news.html` удаляем; вместо этого добавляем в тестовый файл helper-функции, которые программно собирают синтетический HTML для разных сценариев (1+ HW entry, 0 HW entries, отсутствие нужной секции, тело статьи через несколько чанков, пустое тело, оборванный body-референс). Покрытие: AC1–AC10 + регрессионный тест на «thumbnail-only» image policy.

**Интеграционные тесты:** делаем — обновляем `tests/test_mattel_integration.py`. 3 существующих теста (`test_mattel_post_flows_into_pending_queue`, `test_mattel_http_failure_does_not_crash_job`, `test_mattel_duplicate_is_not_restaged`) остаются; меняем фикстуру на сборку с инжектированной HW-записью, чтобы `len(pending_articles) == 1` ассерт оставался валиден. Это границы интеграции source→registry→repo, важные для гарантий AC11–AC12.

**E2E тесты:** не делаем. Live-hit к `corporate.mattel.com` flaky (Mattel может блокировать; HW-gaps по неделям нормальны). Manual smoke (AC13) и post-deploy verification покрывают то, что E2E пытались бы.

## Как проверить

### Агент проверяет

| Шаг | Инструмент | Ожидаемый результат |
|-----|-----------|---------------------|
| 1. Полный тестовый набор | `pytest tests/ -q` | Все тесты зелёные (407+ существующих + новые/обновлённые в `test_mattel_news_source.py` и `test_mattel_integration.py`). |
| 2. Прицельный прогон Mattel-тестов | `pytest tests/test_mattel_news_source.py tests/test_mattel_integration.py -v` | Все Mattel-тесты зелёные, нет skip'нутых. |
| 3. Manual smoke против live Mattel | `python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())"` | Возврат `[]` (если HW=0 на странице сегодня) ИЛИ валидный список dict'ов. STDERR пустой (нет «parsing error»). |
| 4. Smoke-парсинг HW-статьи (если есть в листинге) | `python3 -c "from mattel_news_source import fetch_mattel_article; print(fetch_mattel_article('<HW link>'))"` | Возврат dict с непустым `paragraphs` и `images=[thumb]` ИЛИ `None` без падения. |
| 5. Импорт-проверка | `python3 -c "from mattel_news_source import fetch_mattel_news, fetch_mattel_article, MattelNewsError, NEWS_URL, ARTICLE_URL_PREFIX, MAX_RESPONSE_SIZE; print('ok')"` | Печатает `ok` — все экспортируемые символы на месте (AC11). |

### Пользователь проверяет

- **Что:** отсутствие «Mattel news parsing error» в админ-чате после deploy. **Как:** оператор смотрит чат с ботом в Telegram спустя 12+ часов после прода (один cron-tick). **Зачем:** Подтверждает AC3 в проде — отсутствие HW сегодня не должно генерить шум.
- **Что:** в течение 7 дней (или к моменту следующего HW-релиза от Mattel) убедиться, что HW-пост попадает в pending-очередь и оператор получает обычный queue-pressure ping. **Как:** наблюдение за `hw_review list` или прямой `sqlite3 news.db "select link from pending_articles where source_name='mattel'"`. **Зачем:** Подтверждает AC1 + AC6 + AC12 в живом окружении (синтетические фикстуры могут разойтись с реальным форматом — риск R2).
- **Что:** запустить smoke-команду на VPS после `bash deploy.sh`. **Как:** `ssh ... "cd /home/.../bot && python3 -c 'from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())'"`. **Зачем:** Подтверждает что обновлённый код действительно скопирован и импортируется на сервере (deploy.sh FILES list уже включает `mattel_news_source.py`).
