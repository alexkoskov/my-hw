"""The runtime prompt must survive repository and flattened deploy layouts."""

import importlib
from pathlib import Path

import pytest

from _llm_common import _resolve_prompt_path


REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINES = (
    "claude_transcreation",
    "openrouter_transcreation",
    "openai_transcreation",
    "gemini_transcreation",
)


@pytest.mark.parametrize("module_name", ENGINES)
def test_each_engine_loads_the_packaged_prompt(module_name, monkeypatch):
    engine = importlib.import_module(module_name)
    monkeypatch.setattr(engine, "_PROMPT_CACHE", {})
    expected = (REPO_ROOT / "prompts" / "ux-guidelines.md").read_text(encoding="utf-8")
    assert engine._load_prompt() == expected


@pytest.mark.parametrize("module_name", ENGINES)
def test_each_engine_supports_flattened_deploy_prompt(module_name, monkeypatch, tmp_path):
    engine = importlib.import_module(module_name)
    monkeypatch.setattr(engine, "_MODULE_DIR", str(tmp_path))
    monkeypatch.setattr(engine, "_PROMPT_CACHE", {})
    expected = (REPO_ROOT / "prompts" / "ux-guidelines.md").read_text(encoding="utf-8")
    (tmp_path / "ux-guidelines.md").write_text(expected, encoding="utf-8")
    assert engine._load_prompt(_resolve_prompt_path(str(tmp_path))) == expected
