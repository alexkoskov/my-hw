# Global working agreements

## Two-branch Git workflow: `dev` -> `main`

Enforce this workflow for every Git repository, regardless of its previous branch names or workflow. `dev` is always the working branch and `main` is always the stable branch.

### Mandatory branch normalization

- Before the first pull, push, commit, or branch synchronization in any repository, normalize its branch structure to `dev` and `main`.
- Always fetch and inspect the complete local and remote commit graph before creating or moving branch references.
- If `main` does not exist, create it from the authoritative remote default branch. If no remote default exists, use the current established stable branch. If the authoritative source is ambiguous because histories have diverged, preserve all histories and ask the user which line should seed `main`.
- If `dev` does not exist, create it from the current `main`.
- If branches with other names contain unique commits, preserve them under their existing names or a clearly named `legacy/<name>-<date>` backup. Never discard their commits while normalizing.
- If existing `dev` and `main` have diverged, preserve both histories and ask before resolving the divergence. Do not reset either branch merely to make the names match.
- Publish a missing `origin/dev` only during the normal push workflow. Publish or update `origin/main` only after the user explicitly approves the `main` step.
- Once normalized, all new work must continue through `dev` -> explicit approval -> `main`, even if the repository previously used `master`, feature-only branches, trunk-based development, or another workflow.

### General rules

- Treat `dev` as the default working branch. Finish every completed pull, push, or synchronization operation on `dev`.
- Treat `main` as the stable branch. Fetching and inspecting `origin/main` is always allowed, but never move local `main` or push `origin/main` without the user's explicit confirmation for that specific `main` step.
- An explicit approval may be part of the original request, for example: "push dev, then main" or "push both branches." Do not ask for the same confirmation again in that operation.
- Before any pull, push, or branch synchronization, run `git fetch --all --prune`, require a clean working tree, and inspect local and remote `dev` and `main` with ahead/behind and ancestry checks.
- Determine which branch is newer from the Git commit graph, not from commit timestamps alone.
- Never force-push, rewrite commits, drop commits, or delete/recreate branches without separate explicit approval.
- If `dev` and `main` have diverged and both contain unique commits, do not choose a winner automatically. Preserve both histories, explain the divergence, and ask before resolving it.

### Meaning of "push" / "пуш"

- A request to "push" always means sending commits to the repository's GitHub remote with `git push`; creating local commits, merges, tags, or branch references does not satisfy a push request.
- Use the GitHub remote that tracks the branch, normally `origin`. Before pushing, verify that its URL points to GitHub. If no GitHub remote exists or the intended GitHub remote is ambiguous, ask the user instead of stopping after local Git operations.
- Complete the requested remote push in the same workflow after required checks pass. Do not report a push as complete until the corresponding GitHub remote-tracking branch has been fetched and verified at the intended commit.

### When the user says "push" / "пуш"

1. Fetch all remotes and inspect local and remote `dev` and `main`.
2. Ensure the intended changes are committed on `dev`. If `main` contains commits missing from `dev`, integrate `main` into `dev` safely before pushing. If the histories have diverged or conflict resolution is ambiguous, ask the user.
3. Run the repository's relevant tests and checks.
4. Push `dev` to the verified GitHub remote, normally `origin/dev`.
5. Fetch again and verify that local `dev` and the GitHub `origin/dev` are synchronized.
6. Unless the original request already explicitly approved the `main` step, ask: "Changes were pushed to dev. Synchronize and push them to main?" / "Изменения отправлены в dev. Синхронизировать и отправить их в main?"
7. Only after explicit approval, update local `main` from `dev`, preferring a fast-forward, run any checks required for the final tree, and push `main` to the verified GitHub remote. If this creates a new merge commit on `main`, integrate that exact final commit back into `dev`, rerun relevant checks, and push `dev` to GitHub again so both branches can end at the same commit.
8. Fetch again and verify that local `dev`, local `main`, GitHub `origin/dev`, and GitHub `origin/main` are synchronized at the same intended commit.
9. Switch back to `dev`.

If the user does not approve updating `main`, leave local and remote `main` unchanged and continue on `dev`.

### When the user says "pull" / "пулл"

1. Fetch all remotes and inspect local and remote `dev` and `main`.
2. Compare both branches by ancestry and ahead/behind counts:
   - if they are equal, update local `dev`, report whether local `main` also needs to move, and do not move `main` without explicit approval;
   - if one branch contains the other, update local `dev` from the branch containing the newer history when this is safe, but do not move or push `main` without explicit approval;
   - if they have diverged, preserve both histories, report the unique commits on each side, and ask how to synchronize them.
3. Report which branch was newer and what was pulled into local `dev`.
4. If `dev` and `main` are not synchronized or local `main` needs to move, ask: "Synchronize dev and main?" / "Синхронизировать dev и main?"
5. Only after explicit approval, integrate the newer history into the lagging branch, run checks, update the required GitHub remote branch or branches, and verify synchronization.
6. Finish on `dev`.

### Cross-Mac bootstrap

- After a pull, inspect any newly added or changed repository `AGENTS.md` before continuing; newly pulled instructions do not become active automatically in an already-running Codex session.
- A repository that supports this bootstrap must carry a complete canonical global workflow file, preferably `docs/codex-global-AGENTS.md`. Do not treat an abbreviated project `AGENTS.md` as a complete global template.
- If the current Mac's global `~/.codex/AGENTS.md` does not contain both the `Two-branch Git workflow: dev -> main` and `Mandatory branch normalization` sections, and the repository contains the canonical workflow file, ask whether to install that complete file globally on this Mac.
- If no canonical workflow file exists, report that automatic bootstrap is unavailable and ask how to proceed. Never invent or reconstruct a supposedly canonical global rule from partial project instructions.
- Never modify another Mac's global instructions without explicit confirmation.
