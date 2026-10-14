# Minimum-sufficient planning

## Decide whether an artifact is needed

- Use a user spec when the desired user behavior, boundaries, or acceptance criteria are materially unclear.
- Use a tech spec when approved behavior still requires consequential choices about architecture, data, security, integrations, migration, observability, or rollout.
- Decompose into task files only when the implementation contains independently actionable units, real dependencies, or useful parallel work. A short ordered plan is enough otherwise.
- An existing approved artifact may be used directly when it already contains the necessary scope, constraints, affected systems, and acceptance criteria.

## Focused interview

When the selected user-spec or tech-spec route still needs consequential owner decisions,
use the `grilling` skill if it is available in the current session. It supplies the interview
method; this ECC route owns the work artifacts and approval gates. Read the discovered skill
path rather than assuming a particular global installation directory.

If the skill is unavailable, ask the necessary clarification directly and continue with the
planning rules below. ECC remains usable without the global skills package.

Reuse settled answers, research facts yourself, and record decisions in the current spec or
existing planning record. Create only the artifacts justified by the selected route. A clear
approved plan needs no additional interview.

## User spec

Inspect the current product and ask only questions whose answers can change the result. There is no fixed number of interview rounds. Record:

- problem and intended outcome;
- users and key scenarios;
- in-scope and out-of-scope boundaries;
- observable acceptance criteria;
- unresolved decisions and assumptions.

Validate against the repository and known product constraints. Ask the owner to approve it; do not self-approve.

## Tech spec

Translate the approved behavior into the smallest safe design. Record only relevant decisions:

- affected components and data flow;
- interfaces, schema, integrations, and failure behavior;
- security/privacy implications;
- migration and compatibility needs;
- proportional verification and rollout/rollback;
- exact stop condition.

Prefer authoritative documentation for unstable external behavior. Explicitly label assumptions and alternatives rejected for concrete reasons. Ask the owner to approve material design choices.

## Task decomposition

Each task should deliver one testable outcome, not one organizational layer. Include scope, dependencies, acceptance criteria, verification, and review boundary. Avoid separate tasks for documentation, tests, security, or review when they are simply part of implementing one narrow behavior.

Use validators only when an independent perspective protects a concrete risk. One combined check is the default; additional rounds require a new defect, unresolved high-risk question, or explicit owner request.
