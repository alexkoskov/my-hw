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

## Superseded audit and QA waves

Tasks 5–8 were removed from the active roadmap under the mandatory minimum-scope
rule. The completed task-level code, security and test reviews plus the green
`main` CI result were reused instead of repeating the same evidence in four new
deliverables. Their original task files remain only as historical planning records.

## Wave 7: Manual production release

### Task 9: Manual production deployment
- **Status:** Done — 2026-09-02
- **Depends on:** Task 4, green `main` CI and explicit release authorization
- **Skill:** deploy-pipeline
- **Reviewers:** none; existing task-level reviews and green CI were reused
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
- [x] Task 9: production deploy явно подтверждён и выполнен после зелёного `main` CI.
- [ ] Task 10: классифицировать естественные E014 в течение 2–4 недель после деплоя.

## Execution constraints

- Работа выполняется только в текущей сессии согласно `AGENTS.md`; обязательная multi-agent часть локального feature-execution skill заменена последовательным выполнением тех же checkpoints.
- `.project-progress.json` обновляется после каждой существенной задачи и перед итоговым отчётом.
- Runtime deployment, push и внешние изменения не выполняются до соответствующего gate.
