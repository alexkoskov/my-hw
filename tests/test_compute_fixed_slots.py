#!/usr/bin/env python3
"""Unit tests for the compute_fixed_slots scheduling algorithm.

Fixed 3-slots-per-day scheduler (operator pacing 2026-06-13: 10:00, 15:00,
19:30 Europe/Moscow). Covers slot eligibility against ``now``, the grace
window for the 10:00 cron-tick latency, the per-day cap as a natural
consequence of the three fixed times, carry-over arithmetic, and the
tz-aware invariant.

Uses pytz Europe/Moscow + tz-aware datetimes (mirrors the integration
tests' fixture style).
"""
import os
import sys
import unittest
from datetime import datetime, time, timezone

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compute_publish_slots import (
    DAILY_PUBLISH_TIMES,
    compute_fixed_slots,
    remaining_fixed_slots,
)


MSK = pytz.timezone("Europe/Moscow")


def msk(year, month, day, hour, minute=0):
    """Build a tz-aware Europe/Moscow datetime."""
    return MSK.localize(datetime(year, month, day, hour, minute))


class TestComputeFixedSlots(unittest.TestCase):
    """Test compute_fixed_slots(n, now, ...) -> (slots, carry_over)."""

    def _assert_slot_times(self, slots, expected_hm):
        """Assert each slot's (hour, minute) and that it is tz-aware."""
        self.assertEqual(len(slots), len(expected_hm))
        for slot, (hour, minute) in zip(slots, expected_hm):
            self.assertEqual((slot.hour, slot.minute), (hour, minute))
            self.assertIsNotNone(slot.tzinfo)

    def test_cron_tick_10_00_n5_yields_three_slots(self):
        """Cron tick at 10:00 sharp, N=5 → 3 slots [10:00, 15:00, 19:30],
        carry_over=2 (the day's fixed cap)."""
        slots, carry = compute_fixed_slots(5, msk(2026, 6, 14, 10, 0))
        self._assert_slot_times(slots, [(10, 0), (15, 0), (19, 30)])
        self.assertEqual(carry, 2)

    def test_n3_at_10_00_no_carry_over(self):
        """N=3 at 10:00 → 3 slots, carry_over=0."""
        slots, carry = compute_fixed_slots(3, msk(2026, 6, 14, 10, 0))
        self._assert_slot_times(slots, [(10, 0), (15, 0), (19, 30)])
        self.assertEqual(carry, 0)

    def test_noon_n3_drops_passed_morning_slot(self):
        """now=12:00, N=3 → 2 slots [15:00, 19:30], carry_over=1
        (10:00 already passed beyond grace)."""
        slots, carry = compute_fixed_slots(3, msk(2026, 6, 14, 12, 0))
        self._assert_slot_times(slots, [(15, 0), (19, 30)])
        self.assertEqual(carry, 1)

    def test_16_00_n3_only_evening_slot(self):
        """now=16:00, N=3 → 1 slot [19:30], carry_over=2."""
        slots, carry = compute_fixed_slots(3, msk(2026, 6, 14, 16, 0))
        self._assert_slot_times(slots, [(19, 30)])
        self.assertEqual(carry, 2)

    def test_after_last_slot_n3_empty(self):
        """now=20:00 (after the last slot + grace), N=3 → 0 slots,
        carry_over=3 (whole batch defers to tomorrow)."""
        slots, carry = compute_fixed_slots(3, msk(2026, 6, 14, 20, 0))
        self.assertEqual(slots, [])
        self.assertEqual(carry, 3)

    def test_n1_at_10_00_single_slot(self):
        """N=1 at 10:00 → 1 slot [10:00], carry_over=0
        (only the oldest article gets a slot)."""
        slots, carry = compute_fixed_slots(1, msk(2026, 6, 14, 10, 0))
        self._assert_slot_times(slots, [(10, 0)])
        self.assertEqual(carry, 0)

    def test_n0_returns_empty(self):
        """N=0 → ([], 0) — no articles, no slots, no carry."""
        slots, carry = compute_fixed_slots(0, msk(2026, 6, 14, 10, 0))
        self.assertEqual(slots, [])
        self.assertEqual(carry, 0)

    def test_grace_within_window_keeps_10_00_slot(self):
        """now=10:03 (3 min past 10:00, within the 5-min grace), N=3 →
        the 10:00 slot is still included (3 slots)."""
        slots, carry = compute_fixed_slots(3, msk(2026, 6, 14, 10, 3))
        self._assert_slot_times(slots, [(10, 0), (15, 0), (19, 30)])
        self.assertEqual(carry, 0)

    def test_grace_boundary_drops_10_00_slot(self):
        """now=10:06 (>5 min past 10:00, beyond grace), N=3 → 10:00 dropped
        → 2 slots [15:00, 19:30], carry_over=1."""
        slots, carry = compute_fixed_slots(3, msk(2026, 6, 14, 10, 6))
        self._assert_slot_times(slots, [(15, 0), (19, 30)])
        self.assertEqual(carry, 1)

    def test_naive_datetime_raises(self):
        """now without tzinfo → ValueError (TZ-aware invariant)."""
        naive_now = datetime(2026, 6, 14, 10, 0)  # no tzinfo
        with self.assertRaises(ValueError):
            compute_fixed_slots(3, naive_now)

    def test_returned_slots_preserve_tzinfo(self):
        """tz-aware now → every returned slot inherits a tz with the same
        UTC offset as now (MSK)."""
        now = msk(2026, 6, 14, 10, 0)
        slots, _ = compute_fixed_slots(3, now)
        self.assertGreater(len(slots), 0)
        for slot in slots:
            self.assertIsNotNone(slot.tzinfo)
            self.assertEqual(slot.utcoffset(), now.utcoffset())


class TestRemainingFixedSlots(unittest.TestCase):
    """Contract for the uncapped fixed-time opportunity helper."""

    def test_returns_all_eligible_times_for_each_day_state(self):
        custom_times = [time(18, 0), time(11, 0), time(14, 0)]
        custom_times_before = list(custom_times)
        cases = (
            ('at first slot', msk(2026, 6, 14, 10, 0), None,
             [(10, 0), (15, 0), (19, 30)]),
            ('midday', msk(2026, 6, 14, 12, 0), None,
             [(15, 0), (19, 30)]),
            ('afternoon', msk(2026, 6, 14, 16, 0), None,
             [(19, 30)]),
            ('after cutoff', msk(2026, 6, 14, 20, 0), None, []),
            ('custom unsorted times', msk(2026, 6, 14, 12, 0), custom_times,
             [(14, 0), (18, 0)]),
            ('utc clock', datetime(2026, 6, 14, 12, tzinfo=timezone.utc),
             None, [(15, 0), (19, 30)]),
        )

        for label, now, daily_times, expected_hm in cases:
            with self.subTest(label=label):
                kwargs = {} if daily_times is None else {
                    'daily_times': daily_times,
                }
                slots = remaining_fixed_slots(now, **kwargs)

                expected_slots = [
                    now.replace(
                        hour=hour, minute=minute, second=0, microsecond=0,
                    )
                    for hour, minute in expected_hm
                ]
                self.assertEqual(slots, expected_slots)
                self.assertTrue(all(slot.date() == now.date() for slot in slots))
                self.assertTrue(all(slot.tzinfo is not None for slot in slots))
                self.assertTrue(
                    all(slot.utcoffset() == now.utcoffset() for slot in slots)
                )

        self.assertEqual(custom_times, custom_times_before)

    def test_grace_boundary_and_naive_clock_contract(self):
        at_boundary = MSK.localize(datetime(2026, 6, 14, 10, 5))
        just_after = MSK.localize(
            datetime(2026, 6, 14, 10, 5, 0, 1)
        )

        self.assertEqual(
            [(slot.hour, slot.minute)
             for slot in remaining_fixed_slots(at_boundary)],
            [(10, 0), (15, 0), (19, 30)],
        )
        self.assertEqual(
            [(slot.hour, slot.minute)
             for slot in remaining_fixed_slots(just_after)],
            [(15, 0), (19, 30)],
        )
        with self.assertRaises(ValueError):
            remaining_fixed_slots(datetime(2026, 6, 14, 10, 0))

    def test_compute_fixed_slots_is_a_capped_prefix_with_unchanged_carry(self):
        now = msk(2026, 6, 14, 12, 0)
        opportunities = remaining_fixed_slots(now)

        for n in (-2, 0, 1, 2, 5):
            with self.subTest(n=n):
                slots, carry = compute_fixed_slots(n, now)
                expected_slots = [] if n <= 0 else opportunities[:n]
                expected_carry = 0 if n <= 0 else n - len(expected_slots)

                self.assertEqual(slots, expected_slots)
                self.assertEqual(carry, expected_carry)

        custom_times = [time(21, 0), time(9, 0), time(13, 0)]
        custom_times_before = list(custom_times)
        custom_now = msk(2026, 6, 14, 13, 7)
        with self.subTest(custom_arguments=True):
            slots, carry = compute_fixed_slots(
                3,
                custom_now,
                daily_times=custom_times,
                grace_minutes=10,
            )
            self.assertEqual(
                slots,
                [
                    custom_now.replace(hour=13, minute=0),
                    custom_now.replace(hour=21, minute=0),
                ],
            )
            self.assertEqual(carry, 1)
            self.assertEqual(custom_times, custom_times_before)

        with self.subTest(empty_daily_times=True):
            slots, carry = compute_fixed_slots(2, now, daily_times=[])
            self.assertEqual(slots, [])
            self.assertEqual(carry, 2)


class TestModuleConstants(unittest.TestCase):
    """DAILY_PUBLISH_TIMES is exported with the operator-set fixed times."""

    def test_daily_publish_times(self):
        self.assertEqual(
            DAILY_PUBLISH_TIMES,
            [time(10, 0), time(15, 0), time(19, 30)],
        )


if __name__ == "__main__":
    unittest.main()
