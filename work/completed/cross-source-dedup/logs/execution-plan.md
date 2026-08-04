# Execution Plan: cross-source-dedup

**Создан:** 2026-06-05

**Total tasks:** 11 across 6 waves.
**Team:** `cross-source-dedup` (created in Phase 1).

---

## Wave 1 (независимые, параллельно)

### Task 1: model_extractor.py + calibration fixture
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 -c "from model_extractor import extract_fingerprint; print(extract_fingerprint(...))"`

### Task 2: Schema migration + repo helpers + rate-limit helpers
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** double-init `init_schema()` smoke (no exception on second call, `model_fingerprint` column present)

### Task 3: Admin-ping builders E014, E015, E016
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_admin_alerts.py -k "e014 or e015 or e016" -v`

## Wave 2 (зависит от Wave 1)

### Task 4: Wire dedup gate в news_bot.job() + integration tests
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_integration.py::TestCrossSourceDedup tests/test_integration.py::TestFingerprintCarryThrough -v`
- **Depends on:** Task 1 (extractor + similarity), Task 2 (repo helpers), Task 3 (alert builders)

### Task 5: backfill_fingerprints.py one-shot script
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `python3 backfill_fingerprints.py --dry-run --days 1` on test DB
- **Depends on:** Task 1 (extractor), Task 2 (update_published_fingerprint)

## Wave 3 (Audit — параллельно, аудиторы IS the review)

### Task 6: Code Audit
- **Skill:** code-reviewing
- **Teammate:** code-auditor
- **Output:** `logs/audits/code-audit.md`
- **Depends on:** Task 4, Task 5

### Task 7: Security Audit
- **Skill:** security-auditor
- **Output:** `logs/audits/security-audit.md`
- **Depends on:** Task 4, Task 5

### Task 8: Test Audit
- **Skill:** test-master
- **Output:** `logs/audits/test-audit.md`
- **Depends on:** Task 4, Task 5

## Wave 4 (Pre-deploy QA)

### Task 9: Pre-deploy QA
- **Skill:** pre-deploy-qa
- **Output:** `logs/qa-report.json` + summary в decisions.md
- **Depends on:** Task 6, 7, 8

## Wave 5 (Deploy)

### Task 10: Deploy
- **Skill:** deploy-pipeline
- **Operator-driven:** агент готовит команды, оператор запускает `./deploy.sh` и пишет ответ.
- **Depends on:** Task 9

## Wave 6 (Post-deploy verification)

### Task 11: Post-deploy verification
- **Skill:** post-deploy-qa
- **Operator-driven:** оператор запускает `python3 backfill_fingerprints.py --days 14` на VPS и постит результат; агент собирает отчёт.
- **Depends on:** Task 10

---

## Проверки, требующие участия пользователя

- [ ] **Task 10 (Deploy):** оператор запускает `./deploy.sh` на проде с правильными env-vars (`SSH_HOST=hwbot@148.135.207.54 DEPLOY_PATH=/home/hwbot/bot`), сообщает результат.
- [ ] **Task 11 (Post-deploy):** оператор запускает `python3 backfill_fingerprints.py --days 14` на VPS, постит summary. Затем 2-недельный пассивный мониторинг E014/E015 в Telegram-чате admin.
- [ ] **Финал:** ручной spot-check архива канала через 2 недели — нет ли пропущенных дублей.

## Эскалации к лиду (тебе)

- Любая задача застряла на 3-м round'е reviewers.
- Аудит-волна нашла блокирующие проблемы.
- Pre-deploy QA провалилась.
- Оператор не подтвердил deploy (Task 10).
