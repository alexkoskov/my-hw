# Pre-deploy QA report: manual-review-workflow

**Status:** PASS (ready to deploy)
**Date:** 2026-04-23
**Agent:** qa-runner (Task 14)
**Full JSON report:** [pre-deploy-qa.json](pre-deploy-qa.json)

## Test suite

| Metric | Value |
|---|---|
| Total | 383 |
| Passed | 383 |
| Failed | 0 |
| Skipped | 0 |
| xfailed | 0 |
| Duration | 14.79s |

Command: `python3 -m pytest tests/ -q` -> `383 passed in 14.79s`.

## Acceptance criteria matrix

| ID | Description | Status | Evidence |
|---|---|---|---|
| US-AC-01 | Prep-phase doesn't publish | pass | test_job_prep_phase.py; process_new_articles removed |
| US-AC-02 | New entries staged with original fields, ru_* NULL | pass | test_job_prep_phase.py; smoke 3 rows, ru_* NULL |
| US-AC-03 | UNIQUE on pending_articles.link | pass | test_pending_articles_repo.py duplicate returns False |
| US-AC-04 | Admin ping format byte-exact | pass | test_admin_ping.py; smoke byte-match confirmed |
| US-AC-05 | list: empty/numbered/failed-footer | pass | test_hw_review_cli.py; test_hw_review_retry.py |
| US-AC-06 | stage rejects partial | pass | test_hw_review_cli.py 8-vector validator |
| US-AC-07 | preview without stage errors; with stage renders HTML | pass | test_hw_review_cli.py; smoke --no-open prints path |
| US-AC-08 | publish creates + sends + moves in one cmd | pass | test_hw_review_publish_flow.py; smoke row in published |
| US-AC-09 | Retry reuses Telegraph URL (no 2nd createPage) | pass | test_publish_retry_reuses_telegraph_url (call_count == 1) |
| US-AC-10 | publish on vanished row: clean error | pass | test_hw_review_publish_flow.py three-state matrix |
| US-AC-11 | Queue = 10 + 0 new -> no fast-track | pass | test_overflow.py empty-entries no-op |
| US-AC-12 | skip with staged ru: y/N prompt | pass | test_hw_review_cli.py skip confirm |
| US-AC-13 | take clears notified_at | pass | test_hw_review_take.py |
| US-AC-14 | take after auto-publish: "already auto-published" | pass | test_hw_review_take.py already-left-pending |
| US-AC-15 | idle >48h: heads-up + auto-publish | pass | test_idle_fallback.py 12 cases |
| US-AC-16 | Queue cap 10; evict only ru-NULL | pass | test_overflow.py; Decision 7 SQL filter |
| US-AC-17 | Fast-track fail: counter shared, 3 strikes -> failed | pass | test_overflow.py + test_idle_fallback.py |
| US-AC-18 | Failed footer byte-exact | pass | test_hw_review_retry.py exact-format |
| US-AC-19 | retry moves failed -> pending, counter reset | pass | test_hw_review_retry.py 14 cases |
| US-AC-20 | Mattel via registry | pass | test_sources_registry.py |
| US-AC-21 | Dedup = published OR skipped | pass | test_pending_articles_repo.py; smoke dedup row |
| US-AC-22 | CLI logger + exit codes + show | pass | test_hw_review_cli.py; CLI smoke 9 subcmds exit 0 |
| Tech-AC-01 | init_db idempotent | pass | test_migration.py; smoke 2x call, 4 tables |
| Tech-AC-02 | UNIQUE insert returns False | pass | test_pending_articles_repo.py |
| Tech-AC-03 | Transactional repo mutators | pass | transactional rollback test |
| Tech-AC-04 | preview --no-open: exit 0, prints path, no browser | pass | test_hw_review_cli.py; smoke |
| Tech-AC-05 | publish out-of-range: exit 1, no traceback | pass | test_hw_review_publish_flow.py |
| Tech-AC-06 | source_name by netloc; 'other' warning | pass | test_sources_registry.py |
| Tech-AC-07 | build_admin_ping([]) == None, format byte-exact | pass | smoke + test_admin_ping.py |
| Tech-AC-08 | Existing tests pass except rewritten | pass | 383 passed |
| Tech-AC-09 | Migration test via PRAGMA | pass | test_migration.py |
| Tech-AC-10 | No new packages | pass | requirements.txt unchanged |
| Tech-AC-11 | Parametrised SQL | pass | grep -nP returns empty |
| Tech-AC-12 | last_error sanitised | pass | test_admin_ping.py; smoke 4 secrets replaced |
| Tech-AC-13 | preview CSP + URL-scheme + tag allowlist | pass | test_preview_renderer.py 63 cases |
| Tech-AC-14 | preview path-guard | pass | test_hw_review_cli.py |
| Tech-AC-15 | stage 8 rejection vectors | pass | test_hw_review_cli.py |
| Tech-AC-16 | Shared attempt_count (Decision 13) | pass | shared _fallback_publish path. Test audit MEDIUM: mixed-path test missing -- code correct, coverage gap only. |
| Tech-AC-17 | Consolidated idle ping (Decision 12) | pass | test_idle_fallback.py consolidated_ping |
| US-User-01 | Channel post visual | deferred | Task 15 |
| US-User-02 | Local HTML preview visual | deferred | Task 15 |
| Tech-AVP-01 | Live Telegraph render fidelity | deferred | Task 15 (curl) |
| Tech-AVP-02 | Live admin ping format | deferred | Task 15 (Telegram MCP) |

## Findings

Nothing blocking. Critical/major findings = 0.

Task 13 test-audit MEDIUM coverage gaps are documented (non-blocking):
- Mixed-path 3-strike test (idle+overflow combined) missing -- shared counter is pinned at repo level, cross-path invariant inherits from shared `_fallback_publish`.
- Decision 9 Telegraph-URL reuse pinned directly at 2/3 call-sites (publish + idle fallback); overflow inherits transitively via `_fallback_publish`.

Both are coverage-matrix gaps, not code-quality defects. Code audit (Task 11) + security audit (Task 12) are clean (0 CRITICAL/HIGH/MEDIUM).

## Deferred to post-deploy (Task 15)

4 criteria. See `deferred_to_post_deploy` block in JSON report. All require live Telegram / Telegraph / human visual validation -- outside pre-deploy scope.

## Smoke verification log

End-to-end tempfile-DB smoke (prep -> stage -> preview --no-open -> publish) with SOURCES, fetch_full_article, send_admin_notification, telegraph_publisher._api_call, hw_review.send_telegraph_teaser all mocked: PASS.

- Admin ping: `"3 ждут review: 🟠 autoevolution ×1, 🟣 mattel ×1, 🟢 lamley ×1"` (byte-exact).
- Preview file: written to `~/.cache/hw-review/hw-*.html`, unlinked after publish.
- published_articles row: present with `via_review=1`.
- processed_news dedup row: present.
- pending_articles count: 3 -> 2 after publish.

## Verdict

**READY TO DEPLOY.**
