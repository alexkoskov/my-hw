# Execution Plan: manual-review-workflow

**Создан:** 2026-04-23
**Total waves:** 10
**Team:** manual-review-workflow

---

## Wave 1 — независимые модули (4 задачи параллельно)

### Task 1: `pending_articles_repo` module
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke

### Task 2: `preview_renderer` module
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke

### Task 3: Admin-ping helper + source vocabulary + sanitize_error_message
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke

### Task 4: Public `preview_nodes` wrapper in `telegraph_publisher`
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify:** smoke

## Wave 2 — зависит от Task 3

### Task 5: Source registry + source_name tagging
- **Depends on:** 3
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke

## Wave 3 — зависит от Tasks 1–5 (prep-фаза)

### Task 6: Refactor `job()` into prep-only + cron bump + delete `process_new_articles`
- **Depends on:** 1, 2, 3, 4, 5
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke

## Wave 4 — CLI скелет

### Task 7: `hw_review` CLI with `list` / `show` / `stage` / `skip` / `preview`
- **Depends on:** 1, 2, 3, 4, 6
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke, user

## Wave 5 — публикация через CLI

### Task 8: `hw_review publish` with Telegraph-URL reuse
- **Depends on:** 7
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke, user

## Wave 6 — idle-fallback

### Task 9: Idle-fallback pass + `hw_review take` command
- **Depends on:** 6, 8
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke

## Wave 7 — overflow/retry

### Task 10: Overflow fast-track pass + `hw_review retry` + failed-footer
- **Depends on:** 9
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify:** smoke

## Wave 8 — Audit Wave (3 аудита параллельно, reviewers: none)

### Task 11: Code Audit
- **Depends on:** 1–10
- **Skill:** code-reviewing
- **Reviewers:** none (auditor IS review)

### Task 12: Security Audit
- **Depends on:** 1–10
- **Skill:** security-auditor
- **Reviewers:** none

### Task 13: Test Audit
- **Depends on:** 1–10
- **Skill:** test-master
- **Reviewers:** none

> Если любой аудитор находит проблемы — спавним ad-hoc fixer-teammate с code-writing + нашедшие проблемы аудиторы как reviewers (до 3 раундов). После approval — Wave 9.

## Wave 9 — Pre-deploy QA

### Task 14: Pre-deploy QA
- **Depends on:** 11, 12, 13
- **Skill:** pre-deploy-qa
- **Reviewers:** none
- **Teammate name:** qa-runner
- **Verify:** smoke

## Wave 10 — Post-deploy verification

### Task 15: Post-deploy verification
- **Depends on:** 14
- **Skill:** post-deploy-qa
- **Reviewers:** none
- **Verify:** smoke, user

---

## Проверки, требующие участия пользователя

- [ ] **Task 7** (user verify): ручная проверка `hw_review list/show/stage/skip/preview` — открытие HTML-превью в локальном браузере, CLI-flow.
- [ ] **Task 8** (user verify): `hw_review publish` в staging — реальный Telegraph createPage + Telegram teaser (или dry-run, как решит оператор).
- [ ] **Task 15** (user verify): post-deploy на живом окружении — проверка крона, админ-пингов, `hw_review` в проде.
- [ ] **После всех волн (Phase 4):** финальное тестирование и approval.
