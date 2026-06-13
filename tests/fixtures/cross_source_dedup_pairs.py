"""Calibration fixture for ``model_extractor`` cross-source dedup feature.

Eight labelled article-pairs used by ``tests/test_model_extractor.py``:
- ``DUPE_PAIRS`` — 4 pairs that SHOULD be classified as duplicate
  (similarity >= 0.50 → hard-block per tech-spec Decision 4).
- ``NON_DUPE_PAIRS`` — 4 pairs that SHOULD be classified as non-duplicate
  (similarity < 0.30 → pass through cleanly).

Pair-dict shape (per code-research §14.F.1):
    {
        'label': 'pair-1-real-2026-06-03',
        'a': {'title': ..., 'subtitle': ..., 'paragraphs': [...],
              'source_name': 'autoevolution'},
        'b': {'title': ..., 'subtitle': ..., 'paragraphs': [...],
              'source_name': 't-hunted'},
        'expected_verdict': 'duplicate' | 'non-duplicate' | 'soft-flag',
        'expected_overlap_min': 0.50,   # for duplicates
        # OR
        'expected_overlap_max': 0.30,   # for non-duplicates
        'note': 'short rationale',
    }

Bodies are synthesised to be representative of real Hot Wheels source-page
content shape. Brand+model mentions reference real HW castings from public
2026 mainline data; surrounding prose is reconstructed (the real
autoevolution side of Pair 1 was blocked by Cloudflare at user-spec time —
see ``note`` field for explicit annotation).
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# DUPLICATE PAIRS (expect classifier verdict 'duplicate', similarity ≥ 0.50)
# ---------------------------------------------------------------------------

DUPE_PAIRS: list[dict] = [
    # Pair 1 — the load-bearing real 2026-06-03 example.
    # Protected by `test_calibration_real_pair_must_pass` — even if the
    # overall calibration accuracy budget allows 1 misclassification, this
    # specific pair MUST classify as duplicate (Decision 13 split).
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
        'expected_verdict': 'duplicate',
        'expected_overlap_min': 0.50,
        'note': (
            'Real 2026-06-03 pair (autoevolution side synthesised — '
            'Cloudflare blocked real fetch at user-spec time). '
            'Brand+model overlap: Subaru Legacy GT, Land Rover S2, '
            'Toyota 4Runner. Load-bearing example — must-pass per Decision 13.'
        ),
    },
    # Pair 2 — Boulevard mix overlap (100% strict overlap).
    {
        'label': 'pair-2-boulevard-mix',
        'a': {
            'title': 'Hot Wheels Boulevard Mix N reveals — Camaro Z28, '
                     'Mustang Boss, Datsun 510',
            'subtitle': 'Three muscle-and-import classics in this month\'s '
                        'Boulevard wave',
            'paragraphs': [
                'The latest Boulevard Mix N has been revealed, and it leans '
                'heavily into late-70s muscle and tuner culture.',
                'The Chevrolet Camaro Z28 leads the trio in second-gen '
                'split-bumper form, finished in metallic blue with white '
                'racing stripes.',
                'A Ford Mustang Boss 302 follows in plum-purple, paired '
                'with a Datsun 510 in BRE-style livery — a tribute to the '
                'Trans-Am golden era.',
                'All three castings ship with Real Riders rubber tires and '
                'detailed metal bases.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Boulevard preview — Camaro Z28, Mustang Boss, '
                     'Datsun 510 spotted',
            'subtitle': 'Lamley sneak peek at the new Boulevard wave',
            'paragraphs': [
                'A Boulevard sneak peek surfaced this week, confirming the '
                'three headliner castings for the upcoming Mix N.',
                'A Chevrolet Camaro Z28 in metallic blue, a Ford Mustang '
                'Boss 302 in plum, and a Datsun 510 in BRE livery were all '
                'spotted in distributor pre-shipment photos.',
                'All three are confirmed for Real Riders treatment per '
                'Mattel\'s Boulevard convention.',
                'Street date is expected in late Q3 2026.',
            ],
            'source_name': 'lamley',
        },
        'expected_verdict': 'duplicate',
        'expected_overlap_min': 0.80,
        'note': 'Synthesised. 100% strict-jaccard expected — three exact '
                'brand+model matches.',
    },
    # Pair 3 — Pop Culture exotics (100% strict overlap).
    {
        'label': 'pair-3-pop-culture',
        'a': {
            'title': 'New Pop Culture series — Lamborghini Countach, '
                     'Porsche 911, Ferrari Testarossa',
            'subtitle': 'Mattel\'s next Pop Culture wave goes full Miami-Vice',
            'paragraphs': [
                'Mattel announced the next Hot Wheels Pop Culture wave '
                'today, and it is a love letter to 80s exotica.',
                'A Lamborghini Countach in arrest-me red leads the lineup, '
                'paired with a Porsche 911 in Guards Red.',
                'The Ferrari Testarossa rounds out the trio in white with '
                'red strakes — an unmistakable Miami-Vice tribute.',
                'All three feature premium Real Riders and metal-on-metal '
                'construction per the Pop Culture standard.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Pop Culture nova série — Countach, 911 e Testarossa',
            'subtitle': 'Lote de tributo aos anos 80 com três ícones',
            'paragraphs': [
                'A Mattel anunciou hoje a nova série Pop Culture com três '
                'ícones dos anos 80.',
                'O Lamborghini Countach vem em vermelho clássico, '
                'acompanhado do Porsche 911 em Guards Red.',
                'O Ferrari Testarossa fecha o trio em branco com listras '
                'vermelhas — um tributo claro à série Miami Vice.',
                'Os três trazem Real Riders e construção metal-metal '
                'padrão Pop Culture.',
            ],
            'source_name': 't-hunted',
        },
        'expected_verdict': 'duplicate',
        'expected_overlap_min': 0.80,
        'note': 'Synthesised. 100% strict-jaccard expected.',
    },
    # Pair 4 — JDM Premium partial overlap (edge case at 50% boundary).
    {
        'label': 'pair-4-jdm-premium-partial',
        'a': {
            'title': 'JDM Premium reveal — Nissan Skyline GT-R, Toyota '
                     'Supra, Mazda RX-7, Subaru WRX',
            'subtitle': 'Four-car JDM Premium assortment confirmed',
            'paragraphs': [
                'The next Hot Wheels JDM Premium assortment will include '
                'four headliner castings, Mattel confirmed today.',
                'A Nissan Skyline GT-R R34 in Bayside Blue leads the wave, '
                'paired with a Toyota Supra A80 in white pearl.',
                'A Mazda RX-7 FD in Chaste White rounds out the rotary '
                'representation, with a Subaru WRX STI completing the '
                'all-wheel-drive trio.',
                'All four feature premium Real Riders and detailed '
                'engine bays.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'JDM heavy hitters — Skyline GT-R and Supra spotted '
                     'in Premium',
            'subtitle': 'Two of the four upcoming JDM Premium castings '
                        'leaked',
            'paragraphs': [
                'Two of the upcoming JDM Premium castings have been spotted '
                'in distributor pre-shipment photos.',
                'A Nissan Skyline GT-R in Bayside Blue and a Toyota Supra '
                'A80 in white pearl are both confirmed for the assortment.',
                'Lamley sources suggest the remaining two slots will lean '
                'toward less common JDM marques, but no further details '
                'have surfaced yet.',
                'Street date is rumoured for late 2026.',
            ],
            'source_name': 'lamley',
        },
        'expected_verdict': 'duplicate',
        'expected_overlap_min': 0.50,
        'note': 'Edge-case at hard-block boundary. A has 4 strict tokens, '
                'B has 2 (Nissan Skyline, Toyota Supra) → jaccard = 2/4 = '
                '0.50 exactly.',
    },
]


# ---------------------------------------------------------------------------
# NON-DUPLICATE PAIRS (expect verdict 'non-duplicate', similarity < 0.30)
# ---------------------------------------------------------------------------

NON_DUPE_PAIRS: list[dict] = [
    # Pair 5 — AC8 probe: same brand, different model, single-token each side.
    # Without the AC8 1-token guard, brand-jaccard would falsely return 1.0.
    {
        'label': 'pair-5-ac8-same-brand-different-model',
        'a': {
            'title': 'New Subaru BRZ casting in Boulevard mix',
            'subtitle': 'A clean rear-drive coupe joins the Boulevard wave',
            'paragraphs': [
                'The Boulevard Mix gains a Subaru BRZ in metallic silver '
                'this month, completing the small-displacement-tuner '
                'representation in the wave.',
                'The casting features the second-generation body with '
                'TRD-style ducktail.',
                'Real Riders and a detailed metal base are standard for '
                'the Boulevard line.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Lamley reviews the Subaru WRX premium release',
            'subtitle': 'A long-overdue WRX in Premium gold',
            'paragraphs': [
                'The Premium Subaru WRX STI hit shelves this week, and '
                'Lamley has the in-hand review.',
                'Painted in World Rally Blue with gold BBS-style wheels, '
                'the casting nails the iconic rally aesthetic.',
                'Premium Real Riders and a detailed interior round out '
                'a strong release.',
            ],
            'source_name': 'lamley',
        },
        'expected_verdict': 'non-duplicate',
        'expected_overlap_max': 0.30,
        'note': 'AC8 probe — single brand+model each side. Strict jaccard '
                '= 0.0 (BRZ vs WRX), AC8 1-token guard blocks brand '
                'fallback. Final: 0.0.',
    },
    # Pair 6 — 1-token strict guard probe (single Toyota each).
    {
        'label': 'pair-6-1-token-guard',
        'a': {
            'title': 'Quick look: 2018 Toyota 4Runner premium release',
            'subtitle': 'The 4Runner gets the Premium treatment',
            'paragraphs': [
                'A 2018 Toyota 4Runner in Army Green hits Premium shelves '
                'next month, complete with roof rack and recovery boards.',
                'The casting marks the first 4Runner Premium release '
                'since 2021.',
                'Real Riders and a detailed undercarriage are standard.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Toyota Supra Premium variant announcement',
            'subtitle': 'A85 Supra confirmed for Premium',
            'paragraphs': [
                'The Toyota Supra A85 will receive a Premium release later '
                'this year, Mattel confirmed in a developer-stream Q&A.',
                'The casting will feature gloss black paint with red '
                'accents, paired with TE37-style wheels.',
                'Standard Premium Real Riders and detailed engine bay '
                'are confirmed.',
            ],
            'source_name': 'lamley',
        },
        'expected_verdict': 'non-duplicate',
        'expected_overlap_max': 0.30,
        'note': 'AC8 1-token strict guard probe. Brand match (Toyota) but '
                'models differ (4Runner vs Supra) → strict = 0.0, brand '
                'fallback blocked by 1-token guard. Final: 0.0.',
    },
    # Pair 7 — Different series, no overlap.
    {
        'label': 'pair-7-different-series',
        'a': {
            'title': 'Boulevard Mix N — Camaro Z28, Mustang Boss, Datsun 510',
            'subtitle': 'Muscle and import classics in this month\'s Boulevard',
            'paragraphs': [
                'The Boulevard Mix N drops next week with three muscle-'
                'and-import classics.',
                'A Chevrolet Camaro Z28 in metallic blue, a Ford Mustang '
                'Boss 302 in plum, and a Datsun 510 in BRE livery headline '
                'the wave.',
                'Real Riders and metal bases standard.',
                'Street date confirmed for the 20th.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Premium Q3 lineup announced — Testarossa, 911, Countach',
            'subtitle': 'Three 80s exotics confirmed for Premium Q3',
            'paragraphs': [
                'Mattel confirmed the Q3 Premium lineup at a recent '
                'collector event.',
                'A Ferrari Testarossa, Porsche 911 Carrera, and '
                'Lamborghini Countach LP500 will headline the assortment.',
                'All three feature premium Real Riders and detailed '
                'metal-metal construction.',
                'Expected street date is late September.',
            ],
            'source_name': 'lamley',
        },
        'expected_verdict': 'non-duplicate',
        'expected_overlap_max': 0.30,
        'note': 'Different series, no brand or model overlap. Expected ~0.0.',
    },
    # Pair 8 — AC6 empty-fp probe: industry news vs car review.
    {
        'label': 'pair-8-empty-fp',
        'a': {
            'title': 'Mattel Q3 earnings exceed expectations',
            'subtitle': 'Strong holiday-season pre-orders drive the beat',
            'paragraphs': [
                'Mattel reported Q3 earnings well above analyst '
                'expectations this morning, driven by strong holiday-'
                'season pre-order volume.',
                'Revenue rose 12% year-over-year, with operating margins '
                'expanding 80 basis points.',
                'Management raised full-year guidance and announced an '
                'expanded buyback authorisation.',
                'Shares jumped 6% in after-hours trading.',
            ],
            'source_name': 'autoevolution',
        },
        'b': {
            'title': 'Hands-on with the Nissan GT-R Premium',
            'subtitle': 'A close look at the new GT-R casting',
            'paragraphs': [
                'The new Nissan GT-R Premium hit shelves this week, and '
                'we have it in hand for a close-up review.',
                'Painted in Bayside Blue with gold BBS-style wheels, the '
                'casting captures the R34 silhouette beautifully.',
                'Premium Real Riders and a detailed engine bay round out '
                'a strong release.',
            ],
            'source_name': 'lamley',
        },
        'expected_verdict': 'non-duplicate',
        'expected_overlap_max': 0.30,
        'note': 'AC6 empty-fp probe. A has no recognised brand+model '
                '(industry news), B has Nissan GT-R. Similarity = 0.0 '
                'via AC6 short-circuit.',
    },
]
