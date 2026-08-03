# Wave 3 Security Audit — publish-idempotency-fix

**Verdict:** **PASS**

**Status:** approved (zero critical, zero high, zero medium, zero low findings)

**Date:** 2026-05-07
**Auditor:** security-auditor (Task 7, Audit Wave)
**Scope:** Code state after Wave 1 (Tasks 1, 2) + Wave 2 (Tasks 3, 4, 5).

## Summary

Audit of the idempotency guard inserted at `news_bot.py:985–1020` (Task 1) and the `INSERT INTO published_articles` → `INSERT OR IGNORE INTO published_articles` change at `pending_articles_repo.py:582` (Task 2) finds **no security findings**. The guard inherits all existing redaction / instance-label / `parse_mode=None` invariants by routing every admin notification through `send_admin_notification` rather than building Telegram payloads directly. The repository change preserves parameterization (placeholder `?` count and bind tuple at lines 581–590 are byte-identical to the pre-change form except for the `OR IGNORE` keyword). No new untrusted input vector is introduced — guard input is limited to `link = row['link']` from internal `pending_articles` rows.

## Scope

Files audited (read-only):

- `news_bot.py` — guard block at lines 985–1020 (after Task 1, commit c1a8076)
- `news_bot.py` — `_redact_text` at lines 249–287 (existing, used by guard via `send_admin_notification`)
- `news_bot.py` — `send_admin_notification` at lines 357–392 (existing, called by guard)
- `news_bot.py` — `_TokenRedactingFilter` at lines 290–347 (existing, applies to all logger emits)
- `pending_articles_repo.py` — `move_to_published` at lines 559–607, with line 582 changed (after Task 2, commit a203e11)
- `pending_articles_repo.py` — `get_published` at lines 372–383 (called by guard)
- `pending_articles_repo.py` — `skip_pending` at lines 656–686 (called by guard cleanup)

## OWASP Top 10 (2021) Walk-through

| # | Category | Applicability | Result |
|---|---|---|---|
| A01 | Broken Access Control | not applicable | No new access boundaries. Guard runs inside the cron-only publish path; `pending_articles_repo` is only callable from in-process code. Telegram admin chat ID is the only "auth boundary," unchanged. |
| A02 | Cryptographic Failures | not applicable | No new credential handling, no new persistence of sensitive data. Telegraph URL is public-by-design; link is public news URL. |
| A03 | Injection (SQL) | applicable | **No issue.** See "Specific check 3" — `INSERT OR IGNORE` preserves `?` placeholders + bind tuple; `get_published` and `skip_pending` already use parameterized queries. |
| A04 | Insecure Design | applicable | **No issue.** See "Specific check 2" — no new untrusted input vector. Decision 8 (skip_pending failure → return True) is documented degraded mode, not a security bypass. |
| A05 | Security Misconfiguration | not applicable | No new config. Existing `INSTANCE_LABEL` prefix and `parse_mode=None` for admin chat sends are inherited via `send_admin_notification`. |
| A06 | Vulnerable Components | not applicable | No new dependencies. Wave 1 introduces zero new imports in either source file. |
| A07 | Identification & Auth Failures | not applicable | No auth logic touched. Telegram bot token / admin ID handling is unchanged. |
| A08 | Software & Data Integrity | applicable | **No issue.** No deserialization. `INSERT OR IGNORE` is a SQLite-native idempotency primitive, documented and in use elsewhere in the same file (line 593 for `processed_news`, line 673 for `skip_pending`). No CI/CD pipeline change. |
| A09 | Logging & Monitoring | applicable | **No issue.** See "Specific check 1" + "Specific check 4" — guard's `[idempotency-guard]` INFO/WARNING/ERROR logs all flow through the root-attached `_TokenRedactingFilter`; admin pings flow through `_redact_text` + `INSTANCE_LABEL` prefix; cleanup-failure ping uses `type(exc).__name__` only (Decision 12 invariant satisfied). |
| A10 | SSRF | not applicable | Guard makes zero outbound HTTP calls of its own. The Telegram API call inside `send_admin_notification` targets a hard-coded host (`api.telegram.org`) via `python-telegram-bot`; `link` is embedded in the message **body**, not in any URL the bot fetches. No URL parsing or fetching of `link` happens in the guard path. |

## Specific Checks

### Check 1: Admin-ping payload redaction

**Pass.**

Guard's two admin-ping payloads (`news_bot.py:997–1000` and `1015–1019`) are plain f-strings interpolating `link` (and `type(cleanup_err).__name__` in the second). They are passed to `send_admin_notification`, which:

1. Routes the message through `_redact_text` (`news_bot.py:368`) before any Telegram API call.
2. Prepends the `INSTANCE_LABEL` prefix (`news_bot.py:371–372`) so prod vs test pings are distinguishable.
3. Sends with `parse_mode=None` (`news_bot.py:383–386`) — Markdown injection via `[label](url)` syntax in `link` is impossible.

URL false-positive check against the five redaction regexes (`_BOT_TOKEN_RE`, `_OPENROUTER_KEY_RE`, `_ANTHROPIC_KEY_RE`, `_OPENAI_KEY_RE`, `_GEMINI_KEY_RE` at `news_bot.py:216–246`): a typical news URL such as `https://orangetrackdiecast.com/2026/05/02/hot-wheels-2026-car-culture-team-transport-k-case-report/` contains no digit-colon-secret shape, no `sk-`, no `sk-or-`, no `sk-ant-`, no `AIza` prefix. Verified by direct `re.search` on representative URLs from `autoevolution.com` and `orangetrackdiecast.com` — zero matches across all five patterns. URLs pass through unredacted (correct functional behavior, with no risk of unrelated-secret leak).

Cleanup-failure ping uses `type(cleanup_err).__name__` (e.g. `OperationalError`), **not** `str(cleanup_err)` — Decision 12 invariant is honored. Even if a future SQLite error message were to embed a credential-looking substring, `_redact_text` would scrub it before the Telegram payload is built.

### Check 2: Guard input source — internal-only

**Pass.**

Guard reads exactly one piece of data: `link = row['link']` at `news_bot.py:984`. `row` is a dict produced by `pending_articles_repo.list_pending()` (called from `news_bot.job()` slot loop at line 1747), populated by `add_pending` during RSS fetch. There is no path for an external user to inject into this code:

- Telegram users cannot post to the bot's pending queue (the bot has no `/push` command; subscribers are read-only on the channel).
- The only writers to `pending_articles` are the bot's own RSS fetcher and (out-of-scope) the operator-only `hw_review` CLI.
- The guard's `link` is the same `link` that already gets embedded in Telegraph titles and Telegram teasers prior to this fix — if a malicious-link XSS vector existed, it would already be exploitable through the existing publish path. The guard does not widen this surface.

Decision 8 (skip_pending failure → return True without striking the slot) is a documented degraded-mode design choice resolving the AC6 vs. AC8 user-spec tension. It does not bypass any security control: the operator still receives an admin ping; the next slot's guard activation retries cleanup; subscribers see no duplicate. Not a security finding.

### Check 3: SQL parameterization in `INSERT OR IGNORE`

**Pass.**

`pending_articles_repo.py:581–590`:

```python
conn.execute(
    "INSERT OR IGNORE INTO published_articles "
    "(link, title, ru_title, telegraph_url, telegraph_path, "
    " source_name, via_review) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)",
    (
        link, title, ru_title, telegraph_url, telegraph_path,
        source_name, 1 if via_review else 0,
    ),
)
```

- 7 named columns, 7 `?` placeholders, 7 bind values. Exact match.
- No f-string, no `%`, no `+` concatenation introducing user data into the SQL string.
- The `OR IGNORE` change is a SQLite **conflict-resolution clause**, parsed by SQLite as part of the SQL grammar, not a user-controlled value. CWE-89 (SQL injection) is not applicable.
- The two upstream queries used by the guard are parameterized as well: `get_published` at line 377 (`"SELECT * FROM published_articles WHERE link=?"`, `(link,)`) and `skip_pending`'s SELECT/INSERT/DELETE at lines 664–680 (all `?` placeholders with tuple binds).

### Check 4: No bypass of existing security controls

**Pass.**

The guard is implemented as a thin caller that delegates all secret-handling and Telegram-side concerns to existing helpers:

- **Token redaction in logs:** `_TokenRedactingFilter` is attached to the root logger (`news_bot.py:324`), to root handlers (`news_bot.py:333–334`), to the noisy HTTP logger family (`335–336`), and to LLM SDK logger families (`341–347`). Guard's `logger.info`, `logger.warning`, and `logger.error` calls (lines 993, 1003, 1010) all propagate through this filter chain — no log emission originates from a private/unfiltered logger.
- **Admin-ping redaction:** Guard's two `send_admin_notification` calls (lines 1001, 1015) route through `_redact_text` at line 368 of the helper. There is no direct `bot.send_message` call inside the guard.
- **`INSTANCE_LABEL` prefix:** Inherited automatically — `send_admin_notification` adds it at line 371–372 before sending. Both guard pings (skip-success and cleanup-failure) get the prefix.
- **`parse_mode=None`:** Inherited automatically — `send_admin_notification` calls `bot.send_message` with no `parse_mode` argument (line 383–386). Markdown/HTML interpretation of `link` is impossible; `link` is treated as plain text.

No new `bot.send_message`, no new `Bot(token=...)` instantiation, no new direct file/credential access, no new outbound HTTP. The guard is a strictly behavioral addition that reuses every existing security primitive.

## Findings

**No findings.**

| # | Severity | Category | Issue | Recommendation |
|---|---|---|---|---|
| — | — | — | None | — |

## Recommendations (informational, non-blocking)

These are observations that **do not** require changes for this feature; recording for future-feature awareness only:

1. *(Informational)* The guard emits `logger.error("[idempotency-guard] skip_pending failed for {link}: {cleanup_err!r} ...")` at line 1010, which uses `repr(cleanup_err)`. SQLite exception `__repr__` typically does not include credentials, but if a future `OperationalError` were to embed a connection string fragment, the redaction filter on root would still scrub recognized credential shapes. No action needed; flagged purely so future code-review of `Exception.__repr__` payloads stays alert.

2. *(Informational)* Decision 8's "skip_pending failure → return True" path will not increment `attempt_count`, so a persistently-broken `skip_pending` (e.g. stuck DB lock) could in theory keep firing the guard ping each slot. This is acknowledged in tech-spec Risk 4 ("recurring admin pings → operator desensitization") and in user-spec Risk 5; the operator is expected to triage after the first ping. Not a security issue — the alarm fires every time, which is the desired loud-failure mode.

## Verdict

**PASS.**

All OWASP-applicable points clear, all four specific checks (admin-ping payload redaction, guard input source, SQL parameterization, no bypass of existing controls) clear. No tasks need rework. Wave 3 audit may proceed to Task 8 (test audit) and ultimately Task 9 (pre-deploy QA).

No critical / high / medium / low findings.
