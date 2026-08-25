---
created: 2026-08-26
status: draft
branch: dev
size: M
---

# Tech Spec: Publication Silence Recovery

## Solution

The runtime fix separates what the operator can honestly regard as a planned publication from the fixed times at which the scheduler must re-check reviewable rows. A pure fixed-time helper owns the 10:00/15:00/19:30 MSK eligibility and grace rules. One atomic SQLite aggregate classifies every pending row as currently publishable, time-deferred, or content-held, so a concurrent review callback is observed either before or after the snapshot instead of disappearing between separate count queries.

`job()` computes the operator-facing plan only from rows that are publishable at the snapshot. When the same snapshot contains at least one deferred or held row, the execution loop retains every remaining fixed time for that day. An empty read at one of those release opportunities skips only that opportunity; the next fixed time performs a fresh `list_pending()` read. The main thread remains the only publisher, callback handlers remain state transitions, and all existing database gates continue to control eligibility.

The external watcher moves its inline date arithmetic into one stdlib-only tri-state classifier shared by tests and the scheduled workflow. The classifier returns `fresh`, `stale`, or `inconclusive` from a Telegraph response and one timezone-aware clock. The workflow preserves an open or closed publication alarm on `inconclusive`, keeps the SSH contour independent, and executes repository code without exposing the Telegraph token to that code.

No database schema, external service, scheduler library, persistent day ledger, or live production action is added.

## Architecture

### What we're building/modifying

- **`compute_publish_slots.py`** — add a pure remaining-fixed-times primitive and make `compute_fixed_slots()` reuse it.
- **`pending_articles_repo.py`** — add one aggregate read that partitions pending rows into publishable, deferred, and held categories in a single SQLite statement.
- **`news_bot.py`** — keep operator-facing planned slots separate from execution opportunities, preserve remaining review-release checks, and make mixed-gate callback status truthful.
- **`publication_watch.py` (new)** — parse Telegraph evidence and classify publication freshness as `fresh`, `stale`, or `inconclusive` using one MSK clock.
- **`.github/workflows/uptime.yml`** — fetch evidence, run the repository classifier without the Telegraph secret, and preserve alarm state on inconclusive evidence or classifier-source failure.
- **Tests** — unit coverage for time/date policy and repository partitioning, real-temp-SQLite integration coverage for review-to-slot timelines, and a static workflow contract test.
- **Project Knowledge** — record the release-opportunity scheduler contract and the watcher's tri-state/default-branch activation model.

### How it works

#### Runtime scheduler and review flow

1. The daily job finishes intake and requests one atomic backlog snapshot from SQLite: `publishable`, `deferred`, and `held`.
2. `planned_slots` and `carry_over` are computed only from `publishable`. Existing `[E008]`/`[E009]`, backlog, and carry-over reporting continue to use those values; deferred and held counts remain separate lines.
3. If `deferred + held > 0`, `execution_slots` contains all fixed times still eligible today. Otherwise it equals `planned_slots`, so a genuinely empty or ordinary one-row day does not wait for artificial later checks.
4. At every execution slot, the main thread sleeps until the slot and reads `list_pending()` again. A row released before that read can publish in the current slot; a release committed after an empty read waits for the next fixed time.
5. An empty read continues only when release opportunities were retained. With no reviewable snapshot backlog, existing early-break behavior remains.
6. Publish, retry, hold-cap, recap, dry-spell, and heartbeat paths remain in the main thread. Empty opportunities increment no publish outcome counter.
7. `[E014] keep` and `[E036] approve` continue to mutate SQLite only. If one gate is cleared while another future deferral/hold remains, the callback status reports the remaining gate instead of promising an immediately eligible slot.
8. A snapshot read failure is logged. The job may fall back to the existing publishable count for the ordinary plan, but it creates no release opportunities from unknown review state; a failure of that fallback propagates and lets the existing process restart policy handle it.

#### External publication watch

1. The SSH greeting probe remains independent and runs even when repository checkout, Telegraph fetch, or classification is unavailable.
2. A fetch step owns `TELEGRAPH_ACCESS_TOKEN`, writes only the API response to a fixed runner-temporary file, and records whether usable evidence was obtained. It never writes the token to the file, command output, or step output.
3. A pinned checkout step uses `persist-credentials: false`, `fetch-depth: 1`, and `continue-on-error: true`. Checkout failure becomes publication `inconclusive`; it does not suppress the host verdict.
4. A classifier step has no Telegraph token. It reads the temporary response through `publication_watch.py`; missing code, exceptions, unexpected output, or missing evidence are coerced to `inconclusive`.
5. `publication_watch.py` validates the JSON shape, newest page path suffix, timezone awareness, and calendar date. Its CLI reads raw JSON from standard input and prints exactly one state to standard output; the pure function accepts an explicit clock and converts it once to `Europe/Moscow`.
6. Yesterday is `fresh` through 20:59 MSK and `stale` from 21:00. Any date older than yesterday is `stale`. A December suffix may map to the previous year only when the current MSK month is January; any other future suffix, impossible date, malformed payload, or unavailable API result is `inconclusive`.
7. `stale` opens the publication issue/alerts only when it is closed; `fresh` resolves only when it is open; `inconclusive` performs neither transition. Host-down still suppresses publication transitions.

### Shared resources

No new heavy process resource is introduced. Existing and workflow-scoped shared state is listed for ownership clarity.

| Resource | Owner (creates) | Consumers | Instance count |
|----------|-----------------|-----------|----------------|
| Production SQLite file | Existing deployment / `pending_articles_repo` connections | Daily job, review listener | 1 file; short-lived connection per repository call |
| Telegraph response file in runner temp | Watcher fetch step | Watcher classifier step | 1 per workflow run, runner-scoped |
| GitHub publication alarm issue | Watcher alert step | Later watcher runs via `gh issue` lookup | 0 or 1 open issue for the publication alarm |

## Decisions

Traceability labels `US-AC1` through `US-AC10` refer to the ten acceptance-criteria bullets in `user-spec.md`; `US-S1` through `US-S3` refer to its three scenarios.

### Decision 1: Separate planned slots from release opportunities

**Decision:** Compute the operator plan from currently publishable rows, while retaining all remaining fixed times only when the atomic snapshot contains deferred or held rows.

**Rationale:** This satisfies US-AC1, US-AC2, US-AC4, US-AC6, and US-AC8: review/timer releases still have a future fixed opportunity, while plan, carry-over, and recap do not present a conditional check as a guaranteed post.

**Alternatives considered:** Count deferred/held rows as planned posts (misstates the plan and still gives one withheld row only one slot); always keep all three times (extends genuinely empty days); schedule independent per-slot jobs (unnecessary lifecycle rewrite).

### Decision 2: Use one atomic backlog partition

**Decision:** Add one aggregate SQLite statement returning mutually exclusive publishable, deferred, and held counts. Held includes rows that also carry a future `publish_after`.

**Rationale:** This satisfies US-AC5 and the concurrency part of US-AC8. A callback committed around planning is classified either before or after one statement; separate reads can miss a row while it transitions between categories.

**Alternatives considered:** Reuse three existing repository calls (cross-query race); add a transaction around unrelated reads (longer lock scope and more complexity); introduce persistent scheduler state (outside the approved scope).

### Decision 3: Keep SQLite as the eligibility source and callbacks as state-only operations

**Decision:** Every retained time performs a fresh `list_pending()` read, and only the existing main-thread loop may publish. Callback resolvers clear or remove queue state and return truthful mixed-gate status.

**Rationale:** This satisfies US-AC1 through US-AC7 and preserves existing auth, token-kind, retry, and idempotency boundaries.

**Alternatives considered:** Publish from the Telegram listener (second publisher, off-slot post, retry/recap bypass); use an in-memory condition/event (duplicates SQLite state and is lost on restart); start a second full `job()` from a callback (duplicate intake and competing slot loops).

### Decision 4: Reuse one pure fixed-time eligibility function

**Decision:** A new pure helper returns every remaining fixed time under the existing timezone-aware and five-minute-grace contract; `compute_fixed_slots()` slices that shared result to the requested planned count.

**Rationale:** This supports US-AC6 and US-AC7 and prevents plan/execution paths from drifting on cutoff or grace arithmetic.

**Alternatives considered:** Call `compute_fixed_slots(3, now)` as an implicit helper (mixes opportunity policy with a magic count); duplicate the time filter in `job()` (two rule sources); modify the meaning of `compute_fixed_slots(n)` (breaks the established `n=1` contract).

### Decision 5: Use a tri-state watcher classifier with conservative year inference

**Decision:** `publication_watch.py` returns exactly `fresh`, `stale`, or `inconclusive`; it uses one aware clock converted once to MSK and rolls December into the previous year only during January.

**Rationale:** This satisfies US-S3, US-AC9, and US-AC10. Ambiguous evidence cannot confidently open or close an alarm, and the UTC-date/MSK-hour oscillation becomes unrepresentable.

**Alternatives considered:** Keep a boolean where API error equals healthy (false recovery); infer the previous year for every future suffix (false stale alert); parse the inline workflow code from tests (fragile and still leaves two contracts).

### Decision 6: Execute the same watcher code in tests and the workflow

**Decision:** The scheduled workflow checks out the repository at its trusted default-branch revision and calls the tested module; the old inline classifier is removed.

**Rationale:** This supports US-AC9 and US-AC10 by removing the production/test source-of-truth split. GitHub documents full-length commit pinning as the immutable form for third-party actions, so `actions/checkout` is pinned to official v4.4.0 commit `11d5960a326750d5838078e36cf38b85af677262`, with credentials not persisted. Sources: [GitHub secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use), [official checkout v4.4.0 commit](https://github.com/actions/checkout/commit/11d5960a326750d5838078e36cf38b85af677262).

**Alternatives considered:** Duplicate the classifier in YAML and Python (untested production copy); fetch repository source ad hoc with `curl` (unverified code path); let checkout failure abort the job (loses the independent host alarm).

### Decision 7: Isolate watcher secrets from repository code

**Decision:** Fetch and classification are separate workflow steps; only the fetch step receives `TELEGRAPH_ACCESS_TOKEN`, and classifier input contains response data only.

**Rationale:** This supports US-AC10 and reduces secret exposure if classifier code or diagnostics regress.

**Alternatives considered:** Pass the token or token-bearing URL to the classifier (unnecessary trust expansion); put response JSON in a command-line argument (process/log exposure); log raw payloads on parse error (possible data leakage and noisy output).

### Decision 8: Preserve bounded current behavior on failures

**Decision:** Unknown scheduler review state creates no additional publish opportunity; unknown publication evidence creates no alarm transition. Host monitoring remains independent.

**Rationale:** This satisfies the failure requirements in US-AC8 and US-AC10 without converting uncertainty into a publish, stale alert, or recovery.

**Alternatives considered:** Fail open to additional scheduler checks (could expose a future logic regression); treat watch uncertainty as fresh (false recovery); treat it as stale (false alarm).

### Decision 9: Implement scheduler and watcher as separate change contours

**Decision:** The runtime scheduler and GitHub watcher are separate tasks/commits with separate tests and rollback notes, followed by one shared audit and QA gate.

**Rationale:** This implements the approved M-sized package and the user-spec Deploy section while preserving its two different activation paths: default-branch workflow activation and later manual Docker rollout.

**Alternatives considered:** One combined implementation commit (harder rollback and review); split into separate user specs (loses the incident-level contract already approved).

## Data Models

### SQLite schema

No schema or migration changes. Existing fields remain authoritative:

- `pending_articles.hold_reason IS NOT NULL` — content-held, regardless of `publish_after`.
- `hold_reason IS NULL AND publish_after > CURRENT_TIMESTAMP` — time-deferred.
- `hold_reason IS NULL AND (publish_after IS NULL OR publish_after <= CURRENT_TIMESTAMP)` — publishable.

The new repository API `get_schedule_backlog_counts() -> tuple[int, int, int]` returns `(publishable, deferred, held)`. The categories are non-negative, mutually exclusive, and sum to the total rows in `pending_articles` at the statement snapshot.

### Watcher state

`PublicationState = Literal['fresh', 'stale', 'inconclusive']` is returned by `classify_telegraph_response(body: str, now: datetime)`. It is the only classifier result accepted by the workflow; unexpected CLI output is converted to `inconclusive` at the workflow boundary.

The response body remains runner-temporary evidence, not application state. GitHub issue presence remains the durable open/closed alarm flag.

## Dependencies

### New packages

- No new Python packages. Runtime and watcher implementation are stdlib/project-code only.
- New workflow action: `actions/checkout` v4.4.0 at full commit `11d5960a326750d5838078e36cf38b85af677262` — load the trusted default-branch classifier without mutable action tags.

### Using existing (from project)

- `compute_publish_slots.py` — existing fixed-time and grace policy.
- `pending_articles_repo.py` / SQLite — queue eligibility, atomic aggregate, and review concurrency boundary.
- `news_bot.py` — existing single-thread publication loop, callback resolvers, retry/recap/heartbeat paths.
- `admin_alerts.py` — unchanged plan/quiet/recap builders consuming truthful planned values and separate withheld counts.
- Python stdlib `datetime`, `json`, `re`, `sys`, and `zoneinfo` — watcher parsing and Moscow conversion.
- GitHub-hosted `curl`, `python3`, `ssh-keyscan`, and `gh` — existing watcher tools.
- Existing `pytest`/`unittest.mock`, tempfile SQLite, and `pytz` test patterns.

## Testing Strategy

**Feature size:** M

### Unit tests

- Remaining fixed times at grace boundaries, midday/restart, after cutoff, custom times, and naive-clock rejection; existing `compute_fixed_slots(n)` behavior remains unchanged.
- Atomic backlog partition for publishable, future-deferred, elapsed-deferred, held, and dual-gate rows; categories are mutually exclusive and exhaustive.
- Watcher matrix: yesterday at 20:59/21:00 MSK, day-before-yesterday at 00:00/02:59/03:00, same-day, Dec-to-Jan, non-December future suffix, impossible/leap dates, malformed JSON/API shapes, empty pages, unexpected path, and naive time.
- Callback status for clearing only one of two active gates, without weakening existing auth/token/idempotency tests.

### Integration tests

- Real tempfile SQLite timeline for `[E014]`: deferred at 10:00, first read empty, real `keep` after that read, exactly the intended link published once at 15:00.
- Symmetric `[E036] approve` timeline plus cancel/reject, silence, expiry, mixed-gate, and callback-at-boundary cases.
- Ordinary one-row and empty-day regression: no artificial 15:00/19:30 waits; restart at 16:00 retains only 19:30.
- Snapshot read error: no extra release opportunity or gate bypass; ordinary fallback plan remains bounded.
- Observability: operator plan uses `planned_slots`, empty opportunities do not enter publish recap, and heartbeat/dry-spell still execute after the bounded loop.
- Static workflow contract: exact pinned checkout, `persist-credentials: false`, checkout failure tolerance, secret only on fetch step, repository classifier invocation, allowed tri-state validation, and no raise/resolve on `inconclusive`.

Tests must assert resulting row identity/state and published-table movement, not only mock call counts. External Telegram, Telegraph, LLM, SSH, and GitHub APIs remain mocked or absent.

### E2E tests

None. The only bot/channel is production, a test post is irreversible, and the approved user-spec excludes live Telegram/Telegraph/workflow/deploy actions. Unit, real-SQLite integration, and workflow contract tests cover the change before manual rollout.

## Agent Verification Plan

**Source:** user-spec "Как проверить" section.

### Verification approach

The agent first records the production timeline test failing against the current implementation, then implements each contour independently and runs its focused unit/integration tests. After both contours are complete, the agent runs scheduler/repository/callback/watcher/workflow suites together, the full repository suite, `git diff --check`, and pre-commit hooks. No verification command receives production credentials or uses external network access.

The final QA report maps every user and technical acceptance criterion to a concrete test/result. Deployment and post-deploy verification remain user-owned future actions and are not part of this execution plan.

### Tools required

- `bash`/`zsh` for deterministic local commands and workflow text checks.
- `venv/bin/python -m pytest` for unit and integration suites.
- `pre-commit` through the repository's configured hooks.
- `git diff --check`, `git status`, and read-only history inspection.
- No MCP, live `curl`, SSH, Telegram, Telegraph, Docker restart, or deploy tool is required.

## Risks

| Risk | Mitigation |
|------|------------|
| Only one half of the scheduler race is changed | RED timeline requires both retained future times and continuation after the first empty read; reviewers verify deletion of either behavior breaks the test |
| Callback crosses the planning/read boundary | Atomic backlog statement and per-opportunity `list_pending()` define the ordering; release after an empty read waits for the next fixed time |
| Held backlog keeps the job alive until 19:30 | Opportunities are limited to the existing three times and created only from a snapshot containing reviewable rows; heartbeat/watch thresholds already tolerate the normal full-day loop |
| Operator plan claims conditional posts | Plan and carry-over use publishable rows only; deferred/held stay separate, and empty opportunities do not affect recap counters |
| A row with two gates receives a false status or publishes early | Held category dominates the aggregate, `list_pending()` retains both SQL predicates, and callback tests cover both gate-clear orders |
| Tests pass while repeatedly selecting the same pending head | Integration publish doubles move the selected row to `published_articles` and assert the exact link plus no duplicate publication |
| Snapshot query fails | Log the failure, use only the existing ordinary publishable fallback, and create no unknown release opportunity; fallback failure aborts the job |
| Checkout dependency hides the host alarm | Checkout runs with `continue-on-error`; missing classifier becomes publication `inconclusive` while the SSH verdict and host transitions continue |
| Repository code sees the Telegraph secret | Fetch and classify are separate steps; only response data crosses the boundary and credentials are not persisted by checkout |
| Mutable or compromised third-party action | Pin official `actions/checkout` v4.4.0 to its full commit SHA and retain least-privilege `contents: read` |
| Inconclusive evidence causes a false recovery or alert | Workflow accepts only three states and explicitly performs no publication transition for `inconclusive` |
| Runtime and watcher activate at different times | Separate commits/tasks and rollback notes; watcher activates on merge to `main`, runtime only after a later user-run Docker rollout |

## User-Spec Deviations

None.

## Acceptance Criteria

- [ ] A pure fixed-time helper is the single source for grace/cutoff eligibility, and all existing `compute_fixed_slots(n)` tests remain green.
- [ ] One SQLite statement returns mutually exclusive/exhaustive publishable, deferred, and held counts, including dual-gate and elapsed-defer rows, without a schema change.
- [ ] Operator-facing plan/carry-over uses only publishable rows; all remaining fixed execution times are retained only when the atomic snapshot contains deferred or held rows.
- [ ] An empty retained opportunity continues to the next fixed time, while an ordinary/empty snapshot without reviewable rows preserves early exit.
- [ ] The exact production `[E014]` timeline publishes the intended row once at 15:00 after `keep`; cancel and silence produce no premature publication.
- [ ] `[E036] approve` receives the same next-slot behavior; reject/silence and both dual-gate clear orders remain non-publishing until all gates are open.
- [ ] No callback calls the publish path, no job uses more than the three remaining fixed times, and restart/cutoff behavior remains unchanged.
- [ ] Empty release checks do not alter plan, carry-over, publish recap, dry-spell, or heartbeat truthfulness; scheduler-state read failures do not bypass a gate.
- [ ] The tested watcher classifier returns the complete MSK boundary/calendar matrix and converts all malformed, missing, impossible, naive-clock, and ambiguous-future evidence to `inconclusive`.
- [ ] The workflow executes that classifier, uses an immutable pinned checkout with non-persisted credentials, and keeps the Telegraph token out of repository code and outputs.
- [ ] Publication alarm transitions are `stale -> raise`, `fresh -> resolve`, `inconclusive -> no-op`; host-down suppression and independent host verdict remain intact.
- [ ] Focused suites, full pytest, pre-commit, and diff checks pass offline; no production publish, restart, deploy, DB mutation, or live external request occurs.

## Implementation Tasks

### Wave 1 (independent foundations)

#### Task 1: Scheduler planning primitives
- **Description:** Add the shared fixed-time eligibility primitive and one atomic scheduler-backlog partition. Cover their boundary and category contracts without changing the database schema or existing planned-slot API.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `compute_publish_slots.py`, `pending_articles_repo.py`, `tests/test_compute_fixed_slots.py`, `tests/test_pending_articles_repo.py`
- **Files to read:** `work/publication-silence-recovery/user-spec.md`, `work/publication-silence-recovery/code-research.md`, `news_bot.py`

#### Task 2: Publication-watch classifier
- **Description:** Create the stdlib-only tri-state Telegraph freshness classifier and its unit matrix. Expose a deterministic local entry point suitable for both tests and the scheduled workflow.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Verify-smoke:** `venv/bin/python -c "from datetime import datetime, timezone; from publication_watch import classify_telegraph_response; print(classify_telegraph_response('{\"ok\":true,\"result\":{\"pages\":[{\"path\":\"sample-08-25\"}]}}', datetime(2026, 8, 26, 17, 59, tzinfo=timezone.utc)))"` -> `fresh`
- **Files to modify:** `publication_watch.py` (new), `tests/test_publication_watch.py` (new)
- **Files to read:** `.github/workflows/uptime.yml`, `work/publication-silence-recovery/user-spec.md`, `work/publication-silence-recovery/code-research.md`

### Wave 2 (contour integrations)

#### Task 3: Review-release scheduler integration
- **Description:** Integrate the planning primitives into the daily job and make both review callbacks truthful for mixed gates. Add real-temp-SQLite timeline coverage for E014/E036 release, rejection, silence, errors, and ordinary scheduling regressions.
- **Skill:** code-writing
- **Reviewers:** code-reviewer, security-auditor, test-reviewer
- **Files to modify:** `news_bot.py`, `tests/test_job_distributed_publish.py`, `tests/test_integration.py`
- **Files to read:** `compute_publish_slots.py`, `pending_articles_repo.py`, `admin_alerts.py`, `tests/test_distributed_schedule_integration.py`, `work/publication-silence-recovery/tech-spec.md`

#### Task 4: Tri-state workflow integration
- **Description:** Replace the inline publication classifier with the repository module and preserve alarm state when evidence or classifier source is inconclusive. Add an offline workflow contract test covering action pinning, secret separation, independent host verdict, and all three publication transitions.
- **Skill:** deploy-pipeline
- **Reviewers:** code-reviewer, security-auditor, deploy-reviewer
- **Verify-smoke:** `venv/bin/python -m pytest tests/test_publication_watch.py tests/test_uptime_workflow.py -q` -> all tests pass without secrets or network
- **Files to modify:** `.github/workflows/uptime.yml`, `tests/test_uptime_workflow.py` (new)
- **Files to read:** `publication_watch.py`, `.github/workflows/ci.yml`, `.claude/skills/project-knowledge/references/deployment.md`, `work/publication-silence-recovery/tech-spec.md`

### Wave 3 (documentation)

#### Task 5: Project Knowledge update
- **Description:** Document the scheduler's conditional release opportunities and the watcher's tri-state/default-branch activation and rollback boundaries. Keep operational guidance explicit that workflow merge and runtime Docker rollout are separate actions.
- **Skill:** documentation-writing
- **Reviewers:** code-reviewer
- **Files to modify:** `.claude/skills/project-knowledge/references/architecture.md`, `.claude/skills/project-knowledge/references/patterns.md`, `.claude/skills/project-knowledge/references/deployment.md`
- **Files to read:** `work/publication-silence-recovery/user-spec.md`, `work/publication-silence-recovery/tech-spec.md`, `news_bot.py`, `.github/workflows/uptime.yml`

### Audit Wave

#### Task 6: Code Audit
- **Description:** Review the completed scheduler, watcher, workflow, and documentation changes holistically for complexity, duplication, naming, error handling, and architectural consistency. Write the feature code-audit report without modifying implementation files.
- **Skill:** code-reviewing
- **Reviewers:** none
- **Files to modify:** `work/publication-silence-recovery/logs/audit/code-audit.json` (new report)
- **Files to read:** `compute_publish_slots.py`, `pending_articles_repo.py`, `news_bot.py`, `publication_watch.py`, `.github/workflows/uptime.yml`, `tests/test_compute_fixed_slots.py`, `tests/test_pending_articles_repo.py`, `tests/test_job_distributed_publish.py`, `tests/test_integration.py`, `tests/test_publication_watch.py`, `tests/test_uptime_workflow.py`

#### Task 7: Security Audit
- **Description:** Audit review authorization boundaries, SQLite queries, workflow action integrity, secret isolation, untrusted evidence parsing, and logging across the completed feature. Write the security-audit report without modifying implementation files.
- **Skill:** security-auditor
- **Reviewers:** none
- **Files to modify:** `work/publication-silence-recovery/logs/audit/security-audit.json` (new report)
- **Files to read:** `pending_articles_repo.py`, `news_bot.py`, `publication_watch.py`, `.github/workflows/uptime.yml`, `tests/test_pending_articles_repo.py`, `tests/test_job_distributed_publish.py`, `tests/test_integration.py`, `tests/test_publication_watch.py`, `tests/test_uptime_workflow.py`

#### Task 8: Test Audit
- **Description:** Review feature tests for behavioral assertions, real SQLite state transitions, boundary coverage, fragmentation, and workflow-contract value. Write the test-audit report without modifying implementation files.
- **Skill:** test-master
- **Reviewers:** none
- **Files to modify:** `work/publication-silence-recovery/logs/audit/test-audit.json` (new report)
- **Files to read:** `tests/test_compute_fixed_slots.py`, `tests/test_pending_articles_repo.py`, `tests/test_job_distributed_publish.py`, `tests/test_integration.py`, `tests/test_publication_watch.py`, `tests/test_uptime_workflow.py`

### Final Wave

#### Task 9: Pre-deploy QA
- **Description:** Run focused and full offline regression suites, pre-commit hooks, diff checks, and acceptance traceability across scheduler and watcher contours. Write the final QA report and leave production deployment and live verification explicitly pending for the user.
- **Skill:** pre-deploy-qa
- **Reviewers:** none
- **Files to modify:** `work/publication-silence-recovery/logs/qa/final-qa.md` (new report)
- **Files to read:** `work/publication-silence-recovery/user-spec.md`, `work/publication-silence-recovery/tech-spec.md`, `compute_publish_slots.py`, `pending_articles_repo.py`, `news_bot.py`, `publication_watch.py`, `.github/workflows/uptime.yml`, `tests/test_compute_fixed_slots.py`, `tests/test_pending_articles_repo.py`, `tests/test_job_distributed_publish.py`, `tests/test_integration.py`, `tests/test_publication_watch.py`, `tests/test_uptime_workflow.py`, `work/publication-silence-recovery/logs/audit/code-audit.json`, `work/publication-silence-recovery/logs/audit/security-audit.json`, `work/publication-silence-recovery/logs/audit/test-audit.json`
