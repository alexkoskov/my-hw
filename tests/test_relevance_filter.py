#!/usr/bin/env python3
"""Tests for ``news_bot._is_hot_wheels_relevant`` + ``filter_new_entries``
sibling-brand filter (added after the Matchbox post leaked into the
channel via autoevolution's cross-tagged RSS feed on 2026-04-28)."""

import itertools
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import news_bot
from news_bot import (
    _hold_for_review_reason,
    _is_hot_wheels_relevant,
    _is_promo_article,
    _is_rejected_genre,
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

    Block rule as tuned across two false-positive review rounds (15
    realistic lamley/autoevolution/t-hunted/orangetrack snippets):

      * a SELLER-voice marker AND (>= 1 DIRECT call-to-action OR >= 2
        call-to-action markers of any tier), OR
      * >= 3 distinct call-to-action markers (dense CTA stack).

    An ad tells the READER TO ACT; journalism does not, even when it
    quotes a shopkeeper. Round 1 established that seller voice is
    required (news covers somebody else's shop); round 2 showed it is
    not sufficient (interviews and community posts quote owners and
    fans saying "our store…"). WEAK commerce words — including the
    URL-slug token — never affect the verdict and are reported for the
    operator only. Returns the matched-marker list (truthy = promo) so
    the E035 alert can show WHY; empty = not promo.
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

    def test_seller_voice_plus_two_offer_ctas_blocked(self):
        """Seller voice + 2 CTA markers of the OFFER tier (no imperative
        needed once two offers stack up)."""
        entry = {'title': 'Hot Wheels novidades',
                 'link': 'https://example.com/2026/07/novidades.html'}
        article = {
            'paragraphs': [
                'Em nossa loja: frete grátis e cupom de 10% para novos '
                'clientes.',
            ],
        }
        markers = _is_promo_article(entry, article)
        self.assertTrue(markers)
        self.assertIn('nossa loja', markers)

    def test_three_ctas_without_seller_voice_blocked(self):
        """Second branch: a dense call-to-action stack blocks even with
        no seller-voice phrase."""
        entry = {'title': 'Hot Wheels novidades',
                 'link': 'https://example.com/2026/07/novidades.html'}
        article = {
            'paragraphs': [
                'Compre já! Frete grátis e cupom para novos clientes.',
            ],
        }
        # CTA: 'compre já' (direct) + 'frete grátis' + 'cupom' = 3.
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

    def test_seller_voice_alone_is_not_enough(self):
        """Round 2's core lesson: seller voice WITHOUT any call to
        action never blocks, no matter how much commerce vocabulary
        surrounds it. Interviews and community posts quote owners and
        fans speaking in exactly this first person."""
        entry = {'title': 'Hot Wheels novidades',
                 'link': 'https://example.com/2026/07/novidades.html'}
        article = {
            'paragraphs': [
                'Visite nossa loja! Estoque novo com desconto de '
                'lançamento e muita oferta à venda.',
            ],
        }
        self.assertFalse(_is_promo_article(entry, article))

    def test_seller_voice_plus_single_offer_cta_not_blocked(self):
        """Threshold pin: seller voice + ONE offer-tier CTA stays below
        the bar — this is the community-roundup quote genre ('our store
        … there was even a discount code floating around'). A DIRECT
        imperative in the same position WOULD block (see
        test_en_promo_blocked)."""
        entry = {'title': 'Hot Wheels finds',
                 'link': 'https://example.com/2026/07/finds.html'}
        article = {
            'paragraphs': [
                'Our store finally restocked the wave, and a discount '
                'code was going around for early birds.',
            ],
        }
        self.assertFalse(_is_promo_article(entry, article))

    def test_cta_plus_weak_without_seller_voice_not_blocked(self):
        """Deliberate round-1 trade-off: a CTA verb plus commerce words,
        with nobody speaking as the shop, no longer blocks. This is the
        price of keeping retail journalism publishable — the dense-stack
        branch (>= 3 CTA) still catches real ad copy."""
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

    # --- false-positive regression set (review round 2) -------------------
    #
    # The seller-voice gate introduced in round 1 blocked a NARROWER but
    # very valuable class: human-interest / interview / community pieces
    # that QUOTE a shop owner or a fan in the first person. The
    # shop-closing retrospective is exactly the lamley genre this channel
    # wants to publish. Fixed by requiring a call to action alongside the
    # seller voice — an ad tells the reader to act, an interview doesn't.

    def test_fp_shop_owner_interview_closing(self):
        entry = {
            'title': 'After 30 Years, a Beloved Hot Wheels Shop Says '
                     'Goodbye',
            'link': 'https://lamleygroup.com/2026/07/'
                    'hot-wheels-shop-owner-interview-closing.html',
        }
        article = {'paragraphs': [
            'For three decades, collectors up and down the coast made the '
            'pilgrimage to this unassuming strip-mall storefront. "Our '
            'store has always focused on giving collectors a place to dig '
            'through boxes nobody else bothers with," the owner told us in '
            'an interview last week. "We still have plenty in stock, and '
            'everything left is at a discount until the doors close for '
            'good." The closure reflects a broader trend of independent '
            'hobby shops struggling against online resellers.',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_pt_shop_closing_owner_quote(self):
        entry = {
            'title': 'Loja histórica de Hot Wheels anuncia fechamento após '
                     '15 anos',
            'link': 'https://t-hunted.blogspot.com/2026/07/'
                    'loja-historica-fechamento-entrevista.html',
        }
        article = {'paragraphs': [
            'Depois de 15 anos atendendo colecionadores na capital, uma '
            'loja tradicional de Hot Wheels anunciou o fechamento das '
            'portas nesta semana. "Em nossa loja sempre tivemos o cuidado '
            'de separar peças raras para quem realmente entende do '
            'assunto", disse o dono ao anunciar o fim das atividades. '
            '"Ainda temos bom estoque, e estamos com desconto em tudo até '
            'o último dia."',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_founder_qa_two_seller_synonyms(self):
        """Two different seller-voice synonyms in quotes and no CTA at
        all — under the round-1 rule the two seller markers alone
        cleared the bar."""
        entry = {
            'title': 'Q&A: The Founder of a Legendary Hot Wheels Shop '
                     'Looks Back',
            'link': 'https://lamleygroup.com/2026/07/'
                    'hot-wheels-shop-founder-qa-retrospective.html',
        }
        article = {'paragraphs': [
            'We sat down with the founder of one of the hobby\'s most '
            'recognizable retail names to talk about three decades in '
            'business. "Our store started as a card table at a swap '
            'meet," he recalled with a laugh. "People still ask why our '
            'shop never moved to a bigger building -- honestly, we just '
            'never wanted to lose that feeling."',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_community_roundup_member_quote(self):
        """A fan's colloquial "our store" (meaning 'the store we shop
        at') plus a factually-reported 'discount code'."""
        entry = {
            'title': 'Community Roundup: Collectors Share Their Best 2026 '
                     'Finds',
            'link': 'https://orangetrackdiecast.com/2026/07/'
                    'community-roundup-best-finds.html',
        }
        article = {'paragraphs': [
            'Every month we round up highlights from the collector '
            'community Discord -- this time, a restock story from a '
            'regular member. "Our store finally got the new Boulevard '
            'wave back in stock this week," one member wrote, "and there '
            'was even a discount code floating around for early birds."',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    def test_fp_owner_profile_single_signal(self):
        """Round-2 control case: the same interview genre with only one
        seller-voice signal and nothing else."""
        entry = {
            'title': 'Meet the Owner Behind a Local Hot Wheels Institution',
            'link': 'https://lamleygroup.com/2026/07/owner-profile.html',
        }
        article = {'paragraphs': [
            '"Our store is really just an extension of my own '
            'collection," the owner said, describing how the shop grew '
            'from a garage hobby into a full storefront over twenty '
            'years.',
        ]}
        self.assertFalse(_is_promo_article(entry, article))

    # --- WEAK-marker reporting hygiene (subsumption) ----------------------
    #
    # Since round 2 the WEAK tier no longer affects the verdict at all —
    # it is reported to the operator in the [E035] ping. Subsumption
    # keeps that report honest: a WEAK word swallowed by a matched
    # marker's own span is not listed as if it were separate evidence.

    def test_weak_inside_a_matched_marker_is_not_reported_twice(self):
        entry, article = self._incident()
        markers = _is_promo_article(entry, article)
        # 'nossa loja' matched; the article ALSO says 'na loja' in the
        # title, so 'loja' is genuine independent evidence here.
        self.assertIn('nossa loja', markers)
        self.assertIn('loja', markers)
        # But scanning the seller phrase alone reports no bare 'loja'.
        strong, weak = news_bot._promo_scan_markers('Em nossa loja.')
        self.assertIn('nossa loja', strong)
        self.assertNotIn('loja', weak)

    def test_no_marker_self_supplies_a_weak_hit(self):
        """Invariant guard over the marker tuples themselves: scanning a
        text that is EXACTLY one decision-tier marker must never yield a
        WEAK hit. Pins the subsumption rule for every current and future
        marker pair, so editing the lists can't silently reintroduce the
        double-count."""
        for marker in news_bot._PROMO_STRONG_MARKERS:
            with self.subTest(marker=marker):
                strong, weak = news_bot._promo_scan_markers(marker)
                self.assertIn(marker, strong)
                self.assertEqual(
                    weak, [],
                    f"marker {marker!r} self-supplied WEAK {weak!r}",
                )

    def test_weak_word_outside_the_matched_span_still_reported(self):
        """The subsumption rule removes only the matched span: the same
        weak word used independently elsewhere is still reported."""
        strong, weak = news_bot._promo_scan_markers(
            'Em nossa loja e também na loja parceira do shopping.')
        self.assertIn('nossa loja', strong)
        self.assertIn('loja', weak)

    # --- word-boundary regression ----------------------------------------

    def test_word_boundary_guard_is_what_keeps_these_below_the_bar(self):
        """Both fixtures sit exactly one CTA marker below the bar and
        cross it the moment a marker starts matching inside a LONGER
        word — i.e. this test fails if the space-padded word-boundary
        folding regresses to naive substring matching. Kept at the
        decision level (not the marker list) so it stays mutation-proof
        under the round-2 rule, where WEAK words no longer count."""
        pt_entry = {'title': 'Loja de Hot Wheels fecha as portas',
                    'link': 'https://example.com/2026/07/fechamento.html'}
        pt_article = {'paragraphs': [
            'Em nossa loja o dono pediu que os fãs não percam a última '
            'semana de atendimento.',
        ]}
        # 'não percam' must NOT match the DIRECT marker 'não perca'.
        # Naive matching → seller + 1 direct CTA → blocked.
        self.assertFalse(_is_promo_article(pt_entry, pt_article))

        en_entry = {'title': 'Collectors swap tips on hunting the wave',
                    'link': 'https://example.com/2026/07/tips.html'}
        en_article = {'paragraphs': [
            'Our store regulars traded coupon codes and discount codes '
            'for the big-box chains this week.',
        ]}
        # Plural 'coupon codes' / 'discount codes' must NOT match the
        # OFFER markers 'coupon code' / 'discount code'. Naive matching
        # → seller + 2 CTA → blocked.
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


class TestPromoMarkerSets(unittest.TestCase):
    """Structural invariants of the promo-marker tuples themselves
    (review round 2, code-reviewer): the tiers carry the block rule, so
    a reworded or misfiled marker must fail loudly here rather than
    silently weaken the filter. Everything is derived from the LIVE
    tuples — no hardcoded copy to drift out of sync."""

    def test_tiers_are_non_empty(self):
        for name in ('_PROMO_CTA_DIRECT_MARKERS', '_PROMO_CTA_OFFER_MARKERS',
                     '_PROMO_SELLER_MARKERS', '_PROMO_WEAK_MARKERS'):
            with self.subTest(tier=name):
                self.assertTrue(getattr(news_bot, name))

    def test_decision_tiers_are_pairwise_disjoint(self):
        """A marker in two tiers would be counted twice by the rule (e.g.
        a seller phrase that also counts as its own CTA would let seller
        voice alone block — exactly the round-2 false-positive class)."""
        tiers = {
            'direct': set(news_bot._PROMO_CTA_DIRECT_MARKERS),
            'offer': set(news_bot._PROMO_CTA_OFFER_MARKERS),
            'seller': set(news_bot._PROMO_SELLER_MARKERS),
            'weak': set(news_bot._PROMO_WEAK_MARKERS),
        }
        for a, b in itertools.combinations(sorted(tiers), 2):
            with self.subTest(pair=(a, b)):
                self.assertEqual(
                    tiers[a] & tiers[b], set(),
                    f"marker(s) in both {a} and {b}",
                )

    def test_strong_union_is_exactly_the_three_decision_tiers(self):
        """``_PROMO_STRONG_MARKERS`` drives the span-blanking pass; if a
        decision-tier marker were missing from it, that marker's span
        would leak WEAK hits into the operator's marker list."""
        self.assertEqual(
            set(news_bot._PROMO_STRONG_MARKERS),
            set(news_bot._PROMO_CTA_DIRECT_MARKERS)
            | set(news_bot._PROMO_CTA_OFFER_MARKERS)
            | set(news_bot._PROMO_SELLER_MARKERS),
        )

    def test_every_marker_used_by_the_rule_is_in_the_folded_lookup(self):
        """The rule reads the raw tuples but matching runs off the
        pre-folded lookups — a marker missing there would be dead
        weight that never matches anything."""
        folded_strong = {m for m, _f in news_bot._PROMO_STRONG_FOLDED}
        folded_weak = {m for m, _f in news_bot._PROMO_WEAK_FOLDED}
        self.assertEqual(folded_strong, set(news_bot._PROMO_STRONG_MARKERS))
        self.assertEqual(folded_weak, set(news_bot._PROMO_WEAK_MARKERS))

    def test_no_marker_folds_to_empty(self):
        """A marker made only of punctuation/accents would fold to the
        bare padding string and then match EVERY article."""
        for marker in (news_bot._PROMO_STRONG_MARKERS
                       + news_bot._PROMO_WEAK_MARKERS):
            with self.subTest(marker=marker):
                self.assertNotEqual(news_bot._promo_fold(marker).strip(), '')

    def test_seller_markers_are_first_person(self):
        """Seller voice means the publisher speaking AS the shop; a
        third-person phrase here would re-open the round-1 class where
        ordinary retail news blocked."""
        for marker in news_bot._PROMO_SELLER_MARKERS:
            with self.subTest(marker=marker):
                self.assertTrue(
                    marker.startswith(('nossa', 'em nossa', 'our')),
                    f"{marker!r} is not a first-person seller phrase",
                )


class _ContentGateFixtures:
    """Article fixtures shared by the hold / genre detector suites."""

    # The real 2026-07-25 prod post the operator complained about: four
    # sentences of "here are the photos", 12 images, and a video our
    # parser cannot embed. It also SAYS «no vídeo abaixo» — the precedence
    # rule (hold beats drop) is tested against exactly this text.
    POSTER_LINK = ('https://t-hunted.blogspot.com/2026/07/'
                   'as-fotos-do-ultimo-poster-da-hot-wheels.html')
    POSTER_TITLE = 'As fotos do último poster da Hot Wheels 2026'

    def _poster(self):
        return (
            {'title': self.POSTER_TITLE, 'link': self.POSTER_LINK},
            {
                'title': self.POSTER_TITLE,
                'subtitle': '',
                'paragraphs': [
                    'Saiu o último poster da Hot Wheels para 2026.',
                    'As fotos mostram os carros da linha básica.',
                    'Confira todas as imagens abaixo.',
                    'Veja também no vídeo abaixo.',
                ],
            },
        )

    def _entry(self, title, link='https://example.com/2026/07/news.html'):
        return ({'title': title, 'link': link},
                {'title': title, 'paragraphs': ['Corpo do texto.']})


class TestHoldForReviewReason(_ContentGateFixtures, unittest.TestCase):
    """``_hold_for_review_reason`` — category 1 of the content gate
    (2026-07-25): poster / catalog / packaging posts are STAGED BUT HELD
    and go out only if the operator presses «✅ Опубликовать».

    Detection is SUBJECT-anchored: only the title and the (title-derived)
    URL slug are scanned. Body text is deliberately out of scope — an
    article that merely MENTIONS a poster is not a poster post.

    Threshold is strict on purpose: ONE marker holds. The operator said
    they would not have published the incident post, and a hold is
    cheap and reversible (one button press), unlike a publish.
    """

    # --- the incident -----------------------------------------------------

    def test_incident_poster_post_is_held(self):
        entry, article = self._poster()
        self.assertTrue(_hold_for_review_reason(entry, article))

    def test_incident_markers_name_the_reason(self):
        entry, article = self._poster()
        markers = _hold_for_review_reason(entry, article)
        self.assertIn('poster', markers)

    def test_incident_holds_on_the_title_alone(self):
        """Slug is corroboration, not the anchor — strip it and the title
        still holds."""
        entry, article = self._poster()
        entry['link'] = 'https://t-hunted.blogspot.com/2026/07/p1.html'
        self.assertTrue(_hold_for_review_reason(entry, article))

    def test_slug_hit_is_reported_with_a_url_prefix(self):
        """A marker found only in the slug is diagnosable as such."""
        entry, article = self._entry(
            'Novidades da semana',
            'https://t-hunted.blogspot.com/2026/07/o-catalogo-2026.html',
        )
        markers = _hold_for_review_reason(entry, article)
        self.assertIn('url:catálogo', markers)

    # --- positives --------------------------------------------------------

    def test_pt_catalog_post_is_held(self):
        entry, article = self._entry('O catálogo Hot Wheels 2026 completo')
        self.assertIn('catálogo', _hold_for_review_reason(entry, article))

    def test_en_catalog_post_is_held(self):
        entry, article = self._entry(
            'The full 2026 Hot Wheels catalog is here')
        self.assertIn('catalog', _hold_for_review_reason(entry, article))

    def test_en_catalogue_spelling_is_held(self):
        entry, article = self._entry('2026 Hot Wheels catalogue leaked')
        self.assertTrue(_hold_for_review_reason(entry, article))

    def test_en_packaging_post_is_held(self):
        entry, article = self._entry(
            'Hot Wheels is changing its packaging in 2026')
        self.assertIn('packaging', _hold_for_review_reason(entry, article))

    def test_pt_packaging_post_is_held(self):
        entry, article = self._entry(
            'Hot Wheels muda a embalagem da linha básica em 2026')
        self.assertIn('embalagem', _hold_for_review_reason(entry, article))

    def test_blister_and_cardback_posts_are_held(self):
        for title, marker in (
            ('Novo blister da Hot Wheels chega em 2026', 'blister'),
            ('New Hot Wheels cardback design revealed', 'cardback'),
            ('Hot Wheels 2026 card art gets a redesign', 'card art'),
        ):
            with self.subTest(title=title):
                entry, article = self._entry(title)
                self.assertIn(
                    marker, _hold_for_review_reason(entry, article))

    def test_case_and_accent_insensitive(self):
        entry, article = self._entry('AS FOTOS DO ULTIMO POSTER DA HOT WHEELS')
        self.assertTrue(_hold_for_review_reason(entry, article))

    # --- negatives --------------------------------------------------------

    def test_convention_exclusive_car_reveal_is_not_held(self):
        entry, article = self._entry(
            'Hot Wheels Convention 2026 exclusive Datsun revealed')
        self.assertFalse(_hold_for_review_reason(entry, article))

    def test_ordinary_model_news_is_not_held(self):
        entry, article = self._entry(
            'Hot Wheels 2026 Super Treasure Hunt Nissan Skyline revealed')
        self.assertFalse(_hold_for_review_reason(entry, article))

    def test_restock_news_is_not_held(self):
        entry, article = self._entry(
            'Hot Wheels Premium chega às lojas em setembro')
        self.assertFalse(_hold_for_review_reason(entry, article))

    def test_checklist_post_is_not_held(self):
        entry, article = self._entry(
            'Hot Wheels 2026 J Case Contents Checklist')
        self.assertFalse(_hold_for_review_reason(entry, article))

    def test_body_mention_of_a_poster_does_not_hold(self):
        """Subject-anchored, not keyword soup: the body may talk about the
        poster all it likes as long as the post is about something else."""
        entry = {'title': 'Hot Wheels reveals the 2026 Datsun 510',
                 'link': 'https://example.com/2026/07/datsun-510.html'}
        article = {
            'title': 'Hot Wheels reveals the 2026 Datsun 510',
            'paragraphs': [
                'The car also shows up on the 2026 poster and in the '
                'catalog spread, next to the new packaging.',
            ],
        }
        self.assertFalse(_hold_for_review_reason(entry, article))

    def test_word_boundaries_are_respected(self):
        """Folding is word-bounded: a marker must not fire on a longer
        word that merely contains it."""
        for title in (
            'Hot Wheels posterior wing redesign explained',
            'Hot Wheels cataloguing tips for collectors',
        ):
            with self.subTest(title=title):
                entry, article = self._entry(title)
                self.assertFalse(_hold_for_review_reason(entry, article))

    # --- robustness -------------------------------------------------------

    def test_empty_and_missing_inputs_do_not_crash(self):
        for entry, article in (
            ({}, {}),
            ({'title': None, 'link': None}, {'paragraphs': None}),
            (None, None),
            ({'link': 'https://example.com/x'}, {}),
        ):
            with self.subTest(entry=entry):
                self.assertEqual(_hold_for_review_reason(entry, article), [])

    def test_malformed_non_string_fields_do_not_raise(self):
        entry = {'title': ['not', 'a', 'string'], 'link': 42}
        article = {'title': None, 'paragraphs': 'not a list'}
        self.assertEqual(_hold_for_review_reason(entry, article), [])

    def test_unparseable_url_does_not_raise(self):
        entry = {'title': 'Ordinary news', 'link': 'http://[oops'}
        self.assertEqual(
            _hold_for_review_reason(entry, {'paragraphs': []}), [])

    def test_title_is_char_capped(self):
        """Scan bound: a megabyte-sized title cannot stall intake, so a
        marker past the cap is not scanned."""
        title = 'a' * (news_bot._PROMO_SCAN_MAX_CHARS + 50) + ' poster'
        entry, article = self._entry(title)
        self.assertFalse(_hold_for_review_reason(entry, article))


class TestIsRejectedGenre(_ContentGateFixtures, unittest.TestCase):
    """``_is_rejected_genre`` — categories 2 and 3 of the content gate:
    video reviews and events/conventions are DROPPED at intake (like the
    promo filter), with an [E037] ping.

    Returns ``(genre, markers)``; ``(None, [])`` means keep. Same
    subject-anchored scan as the hold detector (title + URL slug only).

    The event rule is deliberately NARROW — see
    ``test_convention_exclusive_car_reveal_survives``: a convention name
    is a place, not a subject. Only an ORGANIZATIONAL signal (dates,
    tickets, registration, schedule…) makes the post about the event.
    """

    # --- video reviews ----------------------------------------------------

    def test_pt_video_post_is_dropped(self):
        entry, article = self._entry(
            'Vídeo: Hot Wheels 2026 linha básica completa')
        genre, markers = _is_rejected_genre(entry, article)
        self.assertEqual(genre, 'video')
        self.assertIn('vídeo', markers)

    def test_unboxing_post_is_dropped(self):
        entry, article = self._entry(
            'Unboxing da caixa J de 2026 da Hot Wheels')
        genre, markers = _is_rejected_genre(entry, article)
        self.assertEqual(genre, 'video')
        self.assertIn('unboxing', markers)

    def test_assista_post_is_dropped(self):
        entry, article = self._entry(
            'Assista ao review da linha Boulevard 2026')
        self.assertEqual(_is_rejected_genre(entry, article)[0], 'video')

    def test_watch_colon_lead_is_dropped(self):
        entry, article = self._entry(
            'Watch: the 2026 Hot Wheels Legends Tour final run')
        genre, markers = _is_rejected_genre(entry, article)
        self.assertEqual(genre, 'video')
        self.assertIn('watch:', markers)

    def test_en_video_post_is_dropped(self):
        entry, article = self._entry(
            'Video review: every 2026 Hot Wheels Super Treasure Hunt')
        self.assertEqual(_is_rejected_genre(entry, article)[0], 'video')

    def test_youtube_in_the_title_is_dropped(self):
        entry, article = self._entry(
            'Novo canal no YouTube mostra a linha 2026')
        self.assertEqual(_is_rejected_genre(entry, article)[0], 'video')

    # --- video: the false-positive side -----------------------------------

    def test_article_that_merely_embeds_a_video_survives(self):
        """The operator's rule: a post that CONTAINS a video is not a video
        review — the video has to be the subject."""
        entry = {'title': 'Hot Wheels reveals the 2026 Datsun 510',
                 'link': 'https://example.com/2026/07/datsun-510.html'}
        article = {
            'title': 'Hot Wheels reveals the 2026 Datsun 510',
            'paragraphs': [
                'Watch the reveal in the video below, and assista também '
                'ao unboxing no canal do YouTube.',
            ],
        }
        self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    def test_watch_out_for_is_not_a_video_post(self):
        """`watch` only counts as the «Watch:» lead form — otherwise
        ordinary editorial English («watch out for…», «a car worth
        watching») would be dropped."""
        for title in (
            'Watch out for these five 2026 Treasure Hunts',
            'The 2026 casting worth watching this year',
        ):
            with self.subTest(title=title):
                entry, article = self._entry(title)
                self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    # --- events -----------------------------------------------------------

    def test_pt_convention_with_dates_and_tickets_is_dropped(self):
        entry, article = self._entry(
            'Convenção Hot Wheels 2026: datas e ingressos já disponíveis')
        genre, markers = _is_rejected_genre(entry, article)
        self.assertEqual(genre, 'event')
        self.assertIn('convenção', markers)
        self.assertIn('ingressos', markers)

    def test_en_convention_with_organizational_words_is_dropped(self):
        entry, article = self._entry(
            'Hot Wheels Collectors Convention 2026 will be held in Chicago')
        genre, markers = _is_rejected_genre(entry, article)
        self.assertEqual(genre, 'event')
        self.assertIn('convention', markers)
        self.assertIn('will be held', markers)

    def test_expo_with_registration_is_dropped(self):
        entry, article = self._entry(
            'Diecast Expo 2026: registration opens Monday')
        self.assertEqual(_is_rejected_genre(entry, article)[0], 'event')

    def test_pt_meetup_with_schedule_is_dropped(self):
        entry, article = self._entry(
            'Encontro de colecionadores: programação e credenciamento')
        self.assertEqual(_is_rejected_genre(entry, article)[0], 'event')

    def test_nationals_with_tickets_is_dropped(self):
        entry, article = self._entry(
            'Hot Wheels Nationals 2026 tickets sell out in a day')
        self.assertEqual(_is_rejected_genre(entry, article)[0], 'event')

    # --- events: the CRITICAL false-positive side -------------------------

    def test_convention_exclusive_car_reveal_survives(self):
        """THE guard rail. A convention-exclusive casting reveal is real
        model news — the kind of post the channel exists for. The mere
        NAME of a convention must never be enough to drop."""
        for title in (
            'Hot Wheels Convention 2026 exclusive Datsun revealed',
            'Exclusivo da convenção 2026: o Datsun 510 revelado',
            'Nationals 2026 exclusive Mustang gets a Spectraflame finish',
            'Every Hot Wheels Legends Tour winner, ranked',
        ):
            with self.subTest(title=title):
                entry, article = self._entry(title)
                self.assertEqual(
                    _is_rejected_genre(entry, article), (None, []),
                    f"{title!r} is model news, not an event announcement",
                )

    def test_event_body_details_do_not_drop_a_car_reveal(self):
        """Subject-anchored: the organizational detail may live in the
        BODY of a car-reveal post without turning it into an event post."""
        entry = {'title': 'Hot Wheels Convention 2026 exclusive Datsun revealed',
                 'link': 'https://example.com/2026/07/conv-datsun.html'}
        article = {
            'title': 'Hot Wheels Convention 2026 exclusive Datsun revealed',
            'paragraphs': [
                'The convention will be held in Los Angeles; tickets and '
                'registration open in March, and the full schedule is out.',
            ],
        }
        self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    def test_organizational_words_without_an_event_name_survive(self):
        """The other half of the AND: «dates» alone is ordinary release
        language («release dates», «datas de lançamento»)."""
        for title in (
            'Hot Wheels 2026 mainline release dates confirmed',
            'Datas de lançamento da linha básica 2026',
        ):
            with self.subTest(title=title):
                entry, article = self._entry(title)
                self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    # --- other negatives --------------------------------------------------

    def test_ordinary_news_is_not_a_rejected_genre(self):
        entry, article = self._entry(
            'Hot Wheels Premium chega às lojas em setembro')
        self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    def test_checklist_post_is_not_a_rejected_genre(self):
        entry, article = self._entry(
            'Hot Wheels 2026 J Case Contents Checklist')
        self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    # --- robustness -------------------------------------------------------

    def test_empty_and_missing_inputs_do_not_crash(self):
        for entry, article in (
            ({}, {}),
            ({'title': None, 'link': None}, {'paragraphs': None}),
            (None, None),
        ):
            with self.subTest(entry=entry):
                self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    def test_malformed_non_string_fields_do_not_raise(self):
        entry = {'title': {'nope': 1}, 'link': ['a']}
        article = {'title': 7, 'paragraphs': 'not a list'}
        self.assertEqual(_is_rejected_genre(entry, article), (None, []))

    def test_unparseable_url_does_not_raise(self):
        entry = {'title': 'Ordinary news', 'link': 'http://[oops'}
        self.assertEqual(
            _is_rejected_genre(entry, {'paragraphs': []}), (None, []))


class TestContentGatePrecedence(_ContentGateFixtures, unittest.TestCase):
    """HOLD beats DROP. The incident post is about a poster AND says «no
    vídeo abaixo» — it must reach the operator for a decision, not be
    silently binned as a video post.

    The precedence lives in ``job()`` (hold is evaluated first and the
    genre check is skipped on a hold), so it is pinned here at the
    detector level and again end-to-end in ``test_integration``.
    """

    def test_incident_post_holds_even_though_it_mentions_a_video(self):
        entry, article = self._poster()
        self.assertTrue(_hold_for_review_reason(entry, article))

    def test_poster_video_title_is_both_but_hold_wins(self):
        """Worst case: BOTH detectors fire on the same title. The gate
        must hold, never drop."""
        entry, article = self._entry(
            'Vídeo: as fotos do novo poster da Hot Wheels 2026')
        self.assertTrue(_hold_for_review_reason(entry, article))
        self.assertEqual(_is_rejected_genre(entry, article)[0], 'video')
        # job()'s ordering resolves the tie — see
        # tests/test_integration.py::TestContentGateIntake.


class TestContentGateMarkerSets(unittest.TestCase):
    """Structural invariants of the content-gate marker tuples, mirroring
    ``TestPromoMarkerSets``. Derived from the LIVE tuples so a reworded or
    misfiled marker fails loudly instead of silently weakening the gate."""

    ALL_TIERS = (
        '_HOLD_TITLE_MARKERS',
        '_GENRE_VIDEO_MARKERS',
        '_GENRE_EVENT_NAME_MARKERS',
        '_GENRE_EVENT_ORG_MARKERS',
    )

    def test_tiers_are_non_empty(self):
        for name in self.ALL_TIERS:
            with self.subTest(tier=name):
                self.assertTrue(getattr(news_bot, name))

    def test_tiers_are_pairwise_disjoint(self):
        """A marker in two tiers would make one category silently satisfy
        another's rule (e.g. an event ORG word that also names an event
        would drop on its own, re-opening the reveal false positive)."""
        tiers = {name: set(getattr(news_bot, name)) for name in self.ALL_TIERS}
        for a, b in itertools.combinations(sorted(tiers), 2):
            with self.subTest(pair=(a, b)):
                self.assertEqual(
                    tiers[a] & tiers[b], set(), f"marker(s) in both {a} and {b}")

    def test_no_two_markers_fold_to_the_same_token(self):
        """Matching runs on the folded form, so «catálogo» and «catalogo»
        would be one rule with two names — and would be reported twice in
        the operator's marker list."""
        for name in self.ALL_TIERS:
            folded = [news_bot._promo_fold(m) for m in getattr(news_bot, name)]
            with self.subTest(tier=name):
                self.assertEqual(
                    len(folded), len(set(folded)),
                    f"{name} has markers that fold to the same token",
                )

    def test_no_marker_folds_to_empty(self):
        """A marker made only of punctuation would fold to the bare
        padding string and then match EVERY article."""
        for name in self.ALL_TIERS:
            for marker in getattr(news_bot, name):
                with self.subTest(marker=marker):
                    self.assertNotEqual(
                        news_bot._promo_fold(marker).strip(), '')

    def test_every_marker_is_in_its_folded_lookup(self):
        for name, folded_name in (
            ('_HOLD_TITLE_MARKERS', '_HOLD_TITLE_FOLDED'),
            ('_GENRE_VIDEO_MARKERS', '_GENRE_VIDEO_FOLDED'),
            ('_GENRE_EVENT_NAME_MARKERS', '_GENRE_EVENT_NAME_FOLDED'),
            ('_GENRE_EVENT_ORG_MARKERS', '_GENRE_EVENT_ORG_FOLDED'),
        ):
            with self.subTest(tier=name):
                self.assertEqual(
                    {m for m, _f in getattr(news_bot, folded_name)},
                    set(getattr(news_bot, name)),
                )

    def test_content_gate_markers_do_not_collide_with_promo_markers(self):
        """The two filters run back to back on the same article; an
        overlapping marker would make one drop reason masquerade as the
        other in the operator's alert."""
        gate = set()
        for name in self.ALL_TIERS:
            gate |= set(getattr(news_bot, name))
        promo = (set(news_bot._PROMO_STRONG_MARKERS)
                 | set(news_bot._PROMO_WEAK_MARKERS))
        self.assertEqual(gate & promo, set())


if __name__ == '__main__':
    unittest.main()
