from __future__ import annotations

import re
from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/uptime.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _step(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = workflow.index(marker)
    end = workflow.find("\n      - name: ", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def _section(step: str, start_marker: str, end_marker: str | None = None) -> str:
    start = step.index(start_marker)
    if end_marker is None:
        return step[start:]
    return step[start : step.index(end_marker, start)]


def _case_branch(step: str, label: str) -> str:
    match = re.search(
        rf"^\s+{re.escape(label)}\)\n(?P<body>.*?)(?=^\s+(?:stale|fresh|inconclusive|\*)\)\n)",
        step,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing {label!r} publication-state branch"
    return match.group("body")


def test_checkout_is_immutable_explicit_and_failure_tolerant() -> None:
    workflow = _workflow()
    ssh_step = _step(workflow, "Probe sshd greeting")
    checkout = _step(workflow, "Checkout publication classifier")

    assert workflow.index(ssh_step) < workflow.index(checkout)
    assert (
        "uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
        in checkout
    )
    assert "id: watch_source" in checkout
    assert "ref: ${{ github.event.repository.default_branch }}" in checkout
    assert "persist-credentials: false" in checkout
    assert "fetch-depth: 1" in checkout
    assert "continue-on-error: true" in checkout


def test_telegraph_secret_is_confined_to_bounded_fetch_step() -> None:
    workflow = _workflow()
    fetch = _step(workflow, "Fetch Telegraph evidence")
    classifier = _step(workflow, "Classify publication evidence")
    fetch_commands = "\n".join(
        line for line in fetch.splitlines()
        if not line.lstrip().startswith("#")
    )

    assert "${{ secrets.TELEGRAPH_ACCESS_TOKEN }}" in fetch
    assert "${{ secrets.TELEGRAPH_ACCESS_TOKEN }}" not in workflow.replace(fetch, "")
    assert "TELEGRAPH_ACCESS_TOKEN" not in classifier
    assert "$RUNNER_TEMP/publication-watch-evidence.json" in fetch
    assert "$RUNNER_TEMP/publication-watch-evidence.json" in classifier
    assert "--max-filesize 1048576" in fetch
    assert "--get" not in fetch_commands
    assert "--data-urlencode 'access_token@/dev/fd/3'" in fetch_commands
    assert "--data-urlencode 'limit=1'" in fetch_commands
    assert "head -c 1048577" in fetch
    assert "1048576" in classifier
    assert "$GITHUB_OUTPUT" not in fetch
    assert "TELEGRAPH_BODY" not in workflow
    assert "body=$(" not in workflow


def test_classifier_runs_only_after_successful_checkout() -> None:
    workflow = _workflow()
    classifier = _step(workflow, "Classify publication evidence")
    verdict = _step(workflow, "Decide")

    assert "steps.watch_source.outcome == 'success'" in classifier
    assert "publication_state=inconclusive" in classifier
    assert "[ ! -f publication_watch.py ]" in classifier
    assert "[ ! -s \"$evidence_file\" ]" in classifier
    assert '"$evidence_bytes" -gt 1048576' in classifier
    assert "timeout 10 python3 publication_watch.py" in classifier
    assert '< "$evidence_file"' in classifier
    assert "cmp -s" in classifier
    assert "fresh stale inconclusive" in classifier
    assert "continue-on-error: true" in classifier
    assert "PUB_STATE: ${{ steps.pub.outputs.state }}" in verdict
    assert "PUB_STATE=inconclusive" in verdict
    assert "import datetime" not in workflow
    assert "json.loads" not in workflow
    assert "PYEOF" not in workflow


def test_publication_alarm_uses_complete_tri_state_matrix() -> None:
    workflow = _workflow()
    alert = _step(workflow, "Alert or recover")
    publication = _section(alert, "# ---- publication alarm")

    stale = _case_branch(publication, "stale")
    fresh = _case_branch(publication, "fresh")
    inconclusive = _case_branch(publication, "inconclusive")

    assert "raise \"$PUB_TITLE\"" in stale
    assert "resolve \"$PUB_TITLE\"" not in stale
    assert "resolve \"$PUB_TITLE\"" in fresh
    assert "raise \"$PUB_TITLE\"" not in fresh
    assert "raise \"$PUB_TITLE\"" not in inconclusive
    assert "resolve \"$PUB_TITLE\"" not in inconclusive
    assert "publication alarm unchanged" in inconclusive
    assert "PUB_STATE: ${{ steps.verdict.outputs.pub_state }}" in alert
    assert "PUB_DOWN" not in workflow


def test_host_verdict_remains_independent_and_suppresses_publication_transition() -> None:
    workflow = _workflow()
    ssh_step = _step(workflow, "Probe sshd greeting")
    checkout = _step(workflow, "Checkout publication classifier")
    fetch = _step(workflow, "Fetch Telegraph evidence")
    classifier = _step(workflow, "Classify publication evidence")
    verdict = _step(workflow, "Decide")
    alert = _step(workflow, "Alert or recover")

    assert "\n        if:" not in ssh_step
    assert workflow.index(ssh_step) < workflow.index(checkout)
    assert workflow.index(ssh_step) < workflow.index(fetch)
    assert workflow.index(ssh_step) < workflow.index(classifier)
    assert "if: ${{ always() }}" in verdict
    assert "SSH_OK: ${{ steps.ssh.outputs.ok }}" in verdict

    host_alarm = _section(alert, "# ---- host alarm", "# ---- publication alarm")
    publication_alarm = _section(alert, "# ---- publication alarm")
    suppression = publication_alarm[: publication_alarm.index("          else\n")]

    assert "raise \"$HOST_TITLE\"" in host_alarm
    assert "resolve \"$HOST_TITLE\"" in host_alarm
    assert "PUB_STATE" not in host_alarm
    assert 'if [ "$HOST_DOWN" = true ]; then' in suppression
    assert "publication alarm suppressed" in suppression
    assert "raise \"$PUB_TITLE\"" not in suppression
    assert "resolve \"$PUB_TITLE\"" not in suppression
