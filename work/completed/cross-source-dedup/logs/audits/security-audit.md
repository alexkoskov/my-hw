# Security Audit — cross-source-dedup (Task 7)

**Date:** 2026-06-06
**Auditor:** security-auditor (read-only audit; no source files modified)
**Methodology:** OWASP Top 10 (2021), security-auditor skill. Empirical verification via `python3 -c` for ReDoS, SQL injection, SSRF, and the token-redaction import-order invariant.

---

## Summary

**Verdict: PASS** (0 HIGH, 0 MED, 2 LOW/informational).

All 5 audit focuses were exercised against the live code and empirically validated. The feature introduces no SQL injection, no ReDoS, no SSRF channel, no secret leakage in admin pings, and no Telegram rendering-injection vector. The critical security-review caveat 5 (import `news_bot` before `logging.basicConfig()` in `backfill_fingerprints.py`) is satisfied and was verified by runtime inspection of the root logger's filter chain. No Tasks (1-5) require redo.

---

## Scope

**Code files audited (read-only):**
- `model_extractor.py` (Task 1) — regex ReDoS, lexicon, extractor robustness.
- `backfill_fingerprints.py` (Task 5) — SSRF surface via `entry_stub`, import-order invariant.
- `pending_articles_repo.py` (Task 2) — SQL injection across 7 new helpers, bot_state key construction.
- `admin_alerts.py` (Task 3) — E014/E015/E016 secret leakage, rendering injection.
- `news_bot.py` (Task 4) — `_check_cross_source_dedup`, degraded-mode handler, ping assembly, `fetch_full_article` dispatch, `_TokenRedactingFilter` install, `send_admin_notification`.

**Decisions covered:** 3 (ReDoS), 6 (bot_state keys), 7 (alert codes), 8 (mark_processed), 9 (no source filter), 10 (backfill fetch), 11 (migration), 12 (degraded mode). Tech-spec AC "security-review caveat 5".

**OWASP Top 10 categories applicable & checked:** A03 Injection (SQL + ReDoS + rendering), A04 Insecure Design (SSRF surface of backfill), A05 Security Misconfiguration (deploy.sh FILES list), A09 Logging Failures (token redaction), A10 SSRF. A01/A02/A06/A07/A08 — see Out of Scope.

---

## Findings

### Focus 1 — SSRF surface of `backfill_fingerprints.py` → `fetch_full_article`

**No findings (PASS).**

`backfill_fingerprints.py:167-176` builds `entry_stub` with `link` sourced from `published_articles.link` — rows the bot itself persisted from already-trusted upstream sources. The link is passed to `news_bot.fetch_full_article` (`backfill_fingerprints.py:183`).

`fetch_full_article` (`news_bot.py:1545-1586`) dispatches on `urlparse(link).netloc.lower()` against a **fixed domain allowlist** (`orangetrackdiecast.com`, `corporate.mattel.com`, `lamleygroup.com`, `autoevolution.com`, `blogspot.com`). Any other netloc falls through to `logger.warning("No source handler for domain: ...")` and returns `None` — **no network request is issued**.

Empirically verified — every adversarial scheme/host returned `None` with no fetch:
- `file:///etc/passwd` → None
- `http://169.254.169.254/latest/meta-data/` (cloud metadata) → None
- `http://localhost:6379/` (Redis) → None
- `gopher://evil`, `ftp://internal/`, `http://192.168.1.1/admin` → None

The backfill `entry_stub` shape (`link`, `source_name`, `title`, `published`) matches `fetch_full_article`'s expectations (it only reads `entry.get('link')` for dispatch). The stub opens **no additional SSRF channel** beyond the inherited allowlist defense. Even a tampered DB row cannot redirect the fetch to an internal host. A04/A10: PASS.

### Focus 2 — ReDoS verification of every regex in `model_extractor.py`

**No findings (PASS).**

Three module-level compiled patterns reviewed for unbounded/nested greedy quantifiers and tested empirically against 10KB adversarial inputs:

- `_MODEL_AFTER_BRAND_RE` (`model_extractor.py:115-132`) — all quantifiers bounded: year `\d{2}`, separators `\s{1,3}`/`[\s\-]{1,3}`/`\s{1,2}`, model body `[A-Za-z0-9\-]{1,24}`, composite extra `{0,2}`. The nested group `(?:\s{1,2}[A-Za-z0-9][A-Za-z0-9\-]{0,24}){0,2}` is bounded on both the inner char class and the outer repetition — no catastrophic backtracking shape. Anchored with `\b`.
- `_MODEL_EXTRA_KEEP_RE` (`model_extractor.py:147`) — fully anchored `^...$`, alternation of bounded char classes; no nested unbounded repetition.
- `_UPPERCASE_BRANDS_RE` (`model_extractor.py:165`) — trivial bounded alternation `\b(AMC|BMW|Lotus)\b`.

Empirical timing (all three patterns + full `extract_fingerprint`):

| Input | Length | All patterns |
|-------|--------|-------------|
| `A`*10000 | 10000 | 0.16 ms |
| `Toyota X `*1000 | 9000 | 1.44 ms |
| `2018 Toyota `*1000 | 12000 | 0.42 ms |
| `Toyota` + 10000 spaces + `X` | 10007 | 0.11 ms |
| `Toyota ` + `A-`*5000 (hyphen spam) | 10007 | 1.87 ms |
| `A`*5000 + `Toyota Supra GT `*500 | 13000 | 0.54 ms |
| `Land  Rover `*1000 | 12000 | 0.40 ms |
| `extract_fingerprint` on 10KB adversarial | — | 0.74 ms |

Worst case 1.87 ms — three orders of magnitude under the tech-spec's <10 ms budget and the task's 100 ms ceiling. No catastrophic backtracking. Decision 3 ("bounded quantifiers, anchored, ReDoS-safe") holds. A03: PASS.

### Focus 3 — SQL injection across all 7 new helpers in `pending_articles_repo.py`

**No findings (PASS).**

All 7 new helpers use parametrized `?` placeholders exclusively; no f-string / `%` / string-concat / `.format()` reaches a SQL body. Verified by `grep -n "execute" pending_articles_repo.py` cross-checked against an f-string/concat filter (zero matches in execute lines):

- `list_recent_pending_fingerprints` (`:850-855`) — `datetime('now', ? || ' days')`, value `(f"-{int(days)}",)`. The f-string builds the **bound parameter value** (after `int()` coercion), not the SQL text. Safe.
- `list_recent_published_fingerprints` (`:869-874`) — same pattern. Safe.
- `update_published_fingerprint` (`:891-894`) — `UPDATE ... SET model_fingerprint=? WHERE link=?`, `(_dumps(fingerprint), link)`. Safe.
- `is_pair_rate_limited` (`:942-945`) — `SELECT value FROM bot_state WHERE key=?`. Safe.
- `mark_pair_pinged` (`:970-973`) — `INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)`. Safe.
- `is_dedup_degraded_rate_limited` (`:983-986`) — parametrized `key=?`. Safe.
- `mark_dedup_degraded_pinged` (`:1004-1007`) — parametrized VALUES. Safe.

Compound key `softflag_pair:{new_link}\n{existing_link}` (`_pair_key`, `:921-928`) is built into the **bound parameter** (a VALUE), never into SQL — newline is a value-shape concern, not an injection vector. Backfill's own SELECT (`backfill_fingerprints.py:243-249`) also parametrizes `days` via `?`.

Empirically verified: `update_published_fingerprint(conn, "'; DROP TABLE published_articles; --", {...})` left the table intact (rows unchanged, no DDL executed); evil link in rate-limit helpers stored as a single literal bot_state row. A03: PASS.

### Focus 4 — Secret leakage in admin-ping builders E014/E015/E016

**No findings (PASS).**

- `alert_cross_source_dupe` / E014 (`admin_alerts.py:379-407`) — interpolates only operator-trusted args (links, source names, percentages, model list). No env vars, no exception strings.
- `alert_cross_source_blocked` / E015 (`admin_alerts.py:413-424`) — links + percentage only.
- `alert_dedup_degraded` / E016 (`admin_alerts.py:430-446`) — interpolates only `reason`, which the call site passes as `type(exc).__name__` (`news_bot.py:2024`), **never** `str(exc)`. This matches the established E006 convention and prevents secrets/paths from a network-error string leaking into the ping.

Defense-in-depth confirmed: every admin ping is routed through `send_admin_notification` (`news_bot.py:385-420`), which scrubs the message via `_redact_text` (`:396`) before building the Telegram payload. `_redact_text` (`:277-315`) strips Telegram-bot-token, Anthropic, OpenAI, OpenRouter, and Gemini key shapes. So even an accidental future secret in a ping body would be redacted at the delivery boundary.

`_TokenRedactingFilter` install verified intact after the Task-4/Task-5 changes (`news_bot.py:347-375`) — attached to the root logger, every current root handler, the noisy HTTP loggers, and the LLM SDK loggers, unconditionally at module load. No new ping path bypasses it. A09: PASS.

### Focus 5 — Rendering injection via link content in pings

**No findings (PASS).**

`new_link` / `existing_link` flow into E014/E015 builders and out via `send_admin_notification`, which sends with **`parse_mode=None` (plain text)** (`news_bot.py:404-414`, with an explicit comment that Markdown was rejected on 2026-04-30 precisely to remove the spoofing risk where article-derived `[label](url)` could rewrite the visible message). With no parse mode, Telegram performs no entity parsing — `<script>`, `*bold*`, `[x](y)`, and control-character payloads in a link render as literal text. No HTML/MarkdownV2 escaping is required because no markup is interpreted. A03 (XSS/rendering): PASS.

### Informational (LOW) — non-blocking

- **INFO-1** (`news_bot.py:1945`, `:2016`) — the dedup gate reaches into `pending_repo._connect()` (a private API). Not a security defect (same-process trusted call, no injection surface), but it couples the gate to repo internals. Already noted by the Task-6 code audit as a follow-up ticket; restated here only for completeness. **Severity: LOW / informational. No fix required for security sign-off.**
- **INFO-2** (`backfill_fingerprints.py:43-58, 67-72`) — the module docstring/comment correctly states the redaction filter is installed unconditionally on import, so the import-order convention is currently belt-and-suspenders rather than load-bearing. This is accurate and the convention is the right defensive posture (future-proofs against `news_bot` moving filter install under a condition). **No action needed.** Recorded so a future maintainer does not "simplify" the import order away.

---

## Verified invariants

1. **Import-order / caveat 5 — VERIFIED.** `backfill_fingerprints.py` imports `news_bot` (`:72`) at module top, before `logging.basicConfig()` runs inside `main()` (`:219`). Runtime check confirmed: after `import news_bot` and **before** any `basicConfig`, `_TokenRedactingFilter` is present on the root logger (`logging.getLogger().filters`) AND on its single root handler. After `basicConfig()` (which reuses the existing root handler), the filter remains on both the logger and the handler. Conclusion: token redaction is active for any log line emitted during backfill fetch errors (e.g. `logger.error("backfill failed for %s: %s", link, exc)` at `:185`) → no token leak to stdout/journalctl. Caveat 5 holds.
2. **ReDoS-safe regex — VERIFIED** empirically (Focus 2): worst case 1.87 ms on 10KB adversarial input across all three patterns.
3. **Parametrized SQL — VERIFIED** for all 7 helpers + backfill SELECT (Focus 3): zero non-parametrized SQL; injection attempt had no effect.
4. **SSRF allowlist — VERIFIED** (Focus 1): all non-allowlisted schemes/hosts return None with no fetch.
5. **No `str(exc)` in degraded-mode ping — VERIFIED**: `news_bot.py:2024` passes `type(exc).__name__`; the broad handler (`:2008`) logs via `logger.exception` (which is redaction-filtered).
6. **Plain-text Telegram delivery — VERIFIED**: `parse_mode=None` (`news_bot.py:404-414`).
7. **Token redaction at delivery boundary — VERIFIED**: `send_admin_notification` runs `_redact_text` on every message (`:396`).
8. **A05 deploy config — VERIFIED**: both new top-level files (`model_extractor.py`, `backfill_fingerprints.py`) are present in the `deploy.sh` FILES list (`deploy.sh:56-57`) — no first-tick ImportError.
9. **No hardcoded secrets — VERIFIED**: secret-pattern scan over all 4 audited non-news_bot files returned zero matches.

---

## Out of scope

- **OWASP A01 Broken Access Control** — no user-facing surface; backfill is operator-only CLI; pings go to a fixed admin chat. N/A.
- **OWASP A02 Cryptographic Failures** — feature introduces no cryptography. N/A.
- **OWASP A06 Vulnerable Components** — no new dependencies (`requirements.txt` unchanged; stdlib `re`/`json`/`datetime`/`sqlite3` only per tech-spec). Existing dependency CVE posture is unchanged by this feature and out of scope for a feature audit.
- **OWASP A07/A08** — no auth/session and no deserialization of untrusted input introduced (JSON read from the bot's own SQLite, not external). N/A.
- Unmodified pre-existing modules (`boilerplate_filter.py`, `outage_state.py`, `t_hunted_source.py`, `autoevolution_source.py`, the internals of the per-source fetchers behind the allowlist) — not changed by Tasks 1-5; not audited here.
- Test files — covered by Task 8 (Test Audit).
- General code quality / architecture — covered by Task 6 (Code Audit, verdict PASS-WITH-NOTES).

---

## Verdict

**PASS.**

Zero HIGH and zero MED findings. Two LOW/informational notes (private-API coupling, belt-and-suspanders import order) — neither blocks deploy and neither requires a fix for security sign-off. The critical import-order invariant (caveat 5) is explicitly verified and recorded. No Tasks (1-5) require redo. The feature is cleared from a security standpoint to proceed to Task 9 (Pre-deploy QA).
