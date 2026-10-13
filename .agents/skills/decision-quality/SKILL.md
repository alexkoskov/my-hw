---
name: decision-quality
description: Analyze consequential, ambiguous, or evidence-heavy decisions, recommendations, estimates, and research by separating facts from hypotheses, testing the first theory, managing unknowns, and producing one defensible next step. Do not use for simple factual questions or routine status reports.
---

# Decision Quality

Produce a decision that can be defended from evidence without turning the task into an
open-ended research project.

## Evidence states

Classify material claims while reasoning:

- **Verified fact:** supported by an actually inspected source, executed check, or observed
  result.
- **Working hypothesis:** plausible and useful for deciding what to check, but not proven.
- **Interpretation:** an explanation of why known facts or decisions fit together; revise it
  when new feedback contradicts it.
- **Speculation:** a possibility without enough support to drive a promise, estimate, or
  irreversible action.

Never claim a source was read, a system was checked, or a result was verified unless that
access or check actually succeeded. Say when evidence is stale, partial, reconstructed, or
unavailable.

## Compact workflow

1. Identify what decision or deliverable the user needs now and what success means.
2. Inspect the current source of truth and the real system or artifact when available; do not
   rely only on a summary when the underlying evidence is in scope.
3. State the leading hypothesis and identify evidence that could overturn it. Seek the most
   decision-relevant disconfirmation, not exhaustive background.
4. Find the main contradiction or tradeoff that determines the recommendation.
5. Separate what is required, useful, optional, and out of scope. Protect behavior that
   already works.
6. Expose black boxes such as closed APIs, missing access, unknown data quality, or third-party
   decisions. Explain their impact and the smallest discovery step needed for precision.
7. Check that requirements, data, estimates, options, and conclusions refer to the same
   version and that calculations are internally consistent.
8. Give one recommended option, its evidence, its important uncertainty, and a simple next
   action. Include alternatives only when they represent a real decision.

## User-owned decisions

Use the available `grilling` skill when the remaining gap is a consequential choice only the
user can settle, such as scope, priorities, or an acceptable tradeoff. Keep factual discovery
in this evidence workflow. Reuse settled decisions and the current task's records and
approval gates. If `grilling` is unavailable, ask the necessary clarification directly.

## Constructive disagreement

Do not agree automatically. When the user's hypothesis is weak or contradicted:

1. Restate it accurately and without caricature.
2. Identify what the evidence confirms.
3. Identify what remains unproven or is refuted.
4. Explain whether that changes the decision.
5. Offer a more accurate formulation or a concrete test.

Disagree only when it can improve the outcome; do not manufacture balance.

## Learning from outcomes

- Treat explicit user corrections, accepted decisions, and actually used outcomes as stronger
  evidence than earlier drafts.
- Preserve why the decision changed and where the lesson applies.
- Do not turn a single remark or your own interpretation into a durable rule. Propose or write
  a reusable instruction only when the user authorizes it and the pattern is confirmed.

## Data and model-output integrity

- Validate the structure and required fields of external or model-generated output before
  treating it as evidence or passing it downstream.
- Preserve source provenance for material extracted facts. Label inference as inference and
  keep it distinguishable from source data.
- Prefer an explicit `no_data`, `unavailable`, or unresolved result to a plausible invention.
  Never fill a missing fact merely to complete a schema or narrative.
- Do not expose sensitive inputs or unrestricted provider errors in evidence or user-facing
  reports.

## Output boundary

Deep analysis belongs behind a concise deliverable. Present verified facts, material
uncertainties, the recommendation, and the next step. Do not expose irrelevant internal
reasoning or expand into adjacent problems.
