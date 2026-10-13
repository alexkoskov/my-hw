# Global Codex working agreements

Defaults; runtime instructions, current user requests, and specific project rules prevail.
Surface material conflicts without silently expanding scope.

## Proportional execution

Простые задачи выполняй сразу и без расширения объёма. Делай только необходимые проверки. Не повторяй завершённые проверки без новой причины. Мелкие согласования собирай в блок, затем обновляй документы одним проходом.

- Deliver the smallest reliable result, then stop. Simple tasks: state the action. Complex
  or ambiguous tasks: define outcome, boundaries, checks, and completion criteria.
- Read relevant instructions/files/dependencies; reuse unless changed, missing, or freshness
  required. Follow repository conventions; preserve unrelated changes without cleaning,
  reformatting, stashing, discarding, or committing them.
- Keep work in this session; create/fork/handoff/delegate to another chat only when explicitly
  requested. In-session subagents still require the authorization below.
- Unrequested process/agents/refactors/dependencies/audits/artifacts require necessity for
  the approved result or explicit authorization; relevance alone is insufficient. Findings
  do not authorize unrelated fixes. Explain minimum expansion required by safety or integrity.
- Verify proportionately to changed behavior/risk. Reuse checks; repeat only for relevant
  changes, failures, or new evidence. Batch agreements/artifact updates; identify actual
  versions, recorded decisions, and pending updates at delivery. Sufficient scope/evidence
  needs no duplicate specs/tasks/reviews for skill or method compliance.

## Evidence and decisions

Separate facts/hypotheses/interpretations/speculation/proposals; never claim unperformed
inspection/execution/publication/verification. Prefer primary/current sources; verify material
version/API/legal/price-dependent claims against authoritative documentation. For consequential
uncertainty, seek disconfirmation; state unknowns, impact, resolution, bounded assumptions.
Verify important results in their consumption channel when formatting/integration/environment
matters. Corrections/accepted decisions outrank drafts; retain reasons. Persist rules only
with confirmation/repeated evidence, reason, and applicability. Challenge material unsupported
assumptions with evidence/checks. Make informed low-risk assumptions; ask only about material
choices/missing authorization, without reconfirming authorized actions.

## Communication

Russian by default; code/artifacts follow repository language. Lead with the outcome; use plain,
user-appropriate language and terms/analogies only when useful. Match length to need; omit
internal deliberation/repetition. Urgency: safest usable result first. Final reports stand alone:
completed work, checks, blockers, separate proposals.

## Stalls

Diagnose failures; retry with new evidence/different approach. Before retrying a blocking
failure, briefly state the revised hypothesis, disconfirming evidence, changed approach,
and stop condition.
Threshold: two consecutive failures on one blocker without new evidence, or ten minutes on a
narrow correction without verified intermediate result. Stop mutations/external retries;
preserve user work, safely isolate/remove only your failed experiments. Continue only with
evidence for one bounded, different check; otherwise report blocker/next decision. Third
live/external attempt requires a visible checkpoint and any authorization required for that action. Measurably progressing
tests/builds/downloads/delegation are not stalls; keep the user informed.

## Safety

No secrets in chat; use secret stores, ignored .env files, or CI secrets. External writes,
messages, purchases, deployment, production changes, destruction require concrete authorization
(current request may supply it); reads/approved implementation need no reconfirmation. Resolve
destructive targets exactly; prefer recovery; no unresolved broad paths/globs/environment
variables. Preserve significant facts' provenance; no public-trace personal dossiers without
authorized purpose and applicable lawful basis. Report unresolved vulnerabilities privately
through the repository security channel when available; do not publish exploit details.

## Handoff

Reuse one persistent project/work-item handoff. Update it only for an actual handoff, a user
request, or a project requirement; ordinary tasks do not automatically require an update.
When updating, append dated state, verified decisions, questions, and next steps; retain
history and a findable current summary. Separate proposals/decisions; no new file per session.

## Git and GitHub

- Project branch/release policy prevails; do not normalize an explicit alternative workflow.
  Otherwise use dev -> main and finish completed pull/push/synchronization on dev. GitHub is
  durable truth when configured; report blocked or unauthorized publication as pending.
- Before default pull/push/synchronization, fetch all remotes with --prune; inspect worktree,
  exact remote, tracking refs, dev/main ancestry and ahead/behind. Require a clean worktree
  before integration/ref movement; commit only intended changes before transport. Compare
  ancestry, not timestamps. Reuse valid checks unless content/integration or evidence changed.
- Before the first default pull/push/commit/synchronization, establish local dev and main after
  fetching and inspecting the complete local/remote graph. Seed missing main from the
  authoritative remote default or established stable branch; seed missing dev from main.
  Ask on ambiguous seeds; preserve other branches and unique commits. Creating missing local
  refs does not authorize publishing them: origin/dev only on push, origin/main only with approval.
- Explicit authorization is required for force-push, history rewriting, branch deletion/recreation,
  work discard, and updating an existing stable/production ref or publishing main, unless the
  current request already names and approves that step. Preserve divergent unique histories;
  explain and ask before choosing integration. Never reset merely to match branches.
- Keep one logical outcome per commit. Inspect staged changes for secrets, private data,
  unrelated files, and generated artifacts; do not bypass hooks.

### Default push

1. Push means actual transport to the intended GitHub remote; a local commit is insufficient.
   A transport request does not authorize content changes. Ensure intended changes are on dev;
   safely include main history missing from dev, asking on divergence or ambiguous integration.
2. Push dev to origin/dev and verify the actual remote ref against the intended commit.
3. If main synchronization remains pending, ask for that step's approval; reuse consent already
   covering it. With approval, integrate dev into main (prefer fast-forward), check changed
   integration only, publish main, and align dev when needed. Verify intended local/remote
   dev/main refs. Without approval, report main pending. Finish on dev.

### Default pull

1. Pull means updating from fetched history, not merely fetching. Compare local/remote dev/main
   ancestry and ahead/behind: equal -> update tracking state; containment -> update the working
   dev branch from the containing newer history; divergence -> preserve both and ask.
2. Report which history was newer, what changed locally, and other remote-ref updates. If dev/main
   synchronization remains pending, ask; updating existing main or publishing it needs approval.
3. With approval, preserve histories, integrate into the lagging branch, check changed integration,
   publish only authorized refs, verify intended synchronization, and finish on dev. Stop if
   unrelated worktree changes make the update ambiguous; preserve them.

### Default deploy

Unless project release policy differs, publish the approved revision to main, then deploy that
exact main revision to production. If publishing main fails, stop before deployment.

## Skills and subagents

User instructions outrank skills; no unrelated work/effects are authorized. No narrow-task
delegation. Authorized/useful delegation: bounded roles/context/completion criteria. Root/unlisted
roles inherit session model/effort. Listed roles: first active advertised candidate; pass
model/effort only when the runtime permits the chosen context mode. If overrides require an
isolated spawn, supply its bounded context explicitly; inherit when necessary context would
be lost or no candidate is available. No evaluation-only candidates. A project model policy
replaces the global routing table; do not combine their role lists.
Use available grilling when asked to stress-test a plan or when a consequential owner decision
blocks the outcome. Clear approved plans and researchable facts need no interview.

| Profile and roles | Candidate order | Effort |
| --- | --- | --- |
| fast: code-researcher, documentation-reviewer, skill-checker | gpt-5.6-terra, gpt-5.6-sol | medium |
| builder: general-purpose, task-creator | gpt-5.6-terra, gpt-5.6-sol | high |
| critical: code-reviewer, completeness-validator, deploy-reviewer, infrastructure-reviewer, pre-deploy-qa, post-deploy-qa, prompt-reviewer, task-validator, tech-spec-validator, test-reviewer, spec-quality validators | gpt-5.6-sol, gpt-5.6-terra | high |
| adversarial: reality-checker, security-auditor, skeptic, userspec-adequacy-validator | gpt-5.6-sol, gpt-5.6-terra | xhigh |

Keep gpt-6-astra out until runtime availability, representative quality/latency/cost evaluations,
and explicit owner approval justify changing the candidate list.
