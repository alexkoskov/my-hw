"""Production-regression oracle for broad-pair dedup precision.

The fixture contains only public article metadata and the already-persisted
dedup fingerprints needed by the pure pair rule.  It must remain usable with
no database, network, environment secrets, or production configuration.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import news_bot
import pytest


_FIXTURE = Path(__file__).parent / "fixtures" / "dedup_broad_precision.json"
_RAW_EVIDENCE = (
    Path(__file__).parents[1]
    / "work"
    / "dedup-broad-precision"
    / "corpus-raw"
    / "dedup-pairs.json"
)
_PAIR_FIELDS = {"id", "label", "new", "candidate"}
_RAW_PAIR_FIELDS = {"label", "new", "candidate"}
_ARTICLE_FIELDS = {"public_id", "title", "source_name", "fingerprint"}
_FINGERPRINT_FIELDS = {"strict", "brands", "series", "pairs"}
_EXPECTED_BASELINE = {"dupe_flagged": 3, "not_a_dupe_flagged": 21}
_DUPLICATE_IDS = {"prod-003", "prod-023", "prod-024"}
_SOURCE_HOSTS = {
    "autoevolution": {"www.autoevolution.com"},
    "lamley": {"lamleygroup.com", "www.lamleygroup.com"},
    "orangetrack": {"orangetrackdiecast.com"},
    "t-hunted": {"t-hunted.blogspot.com"},
}
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
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b(?:xox[baprs]-|sk_live_)[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\."
        r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    ),
    re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://\S+",
        re.I,
    ),
    re.compile(r"(?<!\d)-?\d{8,15}(?!\d)"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
_CANONICAL_TOKEN = re.compile(r"[a-z0-9]+(?:[ -][a-z0-9]+)*\Z")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _validate_fingerprint(fingerprint: dict) -> None:
    assert set(fingerprint) == _FINGERPRINT_FIELDS
    for field in _FINGERPRINT_FIELDS:
        values = fingerprint[field]
        assert isinstance(values, list)
        assert values == sorted(set(values))
        assert all(isinstance(value, str) and value for value in values)

    for field in ("strict", "brands", "series"):
        assert all(
            len(value) <= 100 and _CANONICAL_TOKEN.fullmatch(value)
            for value in fingerprint[field]
        )

    for pair in fingerprint["pairs"]:
        assert len(pair) <= 220
        parts = pair.split("|")
        assert len(parts) == 3
        model, series, tier = parts
        assert model == "*" or _CANONICAL_TOKEN.fullmatch(model)
        assert _CANONICAL_TOKEN.fullmatch(series)
        assert tier in {"B", "D"}
        assert model == "*" or model in fingerprint["strict"]
        assert series in fingerprint["series"]


def _validate_article(article: dict) -> None:
    assert set(article) == _ARTICLE_FIELDS
    assert article["title"].strip() == article["title"]
    assert article["title"]
    assert article["source_name"] in _SOURCE_HOSTS

    public_id = article["public_id"]
    parsed = urlsplit(public_id)
    assert public_id.strip() == public_id
    assert parsed.scheme == "https"
    assert parsed.hostname in _SOURCE_HOSTS[article["source_name"]]
    assert parsed.port is None
    assert parsed.username is None
    assert parsed.password is None
    assert not parsed.query
    assert not parsed.fragment
    _validate_fingerprint(article["fingerprint"])


def _validate_records(records: list, pair_fields: set[str]) -> None:
    assert len(records) == 24
    assert Counter(record["label"] for record in records) == {
        "dupe": 3,
        "not_a_dupe": 21,
    }

    article_ids = []
    for record in records:
        assert set(record) == pair_fields
        assert record["label"] in {"dupe", "not_a_dupe"}
        for side_name in ("new", "candidate"):
            _validate_article(record[side_name])
            article_ids.append(record[side_name]["public_id"])
        assert record["new"]["public_id"] != record["candidate"]["public_id"]
    assert len(set(article_ids)) < len(article_ids)


def _validate_no_private_data(corpus: dict) -> None:
    serialized = json.dumps(corpus, ensure_ascii=False)
    assert not any(pattern.search(serialized) for pattern in _SECRET_SHAPES)
    for value in _walk(corpus):
        if isinstance(value, str):
            lowered = value.lower()
            assert not any(part in lowered for part in _PROHIBITED_FIELD_PARTS)


def _validate_raw_corpus(corpus: dict) -> None:
    assert set(corpus) == {"schema_version", "source", "pairs"}
    assert corpus["schema_version"] == 1
    assert corpus["source"] == "sanitized production dedup evidence"
    _validate_records(corpus["pairs"], _RAW_PAIR_FIELDS)
    _validate_no_private_data(corpus)


def _validate_fixture(corpus: dict) -> None:
    assert set(corpus) == {
        "schema_version",
        "source",
        "current_rule_expected",
        "pairs",
    }
    assert corpus["schema_version"] == 1
    assert corpus["source"] == "sanitized production dedup evidence"
    assert corpus["current_rule_expected"] == _EXPECTED_BASELINE
    records = corpus["pairs"]
    _validate_records(records, _PAIR_FIELDS)
    assert [record["id"] for record in records] == [
        f"prod-{number:03d}" for number in range(1, 25)
    ]
    assert {record["id"] for record in records if record["label"] == "dupe"} == (
        _DUPLICATE_IDS
    )
    _validate_no_private_data(corpus)


def _score_current_pair_rule(corpus: dict) -> tuple[Counter, dict[str, str]]:
    score = Counter()
    decisions = {}
    for record in corpus["pairs"]:
        new = record["new"]
        candidate = record["candidate"]
        decision, _match, _suppressed = news_bot._pair_rule_verdict(
            new["fingerprint"]["pairs"],
            [{
                "link": candidate["public_id"],
                "source_name": candidate["source_name"],
                "title": candidate["title"],
                "model_fingerprint": candidate["fingerprint"],
            }],
            new["title"],
        )
        score[f"{record['label']}_{decision}"] += 1
        decisions[record["id"]] = decision
    return score, decisions


def test_production_corpus_integrity():
    raw = _load_json(_RAW_EVIDENCE)
    fixture = _load_json(_FIXTURE)

    _validate_raw_corpus(raw)
    _validate_fixture(fixture)

    derived = {
        "schema_version": raw["schema_version"],
        "source": raw["source"],
        "current_rule_expected": _EXPECTED_BASELINE,
        "pairs": [
            {"id": f"prod-{number:03d}", **record}
            for number, record in enumerate(raw["pairs"], start=1)
        ],
    }
    assert fixture == derived


def test_production_corpus_meets_precision_target():
    corpus = _load_json(_FIXTURE)

    score, decisions = _score_current_pair_rule(corpus)

    assert score["dupe_flag"] == 3, decisions
    assert score["dupe_block"] == 0, decisions
    assert score["dupe_pass"] == 0, decisions
    assert score["not_a_dupe_flag"] <= 1, decisions
    assert score["not_a_dupe_block"] == 0, decisions
    assert score["not_a_dupe_flag"] + score["not_a_dupe_pass"] == 21, decisions


_CAR_CULTURE_PAIR = "toyota supra|car culture|B"
_GATE_CAR_CULTURE_PAIR = "toyota 4runner|car culture|B"
_BOULEVARD_PAIR = "ferrari enzo|boulevard|B"
_RLC_PAIR = "porsche 911|red line club|B"
_DISTINCTIVE_PAIR = "porsche 911|k-pop demon hunters|D"


def _pair_candidate(link, title, pairs, *, source="t-hunted"):
    parsed = [pair.split("|") for pair in pairs]
    return {
        "link": link,
        "source_name": source,
        "title": title,
        "model_fingerprint": {
            "strict": sorted({model for model, _series, _tier in parsed if model != "*"}),
            "brands": [],
            "series": sorted({series for _model, series, _tier in parsed}),
            "pairs": list(pairs),
        },
    }


@pytest.mark.parametrize(
    (
        "new_title",
        "candidate_title",
        "pairs",
        "expected_decision",
        "expected_pairs",
        "expected_suppressed_series",
    ),
    [
        (
            "Toyota Supra joins Car Culture",
            "New Car Culture mix revealed",
            [_CAR_CULTURE_PAIR],
            "flag",
            [_CAR_CULTURE_PAIR],
            [],
        ),
        (
            "Toyota Supra joins Car Culture",
            "Ten affordable cars for July",
            [_CAR_CULTURE_PAIR],
            "pass",
            [],
            ["car culture"],
        ),
        (
            "Ten affordable cars for July",
            "New Car Culture mix revealed",
            [_CAR_CULTURE_PAIR],
            "pass",
            [],
            ["car culture"],
        ),
        (
            "Car Culture mix revealed",
            "",
            [_CAR_CULTURE_PAIR],
            "pass",
            [],
            ["car culture"],
        ),
        (
            "Novo lote da série Car Culture",
            "Mais fotos do lote Car Culture",
            [_CAR_CULTURE_PAIR],
            "flag",
            [_CAR_CULTURE_PAIR],
            [],
        ),
        (
            "New RLC exclusive Porsche 911",
            "Red Line Club release revealed",
            [_RLC_PAIR],
            "flag",
            [_RLC_PAIR],
            [],
        ),
        (
            "the rlc meeting was quiet",
            "RLC exclusive Porsche 911",
            [_RLC_PAIR],
            "pass",
            [],
            ["red line club"],
        ),
        (
            "Ferrari Enzo leads the Boulevard mix",
            "Boulevard Mix 4 unboxing",
            [_CAR_CULTURE_PAIR, _BOULEVARD_PAIR],
            "flag",
            [_BOULEVARD_PAIR],
            ["car culture"],
        ),
    ],
)
def test_broad_pair_requires_series_in_both_titles(
    new_title,
    candidate_title,
    pairs,
    expected_decision,
    expected_pairs,
    expected_suppressed_series,
):
    candidate = _pair_candidate("https://candidate.example/article", candidate_title, pairs)

    decision, match, suppressed = news_bot._pair_rule_verdict(
        pairs,
        [candidate],
        new_title,
    )

    assert decision == expected_decision
    assert (match or {}).get("pairs", []) == expected_pairs
    if match is not None:
        assert match["reason"] == "broad_subject"
    assert [item["series"] for item in suppressed] == (
        [expected_suppressed_series] if expected_suppressed_series else []
    )


@pytest.mark.parametrize(
    "candidate_fingerprint",
    [
        None,
        "not a fingerprint",
        [],
        {},
        {"strict": ["porsche 911"], "brands": ["porsche"]},
        {"pairs": ["malformed"]},
        {
            "strict": ["porsche 911"],
            "brands": [],
            "series": ["k-pop demon hunters"],
            "pairs": True,
        },
        {
            "strict": True,
            "brands": [],
            "series": ["k-pop demon hunters"],
            "pairs": [_DISTINCTIVE_PAIR],
        },
        {"pairs": [_DISTINCTIVE_PAIR]},
        {
            "strict": [],
            "brands": [],
            "series": [],
            "pairs": [_DISTINCTIVE_PAIR],
        },
    ],
)
def test_pair_rule_skips_legacy_or_malformed_candidate_pairs(candidate_fingerprint):
    candidate = {
        "link": "https://candidate.example/article",
        "source_name": "t-hunted",
        "title": "Car Culture release",
        "model_fingerprint": candidate_fingerprint,
    }

    assert news_bot._pair_rule_verdict(
        [_DISTINCTIVE_PAIR],
        [candidate],
        "K-Pop Demon Hunters release",
    ) == ("pass", None, [])


@pytest.mark.parametrize("candidate_title", [None, b"Car Culture release"])
def test_pair_rule_treats_missing_or_non_text_candidate_title_as_subject_rejection(
    candidate_title,
):
    candidate = _pair_candidate(
        "https://candidate.example/article",
        "Car Culture release",
        [_CAR_CULTURE_PAIR],
    )
    if candidate_title is None:
        candidate.pop("title")
    else:
        candidate["title"] = candidate_title

    decision, match, suppressed = news_bot._pair_rule_verdict(
        [_CAR_CULTURE_PAIR],
        [candidate],
        "Toyota Supra joins Car Culture",
    )

    assert decision == "pass"
    assert match is None
    assert [item["series"] for item in suppressed] == [["car culture"]]


def test_distinctive_verdict_preserves_the_full_shared_pair_payload():
    pairs = [_DISTINCTIVE_PAIR, _RLC_PAIR]
    candidate = _pair_candidate(
        "https://candidate.example/distinctive",
        "K-Pop Demon Hunters RLC release",
        pairs,
    )

    decision, match, suppressed = news_bot._pair_rule_verdict(
        pairs,
        [candidate],
        "K-Pop Demon Hunters RLC release",
    )

    assert decision == "block"
    assert match["pairs"] == sorted(pairs)
    assert match["models"] == sorted(pairs)
    assert match["n_matches"] == 2
    assert suppressed == []


def test_suppression_diagnostics_are_bounded_and_sanitized():
    secret = "AKIA" + "A" * 16
    candidates = []
    for index in range(news_bot._DEDUP_SUPPRESSION_MAX_RECORDS + 5):
        candidate = _pair_candidate(
            f"https://user:password@example.com/article/{index}?token=secret",
            f"General roundup\nforged {secret}",
            [_CAR_CULTURE_PAIR],
            source="source\nforged",
        )
        candidates.append(candidate)

    decision, match, suppressed = news_bot._pair_rule_verdict(
        [_CAR_CULTURE_PAIR],
        candidates,
        "General roundup",
    )

    assert decision == "pass"
    assert match is None
    assert len(suppressed) == news_bot._DEDUP_SUPPRESSION_MAX_RECORDS
    serialized = json.dumps(suppressed)
    assert "password" not in serialized
    assert "token=secret" not in serialized
    assert secret not in serialized
    assert all(
        not unicodedata.category(char).startswith("C")
        for item in suppressed
        for field in ("link", "source_name", "title")
        for char in item[field]
    )
    assert all(
        len(item["title"]) <= news_bot._DEDUP_DIAGNOSTIC_TITLE_MAXLEN
        and len(item["source_name"]) <= news_bot._DEDUP_DIAGNOSTIC_SOURCE_MAXLEN
        for item in suppressed
    )

    many_pairs = [
        f"model {index}|series {index}|B"
        for index in range(news_bot._DEDUP_SUPPRESSION_MAX_SERIES + 5)
    ]
    _decision, _match, many_series = news_bot._pair_rule_verdict(
        many_pairs,
        [_pair_candidate(
            "https://candidate.example/many-series",
            "General roundup",
            many_pairs,
        )],
        "General roundup",
    )
    assert len(many_series[0]["series"]) == news_bot._DEDUP_SUPPRESSION_MAX_SERIES


@pytest.mark.parametrize(
    ("candidate_order", "expected_decision", "expected_link", "suppressed_links"),
    [
        (
            ["suppressed", "qualified"],
            "flag",
            "https://candidate.example/qualified",
            ["https://candidate.example/suppressed"],
        ),
        (
            ["qualified", "distinctive"],
            "block",
            "https://candidate.example/distinctive",
            [],
        ),
        (
            ["suppressed", "distinctive"],
            "block",
            "https://candidate.example/distinctive",
            ["https://candidate.example/suppressed"],
        ),
    ],
)
def test_pair_scan_preserves_later_stronger_verdicts(
    candidate_order,
    expected_decision,
    expected_link,
    suppressed_links,
):
    candidates = {
        "suppressed": _pair_candidate(
            "https://candidate.example/suppressed",
            "Ten affordable cars",
            [_CAR_CULTURE_PAIR],
        ),
        "qualified": _pair_candidate(
            "https://candidate.example/qualified",
            "Car Culture mix revealed",
            [_CAR_CULTURE_PAIR],
        ),
        "distinctive": _pair_candidate(
            "https://candidate.example/distinctive",
            "Unrelated title",
            [_DISTINCTIVE_PAIR],
        ),
    }

    decision, match, suppressed = news_bot._pair_rule_verdict(
        [_CAR_CULTURE_PAIR, _DISTINCTIVE_PAIR],
        [candidates[name] for name in candidate_order],
        "Car Culture roundup",
    )

    assert decision == expected_decision
    assert match["link"] == expected_link
    assert [item["link"] for item in suppressed] == suppressed_links


def _gate_fingerprint(strict, *, pairs=(_GATE_CAR_CULTURE_PAIR,)):
    return {
        "strict": list(strict),
        "brands": [],
        "series": ["car culture"] if pairs else [],
        "pairs": list(pairs),
    }


@pytest.mark.parametrize(
    (
        "new_strict",
        "candidate_strict",
        "candidate_pairs",
        "candidate_source",
        "expected_decision",
        "expected_reason",
        "expected_suppressed",
    ),
    [
        (
            ["toyota 4runner", "subaru legacy gt"],
            ["toyota 4runner", "subaru legacy gt"],
            [_GATE_CAR_CULTURE_PAIR],
            "t-hunted",
            "flag",
            "overlap_capped",
            True,
        ),
        (
            ["toyota 4runner", "subaru legacy gt", "honda civic"],
            ["toyota 4runner", "subaru legacy gt", "mazda mx-5", "nissan skyline"],
            [_GATE_CAR_CULTURE_PAIR],
            "t-hunted",
            "flag",
            "overlap",
            True,
        ),
        (
            ["toyota 4runner", "subaru legacy gt", "honda civic", "mazda mx-5"],
            ["toyota 4runner", "ford mustang", "nissan skyline", "porsche 911"],
            [_GATE_CAR_CULTURE_PAIR],
            "t-hunted",
            "pass",
            None,
            True,
        ),
        (
            ["toyota 4runner", "subaru legacy gt"],
            ["toyota 4runner", "subaru legacy gt"],
            [_GATE_CAR_CULTURE_PAIR],
            "autoevolution",
            "pass",
            None,
            True,
        ),
        (
            ["toyota 4runner", "subaru legacy gt"],
            ["toyota 4runner", "subaru legacy gt"],
            [],
            "t-hunted",
            "block",
            "overlap",
            False,
        ),
    ],
)
def test_subject_rejection_caps_only_backstop_block(
    new_strict,
    candidate_strict,
    candidate_pairs,
    candidate_source,
    expected_decision,
    expected_reason,
    expected_suppressed,
):
    fingerprint = _gate_fingerprint(new_strict)
    candidate = {
        "link": "https://candidate.example/article",
        "source_name": candidate_source,
        "title": "Ten affordable cars",
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "model_fingerprint": _gate_fingerprint(
            candidate_strict,
            pairs=candidate_pairs,
        ),
    }

    with patch.object(news_bot, "_fetch_dedup_candidates", return_value=[candidate]):
        decision, match, suppressed = news_bot._check_cross_source_dedup(
            "Unrelated roundup",
            fingerprint,
            object(),
            new_source="autoevolution",
        )

    assert decision == expected_decision
    assert (match or {}).get("reason") == expected_reason
    assert bool(suppressed) is expected_suppressed
    if expected_reason == "overlap_capped":
        assert match["subject_rejected_series"] == ["car culture"]


@pytest.mark.parametrize(
    (
        "raw_similarity",
        "candidate_pairs",
        "expected_decision",
        "expected_reason",
        "expected_suppressed",
    ),
    [
        (0.299999, [_GATE_CAR_CULTURE_PAIR], "pass", None, True),
        (0.30, [_GATE_CAR_CULTURE_PAIR], "flag", "overlap", True),
        (0.499999, [_GATE_CAR_CULTURE_PAIR], "flag", "overlap", True),
        (0.50, [_GATE_CAR_CULTURE_PAIR], "flag", "overlap_capped", True),
        (0.50, [], "block", "overlap", False),
    ],
)
def test_backstop_threshold_boundaries_and_subject_cap(
    raw_similarity,
    candidate_pairs,
    expected_decision,
    expected_reason,
    expected_suppressed,
):
    fingerprint = _gate_fingerprint(
        ["toyota 4runner", "honda civic", "ford mustang"],
    )
    candidate = {
        "link": "https://candidate.example/boundary",
        "source_name": "t-hunted",
        "title": "General diecast roundup",
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "model_fingerprint": _gate_fingerprint(
            ["toyota 4runner", "mazda mx-5", "porsche 911"],
            pairs=candidate_pairs,
        ),
    }

    with (
        patch.object(news_bot, "_fetch_dedup_candidates", return_value=[candidate]),
        patch.object(
            news_bot.model_extractor,
            "similarity",
            return_value=raw_similarity,
        ),
    ):
        decision, match, suppressed = news_bot._check_cross_source_dedup(
            "General diecast roundup",
            fingerprint,
            object(),
            new_source="autoevolution",
        )

    assert decision == expected_decision
    assert (match or {}).get("reason") == expected_reason
    assert bool(suppressed) is expected_suppressed
    if expected_reason == "overlap_capped":
        assert match["subject_rejected_series"] == ["car culture"]
    else:
        assert "subject_rejected_series" not in (match or {})


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://10.0.0.5/private",
        "https://localhost/private",
        "https://www.autoevolution.com:8443/private",
        "https://t-hunted.blogspot.com/2026/08/wrong-source.html",
    ],
)
def test_integrity_rejects_private_or_mismatched_urls(bad_url):
    corpus = _load_json(_FIXTURE)
    corpus["pairs"][0]["new"]["public_id"] = bad_url

    with pytest.raises(AssertionError):
        _validate_fixture(corpus)


@pytest.mark.parametrize(
    "secret_value",
    [
        "AKIA" + "1234567890ABCDEF",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "eyJaaaaaaaaaa.bbbbbbbbbb.cccccccccc",
        "postgresql://alice:s3cr3t@db.internal:5432/prod",
        "-1004027529994",
    ],
)
def test_integrity_rejects_secret_shaped_values(secret_value):
    corpus = _load_json(_FIXTURE)
    corpus["pairs"][0]["new"]["title"] = secret_value

    with pytest.raises(AssertionError):
        _validate_fixture(corpus)


@pytest.mark.parametrize(
    "bad_pair",
    [
        "malformed",
        "model||B",
        "|series|B",
        "model|series|X",
        "model|series|B|extra",
        "unknown model|car culture|B",
    ],
)
def test_integrity_rejects_malformed_pair_keys(bad_pair):
    corpus = _load_json(_FIXTURE)
    corpus["pairs"][0]["new"]["fingerprint"]["pairs"] = [bad_pair]

    with pytest.raises(AssertionError):
        _validate_fixture(corpus)


def test_integrity_rejects_operator_label_swaps():
    corpus = _load_json(_FIXTURE)
    corpus["pairs"][0]["label"], corpus["pairs"][2]["label"] = (
        corpus["pairs"][2]["label"],
        corpus["pairs"][0]["label"],
    )

    with pytest.raises(AssertionError):
        _validate_fixture(corpus)


def test_integrity_rejects_raw_evidence_drift():
    raw = _load_json(_RAW_EVIDENCE)
    raw["pairs"][0]["new"]["unexpected"] = "private production field"

    with pytest.raises(AssertionError):
        _validate_raw_corpus(raw)
