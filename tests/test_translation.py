#!/usr/bin/env python3
"""
Unit tests for transcreate_text (Google-fallback translation path).
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import transcreate_text, _strip_plugs, _strip_plugs_in_blocks


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


class TestStripPlugs(unittest.TestCase):
    """Variant B of the author-plug-filter feature: post-translation
    inline plug removal."""

    def test_canonical_leak_phrase_removed(self):
        # The 2026-05-02 14:40 production leak — must be cut.
        text = "Привет всем коллекционерам. (подписывайтесь на меня в Instagram @diecast215). Конец статьи."
        result = _strip_plugs(text)
        self.assertNotIn('@diecast215', result)
        self.assertNotIn('подписывайтесь', result.lower())
        self.assertIn('Привет всем коллекционерам.', result)
        self.assertIn('Конец статьи.', result)

    def test_en_inline_plug_removed(self):
        text = "Hello readers. Follow me on Instagram @diecast215. End of story."
        result = _strip_plugs(text)
        self.assertNotIn('@diecast215', result)
        self.assertNotIn('follow me', result.lower())
        self.assertIn('Hello readers.', result)
        self.assertIn('End of story.', result)

    def test_google_translate_paren_variant(self):
        # GT might render "follow me on Instagram @x" as different cue verbs
        # — the parenthesised umbrella with mandatory @handle catches them all.
        text = "Текст. (посмотрите меня в Instagram @x_collector). Дальше."
        result = _strip_plugs(text)
        self.assertNotIn('@x_collector', result)
        self.assertIn('Текст.', result)
        self.assertIn('Дальше.', result)

    def test_real_content_with_instagram_preserved(self):
        # Mention without cue verb / @handle → not a plug.
        text = "Коллекционер написал в Instagram, что нашёл редкий Chase."
        self.assertEqual(_strip_plugs(text), text)

    def test_journalistic_paren_no_handle_preserved(self):
        # No @handle in the parens → not caught by the umbrella.
        text = "Хорошие фото есть в источнике (см. фото в Instagram)."
        self.assertEqual(_strip_plugs(text), text)

    def test_corporate_mattel_plug_preserved(self):
        # AC16 — corporate plug is out of scope; me/us anchor doesn't match.
        text = "Follow Mattel on Instagram, X, and Facebook for more news."
        self.assertEqual(_strip_plugs(text), text)

    def test_soft_cue_without_target_preserved(self):
        # «подписывайтесь на новости индустрии через RSS» — cue verb but
        # no target/platform anchor; must NOT match.
        text = "Подписывайтесь на новости индустрии через RSS-агрегаторы — это удобно."
        self.assertEqual(_strip_plugs(text), text)

    def test_multiple_plugs_one_pass(self):
        text = "Начало. (подписывайтесь на меня в Instagram @x). Середина. (follow me on Twitter @y). Конец."
        result = _strip_plugs(text)
        self.assertNotIn('@x', result)
        self.assertNotIn('@y', result)
        self.assertIn('Начало.', result)
        self.assertIn('Середина.', result)
        self.assertIn('Конец.', result)

    def test_none_passthrough(self):
        self.assertIsNone(_strip_plugs(None))

    def test_empty_string_passthrough(self):
        self.assertEqual(_strip_plugs(''), '')

    def test_idempotent(self):
        text = "Текст. (подписывайтесь на меня в Instagram @x). Конец."
        once = _strip_plugs(text)
        twice = _strip_plugs(once)
        self.assertEqual(once, twice)

    def test_strip_plugs_in_blocks_drops_empty_paragraph_block(self):
        blocks = [
            {"type": "paragraph", "text": "Реальный контент."},
            {"type": "paragraph", "text": "(подписывайтесь на меня в Instagram @x)"},
            {"type": "paragraph", "text": "Ещё контент."},
        ]
        result = _strip_plugs_in_blocks(blocks)
        # Block 2 became empty → dropped. Other two kept in order.
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['text'], 'Реальный контент.')
        self.assertEqual(result[1]['text'], 'Ещё контент.')

    def test_strip_plugs_in_blocks_keeps_image_with_empty_caption(self):
        # Image block with caption that becomes empty → image still kept.
        blocks = [
            {"type": "image", "src": "https://cdn/x.jpg",
             "caption": "(подписывайтесь на меня в Instagram @x)"},
        ]
        result = _strip_plugs_in_blocks(blocks)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['caption'], '')

    def test_strip_plugs_in_blocks_passes_through_video(self):
        blocks = [{"type": "video", "src": "https://youtube/abc"}]
        result = _strip_plugs_in_blocks(blocks)
        self.assertEqual(result, blocks)


if __name__ == '__main__':
    unittest.main()
