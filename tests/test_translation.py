#!/usr/bin/env python3
"""
Unit tests for translation (translate_text).
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import translate_text


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


if __name__ == '__main__':
    unittest.main()