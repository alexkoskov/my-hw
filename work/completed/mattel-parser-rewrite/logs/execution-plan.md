# Execution Plan: mattel-parser-rewrite

**Создан:** 2026-04-25

---

## Wave 1 (независимые)

### Task 01: rewrite-parser
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/ -q` зелёный + `python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())"` → `[]` или валидный список без notifier-уведомлений

## Wave 2 (Audit, зависит от Wave 1) — три аудитора параллельно

### Task 02: code-audit
- **Skill:** code-reviewing
- **Reviewers:** none (auditor IS the review)

### Task 03: security-audit
- **Skill:** security-auditor
- **Reviewers:** none

### Task 04: test-audit
- **Skill:** test-master
- **Reviewers:** none

## Wave 3 (Final, зависит от Wave 2) — QA с гейтом на наличие 3 audit-репортов

### Task 05: pre-deploy-qa
- **Skill:** pre-deploy-qa
- **Reviewers:** none

## Проверки, требующие участия пользователя

- [ ] После Wave 1 (если Verify-smoke task 01 провалился по network-reasons): запустить live-smoke вручную и подтвердить отсутствие notifier-сообщения о parsing error.
- [ ] После Wave 3: review QA report и принять решение о deploy (manual deploy.sh или GitHub Actions setup — open item из `project_post_mrw_pending` memory).
- [ ] В течение 7 дней после deploy: подтвердить отсутствие "Mattel news parsing error" в админ-чате; при появлении HW-релиза от Mattel — убедиться, что пост попал в pending_articles.
