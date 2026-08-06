"""Unit tests for ``dom_blocks`` — the shared inline-markup walker.

The module is the foundation of source-formatting-parity: Task 7 (t-hunted)
and all of Phase 2 (lamley, autoevolution) build on it. Both functions that
carried all three defects of 2026-07-28 (`_runs_from_tag` and the emit/walk
pair) now live in ONE place, so the next such bug is fixed once rather than
four times.

Test shape follows test-master § Fragmentation Anti-pattern: one function per
BEHAVIOUR, `parametrize` for input variants.
"""

import ast
import os
import sys
import time

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dom_blocks  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

YOUTUBE_HOSTS = (
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtube-nocookie.com",
    "www.youtube-nocookie.com", "youtu.be",
)
VIMEO_HOSTS = ("vimeo.com", "www.vimeo.com", "player.vimeo.com")


def _tag(html: str, name: str = "p"):
    return BeautifulSoup(html, "html.parser").find(name)


def _soup(html: str):
    return BeautifulSoup(html, "html.parser")


def _build(html: str, **kwargs) -> list:
    """Walk ``html`` with a fresh builder and return its blocks."""
    builder = dom_blocks.BlockBuilder(**kwargs)
    builder.walk(_soup(html))
    return builder.blocks


# --------------------------------------------------------------------------- #
# Runs and text flattening                                                    #
# --------------------------------------------------------------------------- #


class TestRuns:
    def test_bold_inside_paragraph_marks_only_the_bold_span(self):
        """Pins the 2026-07-28 fix: `flush()` must run BEFORE the new format
        goes on the stack, or everything preceding the first bold span comes
        out bold too."""
        runs = dom_blocks.runs_from_tag(
            _tag("<p>Plain <strong>bold</strong> tail.</p>")
        )
        assert [r["text"] for r in runs] == ["Plain ", "bold", " tail."]
        assert "formats" not in runs[0]
        assert runs[1]["formats"] == ["bold"]
        assert "formats" not in runs[2]

    def test_nested_formats_accumulate_on_one_run(self):
        runs = dom_blocks.runs_from_tag(
            _tag("<p><em>it <strong>both</strong></em></p>")
        )
        both = [r for r in runs if r["text"] == "both"]
        assert len(both) == 1
        assert set(both[0]["formats"]) == {"italic", "bold"}

    def test_every_run_is_locatable_in_the_flattened_text(self):
        """The renderer locates each run with ``text.find``. A separator in
        the join would insert a SECOND space at every format boundary and
        break that — the other half of the 2026-07-28 bug."""
        runs = dom_blocks.runs_from_tag(
            _tag("<p>Plain <strong>bold</strong> tail.</p>")
        )
        text = dom_blocks.text_from_runs(runs)
        assert text == "Plain bold tail."
        assert "  " not in text
        for run in runs:
            assert text.find(run["text"].strip()) != -1

    def test_empty_tags_contribute_nothing_and_whitespace_collapses(self):
        """Pins the ACTUAL contract, verified identical to the pre-extraction
        code: an empty inline tag emits no run at all, while a whitespace-only
        one survives as a single space run and disappears in the flattened
        text. Asserting "whitespace runs are dropped" would have been a
        behaviour CHANGE, and the golden gate would have caught it."""
        runs = dom_blocks.runs_from_tag(
            _tag("<p><strong></strong>  <em>   </em>real</p>")
        )
        assert [r["text"] for r in runs] == [" ", "real"]
        assert dom_blocks.text_from_runs(runs) == "real"

    @pytest.mark.parametrize(
        "href",
        ["javascript:alert(1)", "data:text/html,x", "//evil.example/x",
         "file:///etc/passwd", "relative/path"],
        ids=["javascript", "data", "scheme-relative", "file", "relative"],
    )
    def test_unsafe_href_degrades_anchor_to_plain_text(self, href):
        runs = dom_blocks.runs_from_tag(
            _tag(f'<p>Click <a href="{href}">here</a> now.</p>')
        )
        assert all("href" not in r for r in runs)
        assert "here" in dom_blocks.text_from_runs(runs)

    def test_safe_href_survives_on_its_run(self):
        """Negative control for the test above — without it, an
        implementation that dropped EVERY href would still pass."""
        runs = dom_blocks.runs_from_tag(
            _tag('<p>See <a href="https://example.com/x">this</a>.</p>')
        )
        linked = [r for r in runs if r.get("href")]
        assert [r["text"] for r in linked] == ["this"]
        assert linked[0]["href"] == "https://example.com/x"

    def test_color_class_predicate_is_injected(self):
        """Default is conservative — "there are no colour classes" — so a new
        source gets safe behaviour until someone measures its markup."""
        html = '<p><span class="has-vivid-red-color">red</span></p>'
        default_runs = dom_blocks.runs_from_tag(_tag(html))
        assert all("formats" not in r for r in default_runs)

        injected = dom_blocks.runs_from_tag(
            _tag(html),
            has_color_class=lambda n: any(
                "-color" in (c or "") for c in (n.get("class") or [])
            ),
        )
        assert injected[0]["formats"] == ["bold"]

    def test_colour_class_already_on_the_stack_does_not_split_the_run(self):
        """A colour class whose format is already on the stack must open
        nothing and must NOT pre-flush — otherwise one run splits into two
        adjacent runs carrying identical formats, and the golden drifts."""
        has_color = lambda n: any(  # noqa: E731
            "-color" in (c or "") for c in (n.get("class") or [])
        )
        runs = dom_blocks.runs_from_tag(
            _tag('<p><strong>a<span class="has-red-color">b</span></strong></p>'),
            has_color_class=has_color,
        )
        assert [r["text"] for r in runs] == ["ab"]
        assert runs[0]["formats"] == ["bold"]


# --------------------------------------------------------------------------- #
# Heading heuristic (Decision 2 / 2b)                                         #
# --------------------------------------------------------------------------- #


class TestHeadingHeuristic:
    def _looks(self, html: str) -> bool:
        runs = dom_blocks.runs_from_tag(_tag(html))
        return dom_blocks.looks_like_heading(dom_blocks.text_from_runs(runs), runs)

    def test_whole_bold_paragraph_is_a_heading(self):
        assert self._looks("<p><strong>Case A breakdown</strong></p>") is True

    @pytest.mark.parametrize(
        "ending, expected",
        [(".", False), ("", True), ("…", True), ("….", True),
         ("?", True), ("!", True)],
        ids=["period", "none", "ellipsis", "ellipsis-period", "question", "bang"],
    )
    def test_final_punctuation_decides(self, ending, expected):
        """A trailing period means prose. Ellipsis and ?/! are
        heading-compatible — real orangetrack/t-hunted section titles use
        them."""
        assert self._looks(f"<p><strong>Also ran{ending}</strong></p>") is expected

    @pytest.mark.parametrize(
        "html",
        [
            "<p>Intro <strong>bold tail</strong></p>",
            "<p><strong>Part</strong> and <strong>two</strong></p>",
            "<p><strong>Ford</strong> vs Ford</p>",
        ],
        ids=[
            "partial-coverage",
            "two-separate-spans",
            "repeated-substring-must-not-fake-coverage",
        ],
    )
    def test_partial_bold_coverage_is_not_a_heading(self, html):
        """Three different ways coverage can fail, same verdict. The last one
        is mechanically load-bearing: coverage is computed with ``text.find``,
        so a bold word repeated later in the paragraph must not be read as
        covering the whole line."""
        assert self._looks(html) is False

    def test_length_does_not_disqualify_a_heading(self):
        """There is deliberately NO length limit — an accepted deviation from
        the user-spec. This test exists so that a limit reintroduced later
        fails loudly instead of silently reclassifying headings."""
        for size in (81, 101, 200):
            html = f"<p><strong>{'Long section title ' * size}</strong></p>"
            assert self._looks(html) is True, size

    def test_heuristic_is_off_by_default_in_the_builder(self):
        """orangetrack must not gain headings from this feature (AC10)."""
        blocks = _build("<p><strong>Case A breakdown</strong></p>")
        assert [b["type"] for b in blocks] == ["paragraph"]

        opted_in = _build(
            "<p><strong>Case A breakdown</strong></p>", headings_from_bold=True
        )
        assert [b["type"] for b in opted_in] == ["heading"]

    def test_punctuation_check_uses_endswith_not_a_regex(self):
        """Decision 8 ReDoS contract — the ending check is string work."""
        source = open(
            os.path.join(_REPO_ROOT, "dom_blocks.py"), encoding="utf-8"
        ).read()
        tree = ast.parse(source)
        func = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "looks_like_heading"
        )
        body = ast.dump(func)
        assert "endswith" in body
        assert "re." not in ast.unparse(func)


# --------------------------------------------------------------------------- #
# Structural defaults — shared policy, NOT per-site parameters                #
# --------------------------------------------------------------------------- #


class TestStructuralDefaults:
    @pytest.mark.parametrize("tag", ["h2", "h3", "h4"])
    def test_h2_h3_h4_normalise_to_heading_level_3(self, tag):
        blocks = _build(f"<{tag}>Section title</{tag}>")
        assert blocks == [
            {"type": "heading", "level": 3, "text": "Section title",
             "runs": [{"text": "Section title"}]}
        ]

    def test_h5_emits_a_paragraph_block(self):
        """The ``babc67c`` carve-out, not a forgotten case: on orangetrack
        <h5> is an in-paragraph marker with long text, and heading typography
        looked uneven."""
        blocks = _build("<h5>Section</h5>")
        assert [b["type"] for b in blocks] == ["paragraph"]

    @pytest.mark.parametrize("tag", ["h1", "h6"])
    def test_h1_and_h6_emit_nothing(self, tag):
        assert _build(f"<{tag}>Dropped</{tag}><p>Body.</p>") == [
            {"type": "paragraph", "text": "Body.", "runs": [{"text": "Body."}]}
        ]

    def test_br_inside_one_paragraph_splits_into_separate_blocks(self):
        """Regression for 2026-05-14: a series checklist authored as one <p>
        with <br> between items rendered as a single collapsed line."""
        blocks = _build(
            "<p>#151 – <strong>Alphard</strong>"
            "<br>#152 – <strong>CC850</strong></p>"
        )
        assert [b["text"] for b in blocks] == [
            "#151 – Alphard", "#152 – CC850",
        ]

    @pytest.mark.parametrize("parent", ["ul", "ol"])
    def test_ul_and_ol_are_treated_identically(self, parent):
        blocks = _build(f"<{parent}><li>Ferrari</li></{parent}>")
        assert [b["type"] for b in blocks] == ["list_item"]
        assert blocks[0]["text"] == "Ferrari"

    def test_list_item_text_carries_no_bullet(self):
        """AC2 litmus: the bullet is the publisher's job, added AFTER
        translation so an LLM cannot strip it."""
        blocks = _build("<ul><li>Ferrari</li></ul>")
        assert "•" not in blocks[0]["text"]

    def test_empty_list_item_emits_nothing(self):
        assert _build("<ul><li></li><li>   </li></ul>") == []


# --------------------------------------------------------------------------- #
# The four per-site seams                                                     #
# --------------------------------------------------------------------------- #


class TestSiteSeams:
    def test_image_dedup_key_is_injected(self):
        blogger = (
            '<img src="https://x/s320/car.jpg" />'
            '<img src="https://x/s1600/car.jpg" />'
        )
        # Default key (strip the query) sees two distinct paths.
        assert len(_build(blogger)) == 2
        # A key that also normalises the size segment collapses them.
        collapsed = _build(
            blogger,
            image_dedup_key=lambda src: src.replace("/s320/", "/s/").replace(
                "/s1600/", "/s/"
            ),
        )
        assert len(collapsed) == 1

    def test_chrome_class_predicate_skips_the_whole_subtree(self):
        html = (
            '<div class="sharedaddy"><p>Share this</p></div>'
            "<p>Real content.</p>"
        )
        assert [b["text"] for b in _build(html)] == ["Share this", "Real content."]
        filtered = _build(
            html,
            is_chrome_class=lambda n: any(
                "sharedaddy" in (c or "").lower() for c in (n.get("class") or [])
            ),
        )
        assert [b["text"] for b in filtered] == ["Real content."]

    def test_img_src_picker_is_injected(self):
        html = '<img src="https://x/small.jpg" data-big="https://x/big.jpg" />'
        assert _build(html)[0]["src"] == "https://x/small.jpg"
        picked = _build(
            html,
            pick_img_src=lambda img: dom_blocks.safe_img_src(img.get("data-big")),
        )
        assert picked[0]["src"] == "https://x/big.jpg"

    def test_video_host_gate_runs_before_the_id_regex(self):
        """An attacker URL merely CONTAINING a YouTube embed path must not be
        wrapped — this is the gap autoevolution's regex-only path has, and
        Decision 1 keeps the check inside dom_blocks so a source cannot
        inject a wrapper that skips it."""
        hostile = "https://attacker.example/r?u=https://youtube.com/embed/abc123XYZ"
        assert dom_blocks.video_embed_url(
            hostile, hosts=YOUTUBE_HOSTS, provider="youtube"
        ) is None

    def test_video_provider_data_selects_the_wrapper(self):
        youtube = "https://www.youtube.com/embed/abc123XYZ"
        vimeo = "https://vimeo.com/123456789"
        assert dom_blocks.video_embed_url(
            youtube, hosts=YOUTUBE_HOSTS, provider="youtube"
        ).startswith("https://telegra.ph/embed/youtube?url=")
        # A source carrying only YouTube data must not wrap Vimeo.
        assert dom_blocks.video_embed_url(
            vimeo, hosts=YOUTUBE_HOSTS, provider="youtube"
        ) is None
        assert dom_blocks.video_embed_url(
            vimeo, hosts=VIMEO_HOSTS, provider="vimeo"
        ).startswith("https://telegra.ph/embed/vimeo?url=")


# --------------------------------------------------------------------------- #
# Resource bounds (Decision 8)                                                #
# --------------------------------------------------------------------------- #


class TestResourceBounds:
    @pytest.mark.parametrize("dimension", ["runs", "text"])
    def test_over_the_bound_runs_are_stripped_and_the_text_survives(self, dimension):
        """Past the bound the block loses its runs and keeps its text IN FULL.
        Never truncated, never dropped — this is a resource fuse, not an
        editorial threshold."""
        if dimension == "runs":
            inner = "".join(
                f"<strong>w{i:04d}</strong> "
                for i in range(dom_blocks.MAX_RUNS_PER_BLOCK + 5)
            )
        else:
            inner = "<strong>x</strong>" + "y" * (dom_blocks.MAX_TEXT_FOR_RUNS + 1)
        runs = dom_blocks.runs_from_tag(_tag(f"<p>{inner}</p>"))
        assert len(runs) == 1
        assert "formats" not in runs[0]
        plain = _tag(f"<p>{inner}</p>").get_text()
        assert runs[0]["text"].strip().replace("  ", " ")[:40] == (
            plain.strip().replace("  ", " ")[:40]
        )

    def test_within_the_bound_runs_are_kept(self):
        """Positive control: without it the bound could be set to zero and
        every test above would still pass."""
        # Each <strong> plus its trailing space yields TWO runs, so stay well
        # under half the bound to test the in-bounds side rather than the edge.
        inner = "".join(
            f"<strong>w{i:04d}</strong> "
            for i in range(dom_blocks.MAX_RUNS_PER_BLOCK // 4)
        )
        runs = dom_blocks.runs_from_tag(_tag(f"<p>{inner}</p>"))
        assert 1 < len(runs) <= dom_blocks.MAX_RUNS_PER_BLOCK
        assert any(r.get("formats") == ["bold"] for r in runs)

    def test_pathological_input_completes_quickly(self):
        inner = "".join(f"<strong>w{i}</strong>" for i in range(20_000))
        started = time.perf_counter()
        runs = dom_blocks.runs_from_tag(_tag(f"<p>{inner}</p>"))
        elapsed = time.perf_counter() - started
        assert len(runs) == 1
        assert elapsed < 5.0, f"walk is not bounded: {elapsed:.2f}s"


# --------------------------------------------------------------------------- #
# State ownership and module isolation                                        #
# --------------------------------------------------------------------------- #


class TestIsolation:
    def test_two_builders_share_neither_blocks_nor_seen_images(self):
        """One builder per article. A module-level singleton would leak blocks
        between articles and dedup one article's images against another's."""
        first = dom_blocks.BlockBuilder()
        first.walk(_soup('<p>One.</p><img src="https://x/a.jpg" />'))
        second = dom_blocks.BlockBuilder()
        second.walk(_soup('<p>Two.</p><img src="https://x/a.jpg" />'))
        assert [b["type"] for b in second.blocks] == ["paragraph", "image"]
        assert second.blocks[0]["text"] == "Two."
        assert len(first.blocks) == 2

    def test_module_imports_stay_leaf(self):
        """dom_blocks must remain a leaf of the import graph: no parser, no
        publisher, no news_bot, and — per Decision 6 — no feature_flags. The
        flag may not gate emission inside the shared module, because
        orangetrack is its first consumer and gating here would strip ITS
        blocks when the flag is off (a regression on a working source and an
        AC10 violation)."""
        source = open(
            os.path.join(_REPO_ROOT, "dom_blocks.py"), encoding="utf-8"
        ).read()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "feature_flags", "news_bot", "telegraph_publisher",
            "orangetrack_source", "t_hunted_source", "lamley_source",
            "autoevolution_source", "boilerplate_filter",
        }
        assert not (imported & forbidden), sorted(imported & forbidden)
