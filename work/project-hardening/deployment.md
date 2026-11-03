# Production deployment and automation

Current status: **automatic deployment implemented locally; publication and first
run pending**. On 2026-11-03 the owner authorized production deployment and then
requested automatic deployment. This covers publishing the required workflow
through a green-CI dev→main PR and its normal deployment of current main.
No production access or successful rollout has been established yet.

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

Publication/rollout evidence: pending. The workflow result, deployment artifact
and immutable production tag are authoritative; a green PR alone does not mean
the application was deployed. Do not mark DEPLOY-AUTO-001 done until the actual
production workflow succeeds.

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
