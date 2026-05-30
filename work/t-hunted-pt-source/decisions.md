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

## Task 5: Wiring tests (telegram hashtag + dispatcher unit)

**Status:** Done
**Commit:** ced0422
**Agent:** teammate (general-purpose, opus)
**Summary:** Test-only task — added 2 tests to `tests/test_telegram.py` (`TestSourceHashtag::test_t_hunted_hashtag` exact `'#thunted'`, `TestSendTelegraphTeaser::test_t_hunted_teaser_uses_thunted_tag` exact `'#thunted #news'` byte-for-byte) and created `tests/test_news_bot_dispatcher.py` (pytest-style, 59 LOC) with `test_fetch_full_article_routes_blogspot_to_t_hunted` (patches `news_bot.t_hunted_source.fetch_t_hunted_article`, asserts call_count==1 + args[0]==link + result passthrough) and `test_fetch_full_article_unknown_domain_returns_none`. Production code untouched (T3 wiring already live).
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved (3 minor optional) → [logs/working/task-5/code-reviewer-round1.json]
- test-reviewer: passed (4/4 litmus, 2 minor) → [logs/working/task-5/test-reviewer-round1.json]

**Verification:**
- `pytest tests/test_telegram.py tests/test_news_bot_dispatcher.py -v` → 15 passed
- `pytest tests/ -q` → 991 passed, 2 skipped (no regressions)

## Task 7: ux-guidelines.md PT widening + t-hunted style block + glossary

**Status:** Done
**Commit:** 92d870c
**Agent:** teammate (general-purpose, opus)
**Summary:** 3 surgical edits to `.claude/skills/project-knowledge/references/ux-guidelines.md` (+36 lines net): (1) widened system-prompt input-language sentence to `**входящий текст (английский или португальский)**` per Decision 5 canonical form; (2) added `### 🟤 t-hunted` per-source style block (verbatim from code-research §D, mirrors autoevolution/lamley/mattel structure, `[TBD operator]` markers where deferred); (3) added `## Glossary — PT/EN/RU` section (14-row markdown table from code-research §E, two `[VERIFY operator]` flags). Created `tests/test_ux_guidelines_structure.py` (122 LOC, 4 tests) pinning sentence widening + t-hunted block presence + glossary table cardinality + section ordering.
**Deviations:** None substantive. Block-quote integrity verified: only the widened sentence differs vs baseline.

**Reviews:**

*Round 1:*
- code-reviewer: approved (3 minor optional) → [logs/working/task-7/code-reviewer-round1.json]
- prompt-reviewer: approved_with_suggestions (6 minor polish, none blocking) → [logs/working/task-7/prompt-reviewer-round1.json]

**Verification:**
- `pytest tests/test_ux_guidelines_structure.py -v` → 4 passed (TDD red→green confirmed)
- `pytest tests/ -q` → 991 passed, 2 skipped (no regressions)

## Task 8: Integration smoke EN+PT (dispatch + routing)

**Status:** Done
**Commit:** bb92447 + 0cd8fe0 (round 2 fix)
**Agent:** teammate (general-purpose, opus) + fixer (general-purpose, opus)
**Summary:** Added `test_integration_t_hunted_en_pt_dispatch_and_routing_smoke` to `tests/test_distributed_schedule_integration.py::TestDistributedSchedule`. Reuses class-level harness (`_set_rss_entries`, `freeze_time('2026-05-26 09:00:00')`, base `fetch_full_patcher`). Two RSS entries (EN autoevolution + PT t-hunted blogspot) flow through `_resolve_source_name` → DB row → `transcreate_via_claude` (`side_effect=[en_result, pt_result]`) → publisher. Asserts: published count == 2; `published_articles.source_name` row values == `{'autoevolution', 't-hunted'}` (proves T3 wiring reaches storage); 4 `call_args_list` assertions on EN/PT `link` + `source_name` inside the article dict (pins LLM-payload language differentiation); no E031/E032/E033 admin alerts; outage state untouched.
**Deviations:** Renamed from "mixed tick smoke" to drop "full pipeline" framing — test honestly scopes to dispatch + routing + DB + no-alert paths; real parser path and PT boilerplate filter delegated to T1/T4 unit tests with explicit OUT OF SCOPE block comment.

**Reviews:**

*Round 1:*
- code-reviewer: approved (4 minor optional) → [logs/working/task-8/code-reviewer-round1.json]
- test-reviewer: needs_improvement (2 MAJOR: no `call_args_list` inspection + "full pipeline" framing) → [logs/working/task-8/test-reviewer-round1.json]

*Round 2 (after fix):*
- test-reviewer: passed (both majors closed, signature verified, OUT OF SCOPE block honest) → [logs/working/task-8/test-reviewer-round2.json]

**Verification:**
- `pytest tests/test_distributed_schedule_integration.py -k integration_t_hunted -v` → 1 passed (renamed test green, `-k` filter substring preserved)
- `pytest tests/ -q` → 991 passed, 2 skipped (no regressions)

## Task 9: Code Audit

**Status:** Done
**Commit:** (audit-only, no code change)
**Agent:** code-reviewer auditor (final-state holistic, not diff)
**Summary:** approved_with_suggestions — 0 critical, 0 major, 5 minor (doc/cosmetic only). Cross-task wiring verified coherent end-to-end: parser contract ↔ dispatcher branch ↔ `SOURCE_HASHTAG_OVERRIDE` ↔ `NETLOC_TO_SOURCE` ↔ all 3 deploy `FILES` arrays line up. No shared-resource regressions, no duplicate logic beyond deliberate lamley-mirror baseline. Report → [logs/working/audit/code-audit.json].
**Deviations:** None. Minor follow-ups (non-blocking, deferred to backlog): tech-spec Decision 2 prose refresh, fetch_full_article docstring widening, admin_alerts E030↔E031 ordering note.

## Task 10: Security Audit

**Status:** Done
**Commit:** (audit-only, no code change)
**Agent:** security-auditor (final-state OWASP-style, not diff)
**Summary:** approved — 0 Critical, 0 High, 2 Medium, 1 Low across 15 files. SSRF allowlist is exact-match on `parsed.hostname` (not netloc), strictly matches lamley baseline. All 10 PT regex patterns `^`-anchored with bounded quantifiers, run only under 120-char ceiling — ReDoS-safe. Alert bodies scrubbed by `_redact_text`; logger by `_TokenRedactingFilter`. Report → [logs/working/audit/security-audit.json].
**Deviations:** Medium findings (SEC-002 redirect-follow allowlist bypass, SEC-003 buffered-content-then-cap) are pre-existing systemic gaps INHERITED from lamley baseline — flagged for separate hardening PR, NOT regressions introduced by this feature. SEC-001 (http+https allowlist) similarly inherited. None block Wave 5.

## Task 11: Test Audit

**Status:** Done
**Commit:** (audit-only, no code change)
**Agent:** test-reviewer auditor (final-state AC-coverage + pyramid + litmus, not diff)
**Summary:** passed — 0 critical, 0 major, 6 minor. AC1–AC10 all covered with strong assertions. Pyramid healthy: ~53 unit + 1 integration smoke + 0 E2E. Clean isolation (tempfile DBs, monkeypatch, freeze_time). All 5 litmus probes pass on SSRF/image-dedup/hashtag/dispatcher/PT-boilerplate. No flake risk. Report → [logs/working/audit/test-audit.json].
**Deviations:** Minor non-blocking gaps tracked: no direct invariant test on feeds.json entry (covered indirectly by netloc-map test), deploy-invariant uses substring (not FILES-array membership), autouse `[E03N-STUB]` fixture in `test_t_hunted_source.py` is dead-but-harmless after T2 landed real builders (recommendation: drop stub, bind to real `[E031]/[E032]/[E033]` codes for stronger pinning) — deferred to backlog.

## Task 12: Pre-deploy QA

**Status:** Done
**Commit:** (QA-only, no code change)
**Agent:** qa-runner (pre-deploy-qa skill)
**Summary:** `status: passed`. Full pytest suite 991 passed / 2 skipped / 0 failed (zero regressions vs 935+ baseline, +56 feature tests). 23 acceptance criteria checked: 22 passed, 1 not_verifiable (live RSS connectivity). 0 criticals, 0 majors, 1 minor (t_hunted_source.py 206 LOC vs ≤170 soft target — already accepted by code audit + decisions.md Task 1). Recommendation: PROCEED to Task 13 (Deploy). Full report → [logs/qa/pre-deploy-qa.json].
**Deviations:** None.
**Deferred to post-deploy:** 8 criteria require live verification — handed off to Task 14 in `deferredToPostDeploy` of the JSON report. Headline items: AC_RSS_LIVE_CONNECTIVITY (feedparser.parse on t-hunted RSS), AC_CI_GREEN_ON_PR (after PR open), AC_SERVICE_CLEAN_RESTART (post-deploy systemctl + journalctl), AC_NO_ALERTS_24H ([E031-E033] absence in 24h window), AC_TELEGRAM_VISUAL_RENDER (hero image + #thunted hashtag operator visual), AC_RU_QUALITY_FIRST_PUBLISHES (subjective 5-10 sample review), AC_NO_REGRESSION_OTHER_SOURCES (7-day cadence watch), AC_LLM_BUDGET_25_PCT (30-day billing review).

**Verification:**
- `pytest tests/ -q` → 991 passed, 2 skipped, 0 failed in 13.69s
- All 8 targeted feature suites green (test_t_hunted_source, admin_alerts -k t_hunted, sources_registry, telegram -k thunted, boilerplate_filter -k portuguese, ux_guidelines_structure, distributed_schedule_integration -k integration_t_hunted, deploy_files_invariant)
- Smoke: `_source_hashtag('https://t-hunted.blogspot.com/2026/05/post.html')` → `'#thunted'` exact
- Smoke: `grep -l "t_hunted_source.py" deploy.sh .github/workflows/deploy.yml .github/workflows/deploy_test.yml` → all 3 files in output
- Hard-constraints: no diff against origin/dev for requirements.txt / .env.example / pending_articles_repo.py; no actual imports of curl_cffi / playwright / selenium / headless in feature code
- Full report: [logs/qa/pre-deploy-qa.json]


## Task 13: Deploy (test instance) — COMPLETED WITH INCIDENT

**Status:** Done (with deferred follow-up — see Observations)
**Commit:** 7e967da (squash-merge of PR #12 to dev). No code change in this task; deploy ops only.
**Agent:** main agent (orchestrator) + manual SSH verification authorized by user
**Summary:** PR #12 squash-merged to `dev`; auto-triggered `deploy_test.yml` (run 26679230672) reported success but functionally FAILED — `t_hunted_source.py` was not deployed because `workflow_run` executed main's stale YAML (21-entry FILES array, no `t_hunted_source.py`), while `Checkout` correctly fetched dev's source (with new import). Service crashlooped 38× with `ModuleNotFoundError`. Recovered via manual `gh workflow run deploy_test.yml --ref dev` (run 26679953068) which uses dev's YAML (22-entry FILES). Post-recovery: service active, t_hunted_source.py on server, clean first cron tick, autoevolution publish to Telegram succeeded. Promotion `dev` → `main` DEFERRED to Task 14 sign-off. Full incident report → [logs/deploy/deploy-test.log](logs/deploy/deploy-test.log).
**Deviations:** Deviated from happy path of T13 spec — initial auto-deploy failed due to GitHub Actions `workflow_run` infrastructure limitation. Manual `workflow_dispatch --ref dev` used as documented recovery path (still through CI/CD pipeline, not SSH). SSH was used ONLY for read-only verification of broken service state and post-recovery confirmation — authorized as emergency debugging per CLAUDE.md.

**Observations flagged for Task 14:**
1. **Missing E031/E032/E033 alerts on t-hunted parse failures.** First post-recovery cron tick had 2 t-hunted entries returning `None` (warning `No article data for https://t-hunted.blogspot.com/...`), but no corresponding `[E03x]` admin alert in journal. Either (a) parser returns None without invoking alert builder (logic gap in `fetch_t_hunted_article`), or (b) alert sent but not surfaced in journal. T14 should verify admin Telegram channel received E03x or open hotfix.
2. **No SCP failure signal.** Workflow reported `success` despite functionally broken deploy. The infrastructure-fix needs a sanity check (e.g., grep news_bot.py imports against FILES list at deploy time, OR add post-deploy server-side smoke that imports news_bot in a subshell before restart).

**Recommended infrastructure-fix follow-up (separate PR, before promoting dev → main):**
- **Option A.** Read FILES from a checked-out file (e.g., `deploy_files.list`) instead of hardcoding in YAML — workflow_run still loads main's YAML, but FILES content comes from the checked-out dev SHA. Smallest change, fixes root cause.
- **Option B.** Add a smoke step after Restart: `ssh "python3 -c 'import news_bot'"` — fails fast if any new import is missing.
- **Option C.** Cherry-pick the deploy_test.yml change to main as a separate infrastructure PR BEFORE feature merges — fragile (requires discipline every time FILES changes), not recommended.
- Author's pick: Option A + Option B together. Option B is a defence-in-depth backstop that catches any future FILES drift.

## Task 14 (in progress): Hotfix — publish photo-gallery posts

**Status:** In progress (24h watch ongoing)
**Hotfix commit:** a0ae3f8 (squash-merge of PR #13 to dev)
**Hotfix dispatch deploy:** GitHub Actions run 26682318000 (workflow_dispatch --ref dev, 22 files copied)
**Agent:** main agent (during T14 post-deploy watch)
**Summary:** Discovered during T+0 watch: 2026-05-30 cron tick saw 2 t-hunted RSS entries, both silently skipped via the "thin-body" path (paragraphs=[] after subtitle lift). User confirmed product intent — these photo-gallery posts (single intro paragraph + product photos) are the dominant t-hunted format and must publish so subscribers learn about new releases. Hotfix scope: t_hunted_source.py only — conditional subtitle lift (skip when fewer than 2 paragraphs survive boilerplate filter) + _IMAGE_LIMIT raised 10 → 30. Lamley unchanged (review-style posts always carry 2+ paragraphs). New test test_single_paragraph_post_keeps_paragraph_in_body_with_empty_subtitle pins behavior.
**Deviations:** Required a second deploy iteration during T14 watch. Auto-triggered deploy_test.yml hit the same workflow_run/FILES drift as Wave 6 — service stayed on previous parser code; recovery via gh workflow run deploy_test.yml --ref dev. Total deploy time including workflow queue: ~3 min. Service uptime since recovery (verified via user-pasted SSH output at 14:46 МСК / 12:46 BST): 33 min, no restarts, CLEAN grep on E031/E032/E033/Traceback/ImportError.

**Verification (T+35 min from hotfix deploy):**
- pytest tests/ -q → 992 passed, 2 skipped, 0 failed (PR #13 baseline, +1 new test vs 991)
- Suite at PR #13 CI run 26682163371 → success
- File mtime on /home/hwbot/bot_test/t_hunted_source.py = 12:11 BST = 14:11 МСК (recovery deploy timestamp)
- File size 8701 bytes (was 7920 — hotfix added 781 bytes of code + comments)
- journalctl grep CLEAN since deploy timestamp
- Today's tick slots 1/4 + 2/4 published before recovery (autoevolution Car Culture 12:06, autoevolution Legends Tour 13:36) — visible on @myhwchannel123, no t-hunted yet because today's 2 t-hunted entries were already processed-as-skipped before hotfix landed
- Next opportunity for t-hunted publish: tomorrow 10:00 МСК (2026-05-31) natural cron tick

**Pending T14 acceptance criteria:**
- AC1 24h E03x grep: deferred to 2026-05-31 ~10:11 МСК
- AC2 first t-hunted publish with hashtag + hero: BLOCKED on next RSS poll bringing a new t-hunted entry (could be tomorrow's tick or later, depending on t-hunted publishing cadence)
- AC3 RU translation no PT leakage: BLOCKED on AC2
- AC4 5-post quality spot-check: BLOCKED on accumulating ≥5 publishes (could take all 7 days or longer)
- AC5 7-day cumulative E03x: ongoing
- AC6 no regression in other sources: ongoing (slots 1/4 + 2/4 today are autoevolution, healthy signal)
