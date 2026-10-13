---
name: project-progress
description: Create, validate, or maintain a repository project-progress tracker when the user explicitly asks, project instructions require it, or an existing tracker must reflect a substantial task or blocker change. Report truthful weighted progress without inventing completion or triggering a release. Do not trigger solely because ordinary project work is substantial.
---

# Project Progress

Track the project's complete path to the next concrete usable release, not only the feature
active in the current session. Exclude later milestones and unrelated wishlist items.

## Activation boundary

- Use the repository's own `.project-progress.example.json`,
  `project-progress.schema.json`, and project instructions as the contract.
- Do not introduce a tracker into a repository that has no such contract unless the user asks.
- Keep the milestone and task list separate. Include only work required for the next usable
  release: implementation, relevant verification or review, deployment, and post-deploy
  verification.

## Truth rules

- Record only inspected facts. A task is done only when its deliverable exists and the
  relevant verification passed.
- Keep proposals separate from tracker facts and preserve the source of material evidence.
- Use only statuses, weights, dependency forms, and blocker forms allowed by the local schema.
- Keep dependencies between task IDs and blocker links between blocker IDs and affected task
  IDs. Maintain both sides of every blocker relationship.
- Never store a manually estimated percentage. Calculate and report completed weight divided
  by total weight from the file.
- Never include secrets, personal or customer data, private endpoints, or sensitive
  infrastructure details.

## Update points

Update the tracker only when inspected evidence changes a task, scope, status, deliverable,
verification result, or blocker. Set `updated_at` to the actual ISO 8601 timestamp with timezone
when the file changes. Before a substantive final project report, compare the tracker with the
current evidence and update it only if it is stale; do not rewrite it merely to refresh a report
or timestamp.

## Validation

Before reporting or publishing, verify:

1. JSON parsing and local schema validation;
2. unique IDs and resolvable dependencies and blocker references;
3. allowed weights and statuses;
4. dependency cycles and blocker relationship symmetry;
5. deliverable and verification evidence for every done task;
6. absence of sensitive data.

Reuse a successful validation when the tracker, schema, and relevant evidence have not changed.
Rerun only checks invalidated by a change.

Publishing follows the repository's branch and release policy. A progress-only change never
authorizes movement of a stable branch or a deployment.
