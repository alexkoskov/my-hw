# Current project handoff

Last factual update: **2026-10-14 (Europe/Moscow)**.
Read this file first to resume. Record ownership is described in [the work index](README.md).

## Where work stopped

- **Latest owner request:** fix both known dedup CI failures, then finish
  [PR #42](https://github.com/alexkoskov/my-hw/pull/42) (`dev` → `main`). Push,
  PR creation, and merge after green CI are authorized; deployment is not requested.
- **Last coding work:** Codex migration and continuation records are complete.
  Dedup flags now preserve the actual high-score/title-cap reason; the mixed
  keyboard test uses a title-qualified hard-block candidate. Thresholds and
  block/flag/pass decisions remain unchanged.
- **Git:** work continues on `dev`. The original migration was validated at
  `601a4b3`; the follow-up dedup correction and its checks are recorded below.
  PR #42 is authoritative for merge status and the final `main` revision.
  Inspect current Git refs instead of inferring publication from a dated journal.
- **Production:** the last recorded deployment was 2026-10-01 at `cda27b7`,
  documented in [the previous deployment record](SESSION-2026-09-17.md).
  Production was not inspected in this chat; that record is not a live health check.

## Exact next step

Check PR #42 first: if open, finish the already-authorized merge only after its
latest test job succeeds; if merged, align `dev`/`main` refs and continue the
selected product work. No additional product implementation was selected.
The active product item is **DBP-015**: obtain the owner's classification of the
2026-09-10 E014 article pair in the [post-deploy report](dedup-broad-precision/logs/working/post-deploy-qa-report.json),
then continue its existing natural-observation plan. Evidence is checked only
through 2026-09-17; elapsed time alone does not complete observation.

**STAB-002** and **OBSV-002** remain planned. Their approved requirements and
record locations are linked in [the work index](README.md#active-and-pending-work).
Their tech-spec/checkpoint scaffolds are not evidence of implementation or a
newly approved next wave. Follow the owner's selected task and existing approval.

## Verification and limits

- Last full suite, 2026-10-14: **2128 passed, 2 skipped; 556 subtests passed**.
  Focused dedup, review-button, alert, and precision checks: **226 passed,
  21 subtests passed**, including the existing 24-pair production regression corpus.
- The earlier two baseline failures are fixed. A high-overlap flag now distinguishes
  missing common models in titles from rejected series qualification. The hard-block
  keyboard scenario names the actual shared model/series; its E015/no-buttons
  assertions remain. Four threshold/title cases guard the new reason metadata.
- Earlier packaging evidence remains in [the migration record](SESSION-2026-10-14.md):
  Docker imports/four prompt loaders and seven native Codex skills/role configs
  passed. The dedup correction does not change packaging; no new image or production
  deployment was performed for this correction.
- Current CI and the final publication outcome are linked from PR #42. Reuse checks
  only while their relevant inputs are unchanged; metadata-only edits do not
  invalidate the recorded local code checks.
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
- **2026-10-14:** Owner declined merging with the two known CI failures and
  requested their repair first. Corrected the E014 cap explanation and stale
  hard-block fixture; preserved dedup verdicts and thresholds. Local full suite:
  2128 passed, 2 skipped. Continue the authorized PR #42 merge after green CI.
