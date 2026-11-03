"""Runtime import closure in the two disabled historical SCP manifests.

Production deploys a complete Docker image; the executable release gate and
transaction tests live in test_production_deploy.py. Keep the remaining legacy
arrays consistent without pretending that they verify current image contents.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEPLOY_FILES = {
    "deploy.sh": REPO_ROOT / "deploy.sh",
    "deploy_test.yml": REPO_ROOT / ".github" / "workflows" / "deploy_test.yml",
}

#: Entry point whose runtime needs define the manifest.
ENTRY_MODULE = "news_bot"


def _parse_files_array(path: Path) -> set[str]:
    """Extract the quoted entries of the `FILES=( ... )` array.

    The double-quoted form is identical in the bash array and inside the YAML
    `run: |` block, so one parser fits both files.
    """
    text = path.read_text(encoding="utf-8")
    match = re.search(r"FILES=\((.*?)\n\s*\)", text, re.S)
    assert match, f"no FILES=( ... ) array found in {path.name}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _local_modules() -> set[str]:
    """Module names importable from the repo root (first-party, un-namespaced)."""
    return {p.stem for p in REPO_ROOT.glob("*.py")}


def _direct_imports(module: str, local: set[str]) -> set[str]:
    """First-party modules imported by `module`, including lazy in-function ones.

    Lazy imports count: `llm_transcreation._select_engine` imports each engine
    inside a function, and a missing engine still crashes the run that selects it.
    """
    source = REPO_ROOT / f"{module}.py"
    if not source.exists():
        return set()
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & local


def _runtime_closure() -> set[str]:
    """Every first-party module reachable from the entry point, transitively."""
    local = _local_modules()
    seen: set[str] = set()
    stack = [ENTRY_MODULE]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(_direct_imports(current, local) - seen)
    seen.discard(ENTRY_MODULE)
    return seen


def test_retained_manifests_are_identical():
    """Retained legacy manifests must not drift."""
    arrays = {name: _parse_files_array(path) for name, path in DEPLOY_FILES.items()}
    reference_name, reference = next(iter(arrays.items()))
    for name, entries in arrays.items():
        missing = reference - entries
        extra = entries - reference
        assert not (missing or extra), (
            f"{name} FILES array differs from {reference_name}: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


def test_every_runtime_module_is_in_every_manifest():
    """Derived, not hardcoded: whatever news_bot imports must ship."""
    required = {f"{module}.py" for module in _runtime_closure()}
    assert required, "import closure came out empty — the parser is broken, not the manifest"

    for name, path in DEPLOY_FILES.items():
        entries = _parse_files_array(path)
        missing = sorted(required - entries)
        assert not missing, (
            f"{name} FILES array is missing {missing} — news_bot would ImportError "
            "on the next tick with no CI signal (Risk R7). Add them to both retained "
            "manifests."
        )


def test_feature_flags_is_in_every_manifest():
    """Addressed check, on purpose — the derived test above does NOT cover
    ``feature_flags.py`` yet.

    Nothing in news_bot's import closure imports it until Task 7 of
    source-formatting-parity wires it into ``t_hunted_source`` (which IS in
    the closure, pinned by ``test_closure_covers_the_known_runtime_modules``).
    Until then the entry rests solely on
    ``test_retained_manifests_are_identical`` — and that one catches only
    DIVERGENCE between manifests: forgetting the module in both at once
    slips through.

    After Task 7 this becomes a duplicate safety net. Leave it: an explicit
    check costs nothing and survives the module being unwired again.
    """
    for name, path in DEPLOY_FILES.items():
        entries = _parse_files_array(path)
        assert "feature_flags.py" in entries, (
            f"feature_flags.py missing from {name} FILES — the source-formatting "
            "kill switch would not reach the server."
        )


def test_closure_covers_the_known_runtime_modules():
    """Guard the guard: if the AST walk silently stopped finding imports, the
    test above would pass vacuously. Pin a few modules that must always be in the
    closure — the LLM dispatcher, a source parser and the publisher."""
    closure = _runtime_closure()
    for module in ("llm_transcreation", "_llm_common", "telegraph_publisher",
                   "t_hunted_source", "pending_articles_repo", "dom_blocks"):
        assert module in closure, (
            f"{module} vanished from news_bot's import closure — either it was "
            "genuinely removed, or the import parser stopped working and this "
            "whole test file is now vacuous."
        )


def test_non_deployed_tools_stay_out():
    """`hw_review.py` and `preview_renderer.py` are the archived operator-side
    CLI (2026-04-30). They run in the operator's local session, never on the
    server, so they must not creep into the manifest."""
    for name, path in DEPLOY_FILES.items():
        entries = _parse_files_array(path)
        for tool in ("hw_review.py", "preview_renderer.py"):
            assert tool not in entries, (
                f"{tool} appears in {name} FILES — it is operator-local only "
                "(archived 2026-04-30) and should not ship to the server."
            )


def test_runtime_prompt_exists_and_ships_in_every_manifest():
    from _llm_common import _PROMPT_PATH_DEFAULT_PARTS

    relative = Path(*_PROMPT_PATH_DEFAULT_PARTS)
    assert (REPO_ROOT / relative).is_file(), "The runtime editorial prompt is missing"
    for name, path in DEPLOY_FILES.items():
        assert relative.as_posix() in _parse_files_array(path), (
            f"{name} omits the prompt loaded by every LLM engine"
        )
