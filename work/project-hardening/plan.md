---
status: complete
revision: 2
---

# Project hardening and correctness plan

Owner request, 2026-11-03: verify the Docker-context, privilege and resolved-
dependency findings; write a remediation plan; have agents challenge it; revise
it and begin implementation. That request authorized local implementation and
isolated verification. The subsequent owner request explicitly authorizes push,
PR into `main` and merge after green CI. Production mutation and GitHub settings
remain outside this work.

Baseline: `b2cecbedaf5082882c49694db201ffc7ec608d23`; `dev` and `main` matched.
No unrelated working-tree changes were present. The existing STAB/OBSV roadmap
items keep their own scope and acceptance; this plan does not declare them done.

## Verified starting evidence

- Full local baseline: 2128 passed, 2 skipped, 556 subtests passed (24.39 s).
- Built the baseline Dockerfile with a temporary pip CA mount only; application
  contents and dependency requirements were unchanged. Synthetic SQLite-named
  sentinels in `backups/` and `backups/nested/` appeared in `/app`; root and
  `data/` sentinels were excluded. No real database or secret was used.
- Baseline image runs as uid 0 and contains no `curl`. Resolved packages are in
  [baseline-image.json](logs/baseline-image.json); Requests is 2.32.3.
- `pip check` passed. The actual-image dependency audit found two Requests
  advisories, fixed from 2.32.4 and 2.33.0 respectively (duplicate alias records
  are not independent vulnerabilities). `PYSEC-2022-252` also flags
  deep-translator 1.11.4: its primary advisory explicitly lists malicious
  1.8.5 but contains an unclosed range. Preserve this triage distinction; do not
  claim that 1.11.4 is proven compromised or silently ignore all advisories.
- A disposable derived image with readable application paths retained a route
  installed at root startup, then ran Python as uid/gid 10001 with effective and
  bounding capabilities zero and NoNewPrivs=1. SQLite and prompt reading worked;
  changing the route was denied. A recreated container read the same test ledger.
  This isolated internal network does not establish production VPN health.
- Simply setting a non-root uid on the original image failed to read `prompts/`
  because build-context directories were private. Permission normalization,
  writable state/lock locations and existing-volume ownership are rollout needs.

## Scope and invariants

Fix the reviewed defects with behavioral regression evidence. Preserve the
editorial prompt, provider choices, fixed schedule, daily cap, held-article
policy, dedup thresholds, captured fixtures and historical work evidence.
No new reminder feature, bulk history cleanup, production backfill, deployment,
branch-protection change or unmeasured performance refactor is included.
Use synthetic secrets, temporary databases and mocked transports. Never start
the publishing main loop for verification.

## Revised work packages

| ID | Outcome and mutation boundary | Acceptance |
| --- | --- | --- |
| H1 | Runtime: `news_bot.py` and targeted tests. Cache completed translation/publish stages; classify envelope outages consistently via H2; enforce actual publish deadline; stage E014 rows before issuing buttons; redact final formatted tracebacks; production startup must reject missing/unexpected ledger before external calls. | Retry does not repeat translation or change published text; no late irreversible send; immediate callback sees its row; fake secrets absent from handler output; missing production ledger prevents external calls. |
| H2 | LLM shared parser and four providers; Telegraph secure token persistence; targeted tests. Normalize rejected `blocks`; reject unalignable multi-paragraph structured output while preserving tolerant flat text and the accepted single-intro/empty-RU gallery case. | Invalid nonempty blocks use original structure or fail explicitly; gallery media survive without EN placeholder; no partial multi-paragraph translation is published; outage envelopes don't consume strikes; `.env` writes are private and atomic. |
| H3 | Repository/model/DOM: `pending_articles_repo.py`, `model_extractor.py`, `dom_blocks.py`, related tests. Preserve fingerprint AND RU stage fields across failed/retry with additive migrations; recognize adjacent models; coordinate alias-title fix with H1; preserve anchor whitespace and nested breaks. | Real SQLite lifecycle retains fingerprint, translation and URL; legacy NULL rows still work; expected alias/adjacent-model verdict changes are explicit; existing precision corpus remains checked; source golden/alignment tests pass. |
| H4 | Network: source adapters and a bounded shared transport helper if needed; dependencies and related tests. Validate each redirect, bound decoded bytes and wall-clock read time; update vulnerable resolved dependencies without disabling proxy/TLS. | Forbidden redirect is never requested; chunked/compressed/slow responses are bounded; both HTTP backends remain compatible; resolved image dependency audit is reviewed. |
| H5 | Delivery: Dockerfile/Compose/entrypoint, `.dockerignore`, watchdog/prod_check, CI/uptime workflow and their tests. Exclude nested state; bootstrap route then drop uid/caps; fail safely on unusable existing volume; make alert delivery and UNKNOWN health honest; remove unsafe CI skip. | Real image excludes sentinels, has required commands, retains route and persistent fixture ledger under uid!=0/caps=0; failed notification retries; all changed code triggers tests. |
| H6 | Current documentation by the documentation implementer; handoff, plan, tracker and integration evidence by root. Remove broad discard; reconcile deployment policy/prompt checks; correct work index/test descriptions/Codex references; keep historical files. | One current handoff and deployment policy; linked skills exist; no stale active-failure claim; tracker reflects only verified completion. |

H2/H3/H4/H5 can proceed independently after plan criticism. H1 owns
`news_bot.py`; other implementers request its changes rather than editing it.
H5 owns Docker/scripts/workflows; H6 integrates records and shared docs once.
Tests should fail for the reported behavior on baseline, not merely mirror code.

## Integration and stop condition

Run package-specific checks, then one integrated full suite after all relevant
changes. Build the final image; inspect actual packages and vulnerabilities;
verify isolated route/drop, persistent SQLite, excluded secrets/backups, prompt
loaders and imports. Validate documentation links, tracker, shell and diff.
No performance promises: measure before deciding indexes/caches/retention.
Finish with all in-scope confirmed defects fixed and checked, or explicitly
record a concrete unresolved blocker without silently broadening scope.

## Criticism and revisions

Revision 1 was challenged by three independent agents: reliability, security,
and verification/operations. The owner already authorized implementation after
this revision; no new approval gate is inferred from the planning templates.

Revision 2 incorporates their required corrections:

1. Preserve thin galleries (one source intro, empty RU paragraphs, media/title
   retained). Other unalignable structured responses fail explicitly; flat
   segmentation remains tolerant. Inspect actual rendered nodes, not only JSON.
2. Persist cleaned RU stage before page creation, preserve it with fingerprint
   in failed/retry, and reuse it after restart. External response loss still
   prevents an exactly-once guarantee; do not promise one.
3. Deadline has a distinct deferred outcome, with no attempt/hold increments.
   Do not BEGIN an irreversible send after 20:00; successful send bookkeeping
   may finish afterwards. Check LLM completion and retry delays against clock.
4. E014 order is committed pending -> token -> confirmed send -> rate-limit
   acknowledgment. Cover failed insert/send and callback during sender execution;
   never reinsert a row removed by that callback.
5. H1 exclusively owns `news_bot.py` and `tests/test_integration.py`; H2 owns
   shared/provider/Telegraph tests; H3 owns repo/model/DOM tests; H4 owns source
   and HTTP helper tests; H5 owns scripts/container/workflow tests. New test files
   must be domain-specific. H3 owns additive RU/fingerprint DDL and move contracts.
6. Container contract: Compose pins `DB_FILE=/data/news.db`, production label
   and `NEWS_BOT_LOCK_FILE=/data/.news_bot.lock`. H1 reads the lock override and
   performs production preflight before `init_db` or external calls. H5 owns
   root route bootstrap -> uid/gid 10001 -> zero caps/NoNewPrivs, readable
   root-owned code, and fail-fast checks of existing state permissions. Never
   recursively chown a volume. Existing ownership migration is a documented
   deployment prerequisite, not a production operation in this task.
7. Redact final formatted output, including cached traceback/stack_info and exact
   configured opaque secrets; preserve useful traceback length and fail closed.
   `.env` persistence uses same-directory private temp + atomic replace, refuses
   symlinks, preserves old content on failure and reports persistence honestly.
8. H4 separates byte bounds, body deadline and full-HTTP timeouts. For cffi,
   prefer bounded callback under its existing total timeout over unbounded stream
   queue. For Requests, use bounded raw reads and an explicit short inactivity
   timeout/body deadline; do not claim a strict total HTTP deadline for slow
   headers/chunk framing. Verify with a local trickle/stall server and bounded
   compressed input; preserve TLS, proxy and impersonation.
9. Remove CI skip rather than build a new diff classifier. Execute uptime shell
   transitions with stub transports including HTTP200 `{ok:false}`, missing
   credentials, failed recovery and successful retry. Keep tri-state semantics.
10. Final Docker gate uses the actual committed entrypoint and Compose-equivalent
    settings, prepared and incompatible legacy volumes, all capability sets,
    lock contention, sentinel exclusions including nested WAL/SHM and fake env,
    image/base identity and actual dependency audit. Execute the existing shell
    backup script and validate restored ledger/integrity on temporary data.

Deferred proposals remain: production backfill, existing STAB/OBSV feature work,
branch-protection changes, global dependency locking, retention, indexing and
large transport/process frameworks. They are not prerequisites for these fixes.

## Execution outcome — 2026-11-03

All six work packages and integrated acceptance are complete. Six implementers
worked inside non-overlapping file boundaries after the three plan critics;
root integrated and checked the result. Following the owner's publication
request, seven scoped commits were pushed at `ecdaea4` and
[PR #48](https://github.com/alexkoskov/my-hw/pull/48) opened into `main`.
Its current head, CI and merge status record the final promotion outcome.
GitHub settings, production state and deployment remain unchanged.

- Full integrated suite: **2276 passed, 3 skipped, 566 subtests passed**.
  Initial integration exposed two Mattel fixtures lacking a real HTTP status;
  their successful-response fixtures now explicitly specify 200. Assertions
  were preserved, the focused file passed, then the full suite passed.
- Final image's real entrypoint passed uid/gid 10001, all five capability sets
  zero, NoNewPrivs=1, root-owned non-writable code, four prompt loaders,
  shared lock contention, persistent ledger after recreate, retained route,
  denied route mutation and state/secret exclusions. Missing DB and incompatible
  legacy ownership refused to start the worker.
- Actual shell backup and restore passed with SQLite CLI in a separate QA
  image; runtime dependencies were not expanded for the test.
- Requests 2.34.2 resolves both baseline Requests advisories. Final `pip check`
  passed; the audit's one remaining disputed deep-translator advisory is
  classified explicitly in [verification.md](verification.md).
- Native Codex found all seven required skills and seven project-role
  registrations without starting a model turn. Configured pre-commit checks
  and an additional scan of intended added content passed.

Details, image/base identity, actual dependency manifests, source hashes and
remaining limits: [verification.md](verification.md). Production ownership
migration is an explicit prerequisite for a later authorized rollout.
