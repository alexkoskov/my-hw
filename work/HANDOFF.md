# Current project handoff

Last factual update: **2026-11-03 (Europe/Moscow)**.
Read this file first to resume. Record ownership is described in [the work index](README.md).

## Where work stopped

- **Latest owner request:** enable automatic production deployment from GitHub.
  The owner already authorized deployment, then selected the automatic route.
  A main-CI-gated Docker workflow and checkpointed host/runner controllers are
  implemented; publish through a green-CI dev→main PR and inspect the first run.
- **Last coding work:** runtime/LLM rendering, retry persistence, deadlines,
  callbacks, log redaction, data/DOM/model extraction, bounded source fetching,
  rootless worker startup, persistent-state safety, monitoring delivery, CI and
  current documentation are corrected. Detailed checks and limits are in
  [verification](project-hardening/verification.md).
- **Git:** PR #48 is merged. The automatic-deployment changes are local on `dev`
  over synchronized `323c7c4` dev/main refs. Publication and first workflow run
  remain pending; GitHub PR/workflow state records the final transport outcome.
- **Production:** not accessed or changed in this task. GitHub runners require
  dedicated `PROD_SSH_PRIVATE_KEY` and verified `PROD_SSH_KNOWN_HOSTS` for Moscow;
  availability is unverified. Missing configuration must fail before SSH. The
  [deployment record](project-hardening/deployment.md) owns the new verification,
  setup requirements and rollback details. Historical records are not live health.

## Exact next step

Finish the verified automatic-deployment changes, push `dev`, open/merge the PR
only after its actual test job passes, align dev/main, then inspect push-CI and
`Deploy production` for exact main. No additional publication/deployment consent
is needed for this requested outcome. If dedicated GitHub secrets are missing,
leave the production task incomplete and direct the owner to the existing
[one-time setup](../.agents/skills/project-knowledge/references/deployment.md#one-time-github-ssh-setup).
Do not substitute old NL credentials or initialize an empty database.

The existing product item **DBP-015** still needs the owner's classification of
its 2026-09-10 E014 pair in the [post-deploy report](dedup-broad-precision/logs/working/post-deploy-qa-report.json),
then its recorded observation plan. **STAB-002** and **OBSV-002** keep their
remaining acceptance; partial startup/CI hardening does not complete those
larger items. Follow the owner's selected work rather than old checkpoints.

## Verification and limits

- Automatic deployment local suite: **2316 passed, 3 skipped, 566 subtests
  passed**. Real backup encryption and offline rollback/gate tests passed;
  actionlint and Bash syntax passed. Live SSH/deployment is still unverified.
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
  publication and first actual workflow outcome remain pending in the
  [deployment record](project-hardening/deployment.md).
