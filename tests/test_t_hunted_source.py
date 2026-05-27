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
        lambda link: f"[E031-STUB] host rejected: {link}",
        raising=False,
    )
    monkeypatch.setattr(
        admin_alerts,
        "alert_t_hunted_fetch_error",
        lambda link, err: f"[E032-STUB] fetch error: {link}: {err}",
        raising=False,
    )
    monkeypatch.setattr(
        admin_alerts,
        "alert_t_hunted_no_body",
        lambda link: f"[E033-STUB] no body: {link}",
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
        # Notifier receives a string payload.
        msg = notifier.call_args.args[0]
        assert isinstance(msg, str)

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

    def test_image_limit_enforced(self):
        # 15 unique image paths so dedup doesn't collapse them.
        imgs = "\n".join(
            f'<img src="https://blogger.googleusercontent.com/img/{i}/=s1600/x.jpg" />'
            for i in range(15)
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
        assert t_hunted_source._IMAGE_LIMIT == 10

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
        assert "Second content paragraph." in out["paragraphs"]


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
