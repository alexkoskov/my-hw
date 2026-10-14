# Proportional QA, release, and closeout

## Pre-release

Map acceptance criteria to existing evidence first. Run only missing or invalidated checks. Use a full suite, browser run, integration environment, security review, or load test only when the changed surface or approved acceptance criteria require it.
Treat CI as evidence only when the relevant required jobs actually ran against the intended
revision and passed; a skipped or unrelated green job does not verify the release.

Separate:

- current-task defects that prevent acceptance;
- unrelated debt or optional improvements;
- checks that can only run after deployment.

Do not fix the latter two categories without approval.

## Deployment

Movement of a stable branch and deployment are distinct external effects, and authorization must cover each effect that will occur. One explicit request may cover both when it clearly covers the stable target, deployment target, and revision; do not ask again for an effect already covered. Production mutation and secrets changes likewise require authorization covering the concrete action. Before acting, name the targets, exact revision, verification, rollback route, and stop condition. If the revision changes, request fresh approval only when the existing authorization does not cover the new revision.

## Post-deploy

Verify only the real channel and behavior affected by the release. Prefer safe, reversible probes. Do not send real messages, create customer data, charge accounts, or run destructive tests unless that exact external effect was authorized.
Record the exact deployed revision or tag using the project's established release record, and
verify the affected behavior rather than only checking that a process is alive.

If production differs from the approved revision or required credentials/authority are absent, stop and report the blocker rather than improvising around it.

## Closeout

Update only an existing project-knowledge reference, decision record, or work status whose
verified fact changed or whose update is required by project instructions. Do not create or
touch every artifact category automatically. Metadata-only closeout does not create a reason
to rerun tests or deploy. Report the outcome, evidence, unresolved current-task defects, and
separately any future debt.
