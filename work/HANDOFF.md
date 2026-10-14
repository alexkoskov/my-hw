# Current project handoff

Last factual update: **2026-10-14 (Europe/Moscow)**.
Read this file first to resume. Record ownership is described in [the work index](README.md).

## Where work stopped

- **Current request:** simplify documentation and continuation records. The
  persistent handoff and startup/update rules replace mandatory daily journals.
- **Last coding work:** the Codex migration is complete locally; shared skills,
  project knowledge, role configs, and runtime prompt packaging were checked.
  Details: [migration evidence](SESSION-2026-10-14.md).
- **Git:** branch `work`, base `14e40d262de99bdb8026500104a20d184337d28d`.
  The migration and documentation changes are uncommitted. No commit, push, or
  deployment was performed in this chat. Inspect current Git state before acting.
- **Production:** the last recorded deployment was 2026-10-01 at `cda27b7`,
  documented in [the previous deployment record](SESSION-2026-09-17.md).
  Production was not inspected in this chat; that record is not a live health check.

## Exact next step

Documentation work is complete; no new coding task was selected by this request.
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
