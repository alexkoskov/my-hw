# Decisions Log: llm-402-outage

Standalone fix (no user-spec / tech-spec) — plan item 1.1 of `work/PLAN-2026-08-03.md`,
delivered via `/write-code` per the S-size shortcut rule.

---

## Task standalone: 402 «Insufficient credits» must hold the article, not kill it

**Status:** Done
**Commit:** _(pending — not yet committed)_
**Agent:** main agent

**Summary:** The 2026-07-14 loss of two articles was a classification bug, not a
billing accident: no SDK gives HTTP 402 a dedicated exception class, so it fell
into the bare `APIStatusError` catch-all → per-article → 3 strikes →
`failed_articles`, while 401 and 403 (the same class of global access problem)
were already held. Added the shared `_llm_common._ACCOUNT_LEVEL_STATUS_CODES`
(402/407/408) plus an `_is_account_level_status` helper, and routed those codes
to `ClaudeOutageError` in all four engines.

**Deviations:** Scope grew twice, both times because a reviewer found the same
defect class elsewhere and the fix was small:

1. The task named only `openrouter_transcreation`. `openai` and `claude` carry a
   byte-identical catch-all, and `architecture.md` states the failure model is
   "identical whichever engine is selected" — fixing one engine would have made
   that claim false. All three now share one set.
2. `gemini` needed the opposite change. google-genai raises one `ClientError`
   for every 4xx, and its unknown-code default was **outage** — so 413/451 there
   would have pinned a row to the queue head, exactly the failure the other
   engines' conservative default exists to prevent. Its default was flipped to
   per-article and its outage codes listed explicitly.

Two further changes came out of review and are not in the original plan item:
the `[hold]` log line in `news_bot.py` now carries the cause (it is the only
record of why an article was held), and `sanitize_error_message` now truncates
at 500 chars.

---

### Decisions

**D1. The unknown-status default stays per-article — the catch-all was NOT flipped.**

The plan asked whether an unknown code "is more likely global than
article-specific". Measured rather than assumed: on `openai 2.32.0`, the bare
`APIStatusError` catch-all receives 402, 405, 407, 408, 413, 451 among others —
and 413 (payload too large) and 451 (legal) genuinely are about one article.

The two failure modes are not symmetric. A wrong per-article call costs **one
article** after 3 strikes and the queue keeps moving. A wrong outage call costs
**the whole channel**: `news_bot.job()` re-reads `pending_repo.list_pending()[0]`
at every slot, so a permanently-failing row at the head is retried forever and
never struck out — and `outage_state._compute_next_state` stops emitting pings
once `ping_count >= 3`, so the stall goes quiet after two hours.

So the set is opt-in and explicit. Extend it; do not flip the default.

**D2. 407 and 408 were included alongside 402.**

Not speculative — each is the twin of a code already classified as an outage.
407 (proxy auth) is the transport sibling of 401/403; 408 (server-side timeout)
is the server-side twin of `APITimeoutError`. Prod egress runs through a non-RU
VPN, which makes 407 a plausible topology, not a hypothetical.

**D3. Gemini restates the rule by number, and must not use the shared helper.**

A real `google.genai.errors.ClientError` exposes `.code` and has **no**
`.status_code` (verified on google-genai 1.73.1). `_is_account_level_status`
reads `.status_code`, so calling it there would silently return False for every
code. `gemini_transcreation._CLIENT_OUTAGE_CODES` therefore lists
`{401,403,404,409,429,499} | _ACCOUNT_LEVEL_STATUS_CODES` by hand. `499
cancelled` is Gemini-only (documented at ai.google.dev/gemini-api/docs/api-errors)
and is its `APITimeoutError` equivalent; `409` is included because
openai/openrouter hold it as `ConflictError`.

**D4. The `[hold]` log line carries the cause, and that is a contract.**

A held row never reaches `increment_attempt` (no `last_error`) and never enters
the `[E034]` recap — both are `failed`-branch only. Without the cause in the log
line, an empty balance and a dead network are indistinguishable, and
`[E010]/[E011]/[E012]` are generic «LLM недоступна». That exact ambiguity already
produced one wrong diagnosis on 2026-06-10, when every external check was green
and the real cause was server-side DNS loss.

**D5. `sanitize_error_message` truncates at 500 characters.**

An SDK error message is the upstream response body verbatim when it isn't JSON
(`openai/_base_client.py`: `err_msg = err_text or ...`). A captive portal or
intercepting proxy answering 4xx with a multi-KB HTML page would land whole in
the journal — and on the hold path that repeats every slot, against a 10 MB × 3
docker log cap. 500 chars keeps a full gateway error body (measured ~220 chars)
intact. Truncation runs **after** redaction: cutting first could split a secret
and leave an unredacted prefix in the tail.

---

**Reviews:**

*Round 1:*
- code-reviewer: 3 major, 3 minor → [code-reviewer-1.json](logs/working/task-standalone/code-reviewer-1.json)
- security-auditor: approved, 2 major, 3 minor → [security-auditor-1.json](logs/working/task-standalone/security-auditor-1.json)
- test-reviewer: 2 major, 4 minor → [test-reviewer-1.json](logs/working/task-standalone/test-reviewer-1.json)

*Round 2 (after fixes):*
- code-reviewer: approved with suggestions; all 6 round-1 findings resolved; 1 new major (gemini 499/409) → [code-reviewer-2.json](logs/working/task-standalone/code-reviewer-2.json)
- security-auditor: approved, leak not re-opened; 1 new finding (no truncation) → [security-auditor-2.json](logs/working/task-standalone/security-auditor-2.json)
- test-reviewer: all round-1 findings verified resolved by mutation; 1 new major (gemini test double) → [test-reviewer-2.json](logs/working/task-standalone/test-reviewer-2.json)

Every round-2 finding above was applied. Findings deliberately NOT applied are
listed under "Left open" below.

**Verification:**

- `python3 -m pytest -q` → **1646 passed, 460 subtests** (baseline 1626 + 20 new)
- `pre-commit` on all changed files → all hooks pass, gitleaks clean
- Mutation control — every new rule is pinned by a test that fails without it:

  | Mutation | Result |
  |---|---|
  | `_ACCOUNT_LEVEL_STATUS_CODES` → `frozenset()` | 10 fail |
  | drop 402 from the set | 5 fail |
  | blanket `return ClaudeOutageError(...)` (no code check) | 6 fail |
  | gemini: restore the old outage-by-default mapping | 4 fail |
  | gemini: drop 499 / drop 409 | 1 fail each |
  | gemini: read `.status_code` instead of `.code` | 9 fail |
  | openai: bypass the classifier | 3 fail |
  | `[hold]` line drops the cause | 1 fail |
  | `[hold]` line skips `sanitize_error_message` | 1 fail |
  | `sanitize_error_message` drops truncation | 1 fail |

- Not verified on production. This changes behaviour only on an error path that
  requires a real 402/407/408 from the gateway; there is no safe way to trigger
  it without emptying the balance. The next genuine low-balance event is the
  test — `[E019]` fires first, and the journal will now name the cause.

---

### Left open (deliberately not fixed here)

Each is a design-level issue surfaced by review, larger than this fix and
recorded in `work/PLAN-2026-08-03.md`:

1. **The hold is unbounded and eventually silent.** A row that fails
   permanently at the queue head is retried forever, and pings stop after
   `ping_count >= 3`. This is pre-existing hold-and-wait behaviour for *every*
   outage code, not something 402 introduced — but adding 402 raises the stakes,
   because OpenRouter's 402 is per-*request* ("requires more credits, or fewer
   max_tokens"), so a long round-up can 402 while shorter articles behind it
   would have succeeded.
2. **Proxy credentials are not in the redaction vocabulary.** `HTTPS_PROXY=http://user:pass@host`
   matches none of the five key regexes and is absent from `_SECRET_ENV_NAMES`.
   Not reachable today — the security auditor verified that a proxy failure
   surfaces as `APIConnectionError("Connection error.")` with no credentials in
   the message, cause or traceback — but it pairs with the truncation gap D5 closed.
3. **A 200 response carrying an error body is untested.** If OpenRouter ever
   returns HTTP 200 with `{"error": ...}` instead of a status code, parsing
   raises a per-article error and the article is still lost. The 2026-07-14
   incident was NOT this case — the stored `last_error` names `APIStatusError`,
   which the SDK only produces on a non-2xx response — but the branch has no
   test.
4. **Stale docstring** at `tests/test_job_distributed_publish.py:508`, reported
   twice by the test reviewer. Pre-existing, unrelated to this change.
