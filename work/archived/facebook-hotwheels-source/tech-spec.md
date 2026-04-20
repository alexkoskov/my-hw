---
created: 2026-04-17
status: approved
branch: dev
size: M
---

# Tech Spec: Facebook Hot Wheels Source

## Solution

Integrate Facebook page https://www.facebook.com/hotwheels/ as a news source for the existing Hot Wheels Telegram bot. The solution extends the current RSS‑based pipeline with a Facebook‑specific fetcher that attempts three methods in order of preference:

1. **RSS feed** – Use Facebook’s built‑in RSS endpoint (`/hotwheels/rss`) if available. Note that RSS may be unavailable; if so, the system falls back to Graph API (if a token is provided) or HTML parsing.
2. **Graph API** – Fallback to official Graph API (requires access token) to retrieve page posts with structured JSON. If token is missing, this method is skipped.
3. **HTML parsing** – Last‑resort scraping of the public page HTML using BeautifulSoup.

Retrieved posts are filtered by configurable keywords (`event`, `announcement`, `coming soon`, `glow‑n‑fire`) to exclude event announcements. Remaining posts are transformed into a uniform entry format compatible with the existing `news_bot.py` processing pipeline (translation, summarization, Telegram posting). The integration is designed to be isolated: errors in Facebook fetching do not affect other RSS sources, and Facebook's rate‑limit policies (with exponential backoff and retry logic, e.g., 2‑second sleep between Graph API calls) are respected.

All modifications are backward compatible; the existing `feeds.json` continues to work as a list of strings, while Facebook configuration is read from a separate `facebook_source.json` file.

## Architecture

### What we're building/modifying

- **New module `facebook_source.py`** – Contains functions to fetch posts via RSS, Graph API, and HTML parsing; filters posts by keywords; returns entries in the same format as RSS entries.
- **Configuration file `facebook_source.json`** – Stores Facebook page URL, filter keywords, enabled flag, and method priority. The Facebook access token is read from environment variable `FACEBOOK_ACCESS_TOKEN` (optional; if missing, Graph API method is skipped).
- **Modified `job()` function in `news_bot.py`** – Loads Facebook config, calls `fetch_facebook_posts`, merges entries with RSS entries, passes them through existing pipeline.
- **Extended error handling** – Wrap Facebook fetching in try‑catch, log errors, send admin notifications on persistent failures.

### How it works

1. On each run, `job()` loads RSS feeds as before, then loads `facebook_source.json` (if present) and reads the Facebook access token from environment variable `FACEBOOK_ACCESS_TOKEN`.
2. If Facebook source is enabled, `fetch_facebook_posts(config)` tries methods in order: RSS → Graph API (if token present) → HTML. If RSS is unavailable, it falls back to Graph API; if token is missing, Graph API is skipped.
3. Each method returns a list of posts with fields `link`, `title`, `published`, `message`, `images`.
4. Posts containing any keyword from `filter_keywords` are discarded.
5. Remaining posts are appended to `all_entries` with a `feed_url` field set to the Facebook page URL.
6. The unified list passes through `filter_new_entries` (duplicate detection via database) and `process_new_articles` (translation, summarization, Telegram posting).
7. Errors in any Facebook method are caught, logged, and the next method is attempted; if all methods fail, an admin notification is sent and the bot continues with RSS feeds.

### Shared resources

None – the Facebook fetcher uses its own HTTP session but does not share heavy resources across components. The SQLite database connection is already shared by the existing pipeline.

## Decisions

### Decision 1: Method priority (RSS > Graph API > HTML)
**Decision:** Try RSS first; if unavailable or returns no posts, attempt Graph API; if token missing or fails, fall back to HTML parsing.
**Rationale:** RSS is simplest and requires no authentication; Graph API is official and stable; HTML parsing is fragile and should be last resort. This matches user spec "Мы решили сначала проверить доступность RSS‑ленты Facebook...".
**Alternatives considered:** Using only HTML parsing (simpler but brittle); using only Graph API (requires token, may not be available to all users). The chosen order maximizes reliability while respecting Facebook's terms.

### Decision 2: Configuration approach [TECHNICAL]
**Decision:** Store Facebook‑specific settings (page URL, filter keywords, enabled flag, method priority) in a separate file `facebook_source.json`. The Facebook access token is read from environment variable `FACEBOOK_ACCESS_TOKEN` (optional; if missing, Graph API method is skipped).
**Rationale:** Keeps backward compatibility with existing RSS‑only deployments and follows security best practices by not storing tokens in plain‑text config files. The current `feeds.json` is a simple list of strings; changing its format would break existing installations and require migration. Environment variables are easier to manage in deployment environments.
**Alternatives considered:** Extending `feeds.json` to support objects (type, url, params) – more unified but introduces complexity and risk of breaking existing tests. A separate file is safer and can be added optionally.

### Decision 3: Keyword filtering implementation
**Decision:** Filter posts using case‑insensitive substring matching against a configurable list (`["event", "announcement", "coming soon", "glow-n-fire"]`). No regular expressions initially.
**Rationale:** User spec explicitly lists these keywords as exclusion criteria. Substring matching is sufficient and avoids false positives from regex complexity.
**Alternatives considered:** Regular expressions (more flexible but harder to configure), NLP classification (overkill). Simplicity wins for a Medium‑size feature.

### Decision 4: Language detection and translation
**Decision:** Rely on existing `transcreate_text` function which uses Google Translate with automatic source language detection. Only translate if detected language is English; otherwise keep original.
**Rationale:** User spec requires translation from English to Russian. The existing pipeline already supports automatic detection; we reuse it without modification.
**Alternatives considered:** Adding explicit language detection before translation (extra complexity). Using the existing detection is adequate.

### Decision 5: Image handling
**Decision:** Extract up to the first two images from each Facebook post (if available) and pass them to `send_to_telegram`. If no images, proceed with text only.
**Rationale:** User spec states "изображения (первые 1‑2)". The existing `send_to_telegram` already supports multiple images; we just need to extract image URLs from Facebook posts.
**Alternatives considered:** Downloading and re‑uploading images (unnecessary), ignoring images (would reduce content quality). Extracting 1‑2 is a reasonable balance.

### Decision 6: Error notification
**Decision:** Use existing `send_admin_notification` function for any parsing or API errors that cause the Facebook source to fail entirely. Errors are logged but do not stop processing of other RSS feeds.
**Rationale:** User spec requires "При ошибке парсинга ... отправляет уведомление администратору и продолжает работу с другими источниками". The existing notification mechanism meets this requirement.
**Alternatives considered:** Sending notifications via different channel (email, Slack), aggregating errors before sending. Reusing the existing Telegram notification is simplest.

### Decision 7: Facebook page identification [TECHNICAL]
**Decision:** Use the page URL (`https://www.facebook.com/hotwheels/`) as the primary identifier; extract page ID from URL if needed for Graph API.
**Rationale:** The URL is the only input provided by the user. Graph API requires a page ID, which can be derived from the URL (or looked up via API). This keeps configuration simple.
**Alternatives considered:** Requiring the user to provide both URL and page ID (extra configuration burden). Deriving ID automatically is more user‑friendly.

### Decision 8: Dependencies [TECHNICAL]
**Decision:** Use `requests` and `beautifulsoup4` (already in `requirements.txt`) for HTTP and HTML parsing; do not add `facebook‑sdk`.
**Rationale:** The Graph API can be accessed via simple HTTP requests; adding an extra SDK introduces another dependency without significant benefit. The existing project already uses `requests`.
**Alternatives considered:** Adding `facebook‑sdk` for easier token handling and rate limiting – but the SDK is heavier and may not be needed for simple post retrieval.

### Decision 9: Hashtag formatting
**Decision:** Append hashtags `#hotwheels #facebook` to the end of each Telegram post, after the original link.
**Rationale:** User spec explicitly includes these hashtags as part of the posting format. They help categorize posts in the channel.
**Alternatives considered:** Configurable hashtags (extra complexity), omitting hashtags (would deviate from spec). Implementing as specified.

## Data Models

DB schemas, interfaces, types. Skip if N/A.

## Dependencies

### New packages
- None (all required packages are already in `requirements.txt`).

### Using existing (from project)
- `feedparser` – RSS parsing (already used).
- `requests` – HTTP client for Graph API and HTML fetching.
- `beautifulsoup4` – HTML parsing.
- `deep_translator` – translation.
- `python‑telegram‑bot` – Telegram posting.
- `schedule` – scheduling.
- **Facebook Graph API access token** – provided via environment variable `FACEBOOK_ACCESS_TOKEN` (optional; required for Graph API method).
- Existing modules `news_bot.py`, `send_to_telegram`, `transcreate_text`, `summarize_text_with_limit`, `send_admin_notification` will be reused.

## Testing Strategy

**Feature size:** M

### Unit tests
- **Facebook RSS parsing** – mock RSS feed response, verify entry extraction.
- **Graph API response parsing** – mock JSON response, verify field mapping.
- **Rate‑limiting behavior** – simulate HTTP 429 responses, verify exponential backoff and retry logic.
- **Token expiration handling** – mock expired token responses, verify appropriate error handling and fallback.
- **HTML parsing** – mock HTML page, extract post text and images.
- **Keyword filtering** – test posts with/without keywords are filtered correctly.
- **Fallback chain** – simulate failures, ensure next method is attempted.
- **Configuration loading** – read `facebook_source.json` with valid/invalid data.

### Integration tests
- **Full Facebook source integration** – run `fetch_facebook_posts` with mocked HTTP responses, verify uniform entry format.
- **Integration with existing pipeline** – feed Facebook entries into `process_new_articles` (mocked Telegram) and verify translation, summarization, posting.
- **Error isolation** – simulate Facebook source failure, ensure RSS feeds still processed.

### E2E tests
- None (per user spec, E2E tests are not required for this Medium‑size feature due to external service dependencies).

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach
Agent verification follows the steps outlined in the user spec:

1. **Unit/integration test execution** – run all new tests (Facebook RSS parsing, Graph API response parsing, rate‑limiting behavior, token expiration handling, HTML parsing, keyword filtering, fallback chain) with mocked HTTP responses.
2. **Smoke test of Facebook parsing** – run a script that fetches a static HTML page mimicking a Facebook post, extracts title, text, images, and verifies keyword filtering passes/fails as expected.
3. **Integration with existing pipeline** – feed mock Facebook entries into `process_new_articles` and verify translation, summarization, and Telegram posting (using mocked Telegram API).
4. **Database verification** – after processing a mock post, check that its link appears in the `processed_news` table.
5. **Error handling verification** – simulate RSS/Graph API/HTML failures and ensure admin notification is sent and other RSS feeds continue.
6. **Post‑deploy verification** (if applicable) – after deployment, manually check that real Facebook posts (non‑event) appear in the Telegram channel with correct translation and hashtags.

Per‑task smoke checks are specified in each task's Verify‑smoke / Verify‑user fields in Implementation Tasks. Post‑deploy checks are described in the Post‑deploy verification task description.

### Tools required
- Python + pytest (for unit/integration tests)
- SQLite CLI (for database verification)
- curl (optional, for RSS endpoint testing)
- Telegram MCP (for post‑deploy channel inspection)
- bash (for running test scripts)

## Risks

| Risk | Mitigation |
|------|-----------|
| Facebook changes HTML structure or disables RSS | Implement fallback chain (RSS → Graph API → HTML); monitor errors and send admin notifications; consider Graph API as primary source. |
| Facebook blocks IP due to excessive requests | Respect Facebook's rate‑limit policies; implement exponential backoff with retry logic (e.g., 2‑second sleep between Graph API calls); optionally use proxy. |
| Keyword filtering false positives/negatives | Use configurable keyword list with case‑insensitive substring matching; allow manual tuning via config; consider regex patterns for edge cases. |
| Errors in Facebook parsing stop other RSS feeds | Isolate Facebook source with try‑catch; log errors and continue processing other feeds; send admin notification only on persistent failure. |
| Graph API access token expires | Document token renewal process; optionally implement token‑refresh logic; store token in environment variable for easy update. |
| HTML parsing fragility (page structure changes) | Rely on Graph API as primary method; treat HTML parsing as last‑resort fallback; alert admin when HTML parsing fails. |
| Increased runtime due to additional source | Keep Facebook fetching sequential with timeouts; consider fetching concurrently with RSS feeds (future optimization). |
| Backward compatibility break | Keep existing `feeds.json` format unchanged; introduce separate `facebook_source.json` config file; maintain existing tests. |

## Security considerations

- **URL validation**: Basic SSRF protection is implemented by validating that the Facebook page URL matches the expected domain pattern (`https://www.facebook.com/...`). This prevents the fetcher from being used to attack internal services.
- **Token storage**: The Facebook access token is stored in environment variable `FACEBOOK_ACCESS_TOKEN` rather than plain‑text configuration files, reducing risk of accidental exposure.

## User-Spec Deviations

<!-- Document every place where tech-spec deviates from, extends, or reinterprets user-spec.
     Each entry needs: requirement ID, what user-spec says, what tech-spec does, why, approval status.
     If no deviations — write "None". -->

- **Added: Graph API fallback** (not in user‑spec). User‑spec mentions only RSS and HTML parsing. Tech‑spec adds Graph API as an intermediate step between RSS and HTML to improve reliability and structure. Reason: Graph API is official, provides structured data, and is more stable than HTML scraping. → [PENDING USER APPROVAL]

<!-- Example entries:
- **US-3 (Push notifications):** user-spec says "real-time push", tech-spec uses polling every 5s instead. Reason: push infrastructure adds 2 weeks, polling meets latency requirements. → [PENDING USER APPROVAL]
- **Added: Rate limiting** (not in user-spec). Reason: public API endpoint needs protection from abuse. → [PENDING USER APPROVAL]
-->

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [ ] Facebook source configuration can be loaded from `facebook_source.json` (or environment variables) and validated.
- [ ] Facebook fetching methods (RSS, Graph API, HTML) are attempted in order; fallback works when earlier methods fail.
- [ ] Keyword filtering excludes posts containing any of the configured keywords (case‑insensitive).
- [ ] Filtered posts are transformed into uniform entry format with `link`, `title`, `published`, `message`, `images` fields.
- [ ] Facebook entries integrate with existing pipeline: duplicate detection via database works.
- [ ] Facebook entries pass through translation (if English) and summarization without errors.
- [ ] Telegram posts include hashtags `#hotwheels #facebook` and link to original Facebook post.
- [ ] Errors in Facebook source are caught and logged; admin notification sent for critical failures; other RSS feeds continue processing.
- [ ] Rate limiting respected (Facebook's rate‑limit policies, exponential backoff, and delays between requests).
- [ ] All new unit and integration tests pass; no regression in existing tests.

## Implementation Tasks

<!-- Tasks are brief scope descriptions. AC, TDD, and detailed steps are created during task-decomposition.

     Verify-smoke: concrete executable checks the agent runs during implementation — no deployment needed.
     Types: command (curl, python -c, docker build), MCP tool (Playwright, Telegram),
     API call (OpenRouter, external services), local server check, agent with test prompt.
     Verify-user: agent asks user to verify something (UI, behavior, experience).
     Both fields optional — omit if task is internal logic fully covered by tests. -->

### Wave 1 (независимые)

#### Task 1: Configuration schema and loader
- **Description:** Create `facebook_source.json` configuration file with schema: page URL, access token (optional), filter keywords, enabled flag, method priority. Write loader function that reads and validates config, falls back to environment variables.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python -c "from facebook_source import load_config; config = load_config(); print(config['page_url'])"` returns expected URL.
- **Files to modify:** `facebook_source.py` (new), `facebook_source.json` (new)
- **Files to read:** `news_bot.py` (for existing config loading pattern), `feeds.json`

#### Task 2: RSS and Graph API fetchers
- **Description:** Implement `fetch_facebook_rss(page_url)` and `fetch_facebook_graph(page_id, access_token)` functions that retrieve posts from Facebook RSS endpoint and Graph API, returning uniform entries with `link`, `title`, `published`, `message`, `images`. Handle HTTP errors and rate limits.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** Run unit test that mocks HTTP responses and verifies entry extraction.
- **Files to modify:** `facebook_source.py`
- **Files to read:** `news_bot.py` (fetch_rss pattern), `requirements.txt`

#### Task 3: HTML parser for Facebook posts
- **Description:** Implement `fetch_facebook_html(page_url)` that downloads public page HTML, extracts posts using BeautifulSoup selectors, and returns entries in same format. Handle missing elements gracefully.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** Parse a static HTML file (test fixture) and assert extracted fields match expected.
- **Files to modify:** `facebook_source.py`
- **Files to read:** `news_bot.py` (fetch_article for HTML parsing patterns)

#### Task 4: Keyword filtering and entry formatting
- **Description:** Implement `filter_posts_by_keywords(posts, keywords)` that excludes posts containing any keyword (case‑insensitive substring). Also implement function `format_facebook_entry(post)` that maps Facebook post fields to RSS‑like entry fields.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** Test filtering with sample posts containing/not containing keywords.
- **Files to modify:** `facebook_source.py`
- **Files to read:** none

### Wave 2 (зависит от Wave 1)

#### Task 5: Fallback orchestrator
- **Description:** Implement `fetch_facebook_posts(config)` that tries RSS, Graph API, HTML in order (skipping unavailable methods), applies keyword filtering, and returns list of uniform entries. Respect rate limits between requests.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** Run integration test with mocked HTTP responses for each method, verify fallback order.
- **Files to modify:** `facebook_source.py`
- **Files to read:** `facebook_source.py` (previous functions)

#### Task 6: Integration into job()
- **Description:** Modify `job()` in `news_bot.py` to load Facebook config, call `fetch_facebook_posts`, merge entries with RSS entries, add `feed_url` field, and pass through existing pipeline. Ensure error isolation (try‑catch around Facebook fetching).
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** Run the bot with Facebook config disabled, verify existing RSS feeds still work.
- **Files to modify:** `news_bot.py`
- **Files to read:** `news_bot.py` (job, load_feeds, fetch_rss, filter_new_entries, process_new_articles), `facebook_source.py`

#### Task 7: Error handling and admin notifications
- **Description:** Enhance error handling: catch exceptions in Facebook fetching, log detailed errors, send admin notification via `send_admin_notification` when all methods fail. Ensure other RSS feeds continue processing.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** Simulate a Facebook failure (e.g., invalid token) and verify notification sent and bot continues.
- **Files to modify:** `news_bot.py`, `facebook_source.py`
- **Files to read:** `news_bot.py` (send_admin_notification)

### Wave 3 (testing)

#### Task 8: Unit tests
- **Description:** Write comprehensive unit tests for each function in `facebook_source.py` (configuration loading, RSS parsing, Graph API parsing, HTML parsing, keyword filtering, fallback orchestrator). Use pytest and mocking.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** Run `pytest tests/test_facebook_source.py` and see all tests pass.
- **Files to modify:** `tests/test_facebook_source.py` (new)
- **Files to read:** `facebook_source.py`, existing test patterns from `tests/`

#### Task 9: Integration tests
- **Description:** Write integration tests that simulate the full pipeline: load config, fetch Facebook posts (mocked), feed into `process_new_articles` (mocked Telegram), verify translation, summarization, posting, and database marking.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** Run integration test suite and verify no regressions.
- **Files to modify:** `tests/integration/test_facebook_pipeline.py` (new)
- **Files to read:** `news_bot.py`, `facebook_source.py`, existing integration tests

### Audit Wave

<!-- Full-feature audit: 3 auditors review all code in parallel. Always present. -->
<!-- Auditors read code and write reports. If issues found — lead spawns a fixer, auditors become reviewers. -->

#### Task 10: Code Audit
- **Description:** Full-feature code quality audit. Read all source files created/modified in this feature (from decisions.md + tech-spec "Files to modify"). Review holistically for cross-component issues: duplicate resource initialization, shared resources compliance with Architecture decisions, architectural consistency. Write audit report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 11: Security Audit
- **Description:** Full-feature security audit. Read all source files created/modified in this feature. Analyze for OWASP Top 10 across all components, cross-component auth/data flow. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 12: Test Audit
- **Description:** Full-feature test quality audit. Read all test files created in this feature. Verify coverage, meaningful assertions, test pyramid balance across all components. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

<!-- QA is always present. Deploy and Post-deploy — only if applicable for this feature. -->

#### Task 13: Pre-deploy QA
- **Description:** Acceptance testing: run all tests, verify acceptance criteria from user-spec and tech-spec.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

#### Task 14: Deploy (if applicable)
- **Description:** Deploy updated bot to production environment, verify logs.
- **Skill:** infrastructure
- **Reviewers:** none

#### Task 15: Post-deploy verification (if applicable)
- **Description:** Live environment verification:
  - Check that Facebook posts appear in Telegram channel (non‑event posts).
  - Verify translation and hashtags are present.
  - Monitor logs for errors.
  Tools: Telegram MCP, curl, bash.
- **Skill:** post-deploy-qa
- **Reviewers:** none