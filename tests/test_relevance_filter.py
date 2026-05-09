#!/usr/bin/env python3
"""Tests for ``news_bot._is_hot_wheels_relevant`` + ``filter_new_entries``
sibling-brand filter (added after the Matchbox post leaked into the
channel via autoevolution's cross-tagged RSS feed on 2026-04-28)."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import (
    _is_hot_wheels_relevant,
    _is_text_only_checklist,
    filter_new_entries,
)


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


class TestIsTextOnlyChecklist(unittest.TestCase):
    """Two-condition rule: title says "checklist" AND body has < 500
    chars of paragraph text → drop. Subscribers don't want bare
    bullet-list posts; review articles that mention "checklist" with
    real editorial body still pass through.
    """

    def test_bare_checklist_dropped(self):
        entry = {'title': '2026 Hot Wheels Mainline Checklist Q3 Update'}
        article = {
            # Empty / list-only body — barely any prose.
            'paragraphs': [
                'Mainline 2026', 'Q3 release wave',
            ],
        }
        self.assertTrue(_is_text_only_checklist(entry, article))

    def test_review_article_with_checklist_in_title_kept(self):
        """Real review article mentions checklist in title but has
        substantive body text — must NOT trigger the filter."""
        entry = {'title': 'Brad reviews the 2026 Hot Wheels checklist'}
        # 600+ chars of body text → above the 500-char floor.
        long_paragraph = 'A' * 600
        article = {'paragraphs': [long_paragraph]}
        self.assertFalse(_is_text_only_checklist(entry, article))

    def test_no_checklist_in_title_passes_regardless_of_body(self):
        # Even with empty body, no "checklist" word in title → False.
        entry = {'title': 'New Hot Wheels Treasure Hunt revealed'}
        article = {'paragraphs': []}
        self.assertFalse(_is_text_only_checklist(entry, article))

    def test_check_list_with_space_matches(self):
        entry = {'title': "Brad's Check List of Q3 releases"}
        article = {'paragraphs': ['short']}
        self.assertTrue(_is_text_only_checklist(entry, article))

    def test_check_list_with_hyphen_matches(self):
        entry = {'title': "Q3 Check-List drop"}
        article = {'paragraphs': ['short']}
        self.assertTrue(_is_text_only_checklist(entry, article))

    def test_word_boundary_avoids_false_match(self):
        """Substring "checklist" inside another word should NOT trigger
        — only whole-word matches count."""
        # 'checklister' or 'unchecklisted' — neither should match.
        entry = {'title': 'The Checklister organization announces partnership'}
        article = {'paragraphs': ['short']}
        self.assertFalse(_is_text_only_checklist(entry, article))

    def test_case_insensitive_title_match(self):
        entry = {'title': 'BRAD\'S 2026 HOT WHEELS CHECKLIST'}
        article = {'paragraphs': []}
        self.assertTrue(_is_text_only_checklist(entry, article))

    def test_missing_paragraphs_treated_as_zero_length(self):
        entry = {'title': '2026 Hot Wheels Checklist'}
        article = {}  # no paragraphs at all
        self.assertTrue(_is_text_only_checklist(entry, article))

    def test_none_article_handled(self):
        entry = {'title': '2026 Hot Wheels Checklist'}
        # Defensive — _is_text_only_checklist tolerates None / missing
        # article without crashing.
        self.assertTrue(_is_text_only_checklist(entry, None))


if __name__ == '__main__':
    unittest.main()
