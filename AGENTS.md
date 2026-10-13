<!-- codex-global:start -->
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
<!-- codex-global:end -->

# Repository working agreements

## HARD SCOPE RULE: install the cabinet, do not renovate the building

This is a mandatory execution constraint, not a suggestion. The process itself is part of scope: extra planning, tasks, audits, documentation, tests, reviews, QA waves, and release checkpoints are extra work and require the same justification as extra code.

### Minimum-sufficient default

- Implement the smallest reliable change that directly produces the user's approved outcome. Do not improve the surrounding system merely because it is nearby, imperfect, or discoverable.
- For a narrow correction to an otherwise working feature, the default workflow is: inspect the cause, patch it, run focused verification, perform at most one proportionate holistic review only when the risk warrants it, then stop.
- Every plan item must be necessary for the requested result to work safely. Before retaining any step, ask: “Would this task still be required if the only goal were to install this cabinet?” If the answer is no, remove it or place it in future backlog.
- Templates, skills, generated task files, old execution plans, checklists, “best practices,” review suggestions, and available tooling do not authorize additional scope. User intent and the smallest safe outcome take priority over process completeness.
- Evidence already earned remains valid. Do not rerun a full suite, repeat a review, or create another audit merely for reassurance when relevant code has not changed. Documentation, logging, planning, and metadata-only edits do not invalidate prior code evidence.
- Do not split one narrow fix into separate code, security, test, documentation, QA, and release-readiness projects. Use one compact task and one combined verification/review boundary unless the user explicitly requests more rigor or the change truly crosses independent high-risk boundaries.

### Expansion gate

- Expand scope without new approval only when the requested change cannot function safely otherwise, or when a higher-priority safety or integrity requirement makes the extra work mandatory. Explain the exact blocker and the minimum expansion before acting.
- Stop and ask before adding a second review round, a second full-suite run, a new deliverable, dependency, subsystem, migration, external mutation, or unrelated cleanup.
- Pre-existing debt, stale documentation, architectural opportunities, and adjacent improvements never become current-task blockers by discovery alone. Record them separately with source, impact, and proposed scope; do not fix them automatically.
- Review and audit findings are evidence, not authorization. Fix only defects introduced by the current change or required for its approved result to work safely; everything else requires explicit user approval.
- When the user says the work is simple, narrow, or overengineered, immediately stop expanding it and collapse the remaining plan to the minimum safe path. Never defend or continue an oversized legacy plan.
- When the declared stop condition is satisfied, stop. Do not continue searching for work.

### Required reporting and examples

- Before the first mutation, state one compact scope lock: approved outcome, expected files or systems, what is out of scope, and the stop condition.
- At handoff, separate unresolved current-task defects from future debt or adjacent improvements. Ask before fixing either category beyond the approved scope.
- Correct: fix one dedup decision bug, strengthen only the directly affected assertions, run the focused checks, reuse the already-green full-suite result, and stop.
- Wrong: turn that dedup fix into separate corpus, code, security, test, documentation, audit, QA, and release-governance projects when those steps do not independently protect the requested outcome.
- Correct: add crash-safe database writes and their focused verification.
- Wrong: use that request to refactor the worker, redesign the mailer, modernize unrelated docs, or create follow-up audits without approval.

## Production deployment timing

- An explicitly authorized manual production deployment or container restart may run at any time. Time of day is informational and must not block the operation.
- A restart recreates the in-process daily schedule and immediately replans the remaining publication opportunities. Preserve the database volume and runtime configuration, record the rollback commit, and verify a clean start, but do not impose a clock-based deployment gate.

## Keep the website project progress current

`.project-progress.json` is the source for the project progress bar on the
website. Keep it scoped to the full current project roadmap, not only the
feature being implemented, unless the user explicitly changes that scope.

1. Update the file after every substantial roadmap task completion or status
   change and immediately before the final work report for that change.
2. Preserve the repository's project-progress schema: use only allowed task
   statuses and weights, keep task dependencies and blockers resolvable, and
   never store a manually estimated percentage. The website calculates progress
   from completed weight divided by total weight.
3. Set `updated_at` to the actual ISO 8601 time with timezone whenever the file
   changes. Never add secrets, credentials, private endpoints, personal data, or
   production-only data to the tracker.
4. Before committing, verify JSON parsing, schema compliance, unique IDs,
   allowed weights, dependency and blocker references, and the existence of
   deliverables for every task marked `done`. Report the exact `done weight /
   total weight` fraction.
5. Commit progress updates with the project change they describe. The website
   monitors `main`, so promote the validated progress commit through the normal
   `dev` -> `main` workflow, retaining the explicit approval gate for `main`.

## Keep both long-lived branches current

This repository has two long-lived branches with different roles: `main` is the
stable/production line and `dev` is the active development line. Treat both as
first-class project state; never inspect or update only the branch that happens
to be checked out.

The global `dev` -> `main` workflow and its explicit approval gate for `main`
apply to this repository. If that global rule is missing on the current Mac,
`docs/codex-global-AGENTS.md` is the canonical complete template; ask before
installing it into `~/.codex/AGENTS.md`.

Whenever the user asks to pull, sync, update, or push the project:

1. Fetch the remote with pruning, then inspect all local and remote branches for
   newer work, including at least `main`, `dev`, `origin/main`, and `origin/dev`.
2. Report the ahead/behind state of both local long-lived branches and the
   left/right commit counts between `origin/main` and `origin/dev`. State which
   remote branch contains the newest commit.
3. Preserve uncommitted work and the user's checked-out branch. Update local
   `dev` using fast-forward-only operations when possible. Fetching and
   inspecting `origin/main` is allowed, but do not move local `main` or push
   `origin/main` without explicit approval for that specific `main` step.
4. If `main` and `dev` have diverged, treat commits unique to each side as
   intentional until proven otherwise. Report the divergence and ask before
   merging or rebasing one long-lived branch into the other.
5. Distinguish clearly between "the current branch is up to date" and "the
   whole project is up to date." The latter is true only after both long-lived
   branches and any newer work found on other remote branches have been checked.
6. A request to "push" or "пуш" means sending commits to the verified GitHub
   remote, normally `origin`, with `git push`. Local commits, merges, or branch
   updates alone do not complete a push request. Verify the GitHub remote ref
   after pushing.

## Move Docker-volume worktrees to macOS before continuing

When handling a pull, fetch, sync, or project update on macOS, first determine
whether the active repository exists only inside a Docker/Dev Container volume
(common indicators are a path below `/workspaces`, a
`vscode-remote://dev-container` workspace URI, or a Docker volume-backed VS Code
workspace).

If it does, migrate the complete repository to a normal host folder on the Mac
as part of the update workflow:

1. Fetch the remote and inspect all local and remote branches for newer work.
   Report which branch contains the newest project changes and distinguish that
   from updating the currently checked-out branch.
2. Resolve the intended host destination from the local wrapper/workspace
   folder. Never assume a username or hard-code an absolute path. If more than
   one destination is plausible or the destination contains unrelated files,
   ask the user before copying.
3. Copy the repository root itself into the host destination without adding an
   extra directory level. Include `.git`, dotfiles, local configuration such as
   `.env`, untracked files, and working-tree changes.
4. Before treating the migration as complete, compare source and destination
   file counts and byte totals, then validate the local copy with `git status`,
   `git rev-parse --show-toplevel`, and `git fsck --no-dangling`.
5. Remove local VS Code workspace files whose only purpose is opening the old
   Dev Container, and continue all subsequent work from the host folder. Do not
   remove tracked deployment files such as `Dockerfile`, `.dockerignore`, or
   `docker-compose.yml` merely because the development worktree moved.
6. Stop Docker Desktop after successful verification if it was only needed for
   the migration. Treat the old Docker volume and stopped containers as a
   recoverable backup: delete them only after the user explicitly approves the
   permanent data loss.

This agreement governs agent-managed updates. A manual `git pull` cannot install
or run a new Git hook retroactively, so if the user updates the repository by
hand, instruct them to open the pulled repository with Codex once so this
migration can be completed safely.
