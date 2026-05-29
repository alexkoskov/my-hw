"""Structural assertions on ``ux-guidelines.md`` for the t-hunted PT source.

The file at ``.claude/skills/project-knowledge/references/ux-guidelines.md``
is the runtime system-prompt every LLM transcreation engine loads at runtime
(see ``_llm_common._build_system_prompt``). The feature ``t-hunted-pt-source``
makes three edits to it:

1. Widen the input-language assertion in the system-prompt blockquote so the
   LLM accepts PT alongside EN.
2. Add a ``### 🟤 t-hunted`` per-source style block under
   ``## Per-source style notes``.
3. Add a new ``## Glossary — PT/EN/RU`` H2 section with at least 14 baseline
   entries.

These tests pin those three edits structurally so that a future merge or
refactor cannot silently revert them. All assertions are **content-anchored**
(string / heading lookups) — no line-number coupling, so they survive future
prompt-text drift in other features.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UX_GUIDELINES = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "project-knowledge"
    / "references"
    / "ux-guidelines.md"
)


def _read() -> str:
    return UX_GUIDELINES.read_text(encoding="utf-8")


def _section(content: str, heading: str) -> str:
    """Return the slice of *content* from *heading* up to the next ``## `` heading.

    *heading* must be the full line text (e.g. ``"## Glossary — PT/EN/RU"``).
    Returns empty string if *heading* is absent.
    """
    lines = content.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") and not lines[j].startswith("### "):
            end = j
            break
    return "\n".join(lines[start:end])


def test_glossary_section_present():
    content = _read()
    assert "## Glossary — PT/EN/RU" in content, (
        "Missing ``## Glossary — PT/EN/RU`` H2 section — required by "
        "user-spec AC7 and tech-spec Decision 5."
    )


def test_glossary_has_at_least_10_entries():
    section = _section(_read(), "## Glossary — PT/EN/RU")
    assert section, "Glossary section not found — see test_glossary_section_present."

    data_rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip the separator row (``|---|---|---|---|``)
        if re.fullmatch(r"\|[\s\-:|]+\|", stripped):
            continue
        # Skip the header row (contains ``PT`` and ``EN`` column names)
        if "PT" in stripped and "EN" in stripped and "RU" in stripped:
            continue
        data_rows.append(stripped)

    assert len(data_rows) >= 10, (
        f"Glossary table has {len(data_rows)} data rows; expected ≥10 per "
        "user-spec AC7."
    )


def test_t_hunted_style_block_present():
    content = _read()
    per_source = _section(content, "## Per-source style notes")
    assert per_source, "## Per-source style notes section missing."
    assert "### 🟤 t-hunted" in per_source, (
        "Missing ``### 🟤 t-hunted`` per-source style block (with U+1F7E4 "
        "brown circle emoji) inside ``## Per-source style notes`` — required "
        "by tech-spec Decision 5."
    )


def test_input_language_prompt_widened():
    content = _read()
    # Anchor by unique opening phrase of the system-prompt sentence about input
    # language. Take everything up to the next sentence boundary (period
    # followed by whitespace) so the assertion isn't fooled by ``английский``
    # appearing later in the blockquote.
    match = re.search(
        r"Твоя единственная задача[^.]*\.",
        content,
    )
    assert match, (
        "Anchor sentence 'Твоя единственная задача…' not found in "
        "ux-guidelines.md — system-prompt blockquote may have been rewritten."
    )
    sentence = match.group(0).lower()
    assert "английский" in sentence and "португальский" in sentence, (
        "System-prompt input-language assertion must mention both "
        "'английский' AND 'португальский' so the LLM accepts PT input "
        "(tech-spec Decision 5). Found sentence: " + match.group(0)
    )
