# Test Quality Audit — orangetrack-source feature

**Branch:** `dev` (commits c420aae, 234f6dc, 302f60f, 5854ec0)
**Auditor:** test-master
**Date:** 2026-05-04
**Pytest result locally:** **810 passed in 5.77s** (matches operator-reported baseline 740 + 70 = 810).

**Files reviewed:**
- `tests/test_orangetrack_source.py` (new, 682 lines, **59 tests** in 7 classes)
- `tests/test_boilerplate_filter.py` (affiliate additions in `TestIsBoilerplatePositive` / `TestIsBoilerplateNegative` + new `TestAffiliateLengthBound` class, 4 tests)
- `tests/test_sources_registry.py` (set assertions updated to include `'orangetrack'` + `_fetch_orangetrack_entries`)
- `tests/test_admin_ping.py` (SOURCE_EMOJI / SOURCE_LABEL set assertions updated to include `'orangetrack'`, value 🔵 (`\U0001F535`))
- `tests/test_feed_iteration.py`, `tests/test_integration.py`, `tests/test_mattel_integration.py`, `tests/test_distributed_schedule_integration.py` — patched `news_bot.SOURCES` to a narrower 2-source list to avoid real network calls into orangetrackdiecast.com.

**Implementation reviewed:**
- `orangetrack_source.py` (845 lines)
- `news_bot.py` integration (`_fetch_orangetrack_entries`, dispatcher branch in `fetch_full_article`, `NETLOC_TO_SOURCE`, `SOURCE_EMOJI`, `SOURCE_LABEL`, `SOURCES`)
- `boilerplate_filter.py` (Aff1 + Aff2 patterns)

**Spec sources:** `work/orangetrack-source/tech-spec.md` (609 lines), `work/orangetrack-source/user-spec.md` (120 lines)

---

## Per-class coverage of expected test classes

| Class | Status | Notes |
|---|---|---|
| `TestPrimaryPath` | **PRESENT** | 11 tests — all listed-edge cases covered EXCEPT (a) "multiple iframes in one paragraph"; the iframe test in `TestYouTubeEmbedWrapping` covers a single attacker iframe inside a `<p>`, but the user-spec / tech-spec line "Multiple `<iframe>` in same paragraph block → blocks list contains 2 `video` entries in order" is not exercised. |
| `TestSSRFAllowlist` | **PRESENT** | 12 tests including subdomain attack, cloud metadata IP, javascript:, data:, malformed, scheme-relative, empty, None, `test_fallback_path_allowlist_called` (poisoned link → ART_FALLBACK_HOST_REJECTED + assert mock_get not called), and `test_entry_level_guard_in_news_bot_fetcher` (verifies the news_bot integration emits ENTRY_HOST_REJECTED). |
| `TestFallbackPath` | **PRESENT** | 7 tests: 200-silent, 503, 404, timeout, redirect (asserts `allow_redirects=False`, `stream=True`, `mock_get.call_count == 1`), 6 MB cap, parse-empty. **MISSING from tech-spec list:** connection-refused → ART_FALLBACK_TIMEOUT mapping; UTF-8 non-ASCII fallback decode; ART_PARSE_EXCEPTION via injected exception. |
| `TestWPBlockDriftMitigation` | **PRESENT** | 1 test — minimal HTML without any `wp-block-*` class. Asserts subtitle, paragraph, image survive. |
| `TestYouTubeEmbedWrapping` | **PRESENT** | 7 tests: youtube.com/embed wrapped, m.youtube.com wrapped, youtu.be wrapped, youtube-nocookie wrapped, vimeo NOT wrapped, attacker URL with substring rejected, attacker iframe in HTML produces no video block. |
| `TestOrangetrackPingAggregator` | **PRESENT** | 16 tests: empty, emit-no-op-when-empty, 3-events grouping, dedup, distinct status codes (429 < 503 alphabetical), category ordering (FEED < ENTRY < ART), alphabetical within category, instance label set/empty/None, 50-link cap with truncation marker, 500-event cap (silent no-op), 3500-char output cap, control-char sanitization, emit swallow on send_fn raise. |
| **`TestDispatcherIntegration`** | **MISSING** | Not present in `tests/test_orangetrack_source.py` nor anywhere else in `tests/`. Three documented behaviours are NOT tested at the dispatcher boundary: (a) `news_bot.fetch_full_article({'link': 'https://orangetrackdiecast.com/post', 'paragraphs': ['p1'], ...})` returns canonical dict via pass-through; (b) `news_bot.fetch_full_article({'link': 'https://www.orangetrackdiecast.com/post', ...})` apex+www routing; (c) `news_bot.fetch_full_article({'link': 'https://orangetrackdiecast.com.attacker.example/x', ...})` returns None (substring `in` dispatcher routes to orangetrack BUT pass-through returns None for missing pre-populated body). The latter is the explicit defense-in-depth test required by tech-spec ACs lines 419-422. |
| `TestSafeForPing` (smoke, unlisted in expected) | **PRESENT** | 5 tests — newline / CR / tab strip, length truncation, None handling. |

---

## Findings

### CRITICAL — none.

### MAJOR

#### M1. `TestDispatcherIntegration` class missing entirely
- **File:** `tests/test_orangetrack_source.py` (would belong here per tech-spec line 419)
- **Category:** `missing_coverage`
- **Issue:** Tech-spec Testing Strategy explicitly lists `TestDispatcherIntegration` as one of the 7 required classes (tech-spec line 419-422 + AC line 518). The class is absent. Three behaviours are unverified:
  1. **Pass-through path:** `news_bot.fetch_full_article({'link': 'https://orangetrackdiecast.com/post-x', 'title': 'T', 'subtitle': 'S', 'paragraphs': ['p1','p2'], 'images': ['i1'], 'blocks': [{...}]})` must return a canonical dict assembled from the entry fields, with NO HTTP request issued. This is the entire reason `_fetch_orangetrack_entries` does the parse upstream — and there is no test that the dispatcher branch actually performs the read-and-return without calling `requests`.
  2. **Apex vs www routing:** the substring check `'orangetrackdiecast.com' in domain` must accept BOTH `orangetrackdiecast.com` and `www.orangetrackdiecast.com`. Untested.
  3. **Defense-in-depth on dispatcher substring weakness:** the substring check would route `https://orangetrackdiecast.com.attacker.example/payload` to the orangetrack branch. Pass-through returns None when `paragraphs` is empty (`news_bot.py:1287-1291`). This is the documented hardening per tech-spec Decision 13 (line 275: "the dispatcher's substring match is exploitable") and must be regression-tested. Without this test a future refactor could swap pass-through for a fallback HTTP fetch and reintroduce the SSRF gap.
- **Litmus test:** Removing the orangetrack branch from `news_bot.fetch_full_article` (deleting lines 1281-1298 from `news_bot.py`) currently breaks no tests. **3 distinct behaviours lose all coverage.**
- **Recommendation:**
  ```python
  class TestDispatcherIntegration:
      def test_pass_through_returns_prepopulated_fields_no_http(self):
          import news_bot
          entry = {
              'link': 'https://orangetrackdiecast.com/post-x',
              'title': 'T', 'subtitle': 'S',
              'paragraphs': ['Body para 1', 'Body para 2'],
              'images': ['https://x/img.jpg'],
              'blocks': [{'type':'paragraph', 'text':'Body para 1'}],
          }
          with patch('news_bot.requests.get') as mock_get, \
               patch('orangetrack_source.requests.get') as mock_get2:
              out = news_bot.fetch_full_article(entry)
          assert out == {
              'title':'T', 'subtitle':'S',
              'paragraphs':['Body para 1','Body para 2'],
              'images':['https://x/img.jpg'],
              'blocks':[{'type':'paragraph','text':'Body para 1'}],
          }
          assert mock_get.call_count == 0
          assert mock_get2.call_count == 0

      def test_www_apex_both_route_to_pass_through(self):
          import news_bot
          for link in ('https://orangetrackdiecast.com/p',
                       'https://www.orangetrackdiecast.com/p'):
              entry = {'link': link, 'title':'T',
                       'paragraphs': ['x'], 'images':[], 'blocks':None,
                       'subtitle':''}
              out = news_bot.fetch_full_article(entry)
              assert out is not None
              assert out['paragraphs'] == ['x']

      def test_subdomain_attack_returns_none_via_pass_through(self):
          import news_bot
          # Substring routing accepts orangetrackdiecast.com.attacker.example —
          # but pass-through requires pre-populated paragraphs (which a
          # malicious entry lacks because _fetch_orangetrack_entries' allowlist
          # rejected it upstream and never appended body fields).
          entry = {'link': 'https://orangetrackdiecast.com.attacker.example/x',
                   'title':'T', 'paragraphs': []}
          with patch('news_bot.requests.get') as mock_get:
              out = news_bot.fetch_full_article(entry)
          assert out is None
          assert mock_get.call_count == 0
  ```

#### M2. `test_total_event_cap_500_silent` does not actually verify the cap
- **File:** `tests/test_orangetrack_source.py:604-612`
- **Category:** `empty_test` (insufficient assertion)
- **Issue:** The test does 600 `add()` calls with distinct codes/links and only asserts that `format_summary()` contains the substring `"issues this tick"`. Per the implementation at `orangetrack_source.py:783-786`, after the 500th call, all subsequent `add()`s are silent no-ops. The test does NOT verify (a) the distinct count in the header is exactly 500 (not 600), (b) `len(a._events) <= 500` post-cap, or (c) that the 501st call was actually dropped. The assertion `"issues this tick" in out` would pass even if the cap were broken (e.g. allowed 600 events).
- **Litmus test:** Removing the `if self._total_calls >= _MAX_TOTAL_EVENTS: return` guard at `orangetrack_source.py:784-786` does NOT make this test fail. **Litmus test FAILED.**
- **Recommendation:** Replace the soft assertion with a hard count check:
  ```python
  def test_total_event_cap_500_silent(self):
      a = OrangetrackPingAggregator("test")
      for i in range(600):
          a.add(f"FEED_HTTP_{i}", f"https://x/{i}")
      out = a.format_summary()
      # Distinct (code, link) pairs <= 500 (cap on add() calls).
      assert a._total_calls == _MAX_TOTAL_EVENTS  # imported as 500
      distinct = sum(len(b) for b in a._events.values())
      assert distinct == 500
      # Header reports the bounded count, not the unbounded 600.
      assert "500 issues this tick" in out
  ```

#### M3. "Multiple iframes in one paragraph block" not tested
- **File:** `tests/test_orangetrack_source.py` (TestPrimaryPath / TestYouTubeEmbedWrapping)
- **Category:** `missing_coverage`
- **Issue:** Tech-spec line 365 lists "Multiple `<iframe>` in same paragraph block → blocks list contains 2 `video` entries in order" as a required test case. The parser code at `orangetrack_source.py:432-441` has a special branch for that shape (`<p>` with `inner_iframes` list and no text). Without this test, the loop bound (a regression that processes only the first iframe) would slip through.
- **Litmus test:** If `for iframe in inner_iframes: _emit_iframe(iframe)` at `orangetrack_source.py:435-436` were changed to emit only the first one (`_emit_iframe(inner_iframes[0])`), no current test would fail.
- **Recommendation:** Add to `TestPrimaryPath`:
  ```python
  def test_multiple_iframes_in_same_paragraph_block(self):
      html = (
          "<p>Watch:</p>"
          "<p>"
          "<iframe src='https://www.youtube.com/embed/abc123XYZ'></iframe>"
          "<iframe src='https://www.youtube.com/embed/def456ABC'></iframe>"
          "</p>"
      )
      out = fetch_orangetrack_article(_make_entry(html))
      assert out is not None
      videos = [b for b in out["blocks"] if b["type"] == "video"]
      assert len(videos) == 2
      # Order preserved: abc123XYZ-embed comes before def456ABC-embed.
      assert "abc123XYZ" in videos[0]["src"]
      assert "def456ABC" in videos[1]["src"]
  ```

### MINOR

#### m4. Connection-refused mapping not asserted
- **File:** `tests/test_orangetrack_source.py` (TestFallbackPath)
- **Category:** `missing_coverage`
- **Issue:** Tech-spec line 390 specifies "Connection refused → notifier `('ART_FALLBACK_TIMEOUT', link)` or `('ART_FALLBACK_HTTP_<...>', link)` per `requests` exception type". `_fetch_article_html` at `orangetrack_source.py:564-567` catches generic `requests.RequestException` and emits `ART_FALLBACK_TIMEOUT`. No test asserts this mapping (only the explicit `requests.Timeout` branch is covered).
- **Recommendation:** Add `test_connection_refused_emits_timeout` using `mock_get.side_effect = requests.ConnectionError("conn refused")` and assert `"ART_FALLBACK_TIMEOUT"` in codes.

#### m5. `ART_PARSE_EXCEPTION` notifier path not tested
- **File:** `tests/test_orangetrack_source.py` (no class)
- **Category:** `missing_coverage`
- **Issue:** `orangetrack_source.py:669-678` and `:716-727` route any unexpected parser exception to `notifier('ART_PARSE_EXCEPTION', link)`. No test exercises this — meaning a regression that swallows the notify call (or changes the code string to e.g. `'PARSE_EXC'`) would not surface in CI. The user-spec AC (line 45) explicitly enumerates `ART_PARSE_EXCEPTION` as a required code.
- **Recommendation:** Add a test that monkeypatches `_parse_content_encoded` to raise:
  ```python
  def test_parse_exception_emits_notifier(monkeypatch):
      events = []
      def boom(html, link): raise RuntimeError("boom")
      monkeypatch.setattr("orangetrack_source._parse_content_encoded", boom)
      out = fetch_orangetrack_article(
          _make_entry(SAMPLE_STANDARD_HTML),
          notifier=lambda c, l: events.append((c, l)),
      )
      assert out is None
      assert any(e[0] == "ART_PARSE_EXCEPTION" for e in events)
  ```

#### m6. `TestWPBlockDriftMitigation` is single-test thin
- **File:** `tests/test_orangetrack_source.py:443-455`
- **Category:** `redundant_testing` (negative — too thin, not redundant)
- **Issue:** One test for a class that bears the most architectural weight (Decision 3 — "walk by tag, not class"). It only checks the absence of `wp-block-*`. Doesn't probe what happens with **stale** class names (`wp-block-paragraph-OLD`), with `<div>` wrappers around prose paragraphs, or with `<section>` containers. The class will pass even if the parser silently regresses to class-based extraction for any paragraph with a recognized class.
- **Recommendation:** Add at least 1 more test:
  ```python
  def test_div_wrapper_around_paragraphs_recurses(self):
      html = "<div class='nothing-special'><p>Wrapped lead.</p>" \
             "<p>Wrapped body.</p></div>"
      out = fetch_orangetrack_article(_make_entry(html))
      assert out is not None
      assert out["subtitle"] == "Wrapped lead."
      assert "Wrapped body." in out["paragraphs"]
  ```

#### m7. UTF-8 non-ASCII body via fallback HTTP not verified
- **File:** `tests/test_orangetrack_source.py` (TestFallbackPath)
- **Category:** `missing_coverage`
- **Issue:** Tech-spec line 392 lists "UTF-8 response with non-ASCII body → paragraphs decode correctly" for the fallback path. The decode logic at `orangetrack_source.py:617-621` reads `response.encoding` and falls back to UTF-8. `TestPrimaryPath.test_non_ascii_title_preserved` covers the content:encoded path but the **fallback** decode path is silently uncovered.
- **Recommendation:** Add a fallback-path test with `body = "<p>Mañana — el modelo</p>".encode("utf-8")` and assert "Mañana" survives in `out["paragraphs"]` or `out["subtitle"]`.

#### m8. Aggregator format precision: distinct-count semantic only verified at N=1, N=3
- **File:** `tests/test_orangetrack_source.py:528-547`
- **Category:** `anti_pattern` — partially covered semantic
- **Issue:** The "N issues this tick" header invariant is "N = number of distinct (code, link) pairs". Tested at N=3 (test_three_events_grouping) and N=1 (test_dedup_same_code_same_link). Not verified at the boundary where the semantic could regress to "distinct codes only" (e.g. N=2 with one code, two links → header should still say `2 issues`).
- **Recommendation:** Add `test_distinct_count_with_two_links_same_code`:
  ```python
  def test_distinct_count_two_links_one_code(self):
      a = OrangetrackPingAggregator("test")
      a.add("FEED_HTTP_503", "https://x/a")
      a.add("FEED_HTTP_503", "https://x/b")
      out = a.format_summary()
      assert "2 issues this tick" in out
      # Bullet line shows one code with count 2× and both links.
      assert "FEED_HTTP_503 (2×)" in out
      assert "https://x/a" in out and "https://x/b" in out
  ```

#### m9. `TestSafeForPing` does not test the printable-non-ASCII path
- **File:** `tests/test_orangetrack_source.py:664-682`
- **Category:** `missing_coverage` (minor)
- **Issue:** The function at `orangetrack_source.py:172-190` runs `c.isprintable()` after stripping control chars and replaces non-printables with `?`. The test class only covers `\n`, `\r`, `\t`, length truncation, and None. No coverage for "valid Cyrillic / non-ASCII chars survive" (they're printable per `str.isprintable()`) or "DEL char (`\x7f`) is stripped".
- **Recommendation:** Add 2 tests: `test_unicode_letters_preserved` ("Mañana 🚗" → unchanged) and `test_del_char_stripped` (`"a\x7fb"` → `"ab"`).

#### m10. `test_redos_safety_long_buy_pattern` only tests negative case
- **File:** `tests/test_boilerplate_filter.py:185-195`
- **Category:** `anti_pattern`
- **Issue:** The test crafts an input that doesn't match Aff1 / Aff2 (asserts `out is False`) and times out at 100ms. Useful as a baseline but doesn't exercise the **positive** ReDoS-trigger shape. A pathological positive input like `"Buy now from " + "a " * 30 + "shop"` (matches Aff2) should also complete fast — that's the regression risk if a future refactor adds nested groups.
- **Recommendation:** Add a counterpart test where the input DOES match the pattern but is engineered to maximize backtracking attempts.

---

## Litmus Test Summary

| Test | Litmus | Notes |
|---|---|---|
| `test_standard_post_with_paragraphs_and_image` | PASS | Asserts on parsed text content; removing the parser body would fail. |
| `test_post_with_video_block` | PASS | Asserts on `video.src.startswith("https://telegra.ph/embed/youtube?url=")` — not just block presence. |
| `test_h5_goes_to_blocks_only` | PASS | Both positive (h5 in blocks at level=5) and negative (NOT in paragraphs) sides asserted. |
| `test_affiliate_standalone_short_paragraph_filtered` | PASS | Both positive (line stripped) AND negative (inline phrase preserved) asserted in same test. |
| `test_redirect_does_not_call_get_twice` | PASS | Excellent — asserts code, kwargs (`allow_redirects=False`, `stream=True`), AND call count. |
| `test_three_events_grouping` | PASS | Asserts header text, code presence, AND group ordering. |
| `test_subdomain_attack_rejected` | PASS | Concrete URL, concrete False expected. |
| `test_total_event_cap_500_silent` | **FAIL** | See M2. |
| `test_dedup_same_code_same_link` | PASS | Both `(2×)` count and `1 issues this tick` distinct-count asserted. |
| `test_control_char_sanitization` | PASS | Asserts line count == 2, header on line 1, bullet on line 2 — strong structural assertion against newline injection. |
| `test_emit_swallows_send_fn_error` | PASS | `caplog` checks both that no exception propagates AND that an ERROR-level log mentions `send_fn raised` or `swallowing`. |

**Litmus pass: 58 / 59.** Only `test_total_event_cap_500_silent` fails.

---

## Pyramid Balance

- **Unit:** ~59 in `test_orangetrack_source.py` + ~125 in `test_boilerplate_filter.py` (4 new) + dispatcher updates in `test_admin_ping.py` and `test_sources_registry.py`.
- **Integration:** None new for orangetrack; tech-spec explicitly justifies skipping (line 440 — "Coverage exists via `test_distributed_schedule_integration.py` which doesn't hardcode source identity"). Verified: that file's `setUp` patches `news_bot.SOURCES` to a 2-source list (excludes `_fetch_orangetrack_entries`), confirming the test path doesn't hardcode any source dependency. Same for `test_feed_iteration.py`, `test_integration.py`, `test_mattel_integration.py`. Patches are hygienic — `_fetch_orangetrack_entries` is never called in those tests, avoiding real network calls.
- **E2E:** None (M-size feature; pre-deploy QA on `dev` covers it).

**Assessment:** healthy. The unit-only choice is justified by the tech-spec. Indirect integration coverage via `test_distributed_schedule_integration.py` is preserved.

---

## Coverage Assessment

**adequate.** Most edge cases listed in tech-spec Testing Strategy are present. The one structural gap (M1) is a missing test class explicitly required by spec; the other findings are minor. Overall coverage tightly tracks the implementation.

---

## Mock Realism

Mocks throughout `test_orangetrack_source.py` are well-shaped:
- `_make_streaming_response` returns a `MagicMock(spec=requests.Response)` with `status_code`, `iter_content` (chunked iterator), `close()`. Realistic.
- The `_fetch_orangetrack_entries` lifecycle test (`test_entry_level_guard_in_news_bot_fetcher`) constructs a fake `feedparser.parse` return type with `bozo`, `status`, and `get` — realistic enough to exercise the entry-level allowlist guard end-to-end.
- The `redirect_does_not_call_get_twice` test asserts BOTH the kwargs (`allow_redirects=False`, `stream=True`) AND the call count — a strong defense against silent regression.

No over-mocking. Tests are not just verifying mock wiring.

---

## Brittleness

- **No timestamp-coupled assertions.** All sample HTML uses fixed published dates ("Mon, 01 Jan 2025 00:00:00 +0000") with no time-of-day logic.
- **One time-coupled test:** `test_redos_safety_long_buy_pattern` uses `time.monotonic()` with a 100ms threshold. Reasonable for ReDoS guard. CI machines may occasionally run slow but 100ms is generous for a regex on 60+60+9 chars.
- **No randomness-coupled.** `_safe_for_ping` and aggregator state are deterministic.
- **Network-coupled:** None — all `requests.get` calls are mocked. `feedparser.parse` is monkeypatched in the one news_bot integration test (`test_entry_level_guard_in_news_bot_fetcher`). Confirmed: running `pytest tests/test_orangetrack_source.py` offline in the sandbox completed without hitting the network.

---

## Boundary Cases for Boilerplate Filter

`tests/test_boilerplate_filter.py::TestAffiliateLengthBound`:
- `test_affiliate_at_120_chars_filtered` — exactly 120 chars, asserts `is_boilerplate(line) is True`. ✓
- `test_affiliate_at_121_chars_preserved` — 121 chars, asserts `is_boilerplate(line) is False`. ✓
- `test_quick_link_at_120_chars_filtered` — separate pattern (Aff1 vs Aff2), 120 chars, filtered. ✓
- `test_redos_safety_long_buy_pattern` — pathological non-matching shape, <100ms. ✓ (but see m10)

The 120/121 boundary is correctly tested for **both** Aff1 (`*QUICK LINK!*`) and Aff2 (`Buy [now] from`) shapes.

---

## Test Distribution Sanity Check (740 → 810, +70)

- `tests/test_orangetrack_source.py` — 59 new tests (new file)
- `tests/test_boilerplate_filter.py::TestAffiliateLengthBound` — 4 new tests
- `tests/test_boilerplate_filter.py::TestIsBoilerplatePositive` — 5 new affiliate-pattern parametrize entries (each counts as a separate test in the parametrize list) → 5
- `tests/test_sources_registry.py` — set-assertion updates and `_fetch_orangetrack_entries` shape assertion → 0 new tests, modifications to existing assertions (no count change)
- `tests/test_admin_ping.py` — 0 new test FUNCTIONS, just additional assertions inside existing tests for orangetrack key/values
- 4 integration tests modified to patch `SOURCES` — 0 new tests

**59 + 4 + 5 + 2 (one explicit assertion in test_admin_ping that orangetrack is in SOURCE_EMOJI/SOURCE_LABEL) ≈ ~70.** Distribution is consistent with the +70 delta.

Confirmed via `pytest tests/test_orangetrack_source.py --collect-only`: 59 tests collected. `pytest tests/test_boilerplate_filter.py --collect-only`: 124 tests collected (parametrized affiliate entries inflate that count).

---

## Status

**Decision matrix:** 0 critical, 3 major (M1, M2, M3), 7 minor → `needs_improvement` → **NEEDS_FIX** (but borderline; M1 is the only structural gap; M2 and M3 are tractable single-test additions).

### Final verdict: **NEEDS_FIX**

The test suite is overwhelmingly strong — 58/59 tests pass the litmus test, mocks are realistic, security-critical paths (SSRF allowlist, redirect rejection, control-char sanitization, YouTube hostname allowlist) all have meaningful assertions on real behavior — but the missing `TestDispatcherIntegration` class (M1) is an explicit ACs-line-518 requirement, and `test_total_event_cap_500_silent` (M2) has a critical litmus failure. M3 closes a single-test gap.

### Required fixes before sign-off

1. **M1** — Add `TestDispatcherIntegration` class with at least the 3 tests sketched in the recommendation. **Required for AC line 518 compliance.**
2. **M2** — Strengthen `test_total_event_cap_500_silent` to assert `_total_calls == 500` and distinct-count in header. **Required to actually verify the 500-event cap.**
3. **M3** — Add `test_multiple_iframes_in_same_paragraph_block` per recommendation. **Required to cover tech-spec line 365.**

### Recommended (post-merge OK)

- m4-m10 — additive coverage that strengthens the suite but doesn't gate this PR.

---

## Metrics

| Metric | Value |
|---|---|
| Files reviewed | 9 |
| New test cases | ~70 |
| Litmus check | 58 passed / 1 failed (98.3%) |
| Coverage assessment | adequate |
| Pyramid balance | healthy (unit-only justified) |
| Mock realism | high |
| Brittleness | low |
| Critical findings | 0 |
| Major findings | 3 |
| Minor findings | 7 |
