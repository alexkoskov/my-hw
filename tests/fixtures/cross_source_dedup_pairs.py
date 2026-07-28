"""Calibration fixture for the ``dedup-model-series`` pair-tier verdict.

Nine labelled article-pairs consumed by ``tests/test_model_extractor.py``.
This fixture is calibrated for the NEW **pair-rule** (``shares_pair`` over the
``(model + series/theme)`` fingerprint) — NOT the old car-set ``similarity``
Jaccard. The classifier that scores these pairs (``shares_pair`` →
``(any_shared, sorted_shared_pairs, any_distinctive)``) and the harness that
re-wires the calibration tests onto it land in Task 6; this module ships only
the labelled data.

- ``DUPE_PAIRS`` — 4 real cross/same-source dupes (3 SDCC 2026 pop-culture
  exclusives that share a distinctive ``(model|series)`` pair + 1 recurring
  Car Culture line pair that shares only broad pairs).
- ``NON_DUPE_PAIRS`` — 5 probes that MUST NOT hard-block: same car in a
  different series; theme-only Stranger Things mainline-vs-SDCC; same-source
  near-miss on a broad car-line; distinct-but-similar tie-ins; and the real
  2026-07-28 prod false-flag (prose "pop culture" vs a Pop Culture lot, both
  model-less) which must not share ANY pair, not merely no ``|D`` pair.

Pair-dict shape (new pair-tier harness):
    {
        'label': 'pair-1-real-2026-06-03',          # unique; harness lookups use it
        'a': {'title': ..., 'subtitle': ...,
              'paragraphs': [...], 'source_name': 'autoevolution'},
        'b': {'title': ..., 'subtitle': ...,
              'paragraphs': [...], 'source_name': 't-hunted'},
        'expected_verdict': 'duplicate' | 'soft-flag' | 'non-duplicate',
        # 'duplicate'     = distinctive shared pair  → HARD block  ([E015])
        # 'soft-flag'     = broad shared pair (theme-only, or a recurring
        #                   car-line) → article PUBLISHES + ping ([E014])
        # 'non-duplicate' = no shared pair → pass through
        'expected_any_distinctive': bool,           # LOAD-BEARING invariant:
        #   True  ⟺ pair MUST hard-block (a shared |D pair exists)
        #   False ⟺ pair MUST NOT hard-block (never a silent drop)
        'expected_shared_pairs': [ 'porsche 911|k-pop demon hunters|D', ... ],
        #   the concrete pair-keys the extractor should find shared (subset of
        #   the cartesian strict×series product) — documents intent for Task 6.
        'note': 'rationale + real/synthesised marker + real source URL',
    }

Why ``expected_any_distinctive`` is carried per-pair (not just the verdict):
a silent hard-block is irreversible (no manual re-publish, no per-subscriber
recovery), so the asymmetric invariant "MUST block" / "MUST NOT block" is
pinned explicitly and is more important than the aggregate accuracy budget.

Bodies are synthesised to the shape of the real source pages. Brand+model and
series mentions reference real 2026 Hot Wheels releases; the real autoevolution
fetch was Cloudflare-blocked at spec time, so its side is synthesised-but-real
(annotated per ``note``). Every ``Porsche`` in a body is written as
``Porsche 911`` + a lowercase word so the model extractor emits the exact
strict token ``porsche 911`` (a bare ``Porsche <Word>`` would emit a spurious
token); series names use the verbatim lexicon casing.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# The three real SDCC 2026 articles (shared constants so the pairwise dupes
# below stay byte-identical wherever the same article is reused).
#
# Real source URLs (kept in each pair's `note`):
#   t-hunted PT roundup   : https://t-hunted.blogspot.com/2026/06/hot-wheels-revela-exclusivos-da-san.html
#   autoevolution EN      : https://www.autoevolution.com/news/hot-wheels-san-diego-comic-con-releases-are-about-kpop-demon-hunters-stranger-things-and-top-gun-271899.html
#   t-hunted PT «mais fotos»: https://t-hunted.blogspot.com/2026/06/mais-fotos-do-porsche-de-k-pop-demon.html
#
# All three name the concrete "Porsche 911" AND "K-Pop Demon Hunters" (and
# "San Diego Comic-Con"), so the extractor realises the distinctive pairs
# `porsche 911|k-pop demon hunters|D` and `porsche 911|san diego comic-con|D`
# on every side. The Stranger Things / Top Gun exclusives are themed vehicles
# with no recognised car brand → they contribute only series tokens (the two
# roundups additionally share `porsche 911|stranger things|D` and
# `porsche 911|top gun|D` via the cartesian strict×series product).
# ---------------------------------------------------------------------------

_SDCC_THUNTED_ROUNDUP = {
    'title': 'Hot Wheels revela exclusivos da San Diego Comic-Con',
    'subtitle': 'Porsche 911 de K-Pop Demon Hunters, Stranger Things e '
                'Top Gun na San Diego Comic-Con 2026',
    'paragraphs': [
        'A Mattel revelou hoje os Hot Wheels exclusivos da San Diego '
        'Comic-Con 2026, com três grandes franquias homenageadas nesta '
        'edição do evento.',
        'O destaque é o Porsche 911 comemorativo de K-Pop Demon Hunters, '
        'pintado nas cores da banda e com acabamento premium exclusivo da '
        'San Diego Comic-Con.',
        'A linha traz ainda um veículo temático de Stranger Things, '
        'inspirado em Hawkins, e uma peça de Top Gun com pintura naval — '
        'ambos sem marca de carro licenciada.',
        'Todos os exclusivos serão vendidos apenas durante a Comic-Con, em '
        'quantidade estritamente limitada.',
    ],
    'source_name': 't-hunted',
}

_SDCC_AUTOEVO_ROUNDUP = {
    'title': 'Hot Wheels San Diego Comic-Con Releases Are About KPop Demon '
             'Hunters, Stranger Things and Top Gun',
    'subtitle': 'A Porsche 911 joins three franchise tie-ins for the 2026 '
                'San Diego Comic-Con exclusives',
    'paragraphs': [
        'Mattel has unveiled the 2026 San Diego Comic-Con exclusive Hot '
        'Wheels, and this year leans hard into fandom with three franchise '
        'tie-ins.',
        'The headliner is a Porsche 911 celebrating K-Pop Demon Hunters, '
        'finished in the band signature colors with San Diego Comic-Con '
        'exclusive premium detailing.',
        'The lineup also includes a Stranger Things themed vehicle nodding '
        'to Hawkins and a Top Gun piece in naval livery — neither carries a '
        'recognised car brand.',
        'All of the Comic-Con exclusives will be sold only at the show, in '
        'strictly limited numbers.',
    ],
    'source_name': 'autoevolution',
}

_SDCC_MAIS_FOTOS = {
    'title': 'Mais fotos do Porsche 911 de K-Pop Demon Hunters',
    'subtitle': 'Novas imagens do exclusivo da San Diego Comic-Con 2026',
    'paragraphs': [
        'Depois da revelação inicial, surgiram mais fotos do Porsche 911 de '
        'K-Pop Demon Hunters, o exclusivo mais procurado da San Diego '
        'Comic-Con deste ano.',
        'As novas imagens mostram em detalhe a pintura nas cores da banda e '
        'o acabamento premium reservado ao evento.',
        'O modelo continua sendo vendido apenas durante a Comic-Con, sem '
        'previsão de lançamento no varejo.',
    ],
    'source_name': 't-hunted',
}


# ---------------------------------------------------------------------------
# DUPLICATE PAIRS — 4 real dupes.
#   * 3 SDCC pairs share a distinctive |D pair → expect verdict 'duplicate'
#     (hard block), expected_any_distinctive=True.
#   * the Car Culture pair shares only broad |B pairs → 'soft-flag'
#     (publishes), expected_any_distinctive=False.
# ---------------------------------------------------------------------------

DUPE_PAIRS: list[dict] = [
    # Pair 1 — the load-bearing real 2026-06-03 Car Culture Road Trip Mix pair.
    # Under the pair-tier rule this is Car Culture — a recurring, BROAD line —
    # so its shared pairs are all |B → soft-flag (publishes), never a hard
    # block. Its irreversible must-NOT-hard-block property is pinned directly
    # by `test_calibration_not_dupes_never_hard_block` (which selects every
    # `expected_any_distinctive=False` pair across BOTH lists) and its verdict
    # by the ≥7/8 `test_calibration_pair_tier_accuracy` aggregate. NOTE: the old
    # `test_calibration_real_pair_must_pass` was removed in Task 6 — pair-1's
    # former ≥0.50 real-body similarity constraint is no longer enforced by any
    # test, so the label/bodies are no longer load-bearing for a similarity
    # threshold (kept only as a realistic broad-line dupe sample).
    {
        'label': 'pair-1-real-2026-06-03',
        'a': {
            'title': 'New Hot Wheels Car Culture Road Trip Mix preorders start now',
            'subtitle': 'A summer-themed assortment featuring vintage road-trip icons',
            'paragraphs': [
                'Mattel has opened preorders for the new Hot Wheels Car '
                'Culture Road Trip Mix, a five-car assortment that leans hard '
                'into late-summer nostalgia.',
                'Headlining the mix is a Subaru Legacy GT in Bring-a-Picnic '
                'green metallic, complete with a roof rack and surfboards '
                'cast as a single piece.',
                'The lineup also includes a Land Rover S2 in safari livery '
                'and a 2018 Toyota 4Runner with mud-flaps and a roof tent — '
                'both nodding to the camper-culture revival.',
                'A Range Rover Classic in two-tone tan rounds out the road-'
                'trip theme, with the final slot reserved for a collector '
                'chase that Mattel will reveal closer to ship date.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Um novo lote da série Car Culture com carros de viagem',
            'subtitle': 'Mattel revelou os cinco carros da nova série Road Trip Mix',
            'paragraphs': [
                'A Mattel acaba de revelar o novo lote da série Hot Wheels '
                'Car Culture Road Trip Mix, com cinco carros temáticos para '
                'viagens de fim de semana.',
                'O destaque vai para o Subaru Legacy GT em verde metálico, '
                'com bagageiro de teto e duas pranchas de surfe fundidas '
                'como peça única.',
                'O Land Rover S2 vem em pintura safari e o 2018 Toyota '
                '4Runner traz uma barraca de teto e para-lamas extras — '
                'ambos celebram a cultura camper de fim de viagem.',
                'O quinto carro será o chase da série, ainda não revelado '
                'oficialmente pela Mattel.',
            ],
            'source_name': 't-hunted',
        },
        'expected_verdict': 'soft-flag',
        'expected_any_distinctive': False,
        'expected_shared_pairs': [
            'land rover s2|car culture|B',
            'subaru legacy gt|car culture|B',
            'toyota 4runner|car culture|B',
        ],
        'note': (
            'Real 2026-06-03 cross-source pair (autoevolution ↔ t-hunted; '
            'autoevolution side synthesised — Cloudflare blocked the real '
            'fetch at spec time). Shares Subaru Legacy GT / Land Rover S2 / '
            'Toyota 4Runner, but the series is Car Culture — a recurring '
            'BROAD line — so all shared pairs are |B → soft-flag (publishes), '
            'not a hard block. (The old ≥0.50 similarity must-pass test was '
            'removed in Task 6; this pair is now scored purely by the pair-tier '
            'harness, so its real high strict-overlap is no longer asserted.)'
        ),
    },
    # Pair 2 — SDCC cross-source PT ↔ EN. Both roundups name the Porsche 911
    # + K-Pop Demon Hunters (+ SDCC, Stranger Things, Top Gun) → shared
    # distinctive pairs → HARD block.
    {
        'label': 'pair-2-sdcc-cross-source',
        'a': _SDCC_THUNTED_ROUNDUP,
        'b': _SDCC_AUTOEVO_ROUNDUP,
        'expected_verdict': 'duplicate',
        'expected_any_distinctive': True,
        'expected_shared_pairs': [
            'porsche 911|k-pop demon hunters|D',
            'porsche 911|san diego comic-con|D',
            'porsche 911|stranger things|D',
            'porsche 911|top gun|D',
        ],
        'note': (
            'Real SDCC 2026 cross-source dupe: t-hunted PT roundup '
            '(https://t-hunted.blogspot.com/2026/06/hot-wheels-revela-exclusivos-da-san.html) '
            'vs autoevolution EN '
            '(https://www.autoevolution.com/news/hot-wheels-san-diego-comic-con-releases-are-about-kpop-demon-hunters-stranger-things-and-top-gun-271899.html). '
            'Both name Porsche 911 + K-Pop Demon Hunters → distinctive |D '
            'pair shared on both sides → hard block. autoevolution side '
            'synthesised-but-real (Cloudflare-blocked fetch). Empty legacy '
            'similarity `strict`-overlap on the pop-culture side is exactly '
            'why the old Jaccard harness missed this case.'
        ),
    },
    # Pair 3 — SDCC same-source «mais fotos» (both t-hunted). Pins that the
    # any-source pair-rule catches same-source follow-ups the old
    # cross-source-only dedup skipped.
    {
        'label': 'pair-3-sdcc-same-source-mais-fotos',
        'a': _SDCC_THUNTED_ROUNDUP,
        'b': _SDCC_MAIS_FOTOS,
        'expected_verdict': 'duplicate',
        'expected_any_distinctive': True,
        'expected_shared_pairs': [
            'porsche 911|k-pop demon hunters|D',
            'porsche 911|san diego comic-con|D',
        ],
        'note': (
            'Real SDCC 2026 SAME-source follow-up: t-hunted PT roundup '
            '(https://t-hunted.blogspot.com/2026/06/hot-wheels-revela-exclusivos-da-san.html) '
            'vs t-hunted PT «mais fotos» '
            '(https://t-hunted.blogspot.com/2026/06/mais-fotos-do-porsche-de-k-pop-demon.html). '
            'Same source_name (t-hunted) on both sides → pins the any-source '
            'pair-rule (the old cross-source-only dedup skipped same-source). '
            'Shared distinctive Porsche 911 + K-Pop Demon Hunters / '
            'San Diego Comic-Con pairs → hard block.'
        ),
    },
    # Pair 4 — SDCC autoevolution ↔ «mais fotos». Third pairing so the three
    # real SDCC articles pairwise cover each other.
    {
        'label': 'pair-4-sdcc-autoevo-mais-fotos',
        'a': _SDCC_AUTOEVO_ROUNDUP,
        'b': _SDCC_MAIS_FOTOS,
        'expected_verdict': 'duplicate',
        'expected_any_distinctive': True,
        'expected_shared_pairs': [
            'porsche 911|k-pop demon hunters|D',
            'porsche 911|san diego comic-con|D',
        ],
        'note': (
            'Real SDCC 2026 cross-source dupe: autoevolution EN '
            '(https://www.autoevolution.com/news/hot-wheels-san-diego-comic-con-releases-are-about-kpop-demon-hunters-stranger-things-and-top-gun-271899.html) '
            'vs t-hunted PT «mais fotos» '
            '(https://t-hunted.blogspot.com/2026/06/mais-fotos-do-porsche-de-k-pop-demon.html). '
            'Completes pairwise coverage of the three real SDCC articles. '
            'Shared distinctive Porsche 911 + K-Pop Demon Hunters pair → '
            'hard block. autoevolution side synthesised-but-real '
            '(Cloudflare-blocked fetch).'
        ),
    },
]


# ---------------------------------------------------------------------------
# NON-DUPLICATE PAIRS — 4 probes. ALL have expected_any_distinctive=False
# (each MUST NOT hard-block). Two are true passes (no shared pair) and two are
# broad soft-flags (a shared broad/theme-only pair → publishes + ping).
# ---------------------------------------------------------------------------

NON_DUPE_PAIRS: list[dict] = [
    # Pair 5 — SAME car, DIFFERENT series. Proves the key is the PAIR, not the
    # model: both sides are a Porsche 911, but A pairs it with the distinctive
    # K-Pop Demon Hunters franchise while B pairs it with the broad Car
    # Culture line → different pair-keys → NO shared pair → pass.
    {
        'label': 'pair-5-same-car-different-series',
        'a': {
            'title': 'Hot Wheels K-Pop Demon Hunters Porsche 911 revealed',
            'subtitle': 'A premium tie-in celebrating the animated hit',
            'paragraphs': [
                'Mattel has revealed a premium Hot Wheels Porsche 911 '
                'celebrating the K-Pop Demon Hunters franchise, its first '
                'casting tied to the animated hit.',
                'The car wears the band signature colors and ships with '
                'premium Real Riders and event-grade detailing.',
                'It is a one-off collaboration and not part of any recurring '
                'mainline line.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'New Car Culture Porsche 911 joins the recurring mix',
            'subtitle': 'The 911 returns to the Car Culture premium line',
            'paragraphs': [
                'The latest Hot Wheels Car Culture assortment adds a Porsche '
                '911 in classic silver, slotting in as one of five cars in '
                'the mix.',
                'Car Culture is a frequent, recurring premium line, and this '
                '911 has no franchise or event tie-in of any kind.',
                'Real Riders and a detailed metal base round out the release.',
            ],
            'source_name': 't-hunted',
        },
        'expected_verdict': 'non-duplicate',
        'expected_any_distinctive': False,
        'expected_shared_pairs': [],
        'note': (
            'Synthesised. Same model on both sides (porsche 911) but '
            'different series: A emits `porsche 911|k-pop demon hunters|D`, '
            'B emits `porsche 911|car culture|B`. No shared pair → pass. '
            'Guards against keying on the model alone.'
        ),
    },
    # Pair 6 — THEME-ONLY Stranger Things, mainline vs SDCC. Both sides are
    # themed vehicles with NO recognised car brand → empty strict → each emits
    # the theme-only key `*|stranger things|B` (theme-only is ALWAYS broad).
    # Shared broad pair → soft-flag (publishes + ping), never a silent block.
    {
        'label': 'pair-6-theme-only-stranger-things',
        'a': {
            'title': 'Hot Wheels adds a Stranger Things mainline release',
            'subtitle': 'A Hawkins-themed entry joins the 2026 mainline',
            'paragraphs': [
                'The 2026 Hot Wheels mainline gains a Stranger Things entry, '
                'a themed character vehicle inspired by the streets of '
                'Hawkins.',
                'The casting leans on the show 80s aesthetic with neon '
                'graphics and ships at a standard mainline price point.',
                'It is a basic-range release with no premium tires and no '
                'recognised car brand.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Stranger Things Hot Wheels exclusive coming to San Diego Comic-Con',
            'subtitle': 'A limited San Diego Comic-Con 2026 piece for the Hawkins fandom',
            'paragraphs': [
                'A Stranger Things themed Hot Wheels will be sold as a San '
                'Diego Comic-Con 2026 exclusive, a different product from the '
                'mainline entry.',
                'The Comic-Con piece features premium packaging and a limited '
                'event run, again drawing on the Hawkins theme.',
                'It is a themed character vehicle with no recognised car '
                'brand, available only at the show.',
            ],
            'source_name': 't-hunted',
        },
        'expected_verdict': 'soft-flag',
        'expected_any_distinctive': False,
        'expected_shared_pairs': [
            '*|stranger things|B',
        ],
        'note': (
            'Synthesised. Both sides are theme-only (no recognised car brand '
            '→ empty strict), so each emits `*|stranger things|B`; theme-only '
            'keys are ALWAYS broad. Shared broad pair → soft-flag (publishes '
            '+ ping), NOT a silent hard block. B additionally emits '
            '`*|san diego comic-con|B` (not shared). Different products, same '
            'theme.'
        ),
    },
    # Pair 7 — SAME-source near-miss on a broad car-line. Both t-hunted, both
    # Car Culture, different waves that share exactly ONE casting (Subaru
    # Legacy GT) → shared broad pair `subaru legacy gt|car culture|B` →
    # soft-flag, never a hard block.
    {
        'label': 'pair-7-same-source-broad-near-miss',
        'a': {
            'title': 'Novo lote Car Culture: Subaru Legacy GT, Nissan Skyline '
                     'GT-R e Mazda RX-7',
            'subtitle': 'Leva de primavera da série Car Culture',
            'paragraphs': [
                'A Mattel revelou a leva de primavera da série Hot Wheels '
                'Car Culture, com três ícones esportivos.',
                'O destaque é o Subaru Legacy GT em verde metálico, ao lado '
                'de um Nissan Skyline GT-R e de um Mazda RX-7.',
                'Todos trazem Real Riders e base metálica, padrão da linha '
                'Car Culture.',
            ],
            'source_name': 't-hunted',
        },
        'b': {
            'title': 'Novo lote Car Culture: Subaru Legacy GT, Honda Civic '
                     'Type-R e Toyota Supra',
            'subtitle': 'Leva de outono da série Car Culture',
            'paragraphs': [
                'A Mattel revelou a leva de outono da série Hot Wheels Car '
                'Culture, novamente com três esportivos.',
                'O Subaru Legacy GT retorna, agora em azul, acompanhado de um '
                'Honda Civic Type-R e de um Toyota Supra.',
                'A linha Car Culture é uma série recorrente e frequente no '
                'calendário da Mattel.',
            ],
            'source_name': 't-hunted',
        },
        'expected_verdict': 'soft-flag',
        'expected_any_distinctive': False,
        'expected_shared_pairs': [
            'subaru legacy gt|car culture|B',
        ],
        'note': (
            'Synthesised. Same source (t-hunted) both sides, two different '
            'Car Culture waves that share exactly one casting (Subaru Legacy '
            'GT) → one shared BROAD pair `subaru legacy gt|car culture|B` → '
            'soft-flag, not a hard block. The other castings differ '
            '(Nissan Skyline GT-R / Mazda RX-7 vs Honda Civic Type-R / '
            'Toyota Supra) so no other pair is shared.'
        ),
    },
    # Pair 8 — DISTINCT-but-similar. Both are premium collector tie-ins with
    # overlapping surface lexicon (Comic-Con / exclusive / premium / collector)
    # but NO shared (model, series) pair: A = Porsche 911 + K-Pop Demon
    # Hunters, B = a mainline Stranger Things Ford F-150. Different model AND
    # different series → pass. Guards against "any two tie-ins look alike".
    {
        'label': 'pair-8-distinct-but-similar',
        'a': {
            'title': 'Premium Comic-Con exclusive: Porsche 911 for K-Pop Demon Hunters',
            'subtitle': 'A high-end collector tie-in for the animated hit',
            'paragraphs': [
                'This premium San Diego Comic-Con exclusive pairs a Porsche '
                '911 with the K-Pop Demon Hunters franchise, aimed squarely '
                'at collectors.',
                'It ships with event-exclusive premium packaging, Real '
                'Riders, and a numbered certificate.',
                'Only a strictly limited run will be sold at the show.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Mainline Stranger Things Ford F-150 hits shelves',
            'subtitle': 'A premium-looking but basic-range Hawkins tie-in',
            'paragraphs': [
                'The Hot Wheels mainline adds a Stranger Things Ford F-150 in '
                'a Hawkins-inspired livery, a mass-market collector favorite.',
                'Despite the premium-looking graphics it is a standard '
                'basic-range release, widely available at retail.',
                'There is no event exclusivity here — just a regular, widely '
                'available mainline drop.',
            ],
            'source_name': 't-hunted',
        },
        'expected_verdict': 'non-duplicate',
        'expected_any_distinctive': False,
        'expected_shared_pairs': [],
        'note': (
            'Synthesised. Surface-similar premium tie-ins (shared vocabulary: '
            'Comic-Con / exclusive / premium / collector / Hawkins) but no '
            'shared pair: A emits `porsche 911|k-pop demon hunters|D` '
            '(+ `porsche 911|san diego comic-con|D`), B emits '
            '`ford f-150|stranger things|D`. Different model AND series → '
            'pass. Guards against treating any two tie-ins as duplicates.'
        ),
    },
    # Pair 9 — the REAL 2026-07-28 prod false-flag. Two unrelated articles that
    # both mention a broad recurrent line, neither with an extractable model:
    # t-hunted's Lotus Esprit Turbo yields a brand-only token (the case-sensitive
    # `Lotus` pass captures no model) and autoevolution's Lincoln is outside the
    # 36-brand lexicon. Before the theme-only precision fix both degraded to
    # `*|pop culture|B` and soft-flagged each other — note that autoevolution's
    # "pop culture" is ORDINARY PROSE, not a line name. This is the probe that
    # makes the fixture sensitive to that bug class: it is the only pair whose
    # verdict flips when `_theme_only_eligible` is disabled.
    {
        'label': 'pair-9-real-2026-07-28-prose-broad-line',
        'a': {
            'title': 'Mais um novo lote da série Pop Culture de 2026, e com novidade',
            'subtitle': '',
            'paragraphs': [
                'Uma das séries mais colecionadas pelos fãs de Hot Wheels é a '
                'Pop Culture, com suas réplicas de veículos que apareceram em '
                'filmes, séries de TV, desenhos ou jogos, e dessa vez tem um '
                'novo lote com uma novidade: o Lotus Esprit Turbo do 007 com '
                'esquis na traseira.',
                'Você pode ver tudo o que já postamos sobre a série Pop '
                'Culture no link acima.',
            ],
            'source_name': 't-hunted',
        },
        'b': {
            'title': 'First Hot Wheels Super Treasure Hunt for 2027 Is a Lincoln',
            'subtitle': 'The 2027 mainline opens with an unexpected chase',
            'paragraphs': [
                'The Lincoln Continental Mark IV is a pop culture icon that '
                'Hot Wheels has finally cast in Super Treasure Hunt form.',
                'Super Treasure Hunt cars remain the holy grail of the '
                'mainline, hidden roughly one per sealed case.',
            ],
            'source_name': 'autoevolution',
        },
        'expected_verdict': 'non-duplicate',
        'expected_any_distinctive': False,
        'expected_shared_pairs': [],
        'note': (
            'REAL prod pair, 2026-07-28 (t-hunted PT ↔ autoevolution EN). '
            't-hunted side is the real page text; autoevolution side is '
            'synthesised to the shape of the real page (Cloudflare 403 on '
            'fetch), same convention as the SDCC pairs. Completely different '
            'subjects. Both sides have EMPTY `strict`, so before the '
            '2026-07-28 fix both emitted `*|pop culture|B` → shared broad pair '
            '→ [E014] soft-flag. Now broad lines emit no theme-only key, and '
            'autoevolution`s `super treasure hunt` is excluded as a recurring '
            'PROGRAM, so nothing is shared. NOTE: an `expected_any_distinctive'
            '=False` assertion alone does NOT catch this regression — the bug '
            'was a soft flag, not a hard block; `test_calibration_non_dupes_'
            'share_no_pair` is what pins it.'
        ),
    },
]
