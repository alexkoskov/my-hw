# Execution Plan: llm-transcreation-and-distributed-publishing

**Создан:** 2026-04-27

**Scope:** Waves 1–10 (implementation + audits + pre-deploy QA). **Waves 11 (deploy) и 12 (post-deploy verification) НЕ выполняются** в этой сессии — оператор запустит их вручную после CI setup + Anthropic API key.

---

## Wave 1 (независимые foundations) — 4 task'и параллельно

### Task 01: Add `bot_state` migration
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files:** `pending_articles_repo.py`, `tests/test_migration.py`

### Task 02: Create `compute_publish_slots.py` + tests
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Files:** `compute_publish_slots.py` (new), `tests/test_compute_publish_slots.py` (new)

### Task 04: Extend `_TokenRedactingFilter` for ANTHROPIC_API_KEY (3-layer)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor
- **Verify-smoke:** synthetic anthropic-key in log/admin-notify → redacted

### Task 06: Update `requirements.txt` + `.env.example`
- **Skill:** code-writing
- **Reviewers:** code-reviewer
- **Files:** `requirements.txt`, `.env.example`

## Wave 2 (зависит от Wave 1) — 2 task'и параллельно

### Task 03: Create `claude_transcreation.py` + tests (deps: 6)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer, prompt-reviewer
- **Verify-smoke:** invoke transcreate_via_claude with sample article — valid RU dict <30s

### Task 05: Create `outage_state.py` + tests (deps: 1)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer

## Wave 3 — 1 task

### Task 07: Refactor `_fallback_publish` for Claude primary + Google per-article fallback (deps: 3, 5)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** mocked Claude success → Telegraph+teaser; mocked ClaudeTranscreationError → Google fallback chain

## Wave 4 — 1 task

### Task 08: Refactor `job()` for distributed-publish loop + cron change (deps: 2, 3, 5, 7)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** crash-loop guard test; TZ-aware schedule.Job.at; integration 3-article publish at slot times

## Wave 5 — 1 task

### Task 09: Delete legacy auto-publish code + env vars (deps: 8)
- **Skill:** code-writing
- **Reviewers:** code-reviewer

## Wave 6 — 1 task

### Task 10: Strip bureaucratic regex + 4000-char truncation from `transcreate_text` (deps: 9)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer

## Wave 7 (тесты) — 2 task'и параллельно

### Task 11: Update integration tests for new auto-publish path (deps: 10)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer

### Task 12: Create `tests/test_distributed_schedule_integration.py` (deps: 10)
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer

## Wave 8 — 1 task

### Task 13: Update deploy bundle (deploy.sh + GitHub Actions + PK docs) (deps: 11, 12)
- **Skill:** deploy-pipeline
- **Reviewers:** code-reviewer, security-auditor, deploy-reviewer
- **Verify-smoke:** dry-run deploy: ux-guidelines.md + 3 new modules land at expected location; env file has new vars

## Wave 9 (Audit) — 3 task'и параллельно (auditor IS the review)

### Task 14: Code Audit (deps: 13)
- **Skill:** code-reviewing
- **Reviewers:** none (auditor)
- **Output:** `logs/audit/code-audit.md`

### Task 15: Security Audit (deps: 13)
- **Skill:** security-auditor
- **Reviewers:** none (auditor)
- **Output:** `logs/audit/security-audit.md`

### Task 16: Test Audit (deps: 13)
- **Skill:** test-master
- **Reviewers:** none (auditor)
- **Output:** `logs/audit/test-audit.md`

## Wave 10 (Pre-deploy QA) — 1 task

### Task 17: Pre-deploy QA (deps: 14, 15, 16)
- **Skill:** pre-deploy-qa
- **Reviewers:** none (QA agent)
- **Verifies:** AC1-AC31 from user-spec + 10 technical ACs from tech-spec
- **Smoke:** `pytest tests/ -q` green; live Claude API call ~$0.01; redaction synthetic test
- **Output:** `logs/qa/pre-deploy-qa.md`

---

## Wave 11–12 — НЕ ВЫПОЛНЯЮТСЯ В ЭТОЙ СЕССИИ

Оператор запустит вручную после:
1. SSH key + GitHub Secrets (`SSH_HOST`, `SSH_USER`, `DEPLOY_PATH`, `SSH_PRIVATE_KEY`)
2. Anthropic API key (console.anthropic.com → top up $5 → Secrets `ANTHROPIC_API_KEY`, `TZ=Europe/Moscow`)

---

## Проверки, требующие участия пользователя

- [ ] Task 04: оператор подтверждает что в логах не видит `sk-ant-*` (Wave 1)
- [ ] Task 03: оператор может запустить smoke в dev container (Wave 2) — но это локальный smoke, requires ANTHROPIC_API_KEY в .env
- [ ] Task 17 (pre-deploy QA): оператор просматривает QA report перед сменой ветки на main (Wave 10)
- [ ] После Wave 10: оператор делает manual code review на ветке dev перед запуском Wave 11–12
