# Security Audit — feature `dedup-model-series`

- **Auditor:** security-auditor (Task 8, Audit Wave)
- **Date:** 2026-07-14
- **Branch:** dev
- **Scope:** Holistic OWASP-Top-10 whole-feature audit of the tiered
  series/theme pair-rule. UNTRUSTED input = RSS feed titles/bodies + LLM
  transcreation output. End-to-end flow traced:
  `extract_fingerprint` (regex over title/body) → `{strict,brands,series,pairs}`
  JSON → `insert_pending`/`move_to_published` (`?` placeholder, opaque blob) →
  30-day candidate SELECT → pair compare in `_check_cross_source_dedup` →
  matched tokens rendered into `[E015]`/`[E014]` admin pings →
  `send_admin_notification` (plain-text, redacted).
- **Files reviewed:** `model_extractor.py`, `admin_alerts.py`, `news_bot.py`
  (gate + `send_admin_notification` + `_redact_text`), `backfill_fingerprints.py`,
  `pending_articles_repo.py`, `tests/test_pending_articles_repo.py::TestSqlAudit`.
- **Method:** analysis only — no source changed. Empirical probes run to
  substantiate the ReDoS, charset-integrity, SQL-literal, and tier-spoofing
  claims (evidence inline below).

---

## Verdict: PASS-WITH-NOTES

All four targeted threats are closed as verified invariants. The general OWASP
pass is clean. **Zero Critical, zero Major, zero Minor findings.** Two
non-blocking informational observations are recorded (both pre-existing and/or
by-design) plus the coverage note the task asked to surface. Nothing to
fix-before-deploy.

## Summary

| Severity | Count |
|----------|-------|
| Critical | 0 |
| Major    | 0 |
| Minor    | 0 |
| Informational (non-blocking) | 2 |

The feature adds no new external dependency and no new top-level module — it
extends three already-deployed modules (`model_extractor.py`, `admin_alerts.py`,
`backfill_fingerprints.py`), all three already present in `deploy.sh` and both
`deploy.yml` / `deploy_test.yml` FILES arrays. All new SQL is parameterized,
all new regexes are bounded, the admin-ping surface is plain-text + redacted,
and the untrusted tokens are charset-restricted to a `|`-free, newline-free
space that cannot corrupt the `<model>|<series>|<tier>` pair key or forge a
tier. Degraded mode fails open to publish while still logging a full traceback
and firing a rate-limited `[E016]` — it does not silently swallow a
security-relevant failure.

---

## Closed target threats (verified invariants)

### Threat 1 — ReDoS in new series/tier regexes — CONFIRMED SAFE

The two new compiled passes are built to mirror the `_MODEL_AFTER_BRAND_RE`
discipline documented in the module header (`model_extractor.py:191-233`):

- `_alias_to_pattern` (`model_extractor.py:317-324`) relaxes literal spaces to
  the **bounded** `\s{1,3}` — never `\s+`/`\s*` — and `re.escape`-s every other
  character. So `_SERIES_RE` (`:333-336`) is an alternation of escaped literals
  joined by `\s{1,3}`, longest-first (prefix-safe). No unbounded quantifier, no
  nested unbounded group, no back-reference.
- `_SERIES_ACRONYM_RE` (`:340`) is a plain fixed-string alternation
  (`SDCC|RLC|STH|ZAMAC|Zamac`), case-sensitive — no quantifier at all.
- The normalization substitutions `re.sub(r'\s{1,3}', ' ', ...)` in
  `_canonical_brand` (`:369`) and `_canonical_series` (`:383`) are likewise
  bounded.
- `_MODEL_EXTRA_KEEP_RE` (`:248`) and `_MODEL_CONNECTOR_STOPWORDS` (a
  `frozenset`, not a regex) add no backtracking surface.

**Evidence (empirical):** every new/adjacent regex against 50 KB adversarial
inputs (`'a'*50000`, `' '*50000`, `'san diego comic-con '*3000`,
`'stranger things'+' '*40000`, `'sdcc '*10000`, etc.):

```
_SERIES_RE               worst=2.68ms
_SERIES_ACRONYM_RE       worst=0.99ms
_MODEL_AFTER_BRAND_RE    worst=5.99ms
_MODEL_EXTRA_KEEP_RE     worst=0.63ms
```

Linear time; no catastrophic backtracking. **Invariant holds.** (CWE-1333 N/A.)

### Threat 2 — SQL safety of `json_extract('$.pairs')` in backfill — CONFIRMED SAFE

`backfill_fingerprints.py:296-304` is the only `execute()` in the module (AST
walk confirmed). The SQL body is entirely static string literals;
`json_extract(model_fingerprint, '$.pairs')` and the `CASE WHEN
json_valid(...)` guard are static SQL literals with **no** f-string / `%` /
`+` interpolation. The single bound value is `(f"-{int(args.days)}",)` via a
`?` placeholder — and `args.days` is `int()`-cast here **and** already
range-validated `[1,90]` (rejecting non-ints) by the `_days_in_range`
argparse `type=` callable (`:111-123`), so there is no injection surface even
inside that f-string (it interpolates an int, not a string).

Untrusted series/model tokens **never reach the SQL body** — they live inside
the opaque `model_fingerprint` JSON blob, written via `?` placeholders in
`update_published_fingerprint` (`pending_articles_repo.py:891-894`,
`_dumps(fingerprint)` bound as `?`) and in `insert_pending` /
`move_to_published`. The 30-day gate SELECTs
(`list_recent_{pending,published}_fingerprints`, `:850-877`) bind `days` via
`?` too.

**Evidence:** ran the exact `TestSqlAudit` forbidden-pattern set against
`backfill_fingerprints.py` by hand → `forbidden SQL-interpolation hits: NONE`.

**Coverage note (as the task requires):** `TestSqlAudit`
(`tests/test_pending_articles_repo.py:1290-1321`) hard-codes and scans **only**
`pending_articles_repo.py` — it does **not** cover `backfill_fingerprints.py`.
`python3 -m pytest -k SqlAudit` → `1 passed`, green for the repo module.
Backfill's `json_extract` was therefore verified by hand (above). **Invariant
holds.** (CWE-89 N/A.)

### Threat 3 — untrusted tokens in `[E015]`/`[E014]` pings — CONFIRMED SAFE

The send path is plain-text and secret-scrubbed:
`send_admin_notification` (`news_bot.py:462-497`) calls
`bot.send_message(chat_id=..., text=safe_message)` with **no `parse_mode`
argument** → PTB default `parse_mode=None` (plain text). The dedicated comment
(`:487-493`) records the 2026-04-30 decision: Markdown was rejected both for
`can't parse entities` breakage AND to remove the spoofing risk where
article-derived text could rewrite the visible message via crafted
`[label](url)` syntax. `safe_message = _redact_text(message)` (`:479`) scrubs
bot-token / Anthropic / OpenAI / OpenRouter / Gemini key shapes before the
payload is built.

The builders do **not** re-introduce markup:
- `admin_alerts.py` imports only `typing`; it contains **no** `Bot`, **no**
  `send_message`, and **no** `parse_mode` anywhere. The builders
  (`alert_cross_source_blocked` `:508-538`, `alert_cross_source_dupe`
  `:454-501`) are pure `str` builders.
- `_render_pair` / `_render_pairs_block` (`:427-448`) only `.split("|")`, drop
  the tier tag, drop the `*` sentinel, and `" + ".join(...)` — plain text, no
  Markdown/HTML construction around the untrusted series/model tokens or links.

The gate (`news_bot.py:2313-2352`) passes `pairs=match.get('pairs')` and the
existing/new links into these builders; because the final send is plain-text,
a crafted feed title, series token, or article URL cannot inject Telegram
entities. **Invariant holds.** (CWE-79/CWE-150 N/A.)

### Threat 4 — lexicon canonical charset integrity — CONFIRMED SAFE

`model_extractor.py:162-165` is a load-time `assert` (fires at import, like the
compiled-regex constants) that **every** canonical in `SERIES_LEXICON.values()`
contains no `|` and no `\n`. It iterates the full `.values()` set (all 12
canonicals — verified), not a subset, so the `<model>|<series>|<tier>` pair-key
separator can never be forged from the series side. A companion tier-consistency
assertion (`:172-179`) additionally guarantees every alias of one canonical
shares one tier.

**Evidence (empirical):**
- All 12 canonicals are pipe/newline-free; injecting `('bad|series',
  'distinctive')` makes the assertion fire → the import would crash, not ship a
  broken key.
- Theme-only sentinel is **always** `|B`: `Stranger Things` with no model →
  `['*|stranger things|B']`; no theme-only key can ever end `|D`
  (`_build_pairs` `:423-425` hard-codes `"*|{canonical}|B"`).
- A smuggle attempt — title `"Toyota Supra|D fake stranger things"` with a
  newline inside a paragraph — produced `['toyota supra|stranger things|D']`:
  the literal `|D`/newline in feed text was stripped because the model-token
  charset is `[A-Za-z0-9][A-Za-z0-9\-]{1,24}` (no `|`, no newline). Every
  emitted key has **exactly two** pipes (shape intact).

**Invariant holds.** (CWE-74 separator-injection N/A.)

---

## General OWASP-Top-10 pass (2021)

- **A01 Broken Access Control** — N/A. No new authz surface. Admin pings target
  the fixed `TELEGRAM_ADMIN_ID`; no user-supplied routing.
- **A02 Cryptographic Failures** — N/A. No crypto added. Secrets stay in env,
  redacted from both logs (`_TokenRedactingFilter`, `news_bot.py:384-441`) and
  admin pings (`_redact_text`).
- **A03 Injection** — SQL parameterized (Threat 2); ReDoS bounded (Threat 1);
  markup/entity injection blocked by plain-text pings (Threat 3); no OS
  command / LDAP surface introduced.
- **A04 Insecure Design** — Strong. Fail-safe polarity (untagged →
  `_SERIES_DEFAULT_TIER='broad'`, `:157,186-188`); theme-only always broad;
  distinctive `|D` requires curated lexicon tag AND a concrete model; runtime
  kill-switch `DEDUP_SERIES_ENABLED` (`:130-132`, default-on, off-words only);
  degraded mode fails open to publish.
- **A05 Security Misconfiguration** — Toggle default documented; no default
  credentials; no new headers/CORS surface (Telegram bot, not a web server).
- **A06 Vulnerable Components** — No new dependency. Feature files import only
  stdlib (`re`, `typing`, `argparse`, `json`, `logging`, `sqlite3`, `sys`,
  `time`) + already-deployed first-party modules. No new first-party import is
  pulled into `news_bot` without a FILES-array entry (verified against
  `deploy.sh` + both workflow FILES arrays).
- **A07 Identification & Auth Failures** — N/A (no auth flow).
- **A08 Software & Data Integrity** — JSON only, no pickle / `yaml.load` /
  `eval`. `model_fingerprint` blobs are deserialized with `json.loads`;
  `_already_backfilled` (`backfill_fingerprints.py:157-177`) wraps it in
  try/except and treats non-dict / corrupt blobs as "reprocess"; the gate and
  `shares_pair` guard every candidate with `isinstance(..., dict)` +
  `.get('pairs') or []`, so a malformed historical row is skipped silently, not
  a crash or a mis-verdict. No insecure deserialization.
- **A09 Security Logging & Monitoring** — Degraded mode
  (`news_bot.py:2356-2390`) logs a full traceback via `logger.exception` AND
  fires a rate-limited `[E016]` ping (`type(exc).__name__` only, never
  `str(exc)`) — it does **not** silently swallow a security-relevant failure;
  the attack/fault stays observable. Block/flag verdicts log INFO. Fail-open to
  publish is by design (user-spec AC9 / Decision 12).
- **A10 SSRF** — No new user-controlled URL fetch. Backfill's
  `news_bot.fetch_full_article(entry_stub)` (`:221`) fetches
  `row['link']` drawn from our own `published_articles` table (links already
  fetched + published previously, host-allowlisted at ingestion). No feed-time
  URL from this feature reaches a server-side request. Not a new SSRF surface.

### Tier / sentinel spoofing edge cases (explicitly checked)

- **`"*|<series>|B"` sentinel** — `*` is a literal plain-text placeholder, not a
  glob; it never touches SQL (rides inside the opaque blob) and renders in the
  ping as the bare series (`_render_pair` drops it). It cannot become `|D`.
- **`|D`-vs-`|B` selection** — the tier is computed from the LOCAL lexicon at
  extraction time on both sides and stored; a candidate cannot present an
  attacker-chosen tier because the composing tokens are `|`-free and the suffix
  is code-generated (`_tier_suffix`, `:186-188`). `endswith('|D')` fires only
  for a genuinely tiered-distinctive key (a model ending in "D", e.g.
  `ford gt-d|car culture|B`, still ends `|B`). A crafted candidate can neither
  forge a `|D` to cause a false silent hard-block, nor downgrade a real `|D` to
  bypass one — the only "bypass" available is to word an article so no model is
  recognized, which fails safe toward publish (the intended polarity).

---

## Informational / non-blocking observations

These are **not** findings and require **no** action — recorded for the lead.

1. **`TestSqlAudit` scope gap (documented, expected).** The SQL-parameterization
   guard test scans only `pending_articles_repo.py`; `backfill_fingerprints.py`
   is outside its net. This matches tech-spec AC and was audited by hand here
   (Threat 2). Optional hardening if desired: parametrize `TestSqlAudit` over a
   file list including backfill so the guarantee is regression-pinned rather
   than re-verified manually each feature. Purely optional.

2. **Rate-limit pair key uses a `\n` separator over link values**
   (`pending_articles_repo.py:921-928`,
   `f"{prefix}{new_link}\n{existing_link}"`). Pre-existing from the shipped
   cross-source-dedup feature, not introduced here. Links are host-allowlisted
   at ingestion and a URL cannot contain a raw newline; worst theoretical case
   is a mis-scoped soft-flag rate-limit (an observability nuance), never SQL
   injection (bound via `?`) nor ping markup injection (plain-text send). No
   action needed; noted for completeness of the trust-boundary trace.

---

## Acceptance-criteria coverage

- [x] Report written with explicit verdict + severity-classified findings.
- [x] Threat 1 (ReDoS) closed — all new series/tier quantifiers bounded;
      empirical linear-time evidence recorded.
- [x] Threat 2 (SQL) closed — `json_extract` static literal, `days`
      parameterized + int-cast, untrusted tokens confined to the opaque blob;
      `TestSqlAudit` backfill gap noted and hand-verified.
- [x] Threat 3 (pings) closed — `[E015]`/`[E014]` plain-text (`parse_mode=None`),
      builders introduce no markup around untrusted tokens, `_redact_text` in
      place.
- [x] Threat 4 (charset) closed — load-time assertion present and complete
      (all 12 canonicals), empirically shown to fire.
- [x] OWASP flow trace (untrusted input → JSON → DB → ping) done; degraded mode
      does not hide attacks.
- [x] Every closed invariant recorded explicitly; each observation carries
      file:line + rationale.
