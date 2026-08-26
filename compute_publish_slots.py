"""Pure scheduling algorithm for distributed publishing in the 13:00–20:00 МСК window.

Foundation module (Wave 1, Task 02 of llm-transcreation-and-distributed-publishing).
Stdlib-only, side-effect-free — safe to call from cron tick (`job()`) and from
container restart recompute paths alike.

Algorithm (adaptive, two-branch by fact, single-form by code):

    effective_start  = max(window_start, now)
    remaining_min    = (window_end - effective_start) in minutes
    raw_interval     = remaining_min / N                     (N > 0)
    interval         = max(raw_interval, MIN_INTERVAL_MINUTES)
    max_publishable  = floor(remaining_min / interval) + 1
    slots_count      = min(N, max_publishable)
    slots            = [effective_start + interval*i for i in range(slots_count)]
    carry_over       = N - slots_count

When `now <= window_start` (cron tick from 12:00) `effective_start = window_start`
and `remaining_min = 420`, so `raw_interval = 420/N` — full-window pacing.
When `now > window_start` (container restart mid-window) `effective_start = now`
and `remaining_min = window_end - now`, so the interval adapts to the leftover.

Daily publish cap is a natural consequence of `MIN_INTERVAL_MINUTES`: with
the production 10:00–20:00 МСК window (600 min) and a 90-minute interval,
`floor(600 / 90) + 1 = 7` slots/day. No explicit cap constant is declared —
that would duplicate the invariant and break customisation through the
`min_interval_min` parameter.

Examples (from user-spec / tech-spec Architecture):

    Cron tick at 12:00, N=7      → 7 slots at 13:00, 14:00, …, 19:00 (60-min interval)
    Cron tick at 12:00, N=15     → 11 slots at 40-min interval, carry_over=4
    Restart at 16:00, N=5        → 5 slots at 16:00, 16:48, 17:36, 18:24, 19:12
    Restart at 19:50, N=5        → 1 slot at 19:50, carry_over=4
    Cron tick at 21:00, N=5      → empty list, carry_over=5 (postpone to next day)
"""
from datetime import datetime, time, timedelta
from math import floor
from typing import List, Tuple

WINDOW_START: time = time(13, 0)
WINDOW_END: time = time(20, 0)
MIN_INTERVAL_MINUTES: int = 90

#: Three FIXED daily publish times in Europe/Moscow (operator pacing decision
#: 2026-06-13: one post morning, one afternoon, one evening). Consumed by
#: ``compute_fixed_slots`` — the production scheduler. ``job()`` interprets
#: these against ``now``'s tzinfo (always MSK), so no tz is attached here.
DAILY_PUBLISH_TIMES: List[time] = [time(10, 0), time(15, 0), time(19, 30)]


def remaining_fixed_slots(
    now: datetime,
    daily_times: List[time] = DAILY_PUBLISH_TIMES,
    grace_minutes: int = 5,
) -> list[datetime]:
    """Return every fixed publish time still eligible today.

    ``daily_times`` is interpreted in ``now``'s timezone, sorted without
    mutating the caller's collection, and filtered with an inclusive grace
    window. The result is intentionally uncapped; callers decide how many of
    the remaining opportunities they plan to use.

    Raises:
        ValueError: if ``now`` is tz-naive.
    """
    if now.tzinfo is None:
        raise ValueError("remaining_fixed_slots requires tz-aware datetime")

    today = sorted(
        datetime.combine(now.date(), slot_time, tzinfo=now.tzinfo)
        for slot_time in daily_times
    )
    cutoff = now - timedelta(minutes=grace_minutes)
    return [slot for slot in today if slot >= cutoff]


def compute_fixed_slots(
    n: int,
    now: datetime,
    daily_times: List[time] = DAILY_PUBLISH_TIMES,
    grace_minutes: int = 5,
) -> Tuple[List[datetime], int]:
    """Compute today's publish slots from the FIXED daily times.

    Production scheduler (operator pacing decision 2026-06-13): publish at most
    once per fixed time — 10:00, 15:00, 19:30 МСК by default. Replaces the
    dynamic even-spread of ``compute_publish_slots`` (kept DORMANT for reference
    and its existing unit tests).

    Args:
        n: Number of pending articles waiting to be published. Must be >= 0.
        now: Current time. MUST be tz-aware; raises ValueError otherwise.
        daily_times: Fixed wall-clock times to publish at (default 10:00, 15:00,
            19:30). Interpreted against ``now``'s tzinfo.
        grace_minutes: How long after a fixed time the slot is still eligible
            (default 5). The 10:00 cron tick reaches slot-planning a few
            seconds/minutes after 10:00:00, so without grace the 10:00 slot
            would be wrongly dropped. Grace MUST stay small: a deploy-restart
            well after a fixed time must NOT re-fire that slot — that would
            break the 3/day guarantee.

    Returns:
        (slots, carry_over) where:
        - slots is a list of tz-aware datetimes (inheriting tzinfo from `now`)
          at which to publish, ordered ascending — at most ``len(daily_times)``
          and at most ``n``.
        - carry_over = n - len(slots) is the number of articles that did not fit
          today's remaining fixed slots and should be deferred to the next tick.

    Raises:
        ValueError: if `now` is tz-naive (scheduler invariant: MSK-aware only).
    """
    if now.tzinfo is None:
        raise ValueError("compute_fixed_slots requires tz-aware datetime")

    # Spec contract is `n >= 0`; treating negative N as a no-op mirrors
    # ``compute_publish_slots`` and keeps callers defensively safe.
    if n <= 0:
        return [], 0

    slots = remaining_fixed_slots(now, daily_times, grace_minutes)[:n]
    carry_over = n - len(slots)
    return slots, carry_over


# DORMANT: ``compute_publish_slots`` (the dynamic even-spread below) is
# superseded by ``compute_fixed_slots`` above for production scheduling. It is
# retained for reference and to keep its existing unit-test coverage green.
def compute_publish_slots(
    n: int,
    now: datetime,
    window_start: time = WINDOW_START,
    window_end: time = WINDOW_END,
    min_interval_min: int = MIN_INTERVAL_MINUTES,
) -> Tuple[List[datetime], int]:
    """Compute publication time slots for N pending articles within today's window.

    Args:
        n: Number of pending articles waiting to be published. Must be >= 0.
        now: Current time. MUST be tz-aware; raises ValueError otherwise.
        window_start: Daily window open time (default 13:00).
        window_end: Daily window close time (default 20:00).
        min_interval_min: Minimum spacing between publications in minutes (default 90).

    Returns:
        (slots, carry_over) where:
        - slots is a list of tz-aware datetimes (inheriting tzinfo from `now`)
          at which to publish, ordered ascending.
        - carry_over = n - len(slots) is the number of articles that did not fit
          today's remaining window and should be deferred to the next cron tick.

    Raises:
        ValueError: if `now` is tz-naive (scheduler invariant: MSK-aware only).
    """
    if now.tzinfo is None:
        raise ValueError("compute_publish_slots requires tz-aware datetime")

    # Spec contract is `n >= 0`; treating negative N as a no-op rather than
    # raising keeps callers (e.g. count_pending() arithmetic) defensively safe.
    if n <= 0:
        return [], 0

    tzinfo = now.tzinfo
    window_start_dt = datetime.combine(now.date(), window_start, tzinfo=tzinfo)
    window_end_dt = datetime.combine(now.date(), window_end, tzinfo=tzinfo)

    # Past close — defer the entire batch to the next day.
    # Strict `>=` so 20:00 itself is exclusive (per AC3: nothing publishes after window close).
    if now >= window_end_dt:
        return [], n

    effective_start = max(window_start_dt, now)
    remaining_minutes = (window_end_dt - effective_start).total_seconds() / 60.0

    raw_interval = remaining_minutes / n
    interval = max(raw_interval, float(min_interval_min))

    # `floor(remaining/interval) + 1` accounts for the always-present slot at
    # effective_start; subsequent slots fit only while they remain inside the window.
    max_publishable_now = floor(remaining_minutes / interval) + 1
    slots_count = min(n, max_publishable_now)

    slots = [
        effective_start + timedelta(minutes=interval * i)
        for i in range(slots_count)
    ]
    carry_over = n - len(slots)
    return slots, carry_over
