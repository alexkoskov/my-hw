"""LLM transcreation dispatcher — selects engine by ``LLM_PROVIDER`` env var.

Public API:
    * ``transcreate_via_claude(article)`` — main entry point (function name
      kept for backward compat; actually routes to the configured engine).
    * ``health_check()`` — non-raising bool probe.
    * ``ClaudeOutageError``, ``ClaudeTranscreationError`` — shared exception
      types from ``_llm_common`` (single class identity across engines).
    * ``is_outage_error(exc)``, ``is_per_article_error(exc)``.

Environment:
    LLM_PROVIDER  ``claude`` (default) | ``gemini``
    ANTHROPIC_API_KEY / ANTHROPIC_MODEL  (when LLM_PROVIDER=claude)
    GEMINI_API_KEY / GEMINI_MODEL        (when LLM_PROVIDER=gemini)

Engines share the system prompt (``ux-guidelines.md`` + JSON envelope),
JSON output schema, and post-processing (emoji safety net, paragraph
truncation). They differ only in SDK calls and exception classification.

Adding a new engine:
    1. Create ``<name>_transcreation.py`` mirroring the public API.
    2. Use ``from _llm_common import ClaudeOutageError, ClaudeTranscreationError``
       so exceptions share class identity.
    3. Add a branch to ``_select_engine()`` below.
"""

from __future__ import annotations

import os
import logging

from _llm_common import ClaudeOutageError, ClaudeTranscreationError

logger = logging.getLogger(__name__)


def _select_engine():
    """Return the engine module based on ``LLM_PROVIDER`` env var.

    Falls back to Claude on unknown values (logs a warning).
    Lazy-imports the engine module so the unused engine's SDK is not
    loaded at import time.
    """
    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    if provider == "gemini":
        import gemini_transcreation as engine
        return engine
    if provider == "claude":
        import claude_transcreation as engine
        return engine
    logger.warning(
        "LLM_PROVIDER=%r is not recognized; falling back to claude",
        provider,
    )
    import claude_transcreation as engine
    return engine


_engine = _select_engine()

# Re-export the engine's public API as module-level names so callers do
# ``from llm_transcreation import transcreate_via_claude`` exactly as if
# the engine module itself was imported.
transcreate_via_claude = _engine.transcreate_via_claude
health_check = _engine.health_check
is_outage_error = _engine.is_outage_error
is_per_article_error = _engine.is_per_article_error

__all__ = [
    "transcreate_via_claude",
    "health_check",
    "is_outage_error",
    "is_per_article_error",
    "ClaudeOutageError",
    "ClaudeTranscreationError",
]
