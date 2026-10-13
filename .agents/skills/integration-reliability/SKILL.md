---
name: integration-reliability
description: Design, implement, or review a workflow with retryable side effects, a meaningful external-service failure boundary, or independently failing sources. Use explicit outcomes, idempotency, bounded retries, failure isolation, and proportionate observability. Do not use for one low-risk read-only call with trivial failure handling.
---

# Integration Reliability

Make the requested integration dependable without building a generic platform around it.

## Define the boundary

1. Identify the external call or side effect, its owner, input contract, output contract,
   timeout, and accepted failure behavior.
2. Validate external responses before using them. Represent meaningful outcomes explicitly,
   using project terms such as `ok`, `no_data`, `not_configured`, and `unavailable`
   instead of collapsing every non-success into one exception.
3. Isolate independent sources. One provider failure may produce a partial result when the
   approved product behavior allows it; it must not silently corrupt successful results.

## Make effects safe

- Establish idempotency before retrying any write or notification. A local correlation key is
  not enough: automatic replay is safe only when the receiving system guarantees idempotency
  for the same operation key.
- Retry only failures classified as transient. Bound attempts and total time, honor
  `Retry-After`, and use exponential backoff with jitter where appropriate.
- Do not retry authentication, permission, validation, or permanent contract errors.
- Treat a timed-out external write with no definitive response as `unknown`, not failed. If
  receiver-side idempotency is unavailable, check remote status, reconcile, or require a manual
  decision instead of replaying the effect automatically.
- Keep state transitions atomic where practical. If partial writes are possible, define the
  smallest repair or reconciliation path required by the approved behavior.
- Use an existing outer timeout or cancellation boundary when a dependency could materially
  hold the workflow; do not build a new control plane for one low-risk call.

## Observe without leaking

When operational risk warrants persistent observation, reuse the project's logging mechanism
to record an opaque correlation ID, source, outcome, duration, attempt count, and safe error
category. Do not build a separate logging stack for a single low-risk integration.
Do not log secrets, tokens, personal data, customer payloads, or unrestricted provider error
bodies. Preserve enough typed context to distinguish `no_data` from a technical failure.

## Verify and stop

Use synthetic inputs and controlled local or test services by default. Cover the success path,
one representative permanent failure, the relevant transient retry path, and idempotent repeat
only when each risk exists in the changed behavior. Stop once the requested integration
contract and its focused checks are complete.
