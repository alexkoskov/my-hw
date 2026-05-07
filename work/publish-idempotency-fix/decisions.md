# Decisions Log: publish-idempotency-fix

Agent reports on completed tasks. Each entry is written by the agent that executed the task.

---

<!-- Entries are added by agents as tasks are completed.

Format is strict — use only these sections, do not add others.
Do not include: file lists, findings tables, JSON reports, step-by-step logs.
Review details — in JSON files via links. QA report — in logs/working/.

## Task N: [title]

**Status:** Done
**Commit:** abc1234
**Agent:** [teammate name or "main agent"]
**Summary:** 1-3 sentences: what was done, key decisions. Not a file list.
**Deviations:** None / Deviated from spec: [reason], did [what].

**Reviews:**

*Round 1:*
- code-reviewer: 2 findings → [logs/working/task-N/code-reviewer-1.json]
- security-auditor: OK → [logs/working/task-N/security-auditor-1.json]

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-N/code-reviewer-2.json]

**Verification:**
- `npm test` → 42 passed
- Manual check → OK

-->

## Task 1: Add idempotency guard to `_fallback_publish`

**Status:** Done
**Commit:** c1a8076
**Agent:** main agent
**Summary:** Inserted idempotency guard at the entry of `_fallback_publish` (between lines 984 and 985 of `news_bot.py`), implementing Decisions 1, 2, 3, 4, 8. Guard calls `pending_repo.get_published(link)` via the existing alias from line 48; on hit, logs INFO with `[idempotency-guard]` marker, sends admin ping (return-value checked, WARNING on False, no try/except per Decision 4), wraps `pending_repo.skip_pending(link)` in try/except (on failure logs ERROR + sends second admin ping + still returns True per Decision 8), returns True. On miss the function falls through unchanged.
**Deviations:** None.

**Reviews:**

Reviews for Task 1 will be conducted in the audit wave (Tasks 6–8). Tests for the guard are deferred to Task 3 (Wave 2) per the task's explicit decomposition pattern.

**Verification:**
- `python3 -m py_compile news_bot.py` → exit 0, no output.
- `python3 -c "from news_bot import _fallback_publish; print(_fallback_publish.__doc__[:100])"` → outputs `Auto-publish a pending row through Claude (primary) or Google\n    (per-article + global fallback). Used` (no ImportError).
- `git diff news_bot.py` (pre-commit) → 36 insertions localized to a single block immediately after line 984; no other file changes.

## Task 2: Make `move_to_published` idempotent on duplicate link

**Status:** Done
**Commit:** a203e11
**Agent:** main agent
**Summary:** Заменил `INSERT INTO published_articles` на `INSERT OR IGNORE INTO published_articles` на line 582 в `pending_articles_repo.move_to_published` (defense-in-depth для пути `hw_review.cmd_publish`, который не проходит через guard из Task 1). Параметризация, bind-кортеж, шаги 2/3 (`INSERT OR IGNORE INTO processed_news`, `DELETE FROM pending_articles`) и форма транзакции (`try`/`except: rollback; raise`/`finally: close`) не тронуты — diff ровно одна строка. Соответствует Decision 5 tech-spec'а.
**Deviations:** None.

**Reviews:**

Reviews for Task 2 will be conducted in the audit wave (Tasks 6–8). Test T6 deferred to Task 4 (Wave 2) per task spec.

**Verification:**
- `python3 -m py_compile pending_articles_repo.py` → exit 0, без вывода.
- `grep -n "INSERT OR IGNORE INTO published_articles" pending_articles_repo.py` → ровно 1 матч (line 582).
- `grep -c "INSERT OR IGNORE" pending_articles_repo.py` → 4 (было 3, прирост ровно +1).
- `git diff pending_articles_repo.py` (pre-commit) → одна изменённая строка (line 582).

## Task 4: Add repository test for `move_to_published` idempotency

**Status:** Done
**Commit:** dbce617
**Agent:** main agent
**Summary:** Добавлен метод `test_move_to_published_idempotent_on_duplicate_link` в класс `TestMoves(_TmpDbCase)` в `tests/test_pending_articles_repo.py`. Тест выполняет два последовательных `move_to_published` для одного и того же `link` (re-stage между ними — сырой SQL `INSERT INTO pending_articles` через `self._conn()` с непустым `ru_title` для NOT NULL-инварианта published-схемы) и фиксирует пост-Task-2 контракт: второй вызов не поднимает `IntegrityError`, в `published_articles` остаётся ровно одна строка с ПЕРВЫМИ значениями (`telegraph_url='https://telegra.ph/first'`, `telegraph_path='first'`, `via_review=0`) — литмус против регрессии `INSERT OR REPLACE`, — а `pending_articles` пуст после второго `DELETE`. Соответствует tech-spec T6 и Acceptance Criteria task-файла.
**Deviations:** None.

**Reviews:**

Reviews for Task 4 will be conducted in the audit wave (Tasks 6–8) per task spec.

**Verification:**
- `pytest tests/test_pending_articles_repo.py -q -k "test_move_to_published_idempotent_on_duplicate_link"` → `1 passed, 35 deselected`.
- `pytest tests/test_pending_articles_repo.py -q` → `36 passed` (было 35, прирост ровно +1, регрессий нет).
- pre-commit (gitleaks, trailing whitespace, EOF, merge conflicts, large files, private key) → all passed.

## Task 3: Add 5 unit tests for the `_fallback_publish` guard

**Status:** Done
**Commit:** 0535363
**Agent:** main agent
**Summary:** Добавлен новый класс `TestIdempotencyGuard(_FallbackPublishPathsCase)` в `tests/test_fallback_publish_paths.py` с пятью тестами, покрывающими все четыре входные ветки `_fallback_publish` плюс admin-ping контракт: T1 Claude-path с `assertLogs` на `[idempotency-guard]` маркер (AC10), T2 outage-shortcut (фиксирует регрессию, при которой guard окажется ПОСЛЕ `is_fallback_active` shortcut'а), T3 zombie-row с `telegraph_url=NULL` (доказывает, что guard стоит до Telegraph CREATE branch), T4 admin ping содержит `"⚠️ Skipped re-publish of "` + link, T5 admin ping вернул False — `skip_pending` всё равно отработал, return True, WARNING лог. Pre-stage в `published_articles` сделан raw SQL helper'ом `_pre_stage_published` (паттерн из `tests/test_hw_review_publish_flow.py:271`); cleanup verification (`processed_news`) — отдельный raw SQL helper `_processed_news_has`. Все side-effect моки обёрнуты в `AssertionError` для громких падений при регрессии guard'а (dominator-position литмус).
**Deviations:** None.

**Reviews:**

Reviews for Task 3 will be conducted in the audit wave (Tasks 6–8) per task spec.

**Verification:**
- `pytest tests/test_fallback_publish_paths.py -q -k "skip_if_link_already_published or admin_ping"` → `5 passed, 5 deselected`.
- `pytest tests/test_fallback_publish_paths.py -q` → `10 passed` (было 5 + новых 5, регрессий нет).
- pre-commit (gitleaks, trailing whitespace, EOF, merge conflicts, large files, private key) → all passed.

## Task 5: Add 1 integration test for slot-loop with mixed zombie + fresh rows

**Status:** Done
**Commit:** efc5910
**Agent:** main agent
**Summary:** Добавлен метод `test_slot_loop_does_not_repost_already_published` в класс `TestDistributedSchedule` в `tests/test_distributed_schedule_integration.py`. Тест end-to-end прогоняет `news_bot.job()` поверх Wave-1 правок с pre-stage сценарием T7 (1 published-row + 1 zombie pending в carry-over tier с `attempt_count=2` + кешированным `telegraph_url` + 1 fresh pending). Литмус-asserts: `mock_teaser` вызван ровно один раз с `link_fresh` во второй позиционной аргументе (НЕ count-only), `published_articles` содержит ровно 2 строки (zombie pre-existing + fresh), `failed_articles=0` (литмус AC6 — без guard'а строка с `attempt_count=2` ткнулась бы в strike #3 → `move_to_failed`), admin-ping содержит `link_zombie` + маркер `Skipped re-publish`, `processed_news` содержит обе ссылки (zombie через `skip_pending`, fresh через `move_to_published`), `pending_articles` пуст. Pre-stage published-row сделан raw SQL по канону `tests/test_hw_review_publish_flow.py:271`; carry-over markers (`fetched_at = datetime('now', '-2 days')`, `attempt_count=2`, `telegraph_url`) — raw UPDATE после `insert_pending`. RSS источники пустые через `self._set_rss_entries([])` чтобы слот-луп прошёл ровно по двум pre-staged строкам. Mock на `transcreate_via_claude` — список из ОДНОГО элемента (только fresh должна дойти до Claude); тест дополнительно проверяет `mock_claude.call_count == 1`.
**Deviations:** None.

**Reviews:**

Reviews for Task 5 will be conducted in the audit wave (Tasks 6–8) per task spec.

**Verification:**
- `pytest tests/test_distributed_schedule_integration.py -q -k "test_slot_loop_does_not_repost_already_published"` → `1 passed, 4 deselected`.
- `pytest tests/test_distributed_schedule_integration.py -q` → `5 passed` (было 4 + новый 1, регрессий нет).
- pre-commit (gitleaks, trailing whitespace, EOF, merge conflicts, large files, private key) → all passed.
