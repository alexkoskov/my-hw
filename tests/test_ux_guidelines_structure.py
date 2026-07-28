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

A second group (2026-07-28) pins the «машинка» object canon and the single
sanctioned slang carve-out «машонка» — including the caption-pass guard, which
reaches into the four engine modules because ``_BLOCK_TRANSLATE_SYSTEM`` never
loads this file.
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


# ---------------------------------------------------------------------------
# «Машинка» canon + the single sanctioned slang carve-out (2026-07-28).
#
# Channel feedback: readers do not say «миниатюра» / «фигурка» — the calque
# reads as unnatural. «Машинка» is the canon for the object itself, and the
# chat's own slang «машонка» is allowed in strictly bounded doses.
#
# These assertions pin a PROMPT-BEHAVIOUR contract, not prose: the object canon,
# the carve-out, its four limits, and — critically — that the carve-out did not
# dissolve the general ban on invented words it is an exception to.
# ---------------------------------------------------------------------------


def _glossary_rows() -> list[str]:
    """Data rows of the glossary table, lowercased."""
    rows = []
    for line in _section(_read(), "## Glossary — PT/EN/RU").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if re.fullmatch(r"\|[\s\-:|]+\|", stripped):
            continue
        if "PT" in stripped and "EN" in stripped and "RU" in stripped:
            continue
        rows.append(stripped.lower())
    return rows


def test_glossary_pins_mashinka_as_object_canon():
    """The bans must live in the TABLE ROW, not merely somewhere in the section.

    Scoped to the row on purpose: «фигурка» also appears in the figurine
    carve-out prose below, so a section-wide `in` check stays green even if the
    ban is deleted from the row.
    """
    rows = [r for r in _glossary_rows() if "miniatura" in r]
    assert rows, (
        "Glossary must carry a `Miniatura` row — PT bodies use the word "
        "constantly and without a row the LLM calques it to «миниатюра»."
    )
    row = rows[0]
    assert "машинка" in row, "«Машинка» must be the RU canon for the object."
    for banned in ("миниатюра", "фигурка", "моделька", "изделие", "экземпляр"):
        assert banned in row, (
            f"The `Miniatura` row must name «{banned}» as a rejected "
            "translation. A positive-only rule does not stop a calque the "
            "model already produces, and the row / prose / red-flag ban lists "
            "must agree — a word banned in one and missing from another is "
            "invisible to the self-check that exists to catch it."
        )


def test_object_canon_has_exactly_one_glossary_row():
    """`Carrinho` and `Miniatura` must not be two rows with different licence.

    They previously were: `Carrinho` offered «Машинка / дайкаст» (free
    alternation — itself against the one-term-one-word rule right below the
    table) while `Miniatura` made «дайкаст» conditional. Both EN columns cover
    *diecast car*, so an EN article matched two rows granting different
    permissions.
    """
    matching = [
        r for r in _glossary_rows()
        if "carrinho" in r or "miniatura" in r
    ]
    assert len(matching) == 1, (
        "Expected ONE merged row covering carrinho/miniatura; found "
        f"{len(matching)}: {matching}"
    )


def test_figurine_exception_is_scoped_to_actual_figurines():
    """«Фигурка» → «машинка» ONLY when the subject IS the car.

    Hot Wheels franchise sets occasionally ship a real character figurine;
    rewriting that to «машинка» would be a factual error, not a style fix.
    """
    section = _section(_read(), "## Glossary — PT/EN/RU").lower()
    assert "фигурка персонажа" in section, (
        "The «фигурка» rule must carve out real character figurines "
        "(anchor phrase «фигурка персонажа»), otherwise the substitution "
        "introduces factual errors on franchise sets."
    )


def test_mashonka_slang_carveout_is_bounded():
    content = _read()
    lowered = content.lower()
    assert "машонка" in lowered, (
        "The sanctioned slang «машонка» must be named in the prompt — it is "
        "otherwise forbidden by the invented-words rule."
    )
    section = _section(content, "## Glossary — PT/EN/RU").lower()
    # All limits live together with the carve-out, so the LLM cannot read the
    # permission without the constraints.
    # Dosage, as the operator finally set it (2026-07-28): every article, at
    # most once. Deliberately a PER-ARTICLE rule — the earlier «one article in
    # five» draft was unimplementable, since each article is translated by an
    # independent call with no memory of the previous ones, so the model cannot
    # ration across articles and falls back to using the word whenever allowed.
    # Counting inside one article it can do.
    assert "ровно один раз в статье" in section, (
        "Carve-out must state the per-article dosage the operator chose."
    )
    assert "не больше" in section, "Missing the once-per-article ceiling."
    assert "заголов" in section, "Carve-out must forbid the slang in titles."
    assert "сноск" in section or "первом упоминании" in section, (
        "Carve-out must require the first-mention gloss for readers who are "
        "not in the chat."
    )


def test_invented_words_ban_survives_the_carveout():
    """Regression guard: the carve-out is ONE named exception, not an opening.

    The blockquote ban and the red-flag self-check are what keep «коллективка»
    /«эксклюзивка» out. A future edit that widens «машонка is allowed» into
    «slang is allowed» must fail here.
    """
    content = _read()
    assert "Запрещены выдуманные слова и сленговые неологизмы" in content, (
        "The system-prompt ban on invented words / slang neologisms was "
        "removed or reworded — the «машонка» carve-out depends on it still "
        "standing as the default."
    )
    red_flags = _section(content, "## Red flags to self-check before stage")
    assert red_flags, "## Red flags section missing."
    assert "коллективка" in red_flags, (
        "The red-flag self-check against non-dictionary words must survive."
    )


def test_caption_pass_carries_the_object_canon():
    """The image-caption second pass has its OWN system prompt.

    ``_translate_block_strings`` in every engine uses ``_BLOCK_TRANSLATE_SYSTEM``
    and never loads ``ux-guidelines.md``, so the «машинка» canon does not reach
    captions unless it is stated there too. Without this guard the body would
    say «машинка» while the picture caption under it said «миниатюра».

    Slang is deliberately NOT propagated: a caption is one line, too short to
    carry the required first-mention gloss.
    """
    engines = [
        "claude_transcreation.py",
        "openrouter_transcreation.py",
        "openai_transcreation.py",
        "gemini_transcreation.py",
    ]
    for name in engines:
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        marker = "_BLOCK_TRANSLATE_SYSTEM"
        assert marker in source, f"{name}: {marker} not found."
        start = source.index(marker)
        block = source[start:start + 1200]
        assert "машинка" in block, (
            f"{name}: the caption system prompt must pin «машинка» as the "
            "object canon — it never loads ux-guidelines.md."
        )
        assert "фигурка" in block, (
            f"{name}: the caption prompt must carry the character-figure "
            "exception too, or captions will mislabel real figurines."
        )
        assert "машонка" not in block, (
            f"{name}: slang must NOT be propagated to captions — a caption is "
            "too short to carry the mandatory first-mention gloss."
        )
