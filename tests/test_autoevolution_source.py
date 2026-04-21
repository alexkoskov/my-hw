#!/usr/bin/env python3
"""Unit tests for autoevolution_source.enrich_entry."""

import pytest

from autoevolution_source import enrich_entry


class TestEnrichEntry:
    def test_basic_entry_with_summary_and_images(self):
        entry = {
            "title": "Hot Wheels Pickup",
            "summary": "A cool new pickup truck.",
            "media_content": [{"url": "https://s1.cdn.example/1.jpg", "medium": "image"}],
            "media_thumbnail": [{"url": "https://s1.cdn.example/2.jpg"}],
        }
        out = enrich_entry(entry)
        assert out["title"] == "Hot Wheels Pickup"
        assert out["paragraphs"] == ["A cool new pickup truck."]
        assert out["images"] == [
            "https://s1.cdn.example/1.jpg",
            "https://s1.cdn.example/2.jpg",
        ]

    def test_strips_continue_reading_link(self):
        entry = {
            "title": "T",
            "summary": 'Body text here. (<a href="https://example.com">continue reading...</a>)',
        }
        out = enrich_entry(entry)
        assert "continue reading" not in out["paragraphs"][0]
        assert "<a" not in out["paragraphs"][0]
        assert out["paragraphs"][0].startswith("Body text here.")

    def test_html_entities_decoded(self):
        entry = {"title": "T", "summary": "Ford &amp; Chevy"}
        out = enrich_entry(entry)
        assert out["paragraphs"] == ["Ford & Chevy"]

    def test_multiple_paragraphs_split_on_double_newline(self):
        entry = {"title": "T", "summary": "First paragraph.\n\nSecond paragraph."}
        out = enrich_entry(entry)
        assert out["paragraphs"] == ["First paragraph.", "Second paragraph."]

    def test_dedupes_images_across_media_fields(self):
        entry = {
            "title": "T",
            "summary": "x",
            "media_content": [{"url": "https://a.jpg"}],
            "media_thumbnail": [{"url": "https://a.jpg"}],
        }
        out = enrich_entry(entry)
        assert out["images"] == ["https://a.jpg"]

    def test_no_summary_falls_back_to_title(self):
        entry = {"title": "Just a title"}
        out = enrich_entry(entry)
        assert out["paragraphs"] == ["Just a title"]

    def test_returns_none_for_empty_entry(self):
        assert enrich_entry({}) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
