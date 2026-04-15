---
created: 2026-04-15
status: approved
branch: dev
size: M
---

# Tech Spec: multiple-rss-feeds

## Solution

Extend the existing Hot Wheels News Bot to support multiple RSS feeds (up to 5) by reading feed URLs from a JSON configuration file (`feeds.json`). The bot will iterate over each feed, fetch new articles, deduplicate globally via SQLite, translate, summarize, and post to Telegram. Each feed is processed independently with error isolation, so failures in one feed do not affect others. Backward compatibility is maintained: if `feeds.json` is missing, the bot falls back to the hardcoded RSS URL.

## Architecture

### What we're building/modifying

- **Configuration loader** – New module to read `feeds.json` (list of URLs) and validate up to 5 feeds.
- **RSS feed iterator** – Modify `job()` to loop over feeds, aggregate entries, and apply per‑feed error handling.
- **Error isolation wrapper** – Wrap each feed's `fetch_rss` call in try‑catch, log errors, continue to next feed.
- **Logging enhancements** – Include feed source in log messages for better observability.

### How it works

1. At startup, the script loads `feeds.json` from the project root (or uses fallback RSS URL).
2. For each enabled feed URL, `fetch_rss` is called inside a try‑catch block.
3. Entries from all feeds are collected into a single list.
4. Global duplicate detection (`filter_new_entries`) filters entries already present in the `processed_news` table.
5. Up to `global_limit` (configurable, default 3) new articles are processed through the existing pipeline (scraping, translation, summarization, Telegram posting).
6. Each processed article is recorded in the database to prevent future reprocessing.

Data flow remains unchanged after the aggregation step; translation, summarization, and Telegram posting are unaffected.

### Shared resources

None.

| Resource | Owner (creates) | Consumers | Instance count |
|----------|----------------|-----------|----------------|

## Decisions

### Decision 1: Configuration format
**Decision:** Use a simple JSON file `feeds.json` containing a list of feed URLs (strings). No per‑feed settings.
**Rationale:** User requested a simple configuration file; JSON is easy to read/write with Python’s standard library and allows straightforward validation. Per‑feed settings (enabled, limit) are out of scope for MVP.
**User‑spec requirement:** Пользователь добавляет до 5 RSS‑URL в конфигурационный файл (или переменные окружения).
**Alternatives considered:** Environment variable `RSS_FEEDS` (comma‑separated) – less flexible, harder to manage 5 URLs; YAML – more complex parsing without clear benefit.

### Decision 2: Error isolation
**Decision:** Wrap each feed fetch in its own try‑catch block, log the error, and continue with the next feed.
**Rationale:** Ensures that a single malformed or unavailable feed does not stop the whole job, meeting user‑spec requirement “ошибки … не останавливают обработку остальных лент”.
**Alternatives considered:** Let exceptions propagate and stop the job – violates requirement; collect errors but stop after too many failures – overcomplicates.

### Decision 3: Backward compatibility
**Decision:** If `feeds.json` does not exist or is empty, fall back to the hardcoded `RSS_URL` (current single feed).
**Rationale:** Existing deployments continue to work without configuration changes; migration is optional.
**User‑spec requirement:** Конфигурация лент может быть изменена без изменения кода (через файл или переменные окружения).
**Alternatives considered:** Require configuration file – would break existing setups; use environment variable to toggle – adds unnecessary complexity.

### Decision 4: Processing limit [TECHNICAL]
**Decision:** Keep the existing `limit=3` per job (global limit across all feeds), not per feed.
**Rationale:** Prevents the bot from processing too many articles in a single run, which could exceed timeouts or Telegram rate limits. The limit is small because the bot runs daily; users can adjust it in code if needed.
**User‑spec requirement:** None (technical optimization to respect performance constraints).
**Alternatives considered:** Per‑feed limits – adds configuration complexity; no limit – risks processing dozens of articles daily, possibly exceeding external API quotas.

### Decision 5: Deduplication scope [TECHNICAL]
**Decision:** Keep global deduplication (same `processed_news` table for all feeds) because article URLs are globally unique.
**Rationale:** Simplifies database schema and prevents duplicate posts across different feeds that might reference the same article.
**User‑spec requirement:** None (technical optimization for deduplication).
**Alternatives considered:** Per‑feed deduplication – would require schema changes and offers no benefit for this use case.

## Data Models

No changes to existing data models. The `processed_news` table remains unchanged.

## Dependencies

### New packages
- None (JSON parsing uses built‑in `json` module).

### Using existing (from project)
- `feedparser` – RSS parsing (already used)
- `requests` – HTTP fetching (already used)
- `beautifulsoup4` – HTML parsing (already used)
- `deep_translator` – translation (already used)
- `schedule` – scheduling (already used)
- `python-telegram-bot` – Telegram API (already used)

## Testing Strategy

**Feature size:** M

### Unit tests
- Scenario 1: Configuration loader reads `feeds.json` correctly and validates up to 5 URLs.
- Scenario 2: Fallback to hardcoded RSS URL when config file missing.
- Scenario 3: Error isolation – simulate failing feed, verify others are processed.
- Scenario 4: Feed iteration aggregates entries from multiple feeds.

### Integration tests
- Scenario 1 (required for M size): End‑to‑end test with mock RSS feeds (using `feedparser` mock) and mock Telegram API, verifying the whole pipeline processes articles from multiple feeds and respects global limit.

### E2E tests
- "None" (per user‑spec: “E2E тесты не делаем”).

## Agent Verification Plan

**Source:** user‑spec "Как проверить" section.

### Verification approach
Agent will verify the feature by:
1. Creating a test `feeds.json` with two mock RSS feed URLs (using a local HTTP server serving static RSS XML).
2. Running the bot and checking logs for successful processing of articles from both feeds.
3. Verifying that duplicate detection works across feeds (add an article already in DB, ensure it's skipped).
4. Simulating a failing feed (invalid URL) and confirming error is logged but other feed is still processed.

### Tools required
- `curl` or `httpie` to verify RSS endpoints.
- `sqlite3` to inspect `news.db`.
- Python’s `http.server` to serve mock RSS.

## Risks

| Risk | Mitigation |
|------|-----------|
| Increased runtime due to sequential fetching of multiple feeds. | Add per‑feed timeout (default 10 seconds). If a feed times out, skip it and log. |
| Configuration file syntax errors cause startup failure. | Validate JSON at startup, fall back to hardcoded RSS URL with warning log. |
| One feed constantly failing fills logs with errors. | Log each feed error only once per run, suppress repeated identical errors. |
| Global limit may cause starvation (articles from one feed dominate the limit). | Consider round‑robin selection across feeds (future enhancement). For MVP, accept simple global limit. |

## User-Spec Deviations

None.

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [x] Бот читает `feeds.json` и использует указанные RSS-ленты (до 5).
- [ ] Если `feeds.json` отсутствует, бот использует запасной RSS‑URL (текущий).
- [ ] Ошибка парсинга одной ленты не прерывает обработку остальных лент.
- [ ] Все новые статьи из всех лент проходят дедупликацию по ссылке (глобально).
- [ ] Ограничение `limit=3` применяется глобально (суммарно по всем лентам).
- [ ] В логах для каждой статьи указан источник (URL ленты).
- [ ] Unit‑тесты проходят для новых функций.
- [ ] Интеграционный тест с mock‑лентами проходит.
- [ ] Нет регрессий в существующем функционале (одиночная лента работает как прежде).

## Implementation Tasks

### Wave 1 (независимые)

#### Task 1: Configuration loader
- **Description:** Create `feeds.json` configuration file format (JSON array of up to 5 strings). Write `load_feeds()` function that reads the file, validates URLs, and returns list. If file missing or invalid, fall back to hardcoded RSS URL.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "import json; json.load(open('feeds.json'))"` → valid JSON; `python -c "from news_bot import load_feeds; print(load_feeds())"` → list of URLs
- **Files to modify:** `news_bot.py` (add `load_feeds` function), `feeds.json` (example)
- **Files to read:** `news_bot.py` (current RSS_URL), `patterns.md` (configuration conventions)

- [x] #### Task 2: Feed iteration and error isolation
- **Description:** Modify `job()` to iterate over feeds from `load_feeds()`. For each feed, wrap `fetch_rss` in try‑catch, log errors, collect entries. Aggregate entries before duplicate filtering. Keep global limit (`limit=3`) across all feeds.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** Run script with test feeds (one invalid) and check logs for error isolation.
- **Files to modify:** `news_bot.py` (`job`, `fetch_rss` call site)
- **Files to read:** `news_bot.py` (existing `job`, `fetch_rss`, `filter_new_entries`, `process_new_articles`)

- [x] #### Task 3: Enhanced logging
- **Description:** Add feed source (URL) to log messages when processing entries. Include feed index in logs for clarity.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** Run script with multiple feeds, verify logs contain feed identifiers.
- **Files to modify:** `news_bot.py` (logging statements in `job`, `fetch_rss`, `process_new_articles`)
- **Files to read:** `patterns.md` (logging conventions)

### Wave 2 (зависит от Wave 1)

#### Task 4: Unit tests for new functionality
- **Description:** Write unit tests for `load_feeds`, feed iteration, error isolation. Use `unittest.mock` to simulate file I/O and network errors.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `python -m pytest tests/ -xvs` → all new tests pass.
- **Files to modify:** `tests/test_news_bot.py` (create if missing)
- **Files to read:** `news_bot.py` (new functions), `patterns.md` (testing conventions)

#### Task 5: Integration test with mock feeds
- **Description:** Create integration test that runs the full pipeline with mock RSS feeds (local HTTP server serving static RSS XML) and mock Telegram API. Verify articles from multiple feeds are processed, duplicates skipped, errors isolated.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** Run integration test suite; check that mock Telegram received expected posts.
- **Files to modify:** `tests/integration/test_multiple_feeds.py`
- **Files to read:** `news_bot.py`, `patterns.md` (integration testing)

### Audit Wave

#### Task 6: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified in this feature (`news_bot.py`, test files). Review holistically for cross‑component issues: duplicate resource initialization, architectural consistency, error‑handling completeness.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 7: Security Audit
- **Description:** Full-feature security audit. Read all source files created/modified in this feature. Analyze for OWASP Top 10 across all components, cross‑component auth/data flow, input validation (URLs), secure file reading.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 8: Test Audit
- **Description:** Full-feature test quality audit. Read all test files created in this feature. Verify coverage, meaningful assertions, test pyramid balance across all components.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 9: Pre-deploy QA
- **Description:** Acceptance testing: run all tests (unit, integration), verify acceptance criteria from user‑spec and tech‑spec, ensure no regression on single‑feed mode.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 10: Deploy (optional)
- **Description:** Deploy updated bot to production server (if applicable). Update configuration file on server, restart service.
- **Skill:** deploy-pipeline
- **Reviewers:** none

#### Task 11: Post-deploy verification (optional)
- **Description:** Live environment verification: run bot with production feeds, check Telegram channel for new posts, verify logs for any errors.
- **Skill:** post-deploy-qa
- **Reviewers:** none
