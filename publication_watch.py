"""Classify Telegraph publication evidence for the uptime watcher."""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from typing import Literal, NoReturn
from zoneinfo import ZoneInfo


PublicationState = Literal["fresh", "stale", "inconclusive"]

_MAX_INPUT_BYTES = 1024 * 1024
_MOSCOW = ZoneInfo("Europe/Moscow")
_DATE_SUFFIX = re.compile(r"-([0-9]{2})-([0-9]{2})\Z")


def _reject_nonstandard_json_constant(_value: str) -> NoReturn:
    raise ValueError("non-standard JSON constant")


def _moscow_clock(now: datetime) -> datetime | None:
    try:
        if not isinstance(now, datetime) or now.tzinfo is None:
            return None
        if now.utcoffset() is None:
            return None
        return now.astimezone(_MOSCOW)
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _publication_date(path: str, today: date) -> date | None:
    match = _DATE_SUFFIX.search(path)
    if match is None:
        return None

    month, day = (int(part) for part in match.groups())
    try:
        published = date(today.year, month, day)
    except (OverflowError, ValueError):
        return None

    if published <= today:
        return published

    if today.month != 1 or month != 12:
        return None

    try:
        return date(today.year - 1, month, day)
    except (OverflowError, ValueError):
        return None


def classify_telegraph_response(body: str, now: datetime) -> PublicationState:
    """Return the publication freshness supported by a Telegraph response."""

    if not isinstance(body, str):
        return "inconclusive"

    now_moscow = _moscow_clock(now)
    if now_moscow is None:
        return "inconclusive"

    try:
        payload = json.loads(
            body,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return "inconclusive"

    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return "inconclusive"

    result = payload.get("result")
    if not isinstance(result, dict):
        return "inconclusive"

    pages = result.get("pages")
    if not isinstance(pages, list) or not pages:
        return "inconclusive"

    first_page = pages[0]
    if not isinstance(first_page, dict):
        return "inconclusive"

    path = first_page.get("path")
    if not isinstance(path, str):
        return "inconclusive"

    published = _publication_date(path, now_moscow.date())
    if published is None:
        return "inconclusive"

    age_days = (now_moscow.date() - published).days
    if age_days == 0:
        return "fresh"
    if age_days == 1:
        return "stale" if now_moscow.hour >= 21 else "fresh"
    if age_days > 1:
        return "stale"
    return "inconclusive"


def main() -> int:
    """Read bounded evidence from stdin and emit one tri-state line."""

    state: PublicationState = "inconclusive"
    try:
        raw_body = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw_body) <= _MAX_INPUT_BYTES:
            body = raw_body.decode("utf-8")
            state = classify_telegraph_response(
                body,
                datetime.now(timezone.utc),
            )
    except (AttributeError, OSError, UnicodeDecodeError):
        state = "inconclusive"

    try:
        sys.stdout.write(f"{state}\n")
    except (BrokenPipeError, OSError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
