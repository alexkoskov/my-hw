#!/usr/bin/env python3
"""Tests for ``news_bot._is_hot_wheels_relevant`` + ``filter_new_entries``
sibling-brand filter (added after the Matchbox post leaked into the
channel via autoevolution's cross-tagged RSS feed on 2026-04-28)."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import _is_hot_wheels_relevant, filter_new_entries


class TestIsHotWheelsRelevant(unittest.TestCase):
    def test_hot_wheels_in_title_is_relevant(self):
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'New Hot Wheels Premium F1 cars are here',
        }))

    def test_matchbox_only_is_not_relevant(self):
        self.assertFalse(_is_hot_wheels_relevant({
            'title': '5 New Matchbox Working Rigs Blue-Collar Workers Will Love',
        }))

    def test_matchbox_with_hot_wheels_is_relevant(self):
        """Cross-over articles that mention BOTH brands stay in —
        autoevolution's editorial Mattel round-ups often do this."""
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Matchbox vs Hot Wheels — which is the better Mattel buy?',
        }))

    def test_neutral_title_defaults_to_relevant(self):
        # A title without any sibling-brand keyword falls through to
        # "include" — we'd rather over-publish than drop a legitimate
        # entry. Dedup handles repeats.
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Bugatti supercar news today',
        }))

    def test_empty_title_is_relevant(self):
        self.assertTrue(_is_hot_wheels_relevant({}))
        self.assertTrue(_is_hot_wheels_relevant({'title': ''}))
        self.assertTrue(_is_hot_wheels_relevant({'title': None}))

    def test_case_insensitive(self):
        self.assertFalse(_is_hot_wheels_relevant({
            'title': '5 NEW MATCHBOX WORKING RIGS',
        }))
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'NEW HOT WHEELS RELEASE',
        }))


class TestFilterNewEntriesIntegration(unittest.TestCase):
    """End-to-end through ``filter_new_entries``: dedup + relevance work
    together, neither shadows the other."""

    def setUp(self):
        # ``is_processed`` reads news.db; bypass to keep test pure.
        self._is_processed_patcher = patch('news_bot.is_processed', return_value=False)
        self._is_processed_patcher.start()

    def tearDown(self):
        self._is_processed_patcher.stop()

    def test_matchbox_filtered_hot_wheels_kept(self):
        entries = [
            {'link': 'http://x/hw', 'title': 'Hot Wheels new release'},
            {'link': 'http://x/mb', 'title': '5 New Matchbox Working Rigs'},
            {'link': 'http://x/neutral', 'title': 'Bugatti news'},
        ]
        out = filter_new_entries(entries)
        out_links = [e['link'] for e in out]
        self.assertIn('http://x/hw', out_links)
        self.assertNotIn('http://x/mb', out_links)
        # Neutral entries pass through (not the filter's job to gate them).
        self.assertIn('http://x/neutral', out_links)

    def test_dedup_within_same_batch_still_applies(self):
        entries = [
            {'link': 'http://x/dup', 'title': 'Hot Wheels news'},
            {'link': 'http://x/dup', 'title': 'Hot Wheels news'},
        ]
        out = filter_new_entries(entries)
        self.assertEqual(len(out), 1)


if __name__ == '__main__':
    unittest.main()
