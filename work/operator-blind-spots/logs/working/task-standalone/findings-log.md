# Findings log — increment A ([E017] once per calendar day)

Round 1 reviewers: code-reviewer (approved_with_suggestions), security-auditor
(approved), test-reviewer (needs_improvement).

| # | Source | Severity | Finding | Action | Reason |
|---|--------|----------|---------|--------|--------|
| 1 | code-reviewer, security-auditor | major | The gate call can raise (locked DB, disk fault); the raise lands in the block's outer `except` and the tick's `[E017]` is dropped after the dry spell was already detected | Applied | Verified by fault injection. Contradicts the user-spec's «если отметка испорчена или недоступна, тревога всё равно уходит», and this alert has no redundant channel the way [E038] has «Отложено: N» |
| 2 | security-auditor | major | `_parse_dt_tolerant` catches only `ValueError`, but `fromisoformat` raises `TypeError` on a non-str; `bot_state.value` is TEXT and TEXT affinity leaves a BLOB a BLOB, so one malformed row disables `[E017]` indefinitely | Applied | Widened to `(ValueError, TypeError)` and made the gate structurally fail-open with a blanket `except` — the guarantee should not rest on which exception a parser happens to raise |
| 3 | test-reviewer | major | Restart survival unpinned: three `job()` calls in ONE process, so an in-process marker — the exact 2026-08-13 bug shape — passes | Applied | Two new tests crossing the process boundary in both directions. Each verified to kill the in-process mutant **in isolation**, not via cross-test pollution |
| 4 | security-auditor | minor | `.date()` reads the stored value's own offset; a `+05:00` marker matches a UTC day it does not belong to and eats that day's alarm | Applied | Normalised with `astimezone(timezone.utc)` before comparing. Increments B and C copy this idiom |
| 5 | security-auditor | minor | Docstring claimed "the host runs UTC", contradicted by the `TZ=Europe/Moscow` startup guard | Applied | Restated as TZ-independent by construction — both sides come from `datetime.now(timezone.utc)` |
| 6 | code-reviewer | minor | Docstring justified boundary safety via the 07:00 UTC cron, but restart ticks — the reason this gate exists — land at any hour | Applied | The midnight double-ping is now stated as accepted (two pings, not four; still per calendar day) so nobody "fixes" it back into a rolling window |
| 7 | test-reviewer | minor | The `late yesterday` case is the only one that kills a rolling-window implementation, and against the real clock it stops discriminating between 23:30 and 23:59 UTC | Applied | Frozen at 00:05 UTC with `freezegun` (already a dev dep) |
| 8 | test-reviewer | minor | `marked yesterday` (`now - 1 day`) sits exactly on the 24 h boundary — outcome hinges on sub-millisecond ordering and it kills nothing | Applied | Replaced with a mid-yesterday value |
| 9 | test-reviewer, security-auditor | minor | No coverage for the gate itself raising, nor for the non-str value | Applied | Added at both repo and job level; BLOB and int cases added to the garbage set |
| 10 | security-auditor | minor | A stuck container clock pins the gate closed indefinitely | Applied in part | A future-dated marker is now ignored (it can never be overwritten, so honouring it silences forever). Genuine clock-stall backstop stays the external `uptime.yml` watchdog, which does not read the bot's clock |
| 11 | code-reviewer | minor | The suppressed branch logged at INFO, so a multi-day outage shows day one and then nothing to a WARNING grep | Applied | Raised to WARNING — severity should track the condition, not the notification decision |
| 12 | security-auditor | minor | `_parse_dt_tolerant` logs the raw `bot_state` value with `%r` | Skipped | Pre-existing shared helper; the auditor's own assessment is "no exposure with the current key set". It becomes real when increment B adds keys holding tokens and links — recorded there, not fixed here as drive-by scope |
| 13 | code-reviewer | minor (optional) | Third near-identical rate-limit marker pair in the module | Skipped | The reviewer explicitly did not recommend extracting now. Recorded as the threshold to decide at increment C, which would add the fourth |

## Round 2

Verdicts: code-reviewer `approved_with_suggestions` (critical withdrawn — see
below), security-auditor `approved`, test-reviewer `needs_improvement`.

| # | Source | Severity | Finding | Action | Reason |
|---|--------|----------|---------|--------|--------|
| 14 | code-reviewer | ~~critical~~ withdrawn | Three of the new tests failed ~1 in 20 runs: `mark_dry_spell_pinged()` returning normally and leaving no row | No code change | Not a defect. Two other agents and I were installing mutants into `pending_articles_repo.py` / `news_bot.py` in the **shared working tree** while those runs executed. The reviewer reproduced both failures byte-for-byte from named mutants (`M13_wrong_key`, `M9_job_gate_fail_closed`) in an isolated copy. Process lesson recorded: a mutation harness must run on a `git worktree` or a tar-copy, never the tree others are testing against |
| 15 | test-reviewer | major | The guard around `mark_dry_spell_pinged()` survived deletion — nothing pinned it | Applied, after re-scoping | The first attempt asserted the heartbeat still runs; extracting the block into `_maybe_ping_dry_spell()` made the outer handler guarantee that anyway, so the test passed against the mutant. Rewritten to pin what the guard actually buys: the fault is reported as «could not record the [E017] send», not as the generic «channel-silence check failed». Verified against the mutant in an isolated copy |
| 16 | test-reviewer | major | `except (ValueError, TypeError)` survived reversion — the dry-spell gate's blanket `except` masks it, so the test whose docstring claimed to pin it did not | Applied | New test at the real exposure: `is_hold_cap_ping_rate_limited` shares the parser and has no blanket handler. Verified: reverting to `except ValueError` fails it with `TypeError` |
| 17 | security-auditor | minor | The future-dated branch logs the raw value with `%r`; "parses as ISO-8601" does not bound length — `fromisoformat` accepts unlimited fractional digits, so a crafted marker emits a ~500 KB WARNING every tick | Applied | Logs `marked` — the normalised date — which is bounded and is also what the gate actually compared |
| 18 | code-reviewer | minor | The dry-spell block grew from 6 to ~45 lines at six levels of nesting inside a 1000-line `job()` | Applied | Extracted to `_maybe_ping_dry_spell()` |
| 19 | test-reviewer | minor | Two job tests flip when midnight UTC lands 0.1–2.0 s into the run | Applied | `_midday()` helper pins the start to 12:00 UTC with the clock still ticking |
| 20 | code-reviewer | minor | The `12345` garbage case does not exercise the non-str path — TEXT affinity converts it back to `'12345'` | Applied | Dropped, with the reason recorded in the test |
| 21 | test-reviewer | minor | The future-marker case cannot fail under an equality gate — it pins the rule as equality, not the guard | Applied | Comment corrected to say what it actually pins |
| 22 | security-auditor | minor | "Fails OPEN on EVERY path" overstated — a stuck clock fails closed | Applied | Qualified, and `uptime.yml` named as the backstop |
| 23 | code-reviewer | minor | No `project-knowledge` entry for [E017] anywhere, while [E038] is documented in detail | Applied | New `patterns.md` section |
| 24 | code-reviewer | minor | `alert_channel_silent` still told the operator to run `sudo journalctl -u news_bot.service` — dead since the Docker migration, and now seen once per outage day | Applied | Changed to `docker logs --tail 50 hw-news-bot`. Deliberate small scope extension: it is the body of the very alert this increment changes |
| 25 | code-reviewer | minor | These `job()` tests make LIVE network calls — the failure output contained a real OpenRouter balance read | Skipped, recorded | Pre-existing and broad: it spends real money on every test run and couples the suite to a third party. Fixing it means auditing the whole `job()` test surface for un-mocked egress — a separate piece of work, not a drive-by |

### Carried to increment B

- Sweep the `%r` raw-`bot_state` logging in `_parse_dt_tolerant` **and** the copy
  in the future-dated branch — B adds keys holding tokens and links, which is
  when it stops being harmless. One truncation fixes both.
- `journalctl` still appears in `admin_alerts.py:7` (module docstring) and
  `:142` (another alert body). Same dead advice, different alerts.

### Carried to increment C

- The rate-limit marker pair is now the third near-identical copy in
  `pending_articles_repo`. C would add the fourth — decide extraction there.
