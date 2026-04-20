---
created: 2026-04-20
status: draft
branch: dev
size: S
---

# Tech Spec: Mattel Corporate News Source

## Solution

Новый модуль `mattel_news_source.py` загружает страницу `https://corporate.mattel.com/news`, извлекает встроенный JSON из `<script id="__NEXT_DATA__">`, фильтрует записи по вхождению `hot wheels` в `title` или `handle` и возвращает список entries в формате, совместимом с существующим пайплайном `news_bot.py`. Функция `job()` в `news_bot.py` расширяется вызовом этого модуля после обработки RSS-фидов, ошибки изолированы через try-catch.

Фича S-размера: один новый модуль, одна модификация в `news_bot.py`, набор unit-тестов с фикстурой HTML.

## Architecture

### What we're building/modifying

- **Новый модуль `mattel_news_source.py`** — функция `fetch_mattel_news()` возвращает список entries.
- **Модификация `news_bot.py::job()`** — вызов `fetch_mattel_news()` после RSS-цикла, мёрдж результатов в `all_entries`.
- **Новый тест `tests/test_mattel_news_source.py`** — unit-тесты с фикстурой HTML.
- **Новая фикстура `tests/fixtures/mattel_news.html`** — реальный HTML страницы, сохранённый один раз.

### How it works

1. `job()` выполняет обычный цикл по RSS-фидам.
2. После RSS вызывается `fetch_mattel_news()` внутри try-catch.
3. `fetch_mattel_news()`:
   - Делает HTTP GET к `corporate.mattel.com/news` с `User-Agent` браузера и timeout.
   - Извлекает содержимое `<script id="__NEXT_DATA__" type="application/json">` регуляркой.
   - Парсит JSON, находит `props.pageProps.page.data.state.article2.entries`.
   - Для каждой записи: фильтрует по `hot wheels` / `hot-wheels` в `title.lower()` или `handle.lower()`.
   - Формирует entry-словарь: `link`, `title`, `summary`, `published_parsed` (struct_time для совместимости с feedparser-entries), `feed_url`.
4. При ошибке (HTTP, отсутствие `__NEXT_DATA__`, невалидный JSON, отсутствие ожидаемого пути в JSON) — вызывает `send_admin_notification()`, возвращает `[]`.
5. Результат передаётся в `filter_new_entries()` вместе с RSS-записями, дальше стандартный пайплайн.

### Shared resources

Нет. Используется уже существующая SQLite-БД через `is_processed/mark_processed` (вызываются внутри `filter_new_entries`).

## Decisions

### Decision 1: Parsing strategy — embedded JSON, not Playwright
**Decision:** Парсить `<script id="__NEXT_DATA__">` из HTML.
**Rationale:** Данные доступны в HTML сразу после загрузки, Playwright даёт значительный overhead (browser runtime, ~100MB зависимостей).
**Alternatives:** Playwright/Selenium (overkill); поиск публичного API Mattel (не существует).

### Decision 2: Filter logic — substring in title or handle
**Decision:** `'hot wheels' in title.lower() or 'hot-wheels' in handle.lower()`.
**Rationale:** Категории у Mattel общие (`Brand News`, `Corporate News`), не годятся для фильтрации. `handle` (URL-слаг) — стабильный индикатор; `title` — для случаев, когда слаг использует другой формат.
**Alternatives:** Регулярки (избыточно); NLP-классификация (overkill для простого кейса).

### Decision 3: Entry format — feedparser-compatible dict
**Decision:** Возвращать словари с ключами `link`, `title`, `summary`, `published_parsed`, `feed_url`, совместимые с результатом `feedparser.parse().entries`.
**Rationale:** Существующий пайплайн (`filter_new_entries`, `get_article_data`, `process_new_articles`) работает с такими entries. Унификация избавляет от модификации downstream-кода.
**Alternatives:** Новый формат entry с адаптером (лишний слой).

### Decision 4: Error handling — fail-soft with admin notification
**Decision:** Любая ошибка (HTTP, parsing, JSON path) → `send_admin_notification`, возврат `[]`.
**Rationale:** Соответствует user-spec: "При ошибке... продолжает работу с другими источниками". Не роняет основной job.
**Alternatives:** Пробросить исключение (сломает job); silent fail (админ не узнает о поломке).

### Decision 5: No token/config file required
**Decision:** URL зашит в модуль как константа. Нет `.json` конфига, нет env переменных.
**Rationale:** Source простой, один URL, настраивать нечего. Размер S, не нужна инфраструктура конфига.
**Alternatives:** Вынести в `feeds.json` или отдельный config — преждевременная абстракция.

## Data Models

Entry dict format:
```python
{
    'link': str,              # 'https://corporate.mattel.com/news/{handle}'
    'title': str,
    'summary': str,           # из поля 'excerpt' или из body, если excerpt пуст
    'published_parsed': struct_time,  # time.struct_time, parsed из 'date' ('YYYY-MM-DD')
    'feed_url': str,          # 'https://corporate.mattel.com/news' (для логов)
}
```

## Dependencies

### New packages
- Нет. Используем уже имеющиеся `requests`, `json`, `re` (stdlib).

### Using existing
- `requests` — HTTP.
- `re` — извлечение `__NEXT_DATA__` из HTML.
- `json` — парсинг.
- `time.strptime` — парсинг даты.
- `news_bot.send_admin_notification` — уведомления.
- `logging` — логи.

## Testing Strategy

**Feature size:** S

### Unit tests (`tests/test_mattel_news_source.py`)
- `test_fetch_mattel_news_success` — фикстура HTML, проверяем извлечение Hot Wheels записи.
- `test_fetch_mattel_news_filters_non_hotwheels` — среди записей только Hot Wheels попадает в результат.
- `test_fetch_mattel_news_http_error` — мок 500, возвращает `[]`, вызван `send_admin_notification`.
- `test_fetch_mattel_news_missing_next_data` — HTML без `__NEXT_DATA__`, возврат `[]`, уведомление админа.
- `test_fetch_mattel_news_invalid_json` — битый JSON, возврат `[]`, уведомление.
- `test_fetch_mattel_news_missing_entries_path` — JSON есть, но нет `state.article2.entries`, возврат `[]`, уведомление.
- `test_fetch_mattel_news_entry_format` — проверка обязательных полей в entry (link, title, summary, published_parsed).

### Integration tests
- `test_job_with_mattel_source_integration` — мокаем HTTP и Telegram, проверяем что Mattel-entry проходит полный пайплайн и попадает в БД.

### E2E tests
Нет (соответствует user-spec).

## Agent Verification Plan

1. Запустить `pytest tests/test_mattel_news_source.py -v` — все тесты проходят.
2. Smoke: `python -c "from mattel_news_source import fetch_mattel_news; print(len(fetch_mattel_news()))"` — возвращает ≥0 без падения.
3. Проверить логи: при успехе логируется число найденных Hot Wheels записей.

## Risks

| Risk | Mitigation |
|------|-----------|
| Mattel изменит структуру JSON | try-catch на всех уровнях парсинга, уведомление админа, изоляция от RSS |
| Cloudflare заблокирует scraper | Корректный User-Agent; при 403 — уведомление и пропуск источника |
| Ложные срабатывания фильтра | Проверка в `handle` (URL-слаг надёжнее заголовка) |
| Сломается HTML фикстура при обновлении сайта | Фикстура зафиксирована в репо; unit-тесты независимы от live-сайта |

## Security considerations

- **SSRF:** URL зашит константой, пользовательский ввод не принимается.
- **XSS/Injection:** JSON-парсинг, не `eval`.
- **Rate limiting:** Один запрос в сутки — значительно ниже любых разумных лимитов.

## User-Spec Deviations

None.

## Acceptance Criteria

- [ ] `fetch_mattel_news()` возвращает список entries, совместимых с пайплайном `news_bot.py`.
- [ ] Только Hot Wheels записи попадают в результат.
- [ ] Ошибки HTTP/parsing не ломают `job()`, шлют admin notification.
- [ ] Все unit-тесты проходят.
- [ ] Интеграция в `job()` работает: записи от Mattel проходят через `filter_new_entries` и `process_new_articles`.
- [ ] Опубликованные в Telegram посты содержат ссылку на `corporate.mattel.com/news/{handle}`.

## Implementation Tasks

### Wave 1 (независимые)

#### Task 1: Core parser — fetch + extract JSON + filter
- **Description:** Создать `mattel_news_source.py` с функцией `fetch_mattel_news()`. Реализовать HTTP-загрузку с User-Agent, извлечение `__NEXT_DATA__` регуляркой, парсинг JSON, фильтр по Hot Wheels, формирование entry-dicts.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news()[:1])"` — хотя бы не падает.
- **Files:** `mattel_news_source.py` (new)
- **Files to read:** `news_bot.py` (fetch_rss pattern, send_admin_notification)

#### Task 2: Unit tests + HTML fixture
- **Description:** Сохранить реальный HTML как фикстуру `tests/fixtures/mattel_news.html`. Написать unit-тесты для всех веток: success, filter, HTTP error, missing __NEXT_DATA__, invalid JSON, missing path, entry format.
- **Skill:** code-writing, test-master
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `pytest tests/test_mattel_news_source.py -v` — все тесты проходят.
- **Files:** `tests/test_mattel_news_source.py` (new), `tests/fixtures/mattel_news.html` (new)
- **Files to read:** `mattel_news_source.py`, existing test patterns

### Wave 2 (зависит от Wave 1)

#### Task 3: Integration into job()
- **Description:** В `news_bot.py::job()` после цикла RSS вызвать `fetch_mattel_news()` внутри try-catch, добавить результат в `all_entries`. Логирование успеха/ошибки.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** Запустить `python3 news_bot.py` (без токенов) — нет падений, в логах видно попытку Mattel source.
- **Files:** `news_bot.py`
- **Files to read:** `news_bot.py::job`, `mattel_news_source.py`

#### Task 4: Integration test
- **Description:** Тест проходит полный путь: мок HTTP Mattel → `job()` → мок Telegram → запись в БД (in-memory sqlite).
- **Skill:** code-writing, test-master
- **Reviewers:** test-reviewer
- **Verify-smoke:** `pytest tests/test_mattel_integration.py -v` — тест проходит.
- **Files:** `tests/test_mattel_integration.py` (new)

### Audit Wave

#### Task 5: Code + Security audit
- **Description:** Обзор всех модификаций на качество и безопасность (SSRF, парсинг внешних данных, error handling).
- **Skill:** code-reviewing, security-auditor
- **Reviewers:** none

### Final Wave

#### Task 6: Pre-deploy QA + deploy
- **Description:** Прогнать все тесты, проверить acceptance criteria, деплой.
- **Skill:** pre-deploy-qa
- **Reviewers:** none
