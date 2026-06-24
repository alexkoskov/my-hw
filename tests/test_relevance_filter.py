#!/usr/bin/env python3
"""Tests for ``news_bot._is_hot_wheels_relevant`` + ``filter_new_entries``
sibling-brand filter (added after the Matchbox post leaked into the
channel via autoevolution's cross-tagged RSS feed on 2026-04-28)."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
from news_bot import (
    _is_hot_wheels_relevant,
    _is_text_only_checklist,
    filter_new_entries,
)


class TestRssEntryLabels(unittest.TestCase):
    """`_fetch_rss_entries` must carry each entry's Blogger labels (RSS
    <category> → feedparser `tags`) into the item dict so the relevance
    filter can reject by the source's own brand label."""

    def test_labels_captured_from_feed_tags(self):
        fake_entry = {
            'link': 'https://t-hunted.blogspot.com/2026/06/moving.html',
            'title': 'Mais fotos dos carros da série Moving Parts de 2026',
            'summary': '...',
            'tags': [{'term': 'Matchbox'}],
        }
        with patch('news_bot.load_feeds',
                   return_value=['https://t-hunted.blogspot.com/feeds/posts/default?alt=rss']), \
             patch('news_bot.fetch_rss', return_value=[fake_entry]):
            items = news_bot._fetch_rss_entries()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].get('labels'), ['Matchbox'])

    def test_missing_tags_yield_empty_labels(self):
        fake_entry = {'link': 'https://www.autoevolution.com/news/x.html',
                      'title': 'Hot Wheels news', 'summary': ''}
        with patch('news_bot.load_feeds',
                   return_value=['https://www.autoevolution.com/rss/tag-Hot+Wheels.xml']), \
             patch('news_bot.fetch_rss', return_value=[fake_entry]):
            items = news_bot._fetch_rss_entries()
        self.assertEqual(items[0].get('labels'), [])


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

    def test_t_hunted_default_reject_without_hot_wheels(self):
        """2026-06-09 incident: ``Topper Toys 1970: Johnny Lightning…``
        and ``Mundo Premium 64 #241: Porsche, Mustang…`` slipped from
        t-hunted (Brazilian-Portuguese diecast-collector blog covering
        many brands) because the default for neutral titles was
        "include". For broad-diecast sources the default flips to
        "reject" — title MUST mention Hot Wheels explicitly to pass.
        """
        self.assertFalse(_is_hot_wheels_relevant({
            'title': 'Topper Toys 1970: Johnny Lightning e os loucos Jet Power',
            'link': 'https://t-hunted.blogspot.com/2026/06/topper-toys-1970.html',
        }))
        self.assertFalse(_is_hot_wheels_relevant({
            'title': 'Mundo Premium 64 #241: Porsche, Mustang e Diablo',
            'link': 'https://t-hunted.blogspot.com/2026/06/mundo-premium-64-241.html',
        }))
        self.assertFalse(_is_hot_wheels_relevant({
            'title': 'M2 Machines novidades de junho',
            'link': 'https://t-hunted.blogspot.com/2026/06/m2-machines.html',
        }))

    def test_t_hunted_with_hot_wheels_keeps_entry(self):
        """A real HW post on t-hunted that mentions Hot Wheels explicitly
        must pass the broad-diecast filter — same code path that catches
        autoevolution's HW posts."""
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Hot Wheels Boulevard mix-3 review',
            'link': 'https://t-hunted.blogspot.com/2026/06/hot-wheels-boulevard.html',
        }))
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Novidades Hot Wheels J Case mainline',
            'link': 'https://t-hunted.blogspot.com/2026/06/hot-wheels-j-case.html',
        }))

    def test_t_hunted_hw_series_name_keeps_entry(self):
        """2026-06-23 incident: t-hunted's Portuguese titles name the Hot
        Wheels *series* (Silver Series, Pop Culture, Neon Speeder, RLC…)
        without the literal words "hot wheels", so the broad-diecast
        default-reject was dropping genuine HW posts and the channel went
        silent for days. A recognised HW series/line name now counts as a
        Hot Wheels signal."""
        for title in (
            'Uma nova Silver Series com uma Ferrari!',
            'Mais fotos do novo lote da série Pop Culture',
            'Mais fotos da série Neon Speeder de 2026',
            'Hot Wheels 2026 Car Culture / Vintage Racing T case report',
            'A volta da pickup Ford F-100 para o Red Line Club!',
        ):
            self.assertTrue(
                _is_hot_wheels_relevant({
                    'title': title,
                    'link': 'https://t-hunted.blogspot.com/2026/06/x.html',
                }),
                f"HW series title should pass the t-hunted filter: {title!r}",
            )

    def test_sibling_brand_beats_hw_series_name(self):
        """A shared series name (e.g. Matchbox also has a "Moving Parts"
        line) must NOT pass when the title also names a sibling brand —
        the explicit sibling-brand reject takes precedence over the
        series-name signal."""
        self.assertFalse(_is_hot_wheels_relevant({
            'title': 'Mais fotos da série Moving Parts do filme da Matchbox',
            'link': 'https://t-hunted.blogspot.com/2026/06/moving-parts-matchbox.html',
        }))

    def test_sibling_brand_label_rejects(self):
        """2026-06-24: the source's own brand label (Blogger "Labels:",
        carried as RSS <category>) is authoritative. A post tagged with a
        sibling diecast brand is rejected even if the title contains a word
        that looks like a HW series — e.g. "Moving Parts" is a MATCHBOX line.
        This is the exact post that leaked through the title-only filter."""
        self.assertFalse(_is_hot_wheels_relevant({
            'title': 'Mais fotos dos carros da série Moving Parts de 2026',
            'link': 'https://t-hunted.blogspot.com/2026/06/moving.html',
            'labels': ['Matchbox'],
        }))
        # Other sibling brands seen in the live feed.
        for brand in ('Maisto', 'Tomica', 'Johnny Lightning'):
            self.assertFalse(_is_hot_wheels_relevant({
                'title': 'Alguma novidade diecast',
                'link': 'https://t-hunted.blogspot.com/2026/06/x.html',
                'labels': [brand],
            }), f"label {brand!r} must reject")

    def test_hw_series_label_keeps_entry(self):
        """A Hot Wheels series label keeps the post even when the title has
        no HW signal (e.g. the Pop Culture Porsche, titled in Portuguese)."""
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Mais fotos do Porsche de K-Pop Demon Hunters',
            'link': 'https://t-hunted.blogspot.com/2026/06/porsche.html',
            'labels': ['Pop Culture'],
        }))

    def test_sibling_label_beats_hw_series_label(self):
        """If somehow tagged with both, the sibling brand wins (reject)."""
        self.assertFalse(_is_hot_wheels_relevant({
            'title': 'Alguma série',
            'link': 'https://t-hunted.blogspot.com/2026/06/y.html',
            'labels': ['Silver Series', 'Matchbox'],
        }))

    def test_explicit_hot_wheels_title_beats_sibling_label(self):
        """Cross-over articles naming Hot Wheels explicitly still pass even
        with a sibling-brand label (preserves the Matchbox-vs-HW round-ups)."""
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Hot Wheels vs Matchbox — qual comprar?',
            'link': 'https://t-hunted.blogspot.com/2026/06/z.html',
            'labels': ['Matchbox'],
        }))

    def test_moving_parts_no_label_falls_through_to_reject(self):
        """Defense-in-depth: 'moving parts' was removed from the HW-series
        title guesses (it's a Matchbox line), so even with the label missing
        a t-hunted Moving Parts post is rejected by the broad-diecast default."""
        self.assertFalse(_is_hot_wheels_relevant({
            'title': 'Mais fotos dos carros da série Moving Parts de 2026',
            'link': 'https://t-hunted.blogspot.com/2026/06/moving.html',
        }))

    def test_non_t_hunted_neutral_title_still_defaults_to_include(self):
        """Regression: only t-hunted (and other future broad-diecast
        sources) gets the strict default. autoevolution / lamley /
        orangetrack continue to default-include neutral titles —
        operator-confirmed those are HW-focused enough that strict
        filtering would drop legitimate content."""
        # No link → no source identification → default include.
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Bugatti supercar news today',
        }))
        # autoevolution link → not in broad-diecast list → default include.
        self.assertTrue(_is_hot_wheels_relevant({
            'title': 'Bugatti supercar news today',
            'link': 'https://www.autoevolution.com/news/bugatti-news-271234.html',
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

    def test_orangetrack_case_contents_checklist_slug_dropped_regardless_of_body(self):
        """URL-slug trigger (A): orangetrack's recurring 'case-contents-
        checklist' posts pad the body with per-car blurbs so the
        500-char floor doesn't fire, but the prose is ~80% proper
        nouns and the LLM produces English-leaking output. Drop on
        URL pattern alone — body length irrelevant. Regression for
        the 2026-05-12 prod silence."""
        entry = {
            'title': 'Hot Wheels Basics 2026 J Case Contents Checklist for Mainline',
            'link': 'https://orangetrackdiecast.com/2026/05/11/'
                    'hot-wheels-basics-2026-j-case-contents-checklist-for-mainline/',
        }
        article = {'paragraphs': ['x' * 4000]}  # well above the body floor
        self.assertTrue(_is_text_only_checklist(entry, article))

    def test_orangetrack_h_case_contents_checklist_also_dropped(self):
        """The pattern repeats monthly with a different letter — H, G, J,
        and onward. URL match is case-insensitive."""
        entry = {
            'title': 'Hot Wheels Basics 2026 H Case Contents Checklist for Mainline',
            'link': 'https://orangetrackdiecast.com/2026/04/19/'
                    'Hot-Wheels-Basics-2026-H-Case-Contents-Checklist-For-Mainline/',
        }
        article = {'paragraphs': ['x' * 4000]}
        self.assertTrue(_is_text_only_checklist(entry, article))

    def test_case_report_slug_is_not_caught_by_url_trigger(self):
        """'Case-report' (not 'case-contents-checklist') stays in — those
        posts have real editorial content (e.g. team-transport-K
        report that successfully shipped 2026-05-06). Only the
        narrow 'case-contents-checklist' slug is filtered. Body
        below floor still wouldn't trigger trigger B because the
        title has no 'checklist' word."""
        entry = {
            'title': 'Hot Wheels 2026 Car Culture Team Transport K Case Report',
            'link': 'https://orangetrackdiecast.com/2026/05/02/'
                    'hot-wheels-2026-car-culture-team-transport-k-case-report/',
        }
        article = {'paragraphs': ['short']}
        self.assertFalse(_is_text_only_checklist(entry, article))


if __name__ == '__main__':
    unittest.main()
