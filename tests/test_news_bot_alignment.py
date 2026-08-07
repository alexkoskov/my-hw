#!/usr/bin/env python3
"""Runtime guard: blocks whose count disagrees with `paragraphs` are dropped.

WHY. `_llm_common` pairs the two lists POSITIONALLY. `_build_user_message`
walks `blocks` and takes the next entry from `paragraphs` for each patchable
block; `_patch_text_with_ru_paragraphs` does the same with the Russian
paragraphs on the way back. Off by one and the translations splice shifted,
so the tail block ships to the channel in the source language. That outage
happened 2026-05-06. Both sides swallow the shortfall SILENTLY today —
`except StopIteration: pass` and a bare `break`, no log, no exception.

THE REAL FAILURE MODE OF THIS GUARD IS A FALSE POSITIVE. A guard that always
fires drops `blocks` on 100 % of publications and turns the whole feature off
without a sound — while every positive test stays green, because those only
assert "on a mismatch the blocks are dropped". The flag would read ON, the
operator would have changed nothing, and plain text would be going out. So the
false-positive controls below are not politeness; they are half the work, and
they are built on REAL parser output rather than hand-made dicts.
"""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _llm_common  # noqa: E402
import news_bot  # noqa: E402
import orangetrack_source  # noqa: E402

LINK = "https://orangetrackdiecast.com/post-x"


def _blocks(kinds):
    """Build blocks from a compact spec: 'p'=paragraph, 'h'=heading,
    'l'=list_item, 'i'=image, 'v'=video."""
    out = []
    for n, kind in enumerate(kinds):
        if kind == "p":
            out.append({"type": "paragraph", "text": f"para {n}", "runs": []})
        elif kind == "h":
            out.append({"type": "heading", "level": 3, "text": f"head {n}",
                        "runs": []})
        elif kind == "l":
            out.append({"type": "list_item", "text": f"item {n}", "runs": []})
        elif kind == "i":
            out.append({"type": "image", "src": f"https://x/{n}.jpg"})
        elif kind == "v":
            out.append({"type": "video", "src": f"https://telegra.ph/embed/y{n}"})
    return out


def _paras(n):
    return [f"para {i}" for i in range(n)]


# --------------------------------------------------------------------------- #
# Positive — the guard fires                                                  #
# --------------------------------------------------------------------------- #


def test_mismatch_drops_blocks():
    assert news_bot._blocks_if_aligned(LINK, _blocks("ppppp"), _paras(4)) is None


@pytest.mark.parametrize(
    "kinds, n_paras", [("ppppp", 4), ("ppp", 5)],
    ids=["more-blocks", "more-paragraphs"],
)
def test_mismatch_in_either_direction_drops_blocks(kinds, n_paras):
    """One-sided invariants are the 2026-07-28 lesson: a guard checked in only
    one direction passes while half the failures walk through."""
    assert news_bot._blocks_if_aligned(LINK, _blocks(kinds), _paras(n_paras)) is None


def test_mismatch_warning_names_link_and_both_counts(caplog):
    """The WARNING is the ONLY trace. Dropped blocks write no `last_error`, do
    not appear in the [E034] recap and send no ping (Decision 3b) — a message
    without the numbers leaves the operator with nothing to go on. The project
    learned this on 2026-06-10, when E011 fired, every external check was green
    and the real cause was only in the logs."""
    with caplog.at_level(logging.WARNING, logger="news_bot"):
        news_bot._blocks_if_aligned(LINK, _blocks("ppppp"), _paras(4))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert LINK in text
    assert "5" in text and "4" in text


def test_mismatch_sends_no_admin_notification():
    """Decision 3b — logged, not pinged. Pinned so a later agent does not
    "improve" the guard by adding an alert."""
    with patch.object(news_bot, "send_admin_notification") as ping:
        news_bot._blocks_if_aligned(LINK, _blocks("ppppp"), _paras(4))
    ping.assert_not_called()


def test_list_item_counts_as_patchable_on_mismatch():
    """Alignment that rests only on list_item blocks is still alignment."""
    assert news_bot._blocks_if_aligned(LINK, _blocks("pll"), _paras(3)) is not None
    assert news_bot._blocks_if_aligned(LINK, _blocks("pll"), _paras(2)) is None


def test_heading_counts_as_patchable_on_mismatch():
    assert news_bot._blocks_if_aligned(LINK, _blocks("phh"), _paras(3)) is not None
    assert news_bot._blocks_if_aligned(LINK, _blocks("phh"), _paras(2)) is None


# --------------------------------------------------------------------------- #
# False-positive controls — the guard stays silent (the other half)           #
# --------------------------------------------------------------------------- #


def test_real_orangetrack_article_stays_aligned(caplog):
    """THE test that catches an always-firing guard. Runs the REAL parser
    rather than a hand-built dict: a dict would only prove the arithmetic,
    while what has to be proven is that the guard is silent on what a source
    actually emits."""
    entry = {
        "link": LINK,
        "title": "Sample Title",
        "content": [{"value": (
            "<p>The first paragraph introduces the casting.</p>"
            "<p>The second paragraph explains its rarity.</p>"
            '<figure><img src="https://orangetrackdiecast.com/i.jpg" /></figure>'
            "<p>A third paragraph after the image.</p>"
        )}],
        "summary": "",
        "published": "Mon, 01 Jan 2025 00:00:00 +0000",
    }
    article = orangetrack_source.fetch_orangetrack_article(entry)
    assert article is not None and article["blocks"]

    with caplog.at_level(logging.WARNING, logger="news_bot"):
        kept = news_bot._blocks_if_aligned(
            LINK, article["blocks"], article["paragraphs"],
        )
    assert kept is article["blocks"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_aligned_article_logs_no_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="news_bot"):
        kept = news_bot._blocks_if_aligned(LINK, _blocks("pphi"), _paras(3))
    assert kept is not None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize("blocks", [None, []], ids=["none", "empty"])
def test_article_without_blocks_is_not_flagged_as_mismatch(blocks, caplog):
    """mattel, lamley before Phase 2, and any source with the flag off. A
    WARNING here would land on every single publication without blocks."""
    with caplog.at_level(logging.WARNING, logger="news_bot"):
        assert news_bot._blocks_if_aligned(LINK, blocks, _paras(3)) in (None, [])
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_video_only_post_with_synthesized_paragraph_stays_aligned(caplog):
    """orangetrack synthesizes `paragraphs = [title]` for a video-only post
    when there are ZERO patchable blocks. A naive `len(patchable) != len(paras)`
    fires here, drops the blocks and LOSES THE VIDEO from the page — a
    regression on a working source and an AC10 violation."""
    blocks = _blocks("vi")
    with caplog.at_level(logging.WARNING, logger="news_bot"):
        kept = news_bot._blocks_if_aligned(LINK, blocks, ["Synthesized title"])
    assert kept is blocks
    assert any(b["type"] == "video" for b in kept)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_media_only_blocks_do_not_count_as_patchable_mismatch(caplog):
    with caplog.at_level(logging.WARNING, logger="news_bot"):
        kept = news_bot._blocks_if_aligned(LINK, _blocks("piiivv"), _paras(1))
    assert kept is not None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# --------------------------------------------------------------------------- #
# Wiring and robustness                                                       #
# --------------------------------------------------------------------------- #


def test_mismatch_detection_uses_llm_common_patched_types(monkeypatch):
    """Counted through `_llm_common._PATCHED_TEXT_BLOCK_TYPES`, the tuple BOTH
    sides of the pairing actually read — not a literal retyped in news_bot,
    which is the very drift this guard exists to catch."""
    monkeypatch.setattr(_llm_common, "_PATCHED_TEXT_BLOCK_TYPES", ("paragraph",))
    # With only `paragraph` patchable, the two headings stop counting, so 1
    # patchable block against 1 paragraph is now aligned.
    assert news_bot._blocks_if_aligned(LINK, _blocks("phh"), _paras(1)) is not None


def test_mismatch_check_failure_is_fail_open_and_keeps_blocks(caplog):
    """Fail-open, same contract as the promo filter, the content gate and the
    dedup gate: a broken check must not take the article down."""
    class Exploding(list):
        def __iter__(self):
            raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING, logger="news_bot"):
        kept = news_bot._blocks_if_aligned(LINK, Exploding(), _paras(3))
    assert kept is not None  # article keeps its blocks rather than being lost


def test_mismatch_stores_sql_null_not_empty_list(tmp_path):
    """`None` and `[]` are different downstream: `_dumps` stores None as SQL
    NULL and `[]` as the string '[]', which reads back as "blocks are empty"
    rather than "there are no blocks" — a different path in the publisher."""
    import sqlite3

    import pending_articles_repo as repo

    db = str(tmp_path / "t.db")
    with sqlite3.connect(db) as conn:
        repo.init_schema(conn)

    row = {
        "link": LINK, "source_name": "orangetrack", "feed_url": "f",
        "title": "T", "subtitle": "", "paragraphs": _paras(4),
        "images": [], "blocks": news_bot._blocks_if_aligned(
            LINK, _blocks("ppppp"), _paras(4)),
        "pub_date": "", "model_fingerprint": None,
    }
    assert row["blocks"] is None

    with patch.object(news_bot, "DB_FILE", db):
        assert repo.insert_pending(row)
        stored = repo.get_pending(LINK)

    assert stored is not None
    assert stored["blocks"] is None
    with sqlite3.connect(db) as conn:
        raw = conn.execute(
            "SELECT blocks FROM pending_articles WHERE link = ?", (LINK,)
        ).fetchone()[0]
    assert raw is None, f"stored as {raw!r} instead of SQL NULL"
