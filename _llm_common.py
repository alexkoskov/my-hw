"""Shared exception types for LLM transcreation engines.

Defined here (not inside per-engine modules) so that all engines
share one class identity — `pytest.raises(ClaudeOutageError)` and
`isinstance(exc, ClaudeOutageError)` work regardless of which engine
(Claude / Gemini / future) raised the exception.

Names retain the "Claude" prefix for backward compatibility with
existing tests and news_bot.py imports. Semantics are provider-agnostic.
"""


class ClaudeTranscreationError(Exception):
    """Per-article LLM failure (refusal, malformed JSON, schema mismatch).

    Caller falls back to Google Translate for THIS article only;
    outage state machine NOT advanced.
    """


class ClaudeOutageError(Exception):
    """API-level LLM failure (network, 429, 5xx, auth, quota).

    Caller advances outage state machine (admin pings, 2h grace,
    then global Google fallback).
    """
