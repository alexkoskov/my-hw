---
created: 2026-04-25
status: draft
branch: dev
size: S
---

# Tech Spec: mattel-parser-rewrite

## Solution

Rewrite `mattel_news_source.py` to extract Hot Wheels news from the embedded Next.js RSC flight payload (`self.__next_f.push([1, "..."])`) instead of the now-absent `<script id="__NEXT_DATA__">` JSON. Live verification on 2026-04-24 confirmed that listing entries with the same field names (`handle`, `title`, `date`, `excerpt`, `seo_description`, `thumbnail`) are still emitted — they just live inside the largest streaming chunk under the anchor `"article2":{"entries":[...]`. Article-page bodies are now referenced by `body: "$<row-id>"`, which resolves to a separate text-row marker `<row-id>:T<hex-len>,<content>` that may span multiple flight chunks.

The rewrite preserves the public surface 1:1 (`fetch_mattel_news`, `fetch_mattel_article`, `MattelNewsError`, the constants, and the internal helpers `_is_hotwheels` / `_build_entry` / `_notify` / `_extract_entries` whose names are imported by unit tests) so `news_bot.py` and the registry plumbing keep working without changes. No new dependencies — only `re`, `json`, `time`, `requests`, and `beautifulsoup4` are used. The old fixture file `tests/fixtures/mattel_news.html` is deleted in favour of a Python builder module (`tests/fixtures/mattel_flight_builder.py`) that synthesises HTML matching the live flight format, so per-test fixtures are deterministic and edge-case-friendly.

The migration is shipped as a single atomic wave: parser, tests, and fixture-builder change together because the parser↔test contract is bidirectional and any staged intermediate state would leave the test suite red.

## Architecture

### What we're building/modifying

- **`mattel_news_source.py`** — full body rewrite. Public exports preserved verbatim. Adds private helpers `_iter_flight_payloads`, `_concat_flight`, `_extract_listing_entries` (replaces `_extract_entries` body), `_extract_article_entry`, `_find_entry_by_handle_or_url`, `_resolve_body_html`, `_paragraphs_from_body`, `_excerpt_to_str`. Internal helpers `_is_hotwheels`, `_build_entry`, `_notify` keep names + signatures so existing unit tests in `TestIsHotwheels` / `TestBuildEntry` continue to pass without changes.
- **`tests/fixtures/mattel_flight_builder.py`** — new shared module exporting `_make_flight_listing(entries)` and `_make_flight_article(entry, body_html=None, body_chunks=1, truncate=False)`. Builds synthetic HTML whose RSC-flight obramlenie matches the live 2026-04-24 snapshots, so the parser sees the same anchors in synthetic and live HTML.
- **`tests/test_mattel_news_source.py`** — test infrastructure rewritten: drops file-fixture loader and `_article_page` helper, imports builders. Test-class structure preserved. 15 keep + 11 update + 6 new (3 from code-research §11.7 plus 3 added in round-2 to close coverage gaps for AC3, AC6 dict-form, AC7 url-fallback).
- **`tests/test_mattel_integration.py`** — fixture source swap; test bodies and assertions unchanged. 1 keep + 2 update.
- **`tests/fixtures/mattel_news.html`** — deleted (1.16 MB, no longer needed).

### How it works

**Listing path (`fetch_mattel_news`):**

1. HTTP GET on `https://corporate.mattel.com/news` — Chrome UA, 15 s timeout, 5 MB guard, `allow_redirects=False` (Decision 8).
2. `MAX_RESPONSE_SIZE` enforced BEFORE any regex/JSON parsing.
3. `_extract_listing_entries(html)` runs the pipeline: regex-find all `self.__next_f.push([1, "..."])` payloads (linear-time pattern with character-class body, no `(.+?)` — Decision 8) → JS-string-unescape each via `json.loads('"' + raw + '"')` → pick the chunk containing the listing anchor `"article2":{"entries":[` → bracket-match the array (max-iter cap, hex-length sanity bound — Decision 8) → `json.loads` the slice → return the list of entry dicts.
4. Filter via `_is_hotwheels` (substring match on `title` / `handle`) — unchanged.
5. Build feedparser-shaped dicts via `_build_entry` — unchanged 5-key shape (`link`, `title`, `summary`, `published_parsed`, `feed_url`).

**Article path (`fetch_mattel_article`):**

1. **SSRF guard:** before any HTTP call, validate `link.startswith(ARTICLE_URL_PREFIX)`; on mismatch raise `MattelNewsError("invalid article link prefix")` → notifier → `None` (Decision 8 / ES10).
2. HTTP GET on the article URL — same headers/guards as listing, `allow_redirects=False`.
3. `_extract_article_entry(html, link)` concatenates ALL flight pushes into a single unescaped string (boundary-blind for body resolution), locates `"article2":{"entries":[`, finds the entry whose `handle` matches the URL slug (fallback: scan for entry `url == link`). Raises `MattelNewsError("article entry not found ...")` if neither hits.
4. Read entry's `body` field (`"$<row-id>"`); if absent → returns dict with `paragraphs=[]`, no notifier (AC9).
5. `_resolve_body_html(concat, body_ref)` finds `<row-id>:T<hex-len>,` in the concat and reads exactly `<hex-len>` chars of content. Hex length is parsed `int(hex, 16)` and capped at `MAX_RESPONSE_SIZE` (Decision 8) — anything above the cap is treated as content-empty. If the marker is missing or the advertised length exceeds the available stream → returns `""`, treated as content-empty (AC9).
6. `_paragraphs_from_body(body_html)` walks the BeautifulSoup tree (`p`, `li`, `h1`–`h4`) and emits text — same logic the current code has inline.
7. Thumbnail-only image policy preserved: `images = [entry["thumbnail"]["url"]]` if present, else `[]`. `download_media` ignored.

**Error handling — error matrix (inline; preserves fail-soft contract from Decision 6):**

| ES | Scenario | Exception class | log level | notifier message format | return |
|---|---|---|---|---|---|
| ES1 | Listing HTTP error (`requests.RequestException`) | swallowed at boundary | `logger.error` | `"Mattel news HTTP error: <type>"` (sanitised — see Decision 8) | `[]` |
| ES2 | Listing response > 5 MB | `MattelNewsError` raised + caught | `logger.error` | `"Mattel news response too large: <bytes> bytes"` | `[]` |
| ES3 | Listing has no flight pushes (incl. old `__NEXT_DATA__` rollback HTML) | `MattelNewsError("flight payload not found")` | `logger.error` | `"Mattel news parsing error: flight payload not found"` | `[]` |
| ES4 | Listing flight present but no `article2.entries` anchor | `MattelNewsError("article2.entries not found")` | `logger.error` | `"Mattel news parsing error: article2.entries not found"` | `[]` |
| ES5 | Listing entries fail to JSON-decode (incl. `RecursionError` on deep nesting) | `MattelNewsError("invalid JSON in entries array: <type>")` | `logger.error` | `"Mattel news parsing error: invalid JSON in entries array: <type>"` | `[]` |
| ES6 | Article HTTP 4xx/5xx (`raise_for_status` → `HTTPError`) | swallowed | `logger.error` | `"Mattel article fetch error (<link>): <type>"` (sanitised) | `None` |
| ES7 | Article HTTP non-status error (timeout, connection) | swallowed | `logger.error` | same shape as ES6 | `None` |
| ES8 | Article > 5 MB | `MattelNewsError` | `logger.error` | `"Mattel article response too large: <bytes> bytes"` | `None` |
| ES9 | Article flight has no `article2.entries` | `MattelNewsError("article2.entries not found")` | `logger.error` | `"Mattel article fetch error (<link>): article2.entries not found"` | `None` |
| ES9b | Article entry not found by handle/url | `MattelNewsError("article entry not found for handle: <slug>")` | `logger.error` | `"Mattel article fetch error (<link>): article entry not found for handle: <slug>"` | `None` |
| ES9c | Body resolution: row marker missing OR length > available OR length > MAX_RESPONSE_SIZE (per AC9) | NOT raised — content-empty path | `logger.debug` only (no notifier) | none | dict with `paragraphs=[]` |
| ES10 | SSRF guard: link does not start with `ARTICLE_URL_PREFIX` | `MattelNewsError("invalid article link prefix")` | `logger.error` | `"Mattel article fetch error: invalid article link prefix"` (link itself NOT echoed to avoid amplifying the malicious URL into admin chat) | `None` |

`_notify` swallows notifier exceptions (current behavior at `mattel_news_source.py:134-141`). Preserved.

**Backward-rollback handling:** if Mattel rolls back to old `__NEXT_DATA__` HTML, the listing path returns `[]` via ES3 ("flight payload not found"). Operator gets a single notifier ping per cron tick — same fail-soft surface as today's silent regression, just with a different message. No automatic dual-format support; rollback recovery is a separate feature per Decision 5.

### Shared resources

None. No DB pools, no LLM clients, no browser instances. Module-level compiled regex is standard Python practice, not a heavy shared resource. Single `requests.Session` remains an optional caller-passed parameter (existing pattern).

## Decisions

### Decision 1: parse RSC flight payload directly (no headless / no API discovery)

**Decision:** Replace `__NEXT_DATA__` extraction with a regex over `self.__next_f.push([1, "..."])`, JS-string-unescape, and bracket-match on `"article2":{"entries":[`.
**Rationale:** Supports user-spec "Что делаем" and AC1–AC4. Live snapshot 2026-04-24 (code-research §5) confirms data is present; parsing is a pure-stdlib + regex + `json.loads` pipeline; no new dependencies.
**Alternatives considered:**
- Headless browser (`playwright`) — rejected: +100 MB on VPS, more moving parts; user-spec Constraint "Без новых зависимостей".
- Undocumented Builder.io / Contentstack API — rejected: fragile, may be blocked; out of scope.
- Brute-force `<p>`-scan over the concatenated flight — rejected: article pages render the sidebar listing in addition to the article body, so a flat scan would mix in unrelated paragraphs (code-research §6).

### Decision 2: anchor on semantic markers, not positional ones

**Decision:** Find the listing chunk by substring `"article2":{"entries":[` rather than "row 6" or "the biggest push." Locate the body row by the entry's `body: "$<id>"` reference, not a hardcoded row id.
**Rationale:** Mitigates user-spec Risk 1 (future RSC layout drift). When Mattel/Builder.io re-tags the data, semantic anchors keep working as long as the field names stay the same; positional anchors would silently break.
**Alternatives considered:**
- Hardcode "biggest push wins" — rejected: subset of "search all pushes for the anchor"; same cost (one substring search per push).
- Hardcode "row 6" — rejected: row ids are build-stamped by Next.js and change per release.

### Decision 3: preserve internal helper names so existing unit tests keep working

**Decision:** Keep `_is_hotwheels`, `_build_entry`, `_notify`, `_extract_entries` as importable names with unchanged signatures. The body of `_extract_entries` is rewritten; the others are unchanged.
**Rationale:** Supports user-spec AC11 (import preservation) at the unit-test boundary. `tests/test_mattel_news_source.py` imports these names directly (lines 12–22 currently); minimising churn keeps `TestIsHotwheels` and `TestBuildEntry` (10 tests) green by construction. `[TECHNICAL]` choice — the user-spec only requires the public exports, but extending preservation to these internal names cuts test changes from ~26 to ~14.

### Decision 4: shared `mattel_flight_builder.py` module instead of per-test inline helpers

**Decision:** Place `_make_flight_listing` and `_make_flight_article` in `tests/fixtures/mattel_flight_builder.py`; both `test_mattel_news_source.py` and `test_mattel_integration.py` import from it.
**Rationale:** Supports user-spec Decision "строить фикстуры через Python helper-функции". Single source of truth for how the synthetic HTML is shaped; integration tests stay focused on integration concerns; future changes to the flight format need only one file edited.
**Alternatives considered:**
- Inline helpers in `test_mattel_news_source.py` only, with `test_mattel_integration.py` importing the file — rejected: cross-test-file imports under `tests/` are brittle (collection order, side-effects). Shared module under `tests/fixtures/` is the established pattern.

### Decision 5: Shape A wave — atomic single-wave rewrite

**Decision:** Ship parser, builder, and both test files in one implementation task. No staged waves.
**Rationale:** Supports user-spec AC11–AC13. Parser↔test contract is bidirectional: tests assert on parser output; parser reads builder output. Splitting into separate waves creates ever-broken intermediate state (either tests reference `__NEXT_DATA__` while parser doesn't, or vice versa). Total work ~450 lines of code change; well within a single-task scope.
**Alternatives considered:**
- Wave A (builder) → Wave B (parser + tests) → Wave C (smoke) — rejected: introduces a half-deleted `mattel_news.html` fixture state that doesn't compile cleanly (code-research §11.8).

### Decision 6: error matrix — preserve fail-soft contract

**Decision:** Every error path calls `_notify(notifier, message)` and returns `[]` / `None`. The full 12-row matrix (ES1–ES10) is reproduced inline in Architecture → "How it works" → "Error handling" — single source of truth for reviewers and implementers.
**Rationale:** Supports user-spec Constraint "Fail-soft с админ-уведомлением" and AC4 / AC5 / AC7. The current module's contract — `news_bot.job()` keeps running on per-source failure — is preserved verbatim. ES9c (body-row missing or advertised length > available) is intentionally NOT a notifier-trigger, per AC9 (content-empty path).

### Decision 7: anti-drift snapshot smoke test

**Decision:** Add two skip-guarded tests in `tests/test_mattel_news_source.py` that parse `/tmp/mattel_news.html` and `/tmp/mattel_article.html` if present. Skipped in CI (snapshots absent); run locally when the operator captures snapshots before validation.
**Rationale:** Mitigates user-spec Risk 2 (synthetic-fixture / live-format divergence — the exact failure mode that produced this outage). Cost in CI is zero (skip); cost when present is one ~50 ms parse. Catches builder-vs-live drift the synthetic tests can't see.
**Alternatives considered:**
- Live HTTP hit in CI — rejected: flaky, Mattel may block, HW gaps are normal (user-spec testing strategy).
- No anti-drift check — rejected: adequacy round-1 explicitly flagged this gap and it's the highest-impact remaining failure mode.

### Decision 8: security hardening for the HTTP scraper boundary

**Decision:** [TECHNICAL] Apply six security controls absent from the current module and not explicit in user-spec:

1. **SSRF guard.** `fetch_mattel_article(link)` validates `link.startswith(ARTICLE_URL_PREFIX)` before any HTTP call; mismatch → `MattelNewsError` (ES10) → notifier (link itself NOT echoed) → `None`.
2. **Linear-time push regex.** Replace catastrophic-backtracking-prone `r'self\.__next_f\.push\(\[\s*1\s*,\s*"(.+?)"\s*\]\)'` with a JS-string-literal-correct character-class form: `r'self\.__next_f\.push\(\[\s*1\s*,\s*"((?:[^"\\\\]|\\\\.)*)"\s*\]\)'`. Eliminates `.+?` lazy backtracking and correctly handles JS-escaped `\"` inside push bodies.
3. **JSON depth/recursion guard.** Wrap `json.loads` calls (both unescape and entries-array decode) in `try / except (json.JSONDecodeError, RecursionError, ValueError)` → ES5. Mattel HTML is bounded by `MAX_RESPONSE_SIZE = 5 MB`, so memory is bounded; the only remaining risk is `RecursionError` on deeply nested adversarial JSON.
4. **Bracket-match safety.** `_extract_listing_entries` bracket-match runs with an explicit max-iteration cap (`len(unescaped)` — depth and string-literal aware: skip over `"..."` runs and `\\` escapes); if depth never returns to zero by EOF → ES4. `_resolve_body_html` parses hex length, but rejects any value > `MAX_RESPONSE_SIZE` (treated as content-empty per ES9c) so an attacker-supplied `int("ffffffff", 16)` cannot trigger an OOM slice.
5. **Notifier sanitisation.** Notifier messages format only the exception **type** + safe scalars (sizes, anchor names, slugs). Raw `str(exc)` is NOT included — prevents URL/header/cookie leakage from `requests` exceptions into the admin chat.
6. **Redirect & log hygiene.** `requests.get(..., allow_redirects=False)` for both fetches — Mattel's URLs are stable and a redirect would only mask CDN-edge surprises. The existing `httpx`/`urllib3`/`httpcore` INFO suppression in `news_bot._configure_third_party_logging` is preserved unchanged (Mattel module never touches logger config).

**Rationale:** Round-1 security review (`security-review.json`) flagged 1 critical (SSRF) and 3 major (ReDoS, JSON depth, bracket-match safety) findings; these controls close all of them at trivial code cost. None of these were called out in user-spec, so the decision is `[TECHNICAL]` and recorded in `User-Spec Deviations`.

**Alternatives considered:**
- Skip these and accept residual risk — rejected: `fetch_mattel_article(link)` is reachable from a hypothetical future entry source whose URLs aren't sanity-checked, and CDN edge changes can produce malformed responses that crash the cron tick. Cost of these controls is ~30 lines of code.

## Data Models

No new types or schemas. Output shapes are unchanged from the current contract:

```python
# fetch_mattel_news → List[dict] with 5 keys per entry
{
    "link": str,                       # ARTICLE_URL_PREFIX + handle
    "title": str,
    "summary": str,                    # excerpt or title fallback
    "published_parsed": time.struct_time | None,  # from "YYYY-MM-DD"
    "feed_url": str,                   # NEWS_URL
}

# fetch_mattel_article → dict with 4 keys, or None on error
{
    "title": str,
    "subtitle": str,                   # excerpt (text-form), "" if absent
    "paragraphs": List[str],
    "images": List[str],               # [thumbnail_url] or []
}
```

Internal entry-dict shape from `article2.entries` (consumed only inside the module): `{handle, title, date (YYYY-MM-DD), excerpt (str | dict | ""), seo_description, thumbnail: {url}, body: "$<row-id>" | absent, download_media: [...] | [], url, ...other Contentstack fields}`. We rely only on the bolded keys; the rest is ignored.

## Dependencies

### New packages

None.

### Using existing (from project)

- `requests==2.32.3` — `requests.get` for the listing/article fetches; `RequestException` / `HTTPError` for error matrix.
- `beautifulsoup4==4.12.3` — `BeautifulSoup(body_html, "html.parser")` and `find_all(["p", "li", "h1", "h2", "h3", "h4"])` for paragraph extraction.
- stdlib `re` — single compiled flight-push regex.
- stdlib `json` — JS-string unescape (`json.loads('"' + raw + '"')`) + entries-array decode.
- stdlib `time` — `time.strptime(date, "%Y-%m-%d")`.
- stdlib `logging` — module logger; preserve `logger.error` log level.

## Testing Strategy

**Feature size:** S (1 module rewrite + 2 test files updated + 1 fixture builder added; contract preserved; no DB / API / UI surface).

### Unit tests

`tests/test_mattel_news_source.py` — 29 tests after the rewrite (15 keep + 11 update + 3 new):

- **`TestIsHotwheels`** (5 keep): substring match on `title` and `handle`, case-insensitive, missing-fields. Parser-agnostic.
- **`TestBuildEntry`** (5 keep): 5-key dict assembly, excerpt-fallback to title, missing-handle → None, missing-title → None, invalid date → entry kept with `published_parsed=None`. Parser-agnostic.
- **`TestExtractEntries`** (4 update): synthetic HTML via `_make_flight_listing`. Cases: extracts N entries, missing flight payload raises with clear message, invalid JSON raises with clear message, missing `article2.entries` anchor raises with clear message.
- **`TestFetchMattelNews`** (5 keep + 4 update + **1 NEW for AC3**): success path filters HW only (1 of 3 → 1 returned); HTTP error / connection error / oversized response / no-notifier / notifier-failure paths preserved; missing-payload / invalid-JSON / missing-anchor paths assert new error messages. **NEW `test_listing_with_no_hotwheels_returns_empty_without_notifier`** (AC3): build flight HTML with 3 non-HW entries; assert `fetch_mattel_news()` returns `[]` AND `notifier.assert_not_called()` — closes the silent-zero contract that motivated this feature.
- **`TestFetchMattelArticle`** (1 keep + 5 update + 3 new + **2 NEW for AC6/AC7**):
  - paragraphs + thumbnail-only image policy (regression test, locked by patterns.md);
  - thumbnail absent → `images=[]` even if `download_media` non-empty;
  - empty excerpt → empty subtitle;
  - HTTP error / oversized → None + notifier;
  - missing payload / article-entry-not-found → None + notifier (renamed from `null_content_article`);
  - **NEW** body split across multiple chunks → paragraphs come out in order, no gaps/dupes (AC8);
  - **NEW** body absent → `paragraphs=[]`, notifier NOT called (AC9 first half);
  - **NEW** truncated body (advertised length > available) → `paragraphs=[]`, notifier NOT called (AC9 second half);
  - **NEW `test_article_falls_back_to_url_field_when_handle_mismatch`** (AC7): build flight where the entry's `handle` doesn't match the URL slug but `url` field equals the link — assert article is returned correctly;
  - **NEW `test_dict_form_excerpt_extracts_text_field`** (AC6): build flight with `excerpt: {"text": "<actual>"}` — assert returned `subtitle == "<actual>"`.
- **`TestSsrfGuard`** (1 NEW for ES10): pass `link="https://evil.example.com/news/foo"` to `fetch_mattel_article` — assert `None` returned, notifier called with sanitised message that does NOT echo the malicious URL.
- **Anti-drift smoke** (2 NEW, skip-guarded): parse `/tmp/mattel_news.html` if present and assert listing returns a list with no notifier call; parse `/tmp/mattel_article.html` if present and assert article returns a dict with no notifier call.

### Integration tests

`tests/test_mattel_integration.py` — 3 tests (1 keep + 2 update):

- `test_mattel_post_flows_into_pending_queue` — fixture swap (build via `_make_flight_listing` with one HW entry + filler); assertions unchanged (`len(rows) == 1`, `source_name == 'mattel'`, `'hot-wheels' in row['link']`).
- `test_mattel_http_failure_does_not_crash_job` — keep verbatim (no fixture used).
- `test_mattel_duplicate_is_not_restaged` — same fixture swap as test 1.

### E2E tests

None — live `corporate.mattel.com` is flaky, may block, and HW posts gap by weeks. Manual smoke (AC13) and the operator's 7-day post-deploy observation cover what E2E would. Documented in user-spec testing section.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

Per-task `Verify-smoke` runs concrete commands during implementation; Final-Wave QA replays the full suite and verifies acceptance criteria. The auditors in the Audit Wave write reports only — they don't fix.

Concrete commands (from code-research §11.10):

1. `pytest tests/test_mattel_news_source.py tests/test_mattel_integration.py -v` — covers AC1, AC3, AC6, AC7, AC8, AC9, AC10, AC12 via the rewritten suite.
2. `pytest tests/ -q` — full repo green; verifies no regressions in `test_sources_registry`, `test_feed_iteration`, `test_integration`.
3. `python3 -c "from mattel_news_source import fetch_mattel_news, fetch_mattel_article, MattelNewsError, NEWS_URL, ARTICLE_URL_PREFIX, MAX_RESPONSE_SIZE; print('ok')"` — covers AC11.
4. `python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())"` — covers AC13 (live listing smoke).
5. (Conditional) `python3 -c "from mattel_news_source import fetch_mattel_article; print(fetch_mattel_article('<HW link>'))"` — only when a HW post happens to be in the live listing.
6. Anti-drift: `python3 -c "..."` parsing `/tmp/mattel_news.html` and asserting >=0 entries, no notifier — operator-side mitigation for Risk 2.

Operator-side post-deploy verification (from user-spec):
- 12+h after deploy: eyeball Telegram admin chat — expect zero `Mattel news parsing error` messages.
- 7-day window or until next HW press release: monitor `pending_articles` for a `source_name='mattel'` row.

### Tools required

`bash`, `pytest`, `python3`. No MCP tools required (no Playwright, no Telegram MCP — backend parser, no UI).

## Risks

| Risk | Mitigation |
|------|-----------|
| Future RSC layout drift (Mattel/Next.js shuffles row ids, renames `article2`, changes chunk split) | Anchor on semantic markers (`"article2":{"entries":[`, field names) — not positional. Notifier fires on structural break, operator opens follow-up feature. |
| Synthetic fixture diverges from live format (the exact failure mode that produced this outage) | Builder anchors on the same markers the parser reads (Decision 4); anti-drift smoke tests guarded on `/tmp/mattel_*.html` (Decision 7) catch it locally. Operator captures live snapshots pre-deploy. |
| Body content spans many chunks or has nontrivial JS-string escapes | `_concat_flight` joins all pushes before scanning; AC8 / AC9 explicitly tested via `_make_flight_article(body_chunks=N, truncate=...)`. |
| Cloudflare interstitial appears on Mattel (as on autoevolution) | Not addressed in this rewrite — visible when 200 OK turns into 403; future feature switches to `curl_cffi` (already in stack). |
| Wayback doesn't carry the new format | Operator pre-deploy snapshots `/tmp/mattel_news.html` locally as a one-shot debug aid. Documented in user-spec, accepted residual. |
| Adversarial / malformed HTML (deeply nested JSON, oversized hex length, ReDoS-bait push, malicious link to `fetch_mattel_article`) | Decision 8: SSRF link-prefix guard, linear-time string-aware regex, `RecursionError` catch, hex-length cap at `MAX_RESPONSE_SIZE`, depth-and-string-aware bracket-match, sanitised notifier (no raw `str(exc)`). All round-1 security findings (1 critical + 3 major) closed. |

## User-Spec Deviations

The user-spec covers behavior and contract; the items below extend or refine the implementation surface beyond what the user-spec explicitly states. Each is `[TECHNICAL]` (not contradicting any AC) — listed so the user can review the deltas.

- **Decision 3: preserve internal helper names (`_is_hotwheels`, `_build_entry`, `_notify`, `_extract_entries`).** User-spec AC11 only requires preserving public exports + `MattelNewsError`. Tech-spec extends preservation to four internal helpers used by the test suite (`tests/test_mattel_news_source.py:12-22` imports them). **Why:** test-stability optimisation — cuts test changes from ~26 to ~14 by keeping `TestIsHotwheels` and `TestBuildEntry` (10 tests) green by construction. → [PENDING USER APPROVAL]
- **Decision 4: shared `tests/fixtures/mattel_flight_builder.py` module.** User-spec testing section says "helper-функции в тестовом файле." Tech-spec extracts them to a shared module under `tests/fixtures/` so both `test_mattel_news_source.py` and `test_mattel_integration.py` can import without cross-test-file fragility. **Why:** integration tests stay focused on integration concerns; single source of truth for the synthetic-HTML format. → [PENDING USER APPROVAL]
- **Decision 7: anti-drift snapshot smoke tests** (`/tmp/mattel_*.html` skip-guarded). User-spec lists "Manual smoke (AC13)" as the operator-side check; it does not specify an in-suite test. Tech-spec adds two `pytest.skip`-guarded tests that exercise live snapshots when the operator captures them locally. **Why:** mitigates user-spec Risk 2 (synthetic-fixture / live-format divergence — the exact failure mode that produced this outage); CI pays zero cost (skipped); local validation gains real-format coverage. → [PENDING USER APPROVAL]
- **Decision 8: security hardening (SSRF, regex, JSON depth, bracket-match, notifier sanitisation, redirect off).** User-spec is silent on security controls — it focuses on behavior. Tech-spec adds six controls absent from the current module after a security-review round-1 finding flagged 1 critical (SSRF) + 3 major issues. **Why:** zero behavioral change; closes adversarial-input failure modes at trivial cost; preserves admin-chat hygiene. → [PENDING USER APPROVAL]

## Acceptance Criteria

Технические критерии приёмки (дополняют пользовательские из user-spec):

- [ ] Module-level regex `_FLIGHT_PUSH_RE` compiled once; not recompiled per call.
- [ ] `_extract_entries` keeps name + signature so `tests/test_mattel_news_source.py:18` import doesn't break (Decision 3).
- [ ] `_NEXT_DATA_RE` constant deleted from the module; no remaining references in `mattel_news_source.py`.
- [ ] `tests/fixtures/mattel_news.html` deleted from the working tree and from git history (`git rm`).
- [ ] `tests/fixtures/mattel_flight_builder.py` exports `_make_flight_listing(entries)` and `_make_flight_article(entry, body_html=None, body_chunks=1, truncate=False)` matching code-research §11.4 signatures.
- [ ] Anti-drift smoke tests use `pytest.skip(...)` when `/tmp/mattel_*.html` is missing — never fail CI.
- [ ] Test count after rewrite: 32 in `test_mattel_news_source.py` (15 keep + 11 update + 6 new), 3 in `test_mattel_integration.py` (1 keep + 2 update).
- [ ] `pytest tests/ -q` green; no other test files (`test_sources_registry.py`, `test_feed_iteration.py`, `test_integration.py`) require any change.
- [ ] No new entries in `requirements.txt`.

**Security ACs (Decision 8):**

- [ ] `fetch_mattel_article(link)` rejects `link` that does not start with `ARTICLE_URL_PREFIX` — returns `None`, notifier called once with a sanitised message that does NOT echo `link`.
- [ ] `_FLIGHT_PUSH_RE` uses character-class form `(?:[^"\\\\]|\\\\.)*` (no `(.+?)` lazy quantifier); regex is JS-string-literal-correct against escaped `\"`.
- [ ] All `json.loads` calls in the module are wrapped to catch `(json.JSONDecodeError, RecursionError, ValueError)` → ES5.
- [ ] `_resolve_body_html` rejects advertised hex lengths above `MAX_RESPONSE_SIZE` (returns `""`, ES9c content-empty path).
- [ ] `_extract_listing_entries` bracket-match is depth-aware AND string-literal-aware (skips inside `"..."` runs and `\\` escapes); on EOF without depth-zero → ES4.
- [ ] Notifier messages format only exception **type** and safe scalars (sizes, slugs, anchor names) — raw `str(exc)` is NOT included.
- [ ] Both `requests.get` calls pass `allow_redirects=False`.
- [ ] `httpx`/`urllib3`/`httpcore` log-suppression (set in `news_bot._configure_third_party_logging`) is NOT touched by this module.

**Verification:**

- [ ] All AC1–AC13 from user-spec verifiable via the verification plan commands above.
- [ ] All ES1–ES10 from the inline error matrix have a corresponding test.

## Implementation Tasks

### Wave 1 (single atomic task)

#### Task 1: Rewrite mattel_news_source + tests + fixture builder

- **Description:** Replace `__NEXT_DATA__` extraction in `mattel_news_source.py` with RSC-flight-payload parsing while preserving the public surface 1:1. Update both Mattel test files and add `tests/fixtures/mattel_flight_builder.py`; delete the old HTML fixture. Result: `pytest tests/ -q` green; live smoke does not call notifier. See Decisions 1–8 for rationale and the inline error matrix for behavior.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:**
  - `cd /workspaces/debian-2/my-hw && pytest tests/test_mattel_news_source.py tests/test_mattel_integration.py -v` → all green
  - `cd /workspaces/debian-2/my-hw && pytest tests/ -q` → all green
  - `cd /workspaces/debian-2/my-hw && python3 -c "from mattel_news_source import fetch_mattel_news, fetch_mattel_article, MattelNewsError, NEWS_URL, ARTICLE_URL_PREFIX, MAX_RESPONSE_SIZE; print('ok')"` → `ok`
  - `cd /workspaces/debian-2/my-hw && python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())"` → `[]` or list of dicts; STDERR clean (no parsing-error log lines)
- **Files to modify:** `mattel_news_source.py`, `tests/test_mattel_news_source.py`, `tests/test_mattel_integration.py`, `tests/fixtures/mattel_news.html` (delete), `tests/fixtures/mattel_flight_builder.py` (create)
- **Files to read:** `news_bot.py` (lines 24, 955–976, 1037–1047, 1053–1056 — confirm import-side contract; do NOT modify), `pending_articles_repo.py:166–199` (downstream column shape), `tests/test_sources_registry.py` (registry assertions still valid), `work/mattel-parser-rewrite/code-research.md` (§§5, 6, 11.1–11.10 — implementation map), `/tmp/mattel_news.html` and `/tmp/mattel_article.html` if present (live snapshots for builder validation)

### Audit Wave

#### Task 2: Code Audit
- **Description:** Full-feature code quality audit of `mattel_news_source.py` and `tests/fixtures/mattel_flight_builder.py`. Verify Decisions 1–7 are honored: semantic anchors only (no row-id hardcoding); preserved internal helper names; no recompilation of regex per call; fail-soft notifier contract preserved; module-level shared resources match Architecture; no duplicate `requests.Session` creation. Write audit report.
- **Skill:** code-reviewing
- **Reviewers:** none

#### Task 3: Security Audit
- **Description:** Full-feature security audit. Read `mattel_news_source.py`, `tests/fixtures/mattel_flight_builder.py`, and adjacent test code. Cover OWASP-relevant concerns for an HTTP scraper: SSRF (URL validation, no user-controlled URLs), input-size DoS (`MAX_RESPONSE_SIZE` guard active and enforced before parsing), regex catastrophic-backtracking (`_FLIGHT_PUSH_RE` complexity), JSON-parse memory bounds, secret hygiene (no token/credential leak through notifier message), exception-info leakage in admin pings. Write audit report.
- **Skill:** security-auditor
- **Reviewers:** none

#### Task 4: Test Audit
- **Description:** Full-feature test-quality audit. Read both rewritten test files + the new fixture-builder module. Verify: meaningful assertions (not just "doesn't raise"); explicit AC8/AC9 coverage with multi-chunk + truncated cases; no leftover `__NEXT_DATA__` references; thumbnail-only regression test still locks the policy; integration tests use the builder, not the deleted file fixture; anti-drift smoke tests are skip-guarded so CI never fails for a missing snapshot. Write audit report.
- **Skill:** test-master
- **Reviewers:** none

### Final Wave

#### Task 5: Pre-deploy QA
- **Description:** Acceptance testing: run `pytest tests/ -q`, `pytest tests/test_mattel_news_source.py tests/test_mattel_integration.py -v`, the import-smoke one-liner, the live-listing smoke one-liner, and the anti-drift snapshot smoke (only if `/tmp/mattel_news.html` and `/tmp/mattel_article.html` are present locally). Verify every AC from user-spec (AC1–AC13) and every tech-spec AC. Write QA report; flag any user-spec AC that can't be exercised pre-deploy (e.g., AC13 live smoke when network is unavailable) for deferral to post-deploy.
- **Skill:** pre-deploy-qa
- **Reviewers:** none

(No Deploy / Post-deploy tasks in this feature: deploy is operator-side via `bash deploy.sh` per `project_post_mrw_pending` open item — not part of this feature's scope. The operator-side post-deploy checks listed in user-spec "Пользователь проверяет" run outside the agent pipeline.)
