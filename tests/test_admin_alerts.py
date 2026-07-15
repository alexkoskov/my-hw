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
        # New hold-and-wait behaviour: articles are held, not Google-translated.
        self.assertIn("придержан", msg)
        self.assertNotIn("Google Translate", msg)

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
        self.assertIn("придерж", msg)
        self.assertNotIn("Google Translate", msg)

    def test_e011_outage_second_ping(self):
        msg = admin_alerts.alert_outage_second_ping()
        self.assertIn("[E011]", msg)
        self.assertIn("🔴", msg)
        self.assertIn("1 час", msg)

    def test_e012_outage_still_down(self):
        msg = admin_alerts.alert_outage_still_down()
        self.assertIn("[E012]", msg)
        self.assertIn("🔴", msg)
        self.assertIn("придержан", msg)
        self.assertIn("2 час", msg)  # «2 часа» / «2 часов» — оба варианта
        self.assertNotIn("Google Translate", msg)

    def test_e013_outage_recovery(self):
        msg = admin_alerts.alert_outage_recovery()
        self.assertIn("[E013]", msg)
        self.assertIn("🟢", msg)
        self.assertIn("восстановилась", msg)

    def test_e017_channel_silent(self):
        msg = admin_alerts.alert_channel_silent(4)
        self.assertIn("[E017]", msg)
        self.assertIn("⚠️", msg)
        self.assertIn("4", msg)  # the day count is shown

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

    def test_e014_broad_series_flag(self):
        # Broad-tier soft flag: match is a series/theme (here theme-only,
        # no shared concrete model) — the model-overlap params don't apply.
        msg = admin_alerts.alert_cross_source_dupe(
            new_link="https://autoevolution.example/p/a",
            existing_link="https://t-hunted.blogspot.com/p/b",
            new_source="autoevolution",
            existing_source="t-hunted",
            pairs=["*|stranger things|B"],
        )
        self.assertIn("[E014]", msg)
        self.assertIn("🤔", msg)
        # Anchor preserved verbatim.
        self.assertIn("Похож на дубль", msg)
        # Theme-only pair renders the series without a fabricated model.
        self.assertIn("stranger things", msg)
        # No raw-key artifacts (theme marker / tier suffix / separator) leak.
        self.assertNotIn("*", msg)
        self.assertNotIn("|B", msg)
        self.assertNotIn("|", msg)
        self.assertIn("https://autoevolution.example/p/a", msg)
        self.assertIn("https://t-hunted.blogspot.com/p/b", msg)
        self.assertIn("autoevolution", msg)
        self.assertIn("t-hunted", msg)
        # Operator-guidance blocks kept.
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

    def test_e015_blocked_renders_matched_pairs(self):
        # New pair-rule path: E015 blocks on a matched distinctive
        # (model+series) pair — there is no meaningful set-overlap %.
        # Pairs are passed in REVERSE of the expected (sorted) order so the
        # test pins _render_pairs_block's `sorted(...)` determinism: dropping
        # the sort would make the output follow insertion order and fail the
        # relative-position assertion below.
        msg = admin_alerts.alert_cross_source_blocked(
            new_link="https://autoevolution.example/p/a",
            existing_link="https://t-hunted.blogspot.com/p/b",
            pairs=[
                "toyota supra|top gun|D",
                "porsche 911|k-pop demon hunters|D",
            ],
        )
        self.assertIn("[E015]", msg)
        self.assertIn("🚫", msg)
        # Anchor preserved verbatim.
        self.assertIn("Заблокирован дубль", msg)
        # Raw pair keys decoded to readable form: drop |D suffix, '|' -> ' + '.
        self.assertIn("porsche 911 + k-pop demon hunters", msg)
        self.assertIn("toyota supra + top gun", msg)
        # Deterministic sort: 'porsche...' sorts before 'toyota...' regardless
        # of the reversed insertion order above.
        self.assertLess(
            msg.index("porsche 911 + k-pop demon hunters"),
            msg.index("toyota supra + top gun"),
            "matched pairs must render in deterministic sorted order",
        )
        # No raw-key artifacts leak into the operator ping.
        self.assertNotIn("|D", msg)
        self.assertNotIn("|B", msg)
        self.assertNotIn("|", msg)
        # Earlier/canonical link + the discarded new link are both rendered.
        self.assertIn("https://t-hunted.blogspot.com/p/b", msg)
        self.assertIn("https://autoevolution.example/p/a", msg)
        # Short format preserved: still no «Что сделать» block.
        self.assertNotIn("Что сделать", msg)

    def test_e015_blocked_no_pairs_no_overlap_never_renders_none_pct(self):
        # Edge case (task spec): empty/None optional args must NOT leak a
        # literal `Совпадение: None%` into the operator ping. Both the
        # fully-omitted and the explicit-empty-list forms are covered.
        for kwargs in ({}, {"pairs": []}):
            msg = admin_alerts.alert_cross_source_blocked(
                new_link="https://autoevolution.example/p/a",
                existing_link="https://t-hunted.blogspot.com/p/b",
                **kwargs,
            )
            # Anchor + code still present so the ping is still recognizable.
            self.assertIn("[E015]", msg)
            self.assertIn("Заблокирован дубль", msg)
            # The forbidden legacy render must never appear.
            self.assertNotIn("None%", msg)
            self.assertNotIn("None", msg)

    def test_e014_broad_no_pairs_no_overlap_never_renders_none_pct(self):
        # Same edge case for the soft-flag builder: no pairs and no legacy
        # model-overlap params must NOT leak `Совпадение моделей: None% (None/None)`.
        for kwargs in ({}, {"pairs": []}):
            msg = admin_alerts.alert_cross_source_dupe(
                new_link="https://autoevolution.example/p/a",
                existing_link="https://t-hunted.blogspot.com/p/b",
                new_source="autoevolution",
                existing_source="t-hunted",
                **kwargs,
            )
            self.assertIn("[E014]", msg)
            self.assertIn("Похож на дубль", msg)
            self.assertNotIn("None%", msg)
            self.assertNotIn("None/None", msg)
            self.assertNotIn("None", msg)

    def test_e015_pair_tokens_with_underscore_and_asterisk_not_escaped(self):
        # Plain-text passthrough (parse_mode=None): markdown-significant
        # characters inside a REAL model/series token (not the theme-only '*'
        # sentinel) must pass through byte-for-byte, with no escaping.
        msg = admin_alerts.alert_cross_source_blocked(
            new_link="https://autoevolution.example/p/a",
            existing_link="https://t-hunted.blogspot.com/p/b",
            pairs=["model_x|series*name|D"],
        )
        self.assertIn("[E015]", msg)
        self.assertIn("Заблокирован дубль", msg)
        # Decoded verbatim, '_' and '*' intact inside the token.
        self.assertIn("model_x + series*name", msg)
        # No markdown escaping was introduced.
        self.assertNotIn("\\_", msg)
        self.assertNotIn("\\*", msg)

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
            admin_alerts.alert_outage_still_down(),
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
            admin_alerts.alert_publish_recap(
                {'published': 1, 'held': 1, 'failed': 1, 'moved_to_failed': 0,
                 'failures': [('u', 'boom')]},
            ),
        ]
        codes = [m[:6] for m in all_messages]  # "[E0XX]"
        self.assertEqual(len(codes), len(set(codes)),
                         f"Duplicate codes: {codes}")
        # And all match the [E0XX] format.
        for code in codes:
            self.assertRegex(code, r"^\[E\d{3}\]$")


class TestIntakeFunnel(unittest.TestCase):
    """intake-funnel diagnostic (watchdog) — E009/E008 enrichment + the
    pure ``_format_funnel`` helper. The funnel is a plain-int dict built in
    ``news_bot.job()`` step (b); these builders must render it safely and
    NEVER raise, even on malformed input."""

    # A funnel where sources produced entries but every candidate was
    # dropped at the cross-source dedup stage → intake collapsed at dedup.
    DEDUP_COLLAPSE = {
        'sources_fetched': 5,
        'sources_failed': 0,
        'new_count': 3,
        'dropped_no_article': 0,
        'dropped_checklist': 0,
        'dropped_dedup_block': 3,
        'dedup_degraded': 0,
        'staged': 0,
    }

    BUSY = {
        'sources_fetched': 8,
        'sources_failed': 1,
        'new_count': 4,
        'dropped_no_article': 1,
        'dropped_checklist': 0,
        'dropped_dedup_block': 1,
        'dedup_degraded': 0,
        'staged': 2,
    }

    # Sources all threw → nothing fetched → collapse at fetch (failed > 0).
    SOURCES_DOWN = {
        'sources_fetched': 0,
        'sources_failed': 2,
        'new_count': 0,
        'dropped_no_article': 0,
        'dropped_checklist': 0,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'staged': 0,
    }

    # Sources answered but returned zero entries → collapse at fetch (no new).
    NO_ENTRIES = {
        'sources_fetched': 0,
        'sources_failed': 0,
        'new_count': 0,
        'dropped_no_article': 0,
        'dropped_checklist': 0,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'staged': 0,
    }

    # Entries fetched but the pending/processed filters dropped every one
    # (new_count == 0) → "все записи уже известны".
    ALL_KNOWN = {
        'sources_fetched': 4,
        'sources_failed': 0,
        'new_count': 0,
        'dropped_no_article': 0,
        'dropped_checklist': 0,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'staged': 0,
    }

    # new > 0, nothing staged, "no article/text" is the dominant drop stage.
    NO_ARTICLE_MAX = {
        'sources_fetched': 7,
        'sources_failed': 0,
        'new_count': 6,
        'dropped_no_article': 5,
        'dropped_checklist': 1,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'staged': 0,
    }

    # new > 0, nothing staged, "checklist without text" is the dominant stage.
    CHECKLIST_MAX = {
        'sources_fetched': 6,
        'sources_failed': 0,
        'new_count': 5,
        'dropped_no_article': 1,
        'dropped_checklist': 4,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'staged': 0,
    }

    # ------------------------------------------------------------------
    # _format_funnel — pure helper shape + fail-safety
    # ------------------------------------------------------------------
    def test_format_funnel_shape(self):
        block = admin_alerts._format_funnel(self.DEDUP_COLLAPSE)
        self.assertIsInstance(block, str)
        self.assertIn("Воронка", block)
        # Every stage number is rendered — assert LABEL+digit so a stray digit
        # elsewhere in the block can't accidentally satisfy the check.
        self.assertIn("получено записей: 5", block)     # sources fetched (entries)
        self.assertIn("новых после фильтров: 3", block)  # new after filters
        # Drop labels present.
        self.assertIn("дубль-блок", block)
        self.assertIn("нет статьи", block)
        self.assertIn("чеклист", block)
        # Collapse stage pinpointed at dedup. Assert the collapse-note-SPECIFIC
        # line (label + PARENTHESISED count) — this exact format can ONLY come
        # from _funnel_collapse_note picking 'дубль-блок' as the winning stage;
        # the fixed breakdown line above uses 'дубль-блок 3' (no parentheses),
        # so a neutered note that stops pinpointing would fail this assertion.
        self.assertIn("Где схлопнулось: дубль-блок (3)", block)

    def test_format_funnel_all_zero_or_empty_renders_safely(self):
        # Empty dict and an all-zero dict must both render without raising
        # and still produce a readable string.
        for funnel in ({}, dict.fromkeys(self.DEDUP_COLLAPSE, 0)):
            block = admin_alerts._format_funnel(funnel)
            self.assertIsInstance(block, str)
            self.assertIn("Воронка", block)

    def test_format_funnel_non_dict_returns_empty(self):
        for bad in (None, "not a dict", 12345, ["list"], object()):
            self.assertEqual(admin_alerts._format_funnel(bad), "")

    # ------------------------------------------------------------------
    # _funnel_collapse_note — one assertion per winning stage. These are the
    # tests the round-1 review found missing: every branch of the note must
    # name the RIGHT stage, so a broken max()/branch order is caught. Each
    # asserts the collapse-note-SPECIFIC line, not a breakdown fragment.
    # ------------------------------------------------------------------
    def test_collapse_note_sources_failed(self):
        # sources_fetched == 0 AND a source threw → blame the fetch stage.
        block = admin_alerts._format_funnel(self.SOURCES_DOWN)
        self.assertIn("Где схлопнулось: источники не ответили (2)", block)

    def test_collapse_note_no_entries_fetched(self):
        # sources_fetched == 0, none threw → sources simply had nothing new.
        block = admin_alerts._format_funnel(self.NO_ENTRIES)
        self.assertIn("Где схлопнулось: источники не дали новых записей", block)
        # Must NOT be attributed to a failure when nothing threw.
        self.assertNotIn("источники не ответили", block)

    def test_collapse_note_all_known(self):
        # Entries fetched but new_count == 0 → filters already knew them all.
        block = admin_alerts._format_funnel(self.ALL_KNOWN)
        self.assertIn(
            "Где схлопнулось: все записи уже известны (фильтры отсеяли всё)",
            block,
        )

    def test_collapse_note_no_article_dominant(self):
        # new > 0, nothing staged, no-article is the max drop → name it.
        block = admin_alerts._format_funnel(self.NO_ARTICLE_MAX)
        self.assertIn("Где схлопнулось: нет статьи/текста (5)", block)
        # The runner-up (checklist) must NOT be the one pinpointed.
        self.assertNotIn("Где схлопнулось: чеклист", block)

    def test_collapse_note_checklist_dominant(self):
        # new > 0, nothing staged, checklist is the max drop → name it.
        block = admin_alerts._format_funnel(self.CHECKLIST_MAX)
        self.assertIn("Где схлопнулось: чеклист без текста (4)", block)
        # The runner-up (no-article) must NOT be the one pinpointed.
        self.assertNotIn("Где схлопнулось: нет статьи", block)

    # ------------------------------------------------------------------
    # E009 — alert_quiet_day enrichment + back-compat
    # ------------------------------------------------------------------
    def test_e009_quiet_day_with_funnel_renders_breakdown(self):
        msg = admin_alerts.alert_quiet_day(funnel=self.DEDUP_COLLAPSE)
        # Anchor + legacy first line preserved.
        self.assertIn("[E009]", msg)
        self.assertIn("🟢", msg)
        self.assertIn("Бот сработал", msg)
        # Funnel breakdown appended.
        self.assertIn("Воронка", msg)
        self.assertIn("дубль-блок", msg)
        # Collapse stage pinpointed at dedup — assert the collapse-note-SPECIFIC
        # format (label + parenthesised count), not the bare 'дубль-блок' which
        # is already guaranteed by the breakdown line above.
        self.assertIn("Где схлопнулось: дубль-блок (3)", msg)
        # Scope note: translate/post is N/A when the queue is empty.
        self.assertIn("очередь пуста", msg)
        # Plain-text only — no markdown formatting sneaks in.
        self.assertNotIn("**", msg)
        # No secret shapes leak (funnel is ints only, belt-and-suspenders).
        self.assertNotIn("sk-", msg)

    def test_e009_quiet_day_no_arg_backcompat(self):
        # Legacy zero-arg call must still render the exact single line.
        msg = admin_alerts.alert_quiet_day()
        self.assertIn("[E009]", msg)
        self.assertIn("Бот сработал", msg)
        self.assertNotIn("Воронка", msg)

    def test_e009_quiet_day_funnel_none_backcompat(self):
        # Explicit funnel=None behaves like the legacy call.
        self.assertEqual(
            admin_alerts.alert_quiet_day(funnel=None),
            admin_alerts.alert_quiet_day(),
        )

    def test_e009_quiet_day_broken_funnel_does_not_raise(self):
        # A malformed funnel must NOT break the builder. NOTE the two distinct
        # fallbacks: a NON-DICT funnel ("boom"/123/["x"]/object()) returns ""
        # → the legacy single-line ping. A DICT with a bad-valued field
        # ({"sources_fetched": "NaN"}) does NOT fall back — each bad field is
        # coerced to 0 and a zeroed «Воронка» breakdown is rendered. Either way
        # the anchor + legacy first line are present, which is all we assert.
        for bad in ("boom", 123, ["x"], object(), {"sources_fetched": "NaN"}):
            msg = admin_alerts.alert_quiet_day(funnel=bad)
            self.assertIn("[E009]", msg)
            self.assertIn("Бот сработал", msg)

    # ------------------------------------------------------------------
    # E008 — alert_plan_of_day enrichment + legacy positional call
    # ------------------------------------------------------------------
    def test_e008_plan_of_day_legacy_positional_unchanged(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        # Existing positional call (no funnel) must keep working verbatim.
        msg = admin_alerts.alert_plan_of_day(2, 2, slots, 0)
        self.assertIn("[E008]", msg)
        self.assertIn("План на сегодня", msg)
        self.assertIn("Принято свежих: 2", msg)
        self.assertNotIn("Приём:", msg)

    def test_e008_plan_of_day_with_funnel_adds_compact_line(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        msg = admin_alerts.alert_plan_of_day(2, 2, slots, 0, funnel=self.BUSY)
        self.assertIn("[E008]", msg)
        self.assertIn("План на сегодня", msg)
        self.assertIn("Принято свежих: 2", msg)
        # Compact one-line intake summary appended.
        self.assertIn("Приём:", msg)
        self.assertIn("в очередь 2", msg)
        # The BUSY fixture was built to exercise the failed-source and dropped
        # parts of the compact line — pin them so a bug that drops the
        # `failed_part` branch or miscomputes the drop sum is caught.
        self.assertIn("источники-сбои 1", msg)   # sources_failed == 1
        self.assertIn("отсеяно 2", msg)          # no_article(1)+checklist(0)+block(1)

    def test_e008_plan_of_day_broken_funnel_does_not_raise(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        for bad in ("boom", 123, object(), {"staged": "NaN"}):
            msg = admin_alerts.alert_plan_of_day(1, 1, slots, 0, funnel=bad)
            self.assertIn("[E008]", msg)
            self.assertIn("План на сегодня", msg)


class TestPublishRecap(unittest.TestCase):
    """[E034] end-of-tick PUBLISH-stage recap — companion to the E008/E009
    intake funnel. Renders the per-slot outcome counters accumulated by
    ``news_bot.job()`` step (e) plus a capped, pre-sanitized list of failure
    reasons. Contract mirrors the funnel helpers: PURE, plain-text (no
    markdown / parse_mode), and NEVER raises even on malformed input.
    """

    ALL_CLEAN = {
        'published': 3,
        'held': 0,
        'failed': 0,
        'moved_to_failed': 0,
        'failures': [],
    }

    HELD_AND_FAILED = {
        'published': 1,
        'held': 2,
        'failed': 1,
        'moved_to_failed': 1,
        'failures': [
            ('http://example.com/a', 'ClaudeTranscreationError: malformed JSON'),
        ],
    }

    def test_all_published_compact_green_line(self):
        msg = admin_alerts.alert_publish_recap(self.ALL_CLEAN)
        self.assertIn("[E034]", msg)
        self.assertIn("🟢", msg)
        # published/attempted tally — all clean so N/N.
        self.assertIn("опубликовано 3/3", msg)
        # Compact: no failure/held sections on a clean tick.
        self.assertNotIn("провал", msg)
        self.assertNotIn("придержано", msg)
        # Plain-text only.
        self.assertNotIn("**", msg)

    def test_held_and_failed_expanded_yellow(self):
        msg = admin_alerts.alert_publish_recap(self.HELD_AND_FAILED)
        self.assertIn("[E034]", msg)
        self.assertIn("🟡", msg)
        # Tally: 1 published of 4 attempted (1 published + 2 held + 1 failed).
        self.assertIn("опубликовано 1/4", msg)
        # Held note (generic — no internals leaked).
        self.assertIn("придержано 2", msg)
        self.assertIn("Claude недоступна", msg)
        # Failed count + the ≥3-strike subset. Pin the EXACT tail (not just the
        # "провалов: 1" prefix) so deleting the moved_to_failed rendering fails.
        self.assertIn("провалов: 1 (снято после 3 промахов: 1)", msg)
        # Per-failure line: link + sanitized reason.
        self.assertIn("провал: http://example.com/a", msg)
        self.assertIn("malformed JSON", msg)
        self.assertNotIn("**", msg)

    def test_failed_but_none_moved_omits_strike_tail(self):
        # Negative case for the ≥3-strike tail: when moved_to_failed == 0 the
        # tail must be ABSENT, and the count line renders bare "провалов: N".
        recap = {
            'published': 0, 'held': 0, 'failed': 1, 'moved_to_failed': 0,
            'failures': [('http://example.com/a', 'boom')],
        }
        msg = admin_alerts.alert_publish_recap(recap)
        self.assertIn("провалов: 1", msg)
        self.assertNotIn("снято после", msg)

    def test_failure_list_capped_at_five(self):
        recap = {
            'published': 0, 'held': 0,
            'failed': 8, 'moved_to_failed': 0,
            'failures': [
                (f'http://example.com/{i}', f'reason {i}') for i in range(8)
            ],
        }
        msg = admin_alerts.alert_publish_recap(recap)
        rendered = [ln for ln in msg.splitlines() if ln.startswith('провал:')]
        # Exact cap, not just an upper bound: pins the value to
        # RECAP_MAX_FAILURES (5) so an off-by-N that caps at 0/1/3 is caught.
        self.assertEqual(len(rendered), 5)
        self.assertEqual(len(rendered), admin_alerts.RECAP_MAX_FAILURES)
        # The count line still reflects the true total.
        self.assertIn("провалов: 8", msg)

    def test_malformed_failures_value_renders_tally_without_failure_lines(self):
        # Exercises _recap_failure_lines' non-list defensive branch: a
        # malformed (non-list) `failures` value must be skipped silently — the
        # tally lines still render, but no "провал:" section appears.
        recap = {
            'published': 0, 'held': 1, 'failed': 1, 'moved_to_failed': 0,
            'failures': 'oops',  # not a list/tuple
        }
        msg = admin_alerts.alert_publish_recap(recap)
        self.assertIn("[E034]", msg)
        self.assertIn("провалов: 1", msg)
        self.assertIn("придержано 1", msg)
        # No per-failure line could be rendered from a non-list value.
        self.assertNotIn("провал:", msg)
        self.assertNotIn("**", msg)

    def test_builder_never_renders_raw_secret_text(self):
        # Belt-and-suspenders: the builder does NOT itself redact — reasons
        # arrive already sanitized upstream (news_bot.sanitize_error_message,
        # pinned end-to-end by test_integration's
        # test_failed_reason_with_secret_is_sanitized_in_recap). This test only
        # confirms an already-redacted marker survives rendering unchanged and
        # the builder never injects a token/secret shape of its own.
        recap = {
            'published': 1, 'held': 0, 'failed': 1, 'moved_to_failed': 0,
            'failures': [('http://example.com/x', 'Telegram API 500 [REDACTED]')],
        }
        msg = admin_alerts.alert_publish_recap(recap)
        self.assertNotIn("sk-", msg)
        self.assertNotIn("Bearer ", msg)
        self.assertIn("[REDACTED]", msg)

    def test_empty_zero_input_handled(self):
        empty = {'published': 0, 'held': 0, 'failed': 0,
                 'moved_to_failed': 0, 'failures': []}
        msg = admin_alerts.alert_publish_recap(empty)
        self.assertIn("[E034]", msg)
        self.assertIn("🟢", msg)
        self.assertIn("опубликовано 0/0", msg)

    def test_broken_recap_input_does_not_raise(self):
        for bad in ("boom", 123, ["x"], object(), None,
                    {'published': 'NaN', 'failures': 'oops'}):
            msg = admin_alerts.alert_publish_recap(bad)
            # Anchor always present — the builder degrades, never raises.
            self.assertIn("[E034]", msg)
            self.assertNotIn("**", msg)

    def test_non_dict_recap_pins_explicit_guard_message(self):
        # Pin the top-level `if not isinstance(recap, dict)` guard's OWN output
        # text, so deleting the guard fails even though _funnel_int would
        # otherwise degrade a non-dict silently into the compact branch. The
        # fallback is 🟡 (degraded), matching the inner-exception fallback.
        expected = "[E034] 🟡 Публикация: отчёт недоступен"
        for bad in ("boom", 123, ["x"], object(), None):
            self.assertEqual(admin_alerts.alert_publish_recap(bad), expected)

    def test_plain_text_no_markdown(self):
        msg = admin_alerts.alert_publish_recap(self.HELD_AND_FAILED)
        for token in ("**", "```", "__", "]("):
            self.assertNotIn(token, msg)


class TestOpenRouterLowBalanceAlert(unittest.TestCase):
    def test_e019_openrouter_low_balance(self):
        msg = admin_alerts.alert_openrouter_low_balance(3.25, 5.0)
        self.assertTrue(msg.startswith("[E019]"))
        self.assertIn("3.25", msg)   # remaining
        self.assertIn("5.00", msg)   # threshold
        self.assertIn("openrouter", msg.lower())
        self.assertIn("пополни", msg.lower())  # Russian call-to-action


if __name__ == "__main__":
    unittest.main()
