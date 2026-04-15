#!/usr/bin/env python3
"""
Unit tests for configuration loader (load_feeds).
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch, mock_open

# Import the function from news_bot (will be defined later)
from news_bot import load_feeds, RSS_URL


class TestLoadFeeds(unittest.TestCase):
    """Test load_feeds function."""

    def setUp(self):
        # Create a temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)

    def tearDown(self):
        os.chdir(self.original_cwd)
        import shutil
        shutil.rmtree(self.temp_dir)

    def test_missing_file_falls_back(self):
        """When feeds.json does not exist, return fallback list."""
        # Ensure file does not exist
        self.assertFalse(os.path.exists('feeds.json'))
        feeds = load_feeds()
        # Should be a list containing the hardcoded RSS_URL
        self.assertIsInstance(feeds, list)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0], RSS_URL)

    def test_invalid_json_falls_back(self):
        """Invalid JSON content leads to fallback."""
        with open('feeds.json', 'w') as f:
            f.write('{ invalid json')
        feeds = load_feeds()
        self.assertEqual(feeds, [RSS_URL])

    def test_not_a_list_falls_back(self):
        """JSON is not a list -> fallback."""
        with open('feeds.json', 'w') as f:
            f.write('{"url": "https://example.com"}')
        feeds = load_feeds()
        self.assertEqual(feeds, [RSS_URL])

    def test_empty_list_falls_back(self):
        """Empty list -> fallback (since no valid URLs)."""
        with open('feeds.json', 'w') as f:
            f.write('[]')
        feeds = load_feeds()
        self.assertEqual(feeds, [RSS_URL])

    def test_valid_urls_up_to_five(self):
        """Valid JSON array of up to 5 URLs returns them."""
        test_urls = [
            "https://example.com/feed1",
            "https://example.com/feed2",
            "https://example.com/feed3",
        ]
        with open('feeds.json', 'w') as f:
            json.dump(test_urls, f)
        feeds = load_feeds()
        self.assertEqual(feeds, test_urls)

    def test_more_than_five_truncates(self):
        """If more than 5 URLs, only first 5 are taken."""
        test_urls = [f"https://example.com/feed{i}" for i in range(10)]
        with open('feeds.json', 'w') as f:
            json.dump(test_urls, f)
        feeds = load_feeds()
        self.assertEqual(len(feeds), 5)
        self.assertEqual(feeds, test_urls[:5])

    def test_invalid_urls_cause_fallback(self):
        """If any URL is invalid, fallback to RSS_URL."""
        # One invalid URL (missing scheme)
        with open('feeds.json', 'w') as f:
            json.dump(["not-a-url", "https://example.com"], f)
        feeds = load_feeds()
        self.assertEqual(feeds, [RSS_URL])

    def test_file_with_valid_urls_but_not_strings(self):
        """If array contains non-string values, fallback."""
        with open('feeds.json', 'w') as f:
            json.dump([123, {"url": "test"}], f)
        feeds = load_feeds()
        self.assertEqual(feeds, [RSS_URL])


if __name__ == '__main__':
    unittest.main()