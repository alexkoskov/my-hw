#!/usr/bin/env python3
"""
Unit tests for translation (translate_text, transcreate_text).
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import translate_text, transcreate_text


class TestTranslateText(unittest.TestCase):
    """Test translate_text function."""

    @patch('news_bot.GoogleTranslator')
    def test_successful_translation(self, mock_translator_class):
        """translate_text returns translated text when GoogleTranslator works."""
        mock_translator = MagicMock()
        mock_translator.translate.return_value = 'Привет мир'
        mock_translator_class.return_value = mock_translator

        result = translate_text('Hello world', source='auto', target='ru')
        self.assertEqual(result, 'Привет мир')
        mock_translator_class.assert_called_once_with(source='auto', target='ru')
        mock_translator.translate.assert_called_once_with('Hello world')

    @patch('news_bot.GoogleTranslator')
    def test_default_parameters(self, mock_translator_class):
        """translate_text uses default source='auto' and target='ru'."""
        mock_translator = MagicMock()
        mock_translator.translate.return_value = 'Текст'
        mock_translator_class.return_value = mock_translator

        result = translate_text('Text')
        self.assertEqual(result, 'Текст')
        mock_translator_class.assert_called_once_with(source='auto', target='ru')
        mock_translator.translate.assert_called_once_with('Text')

    @patch('news_bot.GoogleTranslator')
    def test_translation_exception_falls_back(self, mock_translator_class):
        """If translation raises exception, return original text."""
        mock_translator = MagicMock()
        mock_translator.translate.side_effect = Exception('API error')
        mock_translator_class.return_value = mock_translator

        result = translate_text('Original text')
        self.assertEqual(result, 'Original text')

    @patch('news_bot.GoogleTranslator')
    def test_empty_text(self, mock_translator_class):
        """Empty text should be translated (GoogleTranslator may return empty)."""
        mock_translator = MagicMock()
        mock_translator.translate.return_value = ''
        mock_translator_class.return_value = mock_translator

        result = translate_text('')
        self.assertEqual(result, '')
        mock_translator.translate.assert_called_once_with('')

    @patch('news_bot.GoogleTranslator')
    def test_logging_on_error(self, mock_translator_class):
        """Ensure error is logged when translation fails."""
        import logging
        mock_translator = MagicMock()
        mock_translator.translate.side_effect = Exception('API error')
        mock_translator_class.return_value = mock_translator

        with self.assertLogs('news_bot', level='ERROR') as cm:
            result = translate_text('test')
        self.assertEqual(result, 'test')
        self.assertTrue(any('Translation failed' in record.message for record in cm.records))


class TestTranscreateText(unittest.TestCase):
    """Test transcreate_text function (post-Decision 11 simplified contract).

    After Decision 11 (Wave 6 / Task 10) only the HW glossary post-pass and
    title emoji prefix remain. The 19 bureaucratic regex patterns, 4
    passive→active flips and 4000-char body truncation were removed because:
      - Claude is now the primary transcreator and writes idiomatic Russian
        (the bureaucratic cleanup targeted Google Translate output).
      - Telegraph has no length limit, so 4000-char truncation is obsolete.
    """

    @patch('news_bot.GoogleTranslator')
    def test_hw_glossary_replaces_hot_wheels_translit(self, mock_translator_class):
        """HW glossary: «Хот Уилс» translit → «Hot Wheels» brand spelling."""
        mock_translator = MagicMock()
        mock_translator.translate.return_value = 'Хот Уилс анонсировали новую серию.'
        mock_translator_class.return_value = mock_translator

        result = transcreate_text('Hot Wheels announced a new series.')
        self.assertIn('Hot Wheels', result)
        self.assertNotIn('Хот Уилс', result)

    @patch('news_bot.GoogleTranslator')
    def test_hw_glossary_replaces_garage_build(self, mock_translator_class):
        """HW glossary: «сборка гаража» → «гаражный проект»."""
        mock_translator = MagicMock()
        mock_translator.translate.return_value = 'Это сборка гаража мечты.'
        mock_translator_class.return_value = mock_translator

        result = transcreate_text('This is a dream garage build.')
        self.assertIn('гаражный проект', result)
        self.assertNotIn('сборка гаража', result)

    @patch('news_bot.GoogleTranslator')
    def test_bureaucratic_phrase_not_replaced(self, mock_translator_class):
        """Regression: bureaucratic regex was removed — «является» must pass through.

        Pre-Decision 11 the function rewrote «является» → «это». After Task 10
        no such substitution happens; the word survives in the output verbatim.
        """
        mock_translator = MagicMock()
        mock_translator.translate.return_value = 'Это является важной частью коллекции.'
        mock_translator_class.return_value = mock_translator

        result = transcreate_text('This is an important part of the collection.')
        self.assertIn('является', result)

    @patch('news_bot.GoogleTranslator')
    def test_passive_construction_not_flipped(self, mock_translator_class):
        """Regression: passive→active flips were removed — «был выполнен» stays."""
        mock_translator = MagicMock()
        mock_translator.translate.return_value = 'Релиз был выполнен вчера.'
        mock_translator_class.return_value = mock_translator

        result = transcreate_text('The release was completed yesterday.')
        self.assertIn('был выполнен', result)
        self.assertNotIn('сделали', result)

    @patch('news_bot.GoogleTranslator')
    def test_body_not_truncated_at_4000(self, mock_translator_class):
        """Regression: 4000-char body truncation was removed.

        Translator returns ~5000 chars with an explicit END_MARKER tail;
        result must contain that marker (would have been chopped pre-Task 10).
        """
        long_body = ('А' * 5000) + ' END_MARKER'
        mock_translator = MagicMock()
        mock_translator.translate.return_value = long_body
        mock_translator_class.return_value = mock_translator

        result = transcreate_text('whatever')
        self.assertIn('END_MARKER', result)
        # Sanity: length is preserved (no implicit cut at 4000).
        self.assertGreater(len(result), 4000)

    @patch('news_bot.GoogleTranslator')
    def test_title_gets_emoji_prefix(self, mock_translator_class):
        """is_title=True prepends a single content-aware emoji."""
        mock_translator = MagicMock()
        mock_translator.translate.return_value = 'Hot Wheels анонсируют новую модель'
        mock_translator_class.return_value = mock_translator

        result = transcreate_text('Hot Wheels announce a new model', is_title=True)
        # Emoji is the first char(s); the result must start with one of the
        # known title emojis chosen by transcreate_text.
        expected_emojis = ('🏆', '🏎️', '🚀', '💎', '🤝', '📢', '🚗', '🔥')
        self.assertTrue(
            any(result.startswith(e) for e in expected_emojis),
            f"Expected result to start with one of {expected_emojis}; got: {result!r}",
        )

    @patch('news_bot.GoogleTranslator')
    def test_fallback_to_original_on_translator_error(self, mock_translator_class):
        """Exception in GoogleTranslator → return the original input text."""
        mock_translator = MagicMock()
        mock_translator.translate.side_effect = Exception('API error')
        mock_translator_class.return_value = mock_translator

        result = transcreate_text('Original English text')
        self.assertEqual(result, 'Original English text')


if __name__ == '__main__':
    unittest.main()
