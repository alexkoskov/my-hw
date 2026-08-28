from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from zoneinfo import ZoneInfo

import pytest

import publication_watch
from publication_watch import PublicationState, classify_telegraph_response


MAX_INPUT_BYTES = 1024 * 1024
MOSCOW = ZoneInfo("Europe/Moscow")
MODULE_PATH = Path(__file__).resolve().parents[1] / "publication_watch.py"


def _body(path: object) -> str:
    return json.dumps(
        {
            "ok": True,
            "extra": "allowed",
            "result": {
                "pages": [{"path": path, "title": "ignored"}],
                "total_count": 1,
            },
        },
        separators=(",", ":"),
    )


def _msk(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MOSCOW)


class _MalformedTimezone(tzinfo):
    def utcoffset(self, _dt: datetime | None) -> timedelta | None:
        return 1  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("now", "path", "expected"),
    [
        (_msk(2026, 8, 26, 23, 59), "posts/sample-08-26", "fresh"),
        (_msk(2026, 8, 26, 20, 59), "sample-08-25", "fresh"),
        (_msk(2026, 8, 26, 21, 0), "sample-08-25", "stale"),
        (
            datetime(2026, 8, 25, 21, 0, tzinfo=timezone.utc),
            "sample-08-24",
            "stale",
        ),
        (
            datetime(2026, 8, 25, 23, 59, tzinfo=timezone.utc),
            "sample-08-24",
            "stale",
        ),
        (
            datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc),
            "sample-08-24",
            "stale",
        ),
        (_msk(2027, 1, 1, 20, 59), "sample-12-31", "fresh"),
        (_msk(2027, 1, 1, 21, 0), "sample-12-31", "stale"),
        (_msk(2024, 3, 1, 20, 59), "sample-02-29", "fresh"),
        (_msk(2024, 3, 1, 21, 0), "sample-02-29", "stale"),
    ],
)
def test_classifies_fresh_and_stale_calendar_boundaries(
    now: datetime,
    path: str,
    expected: PublicationState,
) -> None:
    assert classify_telegraph_response(_body(path), now) == expected


@pytest.mark.parametrize(
    ("body", "now"),
    [
        (None, _msk(2026, 8, 26, 12, 0)),
        ("{", _msk(2026, 8, 26, 12, 0)),
        ("null", _msk(2026, 8, 26, 12, 0)),
        ("[]", _msk(2026, 8, 26, 12, 0)),
        ('"text"', _msk(2026, 8, 26, 12, 0)),
        (
            json.dumps({"result": {"pages": [{"path": "sample-08-26"}]}}),
            _msk(2026, 8, 26, 12, 0),
        ),
        (json.dumps({"ok": True}), _msk(2026, 8, 26, 12, 0)),
        (
            json.dumps(
                {"ok": False, "result": {"pages": [{"path": "sample-08-26"}]}}
            ),
            _msk(2026, 8, 26, 12, 0),
        ),
        (
            json.dumps(
                {"ok": 1, "result": {"pages": [{"path": "sample-08-26"}]}}
            ),
            _msk(2026, 8, 26, 12, 0),
        ),
        (json.dumps({"ok": True, "result": None}), _msk(2026, 8, 26, 12, 0)),
        (json.dumps({"ok": True, "result": {}}), _msk(2026, 8, 26, 12, 0)),
        (
            json.dumps({"ok": True, "result": {"pages": {}}}),
            _msk(2026, 8, 26, 12, 0),
        ),
        (
            json.dumps({"ok": True, "result": {"pages": []}}),
            _msk(2026, 8, 26, 12, 0),
        ),
        (
            json.dumps({"ok": True, "result": {"pages": [None]}}),
            _msk(2026, 8, 26, 12, 0),
        ),
        (
            json.dumps({"ok": True, "result": {"pages": [{}]}}),
            _msk(2026, 8, 26, 12, 0),
        ),
        (
            json.dumps({"ok": True, "result": {"pages": [{"path": 826}]}}),
            _msk(2026, 8, 26, 12, 0),
        ),
        (
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "pages": [
                            {"path": "unknown"},
                            {"path": "sample-08-26"},
                        ]
                    },
                }
            ),
            _msk(2026, 8, 26, 12, 0),
        ),
        (_body("sample"), _msk(2026, 8, 26, 12, 0)),
        (_body("sample-8-26"), _msk(2026, 8, 26, 12, 0)),
        (_body("sample-08-26/extra"), _msk(2026, 8, 26, 12, 0)),
        (_body("sample-08-26\n"), _msk(2026, 8, 26, 12, 0)),
        (_body("sample-02-30"), _msk(2026, 3, 1, 12, 0)),
        (_body("sample-09-01"), _msk(2026, 8, 26, 12, 0)),
        (_body("sample-12-31"), _msk(2026, 11, 30, 12, 0)),
        (_body("sample-02-29"), _msk(2025, 3, 1, 12, 0)),
        (_body("sample-08-26"), datetime(2026, 8, 26, 12, 0)),
        (
            _body("sample-08-26"),
            datetime(2026, 8, 26, 12, 0, tzinfo=_MalformedTimezone()),
        ),
        (
            '{"ok":true,"result":{"pages":[{"path":"sample-08-26"}]},"extra":NaN}',
            _msk(2026, 8, 26, 12, 0),
        ),
    ],
)
def test_invalid_or_ambiguous_evidence_is_inconclusive(
    body: object,
    now: datetime,
) -> None:
    assert classify_telegraph_response(body, now) == "inconclusive"  # type: ignore[arg-type]


def test_recursive_json_is_inconclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_recursion_error(*_args: object, **_kwargs: object) -> None:
        raise RecursionError

    monkeypatch.setattr(publication_watch.json, "loads", raise_recursion_error)

    assert (
        classify_telegraph_response("{}", _msk(2026, 8, 26, 12, 0))
        == "inconclusive"
    )


def _run_cli(payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _path_for(day: datetime) -> str:
    return f"sample-{day:%m-%d}"


def test_cli_recursive_json_is_inconclusive_without_traceback() -> None:
    deeply_nested = ("[" * 10_000 + "0" + "]" * 10_000).encode()

    completed = _run_cli(deeply_nested)

    assert completed.returncode == 0
    assert completed.stdout == b"inconclusive\n"
    assert completed.stderr == b""


def test_only_first_page_controls_the_verdict() -> None:
    body = json.dumps(
        {
            "ok": True,
            "result": {
                "pages": [
                    {"path": "sample-08-26"},
                    None,
                    {"path": "impossible-02-30"},
                ]
            },
        }
    )

    assert (
        classify_telegraph_response(body, _msk(2026, 8, 26, 12, 0)) == "fresh"
    )


def test_cli_emits_exact_tri_state_contract() -> None:
    now = datetime.now(MOSCOW)
    token_shaped_marker = b"TOKEN_SHAPED_EVIDENCE_1234567890"
    cases = [
        (_body(_path_for(now)).encode(), b"fresh\n"),
        (_body(_path_for(now - timedelta(days=3))).encode(), b"stale\n"),
        (b'{"secret":"' + token_shaped_marker + b'"', b"inconclusive\n"),
        (
            json.dumps(
                {
                    "ok": False,
                    "result": {"pages": [{"path": _path_for(now)}]},
                    "evidence": token_shaped_marker.decode(),
                },
                separators=(",", ":"),
            ).encode(),
            b"inconclusive\n",
        ),
    ]

    for payload, expected_stdout in cases:
        completed = _run_cli(payload)

        assert completed.returncode == 0
        assert completed.stdout == expected_stdout
        assert completed.stderr == b""
        assert token_shaped_marker not in completed.stdout
        assert token_shaped_marker not in completed.stderr


def test_cli_enforces_exact_one_mib_byte_limit_without_evidence_leak() -> None:
    now = datetime.now(MOSCOW)
    token_shaped_marker = b"TOKEN_SHAPED_EVIDENCE_1234567890"
    multibyte_json = json.dumps(
        {
            "ok": True,
            "result": {"pages": [{"path": _path_for(now)}]},
            "evidence": f"я{token_shaped_marker.decode()}",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    exact_limit_payload = multibyte_json + b" " * (
        MAX_INPUT_BYTES - len(multibyte_json)
    )
    oversized_payload = exact_limit_payload + b" "

    otherwise_valid = json.dumps(
        {
            "ok": True,
            "result": {"pages": [{"path": _path_for(now)}]},
            "evidence": token_shaped_marker.decode(),
        },
        separators=(",", ":"),
    ).encode()
    invalid_utf8_payload = otherwise_valid.replace(
        token_shaped_marker,
        token_shaped_marker + b"\xff",
    )

    exact = _run_cli(exact_limit_payload)
    oversized = _run_cli(oversized_payload)
    invalid_utf8 = _run_cli(invalid_utf8_payload)

    assert len(exact_limit_payload) == MAX_INPUT_BYTES
    assert exact.returncode == 0
    assert exact.stdout == b"fresh\n"
    assert exact.stderr == b""
    assert token_shaped_marker not in exact.stdout
    assert token_shaped_marker not in exact.stderr

    assert len(oversized_payload) == MAX_INPUT_BYTES + 1
    assert oversized.returncode == 0
    assert oversized.stdout == b"inconclusive\n"
    assert oversized.stderr == b""
    assert token_shaped_marker not in oversized.stdout
    assert token_shaped_marker not in oversized.stderr

    assert invalid_utf8.returncode == 0
    assert invalid_utf8.stdout == b"inconclusive\n"
    assert invalid_utf8.stderr == b""
    assert token_shaped_marker not in invalid_utf8.stdout
    assert token_shaped_marker not in invalid_utf8.stderr


def test_cli_reads_only_limit_plus_sentinel_and_skips_oversized_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingBuffer:
        def __init__(self) -> None:
            self.requested_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.requested_sizes.append(size)
            return b"x" * (MAX_INPUT_BYTES + 1)

    def unexpected_classifier(*_args: object, **_kwargs: object) -> PublicationState:
        pytest.fail("oversized evidence reached the classifier")

    buffer = RecordingBuffer()
    stdout = StringIO()
    fake_sys = SimpleNamespace(
        stdin=SimpleNamespace(buffer=buffer),
        stdout=stdout,
    )
    monkeypatch.setattr(publication_watch, "sys", fake_sys)
    monkeypatch.setattr(
        publication_watch,
        "classify_telegraph_response",
        unexpected_classifier,
    )

    assert publication_watch.main() == 0
    assert buffer.requested_sizes == [MAX_INPUT_BYTES + 1]
    assert stdout.getvalue() == "inconclusive\n"


def test_publication_state_alias_is_exact() -> None:
    assert get_args(PublicationState) == ("fresh", "stale", "inconclusive")
