# Execution Plan: dedup-review-buttons

**Создан:** 2026-07-22

Среда: командный механизм (TeamCreate) недоступен — та же схема реализуется
фоновыми агентами; лид передаёт диффы ревьюерам и замечания исполнителям
(SendMessage), максимум 3 раунда ревью на задачу. Коммиты — локально по ходу;
финальный push — только с согласия оператора.

---

## Wave 1 (независимые, параллельно)

### Task 1: DB concurrency guard + review-token store
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** python -c round-trip put/get/delete review token

### Task 2: Keyboard builder + reply_markup forwarding
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** python -c keyboard callback_data == ['dd:c:t','dd:k:t']

## Wave 2 (зависит от Wave 1) — ПОСЛЕДОВАТЕЛЬНО (общие файлы!)

Оба правят news_bot.py + tests/test_integration.py — cross-task валидация
требует применять по очереди: сначала Task 4, затем Task 3 (порядок безопасен
в обе стороны; 4 раньше — логика без send-site, меньше конфликт-поверхность).

### Task 4: Callback decision logic (resolve_dedup_callback)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer

### Task 3: Attach keyboard at E014 send site (flag-gated)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer

## Wave 3 (зависит от Wave 2, параллельно)

### Task 5: Background review listener + main() wiring
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** python -c hasattr(news_bot,'_run_review_listener')

### Task 6: Config + Project Knowledge docs
- **Skill:** documentation-writing
- **Reviewers:** code-reviewer

## Wave 4 — Audit Wave (параллельно, reviewers: none)

### Task 7: Code Audit — skill code-reviewing
### Task 8: Security Audit — skill security-auditor
### Task 9: Test Audit — skill test-master

Отчёты → logs/working/task-{7,8,9}/. Замечания → ad-hoc fixer + аудиторы
как ревьюеры.

## Wave 5

### Task 10: Pre-deploy QA — skill pre-deploy-qa (полный suite + все AC)

## Wave 6

### Task 11: Deploy runbook (operator-applied) — skill deploy-pipeline
Claude готовит команды; оператор применяет. Вне окна 10:00–20:00 МСК.

## Wave 7

### Task 12: Post-deploy verification (operator-driven) — skill post-deploy-qa

## Проверки, требующие участия пользователя

- [ ] Task 11: оператор применяет деплой (dev→test авто; прод — руками, вне окна,
      + REVIEW_BUTTONS_ENABLED=1 и числовой TELEGRAM_ADMIN_ID в прод .env)
- [ ] Task 12: оператор жмёт кнопки на живом [E014] на проде; проверка логов
- [ ] Финальный push в origin — только после «го» оператора
