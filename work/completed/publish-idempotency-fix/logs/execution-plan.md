# Execution Plan: publish-idempotency-fix

**Создан:** 2026-05-07
**Размер:** S (bug fix, 2 source-файла, 7 тестов)
**Команда:** publish-idempotency-fix

---

## Wave 1 (независимые — source edits)

### Task 1: Add idempotency guard to `_fallback_publish`
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files:** news_bot.py
- **Verify-smoke:** `python3 -m py_compile news_bot.py` → exit 0

### Task 2: Make `move_to_published` idempotent on duplicate link
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files:** pending_articles_repo.py
- **Verify-smoke:** `python3 -m py_compile pending_articles_repo.py` → exit 0; `grep -n "INSERT OR IGNORE INTO published_articles" pending_articles_repo.py` → 1 match

## Wave 2 (зависит от Wave 1 — tests)

### Task 3: Add 5 unit tests for the `_fallback_publish` guard
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files:** tests/test_fallback_publish_paths.py
- **Verify-smoke:** `pytest tests/test_fallback_publish_paths.py -q -k "skip_if_link_already_published or admin_ping"` → 5 passed
- **Depends on:** Task 1

### Task 4: Add 1 repository test for `move_to_published` idempotency
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files:** tests/test_pending_articles_repo.py
- **Verify-smoke:** `pytest tests/test_pending_articles_repo.py -q -k "test_move_to_published_idempotent_on_duplicate_link"` → 1 passed
- **Depends on:** Task 2

### Task 5: Add integration test for slot-loop with mixed zombie + fresh rows
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files:** tests/test_distributed_schedule_integration.py
- **Verify-smoke:** `pytest tests/test_distributed_schedule_integration.py -q -k "test_slot_loop_does_not_repost_already_published"` → 1 passed
- **Depends on:** Task 1, Task 2

## Wave 3 (Audit Wave — parallel, no reviewers)

### Task 6: Code Audit
- **Skill:** code-reviewing
- **Reviewers:** none (auditor IS the review)
- **Output:** logs/working/wave-3-code-audit.md
- **Depends on:** Tasks 3, 4, 5

### Task 7: Security Audit
- **Skill:** security-auditor
- **Reviewers:** none
- **Output:** logs/working/wave-3-security-audit.md
- **Depends on:** Tasks 3, 4, 5

### Task 8: Test Audit
- **Skill:** test-master
- **Reviewers:** none
- **Output:** logs/working/wave-3-test-audit.md
- **Depends on:** Tasks 3, 4, 5

## Wave 4 (Pre-deploy QA)

### Task 9: Pre-deploy QA
- **Skill:** pre-deploy-qa
- **Reviewers:** none
- **Verify:** `pytest tests/ -q` → 829+ passed; `python3 -m py_compile news_bot.py pending_articles_repo.py` → exit 0
- **Depends on:** Tasks 6, 7, 8

## Wave 5 (Deploy)

### Task 10: Deploy
- **Skill:** deploy-pipeline
- **Reviewers:** none
- **Verify-smoke:** GitHub Actions ci.yml + deploy_test.yml + deploy.yml all green
- **Verify-user:** operator observes @myhwchannel123 + runs one-time SQL DELETE on prod news.db
- **Depends on:** Task 9

## Wave 6 (Post-deploy verification)

### Task 11: Post-deploy verification
- **Skill:** post-deploy-qa
- **Reviewers:** none
- **Verify:** journalctl on prod (no UNIQUE constraint failed); SELECT failed_articles count = 0; visual @myhwchannel123 scan over 1-2 days
- **Depends on:** Task 10

---

## Проверки, требующие участия пользователя

- [ ] **Task 10 (Deploy):** оператор делает `git push origin dev` → наблюдает ~30 мин test-канал → `git merge dev → main && git push origin main` → запускает одноразовый SQL `DELETE FROM failed_articles WHERE link='https://orangetrackdiecast.com/2026/05/02/hot-wheels-2026-car-culture-team-transport-k-case-report/'` на prod news.db.
- [ ] **Task 11 (Post-deploy):** оператор открывает `journalctl -u news_bot.service` после деплоя; визуально просматривает `@myhwchannel123` 1-2 дня на отсутствие дублей с одинаковым Telegraph-URL.
- [ ] **Финальное:** если приходит admin-ping `Skipped re-publish` после деплоя — оператор открывает отдельный диагностический task на расследование первопричины (crontab/backup_db.sh/journalctl).

---

## Что мы НЕ закрываем этой фичей

- Корневая причина появления зомби-строки в `pending_articles` (как именно вчерашняя статья снова попала в очередь). Закрывается отдельным операторским task'ом диагностики.
- Восстановление статьи team-transport-k из `failed_articles` обратно в `published_articles` — она УЖЕ в `published_articles` от вчера; запись в failed просто удаляется.
- Throttling admin-ping'ов на случай K>5 одновременных guard-срабатываний — отложено до следующей итерации, если потребуется.

---

## Отклонения от user-spec'а (одобрены оператором 2026-05-07)

- **AC6 vs AC8:** при ошибке `skip_pending` во время guard'а — НЕ инкрементим `attempt_count` (Decision 8 техспека); guard логирует ERROR, шлёт второй admin-ping и возвращает True. Литеральная интерпретация AC8 (cleanup-fail = strike) отвергнута.
