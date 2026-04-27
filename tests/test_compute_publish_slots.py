#!/usr/bin/env python3
"""Unit tests for compute_publish_slots scheduling algorithm.

13 tests covering:
- Boundary N values (0, 1, 4, 7, 10, 11, 15, 20)
- Container restart mid-window (now=16:00) and late-window (now=19:50)
- Now after window end (carry-over everything)
- TZ-naive datetime → ValueError invariant
- Returned slots inherit tzinfo from now

Tests use tz-aware datetime via datetime.timezone(timedelta(hours=3)) (MSK = UTC+3
year-round since 2014) to avoid coupling unit tests of this pure module to the
external pytz dependency that is added in Task 6.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compute_publish_slots import (
    MIN_INTERVAL_MINUTES,
    WINDOW_END,
    WINDOW_START,
    compute_publish_slots,
)


# Moscow standard time = UTC+3 year-round since 2014.
# Avoiding pytz here keeps this module's unit tests self-contained.
MSK = timezone(timedelta(hours=3))


def msk(year, month, day, hour, minute=0):
    """Build a tz-aware MSK datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


class TestComputePublishSlots(unittest.TestCase):
    """Test compute_publish_slots(n, now, ...) -> (slots, carry_over)."""

    def test_n_zero_returns_empty(self):
        """N=0, now=13:00 MSK → empty slots, carry_over=0."""
        slots, carry = compute_publish_slots(0, msk(2026, 4, 26, 13, 0))
        self.assertEqual(slots, [])
        self.assertEqual(carry, 0)

    def test_n_one_at_window_start(self):
        """N=1, now=12:00 MSK → 1 slot at 13:00, carry_over=0."""
        slots, carry = compute_publish_slots(1, msk(2026, 4, 26, 12, 0))
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0], msk(2026, 4, 26, 13, 0))
        self.assertEqual(carry, 0)

    def test_n_four_evenly_spaced(self):
        """N=4, now=12:00 MSK → 4 slots at 105-min interval (13:00, 14:45, 16:30, 18:15)."""
        slots, carry = compute_publish_slots(4, msk(2026, 4, 26, 12, 0))
        self.assertEqual(len(slots), 4)
        self.assertEqual(slots[0], msk(2026, 4, 26, 13, 0))
        self.assertEqual(slots[1], msk(2026, 4, 26, 14, 45))
        self.assertEqual(slots[2], msk(2026, 4, 26, 16, 30))
        self.assertEqual(slots[3], msk(2026, 4, 26, 18, 15))
        self.assertEqual(carry, 0)

    def test_n_seven_hourly(self):
        """N=7, now=12:00 MSK → 7 slots at 60-min interval (13:00…19:00)."""
        slots, carry = compute_publish_slots(7, msk(2026, 4, 26, 12, 0))
        self.assertEqual(len(slots), 7)
        for i, hour in enumerate(range(13, 20)):
            self.assertEqual(slots[i], msk(2026, 4, 26, hour, 0))
        self.assertEqual(carry, 0)

    def test_n_ten_interval_42min(self):
        """N=10, now=12:00 MSK → 10 slots at 42-min interval (420/10), carry_over=0."""
        slots, carry = compute_publish_slots(10, msk(2026, 4, 26, 12, 0))
        self.assertEqual(len(slots), 10)
        self.assertEqual(slots[0], msk(2026, 4, 26, 13, 0))
        # Each subsequent slot is 42 minutes after the previous.
        for i in range(1, 10):
            delta = slots[i] - slots[i - 1]
            self.assertEqual(delta, timedelta(minutes=42))
        self.assertEqual(carry, 0)

    def test_n_eleven_at_floor(self):
        """N=11, now=12:00 MSK → 11 slots at floor=40-min interval (13:00, 13:40, …, 19:40)."""
        slots, carry = compute_publish_slots(11, msk(2026, 4, 26, 12, 0))
        self.assertEqual(len(slots), 11)
        self.assertEqual(slots[0], msk(2026, 4, 26, 13, 0))
        self.assertEqual(slots[-1], msk(2026, 4, 26, 19, 40))
        for i in range(1, 11):
            delta = slots[i] - slots[i - 1]
            self.assertEqual(delta, timedelta(minutes=40))
        self.assertEqual(carry, 0)

    def test_n_fifteen_capped_at_eleven(self):
        """N=15, now=12:00 MSK → 11 slots (natural cap from floor=40), carry_over=4."""
        slots, carry = compute_publish_slots(15, msk(2026, 4, 26, 12, 0))
        self.assertEqual(len(slots), 11)
        self.assertEqual(carry, 4)

    def test_n_twenty_capped_at_eleven(self):
        """N=20, now=12:00 MSK → 11 slots, carry_over=9."""
        slots, carry = compute_publish_slots(20, msk(2026, 4, 26, 12, 0))
        self.assertEqual(len(slots), 11)
        self.assertEqual(carry, 9)

    def test_restart_at_16_n5(self):
        """Restart at 16:00 MSK, N=5 → 5 slots at 48-min interval (16:00, 16:48, 17:36, 18:24, 19:12)."""
        slots, carry = compute_publish_slots(5, msk(2026, 4, 26, 16, 0))
        self.assertEqual(len(slots), 5)
        self.assertEqual(slots[0], msk(2026, 4, 26, 16, 0))
        self.assertEqual(slots[1], msk(2026, 4, 26, 16, 48))
        self.assertEqual(slots[2], msk(2026, 4, 26, 17, 36))
        self.assertEqual(slots[3], msk(2026, 4, 26, 18, 24))
        self.assertEqual(slots[4], msk(2026, 4, 26, 19, 12))
        self.assertEqual(carry, 0)

    def test_restart_at_19_50_n5(self):
        """Restart at 19:50 MSK, N=5 → 1 slot at 19:50, carry_over=4 (remaining=10min < floor)."""
        slots, carry = compute_publish_slots(5, msk(2026, 4, 26, 19, 50))
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0], msk(2026, 4, 26, 19, 50))
        self.assertEqual(carry, 4)

    def test_now_after_window_end(self):
        """now=21:00 MSK, N=5 → empty slots, carry_over=5 (entire batch postponed)."""
        slots, carry = compute_publish_slots(5, msk(2026, 4, 26, 21, 0))
        self.assertEqual(slots, [])
        self.assertEqual(carry, 5)

    def test_naive_datetime_raises(self):
        """now without tzinfo → ValueError (TZ-aware invariant)."""
        naive_now = datetime(2026, 4, 26, 13, 0)  # no tzinfo
        with self.assertRaises(ValueError):
            compute_publish_slots(5, naive_now)

    def test_returned_slots_preserve_tzinfo(self):
        """tz-aware now → every returned slot inherits the same tzinfo."""
        now = msk(2026, 4, 26, 12, 0)
        slots, _ = compute_publish_slots(7, now)
        self.assertGreater(len(slots), 0)
        for slot in slots:
            self.assertIsNotNone(slot.tzinfo)
            # Same UTC offset as `now` (MSK = UTC+3).
            self.assertEqual(slot.utcoffset(), now.utcoffset())


class TestModuleConstants(unittest.TestCase):
    """Module-level constants are exported and have the expected values."""

    def test_constants_exported(self):
        from datetime import time as dtime

        self.assertEqual(WINDOW_START, dtime(13, 0))
        self.assertEqual(WINDOW_END, dtime(20, 0))
        self.assertEqual(MIN_INTERVAL_MINUTES, 40)


if __name__ == "__main__":
    unittest.main()
