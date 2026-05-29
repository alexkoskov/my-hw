#!/usr/bin/env python3
"""Unit tests for the ``news_bot.fetch_full_article`` dispatcher routing.

These tests sit at the unit level (no real HTTP, no source-fetcher work)
and guard the dispatcher's branch ordering — specifically the t-hunted
blogspot branch added in the t-hunted-pt-source feature (Decision 1).

Why a separate file: the dispatcher routing is a tiny, focused concern;
extending ``test_integration.py`` would bury these checks in a large
end-to-end mocking harness. A standalone file keeps the regression signal
obvious if a future edit reorders the if-chain.
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot


def test_fetch_full_article_routes_blogspot_to_t_hunted():
    """A blogspot.com link must route to
    ``t_hunted_source.fetch_t_hunted_article(link, …)`` exactly once and
    propagate its return value. Guards the dispatcher branch added in
    Task 3 (t-hunted-pt-source feature) against accidental reordering or
    removal."""
    expected = {
        'title': 'mocked',
        'subtitle': '',
        'paragraphs': ['p'],
        'images': [],
    }
    link = 'https://t-hunted.blogspot.com/2026/05/post.html'

    mock_fetch = MagicMock(return_value=expected)
    with patch(
        'news_bot.t_hunted_source.fetch_t_hunted_article',
        new=mock_fetch,
    ):
        result = news_bot.fetch_full_article({'link': link})

    assert mock_fetch.call_count == 1
    # First positional argument MUST be the link as-supplied.
    args, _ = mock_fetch.call_args
    assert args[0] == link
    assert result == expected


def test_fetch_full_article_unknown_domain_returns_none():
    """Regression: an unknown domain falls through the entire dispatcher
    if-chain and returns ``None`` (no source handler matched). Locks the
    fall-through behaviour so a future edit can't silently route unknown
    domains to the wrong handler."""
    result = news_bot.fetch_full_article(
        {'link': 'https://unknown.example.com/some-path'}
    )
    assert result is None
