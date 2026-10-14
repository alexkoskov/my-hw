# Work records

Start with [HANDOFF.md](HANDOFF.md). Open the selected work item from its links;
the existing sessions and feature folders do not need to be scanned to resume.
`AGENTS.md` defines when the handoff is read and updated.

## One location per kind of information

| Question | Authoritative location |
| --- | --- |
| Where did we stop; what is the next action? | [HANDOFF.md](HANDOFF.md): current snapshot and short dated outcomes |
| What is planned, in progress, or done across the project? | [Project tracker](../.project-progress.json): task IDs, statuses, dependencies, weighted progress |
| What was approved for this feature? | Its existing `user-spec.md`, relevant design decisions, and recorded owner approvals |
| Why was a decision made? | The work item's existing `decisions.md` or adequate approved planning record |
| Which checks or deployment actually ran? | Detailed evidence in the work item's existing `logs/`; link the result and tested revision from the handoff |
| How does the project normally work? | [Project knowledge](../.agents/skills/project-knowledge/SKILL.md): relevant stable references |
| How is Codex configured? | [Codex setup](../docs/codex-setup.md) |
| What happened in earlier sessions? | Existing dated `SESSION-*`, migration/audit records, and `completed/` or `archived/` work |

## Active and pending work

This is a navigation index. The tracker owns status; the handoff owns the next
selected action. These links do not create another task list or approval gate.

| Item | First relevant record | Recorded limit |
| --- | --- | --- |
| DBP-015 — dedup observation | [Post-deploy report](dedup-broad-precision/logs/working/post-deploy-qa-report.json) | Observation checked through 2026-09-17; one E014 classification unresolved |
| STAB-002 — minimum startup/CI safety fixes | [Approved requirements](stabilization-foundation/user-spec.md) | Remaining implementation is planned; the tech-spec and checkpoint are scaffolds |
| OBSV-002 — reminder for held articles | [Approved requirements](operator-blind-spots/user-spec.md) | Remaining implementation is planned; the tech-spec and checkpoint are scaffolds |
| Codex migration — completed locally | [Verification record](SESSION-2026-10-14.md) | Uncommitted; two dedup test failures predate the migration |

## Keeping records useful

Update the handoff's current snapshot once at a substantive result or pause;
append one short dated outcome in the same file. Record the selected next action,
approval state, exact code/publication state, checks, limits, and evidence links.
Keep raw output and long explanations in the existing work item. Do not create
a new session file or duplicate reports after every chat or command.

Update stable reference docs only when their facts change. Update the project
tracker only for task, deliverable, or blocker changes. A docs-only update does
not invalidate prior code checks. If a recorded revision or fact conflicts with
the current checkout, resolve that specific conflict before expanding the search.

Historical files stay at their established paths so evidence and existing links
remain findable. Their dates, old paths, hooks, and "read first" instructions
describe the past; current continuation rules are in `AGENTS.md` and HANDOFF.md.
