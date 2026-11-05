# Production deployment and automation

Current status: **complete — exact main 77f82f6 is deployed; the full automatic
workflow and production tag succeeded**. On 2026-11-03 the owner authorized production deployment and then
requested automatic deployment. This covers publishing the required workflow
through a green-CI dev→main PR and its normal deployment of current main.
Latest verified production revision: 77f82f611b81d30b8aba8db667cf35fd85a5f4e6,
tag prod-2026-11-05-77f82f6. Latest outcome and recovery references are at the end. No deployment action is pending.

## Automatic GitHub deployment — 2026-11-03

The approved target is still the single Moscow Docker bot at `/opt/hw-news`.
The former NL SCP/systemd workflow is replaced; the retired test workflow stays
disabled. Successful push CI on exact current main triggers the new workflow.
The actual CI test step, main SHA and repository are rechecked. Manual dispatch
uses the same gate. A serialized rollout builds and smoke-checks an image, sends
it with an exact Git bundle, and preserves `.env`, state and shared VPN services.

Recovery has two consistent SQLite backups: a live checkpoint encrypted/uploaded
off-host before cutover and a final snapshot after stopping the old worker.
Private host checkpoints retain old image, Compose and state ownership. Failed
startup restores those without replacing the live database, preserving newly
confirmed publication rows. Only exact state paths are migrated to uid 10001.
The worker's real uid, capabilities, route, startup and existing ledger are checked.
Tags are created only by the successful post-deployment job.

Required one-time configuration is `PROD_SSH_PRIVATE_KEY` plus verified
`PROD_SSH_KNOWN_HOSTS` for Moscow. Availability cannot be inferred from the old
`SSH_*` names. The workflow fails before host access when either is missing.
The current cloud workspace cannot manage these GitHub secrets. Operator setup
and recovery details are in the [deployment runbook](../../.agents/skills/project-knowledge/references/deployment.md#one-time-github-ssh-setup).

Verification before publication:

- Full local Python suite: **2316 passed, 3 skipped, 566 subtests passed**.
  The three skips are the previously recorded source/host-SQLite limits.
- New transport tests run the real shell, generated disposable SSH identity
  and real age encryption/decryption; SSH/SCP/GitHub/server effects are mocked.
- Host transaction tests cover target/CI rejection, pre-stop failures, checksum
  corruption, preserved environment/ledger and rollback including new state files.
- actionlint 1.7.12 and Bash syntax checks passed.
- Actual newly built image `my-hw:auto-deploy-check`, ID
  `sha256:21bdaf93a71f25b8f2e97752f420fcad9d49b1fbbef3105f1b68be5312db46a9`:
  real entrypoint uid/gid 10001, all capability sets zero, NoNewPrivs, core
  imports/four prompt loaders, excluded DB/secrets/controllers and pip check
  passed. The local pip layer used a temporary mounted proxy CA, absent from
  the image; normal Actions builds use the committed Dockerfile unchanged.

Publication and actual first-run evidence:

- [PR #49](https://github.com/alexkoskov/my-hw/pull/49) merged as
  `e43c2b33a1b97314dbbeeee7084c7a072644f0c2`. Implementation head was
  `9d03be926cda399ee492cc20f0166dccd891e897`; actual PR test job
  `126059571451` passed **2317 tests, 2 skips, 566 subtests**.
- [Main push CI](https://github.com/alexkoskov/my-hw/actions/runs/41677231236)
  completed successfully; its actual `Run tests` step passed.
- [First automatic deployment](https://github.com/alexkoskov/my-hw/actions/runs/41677472275)
  was triggered by that CI. `select` passed the exact-main/actual-test gate.
  Deploy job `126061961230` failed at `Configure production SSH identity`:
  the complete pair `PROD_SSH_PRIVATE_KEY`/`PROD_SSH_KNOWN_HOSTS` was unavailable.
  The guard does not establish whether one or both values are missing.
- Build, prepare/SSH, cutover and startup verification were all skipped. No
  production access, database change, restart or production tag occurred.

**Next action:** the operator installs a dedicated public key on Moscow and adds
the private key plus verified host-key entry to GitHub Actions secrets using the
existing runbook. After that change, rerun the failed deployment job or dispatch
`Deploy production` on current main. No repeated attempt is useful before the
configuration changes. Direct SSH access from this Codex environment is not
required for the GitHub runner path. Do not send credentials through chat.

The workflow result, deployment artifact and immutable production tag are
authoritative. DEPLOY-AUTO-001 remains in progress until an actual rollout and
post-start verification succeed. This final status update is published on dev;
the production automation itself is already on main, so a status-only main
promotion is unnecessary while credentials are blocked.

## Earlier manual release preparation (retained, superseded by automation)

## Prepared release

- Approved main: `323c7c4acac74656381e1cfd1a5749998212d260`.
- Target: the existing `hw-news-bot` at `/opt/hw-news` on the documented Moscow VPS.
- Image: `sha256:382393fecedb071944a05bd2671397463e329e3f7b22418a9244d186c064b92a`, identical to the verified runtime-source hashes.
- Local private archive: `/workspace/.local/share/my-hw-production-release-323c7c4/my-hw-main-323c7c4-image.tar`.
- Archive SHA256: `14f741f8cbbfa8bf888d761c54aca464ea0601ac0882db1b9dfa0712f6e93fa4`.
- Archive bytes: 265974784. No production secrets or data are included.
- Deployment tag is pending; it must be created only after successful deployment.

## Earlier direct-access blocker

The selected environment has an empty TCP destination allowlist, no configured
SSH key/agent or managed SSH credential, and no VPN. Server access has not been
attempted through an alternative route. The server, database, permissions,
container and server configuration have not been changed.

The owner needs to configure supported TCP access to the production SSH endpoint
and supply SSH authentication through the environment's protected credentials.
Passwords/private keys must not be sent in chat. Configuration changes use the
environment's supported review workflow; project code cannot grant network access.

## Manual fallback only after supported access is configured

1. Inspect current production revision, running image, clean worktree, exact
   volume source, credential presence (booleans only), existing ledger/integrity,
   state-file ownership, VPN gateway and free disk space. Record the observed
   rollback image/revision; never infer them from dated journals.
2. Preserve a consistent database backup and an off-host recovery copy. Preserve
   the running image and Compose configuration for rollback. Stop only this bot
   before changing ownership of the inspected database/state paths.
3. Follow the existing runbook's exact-path uid/gid 10001 migration. Do not change
   unrelated files, shared VPN services, secrets, or create an empty database.
4. Update the clean production checkout to the approved main revision, load the
   verified image and validate its digest/source association before recreation.
   Preserve the existing .env and persistent bind mount.
5. Verify actual startup, uid/capabilities, route, persistent ledger, lock and
   affected runtime behavior. A restart replans remaining daily slots and may
   perform normal scheduled publishing; no extra test post is authorized.
6. Only after success, tag the exact deployed main revision and update this
   record and the single current handoff. If startup fails, use the recorded
   rollback image/configuration without blindly restoring an older database.

## Credential verification retry — 2026-11-05

The owner reported setting both production secrets after demonstrating local
`SSH_OK` with the existing id_ed25519 identity and `KEY_READY` from passwordless
private-key parsing. Those outputs establish local key usability, not the exact
values pasted into GitHub.

Verified current main is still `e43c2b33a1b97314dbbeeee7084c7a072644f0c2`.
Reran failed jobs of run `41677472275`, attempt 2. Selection job `127165906923`
succeeded. Deployment job `127165905677` failed at `Configure production SSH
identity` on 2026-11-05 at 12:37:59 Moscow time. The missing-secret-pair error
was absent, so both variables were nonempty. No private-key parsing error was
reported. The only runtime diagnostic was exit code 1; the current workflow
has silent key-type and known-host lookup guards, so the log does not uniquely
identify which one failed. Do not assert that either secret's exact contents
were inspected or that host pinning succeeded.

Build, prepare/SSH, backup, cutover and verification were skipped; the record/tag
job was skipped. No production connection, restart, DB change or tag was made.
The owner supplied both safe outputs: the client type is ssh-ed25519, and the
server public-key entry is a valid Ed25519 known_hosts line. Local ssh-keygen
checks confirmed the entry parses and matches the intended host. These are
public inputs, not an inspection of the protected GitHub values. The owner was
asked to replace PROD_SSH_KNOWN_HOSTS with that exact single line, or confirm
that it already matches. Await that response before retrying; do not repeat an
unchanged failing setup blindly.

## Configured SSH and production-worktree blocker — 2026-11-05

After the owner updated PROD_SSH_KNOWN_HOSTS, attempt 3 of run `41677472275`
passed SSH identity validation and real image build/smoke/pip checks. Recovery
preparation failed with only exit 1 because its structured host error was
redirected to a temporary file that the original transport did not publish.

Corrected only the transport's failure reporting in commit
`d0af7978a2fbe993d34774bea15908149db4cdf3`. Structured sanitized reasons are
bounded and retained in the existing result artifact; absent/malformed JSON
uses a generic reason and extra fields are not echoed. Activation/rollback and
application code did not change. Focused deployment suite: 48 passed. All scoped
configured checks passed after correcting the isolated checkout's hook-cache
location and installing the hook; the initial install had failed on read-only
home cache, so the first commit did not run the hook automatically.

[PR #50](https://github.com/alexkoskov/my-hw/pull/50) merged after its actual CI
job `127174985506` passed 2319 tests, 2 skips and 566 subtests. Current main and
remote dev are `0fdb84ff867b0886829e3ef94c896725c32f76ac`; main push CI
`42006862518` passed. The clean release checkout is
`/workspace/my-hw-deploy-recovery` on dev at that revision. The original checkout
keeps its uncommitted operational records intact and has not been integrated
while dirty; only our copied transport experiment was removed there.

[The next automatic deployment](https://github.com/alexkoskov/my-hw/actions/runs/42007051334)
passed selection, SSH setup and image checks. Deployment job `127176469388`
returned the actual host reason: `production checkout has local changes`.
Sanitized evidence artifact `13405640650` (`production-result-42007051334-1`)
was uploaded. This confirms working GitHub-to-production SSH authentication.
Preparation stopped in the worktree preflight, before backup, bot stop, ownership
migration, checkout update or activation. The private checkpoint directory/lock
can have been initialized by the host helper. No bot restart, article repost,
DB rollback or production tag occurred.

**Next step at that point:** await the owner's output of the single SSH read showing
`git -C /opt/hw-news status --short --branch`, `diff --stat` and the current short
commit. Preserve those existing server changes; determine whether they are
runtime configuration, permissions-only changes or code before choosing a safe
integration. Do not discard, overwrite, force/reset, bypass the dirty-tree guard
or rerun the unchanged failing rollout. Private keys/DB/.env contents are not
requested. Deployment remains authorized but incomplete.

## Legacy state bootstrap and activation failure — 2026-11-05

The owner supplied production metadata: `main...origin/main`, only untracked
`backups/` and `data/`, no tracked diff, current commit
`cda27b7b9056dc8630570dbd998bc7e12d174c7c`. Git confirmed it is an ancestor of
main, so no history rewrite is needed.

[PR #51](https://github.com/alexkoskov/my-hw/pull/51) added exact rooted
/data/ and /backups/ exclusions in .gitignore and an idempotent legacy bootstrap
in the host helper's .git/info/exclude. Other local source/untracked changes,
tracked state and metadata symlinks remain blockers. State bytes/permissions and
existing exclusion rules are preserved. Focused suite: 55 passed; strengthened
committed-state rejection passed separately. Actual PR CI job `127185150904`
passed 2326 tests, 2 skips and 566 subtests. Commit f8f8cc5 was merged as
`fb86bc00483d888ebd8a6851b013960c52bd44f1`; clean release checkout and remote
dev/main were verified synchronized there. The original dirty checkout was
preserved without integration.

[Deployment run 42010113508](https://github.com/alexkoskov/my-hw/actions/runs/42010113508)
passed exact-main selection, SSH setup, actual image checks, host preparation,
consistent live database backup, encryption/decryption comparison and off-host
backup upload. Encrypted backup artifact: `13407120285`, name
`production-backup-42010113508-1`, expires 2026-11-19.

Activation job `127186760893` then failed. A best-effort copy found no cutover.db;
this does NOT by itself prove that docker stop was never attempted. Do not claim
that the old bot is running or a rollback succeeded until the host result is read.
Sanitized result artifact `13407035690` (`production-result-42010113508-1`)
exists. Record/tag job was skipped. No successful deployment/tag is confirmed.

The GitHub connector downloaded the artifact into File Service and returned a
reference, but this cloud executor's proxy denies access to its signed download
host. Direct urllib failed; the configured project requests client confirmed a
proxy access denial. Do not bypass this policy or retry that destination. The
system Python lacked requests; the existing project interpreter was used instead,
without installing dependencies. No database backup was downloaded or decrypted.

One bounded local disconfirmation check used the real Compose config command on
the candidate YAML with an empty synthetic .env: the configuration/validator
passed, so do not blame the generic current Compose contract without evidence.
No local container or live publishing loop was started for that check.

**Next action at that point:** await the owner's text from the short
production-result.json in artifact 13407035690. It contains only the host helper's
status/reason. Determine whether the failure was before cutover or reports a
recovery state; verify live health/rollback before another mutation. Do not rerun
activation, restore an older DB, or discard server state while that is unknown.

## Docker 29 image-identity correction — in progress, 2026-11-05

The owner supplied the artifact JSON: status failed, reason `image identity does
not match the tested revision`. This specific guard precedes docker stop, state
ownership changes and checkout promotion, so that failed attempt did not enter
cutover. The owner also supplied actual image metadata: Docker 29.6.1 returns
ID sha256:628dacbac9f5a10d2cbc51e4363ce3ddd3ae11a678d58fc933981e2390207632
and the correct fb86bc0 revision label. The Actions build logged
sha256:9aa961a0f34362f2c6b75ce061ea7d36f859b463a2ed6ec6a39f3c2c0172c4d9.

Moby containerd inspection uses the target descriptor digest for Id; classic
Docker uses a configuration-based ID. Primary source inspection confirmed
containerd GetImage also uses that native target handle:
https://github.com/moby/moby/blob/master/daemon/containerd/image_inspect.go
and https://github.com/moby/moby/blob/master/daemon/containerd/image.go .

PR #52 (head 3cf9f35) replaces only the cross-host ID comparison with a schema-1
SHA256 of ordered layer DiffIDs, platform and every non-default runtime config
field. Omitted empty API defaults are normalized; nonempty nested metadata and
ordered lists remain covered. Exact revision label and archive/source-bundle
checksums are still mandatory. The server's native ID is used only after content
verification for local run/tag/startup checks. Missing/tampered metadata refuses
before worker stop. Application runtime is unchanged.

Both failure phases now log/preserve sanitized status/reason, and backup-copy
failures stop immediately even in best-effort recovery mode. No further artifact
fetch is needed for ordinary host-helper errors. Configured commit hooks passed;
67 focused deployment tests passed, including tampering, native IDs, empty API
defaults and the real shell/age transport. Actual PR CI/promotion/deployment are
pending. Current next action is continuing PR #52 and verifying rollout, not
asking the owner for another credential or artifact.

## Successful automatic production release — 2026-11-05

**Outcome:** the owner-requested production deployment and automatic deployment
setup are complete. [Run 42016189442](https://github.com/alexkoskov/my-hw/actions/runs/42016189442)
completed successfully on its first attempt. Select, deploy and record jobs all
ran and succeeded. Deployed main is
`77f82f611b81d30b8aba8db667cf35fd85a5f4e6`; automatic lightweight tag
`prod-2026-11-05-77f82f6` was verified to point to that exact commit.

Native production image ID:
`sha256:265761ccccb09e8a038126785cad963bbde433d5121fa79c6db4731e492668a7`.
Deployment job `127207265475` reported deployed at **14:09:19 Moscow time**.
Record job `127208747632` succeeded. Main push CI `42015994733` passed;
PR #53 job `127205312592` passed **2343 tests, 2 skips, 566 subtests**.

The real host helper verified the imported content fingerprint/revision and
source bundle before cutover. Actual worker checks passed uid/gid 10001, all
capability sets zero, NoNewPrivs, default route via the shared VPN gateway,
expected native image, no startup restart, both startup markers, SQLite
quick_check and preservation of every prior processed link. These are startup
and state-integrity checks, not a new synthetic channel publication or a
claim that every future external API response is reliable.

Data/backups stayed in place. Existing .env was preserved; only the inspected
state directory/files were migrated to uid 10001 when required. Shared VPN and
other services were not managed. Host rollback checkpoints/images were retained.
The exact latest checkpoint is:
`/opt/hw-news-deployments/77f82f611b81d30b8aba8db667cf35fd85a5f4e6-42016189442-1/checkpoint.json`.
Its rollback image is the prior verified 8b9c544 image
`sha256:496b6f10df0a1d812307002086ae6a253780f71fd9ccc364bdd92b01fb16435d`.
Never restore an old DB over newly confirmed publication history blindly.

Verified encrypted off-host artifacts for the final run:

- Before cutover: `production-backup-42016189442-1`, ID **13410135163**.
- Final stopped-worker snapshot: `production-cutover-42016189442-1`, ID **13409831157**.
- Sanitized outcome: `production-result-42016189442-1`, ID **13409960780**.

Both backup artifacts expire **2026-11-19**; result expires **2026-12-05**.
The owner's corresponding SSH private key is needed for age decryption; it
was never posted in chat or copied to project files. Cloud artifact downloads
remain restricted, but all success evidence was read through supported GitHub
run/job/log/ref APIs. No additional cloud TCP/SSH setup is needed for this path.

Release corrections and evidence:

- PR #50: preserve/print sanitized preparation errors; 48 deployment tests.
- PR #51: exact state-directory exclusions; 55 deployment tests, full CI 2326 passed.
- PR #52: portable execution-content fingerprint and host-native handles;
  67 deployment tests, full CI 2338 passed. The owner-supplied failure and
  server metadata confirmed the earlier refusal was before worker stop.
- PR #53: clear stdout from failed gh lookups instead of treating error JSON
  as a conflicting tag. Five real-shell tag tests cover missing/base/suffix/
  collision cases; the two missing-tag cases reproduced the bug before the fix.
  Actionlint and all configured commit hooks passed. This completed automatic
  same-day suffix registration in the final run.

Interim verified release 8b9c544 is retained as annotated `prod-2026-11-05`;
its tag target was verified and run 42014148761 completed green after its record
job retry. No existing tag was overwritten. The final run created its suffix
tag automatically. Runtime post format was preserved; the earlier delayed
T-Hunted preview incident requires no new fix.

**Continuation:** none for deployment. Future approved main pushes deploy only
following successful actual CI, and manual retries use the same revision gate.
Status-only closeout stays on dev and does not justify another main promotion
or restart. The project roadmap's DBP-015/STAB-002/OBSV-002 remain separate.
