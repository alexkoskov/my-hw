# Code Audit — orangetrack-source feature

**Branch:** `dev` (commits c420aae, 234f6dc, 302f60f, 5854ec0)
**Date:** 2026-05-04
**Reviewer:** Senior Software Architect (code-reviewing skill)

**Files audited (final state on dev):**
- `orangetrack_source.py` (846 LoC; spec said ~270-320, final ~620 productive + 226 docstring/blank)
- `news_bot.py` — `_fetch_orangetrack_entries` (lines 1383–1473), `fetch_full_article` orangetrack branch (lines 1280–1298), `NETLOC_TO_SOURCE` / `SOURCE_EMOJI` / `SOURCE_LABEL` / `SOURCES` registry
- `boilerplate_filter.py` — affiliate patterns Aff1/Aff2 (lines 105–117)
- `tests/test_orangetrack_source.py` (682 LoC, 59 tests, 7 classes — all PASS)
- `tests/test_boilerplate_filter.py` — affiliate additions + `TestAffiliateLengthBound`
- `tests/test_sources_registry.py`, `tests/test_admin_ping.py` — set assertions updated
- `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml` — FILES list (verified byte-for-byte sync at orangetrack_source.py entry)

**Test results:** `pytest tests/test_orangetrack_source.py tests/test_boilerplate_filter.py tests/test_sources_registry.py tests/test_admin_ping.py` → 224 passed.

---

## Verdict: **NEEDS_FIX**

Two findings rise above cosmetic. Neither is a security gap (security audit already PASSED) and neither blocks the cron tick from functioning correctly. But both materially reduce the operator value of the admin-ping aggregator (a primary deliverable of this feature) and one is dead code that lies to readers about its purpose. They are cheap to fix and worth fixing before deploy to prod. Beyond these two, the family fit is excellent — orangetrack matches the autoevolution / lamley / mattel naming conventions, return shape, and error-handling style. Aggregator lifecycle is correctly function-local with try/finally cleanup. All 11 codes from tech-spec Decision 5 are emitted at the documented call sites.

Per the status decision matrix: 0 critical + 2 major findings → **approved_with_suggestions** (verdict equivalent: NEEDS_FIX), but only because the two majors are operator-experience defects, not correctness defects — fix-before-prod is recommended, not fix-before-merge.

---

## Critical (blockers) — none

---

## Major (should-fix before prod)

### M1 — Aggregator `count_total` undercounts when per-code link cap trips

**File:** `/workspaces/debian-2/my-hw/orangetrack_source.py`
**Lines:** 813–822 (`format_summary`), 794–798 (`add` truncated branch)
**Category:** error-handling / observability correctness

**Issue.** When `add(code, link)` is called more than 50 times for the same code with distinct links, links 51+ go into `_truncated_count` instead of `_events`. `format_summary()` then computes `count_total = sum(bucket.values())` from `_events` only — `_truncated_count` is **not** added in. On 60 distinct links of `FEED_HTTP_503`, the bullet line reads `(50×)` and the header reads `50 issues this tick`, but 60 events actually fired.

Reproduced live:
```
total_calls: 60                             # what was add()-ed
bucket size (50 cap): 50
truncated[FEED_HTTP_503]: 10
header: "[test] orangetrack: 50 issues this tick"   # WRONG: should be 60 (per spec)
bullet: "  • FEED_HTTP_503 (50×) — ..."              # WRONG: should be 60×
```

**Impact.** Operator triaging a feed-bug-induced flood gets a misleading event count. The `… 10 more truncated` marker on the link list does cue them that something was clipped, but the `(50×)` count and the header `50 issues` both lie. If the cap trips, the operator's first-glance assessment of severity is materially low.

The tech-spec at Decision 5 says: *"N is the number of distinct (code, link) pairs"*. So the **header** is technically spec-compliant (50 distinct stored pairs). But the **bullet count** `(<count>×)` is intended per AC6 to reflect how many events fired for that code — which should include the truncated overflow. The spec is silent on this edge case; the natural reading and the operational value both argue for including truncated counts in `count_total`.

**Recommendation.** Two-line fix at line 814:
```python
count_total = sum(bucket.values()) + self._truncated_count.get(code, 0)
```
And consider also adjusting the header `distinct` total to include `sum(self._truncated_count.values())` so both numbers tell a consistent story. Add a test similar to existing `test_per_code_link_cap_50` that asserts the count includes the truncated tail.

---

### M2 — Dead imports `ExpatError` / `SAXParseException` in `_fetch_orangetrack_entries`

**File:** `/workspaces/debian-2/my-hw/news_bot.py`
**Lines:** 1400–1409 (function-local imports inside `_fetch_orangetrack_entries`)
**Category:** dead-code / readability

**Issue.** The function imports `ExpatError`, then attempts a triple-fallback import of `SAXParseException` (broken first attempt — `from xml.sax.SAXParseException import SAXParseException` is not a valid module path, that's the class; the fallbacks recover). Neither name is referenced anywhere in the function body; only the comment at line 1427 mentions them: `# ExpatError, SAXParseException, malformed XML, etc.`

Verified:
```
$ grep -n "ExpatError\|SAXParseException" news_bot.py
1401:    from xml.parsers.expat import ExpatError
1403:        from xml.sax.SAXParseException import SAXParseException
1406:            from xml.sax import SAXParseException
1408:            SAXParseException = Exception
1427:                # ExpatError, SAXParseException, malformed XML, etc.
```

The bozo-exception classification at line 1424 only checks `(URLError, socket.timeout, ConnectionError, TimeoutError)`. Anything else (ExpatError, SAXParseException, AttributeError) falls through to the `else: FEED_XML_PARSE` branch — which is correct behaviour, but doesn't depend on the imports.

**Impact.** Eight lines of misleading defensive code. A future reader will assume `ExpatError` / `SAXParseException` are used in the isinstance check and may add a usage that is no-op (because feedparser's bozo_exception is rarely either of those exact classes — it's typically `xml.sax._exceptions.SAXParseException`, which the broken nested import path comment hints at). Worse, the inverted nested-fallback shape `from xml.sax.SAXParseException import SAXParseException` is structurally wrong (it would only succeed if there were a submodule named `SAXParseException` containing a class of the same name, which is not the layout of `xml.sax`) and the `# pragma: no cover` comment hides this from CI.

**Recommendation.** Delete lines 1401, 1403–1408 entirely. Keep `import socket` and `from urllib.error import URLError` (both used). Update the comment at line 1427 to:
```python
# Anything not URLError/socket-timeout/ConnectionError → treat as XML parse.
# (feedparser surfaces SAX/Expat parse errors via bozo_exception too.)
```

---

## Minor (cleanup, optional)

### m1 — Dead parameter `link` in `_parse_content_encoded`

**File:** `/workspaces/debian-2/my-hw/orangetrack_source.py`
**Line:** 340 (signature), 668 + 717 (call sites)
**Category:** dead-code

`_parse_content_encoded(html_str: str, link: str)` declares `link` but never references it in the body. Both call sites pass it (lines 668, 717). Either drop the parameter or use it — current tech-spec Decision 1 says fallback path emits notifier codes from `_fetch_article_html` (which already has access to `link`); `_parse_content_encoded` doesn't need it for any documented purpose.

**Fix:** drop the parameter and the two arguments; or, if intended for future logging context, add `logger.debug(...)` at the entry to use it (less preferred — YAGNI).

---

### m2 — Defensive `hasattr(entry, 'get')` is unnecessary

**File:** `/workspaces/debian-2/my-hw/news_bot.py`
**Lines:** 1436, 1454, 1455, 1456
**Category:** readability

The function has four spots like:
```python
entry.get('link') if hasattr(entry, 'get') else getattr(entry, 'link', None)
```
`feedparser.FeedParserDict` always supports `.get()` (verified — it inherits from dict). The `getattr` fallback never fires. This pattern adds clutter without adding safety. Compare to `_fetch_rss_entries` line 1353 which just calls `entry.get(...)` directly with the same data type.

**Fix:** Replace each two-branch expression with `entry.get(...)`. Saves ~8 LoC and improves readability.

---

### m3 — Docstring at SOURCES registry comment block forgot to update for orangetrack

**File:** `/workspaces/debian-2/my-hw/news_bot.py`
**Line:** 1318
**Category:** documentation drift

Module-level comment block reads:
```
Every returned entry must carry a ``source_name`` string — one of
``'autoevolution'`` / ``'lamley'`` / ``'mattel'`` / ``'other'`` — so the
prep phase can count them per `SOURCE_LABEL` / `SOURCE_EMOJI`.
```
Missing `'orangetrack'` in the enumerated list. Cosmetic but contradicts the runtime registry now that orangetrack is wired in.

**Fix:** Add `'orangetrack'` to the comment list.

---

### m4 — `_runs_from_tag` is a near-duplicate of `autoevolution_source._runs_from_tag`

**Files:**
- `/workspaces/debian-2/my-hw/autoevolution_source.py` lines 41–80
- `/workspaces/debian-2/my-hw/orangetrack_source.py` lines 246–288
**Category:** maintainability / DRY

The two implementations diverge on three lines (orangetrack adds `_safe_href` filtering and the "drop unsafe href, keep plain text" fallback). This duplication is **intentionally accepted** by the tech-spec for the YouTube embed wrapper (Decision 8 — "Five lines duplication is cheaper than introducing a shared module") and is acceptable here too. However, with the orangetrack version being the more secure of the two, an eventual consolidation should prefer the orangetrack flavor.

**Recommendation:** No action this feature. Note the duplication for the next refactor; if a third source-parser ever needs the function, factor out into a shared `parser_helpers.py`.

---

### m5 — `_parse_content_encoded` is 185 lines with six nested closures

**File:** `/workspaces/debian-2/my-hw/orangetrack_source.py` lines 340–525
**Category:** maintainability

Per the universal severity mapping, functions > 100 lines are auto-flagged critical. However, the orangetrack function is structured as **six small helper closures** (`_emit_paragraph` 6 lines, `_emit_heading` 11, `_emit_image` 11, `_emit_iframe` 5, `_walk` ~50, plus the outer driver) sharing the `blocks` and `seen_image_bases` mutable state. Each closure does one thing. Inlining vs extraction tradeoff: extracting would require passing `blocks` and `seen_image_bases` as mutable parameters or wrapping the whole thing in a class, which is heavier than the current shape. Lamley's parser body inside `fetch_lamley_article` is similarly long (~50 lines after the SSRF guard) and mattel's `fetch_mattel_article` is comparable.

I'm intentionally **not** marking this as critical/major because the closures provide genuine cohesion and the whole function is one parse-tree walk. A future refactor could extract the closures into a small `_BlockBuilder` class with `emit_paragraph`/`emit_heading`/etc. methods, but that's a separable change, not a defect.

**Recommendation:** No action this feature. Consider for next feature touching this code.

---

## Cross-component consistency — PASS

### Family fit (autoevolution / lamley / mattel parsers vs orangetrack)

| Convention | autoevolution | lamley | mattel | **orangetrack** |
|---|---|---|---|---|
| Public entry function | `fetch_autoevolution_article(entry)` | `fetch_lamley_article(link, ...)` | `fetch_mattel_article(link, ...)` | `fetch_orangetrack_article(entry, notifier=None)` |
| Returns | `{title, subtitle, paragraphs, images[, blocks]}` | `{title, subtitle, paragraphs, images}` | (canonical) | `{title, subtitle, paragraphs, images, blocks}` |
| SSRF guard | n/a (Cloudflare bypass via curl_cffi) | `_is_allowed_lamley_url` | `ARTICLE_URL_PREFIX.startswith` | `_is_allowed_orangetrack_url` ✓ matches lamley pattern |
| Boilerplate filter | `filter_blocks` + `filter_boilerplate` | `filter_boilerplate` | `filter_boilerplate` | `filter_blocks` + `filter_boilerplate` ✓ |
| Logger name | `logging.getLogger(__name__)` | same | same | same ✓ |
| Hardcoded feed URL | (in feeds.json) | (in feeds.json) | `NEWS_URL` constant | `_FEED_URL` constant ✓ matches mattel |
| HTTP timeout | `REQUEST_TIMEOUT=20` | `REQUEST_TIMEOUT=15` | n/a | `REQUEST_TIMEOUT=15` ✓ matches lamley |
| Image limit | `MAX_IMAGES=10` | `IMAGE_LIMIT=10` | n/a | `IMAGE_LIMIT=10` ✓ |
| Notifier signature | `Callable[[str], None]` (lamley/mattel) | same | same | `Callable[[str, str], None]` — **(code, link)** pair, intentionally different per Decision 5 |

**Notifier signature deviation** is documented in tech-spec Decision 5 (aggregator needs structured codes, not free-text strings). It does NOT propagate up — the aggregator-level `emit(send_fn)` calls `send_fn(text)` which is the standard `Callable[[str], None]` shape, so `send_admin_notification` is plugged in unchanged. Family-correctness preserved at the boundary.

### Naming consistency check

| Surface | Form | Frequency |
|---|---|---|
| Module / source name | `orangetrack` | dominant — used in NETLOC_TO_SOURCE values, SOURCE_EMOJI/LABEL keys, `source_name='orangetrack'`, `_fetch_orangetrack_entries`, `OrangetrackPingAggregator`, function names |
| Hostname / netloc | `orangetrackdiecast.com` | used in NETLOC_TO_SOURCE keys, `_ALLOWED_HOSTS`, `_FEED_URL`, `fetch_full_article` substring check |
| Display / docstring | `Orange Track Diecast` | only in module docstring (line 2) and class docstring |
| Underscore / hyphen | `orange_track`, `orange-track` | **not present anywhere** ✓ |

Naming is internally consistent. No `orange_track` or `orange-track` typos.

---

## Aggregator boundaries — PASS

`OrangetrackPingAggregator` is correctly scoped:
- Created **function-local** at `news_bot.py:1411` inside `_fetch_orangetrack_entries`.
- Lifetime is the stack frame of one cron-tick fetcher call.
- `try/finally` at lines 1416–1472 guarantees `emit()` runs (when non-empty) even if any of `feedparser.parse`, the entry loop, or `fetch_orangetrack_article` raises.
- No module-level singleton, no global state — verified by `grep -n "_aggregator" orangetrack_source.py news_bot.py` (zero hits at module scope).
- Re-entry across cron ticks is safe because each tick recreates the instance.

Edge case: if `feedparser.parse(feed_url)` itself raises BEFORE any `aggregator.add` call, the aggregator stays empty, `is_empty()` is True, `emit()` is skipped — and the exception propagates up to `news_bot.job()` step (b1) which catches it at line 1582–1593 and emits a `⚠️ Source ... failed: ...` admin message. So the operator still sees the failure; just via the legacy ping-per-failure path, not the new aggregator path. Acceptable degradation.

---

## Error-handling completeness (Decision 5 codes) — PASS

All 11 codes documented in tech-spec Decision 5 are emitted somewhere:

| Code | Emitted at |
|---|---|
| `FEED_HTTP_<status>` | `news_bot.py:1420` |
| `FEED_TIMEOUT` | `news_bot.py:1425` |
| `FEED_XML_PARSE` | `news_bot.py:1428` |
| `ENTRY_HOST_REJECTED` | `news_bot.py:1438` |
| `ART_FALLBACK_HTTP_<status>` | `orangetrack_source.py:584` |
| `ART_FALLBACK_TIMEOUT` | `orangetrack_source.py:562, 566, 602, 608` |
| `ART_FALLBACK_HOST_REJECTED` | `orangetrack_source.py:707` |
| `ART_FALLBACK_REDIRECT_<status>` | `orangetrack_source.py:576` |
| `ART_FALLBACK_TOO_LARGE` | `orangetrack_source.py:595` |
| `ART_FALLBACK_PARSE_EMPTY` | `orangetrack_source.py:732` |
| `ART_PARSE_EXCEPTION` | `orangetrack_source.py:675, 724`, `news_bot.py:1448` |

---

## Dead code summary

| Item | File:line | Severity |
|---|---|---|
| `ExpatError` import never used | news_bot.py:1401 | **Major (M2)** |
| `SAXParseException` triple-fallback import never used | news_bot.py:1403–1408 | **Major (M2)** |
| `link` parameter of `_parse_content_encoded` unused | orangetrack_source.py:340 | Minor (m1) |
| `hasattr(entry, 'get')` defensive checks (4 sites) | news_bot.py:1436, 1454–1456 | Minor (m2) |
| `'orangetrack'` missing from SOURCES doc comment | news_bot.py:1318 | Minor (m3) |

---

## Test results (verification)

```
$ pytest tests/test_orangetrack_source.py tests/test_boilerplate_filter.py \
        tests/test_sources_registry.py tests/test_admin_ping.py -q
...........................................................                [100%]
224 passed in 1.54s
```

---

## Final Verdict: **NEEDS_FIX**

Recommend two pre-prod fixes (M1 + M2) plus optional cleanups m1–m3. Total estimated edit cost: ~15 lines changed, ~1 new test for M1. After M1+M2, the feature is approved-with-suggestions and ready for prod merge.
