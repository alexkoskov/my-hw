---
name: test-quality
description: Design, write, or review proportionate automated tests that protect behavior without redundant or fragmented coverage. Use when tests or test strategy are requested, or when changed behavior genuinely needs a regression check. Do not add unrelated test layers or broad suites to trivial changes.
---

# Test Quality

Choose the smallest test set that would fail for a meaningful regression in the approved
behavior and pass for the intended implementation.

## Choose the test boundary

- Prefer unit tests for deterministic business rules, transformations, validation, and error
  decisions.
- Prefer integration tests for database writes, API boundaries, file-system behavior, and
  interactions whose contract matters.
- Prefer end-to-end tests for a few critical user journeys or platform behavior that mocks
  cannot represent reliably.
- If a test needs to mock most of the behavior under examination, move to a more realistic
  test layer instead of adding more mocks.
- For a narrow change, use the existing nearest test layer unless concrete risk requires a
  new one.

## Quality rules

- Test observable behavior and state, not private implementation details or only that a mock
  was called.
- Every test must catch a specific plausible failure that no existing test already catches.
  Coverage without additional confidence is maintenance cost.
- Keep tests deterministic, independent, and based on a known starting state. Control time,
  randomness, external services, and shared data at the appropriate boundary.
- Give failures diagnostic value: the name and assertions should make the broken behavior
  clear.
- Do not weaken an assertion merely to make a failing implementation pass.

## External-system boundary

- Automated tests must not call customer systems or production by default. Use synthetic data
  and controlled local or test environments. A live check requires an explicit label,
  authorization, safe inputs, and a narrow stop condition.
- Mock only the boundary needed for determinism. Prefer a realistic local service, fixture, or
  contract test when a mock would erase the behavior being verified.
- For a narrow parser, heuristic, classifier, or model-backed correction, add the smallest
  focused fixture that proves the behavior. Maintain a versioned regression corpus only when
  several representative cases, data drift, or an existing corpus mechanism justify it.
  Include false-positive and false-negative cases when both risks materially affect the result.

## Avoid fragmentation

Use one test function per behavior and one parameterized case per equivalent input. Inputs
such as several aliases, enum values, or boundary examples belong in a parameter table when
setup, expected behavior, and failure meaning are the same.

Use separate test functions when behavior, setup, execution path, or the explanation of a
failure differs materially. Different modules require separate coverage when testing one
module executes none of the others.

Before adding another test, ask: does it catch a new failure, or is it the same check with a
different literal? If it is only another literal, add a parameterized case.

## Verification and stop condition

Run the focused affected checks first. Run a broader suite only when change risk, repository
policy, or acceptance criteria justify it. After a broader suite passes, rerun only checks
affected by later edits. Stop when the changed behavior is protected and the required checks
pass; do not expand testing into unrelated debt.
