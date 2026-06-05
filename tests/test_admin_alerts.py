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

    # ------------------------------------------------------------------
    # Source-fetcher alerts (E020-E030)
    # ------------------------------------------------------------------

    def test_e020_mattel_news_http_error(self):
        msg = admin_alerts.alert_mattel_news_http_error("ConnectionError")
        self.assertIn("[E020]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("Mattel", msg)
        self.assertIn("ConnectionError", msg)

    def test_e021_mattel_news_parsing_error(self):
        msg = admin_alerts.alert_mattel_news_parsing_error(
            "article2.entries not found"
        )
        self.assertIn("[E021]", msg)
        self.assertIn("🔴", msg)
        self.assertIn("article2.entries not found", msg)
        self.assertIn("mattel_news_source.py", msg)

    def test_e022_mattel_news_generic(self):
        msg = admin_alerts.alert_mattel_news_generic("response too large: 9999")
        self.assertIn("[E022]", msg)
        self.assertIn("response too large", msg)

    def test_e023_mattel_article_invalid_link(self):
        msg = admin_alerts.alert_mattel_article_invalid_link()
        self.assertIn("[E023]", msg)
        self.assertIn("allowlist", msg)

    def test_e024_mattel_article_fetch_error(self):
        msg = admin_alerts.alert_mattel_article_fetch_error(
            "https://corporate.mattel.com/news/x", "Timeout"
        )
        self.assertIn("[E024]", msg)
        self.assertIn("https://corporate.mattel.com/news/x", msg)
        self.assertIn("Timeout", msg)

    def test_e025_lamley_host_rejected(self):
        msg = admin_alerts.alert_lamley_host_rejected("https://evil.example.com/")
        self.assertIn("[E025]", msg)
        self.assertIn("https://evil.example.com/", msg)
        self.assertIn("allowlist", msg)

    def test_e026_lamley_article_too_large(self):
        msg = admin_alerts.alert_lamley_article_too_large(5_000_000)
        self.assertIn("[E026]", msg)
        self.assertIn("5000000", msg)

    def test_e027_lamley_fetch_error(self):
        msg = admin_alerts.alert_lamley_fetch_error(
            "https://lamleygroup.com/p/x", "HTTP 503"
        )
        self.assertIn("[E027]", msg)
        self.assertIn("https://lamleygroup.com/p/x", msg)
        self.assertIn("HTTP 503", msg)

    def test_e028_lamley_no_body(self):
        msg = admin_alerts.alert_lamley_no_body("https://lamleygroup.com/p/y")
        self.assertIn("[E028]", msg)
        self.assertIn("entry-content", msg)

    def test_e031_t_hunted_host_rejected(self):
        msg = admin_alerts.alert_t_hunted_host_rejected(
            "https://evil.example.com/"
        )
        self.assertIn("[E031]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("https://evil.example.com/", msg)
        # Builder must mention SSRF-rejection in Russian — accept either
        # "хост" wording or "allowlist".
        self.assertTrue(
            ("хост" in msg) or ("allowlist" in msg),
            f"Expected 'хост' or 'allowlist' in alert text, got: {msg!r}",
        )

    def test_e032_t_hunted_fetch_error(self):
        msg = admin_alerts.alert_t_hunted_fetch_error(
            "https://t-hunted.blogspot.com/x", "HTTP 503"
        )
        self.assertIn("[E032]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("https://t-hunted.blogspot.com/x", msg)
        self.assertIn("HTTP 503", msg)

    def test_e033_t_hunted_no_body(self):
        msg = admin_alerts.alert_t_hunted_no_body(
            "https://t-hunted.blogspot.com/y"
        )
        self.assertIn("[E033]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("https://t-hunted.blogspot.com/y", msg)
        # Builder must mention missing body — accept any of the documented
        # phrasings (Russian wording or selector name).
        self.assertTrue(
            (
                "не нашёл тело" in msg
                or "не найдено тело" in msg
                or "post-body" in msg
                or "entry-content" in msg
            ),
            f"Expected body-missing wording in alert text, got: {msg!r}",
        )

    def test_e030_orangetrack_summary_header(self):
        msg = admin_alerts.alert_orangetrack_summary_header(7)
        self.assertIn("[E030]", msg)
        self.assertIn("🟡", msg)
        self.assertIn("Orangetrack", msg)
        self.assertIn("7", msg)
        # Backwards-compat: integration tests могут полагаться на формат
        # с числом проблем.

    # ------------------------------------------------------------------
    # Cross-source dedup alerts (E014, E015, E016)
    # ------------------------------------------------------------------

    def test_e014_cross_source_dupe(self):
        msg = admin_alerts.alert_cross_source_dupe(
            new_link="https://orangetrack.example/p/a",
            existing_link="https://lamleygroup.com/p/b",
            new_source="orangetrack",
            existing_source="lamley",
            overlap_pct=35,
            n_matches=2,
            n_total=6,
            models=["toyota 4runner", "subaru legacy gt"],
        )
        self.assertIn("[E014]", msg)
        self.assertIn("🤔", msg)
        # Integration tests pin this exact substring (tech-spec Decision 7).
        self.assertIn("Похож на дубль", msg)
        self.assertIn("https://orangetrack.example/p/a", msg)
        self.assertIn("https://lamleygroup.com/p/b", msg)
        self.assertIn("orangetrack", msg)
        self.assertIn("lamley", msg)
        self.assertIn("35%", msg)
        self.assertIn("2/6", msg)
        self.assertIn("toyota 4runner", msg)
        self.assertIn("subaru legacy gt", msg)
        self.assertIn("Что произошло", msg)
        self.assertIn("Что сделать", msg)

    def test_e015_cross_source_blocked(self):
        msg = admin_alerts.alert_cross_source_blocked(
            new_link="https://orangetrack.example/p/a",
            existing_link="https://lamleygroup.com/p/b",
            overlap_pct=72,
        )
        self.assertIn("[E015]", msg)
        self.assertIn("🚫", msg)
        # Integration tests pin this exact substring (tech-spec Decision 7).
        self.assertIn("Заблокирован дубль", msg)
        self.assertIn("https://orangetrack.example/p/a", msg)
        self.assertIn("https://lamleygroup.com/p/b", msg)
        self.assertIn("72%", msg)
        # Format pin: E015 is intentionally short — no operator action block.
        self.assertNotIn("Что сделать", msg)

    def test_e016_dedup_degraded(self):
        msg = admin_alerts.alert_dedup_degraded(reason="AttributeError")
        self.assertIn("[E016]", msg)
        self.assertIn("⚠️", msg)
        # Integration tests pin this exact substring (tech-spec Decision 7).
        # NOTE: code-research §14.K.3 used "Дедуп упал (degraded mode)" —
        # tech-spec Decision 7 overrides with "Дедуп в degraded mode".
        self.assertIn("Дедуп в degraded mode", msg)
        self.assertIn("degraded", msg)
        self.assertIn("AttributeError", msg)
        self.assertIn("Что произошло", msg)
        self.assertIn("Что сделать", msg)

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
            admin_alerts.alert_mattel_news_http_error("x"),
            admin_alerts.alert_mattel_news_parsing_error("x"),
            admin_alerts.alert_mattel_news_generic("x"),
            admin_alerts.alert_mattel_article_invalid_link(),
            admin_alerts.alert_mattel_article_fetch_error("x", "y"),
            admin_alerts.alert_lamley_host_rejected("x"),
            admin_alerts.alert_lamley_article_too_large(1),
            admin_alerts.alert_lamley_fetch_error("x", "y"),
            admin_alerts.alert_lamley_no_body("x"),
            admin_alerts.alert_t_hunted_host_rejected("x"),
            admin_alerts.alert_t_hunted_fetch_error("x", "y"),
            admin_alerts.alert_t_hunted_no_body("x"),
            admin_alerts.alert_orangetrack_summary_header(1),
            admin_alerts.alert_cross_source_dupe(
                "u", "v", "s1", "s2", 35, 2, 6, ["m1", "m2"],
            ),
            admin_alerts.alert_cross_source_blocked("u", "v", 72),
            admin_alerts.alert_dedup_degraded("AttributeError"),
        ]
        codes = [m[:6] for m in all_messages]  # "[E0XX]"
        self.assertEqual(len(codes), len(set(codes)),
                         f"Duplicate codes: {codes}")
        # And all match the [E0XX] format.
        for code in codes:
            self.assertRegex(code, r"^\[E\d{3}\]$")


if __name__ == "__main__":
    unittest.main()
