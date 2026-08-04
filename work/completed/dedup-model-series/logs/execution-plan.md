# Execution Plan: dedup-model-series

**Создан:** 2026-07-13
**Ветка:** dev (прод не трогаем — деплой применяет оператор вне окна 10:00–20:00 МСК)
**Оркестрация:** lead (main agent) спавнит по агенту на задачу внутри волны, ревьюеры — по списку задачи; коммиты делает lead последовательно (в этом окружении нет TeamCreate). Калибровочные тесты красны by design с Wave 1 до Task 6 — прогоны в Wave 1–2 идут с `-k 'not calibration'`.

---

## Wave 1 (независимые — параллельно)

### Task 1: Series/theme extraction + tier-tagged lexicon (`model_extractor.py`)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** 3 SDCC-тайтла через `extract_fingerprint` → серии/пары/тиры; покрытие лексикона на реальной ленте

### Task 2: Pair-aware [E015] + broad [E014] builders (`admin_alerts.py`)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer

### Task 3: Калибровочная фикстура — 8 пар (`tests/fixtures/cross_source_dedup_pairs.py`)
- **Skill:** code-writing
- **Reviewers:** test-reviewer

## Wave 2 (зависит от Wave 1 — параллельно)

### Task 4: Toggle + tiered gate refactor (`news_bot.py`) — dep [1,2]
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_integration.py::TestCrossSourceDedup`

### Task 5: Backfill widened re-select (`backfill_fingerprints.py`) — dep [1]
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer

## Wave 3 (зависит от 1,3,4)

### Task 6: Калибровочный тест точности (pair-tier) + деплой-раннбук — dep [1,3,4]
- **Skill:** code-writing
- **Reviewers:** test-reviewer

## Audit Wave (зависит от 1–6 — параллельно, reviewers: none, только анализ)

### Task 7: Code Audit  → logs/working/task-7/code-audit.md
### Task 8: Security Audit → logs/working/task-8/security-audit.md
### Task 9: Test Audit → logs/working/task-9/test-audit.md

Если аудит нашёл blocker/major → lead спавнит fixer (code-writing), аудиторы становятся ревьюерами (макс 3 раунда).

## Final Wave (зависит от 7,8,9)

### Task 10: Pre-deploy QA — dep [7,8,9] → logs/working/qa-report.json
### Task 11: Post-deploy verification (operator-applied) — dep [10]

## Проверки, требующие участия пользователя

- [ ] Task 1: глазами сверить покрытие лексикона на реальной ленте (smoke)
- [ ] Task 11: операторские шаги на проде (pre-deploy SELECT COUNT → deploy-dark → backfill → toggle on), строго вне окна 10:00–20:00 МСК
- [ ] После всех волн: финальный обзор + решение о деплое
