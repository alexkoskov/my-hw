"""Regressions for retry state and source-text fidelity."""

import json
import sqlite3
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

import dom_blocks
import model_extractor
import news_bot
import pending_articles_repo as repo


@pytest.fixture
def database(tmp_path, monkeypatch):
    path = tmp_path / "queue.db"
    monkeypatch.setattr(news_bot, "DB_FILE", str(path))
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE processed_news (link TEXT PRIMARY KEY, title TEXT, "
            "pub_date TEXT, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        repo.init_schema(conn)
    return path


@pytest.mark.parametrize("ru_paragraphs", [["Русский текст статьи"], []])
def test_retry_preserves_translation_fingerprint_and_page(database, ru_paragraphs):
    fingerprint = {
        "strict": ["porsche 911"], "brands": ["porsche"],
        "series": ["k-pop demon hunters"],
        "pairs": ["porsche 911|k-pop demon hunters|D"],
    }
    entry = {
        "link": "https://example.com/retry", "source_name": "lamley",
        "title": "Porsche 911 K-Pop Demon Hunters", "paragraphs": ["EN body"],
        "images": ["https://example.com/car.jpg"],
        "model_fingerprint": fingerprint,
    }
    assert repo.insert_pending(entry)
    ru_blocks = [{"type": "image", "src": entry["images"][0]}]
    repo.update_staged(entry["link"], "Русский заголовок", "Подзаголовок",
                       ru_paragraphs, ru_blocks)
    repo.mark_telegraph_published(entry["link"], "https://telegra.ph/saved", "saved")
    repo.move_to_failed(entry["link"], "teaser unavailable")
    failed = repo.get_failed(entry["link"])
    assert failed["model_fingerprint"] == fingerprint
    assert failed["ru_paragraphs"] == ru_paragraphs

    with patch.object(model_extractor, "extract_fingerprint",
                      side_effect=AssertionError("must not recompute saved fp")):
        assert repo.retry_from_failed(entry["link"])
    pending = repo.get_pending(entry["link"])
    assert pending["ru_title"] == "Русский заголовок"
    assert pending["ru_subtitle"] == "Подзаголовок"
    assert pending["ru_paragraphs"] == ru_paragraphs
    assert pending["ru_blocks"] == ru_blocks
    assert pending["model_fingerprint"] == fingerprint
    assert pending["telegraph_url"] == "https://telegra.ph/saved"
    assert pending["telegraph_path"] == "saved"
    assert pending["attempt_count"] == 0
    repo.move_to_published(entry["link"], "https://telegra.ph/saved", "saved", False)
    assert json.loads(repo.get_published(entry["link"])["model_fingerprint"]) == fingerprint
    with sqlite3.connect(database) as conn, patch.object(news_bot, "DEDUP_SERIES_ENABLED", True):
        decision, match, _ = news_bot._check_cross_source_dedup(
            entry["title"], fingerprint, conn, new_source="t_hunted",
        )
    assert decision == "block"
    assert match["link"] == entry["link"]


def test_legacy_failed_row_migrates_and_recovers_only_missing_fingerprint(database):
    with sqlite3.connect(database) as conn:
        # Recreate the old table shape with an actual row before migration.
        conn.execute("DROP TABLE failed_articles")
        conn.execute(repo._FAILED_DDL)
        conn.execute(
            "INSERT INTO failed_articles (link,title,source_name,paragraphs) "
            "VALUES (?,?,?,?)",
            ("https://example.com/legacy", "Porsche 911", "lamley", '["body"]'),
        )
        repo.init_schema(conn)
    assert repo.get_failed("https://example.com/legacy")["ru_title"] is None
    assert repo.retry_from_failed("https://example.com/legacy")
    restored = repo.get_pending("https://example.com/legacy")
    assert restored["ru_paragraphs"] is None
    assert restored["model_fingerprint"]["strict"] == ["porsche 911"]


@pytest.mark.parametrize("separator", [" and ", " e ", " ", ", "])
def test_adjacent_models_are_both_extracted(separator):
    fingerprint = model_extractor.extract_fingerprint({
        "title": f"Porsche 911{separator}Toyota Supra",
    })
    assert fingerprint["strict"] == ["porsche 911", "toyota supra"]
    assert fingerprint["brands"] == ["porsche", "toyota"]


@pytest.mark.parametrize("title, expected", [
    ("Subaru Legacy GT and Toyota Supra", ["subaru legacy gt", "toyota supra"]),
    ("Chevy Camaro Z28 VW Golf GTI", ["chevrolet camaro z28", "volkswagen golf gti"]),
    ("Toyota Ford Mustang", ["ford mustang"]),
    ("Land Rover Defender Range Rover Sport", ["land rover defender", "range rover sport"]),
])
def test_adjacent_brand_scan_preserves_composite_models(title, expected):
    assert model_extractor.extract_fingerprint({"title": title})["strict"] == expected


@pytest.mark.parametrize("href", ["https://example.com", "javascript:alert(1)"])
@pytest.mark.parametrize("markup, expected", [
    ('See<a href="{href}"> this </a>now', "See this now"),
    ('See <a href="{href}"> this </a> now', "See this now"),
    ('See <a href="{href}">this</a>.', "See this."),
    ('<a href="{href}">one </a><a href="{href}">two</a>', "one two"),
])
def test_anchor_spaces_remain_source_text(href, markup, expected):
    tag = BeautifulSoup(f"<p>{markup.format(href=href)}</p>", "html.parser").p
    runs = dom_blocks.runs_from_tag(tag)
    assert dom_blocks.text_from_runs(runs) == expected


@pytest.mark.parametrize("markup", [
    "<strong>Alphard<br>CC850</strong>",
    "<span><strong>Alphard<br><br>CC850</strong></span>",
    '<a href="https://example.com">Alphard<br>CC850</a>',
    "<strong><br>Alphard<br></strong><em>CC850</em><br>",
])
def test_nested_breaks_split_without_mutating_source(markup):
    soup = BeautifulSoup(f"<p>{markup}</p>", "html.parser")
    original = str(soup)
    builder = dom_blocks.BlockBuilder()
    builder.walk(soup)
    assert [block["text"] for block in builder.blocks] == ["Alphard", "CC850"]
    assert str(soup) == original
    if "strong" in markup:
        assert builder.blocks[0]["runs"][0]["formats"] == ["bold"]
    if "<a " in markup:
        assert all(block["runs"][0]["href"] == "https://example.com"
                   for block in builder.blocks)


def test_nested_break_in_list_keeps_a_word_boundary():
    soup = BeautifulSoup("<ul><li><strong>Alphard<br>CC850</strong></li></ul>",
                         "html.parser")
    builder = dom_blocks.BlockBuilder()
    builder.walk(soup)
    assert [b["text"] for b in builder.blocks] == ["Alphard CC850"]
