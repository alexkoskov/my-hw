#!/usr/bin/env python3
"""UI-boilerplate filter for source-parser paragraph lists.

Source pages from autoevolution / lamley / Mattel embed social-share
widgets ("Share on Facebook", "Tweet", "Subscribe", ...) that bleed
through to the article body when we walk ``<p>`` / ``<li>`` tags. These
short labels:

- waste Google Translate calls in the auto-fallback path,
- pollute the manual-review screen so Claude/operator must mentally skip
  them, and
- end up on the published Telegraph page as broken UI leftovers.

This module exposes two helpers each parser calls just before returning:

- ``is_boilerplate(text)`` — single-paragraph predicate.
- ``filter_boilerplate(paragraphs)`` — drop boilerplate, preserve order.

Patterns are length-bounded (<= ``_MAX_BOILERPLATE_LEN`` chars) so a long
sentence that happens to mention "Share on Facebook" inline is kept as
real content. English patterns are primary (sources are English);
Russian patterns are a belt-and-suspenders safety net in case translated
text ever flows through this filter.
"""

from __future__ import annotations

import re
from typing import Iterable, List

# Length threshold: only filter "short" paragraphs (boilerplate is usually a
# label / button text). A long paragraph that happens to mention "Share on
# Facebook" inline is real content — keep it.
# Bumped 80 → 120 in the author-plug-filter feature so longer parenthesised
# plugs like "(follow me on Instagram for the latest reveals @diecast215)"
# (~80–110 chars) fit under the threshold.
_MAX_BOILERPLATE_LEN = 120

# Long-form boilerplate patterns — bypass the length cap above. Each
# pattern must be strictly ``^``-anchored on a phrase that is vanishingly
# unlikely to start a real news-prose paragraph, and free of nested
# greedy quantifiers (ReDoS-safe even on uncapped input). Used for
# multi-sentence promotional outros (CTA + social-media plug + bot name)
# that routinely run 200–400 chars and would otherwise sail past the
# 120-char threshold.
#
# t-hunted ships the same Portuguese outro on EVERY published article:
#   "Saiba mais sobre a série <NAME> e veja mais fotos neste link.
#    Para ver mais novidades todos os dias fique ligado aqui no T-Hunted,
#    curta nossa página no Facebook, siga o nosso Instagram e se inscreva
#    no nosso canal no YouTube!"
# (~274 chars). Without long-form filtering this entire CTA was reaching
# Telegraph after the LLM translated it to Russian (incident 2026-06-02).
_LONG_BOILERPLATE_PATTERNS = [
    # PT — "Saiba mais sobre ..." opener. Distinctive: legitimate news
    # prose rarely opens a paragraph with this phrase. ``\b`` ensures we
    # do not accidentally catch "saibamais..." style typos.
    re.compile(r'^saiba\s+mais\s+sobre\b', re.I),
    # PT — "Para ver mais novidades ..." defence in depth: catches the
    # outro's second sentence if the parser ever splits the CTA across
    # two paragraphs.
    re.compile(r'^para\s+ver\s+mais\s+novidades\b', re.I),
    # RU — defence in depth in case a future PT outro variant slips
    # through and ends up translated. ``Узнать больше о ...`` is the
    # canonical RU equivalent of "Saiba mais sobre ...".
    re.compile(r'^узнать\s+больше\s+о\b', re.I),
    # PT — "Clique aqui / Clique neste link para ver tudo ..." per-article
    # cross-promo CTA. t-hunted ships these "click here to see everything
    # we covered about <topic>" links inline (incident 2026-06-13: the
    # Ferrari F1 Premium post leaked two of them to Telegram — the existing
    # "para ver mais novidades" pattern only caught the third, social plug).
    re.compile(r'^clique\s+(aqui|neste\s+link|no\s+link)\b', re.I),
    # RU — defence in depth for the translated "Кликайте по ссылке ..." /
    # "Кликай сюда ..." form, if a PT click-CTA variant ever slips past the
    # pre-translation filter.
    re.compile(r'^кликай(те)?\s+(по|на|сюда)\b', re.I),

    # --- Store self-promo outro (incident 2026-07-29) ---------------------
    # t-hunted appends its own shop's advertisement to ordinary articles:
    #   "Na Universo Hot Wheels, você encontra modelos Mainline, Premium,
    #    Treasure Hunts, Super-T ..."            (~150 chars)
    #   "A Universo Hot Wheels é a maior loja especializada da América
    #    Latina, oferecendo milhares de produtos ..."   (~164 chars)
    # Both sail past the 120-char cap, and `_is_promo_article` ([E035]) does
    # not catch them either — that gate is all-or-nothing and correctly did
    # NOT drop the article, which is genuine editorial content with an ad
    # TAIL bolted on. Nothing removed the tail, so it was translated and
    # published.
    #
    # Anchored on the shop NAME **plus** a selling verb, never on the name
    # alone: t-hunted is affiliated with the shop and may legitimately report
    # on it ("A Universo Hot Wheels anunciou uma parceria..."), which must
    # survive. `[^.]{0,80}` is bounded — no nested quantifiers, ReDoS-safe on
    # uncapped input as this list requires.
    #
    # A second shop would need its own pair of patterns; this is deliberately
    # a named-source rule, like the "Saiba mais sobre" outro above.
    re.compile(
        r'^n?a\s+universo\s+hot\s+wheels\b[^.]{0,80}\b'
        r'(voc[eê]\s+(encontra|acha|vai\s+encontrar)|encontre)',
        re.I,
    ),
    re.compile(
        r'^a\s+universo\s+hot\s+wheels\s+[eé]\s+a\s+maior\s+loja\b',
        re.I,
    ),
    # RU — defence in depth, in case a PT wording variant slips past the
    # pre-translation pass. Leading `\*{0,2}` because the LLM bolds the shop
    # name (`**Universo Hot Wheels** — крупнейший ...`) and this filter runs
    # BEFORE the renderer decodes those markers.
    re.compile(
        r'^\*{0,2}в\s+\*{0,2}universo\s+hot\s+wheels\b[^.]{0,80}\b'
        r'найд[её]те',
        re.I,
    ),
    re.compile(
        r'^\*{0,2}universo\s+hot\s+wheels\*{0,2}\s*[—–-]\s*крупнейш',
        re.I,
    ),
]

# Platforms covered by author-plug patterns (variant A and B of the
# author-plug-filter feature). Single tuple keeps the alternation in sync
# across patterns. Threads / OnlyFans intentionally out of scope (rare in HW
# articles).
_PLUG_PLATFORMS = (
    'instagram', 'twitter', 'x', 'tiktok', 'youtube',
    'facebook', 'reddit', 'patreon', 'discord', 'linktree',
)
_PLATFORMS_RE = '|'.join(_PLUG_PLATFORMS)

# Each pattern is matched against the WHOLE stripped paragraph,
# case-insensitive. Match → drop the paragraph.
_BOILERPLATE_PATTERNS = [
    # English — primary, since all three source sites are English-language.
    re.compile(
        r'^share\s+(on|via|to)\s+'
        r'(facebook|twitter|x|linkedin|pinterest|whatsapp|telegram|email|reddit)\b',
        re.I,
    ),
    re.compile(r'^(tweet|pin it|pin on pinterest)$', re.I),
    re.compile(r'^email this( article)?$', re.I),
    re.compile(r'^copy (link|url|article url)$', re.I),
    re.compile(r'^subscribe( to (our )?newsletter)?$', re.I),
    re.compile(r'^(related (articles?|posts?)|see also|you may also like)$', re.I),
    re.compile(r'^read more[:\s]*$', re.I),
    re.compile(r'^(tags?|filed under|categories?):', re.I),
    re.compile(r'^comments?$', re.I),
    # ------------------------------------------------------------------
    # Author social-media plugs (author-plug-filter feature, variant A).
    # Drops standalone-paragraph plugs at every source parser. Inline
    # plugs embedded inside larger paragraphs are handled by variant B
    # (post-LLM) in author_plug_filter.py.
    # ------------------------------------------------------------------
    # A1 — umbrella: "follow|check|subscribe to me/us on <platform>".
    # Replaces and supersedes the legacy "^follow us on \w+" pattern.
    re.compile(
        r'^(follow|check|subscribe\s+to)\s+(me|us)\s+on\s+(' + _PLATFORMS_RE + r')\b',
        re.I,
    ),
    # A2 — parenthesised umbrella with mandatory @handle.
    # Catches the canonical leak shape "(follow me on Instagram @diecast215)".
    re.compile(
        r'^\(\s*(follow|check|subscribe\s+to|join)\s+(me|us)\s+on\s+'
        r'(' + _PLATFORMS_RE + r')\s+@\w{2,30}\s*\)$',
        re.I,
    ),
    # A3 — "Platform: handle" shape, with or without @.
    re.compile(
        r'^(' + _PLATFORMS_RE + r')\s*:\s*@?[\w./_-]+\s*$',
        re.I,
    ),
    # A4 — orphan handle on its own line (multi-line plug fallback).
    re.compile(r'^@\w{2,30}$', re.I),
    # A5 — "subscribe to my <feed>" (author form, distinct from the "our"
    # newsletter pattern above).
    re.compile(
        r'^subscribe\s+to\s+my\s+'
        r'(channel|newsletter|patreon|youtube|page|feed)\b',
        re.I,
    ),
    # ------------------------------------------------------------------
    # Affiliate / "Quick Link" promo lines (orangetrack-source feature,
    # Decision 12). Standalone short paragraphs only — length is bounded
    # by ``_MAX_BOILERPLATE_LEN`` (120). All anchored at ``^``, no nested
    # greedy quantifiers (ReDoS-safe).
    # ------------------------------------------------------------------
    # Aff1 — "*QUICK LINK!*" / "Quick link:" affiliate header. Verb-gate
    # dropped 2026-05-08 after a paragraph starting "*QUICK LINK!* Find ..."
    # slipped through (no buy/order/grab/shop verb, but Brad's affiliate
    # markers still produced a clear ad line in the channel). The prefix
    # is distinctive enough on its own — false positives on legitimate
    # prose using "Quick link!" / "Quick link:" as a sentence opener are
    # vanishingly rare in news-article body content (length-bounded at 120 chars).
    re.compile(
        r'^\s*\*?quick\s+link[!:]',
        re.I,
    ),
    # Aff2 — "Buy [now] from <store>" shape. Requires a non-space token
    # after "from" (so a generic short paragraph "Buy from us." also
    # matches; that's intentional — these are short standalone lines).
    re.compile(
        r'^buy\s+(now\s+)?from\s+\S',
        re.I,
    ),
    # Aff3 — "Find ... on eBay" / "Find ... at <store>" — direct affiliate
    # call-to-action without the QUICK LINK prefix. Length-bounded by
    # _MAX_BOILERPLATE_LEN (120); false positives ("find ... on the map")
    # are unlikely in standalone short paragraphs.
    re.compile(
        r'^\s*\*?find\b.*\b(on|at)\s+(ebay|amazon|aliexpress|mattel|walmart)\b',
        re.I,
    ),
    # ------------------------------------------------------------------
    # Russian — defence in depth in case translated text ever reaches us.
    re.compile(
        r'^поделит(ь(ся)?|есь)\s+(на|в|через)\s+'
        r'(facebook|twitter|x|вконтакте|whatsapp|telegram|email)\b',
        re.I,
    ),
    re.compile(r'^твитнуть$', re.I),
    re.compile(r'^подписать(ся|есь)( на (нашу )?рассылку)?$', re.I),
    re.compile(r'^(читайте|смотрите) (также|далее)$', re.I),
    re.compile(r'^(тэги|теги|категории|метки):', re.I),
    re.compile(r'^комментари(и|й)$', re.I),
    # RU affiliate / "Quick Link" — defense-in-depth catch on the post-LLM
    # side in case any EN affiliate variant slips through Aff1/Aff3 above.
    # Russian translation of "QUICK LINK!" varies (быстрая ссылка / быстрая
    # ссылочка / etc.); we anchor on the most stable forms. Added 2026-05-08
    # after "*QUICK LINK!* Find ... on eBay" → «БЫСТРАЯ ССЫЛКА! Найти ...
    # на eBay» reached the channel.
    re.compile(
        r'^\s*\*?быстр(ая|ой)\s+ссылк(а|у|и)[!:]',
        re.I,
    ),
    # RU "Найти ... на eBay" / "Купить ... на Amazon" — direct CTA shape.
    re.compile(
        r'^\s*(найти|купить)\b.*\bна\s+(ebay|amazon|aliexpress|mattel|walmart)\b',
        re.I,
    ),
    # ------------------------------------------------------------------
    # Portuguese — defence in depth for t-hunted.blogspot.com.
    # Blogger PT-locale template emits short standalone labels for
    # share widgets, navigation links and comment forms that bleed into
    # the paragraph stream. All patterns are ^-anchored, ReDoS-safe (no
    # nested greedy quantifiers), and bounded by ``_MAX_BOILERPLATE_LEN``
    # so inline mentions inside real prose are preserved.
    # ------------------------------------------------------------------
    # "Compartilhar no Facebook" / "Compartilhar via WhatsApp" etc.
    re.compile(
        r'^compartilhar\s+(no|em|via|por)\s+'
        r'(facebook|twitter|x|whatsapp|telegram|email)\b',
        re.I,
    ),
    # "Marcadores: hot wheels, mattel" — Blogger labels footer.
    re.compile(r'^marcadores\s*:', re.I),
    # "Postado por <author>" — Blogger byline.
    re.compile(r'^postado\s+por\b', re.I),
    # Bare "Postagem" label (exact-match — avoids FP on prose
    # «Postagem nova foi publicada»).
    re.compile(r'^postagem$', re.I),
    # "Enviar por email" — PT "Email this".
    re.compile(r'^enviar\s+por\s+email\b', re.I),
    # "Postagens mais antigas" / "Postagens mais recentes" — older/newer
    # post navigation links.
    re.compile(r'^postagens\s+mais\s+(antigas|recentes)$', re.I),
    # "Assinar: Postagens (Atom)" — feed-subscribe link.
    re.compile(r'^assinar\s*:', re.I),
    # "Leia mais" — read-more link (anchored as standalone label, not
    # part of inline prose). Only «leia mais» is included; «ler mais»
    # was excluded due to higher FP risk per code-research §4.D.
    re.compile(r'^leia\s+mais$', re.I),
    # "Postar um comentário" / "Postar comentário" — Blogger comment
    # form trigger. ``[áa]`` covers NFC-normalised UTF-8 plus an ASCII
    # fallback in case the feedparser/encoding pipeline strips diacritics.
    re.compile(r'^postar\s+(um\s+)?coment[áa]rio$', re.I),
    # "Página inicial" / "Página principal" home link. ``p[aá]gina``
    # handles both UTF-8 and ASCII fallbacks.
    re.compile(r'^p[aá]gina\s+(inicial|principal)$', re.I),
]


def is_boilerplate(text: str) -> bool:
    """Return True if *text* is a short standalone UI label that should be stripped.

    Empty / whitespace-only strings return False — they are not boilerplate
    in the UI-leftover sense; callers usually drop or keep them by their
    own logic. Anything longer than ``_MAX_BOILERPLATE_LEN`` is treated as
    real prose even if a trigger phrase appears at the start.
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    # Long-form patterns first — these intentionally bypass the
    # ``_MAX_BOILERPLATE_LEN`` cap because they target multi-sentence
    # promotional outros (~200-400 chars). Strict ``^`` anchors + no
    # nested quantifiers keep them ReDoS-safe even on uncapped input.
    for pat in _LONG_BOILERPLATE_PATTERNS:
        if pat.search(s):
            return True
    # Short-form patterns — length-bounded to preserve real prose that
    # happens to mention a trigger phrase inline.
    if len(s) > _MAX_BOILERPLATE_LEN:
        return False
    for pat in _BOILERPLATE_PATTERNS:
        if pat.search(s):
            return True
    return False


def filter_boilerplate(paragraphs: Iterable[str]) -> List[str]:
    """Drop paragraphs identified as boilerplate. Preserve order of the rest."""
    return [p for p in paragraphs if not is_boilerplate(p)]


def filter_blocks(blocks: Iterable[dict]) -> List[dict]:
    """Drop content blocks whose ``text`` / ``caption`` are pure UI
    boilerplate (ads, social-share, "Subscribe" labels). Mirror of
    ``filter_boilerplate`` but for the structured-block representation
    used by autoevolution articles (lead / paragraph / image / video).

    Decision rules per block:

    * Non-dict / empty entries → drop.
    * Block with media (``src`` or ``image_url`` set) → KEEP regardless
      of caption text. We never want to lose visual content; a short
      caption like "1995 Honda NSX" must not look like boilerplate.
      Only drop a media block if its caption matches a boilerplate
      pattern AND ``text`` is empty (rare — represents an ad slot
      that happened to ship with a placeholder image).
    * Pure-text block (no media, just ``text``) → drop if
      ``is_boilerplate(text)`` matches. Same rule as
      ``filter_boilerplate`` for paragraph strings.
    * Anything else → keep.

    Order preserved.
    """
    out: List[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = (block.get("text") or "").strip()
        caption = (block.get("caption") or "").strip()
        has_media = bool(block.get("src") or block.get("image_url"))

        if has_media:
            # Drop only if caption is junk AND there's no other text.
            if caption and is_boilerplate(caption) and not text:
                continue
            out.append(block)
            continue

        # Pure-text block — drop if text is boilerplate (or empty AND
        # caption is boilerplate, though that combination is rare).
        if text and is_boilerplate(text):
            continue
        if not text and caption and is_boilerplate(caption):
            continue
        out.append(block)
    return out
