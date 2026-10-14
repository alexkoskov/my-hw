# Minimum-sufficient execution

## Before editing

Use [the current handoff](../../../../work/HANDOFF.md) to find the selected task
and its last verified step. Inspect only the relevant artifacts and changed
inputs; expand the search when the recorded state is missing or contradicted.

Confirm the approved outcome and inspect the exact code path. State the compact scope lock before the first mutation. Preserve unrelated changes and do not silently rewrite user work.
For a small module, read the complete file when practical so a local patch does not violate a
nearby invariant or duplicate an existing mechanism.

## Implement

1. Find the concrete behavior or cause.
2. Make the smallest reliable change that satisfies the approved acceptance criteria.
3. Add or adjust tests only where they provide meaningful regression evidence.
4. Run the smallest check capable of disproving the change.
5. Review the complete changed surface once when its risk warrants review.
6. Stop at the declared stop condition.

Test-first development is useful for new deterministic behavior and reproducible regressions. It is not mandatory for documentation, metadata, exploratory prototypes, or changes where a test would only mirror the implementation.

Write comments for non-obvious reasons, invariants, or constraints. Do not add comments that
merely restate the code.

## Evidence and review

- Reuse valid evidence when relevant behavior has not changed.
- Prefer behavioral assertions over implementation-detail assertions.
- Do not repeat broad suites or create several review waves for reassurance.
- Choose a specialist only for the risk actually present: security for trust boundaries, test review for complex coverage, reality checking for claims that need adversarial validation.
- Findings outside the approved task are future work. Report them separately; do not fix them without approval.

## Agents and parallelism

Use one implementer by default. Delegate only independent, bounded work when permitted by the active instructions. Give every subagent an exact scope, allowed mutation boundary, expected evidence, and stop condition. Never let parallel agents edit overlapping files without an explicit coordination reason.

## Git

Follow the repository's own branch and release policy. A commit or push is an external workflow action, not an implicit consequence of editing, unless the active instructions or owner explicitly require it. Never force-push or rewrite shared history without explicit authorization.

## Pause and finish

Follow `AGENTS.md`'s continuation-record rule. Update the same
`work/HANDOFF.md` snapshot once, append a short dated outcome, and link detailed
evidence in the existing work item. Do not generate another session journal.
