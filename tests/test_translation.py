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

    def test_forwarding_plug_sentence_removed_facts_kept(self):
        # Incident 2026-08-12: t-hunted recommends a package-forwarding company
        # whenever a drop does not ship internationally. Sentence scope is the
        # whole point — the plug sits at the END of a paragraph carrying prices
        # and model names, so dropping the paragraph (the first attempt) cost
        # the facts.
        text = (
            "Цена в рознице составит 6.99 доллара за штуку, а полный кейс "
            "обойдётся примерно в 84 доллара. Рекомендуем эту компанию для "
            "доставки: www.instagram.com/minidelass/"
        )
        result = _strip_plugs(text)
        self.assertNotIn('instagram.com', result)
        self.assertNotIn('Рекомендуем', result)
        self.assertIn('6.99 доллара', result)
        self.assertIn('84 доллара', result)

    def test_forwarding_plug_removed_in_source_language(self):
        # The RU pass is defence in depth; the PT form reaches `_strip_plugs`
        # too when a rewording slips past the paragraph filter before translation.
        text = (
            "O carro custa 38 dólares. Sugerimos os serviços dessa empresa: "
            "www.instagram.com/minidelass/"
        )
        result = _strip_plugs(text)
        self.assertNotIn('instagram.com', result)
        self.assertIn('38 dólares', result)

    def test_reworded_plug_is_still_caught(self):
        # The load-bearing test of the whole approach: t-hunted rewrites this
        # tail every few weeks, and a rewrite must not need a code change.
        # It is also the test that exposed the first attempt — the signature
        # patterns silently never fired there and the phrase rules did all the
        # work, which the CAPTURED sample could never have revealed.
        text = (
            "A loja não envia pedidos para fora dos Estados Unidos nesta "
            "campanha. Recomendamos o serviço de quem já resolve isso há anos: "
            "www.instagram.com/outra-empresa/"
        )
        result = _strip_plugs(text)
        self.assertNotIn('instagram.com', result)
        self.assertNotIn('Recomendamos', result)
        self.assertIn('não envia pedidos', result)

    def test_recommendation_and_link_in_different_sentences_survive(self):
        # The bound is per-sentence on purpose. If it ever widens, this cuts a
        # sentence carrying a price — the exact loss the filters must not cause.
        text = ("Мы рекомендуем присмотреться к этой серии внимательнее, цена "
                "всего 6.99 доллара. Полная галерея опубликована здесь: "
                "www.instagram.com/collector/")
        self.assertEqual(_strip_plugs(text), text)

    def test_recommendation_without_outside_link_survives(self):
        # One signal is not enough — this is ordinary editorial copy, and the
        # channel exists to publish it.
        text = "Рекомендуем эту модель всем коллекционерам серии Car Culture."
        self.assertEqual(_strip_plugs(text), text)

    def test_outside_link_without_recommendation_survives(self):
        text = "Дизайнер показал прототип в своём Instagram: www.instagram.com/designer/"
        self.assertEqual(_strip_plugs(text), text)

    def test_sibling_brand_domain_is_not_mistaken_for_a_platform(self):
        # `_PLUG_PLATFORMS` contains the bare token `x`, so an unbounded
        # alternation matches the tail of `matchbo|x|.com`. Matchbox is
        # Mattel's sibling brand — the likeliest real domain in this copy.
        text = "Рекомендуем новинку: www.matchbox.com/collectors/2026/"
        self.assertEqual(_strip_plugs(text), text)

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


# ---------------------------------------------------------------------------
# Cross-promo / page-navigation sentences (operator reports 2026-07-29).
#
# Three leaks in three consecutive articles, one family: t-hunted embeds
# "go read our other posts" CTAs and "see the video below" pointers INSIDE
# otherwise legitimate paragraphs. `boilerplate_filter` cannot help — its
# patterns are ^-anchored at PARAGRAPH start and drop the whole paragraph,
# which would take the real prose with them. Sentence granularity is the only
# correct one, which is what `_strip_plugs` already does for author plugs.
# ---------------------------------------------------------------------------


class TestCrossPromoSentenceStripping(unittest.TestCase):
    def test_reported_cross_promo_pair_is_removed(self):
        """The exact paragraph the operator reported."""
        text = (
            "Вы можете посмотреть все, что мы уже публиковали о серии Pop "
            "Culture, по **этой ссылке**. Нажмите здесь и посмотрите, что мы "
            "уже показывали о серии Entertainment."
        )
        self.assertEqual(_strip_plugs(text).strip(), "")

    def test_reported_video_pointer_is_removed(self):
        self.assertEqual(
            _strip_plugs("Больше информации о них есть в видео ниже.").strip(),
            "",
        )

    def test_only_the_pointer_sentence_goes_not_the_paragraph(self):
        """The load-bearing property: real prose in the same paragraph stays.

        A whole-paragraph filter would have deleted the casting news along
        with the pointer — which is why this lives in `_strip_plugs` and not
        in `boilerplate_filter`.
        """
        text = (
            "Mattel показала три новых кастинга. "
            "Больше информации о них есть в видео ниже."
        )
        self.assertEqual(
            _strip_plugs(text).strip(),
            "Mattel показала три новых кастинга.",
        )

    def test_bold_markers_do_not_defeat_the_match(self):
        """The LLM bolds the link text, and this filter runs BEFORE the
        renderer decodes `**` — so the patterns must tolerate the markers."""
        self.assertEqual(
            _strip_plugs("Нажмите **здесь** и посмотрите остальное.").strip(),
            "",
        )

    def test_ordinary_prose_survives(self):
        # A fact must never be collateral damage — the prompt's allowed-drop
        # (d) rewrites fact-carrying pointers instead of dropping them.
        keepers = [
            "Цена — $28.",
            "Коллекционеры делятся находками по всему миру.",
            "В этой серии четыре машинки, и все с Real Riders.",
            # Legitimate reporting that merely mentions a video existing.
            "Mattel выложила промо-видео о новой линейке.",
        ]
        for keeper in keepers:
            with self.subTest(keeper=keeper):
                self.assertEqual(_strip_plugs(keeper).strip(), keeper.strip())

    def test_idempotent(self):
        text = "Ок. Нажмите здесь и смотрите."
        once = _strip_plugs(text)
        self.assertEqual(_strip_plugs(once), once)
