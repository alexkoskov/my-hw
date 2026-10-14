# Codex setup

## Source and shared installation

The shared package is [alexkoskov/codex-global-config](https://github.com/alexkoskov/codex-global-config),
verified at `7d85e79d6275cbf179c3538e1a12e101408f7a81` on 2026-10-14;
its `dev` and `main` refs were identical. The global agreements have one
maintained source; this repository does not carry a second global template.

On a normal workstation, clone that package and run `./install.sh` once.
It installs global `AGENTS.md` in the existing `CODEX_HOME` or `~/.codex/`,
and five base skills in `~/.agents/skills/`:
`decision-quality`, `grilling`, `integration-reliability`, `project-progress`,
and `test-quality`. Do not copy the global agreements into this project's
`AGENTS.md`. Start a new session to refresh instructions and skill discovery.

## Managed cloud workspace

This environment mounts `CODEX_HOME`, the user home, and `/workspace/.agents`
read-only. The supported shared instruction layer is `/workspace/AGENTS.md`,
which applies to projects beneath `/workspace`. It was updated using the
package's `./install.sh --project /workspace --agents-only`; the installer
retained the previous file in `/workspace/.codex-config-backups/`.

The five base skills are already provisioned in `/workspace/.agents/skills/`;
every packaged file matched the GitHub revision above. Keep them shared rather
than checking copies into each repository. Native Codex discovers repository
skills within the Git root, so this checkout uses ignored symbolic links to the
shared files. Recreate them after a fresh checkout with:

```sh
python scripts/link_codex_skills.py /workspace/.agents/skills
```

The script checks all five sources and refuses to overwrite existing project
skills. On a normal workstation, user skills in `~/.agents/skills/` are discovered
globally and these cloud aliases are unnecessary. Environment provisioning must supply
these shared skills on a new managed instance; a project checkout cannot write
the read-only global directories.

The downloaded package is at `/workspace/.local/share/codex-global-config/`.
It is a source checkout, not another active instruction layer. No persistent
managed-environment configuration was changed by this repository migration.

## Project-specific Codex files

- `AGENTS.md`: project constraints, checks, release policy, progress, and the ECC router.
- `work/HANDOFF.md` and `work/README.md`: the current continuation snapshot and
  record index; read these before searching old sessions for the last step.
- `.agents/skills/ecc-flow/`: Codex workflow, templates, model selection policy,
  and the upstream MIT notice.
- `.agents/skills/project-knowledge/`: preserved architecture, project, patterns,
  and deployment facts; read only the references relevant to the task.
- `.codex/config.toml` and `.codex/agents/`: seven optional Codex role contracts.
  These do not launch agents or change the root session's model.
- `prompts/ux-guidelines.md`: application-owned editorial system prompt for all
  LLM engines. It ships independently of the coding agent's configuration.

The ECC files originate from the same configuration package. Project knowledge
was retained rather than replaced by empty bootstrap templates. Role names and
descriptions are registered in `config.toml`; role layer files contain native
Codex settings and developer instructions.

Native Codex loads project configuration only for trusted projects. Review and
trust this repository through the normal Codex prompt on a writable workstation.
Skills still load for an untrusted project. In this managed session, native
app-server startup is constrained by read-only runtime storage. The same Codex
binary was checked in an isolated container with writable state storage: its
`skills/list` request discovered all seven skills without loading errors, including
the five shared paths reached through the project aliases. That check does not
make the managed instance's system configuration writable.
With project trust recorded in the disposable container's user configuration,
`config/read` also loaded all seven role registrations without disabling the
project layer. No model turn or subagent was started.

The bot still supports OpenRouter, OpenAI, Gemini, and Anthropic API providers.
Those are application integrations, separate from the coding agent. Changing the
coding workflow to Codex does not remove a configured translation provider.

Legacy Claude/Cline instructions, hooks, commands, and generic toolkit copies
were removed from the active checkout. Archived `work/` records preserve their
historical names and decisions. A recovery copy is outside this repository at
`/workspace/.local/share/codex-migration-backups/my-hw-2026-10-14.tar.gz`.

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
