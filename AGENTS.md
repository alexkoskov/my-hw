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
