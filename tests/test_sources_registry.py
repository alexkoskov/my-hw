#!/usr/bin/env python3
"""
Unit tests for the SOURCES registry and source-name tagging in
``news_bot.py`` (manual-review-workflow Task 5 / tech-spec Decision 4).

Covers:
- ``NETLOC_TO_SOURCE`` dict + ``_resolve_source_name`` helper.
- ``_fetch_rss_entries`` — per-feed error isolation, feed_url stamping,
  source_name tagging, unknown-netloc warning, empty-feeds fallback,
  FeedParserDict → plain-dict normalisation.
- ``_fetch_mattel_entries`` — mattel tagging, None-safety.
- ``SOURCES`` shape.

Network calls are avoided: tests monkeypatch ``news_bot.load_feeds``,
``news_bot.fetch_rss``, and ``news_bot.fetch_mattel_news``.
"""

import logging
import os
import sys

import feedparser
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
from news_bot import (
    NETLOC_TO_SOURCE,
    RSS_URL,
    SOURCES,
    _fetch_mattel_entries,
    _fetch_rss_entries,
    _resolve_source_name,
)


# ---------------------------------------------------------------------------
# NETLOC_TO_SOURCE constant
# ---------------------------------------------------------------------------

class TestNetlocToSource:
    """Decision 4 — the five explicit netloc keys."""

    def test_has_exactly_the_five_keys(self):
        assert set(NETLOC_TO_SOURCE) == {
            'www.autoevolution.com',
            'autoevolution.com',
            'lamleygroup.com',
            'www.lamleygroup.com',
            'corporate.mattel.com',
        }

    def test_values_are_only_the_three_source_names(self):
        assert set(NETLOC_TO_SOURCE.values()) == {
            'autoevolution', 'lamley', 'mattel',
        }

    def test_autoevolution_both_netlocs(self):
        assert NETLOC_TO_SOURCE['www.autoevolution.com'] == 'autoevolution'
        assert NETLOC_TO_SOURCE['autoevolution.com'] == 'autoevolution'

    def test_lamley_both_netlocs(self):
        assert NETLOC_TO_SOURCE['lamleygroup.com'] == 'lamley'
        assert NETLOC_TO_SOURCE['www.lamleygroup.com'] == 'lamley'

    def test_mattel_netloc(self):
        assert NETLOC_TO_SOURCE['corporate.mattel.com'] == 'mattel'


# ---------------------------------------------------------------------------
# _resolve_source_name
# ---------------------------------------------------------------------------

class TestResolveSourceName:
    """TDD anchor: test_resolve_source_name_* per tasks/5.md."""

    def test_resolve_source_name_known_netlocs(self):
        assert _resolve_source_name('https://www.autoevolution.com/news/x.html') == 'autoevolution'
        assert _resolve_source_name('https://autoevolution.com/news/x.html') == 'autoevolution'
        assert _resolve_source_name('https://lamleygroup.com/2025/11/something.html') == 'lamley'
        assert _resolve_source_name('https://www.lamleygroup.com/2025/11/something.html') == 'lamley'
        assert _resolve_source_name('https://corporate.mattel.com/news/foo') == 'mattel'

    def test_resolve_source_name_unknown_returns_other(self):
        assert _resolve_source_name('https://example.com/x') == 'other'
        assert _resolve_source_name('https://unknown.example/path') == 'other'

    def test_resolve_source_name_case_insensitive(self):
        # Netloc is lowercased before lookup.
        assert _resolve_source_name('https://WWW.Autoevolution.COM/x') == 'autoevolution'
        assert _resolve_source_name('https://LamleyGroup.Com/2025/x') == 'lamley'
        assert _resolve_source_name('https://Corporate.Mattel.Com/news/y') == 'mattel'

    def test_resolve_source_name_no_scheme(self):
        # urlparse of a bare host puts it in path, not netloc — should return 'other'.
        assert _resolve_source_name('lamleygroup.com/x') == 'other'

    def test_resolve_source_name_empty_string(self):
        # Empty netloc → 'other', does not raise.
        assert _resolve_source_name('') == 'other'


# ---------------------------------------------------------------------------
# _fetch_rss_entries
# ---------------------------------------------------------------------------

class TestFetchRssEntries:
    """TDD anchors: test_fetch_rss_entries_* per tasks/5.md."""

    def test_fetch_rss_entries_tags_by_netloc(self, monkeypatch):
        feeds = [
            'https://www.autoevolution.com/rss/tag-Hot+Wheels.xml',
            'https://lamleygroup.com/category/hot-wheels/feed/',
        ]

        def fake_fetch_rss(url):
            if 'autoevolution' in url:
                return [{'link': 'https://www.autoevolution.com/news/a.html',
                         'title': 'Auto A', 'published': 'Mon', 'summary': 'S1'}]
            return [{'link': 'https://lamleygroup.com/2025/11/l.html',
                     'title': 'Lamley L', 'published': 'Tue', 'summary': 'S2'}]

        monkeypatch.setattr(news_bot, 'load_feeds', lambda: feeds)
        monkeypatch.setattr(news_bot, 'fetch_rss', fake_fetch_rss)

        entries = _fetch_rss_entries(notifier=None)

        assert len(entries) == 2
        by_source = {e['source_name']: e for e in entries}
        assert set(by_source) == {'autoevolution', 'lamley'}
        assert by_source['autoevolution']['feed_url'] == feeds[0]
        assert by_source['lamley']['feed_url'] == feeds[1]
        assert by_source['autoevolution']['title'] == 'Auto A'
        assert by_source['lamley']['title'] == 'Lamley L'

    def test_fetch_rss_entries_unknown_netloc_warns(self, monkeypatch, caplog):
        monkeypatch.setattr(news_bot, 'load_feeds', lambda: ['https://unknown.example/feed'])
        monkeypatch.setattr(
            news_bot, 'fetch_rss',
            lambda url: [{'link': 'https://unknown.example/x',
                          'title': 'U', 'published': '', 'summary': ''}],
        )

        with caplog.at_level(logging.WARNING, logger='news_bot'):
            entries = _fetch_rss_entries(notifier=None)

        assert len(entries) == 1
        assert entries[0]['source_name'] == 'other'
        # caplog captures a WARNING mentioning the unknown link.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any('unknown.example/x' in r.getMessage() for r in warnings), (
            f"Expected a WARNING mentioning 'unknown.example/x', got: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_fetch_rss_entries_normalises_feedparser_dict(self, monkeypatch):
        # Feed a real FeedParserDict via feedparser.parse, not a bare dict —
        # ensures we exercise the FeedParserDict → plain-dict normalisation.
        tiny_rss = (
            '<?xml version="1.0"?>'
            '<rss version="2.0"><channel>'
            '<title>t</title><link>https://www.autoevolution.com/</link>'
            '<description>d</description>'
            '<item>'
            '<link>https://www.autoevolution.com/news/x.html</link>'
            '<title>Item Title</title>'
            '<description>Item summary text</description>'
            '<pubDate>Mon, 01 Jan 2025 00:00:00 +0000</pubDate>'
            '</item>'
            '</channel></rss>'
        )
        parsed = feedparser.parse(tiny_rss)
        assert parsed.entries, "feedparser failed to parse the tiny RSS fixture"

        monkeypatch.setattr(news_bot, 'load_feeds', lambda: ['https://www.autoevolution.com/rss'])
        monkeypatch.setattr(news_bot, 'fetch_rss', lambda url: parsed.entries)

        entries = _fetch_rss_entries(notifier=None)
        assert len(entries) == 1
        item = entries[0]

        # Output must be a plain dict — no FeedParserDict leakage.
        assert type(item) is dict
        # Bounded key set: only the explicit fields + feed_url + source_name.
        assert set(item.keys()) == {
            'link', 'title', 'published', 'summary', 'feed_url', 'source_name',
        }
        # feedparser internals must NOT leak through.
        assert 'summary_detail' not in item
        assert 'title_detail' not in item
        assert 'links' not in item

        assert item['link'] == 'https://www.autoevolution.com/news/x.html'
        assert item['title'] == 'Item Title'
        assert 'Item summary text' in item['summary']
        assert item['source_name'] == 'autoevolution'
        assert item['feed_url'] == 'https://www.autoevolution.com/rss'

    def test_fetch_rss_entries_isolates_per_feed_errors(self, monkeypatch, caplog):
        feeds = [
            'https://www.autoevolution.com/rss/broken.xml',
            'https://lamleygroup.com/category/hot-wheels/feed/',
        ]

        def fake_fetch_rss(url):
            if 'broken' in url:
                raise RuntimeError('simulated feed blowup')
            return [{'link': 'https://lamleygroup.com/2025/l.html',
                     'title': 'L', 'published': '', 'summary': ''}]

        monkeypatch.setattr(news_bot, 'load_feeds', lambda: feeds)
        monkeypatch.setattr(news_bot, 'fetch_rss', fake_fetch_rss)

        with caplog.at_level(logging.ERROR, logger='news_bot'):
            entries = _fetch_rss_entries(notifier=None)

        # Second feed's entries still returned — loop did not abort.
        assert len(entries) == 1
        assert entries[0]['source_name'] == 'lamley'
        # Error from the first feed was logged.
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any('broken.xml' in r.getMessage() or 'simulated feed blowup' in r.getMessage()
                   for r in errors)

    def test_fetch_rss_entries_empty_feeds_falls_back(self, monkeypatch):
        captured_urls = []

        def fake_fetch_rss(url):
            captured_urls.append(url)
            return []

        monkeypatch.setattr(news_bot, 'load_feeds', lambda: [])
        monkeypatch.setattr(news_bot, 'fetch_rss', fake_fetch_rss)

        entries = _fetch_rss_entries(notifier=None)

        assert entries == []
        # Fell back to RSS_URL.
        assert captured_urls == [RSS_URL]

    def test_fetch_rss_entries_missing_link_uses_feed_url_for_source(self, monkeypatch):
        # Entry with no link — source should be inferred from feed URL netloc.
        monkeypatch.setattr(
            news_bot, 'load_feeds',
            lambda: ['https://lamleygroup.com/category/hot-wheels/feed/'],
        )
        monkeypatch.setattr(
            news_bot, 'fetch_rss',
            lambda url: [{'title': 'No Link', 'published': '', 'summary': ''}],
        )

        entries = _fetch_rss_entries(notifier=None)
        assert len(entries) == 1
        assert entries[0]['source_name'] == 'lamley'
        assert entries[0]['link'] is None or entries[0]['link'] == ''


# ---------------------------------------------------------------------------
# _fetch_mattel_entries
# ---------------------------------------------------------------------------

class TestFetchMattelEntries:
    """TDD anchors: test_fetch_mattel_entries_* per tasks/5.md."""

    def test_fetch_mattel_entries_tags_mattel(self, monkeypatch):
        fake_entries = [
            {'link': 'https://corporate.mattel.com/news/a', 'title': 'A',
             'summary': 's1', 'feed_url': 'https://corporate.mattel.com/news'},
            {'link': 'https://corporate.mattel.com/news/b', 'title': 'B',
             'summary': 's2', 'feed_url': 'https://corporate.mattel.com/news'},
        ]
        monkeypatch.setattr(
            news_bot, 'fetch_mattel_news',
            lambda notifier=None: fake_entries,
        )

        result = _fetch_mattel_entries(notifier=None)

        assert len(result) == 2
        for item in result:
            assert item['source_name'] == 'mattel'
        # feed_url preserved from source fetcher.
        assert all(r['feed_url'] == 'https://corporate.mattel.com/news' for r in result)

    def test_fetch_mattel_entries_empty_on_none(self, monkeypatch):
        # Defensive: fetch_mattel_news could theoretically return None.
        monkeypatch.setattr(
            news_bot, 'fetch_mattel_news',
            lambda notifier=None: None,
        )
        assert _fetch_mattel_entries(notifier=None) == []

    def test_fetch_mattel_entries_empty_on_empty_list(self, monkeypatch):
        monkeypatch.setattr(
            news_bot, 'fetch_mattel_news',
            lambda notifier=None: [],
        )
        assert _fetch_mattel_entries(notifier=None) == []

    def test_fetch_mattel_entries_passes_notifier_through(self, monkeypatch):
        seen = {}

        def fake_fetch(notifier=None):
            seen['notifier'] = notifier
            return []

        monkeypatch.setattr(news_bot, 'fetch_mattel_news', fake_fetch)

        sentinel = lambda msg: None  # noqa: E731
        _fetch_mattel_entries(notifier=sentinel)
        assert seen['notifier'] is sentinel


# ---------------------------------------------------------------------------
# SOURCES registry shape
# ---------------------------------------------------------------------------

class TestSourcesRegistry:
    """TDD anchor: test_sources_registry_shape per tasks/5.md."""

    def test_sources_registry_shape(self):
        assert isinstance(SOURCES, list)
        assert [f.__name__ for f in SOURCES] == [
            '_fetch_rss_entries',
            '_fetch_mattel_entries',
        ]

    def test_sources_are_callables(self):
        for fetcher in SOURCES:
            assert callable(fetcher)

    def test_sources_accept_notifier_kwarg(self, monkeypatch):
        # Integration smoke: iterate the registry like Task 6 will.
        monkeypatch.setattr(news_bot, 'load_feeds', lambda: [])
        monkeypatch.setattr(news_bot, 'fetch_rss', lambda url: [])
        monkeypatch.setattr(news_bot, 'fetch_mattel_news', lambda notifier=None: [])

        collected = []
        for fetcher in SOURCES:
            collected.extend(fetcher(notifier=None))
        assert collected == []
