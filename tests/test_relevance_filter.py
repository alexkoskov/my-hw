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
    _is_promo_article,
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


class TestIsPromoArticle(unittest.TestCase):
    """Intake promo/ad filter ([E035]). Prod incident 2026-07-25:
    t-hunted.blogspot.com published a pure shop-promo post («Hot Wheels
    antigos e raros na loja Universo Hot Wheels») and the bot translated
    (wasted tokens) + posted it to the channel.

    Block rule as tuned in review round 1 (a 10-snippet false-positive
    probe of realistic lamley/autoevolution/t-hunted headlines blocked
    4 legit stories under the first version):

      * a SELLER-voice marker ('nossa loja' / 'our store' / 'our shop')
        AND (>= 2 distinct STRONG total OR >= 2 distinct WEAK), OR
      * >= 3 distinct STRONG markers (dense CTA stack).

    Seller voice is the ad-vs-journalism discriminator: news covers
    somebody else's shop, an ad speaks as the shop. The URL-slug token
    is a WEAK corroborator only. Returns the matched-marker list
    (truthy = promo) so the E035 alert can show WHY; empty = not promo.
    """

    # --- fixtures ---------------------------------------------------------

    def _incident(self):
        """Modeled on the real 2026-07-25 t-hunted incident post."""
        entry = {
            'title': 'Hot Wheels antigos e raros na loja Universo Hot Wheels',
            'link': 'https://t-hunted.blogspot.com/2026/07/'
                    'hot-wheels-antigos-e-raros-na-loja.html',
        }
        article = {
            'title': 'Hot Wheels antigos e raros na loja Universo Hot Wheels',
            'subtitle': '',
            'paragraphs': [
                'Em nossa loja Universo Hot Wheels você encontra Hot Wheels '
                'antigos e raros para a sua coleção.',
                'Não perca as novidades desta semana!',
                'Garanta o seu antes que acabe o estoque.',
            ],
        }
        return entry, article

    # --- positives --------------------------------------------------------

    def test_incident_pt_promo_blocked(self):
        entry, article = self._incident()
        markers = _is_promo_article(entry, article)
        self.assertTrue(markers)

    def test_incident_markers_name_the_reasons(self):
        """The returned list carries the matched markers so the operator
        alert can show WHY the article was dropped."""
        entry, article = self._incident()
        markers = _is_promo_article(entry, article)
        self.assertIn('nossa loja', markers)
        self.assertIn('não perca', markers)
        self.assertIn('garanta o seu', markers)
        self.assertIn('url:loja', markers)

    def test_en_promo_blocked(self):
        entry = {
            'title': 'Rare Hot Wheels now available at our store',
            'link': 'https://example.com/2026/07/rare-hot-wheels.html',
        }
        article = {
            'paragraphs': [
                'Visit our store and use code HW10 at checkout.',
            ],
        }
        markers = _is_promo_article(entry, article)
        self.assertTrue(markers)
        self.assertIn('our store', markers)
        self.assertIn('use code', markers)

    def test_seller_voice_plus_two_weak_blocked(self):
        """Second blocking branch: seller voice + 2 distinct WEAK
        markers. 'loja' inside 'nossa loja' does NOT count towards the
        two (subsumed), so 'estoque' + 'desconto' are what carry it."""
        entry = {'title': 'Hot Wheels novidades',
                 'link': 'https://example.com/2026/07/novidades.html'}
        article = {
            'paragraphs': [
                'Visite nossa loja! Estoque novo com desconto de '
                'lançamento.',
            ],
        }
        markers = _is_promo_article(entry, article)
        self.assertTrue(markers)
        self.assertIn('nossa loja', markers)

    def test_three_strong_without_seller_voice_blocked(self):
        """Third branch: a dense call-to-action stack blocks even with
        no seller-voice phrase."""
        entry = {'title': 'Hot Wheels novidades',
                 'link': 'https://example.com/2026/07/novidades.html'}
        article = {
            'paragraphs': [
                'Compre já! Frete grátis e cupom para novos clientes.',
            ],
        }
        # strong: 'compre já' + 'frete grátis' + 'cupom' = 3.
        self.assertTrue(_is_promo_article(entry, article))

    def test_url_slug_token_is_weak_only(self):
        """Review round 1: the 'loja'/'shop'/'store' slug token was
        demoted from STRONG to WEAK. A slug plus one strong body phrase
        no longer reaches the bar (this fixture BLOCKED before the
        demotion)."""
        entry = {
            'title': 'Hot Wheels raros e antigos',
            'link': 'https://t-hunted.blogspot.com/2026/07/'
                    'hot-wheels-antigos-e-raros-na-loja.html',
        }
        article = {'paragraphs': ['Garanta o seu hoje mesmo.']}
        self.assertFalse(_is_promo_article(entry, article))

    def test_url_slug_token_still_reported_as_marker_when_blocked(self):
        """Demoted, but still diagnostic: when the article blocks on its
        own merits the slug token is listed in the marker output."""
        entry, article = self._incident()
        self.assertIn('url:loja', _is_promo_article(entry, article))

    def test_cta_plus_weak_without_seller_voice_not_blocked(self):
        """Deliberate round-1 trade-off: a CTA verb plus commerce words,
        with nobody speaking as the shop, no longer blocks. This is the
        price of keeping retail journalism publishable — the dense-stack
        branch (>= 3 STRONG) still catches real ad copy."""
        entry = {'title': 'Hot Wheels novidades',
                 'link': 'https://example.com/2026/07/novidades.html'}
        article = {
            'paragraphs': [
                'Compre já: desconto especial na loja parceira.',
            ],
        }
        # strong: 'compre já' (no seller voice); weak: 'desconto', 'loja'.
        self.assertFalse(_is_promo_article(entry, article))

    def test_accent_and_case_insensitive(self):
        """'NÃO PERCA' and the accent-less 'nao perca' both match the
        canonical 'não perca' marker (accent-strip + lowercase on both
        sides)."""
        for phrase in ('NÃO PERCA', 'nao perca'):
            entry = {'title': 'Hot Wheels raros',
                     'link': 'https://example.com/2026/07/raros.html'}
            article = {
                'paragraphs': [f'{phrase}! Garanta o seu hoje em nossa loja.'],
            }
            markers = _is_promo_article(entry, article)
            self.assertTrue(markers, f'phrase {phrase!r} must block')
            self.assertIn('não perca', markers)

    # --- negatives (must NOT block) --------------------------------------

    def test_en_news_weak_only_not_blocked(self):
        """Legit news uses commerce vocabulary ('hits stores', 'in
        stock', 'discount') — weak markers alone never block, whatever
        their count."""
        entry = {
            'title': 'New Hot Wheels line hits stores in September',
            'link': 'https://example.com/2026/07/new-line-september.html',
        }
        article = {
            'paragraphs': [
                'The 2027 mainline will be in stock at retailers '
                'nationwide, and collectors hunting a discount can wait '
                'for the holiday season.',
            ],
        }
        self.assertFalse(_is_promo_article(entry, article))

    def test_pt_news_chega_as_lojas_not_blocked(self):
        entry = {
            'title': 'Novidades Hot Wheels chegam às lojas em setembro',
            'link': 'https://t-hunted.blogspot.com/2026/07/'
                    'novidades-setembro.html',
        }
        article = {
            'paragraphs': [
                'A nova série chega às lojas brasileiras em setembro, '
                'com dez modelos inéditos.',
            ],
        }
        self.assertFalse(_is_promo_article(entry, article))

    def test_single_strong_marker_never_blocks(self):
        """One lone STRONG marker (here 'promoção') is not enough — a
        news piece may mention a promo in passing."""
        entry = {'title': 'Hot Wheels em promoção na rede varejista',
                 'link': 'https://example.com/2026/07/varejista.html'}
        article = {
            'paragraphs': ['A rede anunciou os novos preços nesta terça.'],
        }
        self.assertFalse(_is_promo_article(entry, article))

    def test_one_strong_one_weak_not_blocked(self):
        """Boundary below the bar: 1 STRONG + only 1 distinct WEAK."""
        entry = {'title': 'Hot Wheels update',
                 'link': 'https://example.com/2026/07/update.html'}
        article = {'paragraphs': ['Compre já na loja parceira.']}
        # strong: 'compre já'; weak: 'loja' only.
        self.assertFalse(_is_promo_article(entry, article))

    def test_checklist_post_not_blocked(self):
        """A checklist post has no promo call-to-action — it is handled
        by _is_text_only_checklist, not the promo filter."""
        entry = {'title': '2026 Hot Wheels Mainline Checklist Q3 Update',
                 'link': 'https://example.com/2026/07/checklist-q3.html'}
        article = {'paragraphs': ['Mainline 2026', 'Q3 release wave']}
        self.assertFalse(_is_promo_article(entry, article))

    def test_empty_and_missing_inputs_do_not_crash(self):
        """Defensive: empty/missing paragraphs or a None article must
        not crash — and must not be promo."""
        entry = {'title': 'Hot Wheels news',
                 'link': 'https://example.com/2026/07/news.html'}
        self.assertFalse(_is_promo_article(entry, {'paragraphs': []}))
        self.assertFalse(_is_promo_article(entry, {}))
        self.assertFalse(_is_promo_article(entry, None))
        self.assertFalse(_is_promo_article({}, None))

    def test_malformed_non_string_fields_do_not_raise(self):
        """Audit SEC-PROMO-1: a malformed feed can hand us a non-str
        link/title/paragraph (Atom oddities, list-valued fields). The
        filter must degrade to 'not promo' instead of raising —
        an exception here would crash the whole tick BEFORE
        mark_processed and crash-loop on restart."""
        cases = [
            {'link': ['not', 'a', 'string'], 'title': 'Hot Wheels news'},
            {'link': 42, 'title': None},
            {'link': True, 'title': {'x': 'y'}},
            {'link': {'href': 'http://x/loja'}, 'title': 'HW'},
        ]
        for entry in cases:
            with self.subTest(entry=entry):
                self.assertFalse(
                    _is_promo_article(entry, {'paragraphs': ['Body.']}))
        # Non-dict entry / article and non-str paragraph entries too.
        self.assertFalse(_is_promo_article('not a dict', None))
        self.assertFalse(_is_promo_article({}, 'not a dict'))
        self.assertFalse(
            _is_promo_article({'link': 'http://x/y'},
                              {'paragraphs': [None, 7, ['x']]}))

    def test_unparseable_url_does_not_raise(self):
        """urlparse raises ValueError on some malformed hosts (e.g. an
        unterminated IPv6 literal) — the slug signal is optional, the
        filter must carry on."""
        entry = {'title': 'Hot Wheels news', 'link': 'http://[::1/loja'}
        self.assertFalse(_is_promo_article(entry, {'paragraphs': ['Body.']}))

    # --- false-positive regression set (review round 1) -------------------
    #
    # Verbatim snippets from the reviewer's 10-item false-positive probe
    # of realistic lamley/autoevolution/t-hunted coverage. All four were
    # BLOCKED by the first version of the rule; they are exactly the
    # retail/community journalism this channel aggregates and must stay
    # publishable.

    def test_fp_mattel_creations_store_drop(self):
        entry = {
            'title': 'Mattel Creations Opens RLC Store Drop With Member '
                     'Discount',
            'link': 'https://lamleygroup.com/2026/07/'
                    'mattel-creations-store-drop-rlc.html',
        }
        article = {'paragraphs': [
            'Mattel Creations opened orders today for its latest Red Line '
            'Club exclusive. RLC members get a discount at checkout, and '
            'the set is expected to sell out fast, with a restock likely '
            'to be in stock again next month for non-members.',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_hobby_shop_closes(self):
        entry = {
            'title': 'Beloved Hot Wheels Shop Closes After 30 Years in '
                     'Business',
            'link': 'https://lamleygroup.com/2026/07/'
                    'hot-wheels-shop-closes-after-30-years.html',
        }
        article = {'paragraphs': [
            'Hot Wheels Shop, a fixture in the local diecast scene for '
            'three decades, is closing its doors next month, the owners '
            'announced this week. Remaining inventory is now on sale, '
            'with select cases still in stock for walk-in customers '
            'before the final day.',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_mattel_relaunches_online_store(self):
        """The hardest false positive: 'shop now' arrives as an accidental
        bigram ('the revamped shop now offers…') alongside a factual
        'free shipping' — 2 STRONG markers with nobody speaking as the
        shop. The seller-voice requirement is what keeps it publishable."""
        entry = {
            'title': 'Mattel Relaunches Hot Wheels Online Store With New '
                     'Collector Perks',
            'link': 'https://autoevolution.com/news/'
                    'mattel-relaunches-hot-wheels-online-store.html',
        }
        article = {'paragraphs': [
            'Mattel has relaunched its Hot Wheels e-commerce storefront '
            'with a cleaner design and faster checkout, the company '
            'announced Tuesday. The revamped shop now offers free '
            'shipping on orders over fifty dollars, part of a push to '
            'compete with third-party resellers.',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_pt_premium_restock_roundup(self):
        entry = {
            'title': 'Hot Wheels Premium chega às lojas com novidades para '
                     'julho',
            'link': 'https://t-hunted.blogspot.com/2026/07/'
                    'hot-wheels-premium-na-loja-julho.html',
        }
        article = {'paragraphs': [
            'A nova leva Premium chega às lojas físicas e ao e-commerce '
            'nesta semana. O estoque inicial deve ser limitado, o que '
            'preocupa colecionadores de plantão à espera de um possível '
            'desconto de lançamento.',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_collector_hunting_guide_genre(self):
        """The hunting-guide / collector-roundup genre this bot actually
        aggregates: 'buy now' and 'shop now' show up as ordinary
        editorial advice about WHERE OTHERS sell."""
        entry = {
            'title': 'Hunting Guide: Where to Find the 2026 Super Treasure '
                     'Hunts',
            'link': 'https://orangetrackdiecast.com/2026/07/'
                    'super-treasure-hunt-hunting-guide.html',
        }
        article = {'paragraphs': [
            'Collectors who spot the 2026 Super Treasure Hunts on the pegs '
            'should buy now rather than wait — secondary-market prices '
            'climb within days of a case landing. The big-box shop now '
            'stocks the wave earlier than most hobby retailers, and a '
            'few chains still offer an in-store discount on multi-packs.',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    # --- WEAK-subsumption (double-counting) regression --------------------

    def test_weak_inside_matched_strong_is_not_double_counted(self):
        """Review round 1 (major): five STRONG markers contain a WEAK one
        as a word ('nossa loja' ⊃ 'loja' …). Counting both let ONE phrase
        supply its own STRONG hit AND a 'corroborating' WEAK hit, so the
        bar silently collapsed to a single independent signal. Reviewer's
        repro — seller voice + exactly one real weak word — must NOT
        block; it DOES block under the old double-counting behaviour."""
        entry = {'title': 'Hot Wheels novidades',
                 'link': 'https://example.com/2026/07/novidades.html'}
        article = {'paragraphs': [
            'Em nossa loja você encontra desconto em alguns itens '
            'selecionados.',
        ]}
        # strong: 'nossa loja'; genuinely independent weak: 'desconto'
        # only — 'loja' is subsumed by the matched strong phrase.
        self.assertFalse(_is_promo_article(entry, article))

    def test_no_strong_marker_self_supplies_a_weak_hit(self):
        """Invariant guard over the marker tuples themselves: scanning a
        text that is EXACTLY one STRONG marker must never yield a WEAK
        hit. Pins the subsumption rule for every current and future
        marker pair, so editing the lists can't silently reintroduce the
        double-count."""
        for marker in news_bot._PROMO_STRONG_MARKERS:
            with self.subTest(marker=marker):
                strong, weak = news_bot._promo_scan_markers(marker)
                self.assertIn(marker, strong)
                self.assertEqual(
                    weak, [],
                    f"STRONG marker {marker!r} self-supplied WEAK {weak!r}",
                )

    def test_weak_word_outside_the_strong_span_still_counts(self):
        """The subsumption rule removes only the matched STRONG span: the
        same weak word used independently elsewhere still corroborates."""
        strong, weak = news_bot._promo_scan_markers(
            'Em nossa loja e também na loja parceira do shopping.')
        self.assertIn('nossa loja', strong)
        self.assertIn('loja', weak)

    # --- word-boundary regression ----------------------------------------

    def test_word_boundary_guard_is_what_keeps_these_below_the_bar(self):
        """Both fixtures sit ONE weak marker below the bar and cross it
        the moment 'loja'/'store'/'oferta' start matching inside
        'lojas'/'restored'/'ofertas' — i.e. this test fails if the
        space-padded word-boundary folding regresses to naive substring
        matching. (The plain PT/EN news negatives can't catch that: they
        have no STRONG marker at all.)"""
        pt_entry = {'title': 'Hot Wheels novidades',
                    'link': 'https://example.com/2026/07/novidades.html'}
        pt_article = {'paragraphs': [
            'Visite nossa loja! Chegaram novidades nos estoques regionais '
            'e com descontos previstos para julho.',
        ]}
        # Real weak count: 0 ('loja' subsumed; 'estoques'/'descontos' are
        # plurals). Naive substring matching would find 'estoque' and
        # 'desconto' → seller + 2 weak → blocked.
        self.assertFalse(_is_promo_article(pt_entry, pt_article))

        en_entry = {'title': 'Hot Wheels collection news',
                    'link': 'https://example.com/2026/07/collection.html'}
        en_article = {'paragraphs': [
            'Our store restored a classic display; discounted offerings '
            'vary by retailer.',
        ]}
        # Real weak count: 0 ('store' subsumed by 'our store'; 'restored',
        # 'discounted' and 'offerings' are longer words). Naive matching
        # would find 'store', 'discount', 'offer' → blocked.
        self.assertFalse(_is_promo_article(en_entry, en_article))

    # --- scan bounds ------------------------------------------------------

    def test_markers_beyond_paragraph_bound_not_scanned(self):
        """Only the first paragraphs are scanned (bounded scan) — promo
        text buried past the bound does not trigger."""
        entry = {'title': 'Hot Wheels retrospective',
                 'link': 'https://example.com/2026/07/retrospective.html'}
        article = {
            'paragraphs': (
                ['Editorial paragraph about the new casting.'] * 10
                + ['Compre já em nossa loja, não perca!']
            ),
        }
        self.assertFalse(_is_promo_article(entry, article))

    def test_markers_beyond_char_cap_not_scanned(self):
        """The joined body is char-capped — a megabyte body cannot stall
        intake and text past the cap does not trigger."""
        entry = {'title': 'Hot Wheels deep dive',
                 'link': 'https://example.com/2026/07/deep-dive.html'}
        article = {
            'paragraphs': [
                'word ' * 500,  # 2500 chars, exceeds the scan cap
                'Compre já em nossa loja, não perca!',
            ],
        }
        self.assertFalse(_is_promo_article(entry, article))

    def test_title_is_char_capped_too(self):
        """Audit SEC-PROMO-2: the cap applies to the TITLE as well, not
        just the body (a 10MB title measured ~0.6s of CPU per entry).
        Same promo phrases block when they sit inside the cap and are
        invisible past it — proving the slice, not an unrelated miss."""
        promo_tail = 'Compre já em nossa loja: não perca, frete grátis!'
        filler = 'palavra ' * 300  # ~2400 chars, past the 2000 cap
        link = 'https://example.com/2026/07/x.html'
        blocked = _is_promo_article(
            {'title': promo_tail, 'link': link}, {'paragraphs': []})
        self.assertTrue(blocked, 'sanity: the phrases block when scanned')
        self.assertFalse(_is_promo_article(
            {'title': filler + promo_tail, 'link': link},
            {'paragraphs': []},
        ))

    def test_url_path_is_char_capped_too(self):
        """The URL path is capped the same way: a slug token past the cap
        contributes no marker. Asserted on an article that blocks on its
        body anyway, so the absence of 'url:loja' is the only signal
        under test."""
        entry, article = self._incident()
        entry = dict(entry, link='https://t-hunted.blogspot.com/'
                                 + 'a' * 2100 + '/na-loja.html')
        markers = _is_promo_article(entry, article)
        self.assertTrue(markers, 'body markers still block the article')
        self.assertNotIn('url:loja', markers)


if __name__ == '__main__':
    unittest.main()
