# Security Audit — orangetrack-source feature

**Branch:** `dev` (commits c420aae, 234f6dc, 302f60f, 5854ec0)
**Files audited:**
- `orangetrack_source.py` (~846 LoC; spec said ~620, but final is 846 — all security-relevant code present)
- `news_bot.py` — `_fetch_orangetrack_entries` (lines 1383–1473), `fetch_full_article` orangetrack branch (lines 1280–1298)
- `boilerplate_filter.py` — affiliate patterns Aff1/Aff2 (lines 105–117)
- `tests/test_orangetrack_source.py` — 59 tests, all PASS (verified live: `pytest -v` exit 0, 0.77s)

**Verdict:** **PASS**

All nine numbered checks pass. Tests are realistic — they exercise the actual call sites and assert observable side-effects (no `assert True` placeholders, no over-mocking that bypasses the validator). Three minor / informational findings recorded for completeness; none block deploy.

---

## Per-check matrix

| # | Check | Status | Evidence |
|---|-------|--------|----------|
| 1 | SSRF allowlist at TWO call sites | **PASS** | `news_bot.py:1437` (entry-level) + `orangetrack_source.py:704` (fallback-level). Both call `_is_allowed_orangetrack_url`. Live probes: subdomain attack rejected, cloud metadata rejected, `javascript:` / `data:` / `//evil/x` rejected, malformed rejected, userinfo trick (`https://orangetrackdiecast.com@attacker.example/x`) rejected. Tests `test_subdomain_attack_rejected`, `test_cloud_metadata_ip_rejected`, `test_entry_level_guard_in_news_bot_fetcher`, `test_fallback_path_allowlist_called` all PASS. |
| 2 | `allow_redirects=False` + no second GET on 3xx | **PASS** | `orangetrack_source.py:559` (`allow_redirects=False`). 3xx branch at line 571–577 emits `ART_FALLBACK_REDIRECT_<status>` and returns None; only `requests.get` was issued once. Test `test_redirect_does_not_call_get_twice` asserts `mock_get.call_count == 1` AND `kwargs['allow_redirects'] is False` AND `kwargs['stream'] is True`. |
| 3 | Bounded streaming, 5 MB cap, lying-server safe | **PASS** | `stream=True` (line 558), `iter_content(chunk_size=8192)` (line 590), `if len(buf) > MAX_RESPONSE_SIZE: _ping('ART_FALLBACK_TOO_LARGE')` (line 594–595). Test `test_body_too_large_cuts_off` injects 6 MB across 1 KB chunks via mocked `iter_content` (Content-Length not set, so the test is a true lying-server simulation) and asserts `ART_FALLBACK_TOO_LARGE` emitted, returning None. The 5 MB cap is enforced on actual byte accumulation, not Content-Length, so a server lying about its size cannot bypass it. |
| 4 | href / src scheme filter | **PASS** | `_safe_href` (line 198) drops `javascript:`, `data:`, `file:`, scheme-relative `//evil/x`, relative paths. Anchor with dropped href degenerates to plain text per `_runs_from_tag` line 274–276. `_safe_img_src` (line 223) accepts only `http`/`https` absolute URLs, drops `data:`, `file:`, scheme-relative, relative. Tests `test_unsafe_href_dropped_keeps_anchor_text` and `test_unsafe_image_src_dropped` exercise the full `SAMPLE_BAD_SCHEMES_HTML` covering `javascript:`, `//evil.example/x`, `data:image/svg+xml`, `file:///etc/passwd` — all dropped, only `safe.jpg` survives, anchor text remains inline. |
| 5 | YouTube hostname allowlist | **PASS** | `_video_embed_url` (line 128) checks hostname BEFORE the regex (line 148–150). Allowlist contains all 7 spec'd hosts (line 51–59). Live probes: `vimeo.com` → None, `attacker.example/?u=youtube.com/embed/abc` → None, real `youtube.com` / `youtu.be` → wrapped. Tests `test_vimeo_not_wrapped`, `test_attacker_url_with_youtube_substring_rejected`, `test_iframe_in_html_routes_through_allowlist` (asserts no video block from attacker iframe inside content:encoded). |
| 6 | Affiliate regex ReDoS-safety | **PASS** | Both Aff1 (`^\s*\*?quick\s+link[!:].*\b(buy\|order\|grab\|shop)\b`) and Aff2 (`^buy\s+(now\s+)?from\s+\S`) anchored at `^`, no nested greedy quantifiers (no `(.+)+` shape), bounded by `_MAX_BOILERPLATE_LEN=120`. Pathological probes (10 000 iterations each on 120-char crafted inputs) measured 0.002–0.005 ms per call — far below the 100 ms target. |
| 7 | Aggregator size bounds | **PASS** | `_MAX_LINKS_PER_CODE=50` (line 87), `_MAX_TOTAL_EVENTS=500` (line 88), `_MAX_SUMMARY_CHARS=3500` (line 89). `add()` checks total cap first (line 784, silent return), then per-bucket cap (line 794, increments `_truncated_count` for "… N more truncated" marker). `format_summary()` truncates with `[truncated]` marker (line 824–828). Tests `test_per_code_link_cap_50`, `test_total_event_cap_500_silent`, `test_summary_truncated_at_3500_chars` cover all three bounds; `test_total_event_cap_500_silent` calls `add()` 600 times and verifies no raise. |
| 8 | `_safe_for_ping` strip-before-truncate ordering | **PASS** | Lines 184 → 188: `_CONTROL_CHAR_RE.sub("", s)` runs first, THEN `if len(s) > _SAFE_FOR_PING_MAX: s = s[:199] + "…"`. Verified empirically with the smuggling probe: a 200-char input ending in `\x01` strips to 199 chars (no truncation needed, no control byte survives). The reverse ordering would produce 199 + `…` and the control byte would either survive or be replaced with `?`; neither happens. Test `test_control_char_sanitization` asserts that newline-injection in a link does NOT produce a fake summary header line — the bullet stays single-line. |
| 9 | `emit()` swallows `send_fn` exception | **PASS** | Lines 840–845: `try: send_fn(text) except Exception: logger.exception(...)`. Test `test_emit_swallows_send_fn_error` raises `RuntimeError` from `send_fn` and asserts the error is logged at ERROR level and does NOT propagate. |

---

## Test-realism audit

Verified each security test does not pass for the wrong reason:

- `test_redirect_does_not_call_get_twice` — asserts BOTH `call_count == 1` AND the `allow_redirects=False` kwarg shape. If a future refactor accidentally enabled redirects, the kwarg assertion would catch it even if the mock somehow only counted one call.
- `test_body_too_large_cuts_off` — injects 6 144 chunks of 1 KB; `iter_content` is replaced wholesale, so the assertion that `ART_FALLBACK_TOO_LARGE` fires reflects the real byte-counting loop in `_fetch_article_html`, not Content-Length inspection.
- `test_entry_level_guard_in_news_bot_fetcher` — runs the actual `news_bot._fetch_orangetrack_entries` with a monkey-patched `feedparser.parse`, captures admin-ping output via patched `send_admin_notification`, and verifies `ENTRY_HOST_REJECTED` reaches the aggregator. Coverage hits the integration boundary, not just the helper in isolation.
- `test_attacker_url_with_youtube_substring_rejected` and `test_iframe_in_html_routes_through_allowlist` — the second test runs the full DOM walker, so it would catch a regression where the iframe handler skips the allowlist gate.
- `test_control_char_sanitization` — asserts `out.count("\n") == 1` AND structural line shape. A weakened sanitization that left the control byte intact would produce 2 newlines and three lines.
- ReDoS regression coverage is missing as a pytest assertion (the regex is structurally safe and verified live; not a finding — see Minor #1).

All 59 tests **PASS** in 0.77 s.

---

## Findings

### Critical

None.

### Major

None.

### Minor

#### Minor 1 — No automated regression test for affiliate-regex ReDoS
**Category:** A04 Insecure Design / best-practice
**Location:** `tests/test_orangetrack_source.py` (no test class for `boilerplate_filter`)
**Description:** Decision 12 promises ReDoS-safe affiliate patterns. The regex shape is structurally safe (anchored, no nested greedy quantifiers, length-bounded by `_MAX_BOILERPLATE_LEN=120`) and live probes measure < 0.01 ms even on pathological 120-char inputs. However, no pytest regression guard exists. If a future contributor adds a third affiliate pattern with `(.+)+` shape, the safety regression would only surface in production via slow filter passes.
**Impact:** Low — current patterns verified safe; future pattern additions could regress without detection.
**Recommendation:** Add a pytest test that compiles each pattern against a 120-char pathological input and asserts wall-time < 100 ms. Pattern follows existing test-suite convention. Example:
```python
def test_affiliate_patterns_redos_bounded():
    import time
    from boilerplate_filter import is_boilerplate
    for inp in ['*quick link!' + ' '*100 + 'shop', 'buy now from ' + 'a'*100]:
        t0 = time.perf_counter()
        for _ in range(1000):
            is_boilerplate(inp[:120])
        assert (time.perf_counter() - t0) < 0.5  # 500us avg ceiling
```
**CWE:** CWE-1333 (inefficient regular expression).

#### Minor 2 — Inline `import socket` / `from xml.parsers...` inside `_fetch_orangetrack_entries`
**Category:** best-practice
**Location:** `news_bot.py:1400-1409`
**Description:** Imports moved inside the function body for `socket`, `xml.parsers.expat.ExpatError`, `xml.sax.SAXParseException` (with a fallback try/except chain), and `urllib.error.URLError`. Functionally correct, but they re-execute on every cron tick. Not a security issue per se — but the fallback `SAXParseException = Exception` swallow path means a real SAXParseException (if it ever fired and the SAX module structure changed) would silently fall through to the catch-all `Exception` arm without distinguishing it from arbitrary errors, weakening the FEED_XML_PARSE vs FEED_TIMEOUT classification.
**Impact:** Diagnostic noise only. No SSRF / injection consequence.
**Recommendation:** Move imports to module top; if the import shape really is uncertain, log a warning when the fallback assignment activates so operators see the classification has degraded.
**CWE:** CWE-754 (improper check for unusual or exceptional conditions).

#### Minor 3 — `_safe_for_ping` keeps `mailto:` schemes (informational)
**Category:** best-practice
**Location:** `orangetrack_source.py:69`
**Description:** `_ALLOWED_HREF_SCHEMES = {"http", "https", "mailto"}` includes `mailto:` for editorial mailto-links. orangetrackdiecast.com posts in practice rarely include mailto, but in principle a poisoned RSS entry could propagate `mailto:?subject=…&body=…` to the channel via a paragraph anchor. Since the destination renders as plain text on Telegraph (Telegraph's own filters strip mailto on the final render), the practical impact is close to zero — but the project's other sources don't expose `mailto:` in editorial body text either.
**Impact:** Negligible — rendered output unaffected because Telegraph strips it; only present in the intermediate `runs[].href` field, never user-facing.
**Recommendation:** Optional — drop `mailto` from `_ALLOWED_HREF_SCHEMES` for parity with lamley/autoevolution unless an editorial mailto reference is genuinely expected. Keeping it costs nothing and the constant is well-commented.
**CWE:** None applicable (not a vulnerability, scheme-policy choice).

---

## OWASP Top 10 (2021) cross-reference

| Category | Status | Notes |
|---|---|---|
| A01 Broken Access Control | N/A | No multi-tenant / RBAC surface in this module. |
| A02 Cryptographic Failures | N/A | No crypto in feature scope; HTTP fetch is the existing requests stack. |
| A03 Injection | **PASS** | href/src scheme filtering (Decision 2), `_safe_for_ping` control-char strip (Decision 5), HTML parsed via BeautifulSoup, no string-concat into queries. Telegraph node injection vector closed at parser layer. |
| A04 Insecure Design | **PASS** | Response-size DoS bounded; aggregator triple-bound (per-code 50, total 500, output 3500 chars); ReDoS-safe regex shapes; admin-ping failure non-fatal. Threat-modeling artifacts present in tech-spec Decision sections. |
| A05 Security Misconfiguration | **PASS** | No new config surface. `INSTANCE_LABEL` env-var read with `os.getenv` (returns None on absent), label safely passed through `instance_label` formatting. |
| A06 Vulnerable Components | **PASS (informational)** | No dependency changes in this feature; `requests`, `feedparser`, `beautifulsoup4` versions inherited from existing requirements.txt. Out-of-scope for this audit. |
| A07 Auth Failures | N/A | No auth surface. |
| A08 Software and Data Integrity | **PASS** | `feedparser.parse` is the only deserializer; XML parse errors classified as FEED_XML_PARSE (no eval/pickle/yaml.load). |
| A09 Security Logging and Monitoring | **PASS** | `_safe_for_ping` sanitizes adversary-controlled link strings BEFORE they enter the aggregator → admin ping. Newline injection neutralized (test `test_control_char_sanitization`). Logger-level events at WARNING/ERROR for transport / parse failures. |
| A10 SSRF | **PASS** | Allowlist at TWO call sites (Decision 13 fully implemented). `allow_redirects=False`. Bounded stream. Live probes confirm subdomain attack, cloud metadata IP, scheme-relative, malformed, userinfo, IP literals, mixed-case hostnames all behave correctly. |

---

## Hardcoded secrets scan

Reviewed `orangetrack_source.py`, the `news_bot.py` orangetrack-related additions, and `boilerplate_filter.py` additions: zero secrets, zero credentials, zero connection strings. The only hard-coded URL is the public RSS feed URL `https://orangetrackdiecast.com/feed/` (line 40) and the public Telegra.ph proxy prefix (line 157) — both are intended canonical constants per Decision 1 / Decision 8.

---

## Final verdict

**PASS** — All nine spec'd security checks implemented and exercised by realistic tests. Three minor findings recorded for code-quality / future-proofing; none require changes before merging to `dev`.
