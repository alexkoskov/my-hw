#!/usr/bin/env python3
"""
Unit tests for summarization (summarize_text).
"""

import unittest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import summarize_text


class TestSummarizeText(unittest.TestCase):
    """Test summarize_text function."""

    def test_basic_extraction(self):
        """Extract first N sentences."""
        text = "First sentence. Second sentence. Third sentence. Fourth sentence. Fifth sentence. Sixth sentence."
        result = summarize_text(text, sentences=3)
        expected = "First sentence. Second sentence. Third sentence."
        self.assertEqual(result, expected)

    def test_default_sentences(self):
        """Default sentences=5 extracts first five sentences."""
        text = "One. Two. Three. Four. Five. Six. Seven."
        result = summarize_text(text)
        self.assertEqual(result, "One. Two. Three. Four. Five.")

    def test_fewer_sentences_than_requested(self):
        """If text has fewer sentences, return all."""
        text = "Only one sentence."
        result = summarize_text(text, sentences=5)
        self.assertEqual(result, "Only one sentence.")

    def test_empty_text(self):
        """Empty text returns empty string."""
        result = summarize_text("", sentences=5)
        self.assertEqual(result, "")

    def test_punctuation_variety(self):
        """Sentence split works with ! and ?."""
        text = "Hello! How are you? I'm fine. Goodbye."
        result = summarize_text(text, sentences=2)
        self.assertEqual(result, "Hello! How are you?")

    def test_no_trailing_space_after_punctuation(self):
        """Split works when punctuation followed by space."""
        text = "Hello. World."
        result = summarize_text(text, sentences=2)
        self.assertEqual(result, "Hello. World.")

    def test_multiple_spaces(self):
        """Multiple spaces after punctuation handled."""
        text = "First.   Second.   Third."
        result = summarize_text(text, sentences=2)
        self.assertEqual(result, "First. Second.")

    def test_newlines(self):
        """Newlines treated as part of sentence."""
        text = "First.\nSecond.\nThird."
        result = summarize_text(text, sentences=2)
        self.assertEqual(result, "First. Second.")

    def test_custom_sentences_zero(self):
        """sentences=0 returns empty string."""
        text = "Some text."
        result = summarize_text(text, sentences=0)
        self.assertEqual(result, "")

    def test_negative_sentences(self):
        """Negative sentences should still work (slice before start)."""
        text = "Hello."
        result = summarize_text(text, sentences=-1)
        self.assertEqual(result, "")  # Because slice [: -1] yields empty


if __name__ == '__main__':
    unittest.main()