# Security Audit — feature `source-formatting-parity` (Phase 1)

- **Auditor:** security-auditor (Task 11, Audit Wave)
- **Date:** 2026-08-06
- **Branch:** `dev`, audit target = commit `4ad4592` (Tasks 1–9 complete)
- **Scope:** Holistic OWASP-Top-10 audit of the WHOLE Phase-1 chain, not one
  file. UNTRUSTED input = source-article HTML **and** the LLM response. Flow
  traced end to end:
  `fetch` (host allowlist + 2 MB cap, `t_hunted_source.py:166,221`) →
  `BeautifulSoup` + `script/style/noscript` decompose (`t_hunted_source.py:232`) →
  `dom_blocks.BlockBuilder.walk` (regexes, video host gate, `src` picker, dedup
  key, parse-time bounds) → `blocks[] + runs[]` → `_blocks_if_aligned`
  (`news_bot.py:2996`) → `insert_pending` (`?` placeholder, opaque JSON) →
  `_llm_common._encode_format_markers` (request-path bound) → LLM →
  `_decode_format_markers` → `telegraph_publisher._build_content_from_blocks`
  (scheme validation + image cap) → Telegra.ph page; branch path → same nodes →
  `preview_renderer.render_html` → HTML under `file://`.
- **Files reviewed:** `dom_blocks.py` (new, 726 lines — primary object),
  `feature_flags.py` (new), `t_hunted_source.py`, `orangetrack_source.py`,
  `telegraph_publisher.py`, `_llm_common.py`, `news_bot.py`,
  `preview_renderer.py` (unchanged — the reference policy),
  `pending_articles_repo.py` (unchanged), `boilerplate_filter.py` (unchanged),
  `autoevolution_source.py` + `lamley_source.py` (Phase 2 — read to confirm
  unchanged and that their two defects did NOT migrate),
  `tests/fixtures/` (38 pages), `.dockerignore`, `.pre-commit-config.yaml`,
  `.env.example`, `deploy.sh`, both deploy workflows.
- **Method:** analysis only — **no source file changed by this audit**. Every
  invariant below is backed by an executed probe, not by reading the tech-spec.
  Probe output is inline.

---

## Verdict: PASS-WITH-NOTES

**All four named threats are closed and verified in code, with probes.** Zero
Critical, zero Major — **nothing to fix before deploy**. Three Minor findings
and five Informational observations are recorded. Per the operator's standing
rule of 2026-08-06, each finding is labelled **MEASURED** (failure observed on
real data) or **DERIVED** (reasoned from the code). **Every finding in this
report is DERIVED.** None of them is a demanded code change; they are
observations, and the report says so at each one.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| Major | 0 |
| Minor | 3 (all DERIVED) |
| Informational | 5 |

The feature adds **no new package dependency** (`requirements*.txt` unchanged),
**no DB migration**, **no new outbound network call**, and **no authentication
or crypto surface**. Both new modules (`dom_blocks.py`, `feature_flags.py`) are
present in all three deploy manifests. All new SQL is the pre-existing static
statement with `?` placeholders. The one genuinely new attack surface — hostile
source markup now surviving to `iframe src`, `img src`, the LLM request body
and a `file://` preview — is gated at every one of the four points the
tech-spec named.

---

## Verified invariants — the four named threats

### Threat 1 — bounded-quantifier / ReDoS contract — **CLOSED**

Complete list of regexes this feature added, changed, or moved into the shared
module. `grep -n "re.compile"` over the six named files plus a separate sweep
for uncompiled `re.sub`/`re.search`/`re.match`.

| # | File:line | Pattern name | Regex | Verdict |
|---|---|---|---|---|
| 1 | `dom_blocks.py:108-111` | `_VIDEO_PROVIDERS["youtube"]["id_re"]` | `(?:youtu\.be/\|/embed/\|/watch\?v=\|/v/\|/shorts/)([A-Za-z0-9_-]{6,})` | **bounded** — open-topped `{6,}` over a single char class, no nesting; carried from orangetrack, NOT autoevolution's `+`. See Informational 6. |
| 2 | `dom_blocks.py:115` | `_VIDEO_PROVIDERS["vimeo"]["id_re"]` | `(?:vimeo\.com/\|/video/)(\d{6,})` | **bounded** — same shape, digits only. |
| 3 | `dom_blocks.py:231` | inline in `text_from_runs` | `\s+` → `" "` | **bounded** — single char class, linear. |
| 4 | `dom_blocks.py:377` | inline in `runs_from_tag` | `\s+` → `" "` | **bounded** — same. |
| 5 | `telegraph_publisher.py:224` | `_SAFE_MEDIA_URL_RE` (**new**, Task 6) | `^https?://` (IGNORECASE) | **bounded** — `^`-anchored literal, no quantifier over a group. Byte-identical to `preview_renderer._SAFE_URL_RE`. |
| 6 | `_llm_common.py:214-215` | `_MAX_TEXT_FOR_RUNS`/`_MAX_RUNS_PER_BLOCK` (**new**, Task 4) | not a regex | n/a — constants only. |
| 7 | `t_hunted_source.py:66` | `_BLOGGER_SIZE_SUFFIX_RE` (inherited, now driven from `dom_blocks` via the injected dedup key) | `=s\d+(-c)?(?:/\|$)` | **bounded in effect** — `\d+` is open-topped but is a single char class with a literal-anchored prefix and no nested group; cannot backtrack catastrophically. Applies to a short `src` string, not article body. See Informational 6. |
| 8 | `feature_flags.py` | — | none | **no regex in the module.** |
| 9 | `preview_renderer.py:75,83` | `_SAFE_URL_RE`, `_SAFE_ATTR_NAME_RE` | unchanged by this feature | pre-existing, bounded, `^`-anchored. |

**No regex was added to `dom_blocks` that operates over unbounded article
body.** The two body-scale operations are `re.sub(r"\s+", …)` (linear) and
`str.find` loops that are explicitly bounded (Threat 4).

**Anti-pattern did NOT migrate.** `autoevolution.YOUTUBE_ID_RE:33`
(`([A-Za-z0-9_-]+)`) and its regex-only, host-gate-free `_video_embed_url:106`
remain **local to `autoevolution_source.py`**, which `git diff --stat
3362f26..HEAD` confirms is **unchanged**. `lamley_source.py` likewise
unchanged. The shared module carries orangetrack's `{6,}` variant.

**Heading punctuation uses `str.endswith`, not a regex** — direct Decision 8 /
tech-spec AC requirement. `dom_blocks.py:420-423`:

```python
if text.endswith("…") or text.endswith("…."):
    pass  # heading-compatible ellipsis
elif text.endswith("."):
    return False
```

Pinned by `tests/test_dom_blocks.py:217::test_punctuation_check_uses_endswith_not_a_regex`.

**Empirical probe (linearity under doubling, up to 2 MB = the t-hunted fetch
cap).** Adversarial inputs shaped per pattern (`https://youtu.be/` + 2 M `a`;
`/embed/` repeated; `**` repeated; `http` + `sss…://`; `=s999…`). Time ratio on
each doubling of input — a catastrophic backtracker would show ≫2:

```
_STRAY_MARKER_RE.sub                   [0.0064, 0.0060, 0.0195, 0.0526]  ratios=[0.94, 3.25, 2.69]
tp._BOLD_MARKER_RE.finditer-drain      [0.0055, 0.0133, 0.0267, 0.0569]  ratios=[2.42, 2.00, 2.13]
lc._BOLD_MARKER_RE.finditer-drain      [0.0065, 0.0122, 0.0244, 0.0485]  ratios=[1.87, 2.00, 1.99]
lc._BOLD_MARKER_RE unbalanced          [0.0040, 0.0050, 0.0114, 0.0212]  ratios=[1.25, 2.28, 1.86]
yt id_re.search                        [0.0007, 0.0011, 0.0014, 0.0031]  ratios=[1.74, 1.24, 2.18]
yt id_re near-miss (/embed/ ×285k)     [0.0039, 0.0056, 0.0120, 0.0283]  ratios=[1.42, 2.14, 2.37]
```

All linear. Worst absolute cost across every pattern × every adversarial input
at 2 MB is **0.06 s**. A first sweep flagged `_STRAY_MARKER_RE` at ratio 37 —
re-measured, that was `findall()` allocating a 100k-element list, not
backtracking; the pattern is the literal `\*\*` and cannot backtrack. Recorded
here so the discrepancy is not rediscovered as a finding.

### Threat 2 — video host gate BEFORE the ID regex, for every source — **CLOSED**

Gate lives **inside `dom_blocks`**, `dom_blocks.py:169-203`. Order is verified
by reading, not assumed:

```python
host = (parsed.hostname or "").lower()   # dom_blocks.py:193
if host not in hosts:                    # dom_blocks.py:194  ← GATE
    return None
m = spec["id_re"].search(url)            # dom_blocks.py:196  ← regex, AFTER
```

- **Hosts arrive as DATA, not a callable.** `BlockBuilder.__init__` accepts
  `video_hosts=()` / `video_provider=""` (`dom_blocks.py:501-502`) and the
  provider table `_VIDEO_PROVIDERS` (`:104-118`) is module-private. There is no
  injectable wrapper anywhere — Decision 1 honoured.
- **`hostname`, not `netloc`** (`:193`) — strips port and userinfo.
- **Exact tuple membership**, not `in`/`endswith`/substring (`:194`).
- **`.lower()`** on the host, so `HTTPS://YOUTUBE.COM/...` passes.
- **Failure = `None` and no block emitted** (`emit_iframe`, `:610-617`). No raw
  URL is ever put into an `iframe src` "just in case".
- **Vimeo added as DATA** (`:114-117`), not as a second regex path.
- **Every emitter path goes through it.** `emit_iframe` is the only producer of
  a `video` block, and it is the only caller of `video_embed_url` inside the
  module. The only external caller is
  `orangetrack_source._video_embed_url:122-135`, a thin adapter that also
  passes a tuple.

**Probe — the full attack list from the task file, against both call paths:**

```
dom_blocks.video_embed_url(hosts=YOUTUBE_HOSTS, provider="youtube")
  None  https://evil.example/youtube.com/embed/abc123        (substring host)
  None  https://youtube.com.evil.example/embed/abc           (suffix domain)
  None  https://evil.example/?x=youtu.be/abc123              (query smuggling)
  None  //youtube.com/embed/abc                              (scheme-relative)
  None  https://user:pass@youtube.com@evil.example/embed/abc (userinfo)
  None  javascript:/*youtube.com/embed/x*/alert(1)           (js scheme)
  None  https://youtube.com:8080@evil.example/embed/abcdef   (port+userinfo)
  None  data:text/html,<script>/embed/abcdef</script>
  None  https://myoutube.com/embed/abcdef                    (prefix lookalike)
  None  https://notyoutube.com/embed/abcdef
  None  https://youtube.com.evil.example:443/embed/abcdef
  MUST PASS, and does:
  https://YOUTUBE.COM/embed/abcdef      -> https://telegra.ph/embed/youtube?url=…  (case ok)
  https://www.youtube.com/watch?v=…     -> wrapped
  https://youtu.be/abcdefg              -> wrapped
  https://m.youtube.com/embed/abcdefg   -> wrapped
orangetrack._video_embed_url — identical verdict on all 16 inputs.
```

**Gate holds with the kill switch OFF** (probe re-run with
`SOURCE_FORMATTING_ENABLED=0`): the flag gates only
`article["blocks"] = blocks` in the parser (`t_hunted_source.py:344`), never
`dom_blocks`, so orangetrack — which still emits blocks in the off state —
keeps every gate. Verified: video gate, href gate, img gate and the publisher
`src` gate all still reject in the flag-off state.

Pinned by `tests/test_dom_blocks.py:331::test_video_host_gate_runs_before_the_id_regex`.

### Threat 3 — image/video `src` scheme validation in the publisher — **CLOSED**

Centralised in `telegraph_publisher._is_safe_media_url:227-236`, applied at
**all four** places a `src` reaches a node:

| Sink | Line | Covered |
|---|---|---|
| hero figure | `:464-466, 486, 489-493` | yes — hero is selected from `valid_image_idx` |
| body images | `:527-534` (`if i not in kept_image_set: continue`) | yes |
| `iframe src` (video) | `:535-542` | yes — the one the tech-spec risk line omitted |
| flat `images` path (`_build_content`) | `:596-605` | yes — same policy |

- **Policy matches the preview**, not a second one: `_SAFE_MEDIA_URL_RE =
  re.compile(r"^https?://", re.IGNORECASE)` (`:224`) is byte-identical to
  `preview_renderer._SAFE_URL_RE` (`preview_renderer.py:75`). One divergence in
  *application* — see **Minor 1**.
- **Fail-open**: an invalid block is skipped with a WARNING; the call does not
  raise. Probe below confirms across 17 hostile values including `None` and
  `123`.
- **Cap accounting is correct** (the thing most likely to be wrong): invalid
  `src` is dropped **before** the cap (`:458-466`, with an explicit comment),
  so junk never eats a live slot; and if the hero is the invalid one, the next
  valid image becomes hero.

**Probe:**

```
17 hostile src values through _build_content_from_blocks (image AND video blocks):
  javascript:  data:text/html  file:///etc/passwd  vbscript:  //evil/x.jpg
  /img/x.jpg   httpx://evil/x.jpg  "  javascript:"  "\tjavascript:"
  "java\nscript:"  ""  None  123  jAvAsCrIpT:  https:/evil/x.jpg  http:evil/x.jpg
  -> call did NOT raise; URL attrs emitted into the node tree:
       <img src='https://ok.example/hero.jpg'>
       <iframe src='https://telegra.ph/embed/youtube?url=x'>
       <a href='https://t-hunted.example/a'>       (the source footer)
  -> UNSAFE LEAKS: NONE

cap accounting:
  limit=3, [1 junk + 5 valid] -> ['…/0.jpg','…/1.jpg','…/2.jpg']  (junk ate no slot)
  hero invalid, limit=1       -> hero becomes '…/hero.jpg'         (next valid promoted)
  image_limit=0               -> []                                (0 honoured literally)
```

### Threat 4 — request-path bound and its degradation contract — **CLOSED**

`_llm_common._encode_format_markers:260-271`. The bound sits **before** the
`for run in runs:` loop at `:272` — an early return, not a check inside the
loop:

```python
if not runs:
    return text
if len(text) > _MAX_TEXT_FOR_RUNS or len(runs) > _MAX_RUNS_PER_BLOCK:
    logger.warning("[llm-request] DoS bound: text=%d runs=%d — sending paragraph without bold markers", …)
    return text          # ← returns BEFORE any str.find
```

- **Both dimensions** are checked — length *and* count. The count-only case
  (small text, 200k runs) is the one a single bound would miss; probed below.
- **Contract honoured exactly:** `out == text`. Text **intact**, runs stripped,
  no truncation, no exception, no lost article, **no operator ping**, exactly
  one WARNING.
- **Names and values have not drifted** across the three layers:

```
dom_blocks   MAX_RUNS_PER_BLOCK=100  MAX_TEXT_FOR_RUNS=100000   (parse)
_llm_common _MAX_RUNS_PER_BLOCK=100 _MAX_TEXT_FOR_RUNS=100000   (request)
telegraph   _MAX_RUNS_PER_BLOCK=100 _MAX_TEXT_FOR_RUNS=100000   (render)
```

Same names (module-public in `dom_blocks`, private in the other two — a
deliberate visibility difference, not a drift), same values, cross-referenced
by comments in all three files.

- **Resource fuse, not the editorial threshold AC7 forbids.** It keys on
  `len(text)`/`len(runs)`, never on the *proportion* of bold. Order of
  magnitude vs. reality: the measured corpus peaks at **3 runs per block**
  against a bound of 100 — the fuse cannot fire on a real article, which is
  what AC7 requires.
- **Heading heuristic interaction is as Decision 8 predicts:** over the bound
  `runs_from_tag` returns a single unformatted run (`dom_blocks.py:386-391`),
  so the paragraph stops being "entirely bold" and is not promoted. Cosmetic
  and not a structure-suppression lever — the input needed (>100 runs or >100k
  chars in ONE paragraph) is far past anything a blog produces, and the effect
  is losing a heading, not gaining one.
- **Response leg (LLM-controlled) is safe too:** `_decode_format_markers` and
  `telegraph_publisher._decode_bold_markers` are linear (probe below), and the
  200k runs they can produce are caught by the render-path bound.

**Probe:**

```
text=200000 runs=200000  elapsed=0.00005s   out == text: True   markers: False   truncated: False
small text (500) + 200000 runs: elapsed=0.00001s  out == text: True   ← count bound alone catches it
AT bound (100 runs): '**bold**' encoded: True                        ← does not over-fire
render path, same input: 0.00001s, result == [text]
response leg: _decode_format_markers on 1 MB / 200k marker pairs = 2.00s (linear),
              then _render_paragraph_with_runs -> plain text (bound fires: 200000 > 100)
```

Reference: Task 4's own measurement recorded ≈160 s for this input unbounded —
three orders of magnitude of discriminating power.

---

## General OWASP pass

**A01 Broken Access Control — not applicable.** The feature adds no endpoint,
no user, no role. The bot has one admin ID and this feature touches none of it.

**A02 Cryptographic Failures — not applicable.** No new crypto operation, no
key, no secret handling added.

**A03 Injection — CLEAN.**
- *SQL:* `pending_articles_repo.py` unchanged. `blocks` is an opaque JSON blob
  passed as a `?` placeholder (`:381-395`, `_dumps(entry.get('blocks'))`,
  NULL-preserving). Grep for `execute(f"`, `execute(` with `%`/`+` returns
  **zero** — all SQL is static.
- *Telegraph nodes:* structure is dicts, never string concatenation. Task 6
  added no string-built HTML — verified by reading `_build_content_from_blocks`
  in full.
- *Preview HTML:* `preview_renderer.py` is **unchanged by this feature** (Task 6
  added only tests). Its three layers stand: tag allowlist (`:58-62`), URL
  scheme allowlist (`:75`), attribute-name allowlist (`:83`), plus CSP
  (`:87-91`) and `html.escape(..., quote=True)` on every string and every
  retained attribute value.

**End-to-end injection probe.** A synthetic hostile article — inline
`<script>`, `onclick`/`onerror`/`onload` handlers, `<svg onload>`,
`javascript:` in `a href` and `img src` and `iframe src`, `data:text/html`,
`httpx://`, `//evil/`, a `youtube.com`-substring embed URL, and a
`"><script>alert(10)</script>` paragraph body — pushed through the real
t-hunted policy seams → `BlockBuilder` → `_build_content_from_blocks` →
`render_html`:

```
blocks produced:  4 paragraphs (text only), 1 image (the legitimate blogspot URL),
                  1 video (the legitimate youtube embed, proxy-wrapped)
node-tree URL attrs: img=https://1.bp.blogspot.com/real.jpg
                     iframe=https://telegra.ph/embed/youtube?url=…realvid1
                     a=https://t-hunted.example/a   → UNSAFE: NONE
preview HTML:     '<script' False | onclick False | onload False | javascript: False
                  data:text/html False | '<svg' False | httpx:// False | '"><script' False
                  CSP meta present: True
                  tags emitted: a body figure h1 head hr html i iframe img meta p strong style title
```

The single `onerror` substring in the preview is inside **escaped text** — the
source had it entity-encoded (`&lt;img … onerror=…&gt;`), so it is article
prose, correctly re-escaped. Not executable.

**A04 Insecure Design — CLEAN, with one design note.** Every degradation is
fail-open and silent by Decision 3b, and the audit confirms *silent* means
logged-but-not-pinged, not swallowed: `_blocks_if_aligned` logs the link and
both counts (`news_bot.py:3009-3013`); the bound logs both sizes; the publisher
logs each dropped `src`. Bold decoded from `**` in the LLM response — an
untrusted channel — takes the **same** escaping path as source text: it becomes
`runs`, is rendered through `_render_paragraph_with_runs`, and reaches the
preview through the same `html.escape`. Confirmed by the response-leg probe.
Empty/`None`/malformed `blocks` fall back cleanly:

```
blocks=None / [] / [{}] / [{"type":"image"}] / [{"type":"unknown"}] → 1–2 nodes, no raise
```

**A05 Security Misconfiguration — CLEAN.** `feature_flags.py` reads one env var
at import (`:50-52`), stdlib only, no `load_dotenv`, no first-party import — no
cycle, no side effect. `.env.example` documents the flag **commented out**
(`:67`) and carries no secret value. Both new modules appear in **all three**
manifests: `deploy.sh:69-70` (inside the real `FILES=(` array, not a comment),
`.github/workflows/deploy.yml:142-143`, `.github/workflows/deploy_test.yml:113-114`.
An unlisted module is the ImportError-crashloop this project has already
suffered; that hazard is closed.

**A06 Vulnerable Components — CLEAN.** `git diff --stat 3362f26..HEAD --
requirements.txt requirements-dev.txt` is **empty**. No new package. Matches
tech-spec § Dependencies.

**A07 Identification & Authentication Failures — not applicable.** No
authentication surface exists in or near this feature.

**A08 Software and Data Integrity — CLEAN, one sanctioned edit.** The golden
baseline `tests/fixtures/orangetrack_golden.json` was regenerated once, in Task
6's commit `693d1c1`. That is the **operator-approved AC9/AC10 deviation**, and
decisions.md records three programmatic proofs that exactly one fixture changed
and only in `figure` node count (20 → 10). `tests/test_orangetrack_source.py` is
**untouched** across the whole feature (`git diff --stat 3362f26..HEAD` empty) —
the gate passed honestly. No deserialization risk added: `blocks` is
`json.loads` of our own column, never `pickle`/`yaml.load`.

**A09 Security Logging & Monitoring — CLEAN.** Every WARNING this feature added
logs **counts and identifiers, never article body**:
`_llm_common.py:266-270` (two integers), `telegraph_publisher.py:469-472,
479-484, 536-540, 602-604` (a `%.100r`-truncated `src`), `news_bot.py:3009-3013,
3018, 3033, 3038-3041` (link + two counts). `dom_blocks.py` logs **nothing at
all**. No secret can appear in these lines. **Log-injection probe** — an
attacker-controlled `src` carrying newlines and a forged admin line:

```
input:  "javascript:x\nCRITICAL FORGED ADMIN LINE: bot token 123456:AAA\n" + "A"*500
result: 1 log line, newlines rendered as literal \n by %r, payload truncated at 100 chars
```

`%.100r` (repr, not str) is the right choice and it holds.

**Zero `send_admin_notification` on any degradation path of this feature** —
Decision 3b verified by grep across `dom_blocks.py`, `feature_flags.py`,
`telegraph_publisher.py`, `_llm_common.py` (none present) and
`t_hunted_source.py` (its three `_notify` calls are the pre-existing
host-rejected / fetch-failed / no-body alerts, none of them a markup-degradation
path). Untrusted text therefore never travels to Telegram.

**A10 SSRF — not applicable, and confirmed not newly introduced.**
`dom_blocks.py` and `feature_flags.py` contain **no network call whatsoever**;
the feature's diff to `telegraph_publisher.py`/`_llm_common.py` adds none.
Article images and iframes are fetched by the Telegra.ph client and the reader's
browser, not by the bot. The one outbound fetch, `t_hunted_source`, keeps its
exact-hostname allowlist (`:44,166-178`) and its 2 MB cap (`:54,221`) unchanged.

---

## Fixture corpus (Task 1) — checked as a separate object

- **No secrets, cookies, tokens or PII.** `grep -rniE
  "set-cookie|authorization:|api[_-]?key|bearer |csrftoken|password|secret|private[_-]key|BEGIN .*PRIVATE"`
  over `tests/fixtures/` → **0 hits**. Email-shaped strings → **0 hits**.
- **Three token-shaped matches investigated and all three are false positives:**
  1. `eyJpbWciOiJodHRwczpc…` in `lamley-awards-2025-…html` — a Jetpack
     social-image token inside `og:image`, i.e. an HMAC over the page's own
     public metadata, already served publicly. Not our credential.
  2. `sk-SsOX3xjPTwbbFHCRY9W2-h_h-dErVOi` in `mais-um-novo-lote-…html` — a
     substring of a Blogger CDN image path (`…FU75e` + `sk-SsOX…/s1080/…`), not
     an OpenAI key.
  3. `sk-gradient-background` — the WordPress CSS class
     `.has-luminous-dusk-gradient-background`.
- **`.dockerignore` line 3 is `tests`** — the 3.5 MB corpus does not ship to the
  prod image. Verified the line is intact and not commented.
- **Pre-commit not bypassed.** `gitleaks` (`.pre-commit-config.yaml:16-19`) has
  **no exclude** and therefore covers the fixtures; `detect-private-key` also
  runs unexcluded. The only exclude (`:34,36`) applies to
  `trailing-whitespace` / `end-of-file-fixer` and is justified in a comment —
  the pages are byte-exact evidence and both hooks rewrite them. Largest fixture
  is **384 KB** against `check-added-large-files --maxkb=1000`, so the cap was
  respected rather than bypassed. Task 1 records `pre-commit run --all-files`
  passing without `--no-verify`.

---

## Findings

### Minor 1 — publisher and preview scheme policies diverge on leading whitespace, and the parity test hides it — **DERIVED**

**Where:** `telegraph_publisher.py:227-236` (`_is_safe_media_url`) vs.
`preview_renderer.py:128-136` (`_render_attrs`); test at
`tests/test_telegraph_publisher.py:1327-1345`.

**What.** The two regexes are byte-identical, but they are *applied* to
different strings. The publisher validates `src.strip()` and then emits the
**raw, unstripped** value into the node (`:236` vs `:532`). The preview
validates the **raw** value with no strip (`preview_renderer.py:132`). So for a
`src` with leading whitespace the two layers disagree — the publisher emits it,
the preview drops the attribute:

```
value                          publisher  preview
'  https://ok.example/a.jpg'      True     False   ← diverge
'\nhttps://ok.example/a.jpg'      True     False   ← diverge
'\thttps://ok.example/a.jpg'      True     False   ← diverge
'https://ok.example/a.jpg  '      True     True
publisher emits: '  https://ok.example/a.jpg'   preview keeps src attr: False
```

**Attack path.** A source page serves `<img src=" https://cdn/x.jpg">`. The
image publishes to Telegra.ph but is invisible in the preview the operator uses
to check the page before it ships. Direction of divergence is *safe* — the
publisher is the more permissive of the two and the value is still `http(s)`
after stripping, so nothing unsafe can render. The cost is that the preview
stops being an honest rendering of what publishes, which is the property Threat
3 exists to protect.

**Reachability today: none on this feature's paths.** Every Phase-1 `src`
reaches the publisher through `dom_blocks.safe_img_src`
(`dom_blocks.py:151-166`), which returns the **stripped** value, so a
whitespace-prefixed `src` cannot occur. `lamley` and `autoevolution` build
their flat lists with `startswith("http")`, which also excludes it. This is why
the finding is Minor and DERIVED, not measured.

**The test masks it.** `test_publisher_policy_matches_preview_renderer` already
contains the exact divergent input `"  https://a.example/x.jpg"` in its URL
list (`:1336`) — and then neutralises it by calling
`preview_renderer._SAFE_URL_RE.match(url.strip())` at `:1344`. The `.strip()`
is not what the real preview does. The test is green for the wrong reason.

**Recommendation (observation, not a demanded change).** Drop the `.strip()`
from the test's preview arm so it models `_render_attrs`, then make the two
agree in one of two ways: emit `src.strip()` from the publisher, or strip in
`preview_renderer._render_attrs` before matching. The first is the smaller
change and matches what `dom_blocks.safe_img_src` already does.

### Minor 2 — `video_embed_url` accepts any container for `hosts`; a `str` silently degrades exact matching to substring matching — **DERIVED**

**Where:** `dom_blocks.py:194` (`if host not in hosts:`).

**What.** `hosts` is used with `in` and never coerced. Passed a tuple it is
exact membership, as designed. Passed a **string** it becomes a substring test —
precisely the bug class the file's own history documents at
`orangetrack_source.py:48` and `_ALLOWED_HOSTS` (`orangetrackdiecast.com.attacker.example`
passing an `in` check).

```
d.video_embed_url("https://outube.com/embed/abcdef", hosts="youtube.com", provider="youtube")
  -> https://telegra.ph/embed/youtube?url=…   ← a NON-YouTube host wrapped
```

**Attack path.** Not reachable today. Both current call sites pass tuples
(`t_hunted_source.py:270` passes `dom_blocks.YOUTUBE_HOSTS`;
`orangetrack_source.py:134` passes `_YOUTUBE_HOSTS`), and `BlockBuilder`
coerces with `tuple(video_hosts)` (`dom_blocks.py:510`) so the builder path
fails **closed** even on a string. The exposure is the public module function
used directly, and **Phase 2 adds two more callers** to it.

**Recommendation (observation).** One line at the top of `video_embed_url`:
`hosts = tuple(hosts)` — or an `isinstance(hosts, str)` reject. It makes the
seam's guarantee independent of caller discipline, which is the stated point of
Decision 1.

### Minor 3 — quadratic index scan in `_build_content_from_blocks` — **DERIVED**

**Where:** `telegraph_publisher.py:467-468`:

```python
for i in all_image_idx:
    if i not in valid_image_idx:      # list membership → O(n) per iteration
```

**What.** Both operands are lists, so the loop is O(n²) in the number of image
blocks. Measured:

```
 2 000 image blocks  0.013 s
 5 000 image blocks  0.083 s
10 000 image blocks  0.335 s
20 000 image blocks  1.314 s      ← clean quadratic
```

**Attack path.** A source page with many `<img>` tags. `<img
src="https://1.bp.blogspot.com/x/0000001.jpg">` is 51 bytes, so t-hunted's 2 MB
fetch cap admits **~41 000** distinct images, and nothing bounds the *number* of
blocks at parse time (only runs and text per block). Extrapolating the measured
curve, that ceiling costs roughly **5 s** of publish-time CPU — real, but
bounded by the fetch cap into nuisance territory, not the hours-long stall
Threat 4 was about. Verified end to end: a 20 000-`<img>` body walks in 0.08 s
and renders in 1.77 s.

**Reachability vs. measurement.** The 14-article corpus peaks at **27** images
(t-hunted cap 30). Nothing in real data approaches this. DERIVED.

**Recommendation (observation).** `valid_set = set(valid_image_idx)` and test
against it — one line, removes the quadratic entirely. Recorded rather than
demanded, per the 2026-08-06 rule.

### Informational 1 — `dom_blocks` relies on its callers to strip `<script>`/`<style>`

`runs_from_tag` treats every `NavigableString` as text (`dom_blocks.py:282-284`),
and bs4's `Script`/`Stylesheet` are `str` subclasses — so JS/CSS **source**
inside a `<p>`/`<li>`/`<h*>` would be spliced into block text, where
`get_text()` (the pre-feature path) dropped it:

```
new: <p>Lead text<script>var k="secret";alert(1)</script>tail</p> → 'Lead textvar k="secret";alert(1)tail'
old: same input via get_text(" ", strip=True)                     → 'Lead text tail'
```

**Fully mitigated today, and that is MEASURED.** All four parsers decompose
first — `t_hunted_source.py:232`, `orangetrack_source.py:300`,
`lamley_source.py:373`, `autoevolution_source.py:174` — and across the whole
committed corpus (10 t-hunted + 4 lamley + 24 orangetrack) the count of
`script`/`style`/`noscript` **reachable by the runs walker** is **0**. Not
XSS in any case: the output is text, escaped at both sinks. Recorded because the
shared module does not own an invariant it depends on, and a fifth source added
later inherits the trap rather than the guard.

### Informational 2 — the `<br>`-split path serialises and re-parses, so entity-encoded markup is re-interpreted

`emit_paragraph` serialises each `<br>` segment with `str(c)` and re-parses it
through `_parse_fragment` (`dom_blocks.py:552-553, 718-726`). Text that the
source entity-encoded becomes live markup on the second parse, and its text is
mangled or dropped:

```
'<p>seg one<br>&lt;script&gt;alert(1)&lt;/script&gt; tail</p>' → ['seg one', 'alert(1) tail']
'<p>seg one<br>&lt;img src=x onerror=alert(1)&gt; tail</p>'    → ['seg one', 'tail']
control, same content without <br>                             → ['<script>alert(1)</script> tail']
```

**Not injection** — confirmed by pushing it through publisher + preview:
`onerror` absent, `src="x"` absent. `runs_from_tag` yields text only, and both
sinks escape. This is content fidelity (a paragraph loses characters), which is
Task 10's perimeter; recorded here because the shape looks like an injection
seam and should be explicitly cleared rather than left ambiguous.

### Informational 3 — no bound on the *number* of blocks per article

Decision 8 bounds runs and text **per block**; nothing bounds how many blocks
one article produces. The 2 MB fetch cap is the only ceiling, and the blocks
list is JSON-serialised into SQLite. This is the shared root of Minor 3.
DERIVED; corpus peak is ~45 blocks.

### Informational 4 — `t_hunted` materialises the whole body before the size check

`t_hunted_source.py:221` reads `len(response.content)` — the full body is
already in memory when the 2 MB cap is evaluated (not a streamed check).
**Pre-existing and unchanged by this feature**; noted so it is not mistaken for
a gap this audit missed.

### Informational 5 — orangetrack keeps a private `_YOUTUBE_HOSTS` copy

`orangetrack_source.py:53-60` retains its own host tuple while
`t_hunted_source.py:270` uses the published `dom_blocks.YOUTUBE_HOSTS`. Both are
currently identical and both are exact-match, so there is no vulnerability — but
a drifted copy is the exact failure mode `dom_blocks` was created to end. Task 7
recorded this deliberately (touching orangetrack risks the golden gate for no
benefit). Fine to leave; worth folding into Phase 2 when orangetrack is next
opened.

### Informational 6 — two open-topped quantifiers, both safe, both cheap to close

`dom_blocks.py:110` `([A-Za-z0-9_-]{6,})` and `t_hunted_source.py:66` `\d+`.
Neither can backtrack catastrophically (single character classes, no nesting,
literal-anchored prefixes) and both were confirmed linear in the Threat 1 probe.
Recorded explicitly rather than by silence, as the task requires. An upper bound
(`{6,64}`, `{1,12}`) would be free, but is a tidiness preference, not a defect.

---

## Re-check of the "flag default ON" risk

**The risk calculation has NOT changed.** Walking the security reviewer's three
points against what Tasks 1–9 actually built: (1) *no pre-deploy E2E is
possible* — still true, and unchanged; the committed corpus (Decision 9) is a
real improvement in offline evidence but it is fixtures, not the live sites, so
it does not convert into the E2E gate the argument was about. (2) *Decision 3b
silences every failure mode* — still true and now broader in reach, since Task 6
routes `src` into nodes and Task 7 runs hostile HTML through the shared walker;
but observability improved in exactly the places that matter, because Task 8's
alignment WARNING names the article and both counts, and Task 6 logs every
dropped `src` and every cap application, so a bad publish now leaves a
machine-greppable trace in `docker logs` where before it left none. (3) *turning
the flag off needs a restart barred 10:00–20:00 МСК* — unchanged; the flag is
read once at import (`feature_flags.py:50`), so it still cannot deliver
same-day mitigation. Blast radius is narrower than when the decision was taken:
Phase 1 ships t-hunted only, and flag-off does **not** disable the four security
gates — verified in this audit, they hold in both flag states because
`dom_blocks` is deliberately ungated. **Nothing here argues for reopening the
decision, and this audit does not raise the default as a finding.** The one
thing the operator should know before deploy is a consequence, not a new
argument: because the switch cannot act inside the publishing window, the WARNING
lines above are the only same-day signal that something went wrong — so the
first post-deploy log read (Task 15) is doing the work the kill switch is
credited with.

---

## Note on the working tree during this audit

This audit changed **no file** other than this report. During the run, however,
the shared working tree was observed being mutated by a **concurrent agent**
doing mutation testing — first `dom_blocks.py:502` flipped
`headings_from_bold` `False` → `True`, later reverted, then `news_bot.py`
modified. The full-suite run I started landed inside one of those windows and
reported **2 failed, 1897 passed** — both failures
(`test_dom_blocks.py::TestHeadingHeuristic::test_final_punctuation_decides[period]`,
`test_t_hunted_source.py::TestHeadingHeuristic::test_negative_controls_stay_paragraphs[ends-with-period]`)
are **artifacts of that live mutation, not regressions**: re-running
`test_final_punctuation_decides` moments later gave **6 passed**. The audited
artefact is commit `4ad4592`, and the committed `dom_blocks.py:502` has
`headings_from_bold: bool = False` — Decision 2b intact. I deliberately did
**not** revert another agent's in-flight edits.

**Clean baseline, taken from a pristine `git archive 4ad4592` export in the
scratchpad so the shared tree was never touched: `1899 passed, 504 subtests,
0 failed` in 50 s** — exactly the figure Task 9 recorded. The suite at the
audited commit is green.

---

## What was NOT checked, and why

- **Code quality and cross-component consistency** — Task 10's perimeter. One
  overlap flagged: Informational 2 (text fidelity in the `<br>`-split path).
- **Test quality and coverage depth** — Task 12's perimeter. One overlap that
  belongs here too, because it is a security invariant whose test is green for
  the wrong reason: **Minor 1**, `test_publisher_policy_matches_preview_renderer`
  (`tests/test_telegraph_publisher.py:1344`). Otherwise all four named
  invariants **do** have tests pinning them —
  `test_dom_blocks.py:331` (host-before-regex), `:217` (`endswith`, not regex),
  `:363` (parse bound), `test_llm_common.py:121-181` (request bound, both
  dimensions, at-bound control, single WARNING),
  `test_preview_renderer.py:199-283,659` and
  `test_telegraph_publisher.py:1327` (scheme validation) — so no gate is left
  unpinned to vanish in a future refactor.
- **Full-suite green/red as an acceptance gate** — Task 13's perimeter. Recorded
  as context only, see the note above.
- **OWASP A01 / A02 / A07 / A10** — recorded as not applicable with the reason
  stated in the general pass, rather than omitted.
- **Phase 2 sources** (`lamley_source.py`, `autoevolution_source.py`) — read to
  confirm they are unchanged and that their two defects did not migrate into the
  shared module; not audited as shipping code, since they ship in Phase 2.
- **Images hosted on hostile servers serving huge files** — not our risk: the
  bytes are fetched by the Telegra.ph client and the reader's browser, never by
  the bot. Stated so it does not read as an omission.
