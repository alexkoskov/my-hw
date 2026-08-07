"""GOLDEN GATE for orangetrack rendering — do not regenerate casually.

``tests/fixtures/orangetrack_golden.json`` is a snapshot of what orangetrack
produced on UNMODIFIED code, taken before source-formatting-parity touched
anything. Editing that file IS the gate failure, not the fix.

WHY THIS EXISTS. The obvious gate — "the orangetrack tests still pass" — is
weak: ``grep`` for ``strong|<b>|<em>`` across the three parser test files
returns ZERO, so those tests are structurally blind to what this feature
changes. Output can drift while every one of them stays green. A byte-level
comparison is the only gate that actually holds.

THE ONE SANCTIONED CHANGE — TAKEN 2026-08-06. Task 6 applies the image limit
on the block path, so the ``gallery-20-images`` fixture lost ``figure`` nodes
in the preview tree (20 → 10). The operator agreed to that on two conditions:
the diff is shown BEFORE the deploy, and it shows FEWER IMAGES AND NOTHING
ELSE. Both were met — the recorded diff is in the feature's ``decisions.md``.
Any FURTHER difference is a regression, not part of the agreed deviation.

The baseline is rendered with orangetrack's own ``IMAGE_LIMIT``, the same
value ``news_bot`` resolves for it in production. Without that the baseline
would stop reflecting what actually ships, and the diff promised to the
operator would not exist.

THERE IS DELIBERATELY NO REGENERATION MODE — no ``--update`` flag, no
``REGEN`` env var, no helper script in the repo. If re-shooting the baseline
were one command away, it would be run reflexively and the gate would vanish
without a trace. Re-shooting must mean consciously writing code, so that it
shows up in a diff and gets reviewed.
"""

import json
import os

import pytest

from orangetrack_source import IMAGE_LIMIT, _parse_content_encoded
from telegraph_publisher import preview_nodes

# Paths resolve from __file__, not cwd: pytest is run both from the repo root
# and from tests/.
_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURE_DIR = os.path.join(_HERE, "fixtures", "articles", "orangetrack")
_GOLDEN_PATH = os.path.join(_HERE, "fixtures", "orangetrack_golden.json")

#: Same link for every fixture — the baseline must not depend on an
#: arbitrary string. Matches ``_make_entry`` in test_orangetrack_source.py.
LINK = "https://orangetrackdiecast.com/post-x"


def _load_golden() -> dict:
    with open(_GOLDEN_PATH, encoding="utf-8") as fh:
        return json.load(fh)["fixtures"]


def _fixture_names_on_disk() -> set[str]:
    return {
        name[: -len(".html")]
        for name in os.listdir(_FIXTURE_DIR)
        if name.endswith(".html")
    }


def _render(name: str) -> dict:
    """Run one fixture through the parser and the preview renderer."""
    with open(os.path.join(_FIXTURE_DIR, f"{name}.html"), encoding="utf-8") as fh:
        body = fh.read()
    parsed = _parse_content_encoded(body, LINK)
    if parsed is None:
        # Empty / unextractable bodies legitimately parse to None
        # (orangetrack_source.py:539-540, 852-853). The baseline records
        # null and the test asserts null — it must not crash.
        return {"parsed": None, "preview_nodes": None}
    return {
        "parsed": {
            "title": parsed.get("title"),
            "subtitle": parsed.get("subtitle"),
            "paragraphs": parsed.get("paragraphs"),
            "blocks": parsed.get("blocks"),
            "images": parsed.get("images"),
        },
        "preview_nodes": preview_nodes(
            parsed.get("title", ""),
            parsed.get("paragraphs"),
            parsed.get("images"),
            LINK,
            parsed.get("subtitle", ""),
            parsed.get("blocks"),
            image_limit=IMAGE_LIMIT,
        ),
    }


_GOLDEN = _load_golden()


@pytest.mark.parametrize("name", sorted(_GOLDEN), ids=sorted(_GOLDEN))
def test_golden_matches(name):
    """One case per fixture, so a failure names the culprit instead of
    reporting "something changed"."""
    expected = _GOLDEN[name]
    actual = _render(name)
    # Compare serialised strings rather than dicts: the failure message then
    # carries a readable text diff, which is the only thing the reviewer of
    # the Task 6 image-limit change will actually read.
    dump = dict(sort_keys=True, indent=2, ensure_ascii=False)
    assert json.dumps(actual["parsed"], **dump) == json.dumps(
        expected["parsed"], **dump
    ), f"parsed contract drifted for fixture {name!r}"
    assert json.dumps(actual["preview_nodes"], **dump) == json.dumps(
        expected["preview_nodes"], **dump
    ), f"preview node tree drifted for fixture {name!r}"


def test_fixture_set_matches_golden():
    """Without this, a fixture could be deleted and the run would stay green
    — the parametrisation would simply stop generating its case."""
    on_disk = _fixture_names_on_disk()
    in_golden = set(_GOLDEN)
    assert on_disk == in_golden, (
        f"fixture set drifted: only on disk={sorted(on_disk - in_golden)}, "
        f"only in golden={sorted(in_golden - on_disk)}"
    )


def test_gallery_fixture_exceeds_image_limit():
    """Guard for the operator's AC9/AC10 condition itself.

    Task 6 starts applying ``IMAGE_LIMIT`` on the block path, and the ONLY
    way to show the operator that change as a diff is to have a fixture that
    exceeds the limit. Without one, Task 6's diff comes out empty — which
    reads as "nothing changed" when in fact nothing was measured.

    The assertion is about PARSER BLOCKS, which stay at 20 either way; the
    slicing happens in the renderer. So this test stays valid on both sides
    of Task 6, and its render-side counterpart below is what moves.
    """
    parsed = _parse_content_encoded(
        open(
            os.path.join(_FIXTURE_DIR, "gallery-20-images.html"), encoding="utf-8"
        ).read(),
        LINK,
    )
    assert parsed is not None
    image_blocks = [b for b in parsed["blocks"] if b.get("type") == "image"]
    assert len(image_blocks) > IMAGE_LIMIT, (
        f"the AC9/AC10 gallery fixture yields {len(image_blocks)} image blocks "
        f"but IMAGE_LIMIT is {IMAGE_LIMIT} — Task 6 would produce an empty diff "
        "and the operator's condition could not be checked at all."
    )


def test_golden_records_todays_gallery_render_counts():
    """Pin the three numbers that made the Task 6 diff readable.

    Before Task 6: 20 image blocks / 10 flat images / 20 figure nodes.
    After Task 6:  20 image blocks / 10 flat images / **10** figure nodes.

    The parser side did not move and must not — the cap lives in the renderer.
    The third number was the ONLY one allowed to change, it changed by exactly
    the sanctioned amount, and it is now pinned at its new value: a cap that
    silently stops applying fails here.
    """
    summary = _GOLDEN["gallery-20-images"]["summary"]
    assert summary["blocks_by_type"]["image"] == 20
    assert summary["images_flat"] == 10
    assert summary["preview_figure_nodes"] == IMAGE_LIMIT == 10
