#!/usr/bin/env python3
"""Unit tests for preview_renderer.

Covers Decision 1 invariants from tech-spec:
  1. tag allowlist (only the set emitted by telegraph_publisher)
  2. URL-scheme allowlist (^https?://, else attribute dropped)
  3. CSP meta tag present in <head> (byte-exact match)

Plus HTML-escape safety for text and attribute values, Cyrillic
round-trip, void-tag self-close, and the smoke CLI-call shape.
"""

import pytest

import preview_renderer as pr


# --- basic document structure -------------------------------------------------


def test_render_html_returns_doctype_and_title():
    out = pr.render_html([], "T")
    assert out.startswith("<!DOCTYPE html>")
    assert out.endswith("</html>")
    assert "<title>T</title>" in out
    assert "<h1>T</h1>" in out


def test_render_html_escapes_title_html_special_chars():
    out = pr.render_html([], "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_render_html_preserves_cyrillic():
    title = "🔥 Горячие новости"
    nodes = [{"tag": "p", "children": ["Привет мир"]}]
    out = pr.render_html(nodes, title)
    # Unicode survives as-is, not as &#...; entities.
    assert "🔥 Горячие новости" in out
    assert "Привет мир" in out
    assert "&#" not in out  # no numeric entity fallback


def test_empty_nodes_produce_valid_document():
    out = pr.render_html([], "")
    assert out.startswith("<!DOCTYPE html>")
    assert "<title></title>" in out
    assert "<h1></h1>" in out


# --- tag allowlist (invariant 1) ---------------------------------------------


@pytest.mark.parametrize(
    "tag",
    ["p", "figure", "figcaption", "h3", "h4", "i", "b", "strong", "u", "s"],
)
def test_renders_allowed_container_tags(tag):
    out = pr.render_html([{"tag": tag, "children": ["x"]}], "t")
    assert f"<{tag}>x</{tag}>" in out


@pytest.mark.parametrize("tag", ["strong", "u", "s"])
def test_format_tags_emitted_by_the_publisher_survive_preview(tag):
    """REGRESSION 2026-07-28 — the allowlist was missing the tags the publisher
    actually emits for runs metadata.

    `telegraph_publisher._FORMAT_TAGS` maps bold->strong, underline->u,
    strikethrough->s, but only `b`/`i` were allowed here — and `_render_node`
    drops an unknown tag TOGETHER WITH ITS CHILDREN, so the formatted WORD
    disappeared from the preview entirely rather than merely losing its style.
    Telegraph accepts the tags, so published pages were always correct; the
    preview is what lied, which matters because the preview is how a human
    verifies formatting before/after a change.
    """
    nodes = [{"tag": "p", "children": [
        "Hello ", {"tag": tag, "children": ["kept"]}, " world",
    ]}]
    out = pr.render_html(nodes, "t")
    assert f"<p>Hello <{tag}>kept</{tag}> world</p>" in out


def test_allowlist_covers_every_tag_the_publisher_can_emit():
    """Guard the lock-step the comment above `_ALLOWED_TAGS` promises: any
    format tag the publisher can produce must be renderable here."""
    import telegraph_publisher as tp
    emitted = {tag for _fmt, tag in tp._FORMAT_TAGS}
    missing = emitted - pr._ALLOWED_TAGS
    assert not missing, (
        f"publisher emits {sorted(missing)} but the preview allowlist drops "
        "them together with their text"
    )


def test_renders_allowed_a_with_href():
    out = pr.render_html(
        [{"tag": "a", "attrs": {"href": "https://example.com/"},
          "children": ["click"]}],
        "t",
    )
    assert '<a href="https://example.com/">click</a>' in out


def test_renders_allowed_img():
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "https://example.com/x.jpg"}}],
        "t",
    )
    assert '<img src="https://example.com/x.jpg">' in out


def test_renders_allowed_iframe():
    out = pr.render_html(
        [{"tag": "iframe", "attrs": {"src": "https://example.com/e"}}],
        "t",
    )
    assert '<iframe src="https://example.com/e"></iframe>' in out


def test_renders_allowed_hr():
    out = pr.render_html([{"tag": "hr"}], "t")
    assert "<hr>" in out
    assert "</hr>" not in out


def test_unknown_tag_silently_dropped():
    # unknown tags AND their children must not appear in output
    out = pr.render_html(
        [{"tag": "script", "children": ["alert(1)"]}],
        "t",
    )
    assert "<script" not in out
    assert "alert(1)" not in out


@pytest.mark.parametrize("bad_tag", ["style", "object", "svg", "form", "link"])
def test_other_unknown_tags_dropped(bad_tag):
    # Use a distinct payload string so we don't conflict with the inline
    # readability <style> block the renderer legitimately emits.
    payload = "XSS_PAYLOAD_MARKER"
    out = pr.render_html(
        [{"tag": bad_tag, "children": [payload]}],
        "t",
    )
    # The user-supplied unknown tag must never be emitted as its own element,
    # and its children must be dropped entirely (not inlined, not escaped).
    assert payload not in out
    # For tags that do not collide with renderer-emitted elements, assert
    # no closing form appears either. `style` collides with the inline
    # readability <style> block that the renderer legitimately emits in
    # <head>, so we only assert payload absence for that one.
    if bad_tag != "style":
        assert f"</{bad_tag}>" not in out


def test_void_tags_self_close():
    out = pr.render_html(
        [
            {"tag": "hr"},
            {"tag": "img", "attrs": {"src": "https://example.com/x.jpg"}},
        ],
        "t",
    )
    assert "<hr>" in out and "</hr>" not in out
    assert '<img src="https://example.com/x.jpg">' in out
    assert "</img>" not in out


def test_nested_children_recursion():
    nodes = [
        {"tag": "figure", "children": [
            {"tag": "img", "attrs": {"src": "https://example.com/x.jpg"}},
            {"tag": "figcaption", "children": ["caption text"]},
        ]}
    ]
    out = pr.render_html(nodes, "t")
    # figcaption must be inside figure, not after it
    fig_open = out.index("<figure>")
    fig_close = out.index("</figure>")
    figcap = out.index("<figcaption>caption text</figcaption>")
    assert fig_open < figcap < fig_close


def test_non_str_non_dict_node_returns_empty():
    # Defensive: numbers, None, lists as children → silently dropped.
    out = pr.render_html(
        [{"tag": "p", "children": ["ok", 42, None, ["nested"]]}],
        "t",
    )
    assert "<p>ok</p>" in out
    assert "42" not in out
    assert "nested" not in out


# --- URL-scheme allowlist (invariant 2) --------------------------------------


def test_img_src_javascript_dropped():
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "javascript:alert(1)"}}],
        "t",
    )
    assert "<img>" in out
    assert "javascript" not in out
    assert "alert(1)" not in out


def test_img_src_data_dropped():
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "data:image/svg+xml;base64,PHN2Zz4="}}],
        "t",
    )
    assert "<img>" in out
    assert "data:" not in out
    assert "base64" not in out


def test_iframe_src_javascript_dropped():
    out = pr.render_html(
        [{"tag": "iframe", "attrs": {"src": "javascript:alert(1)"}}],
        "t",
    )
    assert "<iframe></iframe>" in out
    assert "javascript" not in out
    assert "alert(1)" not in out


def test_a_href_javascript_dropped():
    out = pr.render_html(
        [{"tag": "a", "attrs": {"href": "javascript:alert(1)"},
          "children": ["click"]}],
        "t",
    )
    # href gone but text remains.
    assert "<a>click</a>" in out
    assert "javascript" not in out
    assert "alert(1)" not in out


def test_a_href_vbscript_dropped():
    out = pr.render_html(
        [{"tag": "a", "attrs": {"href": "vbscript:evil"},
          "children": ["click"]}],
        "t",
    )
    assert "<a>click</a>" in out
    assert "vbscript" not in out


def test_a_href_file_scheme_dropped():
    out = pr.render_html(
        [{"tag": "a", "attrs": {"href": "file:///etc/passwd"},
          "children": ["click"]}],
        "t",
    )
    assert "<a>click</a>" in out
    assert "file:" not in out
    assert "passwd" not in out


def test_https_url_preserved():
    out = pr.render_html(
        [
            {"tag": "img", "attrs": {"src": "https://s1.cdn.example.com/x.jpg"}},
            {"tag": "a", "attrs": {"href": "https://telegra.ph/Hello-01-01"},
             "children": ["t"]},
        ],
        "t",
    )
    assert '<img src="https://s1.cdn.example.com/x.jpg">' in out
    assert '<a href="https://telegra.ph/Hello-01-01">t</a>' in out


def test_http_url_preserved():
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "http://example.com/x.jpg"}}],
        "t",
    )
    assert '<img src="http://example.com/x.jpg">' in out


def test_uppercase_scheme_preserved():
    # Regex uses IGNORECASE so HTTPS://... stays; still no javascript match.
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "HTTPS://example.com/x.jpg"}}],
        "t",
    )
    assert '<img src="HTTPS://example.com/x.jpg">' in out


def test_relative_url_dropped():
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "/local/path.jpg"}}],
        "t",
    )
    assert "<img>" in out
    assert "/local/path.jpg" not in out


def test_empty_url_dropped():
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": ""}}],
        "t",
    )
    assert "<img>" in out


def test_url_with_leading_whitespace_dropped():
    # '^' anchor blocks whitespace padding; regex has no \s* allowance.
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "   https://example.com/x.jpg"}}],
        "t",
    )
    assert "<img>" in out
    assert "https://example.com/x.jpg" not in out


def test_attribute_value_escaped_in_href():
    out = pr.render_html(
        [{"tag": "a", "attrs": {"href": "https://example.com/?q=<script>"},
          "children": ["t"]}],
        "t",
    )
    # scheme matches → attribute kept, but value is HTML-escaped.
    assert "<script>" not in out  # no literal angle bracket inside attribute
    assert "&lt;script&gt;" in out
    assert 'href="https://example.com/?q=&lt;script&gt;"' in out


def test_malicious_attribute_name_dropped():
    # A crafted attribute name that would otherwise break out of the tag
    # must be dropped entirely — html.escape on the value cannot save us
    # if the key itself contains `"`, `>` or whitespace.
    out = pr.render_html(
        [{"tag": "p",
          "attrs": {'x" onerror="alert(1)': "y",
                    "normal": "ok"},
          "children": ["t"]}],
        "t",
    )
    assert "onerror" not in out
    assert "alert(1)" not in out
    # Legitimate attribute on same element still comes through.
    assert 'normal="ok"' in out


@pytest.mark.parametrize("bad_key", [
    'x onerror="y"',
    'src>',
    'data-\nkey',
    '',
    '123-starts-with-digit',
    'with space',
])
def test_non_identifier_attribute_name_dropped(bad_key):
    out = pr.render_html(
        [{"tag": "p", "attrs": {bad_key: "payload"}, "children": ["t"]}],
        "t",
    )
    # Attribute value must never appear in output under a bogus key.
    assert "payload" not in out


def test_non_url_attribute_escaped_and_preserved():
    out = pr.render_html(
        [{"tag": "img", "attrs": {
            "src": "https://example.com/x.jpg",
            "alt": 'she said "hi"',
        }}],
        "t",
    )
    assert 'alt="she said &quot;hi&quot;"' in out


# --- CSP meta (invariant 3) --------------------------------------------------


CSP_META_EXPECTED = (
    '<meta http-equiv="Content-Security-Policy" '
    'content="default-src \'none\'; img-src https:; '
    'frame-src https:; style-src \'unsafe-inline\'">'
)


def test_csp_meta_tag_exact_string():
    out = pr.render_html([], "t")
    assert CSP_META_EXPECTED in out


def test_csp_meta_tag_key_substrings():
    out = pr.render_html([], "t")
    assert 'http-equiv="Content-Security-Policy"' in out
    assert "default-src 'none'" in out
    assert "img-src https:" in out
    assert "frame-src https:" in out
    assert "style-src 'unsafe-inline'" in out


def test_csp_meta_before_body():
    out = pr.render_html([], "t")
    csp_idx = out.index('http-equiv="Content-Security-Policy"')
    body_idx = out.index("<body>")
    assert csp_idx < body_idx


def test_csp_meta_inside_head():
    out = pr.render_html([], "t")
    head_open = out.index("<head>")
    head_close = out.index("</head>")
    csp_idx = out.index('http-equiv="Content-Security-Policy"')
    assert head_open < csp_idx < head_close


# --- HTML-escape safety ------------------------------------------------------


def test_text_child_escapes_html_special_chars():
    out = pr.render_html(
        [{"tag": "p", "children": ['5 < 10 & "quoted"']}],
        "t",
    )
    assert "5 &lt; 10 &amp; &quot;quoted&quot;" in out
    # no unescaped literals
    assert "5 < 10" not in out


def test_text_child_escapes_apostrophe():
    out = pr.render_html(
        [{"tag": "p", "children": ["it's"]}],
        "t",
    )
    # html.escape(quote=True) turns ' into &#x27;
    assert "it&#x27;s" in out
    assert "<p>it's</p>" not in out


def test_attribute_escapes_quotes():
    out = pr.render_html(
        [{"tag": "img", "attrs": {
            "src": "https://example.com/x.jpg",
            "alt": 'she said "hi"',
        }}],
        "t",
    )
    assert 'alt="she said &quot;hi&quot;"' in out
    # Parser must not be confused by raw quote inside attribute.
    assert 'alt="she said "hi""' not in out


def test_onclick_attribute_on_allowed_tag_escaped():
    # onclick is not blocked by the URL-scheme filter (it's not a URL attr),
    # but its value is HTML-escaped, so the payload cannot break out of the
    # attribute quoting. CSP (default-src 'none') blocks execution regardless.
    out = pr.render_html(
        [{"tag": "p", "attrs": {"onclick": "alert('xss')"},
          "children": ["x"]}],
        "t",
    )
    # The value's quote/apostrophe are escaped; attribute syntax stays intact.
    assert "alert('xss')" not in out  # raw apostrophe escaped → &#x27;
    assert "onclick=\"alert(&#x27;xss&#x27;)\"" in out


# --- Cyrillic / Unicode round-trip ------------------------------------------


def test_cyrillic_in_attribute_preserved():
    out = pr.render_html(
        [{"tag": "img", "attrs": {
            "src": "https://example.com/x.jpg",
            "alt": "Ковёр-самолёт",
        }}],
        "t",
    )
    assert 'alt="Ковёр-самолёт"' in out


def test_cyrillic_in_paragraph_preserved():
    nodes = [
        {"tag": "p", "children": [
            "Источник: ",
            {"tag": "a", "attrs": {"href": "https://telegra.ph/x"},
             "children": ["ссылка"]},
        ]}
    ]
    out = pr.render_html(nodes, "Заголовок")
    assert "Источник: " in out
    assert "ссылка" in out
    assert "<h1>Заголовок</h1>" in out


# --- missing/empty attrs and children ---------------------------------------


def test_missing_attrs_key_is_ok():
    out = pr.render_html([{"tag": "p", "children": ["x"]}], "t")
    assert "<p>x</p>" in out


def test_missing_children_key_is_ok():
    out = pr.render_html([{"tag": "p"}], "t")
    assert "<p></p>" in out


def test_void_tag_ignores_children():
    out = pr.render_html(
        [{"tag": "img",
          "attrs": {"src": "https://example.com/x.jpg"},
          "children": ["should-be-ignored"]}],
        "t",
    )
    assert '<img src="https://example.com/x.jpg">' in out
    assert "should-be-ignored" not in out


# --- smoke -------------------------------------------------------------------


def test_smoke_cli_call_shape():
    # Mirrors Verify-smoke from task 2 tech-spec.
    out = pr.render_html([{"tag": "p", "children": ["test"]}], "T")
    assert out[:60].startswith("<!DOCTYPE html>")


def test_smoke_url_scheme_filter():
    out = pr.render_html(
        [{"tag": "img", "attrs": {"src": "javascript:alert(1)"}}],
        "x",
    )
    assert "javascript" not in out
    assert "<img" in out


def test_smoke_csp_present():
    out = pr.render_html([], "t")
    assert "default-src 'none'" in out
    assert "img-src https:" in out
    assert "frame-src https:" in out


# --- no side effects ---------------------------------------------------------


def test_render_html_is_pure_returns_str():
    # Repeated calls with the same input return identical strings.
    nodes = [{"tag": "p", "children": ["x"]}]
    assert pr.render_html(nodes, "t") == pr.render_html(nodes, "t")
    assert isinstance(pr.render_html([], ""), str)


def test_output_parses_as_html_without_error():
    # The rendered document must be well-formed enough that stdlib's
    # html.parser walks it start-to-end without raising. This catches
    # broken tag boundaries or attribute-quoting bugs that substring
    # assertions might miss.
    from html.parser import HTMLParser

    class _Walker(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.saw_h1 = False
            self.depth = 0

        def handle_starttag(self, tag, attrs):
            self.depth += 1
            if tag == "h1":
                self.saw_h1 = True

        def handle_endtag(self, tag):
            self.depth -= 1

    nodes = [
        {"tag": "figure", "children": [
            {"tag": "img", "attrs": {"src": "https://example.com/x.jpg",
                                       "alt": 'tricky "quoted" & <angle>'}},
            {"tag": "figcaption", "children": ["caption"]},
        ]},
        {"tag": "p", "children": [
            "Lead ",
            {"tag": "b", "children": ["bold"]},
            " end",
        ]},
    ]
    out = pr.render_html(nodes, 'Title with "quotes" & <angles>')
    walker = _Walker()
    walker.feed(out)
    walker.close()
    assert walker.saw_h1 is True


# --------------------------------------------------------------------------- #
# AC12 — end-to-end: real publisher nodes must survive the preview renderer   #
# --------------------------------------------------------------------------- #
#
# The 48 tests above render HAND-WRITTEN node trees. None of them referenced
# `blocks`, `runs` or `preview_nodes`, so they could all stay green while the
# preview silently ate what the publisher actually emits — which is exactly
# what happened on 2026-07-28: `<strong>` was missing from `_ALLOWED_TAGS`, an
# unknown tag is dropped TOGETHER WITH ITS CHILDREN, and the word vanished from
# the preview. These tests close the loop: blocks → preview_nodes → render_html.

import telegraph_publisher as _tp  # noqa: E402


_AC12_BLOCKS = [
    {"type": "heading", "level": 3, "text": "Case A breakdown",
     "runs": [{"text": "Case A breakdown"}]},
    {"type": "paragraph",
     "text": "The Alphard is the standout casting here.",
     "runs": [
         {"text": "The "},
         {"text": "Alphard", "formats": ["bold"]},
         {"text": " is the standout casting here."},
     ]},
    {"type": "list_item", "text": "Ferrari Testarossa",
     "runs": [{"text": "Ferrari Testarossa"}]},
    {"type": "list_item", "text": "Koenigsegg CC850",
     "runs": [{"text": "Koenigsegg CC850"}]},
    {"type": "image", "src": "https://cdn.example.com/hero.jpg"},
]


def _render(blocks, **kwargs):
    nodes = _tp.preview_nodes("T", blocks=blocks, **kwargs)
    return nodes, pr.render_html(nodes, "T")


def test_publisher_blocks_render_headings_bold_and_list_items():
    _, out = _render(_AC12_BLOCKS)
    assert "<h3>Case A breakdown</h3>" in out
    assert "<strong>Alphard</strong>" in out
    assert out.count("• ") == 2
    assert "Ferrari Testarossa" in out
    assert "Koenigsegg CC850" in out


def test_preview_does_not_swallow_words_around_bold():
    """The exact shape of the 2026-07-28 incident: a dropped tag takes its
    children with it, so a word disappears. Assert the WHOLE sentence."""
    _, out = _render(_AC12_BLOCKS)
    for word in ("The", "Alphard", "is", "the", "standout", "casting", "here."):
        assert word in out, f"{word!r} vanished from the preview"


def test_preview_image_limit_matches_published_nodes():
    """Preview and publication must agree, and they agree only because they
    are handed the SAME arguments — including the cap."""
    blocks = [
        {"type": "image", "src": f"https://cdn.example.com/x{i}.jpg"}
        for i in range(20)
    ]
    nodes, out = _render(blocks, image_limit=10)
    figures = [n for n in nodes if isinstance(n, dict) and n.get("tag") == "figure"]
    assert len(figures) == 10
    assert out.count("<img") == 10


def test_preview_drops_unsafe_img_src_end_to_end():
    """Defence in depth — the publisher drops it, and the renderer would too."""
    blocks = [
        {"type": "image", "src": "javascript:alert(1)"},
        {"type": "paragraph", "text": "Body.", "runs": [{"text": "Body."}]},
    ]
    _, out = _render(blocks)
    assert "javascript" not in out
    assert "<img" not in out
    assert "Body." in out
