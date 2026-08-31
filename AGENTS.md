# Repository working agreements

## Keep both long-lived branches current

This repository has two long-lived branches with different roles: `main` is the
stable/production line and `dev` is the active development line. Treat both as
first-class project state; never inspect or update only the branch that happens
to be checked out.

Whenever the user asks to pull, sync, or update the project:

1. Fetch the remote with pruning, then inspect all local and remote branches for
   newer work, including at least `main`, `dev`, `origin/main`, and `origin/dev`.
2. Report the ahead/behind state of both local long-lived branches and the
   left/right commit counts between `origin/main` and `origin/dev`. State which
   remote branch contains the newest commit.
3. Preserve uncommitted work and the user's checked-out branch. Update both
   local `main` and local `dev` from their matching remote branches using
   fast-forward-only operations when possible; do not silently merge, rebase,
   force-update, or discard either branch.
4. If `main` and `dev` have diverged, treat commits unique to each side as
   intentional until proven otherwise. Report the divergence and ask before
   merging or rebasing one long-lived branch into the other.
5. Distinguish clearly between "the current branch is up to date" and "the
   whole project is up to date." The latter is true only after both long-lived
   branches and any newer work found on other remote branches have been checked.

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
