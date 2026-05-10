#!/usr/bin/env python3
"""Unit tests for admin_alerts builders.

Each E0XX builder is a pure str -> str / params -> str function. Tests cover:
- Each alert returns its [E0XX] code.
- Severity emoji in first line matches the documented level.
- Russian-language headers (no English leftovers).
- Substrings that integration tests rely on are preserved verbatim.
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin_alerts


MSK = timezone(timedelta(hours=3))


class TestAdminAlerts(unittest.TestCase):

    def test_e001_no_rss_feeds(self):
        msg = admin_alerts.alert_no_rss_feeds("feeds.json missing")
        self.assertIn("[E001]", msg)
        self.assertIn("🔴", msg)
        self.assertIn("feeds.json missing", msg)
        self.assertIn("Что сделать", msg)

    def test_e002_source_fetch_failed(self):
        msg = admin_alerts.alert_source_fetch_failed("orangetrack", "HTTPError 503")
        self.assertIn("[E002]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("orangetrack", msg)
        self.assertIn("HTTPError 503", msg)

    def test_e003_backlog_warning(self):
        msg = admin_alerts.alert_backlog_warning(
            queue_size=80, threshold=50, carry_over=12,
        )
        self.assertIn("[E003]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("80", msg)
        self.assertIn("50", msg)
        self.assertIn("12", msg)
        self.assertIn("Очередь распухла", msg)

    def test_e004_claude_probe_failed(self):
        msg = admin_alerts.alert_claude_probe_failed_at_startup()
        self.assertIn("[E004]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("Claude", msg)
        self.assertIn("Google", msg)

    def test_e005_tz_mismatch(self):
        msg = admin_alerts.alert_tz_mismatch("America/Los_Angeles")
        self.assertIn("[E005]", msg)
        self.assertIn("'America/Los_Angeles'", msg)
        self.assertIn("Europe/Moscow", msg)

    def test_e005_tz_mismatch_none(self):
        msg = admin_alerts.alert_tz_mismatch(None)
        self.assertIn("[E005]", msg)
        self.assertIn("None", msg)

    def test_e006_duplicate_publish_skipped(self):
        msg = admin_alerts.alert_duplicate_publish_skipped(
            "https://example.com/article-x"
        )
        self.assertIn("[E006]", msg)
        # Integration tests pin this exact substring.
        self.assertIn("⚠️ Пропущен дубль публикации", msg)
        self.assertIn("https://example.com/article-x", msg)
        self.assertIn("зомби-строка", msg)

    def test_e007_zombie_cleanup_failed(self):
        msg = admin_alerts.alert_zombie_cleanup_failed(
            "https://example.com/article-y", "OperationalError",
        )
        self.assertIn("[E007]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("https://example.com/article-y", msg)
        self.assertIn("OperationalError", msg)

    def test_e008_plan_of_day(self):
        slots = [
            datetime(2026, 5, 10, 10, 0, tzinfo=MSK),
            datetime(2026, 5, 10, 15, 0, tzinfo=MSK),
        ]
        msg = admin_alerts.alert_plan_of_day(
            inserted=2, queue_size=2, slots=slots, carry_over=0,
        )
        self.assertIn("[E008]", msg)
        # Integration tests pin these exact substrings.
        self.assertIn("План на сегодня", msg)
        self.assertIn("Принято свежих: 2", msg)
        self.assertIn("Всего в очереди: 2", msg)
        self.assertIn("10:00", msg)
        self.assertIn("15:00", msg)
        self.assertIn("Перенесено на завтра: 0", msg)

    def test_e008_plan_of_day_no_slots(self):
        msg = admin_alerts.alert_plan_of_day(
            inserted=0, queue_size=5, slots=[], carry_over=5,
        )
        self.assertIn("[E008]", msg)
        # Empty-slot indicator.
        self.assertIn("—", msg)

    def test_e009_quiet_day(self):
        msg = admin_alerts.alert_quiet_day()
        self.assertIn("[E009]", msg)
        self.assertIn("🟢", msg)
        # Integration tests pin this exact substring.
        self.assertIn("Бот сработал", msg)

    def test_e010_outage_first_ping(self):
        msg = admin_alerts.alert_outage_first_ping()
        self.assertIn("[E010]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("Claude API", msg)
        self.assertIn("Google", msg)

    def test_e011_outage_second_ping(self):
        msg = admin_alerts.alert_outage_second_ping()
        self.assertIn("[E011]", msg)
        self.assertIn("🔴", msg)
        self.assertIn("1 час", msg)

    def test_e012_outage_fallback_engaged(self):
        msg = admin_alerts.alert_outage_fallback_engaged()
        self.assertIn("[E012]", msg)
        self.assertIn("🔴", msg)
        self.assertIn("Google", msg)
        self.assertIn("2 час", msg)  # «2 часа» / «2 часов» — оба варианта

    def test_e013_outage_recovery(self):
        msg = admin_alerts.alert_outage_recovery()
        self.assertIn("[E013]", msg)
        self.assertIn("🟢", msg)
        self.assertIn("восстановилась", msg)

    def test_all_alerts_have_unique_codes(self):
        """Sanity check: no two alerts share the same [E0XX] code."""
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        all_messages = [
            admin_alerts.alert_no_rss_feeds("x"),
            admin_alerts.alert_source_fetch_failed("x", "y"),
            admin_alerts.alert_backlog_warning(1, 2, 3),
            admin_alerts.alert_claude_probe_failed_at_startup(),
            admin_alerts.alert_tz_mismatch("x"),
            admin_alerts.alert_duplicate_publish_skipped("x"),
            admin_alerts.alert_zombie_cleanup_failed("x", "y"),
            admin_alerts.alert_plan_of_day(1, 1, slots, 0),
            admin_alerts.alert_quiet_day(),
            admin_alerts.alert_outage_first_ping(),
            admin_alerts.alert_outage_second_ping(),
            admin_alerts.alert_outage_fallback_engaged(),
            admin_alerts.alert_outage_recovery(),
        ]
        codes = [m[:6] for m in all_messages]  # "[E0XX]"
        self.assertEqual(len(codes), len(set(codes)),
                         f"Duplicate codes: {codes}")
        # And all match the [E0XX] format.
        for code in codes:
            self.assertRegex(code, r"^\[E\d{3}\]$")


if __name__ == "__main__":
    unittest.main()
