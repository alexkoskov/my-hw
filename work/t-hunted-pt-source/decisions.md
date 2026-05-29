# Decisions Log: t-hunted-pt-source

Agent reports on completed tasks. Each entry is written by the agent that executed the task.

---

<!-- Entries are added by agents as tasks are completed.

Format is strict — use only these sections, do not add others.
Do not include: file lists, findings tables, JSON reports, step-by-step logs.
Review details — in JSON files via links. QA report — in logs/working/.

## Task N: [title]

**Status:** Done
**Commit:** abc1234
**Agent:** [teammate name or "main agent"]
**Summary:** 1-3 sentences: what was done, key decisions. Not a file list.
**Deviations:** None / Deviated from spec: [reason], did [what].

**Reviews:**

*Round 1:*
- code-reviewer: 2 findings → [logs/working/task-N/code-reviewer-1.json]
- security-auditor: OK → [logs/working/task-N/security-auditor-1.json]

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-N/code-reviewer-2.json]

**Verification:**
- `npm test` → 42 passed
- Manual check → OK

-->

## Task 1: New parser module + unit tests

**Status:** Done
**Commit:** c12b7e81 (impl) + 98f3693e (round 2 fixes)
**Agent:** teammate (general-purpose, opus) + fixer (general-purpose, opus)
**Summary:** `t_hunted_source.py` (206 LOC) mirrors lamley_source.py minus WAF/throttle apparatus. SSRF allowlist exact-match on `t-hunted.blogspot.com`. Blogger-aware image dedup strips `=s\d+(-c)?` size suffix from path (broader than literal AC regex, documented). 14 unit tests in `tests/test_t_hunted_source.py` (6 TestFetch + 8 TestHostAllowlist). Production code references `admin_alerts.alert_t_hunted_*` builders (Task 2 creates them); test file uses autouse monkeypatch with `[E03N-STUB]` fingerprints.
**Deviations:** (1) Image dedup regex broadened `=s\d+(-c)?$` → `=s\d+(-c)?(?:/|$)` — Blogger places size token mid-path, anchor-only form would miss; documented in source. (2) LOC 206 vs ≤200 target — 6-line overshoot in docstrings, acceptable per AC.

**Reviews:**

*Round 1:*
- code-reviewer: approved_with_suggestions (3 minor, cosmetic) → [logs/working/task-1/code-reviewer-round1.json]
- security-auditor: approved (3 minor, systemic with lamley) → [logs/working/task-1/security-auditor-round1.json]
- test-reviewer: needs_improvement (3 MAJOR + 3 minor) → [logs/working/task-1/test-reviewer-round1.json]

*Round 2 (after fixes):*
- test-reviewer: passed/approved (0 findings, all 3 majors closed) → [logs/working/task-1/test-reviewer-round2.json]

**Verification:**
- `pytest tests/test_t_hunted_source.py -v` → 14 passed
- `pytest tests/ -q` → 947 passed, 2 skipped (no regressions, baseline +12)

## Task 2: Admin alerts E031-E033

**Status:** Done
**Commit:** a318841b
**Agent:** teammate (general-purpose, opus)
**Summary:** Added 3 admin-alert builders to `admin_alerts.py` between E028 (last lamley alert) and E030 (orangetrack aggregator): `alert_t_hunted_host_rejected` (E031, 🟡 SSRF rejection), `alert_t_hunted_fetch_error` (E032, 🟡 HTTP/timeout), `alert_t_hunted_no_body` (E033, 🟡 parser couldn't find body, mentions `<div class="post-body">` selector). Mirrors lamley E025/E027/E028 shape with Russian copy + Ссылка/Ошибка/Что сделать sections. E029 intentionally skipped per code-research §A.5.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved (2 minor cosmetic optional) → [logs/working/task-2/code-reviewer-round1.json]
- test-reviewer: passed (0 findings) → [logs/working/task-2/test-reviewer-round1.json]

**Verification:**
- `pytest tests/test_admin_alerts.py -v` → 29 passed (+3 from 26)
- `pytest tests/ -q` → 950 passed, 2 skipped (baseline +3, no regressions)

## Task 3: news_bot wiring (dispatcher + registries)

**Status:** Done
**Commit:** 9757f72 + 71292e9 (sibling-test hotfix for SOURCE_EMOJI/SOURCE_LABEL expansion)
**Agent:** teammate (general-purpose, opus)
**Summary:** Five atomic edits to `news_bot.py`: `import t_hunted_source`, `NETLOC_TO_SOURCE['t-hunted.blogspot.com'] = 't-hunted'`, `SOURCE_HASHTAG_OVERRIDE = {'t-hunted.blogspot.com': '#thunted'}` with `_source_hashtag` patch (Telegram rejects hyphens in tags), `fetch_full_article` dispatcher branch routing `blogspot.com` netlocs to `t_hunted_source.fetch_t_hunted_article`, plus `SOURCE_EMOJI['t-hunted'] = '🟤'` + `SOURCE_LABEL['t-hunted'] = 'T-Hunted'`. Updated `tests/test_sources_registry.py` for new 8-key / 5-value cardinality and added `TestResolveSourceName::test_resolves_t_hunted`.
**Deviations:** None on the wiring itself. Post-merge sibling-test failures in `tests/test_admin_ping.py::TestSourceVocabulary` (hardcoded 4-entry expected sets) closed by hotfix 71292e9 — extended sets to include `'t-hunted'`.

**Reviews:**

*Round 1:*
- code-reviewer: approved → [logs/working/task-3/code-reviewer-round1.json]
- test-reviewer: approved → [logs/working/task-3/test-reviewer-round1.json]

**Verification:**
- `pytest tests/test_sources_registry.py -v` → all passed (new 8-key + t-hunted resolution tests green)
- `pytest tests/ -q` → 982 passed, 2 skipped (no regressions after hotfix)

## Task 4: Config + boilerplate PT defence-in-depth

**Status:** Done
**Commit:** 2d5417a + 9c36a6c (round 2 fix)
**Agent:** teammate (general-purpose, opus) + fixer (general-purpose, opus)
**Summary:** Added 4th entry to `feeds.json` (`https://t-hunted.blogspot.com/feeds/posts/default?alt=rss`) and appended 10 PT defence-in-depth regex patterns to `boilerplate_filter.py` after the RU block (`^compartilhar`, `^marcadores`, `^postado por`, `^postagem$`, `^enviar por email\b`, `^postagens mais$`, `^assinar`, `^leia mais$`, `^postar um.*comentario$`, `^pagina inicial$`). All patterns `^`-anchored and ReDoS-safe under the 120-char `_MAX_BOILERPLATE_LEN` ceiling. Tests: 16-case positive parametrise, length>120 negative case, ReDoS regression with `(adversarial, expected)` tuples and behaviour-binding assertion, plus an under-120-char inline-`compartilhar` negative case for anchoring guarantee.
**Deviations:** None. Round 1 test-reviewer's suggested expected=True for `enviar por email` case was incorrect (the `\b` boundary fails when followed by word char `x`) — fixer used the actually-correct False and verified all 10 (adversarial, expected) pairs by direct execution.

**Reviews:**

*Round 1:*
- code-reviewer: approved → [logs/working/task-4/code-reviewer-round1.json]
- test-reviewer: needs_improvement (1 MAJOR + 2 MINOR) → [logs/working/task-4/test-reviewer-round1.json]

*Round 2 (after fixes):*
- test-reviewer: passed (all 3 findings closed) → [logs/working/task-4/test-reviewer-round2.json]

**Verification:**
- `pytest tests/test_boilerplate_filter.py -v` → all passed
- `pytest tests/ -q` → 982 passed, 2 skipped (no regressions)

## Task 6: Deploy plumbing (FILES arrays + invariant tests)

**Status:** Done
**Commit:** 93aad9e
**Agent:** teammate (general-purpose, opus)
**Summary:** Added `"t_hunted_source.py"` between `"orangetrack_source.py"` and `"telegraph_publisher.py"` in all three deploy manifests: `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml` (byte-for-byte mirror invariant). Added `tests/test_deploy_files_invariant.py` with 3 tests asserting `"t_hunted_source.py"` is present in each FILES array — guards the recurrent ImportError-on-cron-tick regression.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved → [logs/working/task-6/code-reviewer-round1.json]
- deploy-reviewer: approved → [logs/working/task-6/deploy-reviewer-round1.json]

**Verification:**
- `pytest tests/test_deploy_files_invariant.py -v` → 3 passed
- `pytest tests/ -q` → 982 passed, 2 skipped (no regressions)

