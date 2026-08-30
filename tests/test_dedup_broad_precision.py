"""Production-regression oracle for broad-pair dedup precision.

The fixture contains only public article metadata and the already-persisted
dedup fingerprints needed by the pure pair rule.  It must remain usable with
no database, network, environment secrets, or production configuration.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import news_bot
import pytest


_FIXTURE = Path(__file__).parent / "fixtures" / "dedup_broad_precision.json"
pytestmark = pytest.mark.skipif(
    not _FIXTURE.exists(),
    reason="production regression corpus is awaiting approved read-only export",
)
_PAIR_FIELDS = {"id", "label", "new", "candidate"}
_ARTICLE_FIELDS = {"public_id", "title", "source_name", "fingerprint"}
_FINGERPRINT_FIELDS = {"strict", "brands", "series", "pairs"}
_PROHIBITED_FIELD_PARTS = {
    "api_key",
    "chat_id",
    "credential",
    "database",
    "db_path",
    "fetched_at",
    "operator",
    "password",
    "published_at",
    "secret",
    "server",
    "token",
}
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _load_corpus() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, str):
        yield value


def _score_current_pair_rule(corpus: dict) -> Counter:
    score = Counter()
    for record in corpus["pairs"]:
        new = record["new"]
        candidate = record["candidate"]
        decision, _match = news_bot._pair_rule_verdict(
            new["fingerprint"]["pairs"],
            [{
                "link": candidate["public_id"],
                "source_name": candidate["source_name"],
                "model_fingerprint": candidate["fingerprint"],
            }],
        )
        score[f"{record['label']}_{'flagged' if decision != 'pass' else 'passed'}"] += 1
    return score


def test_production_corpus_integrity():
    corpus = _load_corpus()

    assert set(corpus) == {
        "schema_version",
        "source",
        "current_rule_expected",
        "pairs",
    }
    assert corpus["schema_version"] == 1
    assert corpus["source"] == "sanitized production dedup evidence"
    assert corpus["current_rule_expected"] == {
        "dupe_flagged": 3,
        "not_a_dupe_flagged": 21,
    }

    records = corpus["pairs"]
    assert len(records) == 24
    assert Counter(record["label"] for record in records) == {
        "dupe": 3,
        "not_a_dupe": 21,
    }
    assert [record["id"] for record in records] == [
        f"prod-{number:03d}" for number in range(1, 25)
    ]

    article_ids = []
    for record in records:
        assert set(record) == _PAIR_FIELDS
        assert record["label"] in {"dupe", "not_a_dupe"}
        for side_name in ("new", "candidate"):
            article = record[side_name]
            assert set(article) == _ARTICLE_FIELDS
            assert article["title"].strip()
            assert article["source_name"] in {
                "autoevolution",
                "lamley",
                "orangetrack",
                "t-hunted",
            }

            public_id = article["public_id"]
            parsed = urlsplit(public_id)
            assert parsed.scheme == "https"
            assert parsed.hostname
            assert parsed.username is None
            assert parsed.password is None
            assert not parsed.query
            assert not parsed.fragment
            article_ids.append(public_id)

            fingerprint = article["fingerprint"]
            assert set(fingerprint) == _FINGERPRINT_FIELDS
            for field in _FINGERPRINT_FIELDS:
                values = fingerprint[field]
                assert isinstance(values, list)
                assert values == sorted(set(values))
                assert all(isinstance(value, str) and value for value in values)

    assert all(
        record["new"]["public_id"] != record["candidate"]["public_id"]
        for record in records
    )
    assert len(set(record["id"] for record in records)) == len(records)
    assert len(set(article_ids)) < len(article_ids)  # hubs are intentionally reused

    serialized = json.dumps(corpus, ensure_ascii=False)
    assert not any(pattern.search(serialized) for pattern in _SECRET_SHAPES)
    for value in _walk(corpus):
        if isinstance(value, str):
            lowered = value.lower()
            assert not any(part in lowered for part in _PROHIBITED_FIELD_PARTS)


def test_current_pair_rule_corpus_baseline():
    corpus = _load_corpus()

    score = _score_current_pair_rule(corpus)

    assert score == Counter(corpus["current_rule_expected"])
