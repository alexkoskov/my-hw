# Hardening verification — 2026-11-03

Scope: hardening implementation based on `b2cecbedaf5082882c49694db201ffc7ec608d23`,
first published on `dev` at `ecdaea4` in [PR #48](https://github.com/alexkoskov/my-hw/pull/48).
The PR records the latest publication head, CI and final merge outcome.
Production and GitHub repository settings were not changed. The runtime-source
hashes in [verified-source-hashes.json](logs/verified-source-hashes.json) match
the files used for the final image. The editorial prompt is byte-identical to
the baseline.

## Behavior checks

| Check | Result |
| --- | --- |
| Baseline full suite | 2128 passed, 2 skipped, 556 subtests passed |
| Final full suite, Python 3.13.15 / Requests 2.34.2 | 2276 passed, 3 skipped, 566 subtests passed, 27.80 s |
| LLM/provider/publisher focused checks | 341 passed, 19 subtests passed |
| Data/DOM/model/precision/golden/backfill checks | 399 passed, 40 subtests passed |
| Source/helper checks, including 20 actual local HTTP cases | 277 passed, 2 skipped |
| Runtime combined checks | 410 passed, 88 subtests passed; final targeted changes: 138 passed, 12 subtests passed |
| Operations/classifier/manifest checks | 67 passed, 1 skipped |
| All configured pre-commit hooks on intended files | Passed |
| Additional Gitleaks scan of intended added content | No findings |
| Native Codex discovery/configuration | Seven required skills and seven project-role registrations, no model turns |

Two skips are the existing source-fixture skips; the new third skip requires
the host `sqlite3` executable. Its real shell backup/restore behavior was
executed successfully in a separate QA image with SQLite CLI installed.
This is local evidence, not a newly executed GitHub CI run.

The first integrated run found two Mattel tests whose mocked HTTP responses
omitted `status_code`. Explicit successful status 200 was added to that fixture;
no behavior assertions were removed. The file and subsequent full suite passed.

## Actual image and startup

Final local image: `my-hw:hardening-verified`.
Image ID: `sha256:382393fecedb071944a05bd2671397463e329e3f7b22418a9244d186c064b92a`.
Base reference from the actual build log:
`python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1`.
Platform: Linux/amd64. These identify a newly built verification image, not the
image currently deployed on the server.

The build used the project Dockerfile with only the pip RUN adapted to mount
the managed session's CA as a temporary BuildKit secret. TLS verification stayed
enabled; the CA mount was absent from the final container.

The context came from current tracked/untracked project files, augmented only
with synthetic root/data/backups/nested DB, WAL/SHM and `.env` sentinels. The
same baseline test included nested backups; the corrected final image excludes
them. Application code and prompt permissions were checked as the actual worker.

The real committed entrypoint was used, without replacing it, with a disposable
internal Docker network and Compose-equivalent production settings. Root sets
the isolated gateway, then the Python process runs as uid/gid 10001. Effective,
permitted, inheritable, ambient and bounding capabilities are all zero;
NoNewPrivs is 1. The code is not writable, the route remains, and attempting to
change it is denied. The shared lock excludes another process. Four native
provider prompt loaders work. A recreated container sees the original sentinel
and the test write; SQLite integrity is `ok`.

Unprepared root-owned and missing-database volumes both refuse startup before
the worker command runs. No chown of existing production state was attempted.
The separately authorized rollout must follow the exact-path ownership and
backup procedure in the [deployment runbook](../../.agents/skills/project-knowledge/references/deployment.md).

Evidence:

- [Image identity](logs/final-image.json), [first entrypoint run](logs/container-entrypoint.json),
  [recreated container](logs/container-recreated.json), [safe refusals](logs/container-refusals.json).
- [Shell backup/restore](logs/backup-restore.json): the actual `scripts/backup_db.sh`
  creates a consistent backup; a separate restored file has the sentinel row
  and passes `PRAGMA integrity_check`, running as uid 10001.
- The exact offline [container probe](logs/container-probe.py) and
  [backup probe](logs/backup-probe.py) are preserved as test evidence. They are
  for synthetic disposable state only, never a production publishing check.
- [Native Codex result](logs/native-codex.json).

## Resolved dependencies and audit triage

- [Baseline Python packages](logs/baseline-image.json),
  [baseline audit](logs/baseline-dependency-audit.json).
- [Final Python packages](logs/final-python-packages.json),
  [final OS package versions](logs/final-os-packages.tsv),
  [final Python audit](logs/final-dependency-audit.json).

`pip check` passed for the final image. Auditing its complete installed Python
package list removed both Requests findings: CVE-2024-47081 and CVE-2026-25645.
Requests is pinned and actually installed at 2.34.2. Duplicate advisory aliases
in the baseline JSON do not represent additional independent vulnerabilities.

One signal remains disputed: `PYSEC-2022-252` on deep-translator 1.11.4. The
[primary advisory](https://github.com/pypa/advisory-database/blob/main/vulns/deep-translator/PYSEC-2022-252.yaml)
explicitly enumerates malicious version **1.8.5**, while its ecosystem range
starts at 1.8.5 without a closing event. The auditor consequently also flags
1.11.4. This is not proof that the installed 1.11.4 is compromised; it is also
not a clean audit result. No broad ignore or unverified replacement was added.
The raw finding remains in evidence for an upstream range correction or further
artifact review. OS package versions are recorded; an OS-image CVE scan was
not performed.

## Limits and next release prerequisites

- Requests has byte bounds, a body deadline and a short inactivity timeout.
  Slow HTTP headers/chunk framing are not covered by a strict whole-request
  deadline. curl_cffi retains its transport total timeout and bounded callback.
- No exactly-once guarantee is claimed when an external API succeeds but its
  response is lost. Saved stages and confirmed in-process sends are reused.
- Historical fingerprints were not bulk-recomputed. Adjacent-model and alias
  fixes intentionally correct those input cases; thresholds stay unchanged and
  the existing precision corpus passes.
- Existing production state needs inspected, explicit uid/gid 10001 migration
  at a later authorized rollout; reverting an image also needs a permissions-
  compatible rollback plan.
- STAB-002, OBSV-002 and DBP-015 keep their original remaining acceptance.
  No deployment, promotion, GitHub protection changes, new reminders, broad
  history cleanup or unmeasured indexing/caching refactor occurred.

## Telegram preview incident — resolved observation, 2026-11-03

The owner initially supplied a screenshot of the 19:30 T-Hunted post showing
only `#thunted #news`. The sender puts the article URL in preview options; an
offline mock confirmed that send success alone does not verify card rendering.
A local candidate with a separate article link passed 125 focused tests and
26 subtests, but was never committed, published or deployed.

The owner subsequently confirmed that the card loaded and supplied a second
screenshot showing the title, excerpt, hero image and INSTANT VIEW button above
the same tags. This establishes delayed preview display in this incident, not
permanent loss of the post. The exact Telegram/cache/client layer and delay
length were not measured; full article contents were not inspected.

The unapproved format-change candidate and its tests were removed. Runtime and
Telegram tests match the previously verified HEAD exactly. Existing post format
is retained; no separate link, repost, live message edit or production change
was made. No additional incident task remains on the approved roadmap. Keep
this observation here rather than reopening the incident on a future resume.

## Production follow-up — completed 2026-11-05

The release corrections in PRs #50–53 passed their actual CI test jobs. Final
suite: 2343 passed, 2 skipped, 566 subtests; focused deployment checks: 67 passed
plus five tag shell cases. Exact main 77f82f6 deployed successfully in workflow
42016189442, including actual rootless worker, route/capabilities, startup and
prior ledger verification. Automatic production tag and both encrypted backups
were verified through GitHub APIs/logs. The complete current release/image/
artifact/rollback evidence is in
[the deployment record](deployment.md#successful-automatic-production-release--2026-11-05).
The older local-image IDs and source-hash manifests above remain historical;
they are not the production image ID for this later release.
