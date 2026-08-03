# Wave 3 — Security Audit

**Verdict: PASS**

OWASP Top 10 review of the orangetrack-rendering-fixes Wave 1 implementation
(`orangetrack_source.py`, `_llm_common.py`, `telegraph_publisher.py`) found no
new vulnerabilities. All claimed mitigations from tech-spec Decisions 4, 5, 9,
10 and the Risks table are present in code with the exact semantics promised
(strict scheme whitelist, `removeprefix("www.")` not `lstrip`, non-empty
netloc check, urlparse try/except + WARNING, DoS bounds at 100KB / 100 runs,
empty-text run skipped before `str.find`, bullet-doubling guard before
`"• "` prepend). The Telegraph render path emits dict node-trees only — no
HTML string concatenation anywhere on the new helper code paths. No findings.

---

## Scope

Read-only audit of the post-Wave 1 source:

- `/workspaces/debian-2/my-hw/orangetrack_source.py`
  - `_walk` (the new `<li>` branch at lines 578–599)
  - `_emit_heading` (level-aware dispatch at lines 415–449)
  - `paragraphs_flat` extraction (lines 625–629)
- `/workspaces/debian-2/my-hw/_llm_common.py`
  - `_PATCHED_TEXT_BLOCK_TYPES` constant (line 106)
- `/workspaces/debian-2/my-hw/telegraph_publisher.py`
  - `_strip_www` helper (lines 115–123)
  - `_is_same_site` predicate (lines 126–153)
  - `_render_paragraph_with_runs` helper (lines 160–230)
  - `_build_content_from_blocks` block loop (lines 233–324), in
    particular the `paragraph` / `heading` / `list_item` branches.

Out of scope (existing code, untouched by Wave 1): SSRF guard
`_is_allowed_orangetrack_url`, fallback HTTP fetch, aggregator,
`_safe_for_ping` — those were audited in earlier features.

---

## OWASP Top 10 (2021) checklist

| # | Category | Status | Reason |
|---|----------|--------|--------|
| A01 | Broken Access Control | N/A | No auth boundary in scope; bot is unidirectional RSS → Telegram pipeline. |
| A02 | Cryptographic Failures | N/A | No crypto operations introduced; HTTPS termination is delegated to `requests` and Telegraph SDK. |
| A03 | Injection (XSS / URL injection) | **PASS** | See detailed checks below — node-tree only, scheme whitelist, urlparse try/except. |
| A04 | Insecure Design (trust boundary) | **PASS** | See detailed checks — `removeprefix` precision, non-empty netloc gate; trust boundary documented. |
| A05 | Security Misconfiguration | N/A | No new config / env / CORS / header surface. |
| A06 | Vulnerable Components | N/A | Wave 1 introduces no new dependencies (uses stdlib `urllib.parse` only — confirmed in tech-spec "Dependencies" section). |
| A07 | Auth Failures | N/A | No auth in this code path. |
| A08 | Software / Data Integrity | N/A | No deserialization of untrusted input introduced; LLM JSON handling unchanged. |
| A09 | Security Logging / Monitoring | **PASS** | See detailed checks — urlparse exception WARNING with truncated href + exception class; DoS bound WARNING. |
| A10 | SSRF | N/A | No new HTTP request site introduced; `_is_same_site` is a pure URL comparison and never fetches. The pre-existing SSRF guard `_is_allowed_orangetrack_url` is untouched. |

### A03 — Injection (detailed)

The new helper `_render_paragraph_with_runs` builds a Telegraph node-tree
composed of Python `dict` and `str` values only. No `f"<a href={…}>"`,
no `"".join`, no string templating: each `<a>` is a literal dict
`{"tag": "a", "attrs": {"href": href}, "children": [text[start:end]]}`
(telegraph_publisher.py:226). The Telegraph SDK serializes this as JSON
to `createPage`, which performs its own server-side attribute encoding.
Therefore RSS-supplied text/href values cannot break out of attribute
or text context — there is no XSS sink in the new code path.

URL-injection is blocked at the same-site predicate by a strict
*whitelist* of schemes:

```python
# telegraph_publisher.py:149
if u.scheme.lower() not in ("http", "https"):
    return False
```

This rejects `mailto:`, `javascript:`, `data:`, `file:`, `vbscript:`,
and any other scheme. Whitelist beats blacklist — even if a new exotic
scheme were invented tomorrow, it would still fail this check. Even
then, the helper only uses `href` to wrap a `<a>` node whose contents
Telegraph itself validates server-side; the scheme check is a belt-and-
braces guard.

### A04 — Insecure Design (detailed)

Three architectural defenses make the same-site check safe against
the lookalike-domain bypass that motivated tech-spec Decision 4:

1. **`removeprefix("www.")` (NOT `lstrip("www.")`)** — `_strip_www`
   uses Python 3.9+ `str.removeprefix`, which strips the literal prefix
   `"www."` exactly once if present and otherwise returns the string
   unchanged. `str.lstrip("www.")` is a *character-set* strip that
   would consume any leading run of `w`, `.`, characters and would
   collapse `wwwfake-orangetrackdiecast.com` into `fake-orangetrackdiecast.com`
   (which still wouldn't match orangetrackdiecast.com — but the
   character-class behavior is unsound and the code is correct.)
2. **Non-empty netloc gate** — `_is_same_site` returns False when
   either `href` or `source_netloc` is falsy at the top, AND when the
   parsed `u.netloc` is empty (line 151). Without the latter check,
   `mailto:x` with empty `source_url` would compare two empty netlocs
   and match.
3. **Strict scheme** — see A03 above.

The display-text / href divergence (Risks table row "Display-text vs href
divergence") is documented as a *known limitation, not a vulnerability*:
the link's `href` already lives inside `<a href>` of the original
orangetrack HTML; the bot only preserves it. Trust boundary is the
orangetrack site as a whole — the same boundary that already governs
article body text. See "Known limitations" below.

### A09 — Security Logging & Monitoring (detailed)

Two WARNING log sites added by Wave 1:

- **urlparse exception** — `_is_same_site` wraps `urlparse(href)` in
  `try/except Exception` and on failure logs:
  ```python
  # telegraph_publisher.py:142-148
  except Exception as exc:
      logger.warning(
          "[orangetrack-render] urlparse failed for %s...: %s",
          str(href)[:50],
          type(exc).__name__,
      )
      return False
  ```
  This logs only the first 50 characters of the href (no full URL leak)
  and the exception class name (no exception message that might echo
  attacker input). Severity: WARNING. The function returns False, so
  the run degenerates to plain text — no unhandled exception leaks
  upward.

- **DoS bound** — `_render_paragraph_with_runs` falls through to plain
  text + WARNING when limits are exceeded:
  ```python
  # telegraph_publisher.py:181-187
  if len(text) > _MAX_TEXT_FOR_RUNS or len(runs) > _MAX_RUNS_PER_BLOCK:
      logger.warning(
          "[orangetrack-render] DoS bound: text=%d runs=%d — falling through to plain text",
          len(text),
          len(runs),
      )
      return [text]
  ```

Both messages carry the `[orangetrack-render]` tag so an operator can
grep journalctl for them.

---

## Detailed checks

### 1. removeprefix vs lstrip — lookalike-domain check

**Required**: `_strip_www` must use `removeprefix`, not `lstrip`.
**Code** (telegraph_publisher.py:115–123):

```python
def _strip_www(netloc: str) -> str:
    """Return ``netloc`` lower-cased and with a leading ``www.`` removed.

    Uses ``str.removeprefix`` (Python 3.9+) — NOT ``str.lstrip("www.")``,
    which is a character-set strip and would also match ``wwwfake-…``
    lookalike domains (security audit critical finding).
    """
    n = (netloc or "").lower()
    return n.removeprefix("www.")
```

**Verified** — `removeprefix("www.")` confirmed; the comment explicitly
documents the rejected `lstrip` variant. Test
`test_lookalike_domain_not_matched_negative` (test_telegraph_publisher.py:809)
locks behavior with `https://wwwfake-orangetrackdiecast.com/x` rejected.

### 2. Mailto / javascript scheme rejection

**Required**: scheme check is a whitelist of `("http", "https")`,
applied before the netloc comparison.
**Code** (telegraph_publisher.py:149–150):

```python
if u.scheme.lower() not in ("http", "https"):
    return False
```

**Verified** — whitelist (not blacklist), rejects `mailto:`,
`javascript:`, `data:`, `file:`, `vbscript:`, etc. Tests
`test_mailto_scheme_dropped` (line 759), `test_javascript_scheme_dropped`
(line 765) lock the behavior.

### 3. urlparse exception handling

**Required**: try/except around `urlparse(href)` with WARNING log
(truncated href + exception class) and `return False`.
**Code** (telegraph_publisher.py:140–148):

```python
try:
    u = urlparse(href)
except Exception as exc:
    logger.warning(
        "[orangetrack-render] urlparse failed for %s...: %s",
        str(href)[:50],
        type(exc).__name__,
    )
    return False
```

**Verified** — try/except present, href truncated to 50 chars in log,
exception class name (not message) emitted, returns False. Test
`test_run_with_malformed_href_skipped` (line 742) locks the behavior.

Note: `urlparse` in modern stdlib rarely raises; broad `except Exception`
is appropriate defensive insurance and is consistent with the existing
codebase pattern (`orangetrack_source._is_allowed_orangetrack_url`
uses `except (ValueError, AttributeError)` — slightly tighter, but
both are acceptable).

### 4. Non-empty netloc handling

**Required**: empty netloc on parsed href returns False; empty
`source_netloc` rejected at function top.
**Code** (telegraph_publisher.py:138–139, 151–152):

```python
if not href or not source_netloc:
    return False
…
if not u.netloc:
    return False
```

Plus, the helper `_render_paragraph_with_runs` computes the source
netloc once before the loop (telegraph_publisher.py:189–192):

```python
try:
    source_netloc = urlparse(source_url).netloc if source_url else ""
except Exception:
    source_netloc = ""
```

If `source_url` is empty/malformed, `source_netloc` is `""` and the
falsy guard at line 138 short-circuits every run to False — exactly
the degenerate case `test_empty_netloc_dropped_when_source_also_empty`
covers.

**Verified** — non-empty checks on both sides plus parsed-netloc
non-empty check. Test at line 771.

### 5. DoS bounds (text > 100KB, runs > 100)

**Required**: thresholds 100_000 and 100; fall-through to plain text +
WARNING; bound applied BEFORE the main loop.
**Code** (telegraph_publisher.py:156–157, 181–187):

```python
_MAX_TEXT_FOR_RUNS = 100_000
_MAX_RUNS_PER_BLOCK = 100
…
# DoS bounds (Decision 10)
if len(text) > _MAX_TEXT_FOR_RUNS or len(runs) > _MAX_RUNS_PER_BLOCK:
    logger.warning(
        "[orangetrack-render] DoS bound: text=%d runs=%d — falling through to plain text",
        len(text),
        len(runs),
    )
    return [text]
```

**Verified** — exact thresholds (100_000 and 100), check at line 181
runs immediately after the empty-runs guard (line 178) and *before*
the source_netloc compute and the main span-collection loop (line
197). Returns `[text]` (plain text fall-through) with WARNING.

Tests at lines 894 (`test_dos_bound_skips_helper_on_huge_text`) and 905
(`test_dos_bound_skips_helper_on_too_many_runs`) lock this.

### 6. Bullet-doubling guard

**Required**: leading bullet/whitespace stripped from `block.text`
BEFORE prepending `"• "` literal to the rendered children.
**Code** (telegraph_publisher.py:311–315):

```python
elif t == "list_item":
    text = (block.get("text") or "").lstrip(" •\t\n")  # Decision 10 — strip leading bullet/whitespace before prepending
    nodes.append(p("• ", *_render_paragraph_with_runs(
        text, block.get("runs"), source_url,
    )))
```

**Verified** — `lstrip(" •\t\n")` is a character-set strip applied to
`block["text"]` BEFORE the `"• "` literal is prepended. Note the spec
suggested `" ••\t"` (two bullets); the implementation uses the simpler
single-bullet form `" •\t\n"` plus `\n`. Functional behavior is
equivalent for all realistic LLM outputs (any leading run of these
characters is consumed). Test
`test_list_item_strips_leading_bullet_in_text` (line 853) locks that
LLM output `"• Ferrari"` is rendered as `["• ", "Ferrari"]`, not
`["• ", "• Ferrari"]`.

This is also a defense-in-depth fall-through for the source side:
the parser (`orangetrack_source._walk` `<li>` branch, lines 588–598)
never inserts a bullet; only `_runs_from_tag` text reaches `block.text`.
So the guard activates only on adversarial LLM output, exactly as
designed.

### 7. Empty / whitespace-only run.text — zero-width wrap guard (Decision 9)

**Required**: empty/whitespace-only `run.text` skipped BEFORE the
`str.find` call.
**Code** (telegraph_publisher.py:198–200):

```python
for run in runs:
    run_text = run.get("text") if isinstance(run, dict) else None
    if not run_text or not run_text.strip():
        continue  # Decision 9 — empty/whitespace skip BEFORE str.find
```

**Verified** — the empty-string and whitespace-only check happens
BEFORE `text.find(run_text)` (line 204). Without this guard,
`"some text".find("")` returns 0 and would emit a zero-width `<a>`
at position 0. Test `test_run_with_empty_text_skipped` (line 730)
locks this.

### 8. XSS via node-tree — confirmation of no HTML concatenation

Telegraph builds its server-side HTML from JSON node-trees. The new
helper code paths construct only Python dicts + str:

- `_render_paragraph_with_runs` returns a list whose elements are
  either `str` (segments of `text`) or
  `{"tag": "a", "attrs": {"href": href}, "children": [text[start:end]]}`
  (line 226). No `f"<a href="..."`, no `"".join`, no formatting.
- `_build_content_from_blocks`'s helper inner functions `p`, `heading`,
  `figure_img`, `iframe`, `a`, `i_`, `b_` (lines 259–273) all return
  `dict` literals.
- The `f"h{lvl}"` in `heading()` (line 273) uses `lvl` constrained to
  `(3, 4)` integers — not user input.

There is no string-context HTML construction in the new code. The
final JSON serialization is performed by the Telegraph SDK, which is
considered trusted and out of scope.

---

## Known limitations / not-vulnerabilities

### Display-text vs href divergence (trust boundary)

Documented in tech-spec Risks table, row "Display-text vs href
divergence". Briefly: the substring approach wraps arbitrary RU display
text in `<a>` whose `href` is whatever was in the original
`<a href>` of the orangetrack HTML. If Brad's article body itself
contained a malicious self-link
`<a href="https://orangetrackdiecast.com/phish">Mercedes</a>`, the
bot would faithfully reproduce it.

This is **not a new vulnerability introduced by this feature** — the
same trust boundary applies to all body text from orangetrack
(translations, paragraphs, headings, captions). The href is already
in the source HTML; the bot does not synthesize new hrefs from text.
Defense-in-depth: Decision 4's strict scheme + same-site check ensures
no off-domain redirect is introduced by our code; an external
attacker cannot inject a non-orangetrack href via this path. Detection
of compromise of orangetrack proper is operator-side (visual scan of
the channel during normal post-deploy verification).

This audit confirms the limitation is documented in the Risks table
and that no code change in Wave 1 widens the trust boundary.

### Bullet-strip set is `" •\t\n"` (not `" ••\t"`)

Tech-spec Decision 10 wording suggests `lstrip(" ••\t")`; the
implementation uses `lstrip(" •\t\n")`. Both are character-set
strips with the same defensive intent (consume any combination of
leading bullets and whitespace). The implemented set is functionally
sound — `\n` is added (slight defensive expansion), the duplicated
`••` in the spec is collapsed to a single `•` because character-set
strips treat duplicate chars identically. Not a finding; flagging
only as documentation drift between spec text and code.

### Test for `_strip_www` on the source side (defense-in-depth)

`_is_same_site` calls `_strip_www(source_netloc)` on the right-hand
side too (line 153), so a `source_netloc` of `"www.orangetrackdiecast.com"`
is normalized identically. Test `test_www_prefix_normalized_for_same_site`
(line 777) locks this. Not a finding — confirmation of correctness.

---

## Findings

**No findings.** The Wave 1 implementation matches all security-relevant
mitigations from tech-spec Decisions 4, 5, 9, 10 with exact semantics.
No critical, major, or minor issues identified.

```json
[]
```

---

## Verification artifacts

- All code citations traced to the post-Wave 1 source files at
  `/workspaces/debian-2/my-hw/orangetrack_source.py`,
  `/workspaces/debian-2/my-hw/_llm_common.py`,
  `/workspaces/debian-2/my-hw/telegraph_publisher.py`.
- Test references traced to
  `/workspaces/debian-2/my-hw/tests/test_telegraph_publisher.py`
  (line numbers anchored at the test method `def`s).
- Tech-spec cross-references: Decisions 4, 5, 9, 10; Risks table rows
  "Display-text vs href divergence", "Bullet-doubling", "Quadratic-substring DoS".

Audit performed read-only; no source files modified, no tests
executed.
