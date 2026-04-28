"""LLM transcreation dispatcher — selects engine by env vars.

Selection logic (in order):
    1. If ``LLM_PROVIDER`` is set explicitly → use that engine (overrides
       priority below). Unknown values warn + fall back to the priority chain.
    2. Otherwise, auto-select by API-key presence in priority order:
       ``OPENAI_API_KEY``     → openai     (highest priority — direct, cheapest for OpenAI models)
       ``ANTHROPIC_API_KEY``  → claude     (direct Anthropic)
       ``GEMINI_API_KEY``     → gemini     (direct Google)
       ``OPENROUTER_API_KEY`` → openrouter (lowest priority — multi-model gateway)
    3. If no key is present and no provider set → claude (so news_bot's
       startup health check fails cleanly with admin-ping rather than
       a cryptic import error).

Public API:
    * ``transcreate_via_claude(article)`` — main entry (function name
      kept for backward compat; routes to the configured engine).
    * ``health_check()`` — non-raising bool probe.
    * ``ClaudeOutageError``, ``ClaudeTranscreationError`` — shared types
      from ``_llm_common`` (single class identity across engines).
    * ``is_outage_error(exc)``, ``is_per_article_error(exc)``.

Adding a new engine:
    1. Create ``<name>_transcreation.py`` mirroring the public API.
    2. Use ``from _llm_common import ClaudeOutageError, ClaudeTranscreationError``
       so exceptions share class identity.
    3. Add a branch to ``_select_engine()`` below for the explicit env-var case.
    4. Add a branch to ``_auto_select_by_key_presence()`` for auto-selection.
"""

from __future__ import annotations

import os
import logging

from _llm_common import ClaudeOutageError, ClaudeTranscreationError

logger = logging.getLogger(__name__)


def _has_key(env_var: str) -> bool:
    """True iff ``env_var`` is set to a non-empty value."""
    return bool(os.getenv(env_var, "").strip())


def _auto_select_by_key_presence() -> str:
    """Return engine name based on which API keys are configured.

    Priority: openai > claude > gemini > openrouter. Falls back to
    ``claude`` when no key is configured (so the startup health check
    produces the canonical missing-key admin ping rather than a cryptic
    error).
    """
    if _has_key("OPENAI_API_KEY"):
        return "openai"
    if _has_key("ANTHROPIC_API_KEY"):
        return "claude"
    if _has_key("GEMINI_API_KEY"):
        return "gemini"
    if _has_key("OPENROUTER_API_KEY"):
        return "openrouter"
    return "claude"


def _select_engine():
    """Return the engine module based on ``LLM_PROVIDER`` env or key presence.

    Lazy-imports the engine module so unused engines' SDKs are not loaded.
    """
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        if explicit == "openai":
            import openai_transcreation as engine
            logger.info("LLM_PROVIDER=openai (explicit)")
            return engine
        if explicit == "claude":
            import claude_transcreation as engine
            logger.info("LLM_PROVIDER=claude (explicit)")
            return engine
        if explicit == "gemini":
            import gemini_transcreation as engine
            logger.info("LLM_PROVIDER=gemini (explicit)")
            return engine
        if explicit == "openrouter":
            import openrouter_transcreation as engine
            logger.info("LLM_PROVIDER=openrouter (explicit)")
            return engine
        logger.warning(
            "LLM_PROVIDER=%r is not recognized; falling back to key-presence "
            "auto-selection (openai → claude → gemini → openrouter)",
            explicit,
        )

    auto = _auto_select_by_key_presence()
    if auto == "openai":
        import openai_transcreation as engine
    elif auto == "gemini":
        import gemini_transcreation as engine
    elif auto == "openrouter":
        import openrouter_transcreation as engine
    else:
        import claude_transcreation as engine
    logger.info("LLM_PROVIDER auto-selected: %s (by key presence)", auto)
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
