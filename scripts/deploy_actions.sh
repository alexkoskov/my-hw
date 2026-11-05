#!/usr/bin/env bash
# Runner-side transport. Credentials remain in private runner files only.
set -euo pipefail

[[ ${DEPLOY_SHA:-} =~ ^[a-f0-9]{40}$ ]]
[[ ${GITHUB_RUN_ID:-} =~ ^[0-9]+$ && ${GITHUB_RUN_ATTEMPT:-} =~ ^[0-9]+$ ]]
[[ ${PROD_HOST:-} == 45.90.216.165 ]]
[[ ${RUNNER_TEMP:-} == /* && $RUNNER_TEMP != / && -d $RUNNER_TEMP ]]
umask 077
release="$DEPLOY_SHA-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
stage="/opt/hw-news-deployments/$release"
identity="$RUNNER_TEMP/prod-ssh/key"
known_hosts="$RUNNER_TEMP/prod-ssh/known_hosts"
recipient="$RUNNER_TEMP/prod-ssh/recipient"
ssh_options=(-i "$identity" -o BatchMode=yes -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known_hosts"
  -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)

remote() {
  ssh "${ssh_options[@]}" "root@$PROD_HOST" python3 - "$@" < scripts/deploy_production.py
}

current_main() {
  [[ $GITHUB_REPOSITORY == alexkoskov/my-hw ]]
  [[ $(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq '.object.sha') == "$DEPLOY_SHA" ]]
}

copy_backup() {
  local name=$1
  [[ $name == before || $name == cutover ]]
  scp "${ssh_options[@]}" "root@$PROD_HOST:$stage/$name.db" "$RUNNER_TEMP/$name.db" || return
  age -R "$recipient" -o "$RUNNER_TEMP/$name.db.age" "$RUNNER_TEMP/$name.db" || return
  # Verify that the operator's same private SSH key can recover this artifact.
  age -d -i "$identity" "$RUNNER_TEMP/$name.db.age" | cmp - "$RUNNER_TEMP/$name.db" || return
  rm -f -- "$RUNNER_TEMP/$name.db"
}

report_failure() {
  # Both remote phases emit the same sanitized status/reason contract.
  python3 - "$1" "$RUNNER_TEMP/production-result.json" "$2" <<'PY'
import json,pathlib,sys
reason = f'SSH {sys.argv[3]} failed without a structured host result'
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text())
    if isinstance(data, dict) and data.get('status') == 'failed' and isinstance(data.get('reason'), str):
        reason = data['reason'][:500]
except (OSError, ValueError):
    pass
result = json.dumps({'status': 'failed', 'reason': reason})
pathlib.Path(sys.argv[2]).write_text(result + '\n')
print(result)
PY
}

case ${1:-} in
  prepare)
    current_main || { echo 'A newer main exists; this release will not deploy.' >&2; exit 1; }
    if ! remote prepare "$release" > "$RUNNER_TEMP/production-prepared.json"; then
      report_failure "$RUNNER_TEMP/production-prepared.json" preparation
      exit 1
    fi
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"] in ("prepared","already_deployed") and d["sha"]==sys.argv[2]' "$RUNNER_TEMP/production-prepared.json" "$DEPLOY_SHA"
    copy_backup before
    ;;
  activate)
    current_main || { echo 'A newer main exists; this release will not deploy.' >&2; exit 1; }
    if python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1]))["status"] != "already_deployed")' "$RUNNER_TEMP/production-prepared.json"; then
      python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); d["status"]="deployed"; d["already_current"]=True; print(json.dumps(d))' "$RUNNER_TEMP/production-prepared.json" > "$RUNNER_TEMP/production-result.json"
      python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps({k:d[k] for k in ("status","sha","image","already_current") if k in d}))' "$RUNNER_TEMP/production-result.json"
      exit 0
    fi
    scp "${ssh_options[@]}" "$RUNNER_TEMP/image.tar.gz" "$RUNNER_TEMP/source.bundle" "root@$PROD_HOST:$stage/"
    content_identity=$(python3 scripts/deploy_production.py image-identity "hw-news-bot:$DEPLOY_SHA")
    image_hash=$(sha256sum "$RUNNER_TEMP/image.tar.gz" | cut -d ' ' -f 1)
    bundle_hash=$(sha256sum "$RUNNER_TEMP/source.bundle" | cut -d ' ' -f 1)
    if ! remote activate "$release" "$content_identity" "$image_hash" "$bundle_hash" > "$RUNNER_TEMP/production-result.json"; then
      report_failure "$RUNNER_TEMP/production-result.json" activation
      copy_backup cutover || true
      exit 1
    fi
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"]=="deployed" and d["sha"]==sys.argv[2]' "$RUNNER_TEMP/production-result.json" "$DEPLOY_SHA"
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps({k:d[k] for k in ("status","sha","image") if k in d}))' "$RUNNER_TEMP/production-result.json"
    copy_backup cutover
    ;;
  cleanup)
    if [[ -s $identity && -s $known_hosts && -f $RUNNER_TEMP/production-prepared.json ]]; then
      remote cleanup "$release" >/dev/null || echo 'Incoming archive cleanup needs operator attention.' >&2
    fi
    rm -f -- "$identity" "$known_hosts" "$recipient" "$RUNNER_TEMP/before.db" "$RUNNER_TEMP/cutover.db" "$RUNNER_TEMP/image.tar.gz" "$RUNNER_TEMP/source.bundle"
    ;;
  *) echo 'Expected prepare, activate or cleanup.' >&2; exit 1 ;;
esac
