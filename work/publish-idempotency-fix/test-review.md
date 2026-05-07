# Test Strategy Review — publish-idempotency-fix

Reviewer: test-master. Status: **needs_improvement** — 4 planned tests cover the happy guard path but miss 3 risky surfaces.

## 1. Are the 4 planned tests sufficient?

**Outage-path interaction — 🔴 GAP.** `_fallback_publish` has 4 entry conditions (Claude OK, per-article fail, ClaudeOutageError, `is_fallback_active()=True` shortcut). The guard is placed at line 985 — before the `is_fallback_active()` read at line 1045 — so the dominator argument is correct, BUT the test plan only exercises the Claude-OK path. The most-likely real-world replay is the outage-day path (operator restored a backup during a Claude outage). Add **`test_skip_fires_on_outage_active_path`**: pre-stage published row, set `outage_state.is_fallback_active()=True` → call `_fallback_publish` → assert guard fires, NO `_google_translate` call (mock `transcreate_text` with side_effect=AssertionError), NO Telegraph, return True. Without this test the regression "guard placed after line 1045" stays undetected.

**Stale row WITHOUT cached telegraph_url — 🟡 GAP.** All test fixtures stage rows with `telegraph_url` set. But `_fallback_publish` line 1180 only reuses URL if present; on a NULL `telegraph_url` the duplicate path goes through Telegraph CREATE then duplicate post then IntegrityError. The guard MUST fire regardless. Add a one-line variant of test 1 with `telegraph_url=None` on the stale pending row — pre-stage published row separately.

**Two-tier `list_pending` ordering — 🟡 GAP in test 4.** Code-research §A.4 documents Tier 0 (today) → Tier 1 (carry-over) ordering. The repro scenario is a CARRY-OVER stale row coexisting with TODAY's fresh rows. Integration test must pre-stage stale row with `fetched_at = '2026-05-06 ...'` (Tier 1) AND fresh row with today's `fetched_at` (Tier 0). Without explicit `fetched_at`, both rows land in the same tier and the guard's Tier-1-drains-first behavior isn't exercised. Note: existing `_create_mock_rss_entry` doesn't override `fetched_at` (DB default = CURRENT_TIMESTAMP) — must use raw INSERT for the stale row.

**Multiple stale rows / K admin pings — 🟢 document, don't test.** Code-research §E.3 already concluded "no throttle, K pings is correct". Add a one-line comment in test 2 referencing this decision; no extra test.

## 2. Regression risk surfaces

Verified via grep (`IntegrityError|UNIQUE constraint` across `tests/`, `news_bot.py`, `hw_review.py`): **zero hits**. Code-researcher's claim is accurate. `test_move_to_published_rollback_on_error` (test_pending_articles_repo.py:623) injects `OperationalError` on the 3rd execute — call sequence is unchanged by `INSERT OR IGNORE`, stays green.

**Strike-counting tests — none assert IntegrityError → strike** (slot loop test_distributed_schedule_integration.py inspects strikes via `attempt_count`, not via specific exception types). Safe.

## 3. Edge cases NOT on the test list

🔴 **Pending row's link in `failed_articles` (not published).** Operator manually re-queued via `retry_from_failed`, but a `failed_articles` row could co-exist by manual SQL. Guard reads `get_published`, returns None, normal publish proceeds — fine if `failed_articles` row stays orphaned, BUT will the new publish create a published_articles row + DELETE pending while `failed_articles` keeps the same link? Yes — and that's a silent inconsistency. Out of scope for this fix per user-spec, but **add a 3-line note in test docstring**: "Guard does NOT cover failed_articles collisions — symmetric fix tracked separately."

🟡 **`send_admin_notification` raises (operator's bot DM not started).** Code-research §G shows the implementation wraps `send_admin_notification` in try/except. **Test 2 must assert cleanup proceeds even when `mock_notify.side_effect = TelegramError(...)`** — `skip_pending` MUST be called, function MUST return True. Without this assertion, a regression "early return on notify failure" leaves the stale row in pending forever.

🟢 **hw_review tests.** Code-research §E.4 confirmed `_fallback_publish` change doesn't affect hw_review (separate publish flow). `INSERT OR IGNORE` change to `move_to_published` makes hw_review's manual publish idempotent too — desirable, no test update needed.

## 4. Test data realism

Existing fixtures (`_sample_entry`, `_create_mock_rss_entry`, `_make_claude_result`) reproduce production schema column-by-column. Pre-staging via raw `INSERT INTO published_articles` (per `test_hw_review_publish_flow.py:271` template) reproduces the EXACT row shape from prod. **Tests will catch the real bug, not a synthetic one.** One nuance: prod's stale row had `attempt_count > 0` after first slot's IntegrityError; the test should set `attempt_count` via raw UPDATE on the staged pending row to assert the guard fires regardless of attempt count.

## 5. What's missing — additional findings

🔴 **Litmus test on test 4.** If guard line in `_fallback_publish` is removed, does test 4 fail? With current plan: pre-stage stale + fresh, run job() → buggy code does Telegraph reuse (cached) + dup teaser + IntegrityError + strike. After 1 strike `attempt_count=1`, row stays in pending. Assertion "1 telegram send" fails (got 2: stale + fresh) → **test passes the litmus test ✓**. But add `mock_teaser.call_count == 1` AND assert the SINGLE call's args reference the FRESH link, not the stale one — otherwise a buggy guard that sends the WRONG one passes.

🟡 **Test 3's idempotency assertion.** Plan says "second call NO error, published_articles has 1 row, pending_articles empty." Add: assert the **ORIGINAL** `telegraph_url` and `via_review` are preserved in published_articles after the 2nd call (per code-research §B.3 caveat — `INSERT OR IGNORE` keeps first values). Without this assertion, a regression to `INSERT OR REPLACE` would silently break the audit trail.

🟢 **Logger assertion.** Guard logs `[idempotency] ...` warning — useful for grep in journalctl post-deploy. Add `self.assertLogs('news_bot', level='WARNING')` capture in test 1 with regex match on `[idempotency]`.

## Priority summary

| # | Finding | Severity |
|---|---------|----------|
| 1 | Add outage-path test (is_fallback_active=True + guard) | 🔴 |
| 2 | Add litmus assertion `mock_teaser.call_args` link == fresh link | 🔴 |
| 3 | Add `send_admin_notification` raising case to test 2 | 🟡 |
| 4 | Variant of test 1 with `telegraph_url=None` | 🟡 |
| 5 | Realistic `fetched_at` (Tier 1 stale + Tier 0 fresh) in test 4 | 🟡 |
| 6 | Assert original `telegraph_url`/`via_review` preserved in test 3 | 🟡 |
| 7 | Pre-set `attempt_count=1` on staged stale row in test 1 | 🟢 |
| 8 | `assertLogs` capture for `[idempotency]` warning | 🟢 |
| 9 | Document failed_articles collision out-of-scope in docstring | 🟢 |

**Decision:** add findings 1–2 BEFORE merge. 3–6 should be added in the same task (cheap, ~30 LOC total). 7–9 nice-to-have.
