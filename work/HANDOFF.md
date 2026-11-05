# Current project handoff

Last factual update: **2026-11-05 (Europe/Moscow)**.
Read this file first to resume. Record ownership is described in [the work index](README.md).

## Where work stopped

- **Owner goal completed:** production deployment and automatic GitHub deployment
  are working. [Final workflow](https://github.com/alexkoskov/my-hw/actions/runs/42016189442)
  is green, including automatic release-tag creation.
- **Production:** exact main `77f82f611b81d30b8aba8db667cf35fd85a5f4e6`, tag
  `prod-2026-11-05-77f82f6`. Actual rootless startup, route, capabilities and
  preserved SQLite ledger passed. Data, backups and existing .env were preserved.
  Backup/rollback references and verification limits are in the
  [completed deployment record](project-hardening/deployment.md#successful-automatic-production-release--2026-11-05).
- **Code/publication:** PRs #50–53 are merged. Main stays at the deployed revision;
  factual closeout records belong on dev only and do not require another deploy.
  `/workspace/my-hw-deploy-recovery` was used as the clean release checkout to
  preserve the original checkout's pending records. Final synchronization must
  retain those exact records and all pre-existing branch history.
- **Prior report resolved:** the T-Hunted card loaded after a delay. The proposed
  extra-link experiment was removed; existing post format is preserved. No
  incident repair, repost or formatting decision is pending.

## Exact next step

No deployment, credential setup, artifact download or restart is pending.
Future authorized main releases deploy after actual green push-CI. Do not reopen
resolved blockers from the dated history below or repeat the whole-project audit.

Continue another roadmap item only when selected by the owner. **DBP-015** still
needs classification of its E014 pair in the
[post-deploy report](dedup-broad-precision/logs/working/post-deploy-qa-report.json).
**STAB-002** and **OBSV-002** retain their remaining acceptance. The tracker owns
status/progress; deployment completion does not complete those separate items.

## Verification and limits

- Final release: **2343 CI tests passed, 2 skipped, 566 subtests**. Focused
  deployment suite: 67 passed; tag shell tests: 5 passed. Full real deployment,
  encrypted backups and automatic suffix tag passed on exact main 77f82f6.
- The remaining dated results describe earlier verification stages, not current
  deployment blockers. The deployment record retains correction history.
- Intermediate verification on **2026-11-05:** configured SSH and real Actions image checks passed. PR #50
  passed 48 focused tests and full CI (2319 passed, 2 skipped, 566 subtests).
  Actual host preparation refused local checkout changes before runtime rollout;
  the uploaded sanitized failure artifact confirms that exact blocker.
- Original automatic deployment local suite (2026-11-03): **2316 passed, 3 skipped, 566 subtests
  passed**. Real backup encryption and offline rollback/gate tests passed;
  actionlint and Bash syntax passed. PR CI: 2317 passed/2 skipped/566 subtests;
  main push CI passed. The real deployment gate passed; SSH/deploy were skipped
  due to unavailable dedicated secrets, so no live health claim is possible.
- Full local suite on the final runtime: **2276 passed, 3 skipped;
  566 subtests passed**. Two source skips are pre-existing; the third requires
  host SQLite CLI, whose actual shell backup/restore passed in a QA container.
- The final image's real entrypoint and recreate passed uid/gid 10001, all
  capability sets zero, NoNewPrivs, route retention/denied mutation, persistent
  ledger/integrity, lock contention, four prompt loaders and state/secret
  exclusions. Missing DB and incompatible legacy volume refuse worker startup.
- Requests is 2.34.2; final pip check passed. Both Requests advisories are fixed.
  One disputed upstream deep-translator advisory remains explicitly triaged;
  see the raw audit and limitations in verification. No clean-audit claim.
- Native Codex found all seven required skills and seven role registrations
  without a model turn. Five ignored project aliases point to shared global
  skills; no skill copies or model-policy changes were added.
- Configured scoped pre-commit checks and an additional intended-content
  Gitleaks scan passed. Editorial prompt, fixtures and archives are preserved.
- Requests body bounds do not establish a strict slow-header HTTP deadline;
  external response loss still precludes exactly-once publication guarantees.
  No production backfill, deployment or performance claim was made.
- Roadmap status and exact weighted progress belong to
  [the tracker](../.project-progress.json), not this routing snapshot.

## Outcome history

- **2026-10-14:** Codex migration completed locally; baseline dedup failures remain
  separate. Evidence: [record](SESSION-2026-10-14.md).
- **2026-10-14:** Owner requested one reliable continuation point. Added this
  persistent snapshot and work index; replaced daily-session creation with
  updates at substantive outcomes and pauses. Historical records retained.
- **2026-10-14:** Pushed validated Codex and documentation changes to `dev` and
  opened [PR #42](https://github.com/alexkoskov/my-hw/pull/42). All configured
  pre-commit hooks passed. Product work remains DBP-015; PR status records the
  final promotion outcome. No production deployment was requested.
- **2026-10-14:** Owner declined merging with the two known CI failures and
  requested their repair first. Corrected the E014 cap explanation and stale
  hard-block fixture; preserved dedup verdicts and thresholds. Local full suite:
  2128 passed, 2 skipped. Continue the authorized PR #42 merge after green CI.

- **2026-11-03:** Completed the owner-requested audit follow-up, three-agent
  plan criticism, revised plan and six-domain local implementation. Final full
  suite 2276 passed/3 skipped; actual rootless entrypoint/recreate, synthetic
  state exclusions, backup/restore, dependency audit and native Codex checks
  recorded in [verification](project-hardening/verification.md). Changes remain
  uncommitted on `dev`; no publishing or production work performed.

- **2026-11-03:** Owner authorized push, PR into `main` and merge. Pushed seven
  scoped commits at `ecdaea4` and opened [PR #48](https://github.com/alexkoskov/my-hw/pull/48).
  All commit hooks passed. Runtime/image hashes still match the recorded
  verification; finish promotion after the latest-head test job passes.
  PR status records the final merge outcome. No production deployment requested.

- **2026-11-03:** Owner authorized production deployment of merged PR #48.
  Confirmed approved main and prepared a private archive of the verified image.
  Environment has no TCP grants or SSH authentication, so no server access or
  production mutation occurred. Owner requested setup guidance; resume from
  [deployment record](project-hardening/deployment.md) after protected access is configured.

- **2026-11-03:** Owner requested GitHub automatic deployment. Replaced the
  retired production SCP workflow with exact-main/CI-gated Docker deployment,
  encrypted off-host backups, inspected ownership migration and image/config
  rollback preserving publication history. Local suite 2316 passed/3 skipped;
  PR #49 merged after green CI. The first automatic workflow `41677472275`
  confirmed its exact-main gate, then failed before SSH because the complete
  dedicated secret pair was unavailable. Runtime is unchanged; status and exact
  credential-setup next action are in the
  [deployment record](project-hardening/deployment.md).

- **2026-11-03:** Owner first reported a tags-only T-Hunted post, then confirmed
  the card loaded with a screenshot. Classified as delayed preview display;
  exact network/cache/client cause is unverified. Removed only our uncommitted
  article-link experiment/tests and proposed incident task; preserved the
  existing post format. No push, repost, edit or production change occurred.

- **2026-11-05:** Owner supplied local SSH_OK/KEY_READY and reported saving both
  GitHub secrets. Current main remains e43c2b3. Reran deployment as attempt 2:
  main selection passed, both secret values were nonempty, but SSH setup exited
  1 without a unique diagnostic. No server connection, rollout or tag. Await
  correction/confirmation of GitHub known_hosts before another attempt; supplied
  public key type and host entry were validated locally.

- **2026-11-05:** Known-hosts correction succeeded. After real image checks,
  preparation failed with a hidden diagnostic. Published minimal transport
  reporting fix through PR #50 after 48 focused tests and green full CI (2319
  passed/2 skipped). Main/dev are 0fdb84f in the clean release checkout. Actual
  deployment 42007051334 now reports local server worktree changes; no bot stop
  or rollout. Await owner status/diff-stat/commit output and preserve those changes.

- **2026-11-05:** Owner confirmed only untracked runtime data/backups on server
  cda27b7. Published bounded ignore bootstrap via PR #51 after 55 focused checks
  and green CI (2326 passed/2 skipped). Main/dev now fb86bc0 in clean release
  checkout. Actual preparation and encrypted off-host backup passed; activation
  failed with no cutover.db copy. Live recovery is not yet verified. Await the
  owner's sanitized result JSON because the cloud proxy blocks its download.

- **2026-11-05:** Completed actual production rollout and automatic deployment.
  PR #52 fixed Docker 29 native-ID comparison while preserving content/revision
  checks; PR #53 fixed missing-tag error handling. Final run 42016189442 is green
  on main 77f82f6, including live worker/ledger checks and automatically created
  prod-2026-11-05-77f82f6. Both encrypted backup artifacts are retained until
  November 19. No deployment work or owner credential answer remains pending.
