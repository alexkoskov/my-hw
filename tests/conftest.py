"""Shared pytest fixtures.

Currently:
* zero-out the per-article LLM retry interval so unit tests don't
  spend 5+5 minutes sleeping when they exercise the retry path in
  ``news_bot._fallback_publish``. Production value (300 s) is the
  operator-tuned variant X' from the llm-transcreation-and-distributed-
  publishing follow-up.
"""

import os
import sys

import pytest


# Make the repo-root modules importable from any test file regardless
# of pytest's invocation cwd. This used to live in individual
# ``sys.path.insert`` calls inside each test module — centralising it
# here is harmless for tests that already insert and saves boilerplate
# in new tests.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _zero_llm_retry_interval(monkeypatch):
    """Patch the per-article retry interval to 0 so retry-path tests
    finish in milliseconds. Tests that explicitly want to assert on
    sleep duration can reset the value with their own monkeypatch."""
    try:
        import news_bot
    except ImportError:
        # news_bot may not be importable in some test environments
        # (e.g. lamley_source unit tests run before any news_bot
        # import). The fixture is a best-effort no-op in that case.
        return
    monkeypatch.setattr(news_bot, "_LLM_PER_ARTICLE_RETRY_INTERVAL_S", 0)
