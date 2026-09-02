# Execution Plan: Broad-Pair Dedup Precision

**Создан:** 2026-08-30
**Утверждён:** 2026-08-30 сообщением пользователя «делай»

---

## Wave 1: Regression oracle

### Task 1: Production corpus and scoring harness
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `venv/bin/python -m pytest tests/test_dedup_broad_precision.py -q` → валидный и детерминированный корпус из 24 пар без сети и БД
- **Gate:** обезличенная read-only выгрузка только публичных метаданных и существующих fingerprints; никаких токенов, операторских идентификаторов или сырых строк БД

## Wave 2: Core decision flow

### Task 2: Subject-aware pair rule and capped backstop
- **Depends on:** Task 1
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer

## Wave 3: Operator diagnostics

### Task 3: E014 reasons, suppression logs, and funnel telemetry
- **Depends on:** Task 2
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer

## Wave 4: Project Knowledge

### Task 4: Project Knowledge update
- **Depends on:** Task 3
- **Skill:** documentation-writing
- **Reviewers:** code-reviewer

## Wave 5: Parallel feature audits

### Task 5: Feature code audit
- **Depends on:** Task 4
- **Skill:** code-reviewing
- **Reviewers:** none; audit report is the review

### Task 6: Feature security audit
- **Depends on:** Task 4
- **Skill:** security-auditor
- **Reviewers:** none; audit report is the review

### Task 7: Feature test audit
- **Depends on:** Task 4
- **Skill:** test-master
- **Reviewers:** none; audit report is the review

## Wave 6: Release gate

### Task 8: Pre-deploy acceptance QA
- **Depends on:** Tasks 5, 6, 7
- **Skill:** pre-deploy-qa
- **Reviewers:** none; QA report is the release decision

## Wave 7: Manual production release

### Task 9: Manual production deployment
- **Depends on:** Task 8 and explicit release authorization after green QA
- **Skill:** deploy-pipeline
- **Reviewers:** code-reviewer, security-auditor, deploy-reviewer
- **Verify-smoke:** bounded container status/startup-log checks, without a forced tick or test article
- **Verify-user:** explicitly approve the production release; time of day does not gate deployment

## Wave 8: Live observation

### Task 10: Post-deploy precision verification
- **Depends on:** Task 9
- **Skill:** post-deploy-qa
- **Reviewers:** none; live report is the final evidence
- **Verify-user:** classify naturally occurring E014 and report any false flag, missed duplicate, or E015/E016 spike during the observation window

## Проверки, требующие участия пользователя

- [x] Утвердить валидированную декомпозицию и запуск реализации.
- [ ] Task 9: отдельно подтвердить production deploy после зелёного pre-deploy QA.
- [ ] Task 10: классифицировать естественные E014 в течение 2–4 недель после деплоя.

## Execution constraints

- Работа выполняется только в текущей сессии согласно `AGENTS.md`; обязательная multi-agent часть локального feature-execution skill заменена последовательным выполнением тех же checkpoints.
- `.project-progress.json` обновляется после каждой существенной задачи и перед итоговым отчётом.
- Runtime deployment, push и внешние изменения не выполняются до соответствующего gate.
