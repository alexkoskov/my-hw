"""Unit tests for ``claude_transcreation`` — Anthropic SDK wrapper.

All tests mock the anthropic client; no real network calls are ever issued.
Per task 03 (feature ``llm-transcreation-and-distributed-publishing``), Wave 2.

Coverage targets (15 tests, see TDD anchor in tasks/03.md):

  * happy path returns valid dict (with token logging)
  * each anthropic SDK exception class branch (8 outage + 2 per-article)
  * malformed JSON / paragraph-count mismatch / emoji-prefix safety net /
    defensive 4000-char truncation / subdir→flat prompt fallback
"""

import json
import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure the SDK is installed (Wave 2 task 6 pinned anthropic>=0.45,<0.46).
anthropic = pytest.importorskip("anthropic")
import httpx  # noqa: E402 — transitive dep of anthropic

import claude_transcreation  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_prompt_cache():
    """Each test starts with a fresh prompt cache."""
    claude_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}
    yield
    claude_transcreation._PROMPT_CACHE = {"mtime": None, "body": None, "path": None}


@pytest.fixture
def make_status_error():
    """Build an ``APIStatusError`` subclass instance with a real ``httpx.Response``.

    The anthropic SDK constructors require both ``response`` and ``body``;
    ``side_effect = anthropic.RateLimitError("msg")`` raises ``TypeError``.
    """

    def _make(exc_class, status_code=429, message="status error"):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx.Response(status_code, request=request)
        return exc_class(message=message, response=response, body=None)

    return _make


@pytest.fixture
def make_connection_error():
    """Build an ``APIConnectionError`` (or ``APITimeoutError``) instance."""

    def _make(exc_class=anthropic.APIConnectionError, message="conn"):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        if exc_class is anthropic.APITimeoutError:
            return exc_class(request=request)
        return exc_class(message=message, request=request)

    return _make


@pytest.fixture
def sample_article():
    return {
        "source_name": "autoevolution",
        "title": "New Hot Wheels Chase Car Spotted at Walmart",
        "subtitle": "Collectors line up early",
        "paragraphs": [
            "First paragraph EN text.",
            "Second paragraph EN text.",
        ],
        "blocks": None,
    }


def _good_response_text(paragraph_count=2, title="🏎️ Новый Hot Wheels Chase"):
    payload = {
        "title": title,
        "alts": [
            "Альт первый",
            "Альт второй",
            "Альт третий",
        ],
        "subtitle": "Коллекционеры выстраиваются в очередь",
        "paragraphs": [f"Параграф номер {i + 1}." for i in range(paragraph_count)],
        "blocks": None,
    }
    return json.dumps(payload, ensure_ascii=False)


def _make_mock_client(response_text, input_tokens=1500, output_tokens=900):
    """Build a MagicMock anthropic client whose ``messages.create`` returns
    the canonical SDK response shape (``content[0].text`` + usage)."""
    client = MagicMock()
    text_block = MagicMock()
    text_block.text = response_text
    response = MagicMock()
    response.content = [text_block]
    response.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    response.model = "claude-haiku-4-5"
    client.messages.create.return_value = response
    return client


@pytest.fixture
def mock_anthropic_client(sample_article):
    return _make_mock_client(_good_response_text(len(sample_article["paragraphs"])))


# --------------------------------------------------------------------------- #
# 1. Happy path                                                               #
# --------------------------------------------------------------------------- #


def test_happy_path_returns_valid_dict(mock_anthropic_client, sample_article, caplog):
    caplog.set_level(logging.INFO, logger="claude_transcreation")
    out = claude_transcreation.transcreate_via_claude(
        sample_article, client=mock_anthropic_client
    )
    assert isinstance(out, dict)
    assert out["title"].startswith(("🏆", "🏎️", "🚀", "💎", "🤝", "📢", "🚗", "🔥"))
    assert 2 <= len(out["alts"]) <= 3
    assert out["subtitle"] and isinstance(out["subtitle"], str)
    assert len(out["paragraphs"]) == len(sample_article["paragraphs"])

    # Token + latency observability (AC19)
    log_text = " ".join(rec.getMessage() for rec in caplog.records)
    for token in ("input_tokens", "output_tokens", "latency_ms", "model"):
        assert token in log_text, f"missing {token!r} in log line"


# --------------------------------------------------------------------------- #
# 2-9. SDK exception branches → ClaudeOutageError                              #
# --------------------------------------------------------------------------- #


def test_rate_limit_raises_outage(sample_article, make_status_error):
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.RateLimitError, status_code=429, message="rate"
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_authentication_raises_outage(sample_article, make_status_error):
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.AuthenticationError, status_code=401, message="bad key"
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_api_connection_raises_outage(sample_article, make_connection_error):
    client = MagicMock()
    client.messages.create.side_effect = make_connection_error(
        anthropic.APIConnectionError, message="dns failure"
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_api_timeout_raises_outage(sample_article, make_connection_error):
    client = MagicMock()
    client.messages.create.side_effect = make_connection_error(
        anthropic.APITimeoutError
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_internal_server_raises_outage(sample_article, make_status_error):
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.InternalServerError, status_code=500, message="server boom"
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_permission_denied_raises_outage(sample_article, make_status_error):
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.PermissionDeniedError, status_code=403, message="no perm"
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_model_not_found_raises_outage(sample_article, make_status_error):
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.NotFoundError, status_code=404, message="model not found"
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


# --------------------------------------------------------------------------- #
# 10-11. Per-article SDK exceptions → ClaudeTranscreationError                 #
# --------------------------------------------------------------------------- #


def test_bad_request_raises_per_article(sample_article, make_status_error):
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.BadRequestError, status_code=400, message="refused"
    )
    with pytest.raises(claude_transcreation.ClaudeTranscreationError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_unprocessable_entity_raises_per_article(sample_article, make_status_error):
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.UnprocessableEntityError, status_code=422, message="unprocessable"
    )
    with pytest.raises(claude_transcreation.ClaudeTranscreationError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


# --------------------------------------------------------------------------- #
# 11a-11b. Bare APIStatusError — routed by status code                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status_code", [402, 407, 408])
def test_account_level_status_raises_outage(
    sample_article, make_status_error, status_code
):
    """402 (payment required), 407 (proxy auth), 408 (server-side timeout).

    The SDK has no dedicated class for any of these, so all three arrive as a
    bare ``APIStatusError``. All are account/transport-level — holding and
    retrying is the only outcome that survives the fix (a top-up, a proxy
    coming back). Kept identical across engines; the incident that prompted it
    is recorded in the OpenRouter test.
    """
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.APIStatusError, status_code=status_code, message="account-level"
    )
    with pytest.raises(claude_transcreation.ClaudeOutageError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


@pytest.mark.parametrize("status_code", [413, 451])
def test_unknown_status_stays_per_article(
    sample_article, make_status_error, status_code
):
    """Conservative default preserved: 413 (payload too large) and 451 (legal)
    are about THIS article, and an outage would hold the row at the queue head
    where every later slot re-reads it — blocking the channel instead of
    striking one article out."""
    client = MagicMock()
    client.messages.create.side_effect = make_status_error(
        anthropic.APIStatusError, status_code=status_code, message="article-level"
    )
    with pytest.raises(claude_transcreation.ClaudeTranscreationError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


# --------------------------------------------------------------------------- #
# 12-13. Parse & schema-mismatch failures → ClaudeTranscreationError           #
# --------------------------------------------------------------------------- #


def test_malformed_json_raises_per_article(sample_article):
    client = _make_mock_client("Sorry, I can't translate that.")
    with pytest.raises(claude_transcreation.ClaudeTranscreationError):
        claude_transcreation.transcreate_via_claude(sample_article, client=client)


def test_paragraph_count_mismatch_is_accepted_with_warning(sample_article, caplog):
    # Input has 2 paragraphs but model returns 3 — count divergence is
    # accepted (logged as a warning) per the relaxed contract: long
    # autoevolution articles routinely see the model merge two short
    # adjacent paragraphs into one for editorial flow. Telegraph just
    # renders <p> nodes, exact 1:1 mapping is not a structural requirement.
    client = _make_mock_client(_good_response_text(paragraph_count=3))
    import logging
    with caplog.at_level(logging.WARNING, logger='claude_transcreation'):
        out = claude_transcreation.transcreate_via_claude(
            sample_article, client=client,
        )
    assert len(out['paragraphs']) == 3, (
        'parser must return whatever the LLM produced, not pad/truncate'
    )
    assert any(
        'paragraph count diverges' in rec.message and 'expected 2' in rec.message
        for rec in caplog.records
    ), 'a warning explaining the count divergence must be logged'


# --------------------------------------------------------------------------- #
# 14. Emoji-prefix safety net (post-pass)                                      #
# --------------------------------------------------------------------------- #


def test_title_without_emoji_gets_safety_net(sample_article):
    """Title without one of the 8 known emoji prefixes → safety net adds one
    based on content regex (covers two branches of the cascade)."""
    # Branch 1: "релиз / запуск" → 🚀
    payload = json.loads(_good_response_text(2))
    payload["title"] = "Новый релиз популярной модели"
    client = _make_mock_client(json.dumps(payload, ensure_ascii=False))
    out = claude_transcreation.transcreate_via_claude(sample_article, client=client)
    assert out["title"].startswith("🚀")

    # Branch 2: "легенды / tour" → 🏆
    payload2 = json.loads(_good_response_text(2))
    payload2["title"] = "Legends Tour 2026 объявлен"
    client2 = _make_mock_client(json.dumps(payload2, ensure_ascii=False))
    out2 = claude_transcreation.transcreate_via_claude(sample_article, client=client2)
    assert out2["title"].startswith("🏆")


# --------------------------------------------------------------------------- #
# 15. Defensive paragraph truncation at 4000 chars                             #
# --------------------------------------------------------------------------- #


def test_paragraph_over_4000_truncated_with_warning(sample_article, caplog):
    payload = json.loads(_good_response_text(2))
    # Use Cyrillic so the EN-guard heuristic doesn't fire — this test
    # is about length truncation, not language detection.
    payload["paragraphs"] = ["а" * 5000, "б" * 200]
    client = _make_mock_client(json.dumps(payload, ensure_ascii=False))

    caplog.set_level(logging.WARNING, logger="claude_transcreation")
    out = claude_transcreation.transcreate_via_claude(sample_article, client=client)
    assert len(out["paragraphs"][0]) == 4000
    assert len(out["paragraphs"][1]) == 200
    warning_text = " ".join(rec.getMessage() for rec in caplog.records
                            if rec.levelno >= logging.WARNING)
    assert "truncated" in warning_text.lower()


# --------------------------------------------------------------------------- #
# Bonus: prompt subdir → flat fallback                                         #
# --------------------------------------------------------------------------- #


def test_load_prompt_subdir_then_flat_fallback(tmp_path, monkeypatch):
    """Subdir path missing, flat path present → loader returns flat content."""
    flat = tmp_path / "ux-guidelines.md"
    flat.write_text("FLAT BODY", encoding="utf-8")

    missing_subdir = tmp_path / "subdir-that-does-not-exist" / "ux-guidelines.md"

    # _load_prompt's flat fallback expects the file alongside __file__'s dir.
    # We monkeypatch the module-level dirname pointer to redirect.
    monkeypatch.setattr(
        claude_transcreation, "_MODULE_DIR", str(tmp_path), raising=True
    )
    body = claude_transcreation._load_prompt(str(missing_subdir))
    assert "FLAT BODY" in body
