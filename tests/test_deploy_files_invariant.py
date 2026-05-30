"""Invariant test: every new first-party module imported by news_bot.py must
appear in ALL THREE deploy FILES arrays (deploy.sh, deploy.yml, deploy_test.yml).

Skipping any one of them causes `news_bot.service` to ImportError on the next
cron tick on the server, with no CI signal at deploy time (the workflow's own
`if [[ ! -f "$file" ]]` check only fires for files MISSING locally, not for
files missing from the FILES array). Tech-spec Risk R7.

This test mirrors the shell-grep fallback at the deploy stage but runs as part
of the regular `pytest tests/` suite — so the R7 invariant is caught in CI
before merge, not after deploy.

Implementation note: plain substring match on file contents (no bash/YAML AST
parsing). The double-quoted form `"t_hunted_source.py"` is identical in the
bash array and in the YAML `run: |` block, so one assertion form fits all
three files.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_ENTRY = '"t_hunted_source.py"'


def test_t_hunted_source_in_deploy_sh():
    content = (REPO_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert EXPECTED_ENTRY in content, (
        f'{EXPECTED_ENTRY} missing from deploy.sh FILES array — '
        "news_bot.service will ImportError on the next cron tick (Risk R7)."
    )


def test_t_hunted_source_in_deploy_yml():
    content = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    assert EXPECTED_ENTRY in content, (
        f'{EXPECTED_ENTRY} missing from .github/workflows/deploy.yml FILES array — '
        "prod news_bot.service will ImportError on the next cron tick (Risk R7)."
    )


def test_t_hunted_source_in_deploy_test_yml():
    content = (REPO_ROOT / ".github" / "workflows" / "deploy_test.yml").read_text(
        encoding="utf-8"
    )
    assert EXPECTED_ENTRY in content, (
        f'{EXPECTED_ENTRY} missing from .github/workflows/deploy_test.yml FILES array — '
        "test news_bot_test.service will ImportError on the next cron tick (Risk R7)."
    )
