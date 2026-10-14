---
name: ecc-flow
description: Route substantial, unclear, or risky Codex project work through the smallest sufficient ECC path. Use for planning that needs durable artifacts, release or production work, and continuing an existing work item. A bounded read-only task or clear, narrow, low-risk change may proceed directly without loading the full router or references.
---

# ECC Flow for Codex

ECC is a delivery router, not a mandatory ceremony. Produce the approved outcome safely with the least process and code that the work actually requires.

## Start and resume

1. Read the applicable `AGENTS.md` files and obey their precedence.
2. When the task depends on durable project facts, read `.agents/skills/project-knowledge/SKILL.md`, then only the references relevant to the task.
3. Start resumption from [the current handoff](../../../work/HANDOFF.md), inspect
   the working tree and revision, then open only its linked work item. Consult
   [the work index](../../../work/README.md) if a record's purpose is unclear.
   Dated sessions and old checkpoints supply historical evidence, not the
   current next action. Do not duplicate completed specs, reviews, or tests.
4. Classify the request by uncertainty, breadth, reversibility, security/data risk, and release impact.
5. Before the first mutation on a routed substantial task, state a compact execution budget: outcome, files or systems likely to change, required verification, review boundary, and stop condition.

## Choose the minimum route

| Situation | Route |
|---|---|
| Explanation, status, diagnosis, or read-only review | Inspect and answer directly. |
| Clear, narrow, low-risk fix or explicit ad-hoc change | In one short sentence name the action and focused verification; inspect, patch minimally, verify, and stop without loading the full router or references. |
| Clear bounded implementation with an adequate approved plan | Implement directly from that plan; do not create duplicate specs or tasks. |
| New or materially changed product behavior with unresolved intent | Create or resume `user-spec.md`, then obtain owner approval only for the unresolved product choices. |
| Approved behavior with unresolved consequential architecture, data, security, integration, or rollout choices | Create or resume `tech-spec.md`, then obtain owner approval only for those material choices. |
| Large implementation with independent deliverables, dependencies, or useful parallelism | Decompose the approved tech spec into the minimum necessary task files. |
| Approved implementation work | Execute the plan or tasks, verify in proportion to risk, and review only where justified. |
| Release or production work | Use the release route and its explicit approval gates. |

If a narrower applicable skill is available, use it inside the selected ECC route. It may
refine that step but must not expand scope, create a new approval gate, or duplicate evidence.

Do not advance to a later route merely because its template exists. Do not add interviews, validators, agents, review rounds, full suites, documentation, or release steps unless the result or a concrete risk requires them.

## Planning rules

Read [references/planning.md](references/planning.md) only when requirements, architecture, or decomposition are needed. Use the templates in `assets/templates/` as small checklists, deleting irrelevant sections rather than filling them ceremonially.

The owner approves product scope and any material expansion. Codex may clarify, challenge, and recommend, but may not mark a spec approved on the owner's behalf.

## Execution rules

Read [references/execution.md](references/execution.md) before implementation or test creation. For a read-only review, use it only when its execution guidance is relevant beyond the applicable reviewer contract.

- Keep facts, hypotheses, interpretations, and open questions distinct.
- Prefer direct evidence from code, tests, logs, APIs, and authoritative documentation.
- Treat review findings as evidence, not permission to expand scope.
- Preserve unrelated user changes.
- Stop when the declared outcome and verification are complete.

## Release rules

Read [references/release.md](references/release.md) only for release-acceptance QA, deployment, production mutation, or closeout. Never deploy, move a stable branch, or mutate production without approval covering that effect. One explicit request may cover stable movement and deployment together; do not request the same approval again while it still covers the target and revision.

## Codex-native mechanics

- Use natural-language requests and project skills; do not depend on slash commands or Claude-only hooks.
- Use the plan tool only when several dependent steps materially help coordination.
- Use Codex subagents only when the user or applicable instructions allow them and the work is genuinely independent or benefits from a specialized adversarial check.
- Before spawning a subagent, read `model-policy.yaml` and inspect the models and override modes offered by the current runtime. Pass the first available active candidate plus its reasoning effort only when the runtime permits that override with the chosen context mode. If an override requires a context-isolated spawn, provide the bounded task context explicitly; otherwise inherit from the parent. This project-local policy replaces global model-routing defaults; do not merge their role tables. Evaluation candidates are never spawned.
- Custom role contracts live in `.codex/agents/*.toml`. The root orchestrator and unlisted roles inherit the current session model.
- For Codex product behavior or configuration, consult official OpenAI documentation rather than guessing.

## Artifact state

Use frontmatter status values `draft`, `approved`, `in_progress`, `blocked`, or `complete`. Status must describe verified reality. Keep decisions append-only when reversal history matters. Archive or close work only after its acceptance criteria are actually satisfied.
