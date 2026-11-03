# Current project handoff

Last factual update: **2026-11-03 (Europe/Moscow)**.
Read this file first to resume. Record ownership is described in [the work index](README.md).

## Where work stopped

- **Latest owner request:** push the completed hardening changes to GitHub,
  open a PR into `main`, and merge. Push, PR and merge after green CI are
  authorized; production deployment is not requested. Implementation and the
  [revised plan](project-hardening/plan.md) are complete.
- **Last coding work:** runtime/LLM rendering, retry persistence, deadlines,
  callbacks, log redaction, data/DOM/model extraction, bounded source fetching,
  rootless worker startup, persistent-state safety, monitoring delivery, CI and
  current documentation are corrected. Detailed checks and limits are in
  [verification](project-hardening/verification.md).
- **Git:** seven scoped implementation/documentation commits were pushed to
  `dev` at `ecdaea4`. [PR #48](https://github.com/alexkoskov/my-hw/pull/48)
  targets `main`; its current head, checks and merge status are authoritative.
  The publication metadata follow-up changes no tested runtime content.
  Inspect current refs rather than inferring the final revision from this record.
  GitHub repository settings remain unchanged.
- **Production:** not inspected or changed. The last historical deployment
  record remains 2026-10-01 at `cda27b7`; it is not a live health check. The new
  image was built and tested only in the managed local Docker environment.

## Exact next step

Check [PR #48](https://github.com/alexkoskov/my-hw/pull/48): if it is still open,
finish the already-authorized merge only after the latest head's actual test job
passes; if merged, align local/remote `dev` and `main` and follow the owner's
selected next task. This PR is the durable record of the final publication.

Before a separately authorized production rollout, inspect and migrate only
exact persistent state paths to uid/gid 10001 using the
[deployment runbook](../.agents/skills/project-knowledge/references/deployment.md).
Do not bypass startup refusal with an empty database. Deployment is not authorized.

The existing product item **DBP-015** still needs the owner's classification of
its 2026-09-10 E014 pair in the [post-deploy report](dedup-broad-precision/logs/working/post-deploy-qa-report.json),
then its recorded observation plan. **STAB-002** and **OBSV-002** keep their
remaining acceptance; partial startup/CI hardening does not complete those
larger items. Follow the owner's selected work rather than old checkpoints.

## Verification and limits

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
