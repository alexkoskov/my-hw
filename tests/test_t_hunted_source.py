#!/usr/bin/env python3
"""Unit tests for t_hunted_source.fetch_t_hunted_article.

t-hunted.blogspot.com is a Blogger-hosted Portuguese-language Hot Wheels
news source. Mirrors the lamley test structure minus WAF/throttle classes
(Blogger has no Cloudflare-style bot management at our volume).

Task 2 (admin_alerts E031-E033) is built in parallel — these tests
monkey-patch the three new builder names onto ``admin_alerts`` so this
file is self-contained and passes locally before Task 2 merges.
"""

from unittest.mock import MagicMock

import pytest
import requests

import admin_alerts
import t_hunted_source


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

#: Self-contained alert-builder stubs. Task 2 will replace these with real
#: builders on ``admin_alerts``; until then the parser calls would raise
#: ``AttributeError`` without this fixture.
@pytest.fixture(autouse=True)
def _stub_t_hunted_alerts(monkeypatch):
    monkeypatch.setattr(
        admin_alerts,
        "alert_t_hunted_host_rejected",
        lambda link: "[E031-STUB] host_rejected: " + link,
        raising=False,
    )
    monkeypatch.setattr(
        admin_alerts,
        "alert_t_hunted_fetch_error",
        lambda link, error: "[E032-STUB] fetch_error: " + link + " :: " + str(error),
        raising=False,
    )
    monkeypatch.setattr(
        admin_alerts,
        "alert_t_hunted_no_body",
        lambda link: "[E033-STUB] no_body: " + link,
        raising=False,
    )
    yield


def _make_response(text="", status=200, raise_exc=None, content=None, headers=None):
    """Replica of ``tests/test_lamley_source.py:37`` ``_make_response``."""
    resp = MagicMock(spec=requests.Response)
    resp.text = text
    resp.status_code = status
    resp.content = content if content is not None else text.encode("utf-8")
    resp.headers = headers if headers is not None else {}
    if raise_exc is not None:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    return resp


#: Reference Blogger article shape from code-research §C.1. PT-language
#: «Caça ao tesouro Pop Culture 2026» with two real content paragraphs,
#: two size variants of one image (=s1600 + =s640), one unique second
#: image (=s1600), and two trailing boilerplate paragraphs. PT footers
#: are NOT filtered by ``filter_boilerplate`` until Task 4 lands the PT
#: patterns — so these tests assert behaviour assuming the EN-only
#: filter is in place.
SAMPLE_HTML = """<html><body>
<h3 class="post-title entry-title">Caça ao tesouro Pop Culture 2026</h3>
<div class="post-body entry-content">
  <p>Mais um lançamento que vai agitar a galera dos colecionadores.</p>
  <p>Os modelos chegam em packs de seis.</p>
  <img src="https://blogger.googleusercontent.com/img/abc/=s1600/photo1.jpg" />
  <img src="https://blogger.googleusercontent.com/img/abc/=s640/photo1.jpg" />
  <img src="https://blogger.googleusercontent.com/img/def/=s1600/photo2.jpg" />
  <p>Compartilhar no Facebook</p>
  <p>Marcadores: Pop Culture, 2026</p>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# TestFetchTHuntedArticle (6 methods)
# ---------------------------------------------------------------------------


class TestFetchTHuntedArticle:
    def test_parses_title_subtitle_paragraphs_images(self):
        session = MagicMock()
        session.get.return_value = _make_response(text=SAMPLE_HTML)

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
        )

        assert out is not None
        assert out["title"] == "Caça ao tesouro Pop Culture 2026"
        # First body paragraph after boilerplate filter becomes subtitle.
        assert out["subtitle"] == (
            "Mais um lançamento que vai agitar a galera dos colecionadores."
        )
        # Subtitle is NOT duplicated inside paragraphs.
        assert (
            "Mais um lançamento que vai agitar a galera dos colecionadores."
            not in out["paragraphs"]
        )
        # Second real paragraph stays in body.
        assert "Os modelos chegam em packs de seis." in out["paragraphs"]
        # Image dedup: =s1600 and =s640 of photo1 collapse to one entry;
        # photo2 stays. First-seen src is kept.
        assert out["images"] == [
            "https://blogger.googleusercontent.com/img/abc/=s1600/photo1.jpg",
            "https://blogger.googleusercontent.com/img/def/=s1600/photo2.jpg",
        ]

    def test_returns_none_on_http_error(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("boom")
        notifier = MagicMock()

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
            notifier=notifier,
        )

        assert out is None
        notifier.assert_called_once()
        # Notifier receives a string payload built by the FETCH-error
        # builder specifically — fingerprint pins builder identity so a
        # regression that routes through E031/E033 fails this assertion.
        msg = notifier.call_args.args[0]
        assert isinstance(msg, str)
        assert "[E032-STUB]" in msg

    def test_returns_none_on_missing_body(self):
        session = MagicMock()
        # Only a bare <h1> — no post-body / entry-content / <article> wrapper.
        session.get.return_value = _make_response(
            text="<html><body><h1>Only title</h1></body></html>"
        )
        notifier = MagicMock()

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
            notifier=notifier,
        )

        assert out is None
        notifier.assert_called_once()
        # Fingerprint pins this to the no-body builder — distinguishes
        # from the structurally-similar fetch-error path (both reach
        # this branch with HTTP success / parser failure).
        msg = notifier.call_args.args[0]
        assert "[E033-STUB]" in msg

    def test_image_limit_enforced(self):
        # 35 unique image paths > _IMAGE_LIMIT (30) so the break-clause
        # in the image collection loop is exercised. Each ``/img/{i}/``
        # segment differs so the size-suffix dedup leaves all 35 distinct.
        imgs = "\n".join(
            f'<img src="https://blogger.googleusercontent.com/img/{i}/=s1600/x.jpg" />'
            for i in range(35)
        )
        html = (
            f'<html><body><h3 class="post-title">T</h3>'
            f'<div class="post-body entry-content"><p>Lead.</p>{imgs}</div>'
            f'</body></html>'
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
        )

        assert out is not None
        assert len(out["images"]) == t_hunted_source._IMAGE_LIMIT

    def test_image_dedup_strips_blogger_size_suffix(self):
        # Three images: photo1 in two sizes (=s1600, =s640), photo2 with
        # the -c (cropped) variant. Dedup should collapse photo1's two
        # size variants and treat photo2 separately.
        html = (
            '<html><body><h3 class="post-title">T</h3>'
            '<div class="post-body entry-content">'
            '<p>Lead.</p>'
            '<img src="https://blogger.googleusercontent.com/img/abc/=s1600/photo1.jpg" />'
            '<img src="https://blogger.googleusercontent.com/img/abc/=s640/photo1.jpg" />'
            '<img src="https://blogger.googleusercontent.com/img/xyz/=s320-c/photo2.jpg" />'
            '</div></body></html>'
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
        )

        assert out is not None
        # photo1 dedup: only the first-seen (=s1600) survives.
        # photo2 with -c suffix is kept as a separate image.
        assert out["images"] == [
            "https://blogger.googleusercontent.com/img/abc/=s1600/photo1.jpg",
            "https://blogger.googleusercontent.com/img/xyz/=s320-c/photo2.jpg",
        ]

    def test_blogger_lightbox_lifts_full_size_from_parent_anchor(self):
        # Blogger renders article images as a lightbox sandwich:
        #   <a href=".../s1200/photo.jpg"><img src=".../w200-h200/photo.jpg" /></a>
        # ``img.src`` is the 200×200 grid thumbnail; the wrapping ``<a href>``
        # carries the full-resolution variant. Telegraph embeds the src URL
        # verbatim (no re-hosting), so without this lift subscribers see
        # 200×200 miniatures instead of full photos. This test pins the lift.
        full = "https://blogger.googleusercontent.com/img/AAAAAAA/s1200/p1.jpg"
        thumb = "https://blogger.googleusercontent.com/img/AAAAAAA/w200-h200/p1.jpg"
        html = (
            '<html><body><h3 class="post-title">Title</h3>'
            '<div class="post-body entry-content">'
            '<p>Para 1.</p>'
            '<p>Para 2.</p>'
            f'<a href="{full}"><img src="{thumb}" /></a>'
            '</div></body></html>'
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
        )

        assert out is not None
        # Lift wins: full-size URL ends up in the images list, NOT the thumb.
        assert out["images"] == [full]

    def test_blogger_lightbox_lift_skipped_for_non_blogger_href(self):
        # Defensive: if the wrapping ``<a href>`` points to a non-Blogger
        # URL (e.g. an off-site link), fall back to ``img.src`` so we don't
        # silently leak a hot-link or external URL into the gallery.
        external = "https://example.com/click-tracker?u=blogger.googleusercontent.com"
        thumb = "https://blogger.googleusercontent.com/img/X/s1600/p.jpg"
        html = (
            '<html><body><h3 class="post-title">T</h3>'
            '<div class="post-body entry-content">'
            '<p>Para 1.</p>'
            '<p>Para 2.</p>'
            f'<a href="{external}"><img src="{thumb}" /></a>'
            '</div></body></html>'
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
        )

        assert out is not None
        # External anchor href is not lifted; we keep the Blogger thumb URL.
        assert out["images"] == [thumb]

    def test_single_paragraph_post_keeps_paragraph_in_body_with_empty_subtitle(self):
        # T-hunted photo-gallery posts (new-arrival announcements) typically
        # have one intro paragraph followed by a product photo gallery.
        # Before the conditional-lift fix, the parser lifted the one
        # paragraph into ``subtitle``, leaving ``paragraphs=[]`` which
        # caused news_bot.fetch_full_article to silently skip the post.
        # Verify the lift is skipped when fewer than 2 paragraphs survive
        # boilerplate filtering — the one paragraph stays in body, subtitle
        # is empty, and news_bot will publish the post.
        html = (
            '<html><body><h3 class="post-title">Novo lançamento</h3>'
            '<div class="post-body entry-content">'
            '<p>A loja Universo Hot Wheels recebeu mais um set incrível.</p>'
            '<img src="https://blogger.googleusercontent.com/img/a/=s1600/p1.jpg" />'
            '<img src="https://blogger.googleusercontent.com/img/b/=s1600/p2.jpg" />'
            '<img src="https://blogger.googleusercontent.com/img/c/=s1600/p3.jpg" />'
            '</div></body></html>'
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
        )

        assert out is not None
        # Subtitle stays empty — no lift on single-paragraph post.
        assert out["subtitle"] == ""
        # The intro paragraph stays in the body.
        assert out["paragraphs"] == [
            "A loja Universo Hot Wheels recebeu mais um set incrível."
        ]
        # Image gallery still collected — confirms photo posts ship with
        # their visual payload intact.
        assert len(out["images"]) == 3

    def test_boilerplate_filter_applied_before_subtitle_lift(self):
        # First "paragraph" after title is EN boilerplate ("Share on
        # Facebook") that ``filter_boilerplate`` strips today. If filter
        # ran AFTER subtitle lift, that share-line would become the
        # subtitle. Real lead must end up in ``subtitle`` instead.
        html = (
            '<html><body><h3 class="post-title">Title here</h3>'
            '<div class="post-body entry-content">'
            '<p>Share on Facebook</p>'
            '<p>Real article lead paragraph.</p>'
            '<p>Second content paragraph.</p>'
            '</div></body></html>'
        )
        session = MagicMock()
        session.get.return_value = _make_response(text=html)

        out = t_hunted_source.fetch_t_hunted_article(
            "https://t-hunted.blogspot.com/2026/05/post.html",
            session=session,
        )

        assert out is not None
        assert out["subtitle"] == "Real article lead paragraph."
        assert "Share on Facebook" not in out["subtitle"]
        assert "Share on Facebook" not in out["paragraphs"]
        # Exact-order assertion: only the second content paragraph
        # survives; any extra entry or reordering by filter/lift fails.
        assert out["paragraphs"] == ["Second content paragraph."]


# ---------------------------------------------------------------------------
# TestHostAllowlist (8 methods)
# ---------------------------------------------------------------------------


class TestHostAllowlist:
    """SSRF allowlist: exact-match on ``t-hunted.blogspot.com`` only."""

    def test_allows_t_hunted_blogspot_com(self):
        assert t_hunted_source._is_allowed_t_hunted_url(
            "https://t-hunted.blogspot.com/2026/05/post.html"
        ) is True

    def test_rejects_other_blogspot_subdomain(self):
        # Permissive substring-check in the dispatcher would let this
        # through — exact-match in the parser must NOT.
        assert t_hunted_source._is_allowed_t_hunted_url(
            "https://other.blogspot.com/x"
        ) is False
        assert t_hunted_source._is_allowed_t_hunted_url(
            "https://someblog.blogspot.com/2026/05/post"
        ) is False

    def test_rejects_arbitrary_external_host(self):
        assert t_hunted_source._is_allowed_t_hunted_url(
            "https://attacker.example.com/x"
        ) is False
        # SSRF classics: loopback + AWS metadata IP.
        assert t_hunted_source._is_allowed_t_hunted_url(
            "http://127.0.0.1/x"
        ) is False
        assert t_hunted_source._is_allowed_t_hunted_url(
            "http://169.254.169.254/latest/meta-data/"
        ) is False

    def test_rejects_non_http_scheme(self):
        assert t_hunted_source._is_allowed_t_hunted_url(
            "file:///etc/passwd"
        ) is False
        assert t_hunted_source._is_allowed_t_hunted_url(
            "ftp://t-hunted.blogspot.com/x"
        ) is False
        assert t_hunted_source._is_allowed_t_hunted_url(
            "gopher://t-hunted.blogspot.com/x"
        ) is False

    def test_fetch_returns_none_and_pings_when_host_rejected(self):
        session = MagicMock()
        notifier = MagicMock()

        out = t_hunted_source.fetch_t_hunted_article(
            "https://evil.example.com/x",
            session=session,
            notifier=notifier,
        )

        assert out is None
        # Network was NOT touched — host rejection short-circuits before HTTP.
        session.get.assert_not_called()
        notifier.assert_called_once()
        # Fingerprint pins this to the host-rejected builder — a
        # regression that fires E032/E033 instead would fail here.
        msg = notifier.call_args.args[0]
        assert "[E031-STUB]" in msg

    def test_rejects_userinfo_attack(self):
        # ``urlparse(...).hostname`` correctly returns 'evil.com' here
        # (not 't-hunted.blogspot.com'); split-on-@ of ``netloc`` would
        # have been fooled.
        assert t_hunted_source._is_allowed_t_hunted_url(
            "http://t-hunted.blogspot.com@evil.com/x"
        ) is False

    def test_rejects_suffix_host_attack(self):
        # Substring/endswith-style checks would accept this — exact-match
        # rejects.
        assert t_hunted_source._is_allowed_t_hunted_url(
            "http://t-hunted.blogspot.com.attacker.example/x"
        ) is False

    def test_rejects_subdomain_variants(self):
        # Allowlist holds exactly 't-hunted.blogspot.com' — no www, no
        # nested subdomains.
        assert t_hunted_source._is_allowed_t_hunted_url(
            "http://evil.t-hunted.blogspot.com/x"
        ) is False
        assert t_hunted_source._is_allowed_t_hunted_url(
            "http://www.t-hunted.blogspot.com/x"
        ) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Task 7 — block emission, alignment, heading heuristic, kill switch
# ---------------------------------------------------------------------------
#
# The defect this task must NOT reproduce: the subtitle lift moves ONE entry
# out of the flat list, and if `blocks` keeps it the two lists differ by one.
# `_llm_common` pairs them POSITIONALLY when encoding and consumes them
# sequentially when decoding, and BOTH sides swallow a shortfall silently
# (`except StopIteration`, no log) — so the tail block ships to the channel
# IN PORTUGUESE. That outage happened 2026-05-06; orangetrack answered it by
# hardcoding `subtitle = ""`. The operator wants the lead kept, so the only
# way out is doing the lift as ONE operation over a single list.

import glob
import os

import dom_blocks
import feature_flags

PATCHABLE = ("lead", "paragraph", "heading", "list_item")
FIXTURE_GLOB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures", "articles", "t_hunted", "*.html",
)


def _fetch(html):
    session = MagicMock()
    session.get.return_value = _make_response(html)
    return t_hunted_source.fetch_t_hunted_article(
        "https://t-hunted.blogspot.com/2026/01/post.html", session=session,
    )


def _body(inner, title="Título do post"):
    return (
        f'<html><body><h3 class="post-title">{title}</h3>'
        f'<div class="post-body">{inner}</div></body></html>'
    )


def _patchable(article):
    return [b for b in (article.get("blocks") or []) if b["type"] in PATCHABLE]


class TestBlocksAlignment:
    """`len(patchable blocks) == len(paragraphs)` is the whole point."""

    def test_blocks_and_paragraphs_aligned_after_subtitle_lift(self):
        art = _fetch(_body(
            "<p>Primeiro parágrafo com texto.</p>"
            "<p>Segundo parágrafo com texto.</p>"
            "<p>Terceiro parágrafo com texto.</p>"
            "<p>Quarto parágrafo com texto.</p>"
        ))
        assert art["subtitle"] == "Primeiro parágrafo com texto."
        assert len(_patchable(art)) == len(art["paragraphs"]) == 3

    def test_lifted_subtitle_is_absent_from_blocks(self):
        """If the lead stayed a block it would publish twice AND desync."""
        art = _fetch(_body(
            "<p>Lead que sobe.</p><p>Corpo do post.</p>"
        ))
        assert art["subtitle"] == "Lead que sobe."
        assert all(b.get("text") != "Lead que sobe." for b in art["blocks"])

    def test_single_paragraph_post_stays_aligned_with_empty_subtitle(self):
        """The photo-gallery post is t-hunted's dominant format. An
        unconditional lift empties `paragraphs` and news_bot drops the post on
        its `not article.get('paragraphs')` guard — that cost Hotfix 1."""
        art = _fetch(_body(
            '<p>Único parágrafo.</p>'
            '<img src="https://blogger.googleusercontent.com/img/a/=s1600/p.jpg" />'
        ))
        assert art["subtitle"] == ""
        assert art["paragraphs"] == ["Único parágrafo."]
        assert len(_patchable(art)) == 1

    def test_title_repeating_paragraph_dropped_from_both_lists(self):
        """The SECOND source of desync (§ II-3.2) — a parser-local predicate
        applied to only one of the two lists."""
        art = _fetch(_body(
            "<p>Título do post</p><p>Primeiro real.</p><p>Segundo real.</p>"
        ))
        assert "Título do post" not in art["paragraphs"]
        assert all(b.get("text") != "Título do post" for b in art["blocks"])
        assert len(_patchable(art)) == len(art["paragraphs"])

    def test_title_dedup_survives_the_flattener_change(self):
        """Title comes from `get_text(" ")` → 'a b c'; block text comes from
        `text_from_runs` → 'abc'. Comparing them raw would silently stop
        matching, which is precisely the desync this task exists to prevent."""
        art = _fetch(_body(
            "<p>Caça<b>ao</b>tesouro</p><p>Um.</p><p>Dois.</p>",
            title="Caça ao tesouro",
        ))
        assert len(_patchable(art)) == len(art["paragraphs"])
        assert not any("Caçaaotesouro" == p for p in art["paragraphs"])

    def test_boilerplate_dropped_from_both_lists(self):
        art = _fetch(_body(
            "<p>Conteúdo real do post.</p>"
            "<p>Outro conteúdo real.</p>"
            "<p>Compartilhar no Facebook</p>"
        ))
        joined = " ".join(art["paragraphs"])
        assert "Facebook" not in joined
        assert len(_patchable(art)) == len(art["paragraphs"])

    def test_boilerplate_at_top_does_not_become_subtitle(self):
        """CRITICAL ORDER: filter BEFORE the lift, or a Blogger footer floats
        into the decorative lead."""
        art = _fetch(_body(
            "<p>Compartilhar no Facebook</p>"
            "<p>Primeiro conteúdo de verdade.</p>"
            "<p>Segundo conteúdo de verdade.</p>"
        ))
        assert art["subtitle"] == "Primeiro conteúdo de verdade."

    @pytest.mark.parametrize(
        "path", sorted(glob.glob(FIXTURE_GLOB)),
        ids=lambda p: os.path.basename(p)[:30],
    )
    def test_corpus_blocks_align_with_paragraphs(self, path):
        """The smoke check, as a test — the console version alone would not
        survive a refactor."""
        art = _fetch(open(path, encoding="utf-8").read())
        assert art is not None
        assert len(_patchable(art)) == len(art["paragraphs"])


class TestMarkupSurvives:
    def test_bold_inside_paragraph_survives_as_run(self):
        # A lead paragraph goes first on purpose: the subtitle lift consumes
        # the first patchable block, so the subject of the test must not be it.
        art = _fetch(_body(
            "<p>Lead que sobe para o subtítulo.</p>"
            "<p>Texto <b>negrito</b> final.</p>"
        ))
        para = [b for b in art["blocks"] if b["type"] == "paragraph"][0]
        bold = [r for r in para["runs"] if "bold" in (r.get("formats") or [])]
        assert [r["text"] for r in bold] == ["negrito"]

    def test_inline_link_text_stays_inline(self):
        art = _fetch(_body(
            "<p>Lead que sobe para o subtítulo.</p>"
            '<p>Veja <a href="https://t-hunted.blogspot.com/x">aqui</a> agora.</p>'
        ))
        para = [b for b in art["blocks"] if b["type"] == "paragraph"][0]
        assert "aqui" in para["text"]
        assert any(r.get("href") for r in para["runs"])

    def test_list_items_emit_list_item_blocks(self):
        art = _fetch(_body(
            "<p>Intro do post.</p><ul><li>Ferrari</li><li>Porsche</li></ul>"
        ))
        items = [b for b in art["blocks"] if b["type"] == "list_item"]
        assert [b["text"] for b in items] == ["Ferrari", "Porsche"]
        assert "•" not in items[0]["text"]
        for text in ("Ferrari", "Porsche"):
            assert text in art["paragraphs"]

    def test_youtube_embed_becomes_a_video_block(self):
        """Measured, not speculative: the corpus carries two YouTube embeds
        which the flat-text parser dropped entirely."""
        art = _fetch(_body(
            "<p>Assista abaixo.</p><p>Mais texto aqui.</p>"
            '<iframe src="https://www.youtube.com/embed/lJhcBCcHeOs"></iframe>'
        ))
        videos = [b for b in art["blocks"] if b["type"] == "video"]
        assert len(videos) == 1
        assert videos[0]["src"].startswith("https://telegra.ph/embed/youtube?url=")

    def test_video_blocks_do_not_break_alignment(self):
        art = _fetch(_body(
            "<p>Assista abaixo.</p><p>Mais texto aqui.</p>"
            '<iframe src="https://www.youtube.com/embed/lJhcBCcHeOs"></iframe>'
        ))
        assert len(_patchable(art)) == len(art["paragraphs"])


class TestHeadingHeuristic:
    """t-hunted has ZERO real h2/h3/h4 inside `div.post-body` across all 10
    corpus articles, so the heuristic is the only source of headings — which
    is exactly why the negative controls matter. A heuristic covered only by
    positive tests is how false headings reach the channel."""

    def _types(self, inner):
        art = _fetch(_body(inner))
        return [b["type"] for b in art["blocks"]]

    def test_whole_bold_paragraph_becomes_heading(self):
        types = self._types(
            "<p>Lead que sobe para o subtítulo.</p>"
            "<p><b>Todo Hot Wheels antigo é valioso?</b></p>"
            "<p>Corpo do texto aqui.</p>"
        )
        assert "heading" in types

    @pytest.mark.parametrize(
        "inner",
        [
            "<p>Intro <b>negrito parcial</b></p>",
            "<p><b>Parte</b> e <b>duas</b></p>",
            "<p><b>Frase completa em negrito.</b></p>",
        ],
        ids=["partial-bold", "two-spans", "ends-with-period"],
    )
    def test_negative_controls_stay_paragraphs(self, inner):
        types = self._types(
            "<p>Lead que sobe para o subtítulo.</p>" + inner + "<p>Corpo um.</p>"
        )
        assert "heading" not in types

    def test_long_whole_bold_paragraph_is_still_a_heading(self):
        """Pins the ABSENCE of a length limit (approved deviation from
        user-spec AC2): a limit reintroduced later must fail here."""
        long_title = "Novidades " * 12  # >100 chars
        types = self._types(
            "<p>Lead que sobe para o subtítulo.</p>"
            f"<p><b>{long_title.strip()}</b></p>"
            "<p>Corpo um.</p>"
        )
        assert "heading" in types

    def test_real_heading_tag_still_emits_heading_block(self):
        types = self._types(
            "<p>Lead que sobe para o subtítulo.</p>"
            "<h2>Seção de verdade</h2><p>Corpo um.</p>"
        )
        assert "heading" in types

    def test_heading_text_is_in_flat_paragraphs(self):
        """Headings are in `_PATCHED_TEXT_BLOCK_TYPES`; leaving them out of
        the flat list shifts the pairing by one at every heading."""
        art = _fetch(_body(
            "<p>Lead do post.</p>"
            "<p><b>Uma seção</b></p>"
            "<p>Corpo depois.</p>"
        ))
        assert "Uma seção" in art["paragraphs"]
        assert len(_patchable(art)) == len(art["paragraphs"])


class TestFeatureFlag:
    def test_flag_off_returns_no_blocks(self, monkeypatch):
        monkeypatch.setattr(feature_flags, "SOURCE_FORMATTING_ENABLED", False)
        art = _fetch(_body("<p>Um parágrafo.</p><p>Outro parágrafo.</p>"))
        assert not art.get("blocks")

    def test_flag_off_keeps_paragraphs_and_images(self, monkeypatch):
        inner = (
            "<p>Um parágrafo.</p><p>Outro parágrafo.</p>"
            '<img src="https://blogger.googleusercontent.com/img/a/=s1600/p.jpg" />'
        )
        on = _fetch(_body(inner))
        monkeypatch.setattr(feature_flags, "SOURCE_FORMATTING_ENABLED", False)
        off = _fetch(_body(inner))
        assert off["paragraphs"] == on["paragraphs"]
        assert off["images"] == on["images"]
        assert off["subtitle"] == on["subtitle"]


class TestDegradation:
    def test_broken_markup_still_returns_flat_text(self):
        """AC5 — fail-open. Broken markup publishes as plain text, it does not
        take the tick down."""
        art = _fetch(_body(
            "<p>Texto válido aqui.<p><b>não fechado"
            "<div><p>Mais texto solto.</p>"
        ))
        assert art is not None
        assert art["paragraphs"]
