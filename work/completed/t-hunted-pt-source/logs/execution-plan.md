# Execution Plan: t-hunted-pt-source

**Создан:** 2026-05-26
**Source:** tech-spec.md (approved) + 14 task files (validated, approved)
**Team:** `t-hunted-pt-source` (to be created via TeamCreate)
**Branch:** `feature/t-hunted-pt-source`

---

## Wave 1 (foundation, parallel)

### Task 1: New parser module + unit tests
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `pytest tests/test_t_hunted_source.py -v`
- **Files:** `t_hunted_source.py` (new), `tests/test_t_hunted_source.py` (new)

### Task 2: Admin alerts E031-E033
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Verify-smoke:** `pytest tests/test_admin_alerts.py -k t_hunted -v`
- **Files:** `admin_alerts.py`, `tests/test_admin_alerts.py`

## Wave 2 (parallel, depends on Wave 1)

### Task 3: news_bot.py wiring + sources_registry tests
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Depends on:** Task 1
- **Verify-smoke:** `python -c "from news_bot import _source_hashtag, _resolve_source_name; ..."` → `#thunted t-hunted`; `pytest tests/test_sources_registry.py -v`
- **Files:** `news_bot.py`, `tests/test_sources_registry.py`

### Task 4: feeds.json + boilerplate_filter PT patterns
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Depends on:** none (independent of Wave 1)
- **Verify-smoke:** `pytest tests/test_boilerplate_filter.py -k portuguese -v`
- **Files:** `feeds.json`, `boilerplate_filter.py`, `tests/test_boilerplate_filter.py`

### Task 6: Deploy plumbing + invariant test
- **Skill:** code-writing
- **Reviewers:** code-reviewer, deploy-reviewer
- **Depends on:** Task 1 (file must exist)
- **Verify-smoke:** `pytest tests/test_deploy_files_invariant.py -v` + grep across 3 deploy files
- **Files:** `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml`, `tests/test_deploy_files_invariant.py` (new)

## Wave 3 (parallel, depends on Wave 1-2)

### Task 5: Telegram + dispatcher unit tests
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Depends on:** Task 3
- **Verify-smoke:** `pytest tests/test_telegram.py tests/test_news_bot_dispatcher.py -v`
- **Files:** `tests/test_telegram.py`, `tests/test_news_bot_dispatcher.py` (new)

### Task 7: ux-guidelines.md prompt update + structural test
- **Skill:** prompt-master
- **Reviewers:** code-reviewer, prompt-reviewer
- **Depends on:** none (independent .md edit)
- **Verify-smoke:** `pytest tests/test_ux_guidelines_structure.py -v` + 3 grep checks
- **Files:** `.claude/skills/project-knowledge/references/ux-guidelines.md`, `tests/test_ux_guidelines_structure.py` (new)

### Task 8: Integration smoke EN+PT mixed tick
- **Skill:** code-writing
- **Reviewers:** code-reviewer, test-reviewer
- **Depends on:** Tasks 1, 3
- **Verify-smoke:** `pytest tests/test_distributed_schedule_integration.py -k integration_t_hunted -v`
- **Files:** `tests/test_distributed_schedule_integration.py`

## Wave 4 (audit, parallel, depends on all impl)

### Task 9: Code Audit
- **Skill:** code-reviewing
- **Reviewers:** none (auditor IS the review)
- **Depends on:** Tasks 1-8
- **Output:** `work/t-hunted-pt-source/logs/audits/code-audit.json`

### Task 10: Security Audit
- **Skill:** security-auditor
- **Reviewers:** none
- **Depends on:** Tasks 1-8
- **Output:** `work/t-hunted-pt-source/logs/audits/security-audit.json`

### Task 11: Test Audit
- **Skill:** test-master
- **Reviewers:** none
- **Depends on:** Tasks 1-8
- **Output:** `work/t-hunted-pt-source/logs/audits/test-audit.json`

## Wave 5 (depends on Wave 4)

### Task 12: Pre-deploy QA
- **Skill:** pre-deploy-qa
- **Teammate name:** qa-runner
- **Reviewers:** none
- **Depends on:** Tasks 9, 10, 11
- **Verify-smoke:** full pytest suite + AC traceability matrix
- **Output:** `work/t-hunted-pt-source/logs/qa/pre-deploy-qa.json`

## Wave 6 (depends on Wave 5)

### Task 13: Deploy
- **Skill:** deploy-pipeline
- **Teammate name:** deployer
- **Reviewers:** none
- **Depends on:** Tasks 6 (FILES list), 12 (QA gate)
- **Verify-smoke:** PR → CI → deploy_test.yml → news_bot_test.service active
- **Output:** `work/t-hunted-pt-source/logs/deploy/deploy-test.log`

## Wave 7 (depends on Wave 6)

### Task 14: Post-deploy verification
- **Skill:** post-deploy-qa
- **Reviewers:** none
- **Depends on:** Task 13
- **Verify-smoke + user:** ssh + journalctl + visual operator checklist on test channel
- **Output:** `work/t-hunted-pt-source/logs/qa/post-deploy-qa.json`

---

## Проверки, требующие участия пользователя

- [ ] **Task 7** (`prompt-master`): после первых публикаций оператор уточнит per-source style block для t-hunted в отдельном PR (deferred, не блокирует приёмку).
- [ ] **Task 13** (`deploy`): пользователь подтверждает PR merge `feature → dev`, наблюдает CI status; промоушн `dev → main` остаётся на пользователе после успешной Task 14.
- [ ] **Task 14** (`post-deploy-qa`): визуальный чек на `@myhwchannel123` — первый t-hunted пост с превью, хештег `#thunted`, RU перевод чистый. Оператор подтверждает 5-10 публикаций качества ≥ autoevolution baseline. 7-day stability watch.

---

## Risk envelope

- 8 implementation tasks (Wave 1-3) — изменения по 12-15 файлам в `feature/t-hunted-pt-source` ветке. Локально, до deploy не идёт в production.
- 3 audits + pre-deploy QA — analysis-only + test runs. Без production effects.
- Task 13 (deploy) — first prod-affecting action, **gated by Task 12 QA approval**. Triggers deploy_test.yml, test-instance restart with new code. Промоушн на prod (`dev → main`) — отдельный явный шаг пользователя в Task 14 после положительной верификации.

---

## Team size estimate

- 8 teammates for impl tasks (Wave 1-3) + ~16 reviewers (parallel within tasks) = peak ~24 agents in Wave 1-3 stretch
- 3 auditors (Wave 4)
- 3 final-wave teammates (Wave 5-7)
- Total expected agent runs: 8 impl × (1 teammate + 2-3 reviewers + up to 3 review rounds) + 3 audits + 3 final ≈ 30-50 agent runs over the feature lifetime
- Walltime: hard to estimate — single-task can take 5-30 minutes; full feature execution likely several hours of clock time
