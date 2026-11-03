# Current project handoff

Last factual update: **2026-11-03 (Europe/Moscow)**.
Read this file first to resume. Record ownership is described in [the work index](README.md).

## Where work stopped

- **Latest owner request:** enable automatic production deployment from GitHub.
  The owner already authorized deployment, then selected the automatic route.
  A main-CI-gated Docker workflow and checkpointed host/runner controllers are
  published through merged PR #49. The first automatic workflow reached its
  credential guard and stopped before SSH. Enable the dedicated GitHub secrets.
- **Last coding work:** runtime/LLM rendering, retry persistence, deadlines,
  callbacks, log redaction, data/DOM/model extraction, bounded source fetching,
  rootless worker startup, persistent-state safety, monitoring delivery, CI and
  current documentation are corrected. Detailed checks and limits are in
  [verification](project-hardening/verification.md).
- **Git:** [PR #49](https://github.com/alexkoskov/my-hw/pull/49) merged as
  `e43c2b33a1b97314dbbeeee7084c7a072644f0c2`; local/remote dev/main were verified
  synchronized there. This factual closeout is published on `dev` only; the
  executable automation is already on main. No status-only production rollout.
- **Production:** not accessed or changed in this task. GitHub runners require
  dedicated `PROD_SSH_PRIVATE_KEY` and verified `PROD_SSH_KNOWN_HOSTS` for Moscow;
  the first workflow confirmed the complete pair was unavailable. It failed
  before SSH; production is unchanged. The
  [deployment record](project-hardening/deployment.md) owns the new verification,
  setup requirements and rollback details. Historical records are not live health.

## Exact next step

The operator must install/configure the dedicated SSH identity and verified
host-key entry in GitHub, following the existing
[one-time setup](../.agents/skills/project-knowledge/references/deployment.md#one-time-github-ssh-setup).
Then rerun failed deployment run `41677472275` or manually dispatch production on
current green-CI main. Publishing/production deployment remain authorized for
this requested outcome. Do not retry before configuration changes, substitute
old NL credentials or initialize an empty database. Run/job evidence is in the
[deployment record](project-hardening/deployment.md).

The existing product item **DBP-015** still needs the owner's classification of
its 2026-09-10 E014 pair in the [post-deploy report](dedup-broad-precision/logs/working/post-deploy-qa-report.json),
then its recorded observation plan. **STAB-002** and **OBSV-002** keep their
remaining acceptance; partial startup/CI hardening does not complete those
larger items. Follow the owner's selected work rather than old checkpoints.

## Verification and limits

- Automatic deployment local suite: **2316 passed, 3 skipped, 566 subtests
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
