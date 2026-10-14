# Hot Wheels News Bot — project agreements

Global Codex agreements are inherited from the environment or user configuration;
keep this file limited to project facts, constraints, and overrides.

## Orientation and checks

- Start new or resumed work with `work/HANDOFF.md`, then inspect the current
  branch, `HEAD`, and working-tree status. Follow its links to the selected work
  item; search more widely only for a missing fact, changed revision, or contradiction.
- `work/README.md` maps records to their purpose. Read `README.md` for initial
  orientation and `.agents/skills/project-knowledge/SKILL.md` only for relevant
  durable project facts. Do not reread the whole project to recover the last step.
- The runtime editorial prompt is `prompts/ux-guidelines.md`. Keep it available
  to all four LLM engines and in every deployment manifest; it is application
  data, independent of the coding agent's skills.
- Install dependencies with `pip install -r requirements.txt -r requirements-dev.txt`.
  Run tests with `python -m pytest tests/ -q`. The prepared cloud interpreter is
  `/workspace/.venvs/my-hw/bin/python` when available.
- `python news_bot.py` runs a publishing tick immediately. Use isolated test
  databases and mocked services for verification; do not run it with production
  credentials merely to check the installation.
- Preserve captured HTML fixtures and archived `work/` evidence. Scope any
  pre-commit checks to changed files; do not reformat source captures.

## Git and production policy

- `dev` is the development line; `main` is the stable production line.
  Promote `dev` to `main` through a GitHub pull request with green CI and
  release notes. Moving or publishing `main` requires authorization.
- Production deployment is manual; both automated deploy workflows remain
  disabled. After an authorized successful deployment, tag the deployed `main`
  revision as `prod-YYYY-MM-DD` (use a suffix for a second deployment that day).
- An authorized manual deployment or container restart may run at any time.
  A restart replans the remaining daily publication slots. Preserve the database
  volume and runtime configuration, record the rollback revision, and verify startup.
- When updating a repository that exists only in a Docker volume on macOS,
  follow the host-migration procedure in `docs/codex-setup.md`. It does not apply
  to this Linux cloud workspace.

## Project progress and records

- `.project-progress.json` covers the full approved project roadmap. Update it
  after substantial completed work, status changes, or moved deliverables;
  preserve history, dependencies, blockers, and the existing schema.
- Use allowed statuses and weights; set `updated_at` to the actual ISO timestamp
  with timezone. Validate JSON, unique IDs, references, and completed deliverables.
  Report progress as completed weight divided by total weight, never an estimate.
- The website reads `main`; updating this file does not authorize promotion.
- `work/HANDOFF.md` is the single current continuation record. Before ending or
  handing off substantive work, update its snapshot once: outcome, active item,
  exact next action and its approval state, code/publication/deployment state,
  checks with scope and limits, blockers, and links to evidence. Update it at a
  meaningful pause or before context loss when needed, not after every command.
- Append one short dated history entry per substantive outcome in that same
  file. Keep its current snapshot concise. Detailed decisions and verification
  belong in the existing work item's records; link them instead of copying them.
- Dated `SESSION-*`, old handoffs, audits, and checkpoints are historical
  evidence. Do not create a new session file by default or treat an old
  `next_wave` or draft template as the current task or approval.
- Reuse recorded checks when their relevant inputs are unchanged. Reading,
  explanations, and status-only requests need no record update unless they
  establish a decision or change the project's actual state.

## ECC routing

For substantial, unclear, or risky work, durable planning, releases, or continuing
an existing work item, read `.agents/skills/ecc-flow/SKILL.md` and only its relevant
references. This selects the project's workflow; common authorization, scope,
evidence, and delegation rules remain in the inherited global agreements.
