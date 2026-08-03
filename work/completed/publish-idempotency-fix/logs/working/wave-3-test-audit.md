# Wave 3 — Test Audit (publish-idempotency-fix, Task 8)

**Verdict: PASS**

All 7 new tests across 3 files pass the litmus checks defined in tech-spec § Testing Strategy and the Acceptance Criteria of Task 8. No FAIL findings, no NEEDS-FIX. One minor LOW-severity informational note on shared raw-SQL helpers (not a blocker).

| Test | File | Verdict |
| --- | --- | --- |
| T1 `test_skip_if_link_already_published_claude_path` | `tests/test_fallback_publish_paths.py:515` | PASS |
| T2 `test_skip_if_link_already_published_outage_shortcut_path` | `tests/test_fallback_publish_paths.py:598` | PASS |
| T3 `test_skip_if_link_already_published_no_telegraph_url` | `tests/test_fallback_publish_paths.py:667` | PASS |
| T4 `test_admin_ping_fires_when_guard_skips` | `tests/test_fallback_publish_paths.py:734` | PASS |
| T5 `test_guard_continues_when_admin_ping_returns_false` | `tests/test_fallback_publish_paths.py:776` | PASS |
| T6 `test_move_to_published_idempotent_on_duplicate_link` | `tests/test_pending_articles_repo.py:623` | PASS |
| T7 `test_slot_loop_does_not_repost_already_published` | `tests/test_distributed_schedule_integration.py:701` | PASS |

Pyramid balance for an S-size feature (5 unit + 1 repo + 1 integration): healthy and proportional. No vacuous assertions found.

---

## Per-test findings

### T1 — `test_skip_if_link_already_published_claude_path`

**Location:** `tests/test_fallback_publish_paths.py:515-594`

| Litmus check | Status | Evidence |
| --- | --- | --- |
| AC10 — `assertLogs` captures one INFO entry with `[idempotency-guard]` AND the link | PASS | Lines 561 (`with self.assertLogs('news_bot', level='INFO') as logs:`) and 583-591: filter requires `line.startswith('INFO')` AND `'[idempotency-guard]' in line` AND `link in line`, then `assertEqual(len(marker_lines), 1, ...)`. Triple constraint with strict count = 1. |
| Guard fires BEFORE LLM (Claude default path) | PASS | `mock_claude` wired with `side_effect=AssertionError(...)` (line 526) — would explode if guard was bypassed. `mock_claude.assert_not_called()` line 569. |
| No Telegraph CREATE / mark / teaser / move side-effects | PASS | Lines 532-546: every side-effect mock is wired with `side_effect=AssertionError(...)`, not just `MagicMock()`. Asserts at lines 571-575 confirm none called. |
| Cleanup: pending row deleted + processed_news contains link | PASS | Lines 578-579: `assertIsNone(repo.get_pending(link))` + `assertTrue(self._processed_news_has(link))` (raw-SQL helper). |
| Returns True | PASS | Line 565: `self.assertTrue(ok)`. |
| `outage_state.is_fallback_active()` mocked False (Claude path explicitly) | PASS | Line 551: `patch('news_bot.outage_state.is_fallback_active', return_value=False)`. |

**Litmus extra:** mocks-as-AssertionError pattern (instead of plain `MagicMock`) is a stronger litmus than `assert_not_called` alone — the test would crash mid-run rather than wait for a final assertion. Excellent defensive scaffolding.

---

### T2 — `test_skip_if_link_already_published_outage_shortcut_path`

**Location:** `tests/test_fallback_publish_paths.py:598-663`

| Litmus check | Status | Evidence |
| --- | --- | --- |
| `is_fallback_active=True` actually exercised (line-1045 path) | PASS | Line 634: `patch('news_bot.outage_state.is_fallback_active', return_value=True)`. |
| Translation paths (Claude AND Google) NOT called | PASS | Lines 609-614: both mocks wired with `side_effect=AssertionError(...)`. Asserts at lines 648-649 confirm both `assert_not_called`. This is the critical regression-catch surface: any future refactor placing the guard AFTER the outage shortcut would invoke `transcreate_text` → `mock_google` raises. |
| `[idempotency-guard]` log marker captured under outage path | PASS | Lines 644 + 659-663: `assertLogs` + filter on marker + link, `assertGreaterEqual(len(marker_lines), 1)`. |
| Cleanup verified | PASS | Lines 656-657: `assertIsNone(repo.get_pending(link))` + `_processed_news_has(link)`. |

**Note on assertion strictness:** T1 uses `assertEqual(len, 1)` for the marker count; T2 uses `assertGreaterEqual(..., 1)`. Both valid — T1 is the stricter contract owner (AC10 explicit "exactly one log line"), T2 just asserts presence. Acceptable asymmetry.

---

### T3 — `test_skip_if_link_already_published_no_telegraph_url`

**Location:** `tests/test_fallback_publish_paths.py:667-730`

| Litmus check | Status | Evidence |
| --- | --- | --- |
| `telegraph_url` is genuinely NULL on the pending row | PASS | Line 679: `assertIsNone(row.get('telegraph_url'))` — explicit pre-condition assert. Without this, "guard skipped Telegraph CREATE" could be vacuous if the row happened to carry a cached URL. |
| Telegraph CREATE branch (`publish_article`) NOT called | PASS | Line 687: `mock_publish` wired as AssertionError ("publish_article (Telegraph CREATE) must NOT fire"). Line 720: `mock_publish.assert_not_called()`. |
| Other side-effects also blocked | PASS | Lines 681-701: all 7 mocks AssertionError-wired. Asserts at 722-727. |
| Cleanup verified | PASS | Lines 729-730. |

This is the test that proves the guard sits BEFORE the Telegraph CREATE branch (not just before Telegraph reuse). Owns one specific mutation surface and asserts on it explicitly.

---

### T4 — `test_admin_ping_fires_when_guard_skips`

**Location:** `tests/test_fallback_publish_paths.py:734-772`

| Litmus check | Status | Evidence |
| --- | --- | --- |
| Exactly one admin ping fired | PASS | Line 766: `mock_notify.assert_called_once()`. |
| Ping content contains canonical prefix `"⚠️ Skipped re-publish of "` | PASS | Line 770-771: `ping_text = mock_notify.call_args.args[0]`, `assertIn("⚠️ Skipped re-publish of ", ping_text)`. Reads positional first arg (correct for `send_admin_notification(message)` — single-positional signature at `news_bot.py:357`). |
| Ping contains the link | PASS | Line 772: `assertIn(link, ping_text)`. |
| Guard short-circuited (no side-effects) | PASS | Side-effect mocks all AssertionError-wired (lines 742-748). |

**Robustness note:** the test uses `assertIn` (substring) rather than `assertEqual`, defending against future additions of `INSTANCE_LABEL` prefix, emoji variants, or wording adjustments. Good design choice consistent with how `send_admin_notification` actually composes the payload (`_redact_text` + `INSTANCE_LABEL` prefix).

---

### T5 — `test_guard_continues_when_admin_ping_returns_false`

**Location:** `tests/test_fallback_publish_paths.py:776-826`

| Litmus check | Status | Evidence |
| --- | --- | --- |
| Mock semantics: `return_value=False`, NOT `side_effect=Exception(...)` | PASS | Line 797: `mock_notify = MagicMock(return_value=False)`. Matches actual `send_admin_notification` semantics (`news_bot.py:357` — never raises, returns bool). Test would pass falsely if the implementation grew a try/except around the ping; the mock contract enforces "no try/except needed" (Decision 4). |
| Guard still calls `skip_pending` | PASS | Lines 817-818: `assertIsNone(repo.get_pending(link))` + `_processed_news_has(link)` — DB-level proof that `skip_pending` ran (not just mock-call assertion). |
| Returns True | PASS | Line 815: `assertTrue(ok)`. |
| WARNING log emitted, mentions failed admin ping AND link | PASS | Line 811: `assertLogs('news_bot', level='WARNING')`. Lines 820-824: filter requires `line.startswith('WARNING')` AND `'admin ping' in line` AND `link in line`, `assertGreaterEqual(len(warn_lines), 1)`. |

**Critical mock-correctness note:** this test verifies Decision 4's exact wording — "function never raises, returns bool". A wrong test design would have used `side_effect=Exception("...")`, and the guard implementation would then need defensive try/except (dead code per Decision 4). The current mock contract IS the contract.

---

### T6 — `test_move_to_published_idempotent_on_duplicate_link`

**Location:** `tests/test_pending_articles_repo.py:623-694`

| Litmus check | Status | Evidence |
| --- | --- | --- |
| Second `move_to_published` does NOT raise IntegrityError | PASS | Lines 664-669: second call wrapped in nothing; if it raised, the test would fail with traceback (no `assertRaises`). Exit clean = pass. |
| Published row holds ORIGINAL `url1`/`path1`/`via_review=0` after second call (catches `INSERT OR REPLACE` regression) | PASS | Lines 682-686: `pub = repo.get_published(link)`, `assertEqual(pub['telegraph_url'], 'https://telegra.ph/first')`, `assertEqual(pub['telegraph_path'], 'first')`, `assertEqual(pub['via_review'], 0)`. The second call passed `'https://telegra.ph/second'`/`'second'`/`via_review=True` (lines 666-668), so an `INSERT OR REPLACE` regression would have flipped these to `'second'`/`1` and the assertion would fail loudly. This is the core litmus assertion of T6, and it lands precisely. |
| Exactly one published row for the link | PASS | Lines 672-677: `SELECT COUNT(*) FROM published_articles WHERE link=?` → `assertEqual(n_pub, 1)`. |
| Pending cleaned by step 3 of second call | PASS | Lines 689-694: `assertIsNone(repo.get_pending(link))` + COUNT(*) on pending = 0. |
| Re-stage uses raw SQL (pending PK was deleted on first move) | PASS | Lines 654-661: explicit raw `INSERT INTO pending_articles` with non-empty `ru_title='РуТ2'` (NOT NULL invariant respected). Comment block lines 650-653 explains the raw-SQL choice. |

**Strength of design:** T6 distinguishes `INSERT OR IGNORE` from `INSERT OR REPLACE` purely through value-comparison after second call. Without the original-value assertions, both keywords would pass an "exactly 1 row" count. The first-vs-second value contrast is the actual semantic litmus.

---

### T7 — `test_slot_loop_does_not_repost_already_published`

**Location:** `tests/test_distributed_schedule_integration.py:701-868`

| Litmus check | Status | Evidence |
| --- | --- | --- |
| `mock_teaser.call_args.args[1] == link_fresh` (positional, NOT `kwargs.get('link')`) | PASS | Lines 802-807: `self.mock_teaser.assert_called_once()` then `self.assertEqual(self.mock_teaser.call_args.args[1], link_fresh, ...)`. Comment at lines 796-801 explicitly justifies `args[1]` over `kwargs` because `send_telegraph_teaser(telegraph_url, source_url)` is invoked positionally at `news_bot.py:1273` — verified the signature is `def send_telegraph_teaser(telegraph_url, source_url)` at `news_bot.py:793`. `args[1]` is the source_url = link. Wrong-link false positive cannot pass. |
| AC6 explicit pre/post check: zombie row had `attempt_count=2` BEFORE `job()`, `failed_articles=0` AFTER `job()` | PASS | Lines 751-760: raw `UPDATE pending_articles SET attempt_count=2 ...` is the BEFORE state — set explicitly to litmus level (one strike away from `move_to_failed`). Lines 825-836: AFTER state checked via `SELECT COUNT(*) FROM failed_articles` → `assertEqual(failed_count, 0, "AC6 litmus: ... guard intercepted before strike machinery ran")`. The combination is the proof: without the guard, `attempt_count=2 + UNIQUE failure → strike #3 → move_to_failed → failed_count=1`. The empty `failed_articles` therefore semantically witnesses guard interception. |
| `published_articles` final state: 2 rows (zombie pre-existing + fresh) | PASS | Lines 810-819: `set(published) == {link_zombie, link_fresh}` + `len(published) == 2`. Catches accidental duplicate via INSERT-replace regression in addition to T6. |
| Admin ping for zombie link with `Skipped re-publish` marker | PASS | Lines 840-846: substring scan over `_admin_messages()` requires both `link_zombie in m` AND `'Skipped re-publish' in m`. |
| `processed_news` contains both links | PASS | Lines 850-861: `SELECT link FROM processed_news WHERE link IN (?, ?)`, `assertEqual({r[0] for r in rows}, {link_zombie, link_fresh})`. Proof that the zombie went through `skip_pending` AND fresh went through `move_to_published`. |
| Pending empty post-job | PASS | Lines 865-868: `assertEqual(self._pending_links(), [], ...)`. |
| Claude called exactly once (only fresh reaches translation) | PASS | Lines 780-794: mock_claude with `side_effect = [_make_claude_result(...)]` (single-element list — second call would `StopIteration`-explode). Line 790: `assertEqual(mock_claude.call_count, 1, ...)`. |
| `via_review=0` on zombie pre-stage (auto-bot, not operator) | PASS | Line 736: `'autoevolution', 0` — matches the zombie-row scenario (the intended bug surface) rather than an operator-driven publish. |
| `fetched_at = datetime('now', '-2 days')` on zombie row (carry-over tier marker) | PASS | Line 753: explicit raw UPDATE. Without this, the zombie might fall into the "fresh tier" branch of the slot loop and the dominator-position semantics would not be exercised on the carry-over path. |

**Strength of design:** T7 is the single critical end-to-end litmus for the entire feature. Every assertion is content-grade (link equality, marker substring, count + value combination), not count-only. The `args[1]` choice over `kwargs.get('link')` is documented inline (lines 796-801) so a future refactor that switches the call site to keyword args would surface as an explicit test failure rather than a silent vacuous pass.

---

## Anti-pattern findings

**None found.** Specifically checked:

- **Vacuous assertions (`assert True`, mocks asserting themselves):** none. Every assertion either (a) reads DB state via raw SQL or repo helpers, (b) reads `call_args.args[N]` positional content, or (c) substring-scans log/ping payloads.
- **Mock-only "test the mock" pattern:** none. Side-effect mocks are wired as `AssertionError(...)` exclusively for negative-prove paths, never as plain `MagicMock()` followed by no-op asserts.
- **Excessive mocking covering wrong test type:** appropriate. T1-T5 are unit-scope (5+ mocks acceptable because they isolate `_fallback_publish` from its full dependency graph). T6 is repo-scope and uses zero mocks (real tempfile DB). T7 is integration-scope using the standard `TestDistributedSchedule` setup that mocks only external boundaries (Telegram bot, Anthropic SDK, Telegraph, time.sleep) while keeping the SQLite DB real.
- **`assertLogs` correctly used (T1, T2, T5):** `with self.assertLogs(...)` blocks scope the capture; filtering by level prefix + content + link matches the AC10 / Decision 4 wording.
- **Fragile string matching:** T4 ping prefix uses `assertIn`, not `assertEqual` — survives `INSTANCE_LABEL` / `_redact_text` evolution without false negatives.

---

## Pyramid balance

S-size feature, 7 new tests:
- **5 unit** (`test_fallback_publish_paths.py::TestIdempotencyGuard`) — cover all 4 entry conditions of the guard (Claude / outage shortcut / no-tg URL / admin-ping-True / admin-ping-False) + AC10 log marker. Proportional to the 4-way dominator-position contract.
- **1 repository** (`test_pending_articles_repo.py::TestMoves::test_move_to_published_idempotent_on_duplicate_link`) — defense-in-depth on `INSERT OR IGNORE` change, exercises real SQLite to catch the actual SQL semantics (mocks would not).
- **1 integration** (`test_distributed_schedule_integration.py::TestDistributedSchedule::test_slot_loop_does_not_repost_already_published`) — single end-to-end run of `news_bot.job()` with mixed zombie + fresh rows.

**Assessment: healthy.** Ratio 5:1:1 is exactly what an S-feature with one new code path + one keyword change merits. No inversion (no E2E heavy / unit light). No redundancy:
- T1 dominator path ≠ T2 outage shortcut ≠ T3 Telegraph-CREATE branch — each tests a distinct mutation surface.
- T4 (ping fires) and T5 (ping returns False) split the admin-ping contract along the True/False axis exactly per Decision 4.
- T6 unit-tests the SQL keyword change in isolation; T7 verifies the same change does not regress when both code paths converge through `job()`.

`patterns.md` Testing section guidance for S-size: "5-10 tests, mostly unit + 1 integration if there is a real flow". 5+1+1 sits inside the band.

---

## Recommendations

**No action required.** All 7 tests pass the litmus criteria for AC1-AC10 + Decisions 1-5,8 of the tech-spec. Mock semantics align with actual dependency behavior. AssertionError-wired mocks elevate test signal-to-noise (regressions explode loudly mid-run rather than via end-of-run assertion mismatch).

**Optional (LOW severity, informational only — NOT a fix-task trigger):**
The raw-SQL pre-stage helpers `_pre_stage_published` (`tests/test_fallback_publish_paths.py:479-501`) and the inline raw-SQL block at `tests/test_distributed_schedule_integration.py:725-740` are functionally equivalent and could in principle be hoisted to a shared `tests/_test_helpers.py` if a future feature also pre-stages `published_articles`. This is convergent code via spec ("pattern from `tests/test_hw_review_publish_flow.py:271`"), not duplication that hides a divergence. Leave as-is for this feature; revisit only if a third call-site appears.

**Pyramid balance:** confirmed proportional, no rebalance suggested.

**Test runner status (per task spec — informational, not a quality criterion):**
- `pytest tests/test_fallback_publish_paths.py -q -k "skip_if_link_already_published or admin_ping" -v` → 5 passed (per Task 3 verification log).
- `pytest tests/test_pending_articles_repo.py -q -k "test_move_to_published_idempotent_on_duplicate_link" -v` → 1 passed (per Task 4 verification log).
- `pytest tests/test_distributed_schedule_integration.py -q -k "test_slot_loop_does_not_repost_already_published" -v` → 1 passed (per Task 5 verification log).

**No fix-task needed for Task 3, Task 4, or Task 5.** Hand off to feature orchestrator with PASS verdict.
