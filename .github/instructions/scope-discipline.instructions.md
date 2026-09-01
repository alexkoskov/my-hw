---
description: Enforce minimum-sufficient scope and prevent process-driven overengineering
applyTo: '**'
---

# HARD SCOPE RULE: install the cabinet, do not renovate the building

This is a mandatory execution constraint, not a suggestion. The process itself is part of scope: extra planning, tasks, audits, documentation, tests, reviews, QA waves, and release checkpoints are extra work and require the same justification as extra code.

- Implement the smallest reliable change that directly produces the user's approved outcome.
- For a narrow correction to an otherwise working feature, default to: inspect the cause, patch it, run focused verification, perform at most one proportionate holistic review only when risk warrants it, then stop.
- Keep a plan item only if it is necessary for the requested result to work safely. Templates, generated task files, old plans, checklists, review suggestions, and available tooling do not authorize extra scope.
- Reuse evidence already earned. Do not repeat a full suite, review, or audit when relevant code has not changed; documentation, logging, planning, and metadata-only edits do not invalidate prior code evidence.
- Never split one narrow fix into separate code, security, test, documentation, QA, and release-readiness projects unless the user explicitly asks or independent high-risk boundaries truly require it.
- Expand only when the requested outcome cannot work safely otherwise. Before expanding, stop, explain the exact blocker and minimum extra work, and obtain explicit approval.
- Pre-existing debt and adjacent improvements go to backlog; discovery alone never makes them part of the current task.
- When the user says the work is simple, narrow, or overengineered, immediately collapse the remaining plan to the minimum safe path. Never defend or continue an oversized legacy plan.
- When the approved outcome and stop condition are satisfied, stop.

Correct: fix one dedup decision bug, strengthen only directly affected assertions, run focused checks, reuse the already-green full-suite result, and stop.

Wrong: turn that fix into separate corpus, code, security, test, documentation, audit, QA, and release-governance projects when those steps do not independently protect the requested outcome.
