# Current project handoff

Last factual update: **2026-10-14 (Europe/Moscow)**.
Read this file first to resume. Record ownership is described in [the work index](README.md).

## Where work stopped

- **Latest owner request:** publish the completed Codex migration and continuation
  records through [PR #42](https://github.com/alexkoskov/my-hw/pull/42) (`dev` → `main`).
  The owner explicitly authorized push, PR creation, and merge; deployment was not requested.
- **Last coding work:** the Codex migration is complete locally; shared skills,
  project knowledge, role configs, and runtime prompt packaging were checked.
  Details: [migration evidence](SESSION-2026-10-14.md).
- **Git:** work continues on `dev`; validated content commit `601a4b3` includes
  the previous remote Codex history and the local migration. It was pushed to
  GitHub. Later edits record publication metadata only. PR #42 is the authoritative
  source for its merge status and final `main` revision; inspect current Git refs
  rather than inferring transport state from a dated journal.
- **Production:** the last recorded deployment was 2026-10-01 at `cda27b7`,
  documented in [the previous deployment record](SESSION-2026-09-17.md).
  Production was not inspected in this chat; that record is not a live health check.

## Exact next step

The migration and documentation work are complete; this publication request adds no new coding task.
The active product item is **DBP-015**: obtain the owner's classification of the
2026-09-10 E014 article pair in the [post-deploy report](dedup-broad-precision/logs/working/post-deploy-qa-report.json),
then continue its existing natural-observation plan. Evidence is checked only
through 2026-09-17; elapsed time alone does not complete observation.

**STAB-002** and **OBSV-002** remain planned. Their approved requirements and
record locations are linked in [the work index](README.md#active-and-pending-work).
Their tech-spec/checkpoint scaffolds are not evidence of implementation or a
newly approved next wave. Follow the owner's selected task and existing approval.

## Verification and limits

- Last full suite, 2026-10-14: **2122 passed, 2 skipped, 2 failed; 556 subtests passed**.
  Both failures reproduce on the unmodified base; exact names and scope are in
  [the migration evidence](SESSION-2026-10-14.md#verification).
- Final Docker image: imports and four prompt loaders passed. Native Codex in a
  disposable container discovered seven skills and loaded seven trusted role configs.
- Later edits affect documentation only; the recorded code checks remain relevant
  while their code/dependency inputs are unchanged. Documentation links and
  continuation-rule consistency were checked for this request.
- Managed system storage remains read-only; shared configuration and skill aliases
  are described in [Codex setup](../docs/codex-setup.md).
- Roadmap statuses and weighted progress belong to [the tracker](../.project-progress.json).
  This snapshot routes work; it does not replace detailed evidence or grant release permission.

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
