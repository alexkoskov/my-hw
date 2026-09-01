#!/usr/bin/env python3
"""Unit tests for admin_alerts builders.

Each E0XX builder is a pure str -> str / params -> str function. Tests cover:
- Each alert returns its [E0XX] code.
- Severity emoji in first line matches the documented level.
- Russian-language headers (no English leftovers).
- Substrings that integration tests rely on are preserved verbatim.
"""
import ast
import inspect
import os
import secrets
import sys
import unittest
from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardMarkup

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin_alerts


MSK = timezone(timedelta(hours=3))


def _operator_facing_literals():
    """Every string literal in `admin_alerts` that can reach an operator.

    That means all `str` constants (including the pieces of f-strings, which
    is how every builder composes its text) MINUS module/class/function
    docstrings. Comments never enter the AST at all.

    Why not a plain substring scan over `inspect.getsource`: a source-level
    ban cannot tell alert TEXT from a comment explaining why a given tool
    must never be advertised — it would forbid the code from documenting its
    own rationale, and it dumps the whole module into the failure message.
    """
    tree = ast.parse(inspect.getsource(admin_alerts))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            docstrings.add(id(body[0].value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


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
        # The advice must stay actionable on prod: `hw_review.py` is archived
        # (2026-04-30) and was never deployed to the server, so naming it sent
        # the operator after a tool that does not exist there.
        self.assertNotIn("hw_review", msg)
        self.assertIn("Что сделать", msg)

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

    def test_e014_reason_variants_are_truthful(self):
        common = {
            "new_link": "https://autoevolution.example/p/a",
            "existing_link": "https://t-hunted.example/p/b",
            "new_source": "autoevolution",
            "existing_source": "t-hunted",
            "buttons_enabled": True,
        }
        messages = {
            "broad_subject": admin_alerts.alert_cross_source_dupe(
                **common,
                reason="broad_subject",
                pairs=["*|stranger things|B"],
                # Rejected series are diagnostics for capped overlap only and
                # must never be presented as a qualified broad match.
                subject_rejected_series=["car culture"],
            ),
            "overlap": admin_alerts.alert_cross_source_dupe(
                **common,
                reason="overlap",
                overlap_pct=35,
                n_matches=2,
                n_total=6,
                models=["toyota 4runner", "subaru legacy gt"],
            ),
            "overlap_capped": admin_alerts.alert_cross_source_dupe(
                **common,
                reason="overlap_capped",
                overlap_pct=100,
                n_matches=1,
                n_total=1,
                models=["toyota 4runner"],
                subject_rejected_series=["car culture"],
            ),
        }

        broad = messages["broad_subject"]
        self.assertIn("Совпавшая серия/тема:\nstranger things", broad)
        self.assertNotIn("car culture", broad)
        self.assertNotIn("50%", broad)
        self.assertNotIn("не достигнут", broad)

        overlap = messages["overlap"]
        self.assertIn("35%", overlap)
        self.assertIn("порог автоблокировки (50%) не достигнут", overlap)

        capped = messages["overlap_capped"]
        self.assertIn("car culture", capped)
        self.assertIn("порог автоблокировки достигнут", capped)
        self.assertIn("автоматической блокировки нет", capped)
        self.assertNotIn("не достигнут", capped)

        # Reason-specific copy must not change the shared operator action
        # contract or the real keyboard labels it quotes.
        action_blocks = {
            reason: msg.split("Что сделать:\n", 1)[1]
            for reason, msg in messages.items()
        }
        self.assertEqual(len(set(action_blocks.values())), 1)
        for msg in messages.values():
            self.assertIn("🚫 Не публиковать", msg)
            self.assertIn("👍 Оставить", msg)

    def test_e014_buttons_enabled_advises_pressing_the_buttons(self):
        """`buttons_enabled=True` (send site: keyboard attached) — the advice
        must point at the two inline buttons, the only operator action that
        actually works on prod. Both match tiers are covered: the legacy
        set-overlap shape and the broad `pairs=` shape share the advice
        block, so neither may lose it."""
        tiers = {
            "set-overlap": dict(
                overlap_pct=35, n_matches=2, n_total=6,
                models=["toyota 4runner"],
            ),
            "broad-pair": dict(pairs=["*|stranger things|B"]),
        }
        for tier, kwargs in tiers.items():
            with self.subTest(tier=tier):
                msg = admin_alerts.alert_cross_source_dupe(
                    new_link="https://orangetrack.example/p/a",
                    existing_link="https://lamleygroup.com/p/b",
                    new_source="orangetrack",
                    existing_source="lamley",
                    buttons_enabled=True,
                    **kwargs,
                )
                # Anchor preserved verbatim (integration tests + rate-limit).
                self.assertIn("Похож на дубль", msg)
                self.assertIn("Что сделать", msg)
                # Button labels quoted verbatim from the keyboard builder —
                # the operator matches the text against what he sees.
                self.assertIn("🚫 Не публиковать", msg)
                self.assertIn("👍 Оставить", msg)

    def test_e014_advice_quotes_the_real_button_labels(self):
        """Label parity: the advice hardcodes the button captions, so a
        rename in `build_dedup_review_keyboard` would silently reintroduce
        the advice-vs-reality drift this alert text exists to prevent.
        Pin the two against each other, not just each against a literal."""
        labels = [
            b.text
            for row in admin_alerts.build_dedup_review_keyboard("tok").inline_keyboard
            for b in row
        ]
        msg = admin_alerts.alert_cross_source_dupe(
            "u", "v", "s1", "s2", 35, 2, 6, ["m1"], buttons_enabled=True,
        )
        for label in labels:
            self.assertIn(
                label, msg,
                f"button caption {label!r} is not quoted in the [E014] "
                f"advice — rename the caption and the advice together",
            )

    def test_e014_buttons_disabled_gives_no_tool_advice(self):
        """`buttons_enabled=False` (the default — flag off, no keyboard):
        the text must say POSITIVELY that there is nothing to do. Asserting
        only the absence of `hw_review` would guard the bug by name — any
        other phantom tool («открой админ-панель…») would pass. So we pin
        the «no action exists» wording itself."""
        msg = admin_alerts.alert_cross_source_dupe(
            new_link="https://orangetrack.example/p/a",
            existing_link="https://lamleygroup.com/p/b",
            new_source="orangetrack",
            existing_source="lamley",
            overlap_pct=35,
            n_matches=2,
            n_total=6,
            models=["toyota 4runner"],
        )
        self.assertIn("Похож на дубль", msg)
        self.assertIn("Что сделать", msg)
        # Positive semantic: the advice IS "ничего … нечем".
        self.assertIn("ничего", msg)
        self.assertIn("нечем", msg)
        # No phantom buttons: nothing is rendered under this message.
        self.assertNotIn("🚫 Не публиковать", msg)
        self.assertNotIn("👍 Оставить", msg)

    def test_no_alert_names_an_undeployed_operator_tool(self):
        """Guard the bug CLASS, not one file name: no alert may send the
        operator to a tool that does not exist on the prod container (no
        CLI, no admin panel, no shell script). Covers every builder that
        takes no required args plus the two that carried the bug."""
        forbidden = (
            "hw_review",      # archived 2026-04-30, never deployed
            "админ-панел",    # no web UI exists
            "админку",
            "консол",         # no operator console on the container
        )
        messages = [
            admin_alerts.alert_backlog_warning(80, 50, 12),
            admin_alerts.alert_cross_source_dupe(
                "u", "v", "s1", "s2", 35, 2, 6, ["m1"],
            ),
            admin_alerts.alert_cross_source_dupe(
                "u", "v", "s1", "s2", 35, 2, 6, ["m1"], buttons_enabled=True,
            ),
        ]
        for msg in messages:
            for token in forbidden:
                self.assertNotIn(
                    token, msg.lower(),
                    f"alert points the operator at a non-existent tool "
                    f"({token!r}): {msg!r}",
                )

    def test_e014_buttons_enabled_is_keyword_only(self):
        """The new arg must not shift the existing positional contract —
        legacy positional callers keep working and land on the no-buttons
        branch."""
        with self.assertRaises(TypeError):
            admin_alerts.alert_cross_source_dupe(
                "u", "v", "s1", "s2", 35, 2, 6, ["m1"], True,
            )
        legacy = admin_alerts.alert_cross_source_dupe(
            "u", "v", "s1", "s2", 35, 2, 6, ["m1"],
        )
        self.assertIn("Похож на дубль", legacy)
        self.assertNotIn("🚫 Не публиковать", legacy)

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
            admin_alerts.alert_hold_cap_reached("u", "T", 6, "402", 24),
        ]
        codes = [m[:6] for m in all_messages]  # "[E0XX]"
        self.assertEqual(len(codes), len(set(codes)),
                         f"Duplicate codes: {codes}")
        # And all match the [E0XX] format.
        for code in codes:
            self.assertRegex(code, r"^\[E\d{3}\]$")

    def test_no_alert_advises_the_archived_hw_review_cli(self):
        """Regression guard (2026-07-25): `hw_review.py` is archived since
        2026-04-30 and was never deployed to the prod container, so no admin
        alert may tell the operator to run it.

        Checked over the module's string LITERALS (see `_operator_facing_literals`)
        rather than raw source: it covers every builder, including ones the
        sample list above misses, while leaving comments and docstrings free
        to explain WHY the CLI must not be advertised."""
        offenders = [
            s for s in _operator_facing_literals() if "hw_review" in s
        ]
        self.assertEqual(
            offenders, [],
            "admin alert text must not point the operator at the archived, "
            f"never-deployed hw_review.py CLI; offending literals: {offenders!r}",
        )


class TestDedupReviewKeyboard(unittest.TestCase):
    """build_dedup_review_keyboard — inline keyboard for the E014 review
    buttons (dedup-review-buttons Decision 3 callback_data grammar:
    ``dd:c:<token>`` cancel / ``dd:k:<token>`` keep, cancel FIRST)."""

    @staticmethod
    def _flat_buttons(kb):
        """Flatten rows in order — row layout (1x2 vs 2x1) is not part of
        the contract, the button ORDER is."""
        return [b for row in kb.inline_keyboard for b in row]

    def test_returns_inline_keyboard_markup(self):
        kb = admin_alerts.build_dedup_review_keyboard("tok")
        self.assertIsInstance(kb, InlineKeyboardMarkup)

    def test_two_buttons_callback_data(self):
        kb = admin_alerts.build_dedup_review_keyboard("tok")
        buttons = self._flat_buttons(kb)
        self.assertEqual(len(buttons), 2)
        self.assertEqual(
            [b.callback_data for b in buttons],
            ["dd:c:tok", "dd:k:tok"],  # cancel first, keep second
        )

    def test_button_labels(self):
        kb = admin_alerts.build_dedup_review_keyboard("tok")
        texts = [b.text for b in self._flat_buttons(kb)]
        self.assertIn("🚫 Не публиковать", texts[0])
        self.assertIn("👍 Оставить", texts[1])

    def test_callback_data_under_64_bytes(self):
        # Realistic token as minted by the sender (Task 3):
        # secrets.token_urlsafe(9) → ~12 url-safe chars.
        token = secrets.token_urlsafe(9)
        kb = admin_alerts.build_dedup_review_keyboard(token)
        for b in self._flat_buttons(kb):
            self.assertLessEqual(
                len(b.callback_data.encode("utf-8")), 64,
                f"callback_data over Telegram 64-byte limit: {b.callback_data!r}",
            )


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

    # new > 0, nothing staged, promo-filter ([E035]) is the dominant stage.
    PROMO_MAX = {
        'sources_fetched': 6,
        'sources_failed': 0,
        'new_count': 5,
        'dropped_no_article': 1,
        'dropped_checklist': 0,
        'dropped_promo': 4,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'staged': 0,
    }

    # new > 0, nothing staged, the content gate's genre drop ([E037]) is
    # the dominant stage.
    GENRE_MAX = {
        'sources_fetched': 6,
        'sources_failed': 0,
        'new_count': 5,
        'dropped_no_article': 1,
        'dropped_checklist': 0,
        'dropped_promo': 0,
        'dropped_genre': 4,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'held_for_review': 0,
        'staged': 0,
    }

    # A tick where the only intake result was a HOLD ([E036]): the row is
    # staged, so there is no collapse to report, but the operator must see
    # that the article is parked rather than queued.
    HELD_ONLY = {
        'sources_fetched': 3,
        'sources_failed': 0,
        'new_count': 1,
        'dropped_no_article': 0,
        'dropped_checklist': 0,
        'dropped_promo': 0,
        'dropped_genre': 0,
        'dropped_dedup_block': 0,
        'dedup_degraded': 0,
        'held_for_review': 1,
        'staged': 1,
    }

    # ------------------------------------------------------------------
    # Content-gate counters (2026-07-25)
    # ------------------------------------------------------------------
    def test_format_funnel_renders_genre_drops(self):
        block = admin_alerts._format_funnel(self.GENRE_MAX)
        self.assertIn("жанр 4", block)

    def test_collapse_note_genre_dominant(self):
        block = admin_alerts._format_funnel(self.GENRE_MAX)
        self.assertIn("Где схлопнулось: жанр (4)", block)
        self.assertNotIn("Где схлопнулось: нет статьи", block)

    def test_funnel_line_counts_genre_in_dropped(self):
        # 1 no-article + 4 genre = 5.
        self.assertIn("отсеяно 5",
                      admin_alerts._format_funnel_line(self.GENRE_MAX))

    def test_format_funnel_renders_held_for_review(self):
        block = admin_alerts._format_funnel(self.HELD_ONLY)
        self.assertIn("на утверждение: 1", block)

    def test_held_rows_are_not_counted_as_dropped(self):
        """A hold is not a drop — it is a deferred decision. Folding it
        into the «отсеяно» sum would tell the operator the article is
        gone when it is actually waiting for them."""
        self.assertIn("отсеяно 0",
                      admin_alerts._format_funnel_line(self.HELD_ONLY))

    def test_held_only_tick_reports_no_collapse(self):
        """staged > 0 → nothing collapsed, even though nothing publishable
        came out of the tick."""
        self.assertNotIn("Где схлопнулось",
                         admin_alerts._format_funnel(self.HELD_ONLY))

    def test_legacy_funnel_without_content_gate_keys_still_renders(self):
        """Back-compat: a funnel dict from before this feature (no
        `dropped_genre` / `held_for_review`) must render zeros, not raise."""
        block = admin_alerts._format_funnel(self.DEDUP_COLLAPSE)
        self.assertIn("жанр 0", block)
        self.assertIn("Воронка", block)

    def test_funnel_renders_subject_suppression_without_counting_a_drop(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        base = dict(self.BUSY)
        base.update({
            'dropped_no_article': 0,
            'dropped_dedup_block': 0,
            'sources_failed': 0,
        })

        for value, expected in ((3, True), (0, False), ("NaN", False)):
            with self.subTest(value=value):
                funnel = dict(base, dedup_subject_suppressed=value)
                e008 = admin_alerts.alert_plan_of_day(
                    2, 2, slots, 0, funnel=funnel,
                )
                e009 = admin_alerts.alert_quiet_day(funnel=funnel)
                marker = "статей с подавленными тематическими сравнениями"
                if expected:
                    self.assertIn(f"{marker}: 3", e008)
                    self.assertIn(f"{marker}: 3", e009)
                else:
                    self.assertNotIn(marker, e008)
                    self.assertNotIn(marker, e009)

                # Suppression is informational. It must not inflate the
                # dropped total in either daily format.
                self.assertIn("отсеяно 0", e008)
                self.assertIn("дубль-блок 0", e009)
                self.assertNotIn("Где схлопнулось: дубль-блок", e009)

    # ------------------------------------------------------------------
    # «На утверждении: N» — the held backlog line on the daily ping
    # ------------------------------------------------------------------
    def test_e008_plan_of_day_shows_the_held_backlog(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        msg = admin_alerts.alert_plan_of_day(2, 2, slots, 0, held_count=3)
        self.assertIn("На утверждении: 3", msg)

    def test_e008_plan_of_day_hides_the_line_when_nothing_is_held(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        self.assertNotIn(
            "На утверждении",
            admin_alerts.alert_plan_of_day(2, 2, slots, 0, held_count=0))

    def test_e009_quiet_day_shows_the_held_backlog(self):
        """The important case: the publishable queue is empty and the ONLY
        thing in the DB is a held article. Nothing else would ever surface
        it — «нет ответа = не публикуем» has no timer to remind anyone."""
        msg = admin_alerts.alert_quiet_day(held_count=2)
        self.assertIn("[E009]", msg)
        self.assertIn("На утверждении: 2", msg)

    def test_held_count_is_keyword_only_and_optional(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        # Legacy positional call unchanged.
        self.assertNotIn(
            "На утверждении", admin_alerts.alert_plan_of_day(2, 2, slots, 0))
        self.assertNotIn("На утверждении", admin_alerts.alert_quiet_day())
        with self.assertRaises(TypeError):
            admin_alerts.alert_plan_of_day(2, 2, slots, 0, None, 3)

    def test_broken_held_count_does_not_raise(self):
        slots = [datetime(2026, 5, 10, 10, 0, tzinfo=MSK)]
        for bad in ("boom", None, object(), -1):
            with self.subTest(bad=bad):
                msg = admin_alerts.alert_plan_of_day(
                    1, 1, slots, 0, held_count=bad)
                self.assertIn("[E008]", msg)
                self.assertIn("[E009]",
                              admin_alerts.alert_quiet_day(held_count=bad))

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

    def test_collapse_note_promo_dominant(self):
        # new > 0, nothing staged, promo-filter is the max drop → name it,
        # and the breakdown bullet renders the promo counter.
        block = admin_alerts._format_funnel(self.PROMO_MAX)
        self.assertIn("реклама 4", block)
        self.assertIn("Где схлопнулось: реклама (4)", block)
        self.assertNotIn("Где схлопнулось: нет статьи", block)

    def test_funnel_line_counts_promo_in_dropped(self):
        # Compact busy-day line folds promo drops into the «отсеяно» sum
        # (1 no-article + 4 promo = 5).
        line = admin_alerts._format_funnel_line(self.PROMO_MAX)
        self.assertIn("отсеяно 5", line)

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


class TestPromoBlockedAlert(unittest.TestCase):
    """[E035] — intake promo-filter drop (реклама отсечена до перевода)."""

    def test_e035_promo_blocked(self):
        msg = admin_alerts.alert_promo_blocked(
            "https://t-hunted.blogspot.com/2026/07/"
            "hot-wheels-antigos-e-raros-na-loja.html",
            "Hot Wheels antigos e raros na loja Universo Hot Wheels",
            ["nossa loja", "não perca", "url:loja"],
        )
        self.assertIn("[E035]", msg)
        self.assertIn("🛒", msg)
        # Substring-якорь интеграционных тестов — не менять.
        self.assertIn("Отсечена реклама", msg)
        self.assertIn(
            "https://t-hunted.blogspot.com/2026/07/"
            "hot-wheels-antigos-e-raros-na-loja.html",
            msg,
        )
        self.assertIn(
            "Hot Wheels antigos e raros na loja Universo Hot Wheels", msg,
        )
        # Every matched marker is listed so the operator sees WHY.
        self.assertIn("nossa loja", msg)
        self.assertIn("não perca", msg)
        self.assertIn("url:loja", msg)
        # Operator-guidance blocks follow the file's builder conventions.
        self.assertIn("Что произошло", msg)
        self.assertIn("Что сделать", msg)
        self.assertIn("ложное срабатывание", msg)
        # Plain text only.
        for token in ("**", "```", "__", "]("):
            self.assertNotIn(token, msg)

    def test_e035_empty_markers_render_safely(self):
        # Defensive: an empty marker list must not leak 'None' / raise.
        msg = admin_alerts.alert_promo_blocked("http://u", "T", [])
        self.assertIn("[E035]", msg)
        self.assertIn("Отсечена реклама", msg)
        self.assertNotIn("None", msg)

    def test_e035_long_title_truncated(self):
        # Audit SEC-PROMO-4: the untrusted title is capped so a
        # pathological title can't push the ping past Telegram's
        # 4096-char message limit.
        msg = admin_alerts.alert_promo_blocked(
            "http://u", "T" * 5000, ["nossa loja"],
        )
        self.assertIn("[E035]", msg)
        self.assertIn("Отсечена реклама", msg)
        self.assertLess(len(msg), 4096)
        self.assertIn("…", msg)
        self.assertNotIn("T" * 500, msg)

    def test_e035_non_string_title_does_not_raise(self):
        # Belt-and-braces: the builder is called from a best-effort send
        # site; a non-str title must render, not explode.
        msg = admin_alerts.alert_promo_blocked("http://u", None, ["cupom"])
        self.assertIn("[E035]", msg)
        self.assertIn("cupom", msg)


class TestHeldForReviewAlert(unittest.TestCase):
    """[E036] — content-gate HOLD: a poster/catalog/packaging post is
    staged but parked, and goes out only if the operator approves it."""

    LINK = ("https://t-hunted.blogspot.com/2026/07/"
            "as-fotos-do-ultimo-poster-da-hot-wheels.html")
    TITLE = "As fotos do último poster da Hot Wheels 2026"

    def test_e036_core_fields(self):
        msg = admin_alerts.alert_held_for_review(
            self.LINK, self.TITLE, ["poster", "url:poster"],
            buttons_enabled=True,
        )
        self.assertIn("[E036]", msg)
        self.assertIn("🖼", msg)
        # Substring anchor for the integration tests — do not change.
        self.assertIn("На утверждение", msg)
        self.assertIn(self.LINK, msg)
        self.assertIn(self.TITLE, msg)
        # Every matched marker is listed so the operator sees WHY.
        self.assertIn("poster", msg)
        self.assertIn("url:poster", msg)
        self.assertIn("Что произошло", msg)
        self.assertIn("Что сделать", msg)
        for token in ("**", "```", "__", "]("):
            self.assertNotIn(token, msg)

    def test_e036_states_that_silence_means_no_publish(self):
        """The operator's rule, spelled out in the ping: no answer = the
        article never goes out. Without this line the honest reading of a
        two-button prompt is «it'll go out if I do nothing»."""
        msg = admin_alerts.alert_held_for_review(
            self.LINK, self.TITLE, ["poster"], buttons_enabled=True)
        self.assertIn("не отвечать", msg)
        self.assertIn("НИКОГДА не опубликуется", msg)

    def test_e036_both_branches_promise_no_silent_publish(self):
        """Whether or not buttons render, the ping must never leave the
        operator thinking the article will go out on its own."""
        for enabled in (True, False):
            with self.subTest(buttons_enabled=enabled):
                msg = admin_alerts.alert_held_for_review(
                    self.LINK, self.TITLE, ["poster"], buttons_enabled=enabled)
                self.assertIn("НИКОГДА не опубликуется", msg)

    def test_e036_buttons_enabled_advice_points_at_the_buttons(self):
        msg = admin_alerts.alert_held_for_review(
            self.LINK, self.TITLE, ["poster"], buttons_enabled=True)
        self.assertIn("нажми", msg)

    def test_e036_advice_quotes_the_real_button_labels(self):
        """Label parity (same guard as [E014]): the advice hardcodes the
        captions, so renaming a button without the text would re-open the
        advice-vs-reality drift."""
        labels = [
            b.text
            for row in admin_alerts.build_hold_review_keyboard("tok").inline_keyboard
            for b in row
        ]
        msg = admin_alerts.alert_held_for_review(
            self.LINK, self.TITLE, ["poster"], buttons_enabled=True)
        for label in labels:
            self.assertIn(
                label, msg,
                f"button caption {label!r} is not quoted in the [E036] "
                f"advice — rename the caption and the advice together",
            )

    def test_e036_buttons_disabled_explains_the_article_is_stuck(self):
        """Gate closed (default): there are no buttons under the message,
        so the advice must NOT tell the operator to press one — and must
        say plainly that this instance cannot release the article."""
        msg = admin_alerts.alert_held_for_review(
            self.LINK, self.TITLE, ["poster"], buttons_enabled=False)
        self.assertIn("[E036]", msg)
        self.assertIn("На утверждение", msg)
        self.assertNotIn("нажми", msg)
        self.assertIn("нечем", msg)

    def test_e036_buttons_disabled_is_the_default(self):
        """Fail-safe default: a caller that forgets the kwarg must get the
        no-buttons text, never a promise of buttons that do not exist."""
        self.assertEqual(
            admin_alerts.alert_held_for_review(self.LINK, self.TITLE, ["poster"]),
            admin_alerts.alert_held_for_review(
                self.LINK, self.TITLE, ["poster"], buttons_enabled=False),
        )

    def test_e036_buttons_enabled_is_keyword_only(self):
        with self.assertRaises(TypeError):
            admin_alerts.alert_held_for_review(
                self.LINK, self.TITLE, ["poster"], True)

    # -- reason categories (operator split, 2026-07-25) --------------------

    def test_e036_poster_reason_is_the_default(self):
        """Back-compat: the original single-reason call renders the
        poster/catalog wording verbatim."""
        self.assertEqual(
            admin_alerts.alert_held_for_review(
                self.LINK, self.TITLE, ["poster"]),
            admin_alerts.alert_held_for_review(
                self.LINK, self.TITLE, ["poster"], reason="poster"),
        )
        msg = admin_alerts.alert_held_for_review(
            self.LINK, self.TITLE, ["poster"])
        self.assertIn("постер / каталог / упаковку", msg)

    def test_e036_video_reason_explains_the_ambiguity(self):
        """A suspected video review needs a different judgement call than
        a poster dump — the operator must see WHICH question is being
        asked, plus why the obvious ones never reach them."""
        msg = admin_alerts.alert_held_for_review(
            self.LINK, "Unboxing da caixa J de 2026", ["unboxing …", "unboxing"],
            reason="video", buttons_enabled=True,
        )
        self.assertIn("[E036]", msg)
        self.assertIn("На утверждение", msg)
        self.assertIn("видео-обзор", msg)
        self.assertNotIn("постер / каталог / упаковку", msg)
        # Markers still say WHY it matched.
        self.assertIn("unboxing", msg)
        # And the standing promise holds in this reason too.
        self.assertIn("НИКОГДА не опубликуется", msg)

    def test_e036_reason_is_keyword_only(self):
        with self.assertRaises(TypeError):
            admin_alerts.alert_held_for_review(
                self.LINK, self.TITLE, ["poster"], "video")

    def test_e036_unknown_reason_falls_back_to_poster_wording(self):
        """Never render a blank «Что произошло» — an unset/legacy caller
        gets the original text rather than an empty section."""
        msg = admin_alerts.alert_held_for_review(
            self.LINK, self.TITLE, ["poster"], reason="banana")
        self.assertIn("Что произошло", msg)
        self.assertIn("постер / каталог / упаковку", msg)
        self.assertNotIn("None", msg)

    def test_e036_every_reason_keeps_both_advice_branches_honest(self):
        """The «Что сделать» contract is independent of the reason: with
        buttons it names them, without buttons it says so plainly."""
        for reason in admin_alerts._HOLD_REASON_BLOCKS:
            with self.subTest(reason=reason, buttons=True):
                msg = admin_alerts.alert_held_for_review(
                    self.LINK, self.TITLE, ["m"], reason=reason,
                    buttons_enabled=True)
                self.assertIn("нажми", msg)
                self.assertIn("✅ Опубликовать", msg)
                self.assertIn("НИКОГДА не опубликуется", msg)
            with self.subTest(reason=reason, buttons=False):
                msg = admin_alerts.alert_held_for_review(
                    self.LINK, self.TITLE, ["m"], reason=reason,
                    buttons_enabled=False)
                self.assertNotIn("нажми", msg)
                self.assertIn("нечем", msg)
                self.assertIn("НИКОГДА не опубликуется", msg)

    def test_e036_reason_blocks_are_non_empty_and_distinct(self):
        blocks = admin_alerts._HOLD_REASON_BLOCKS
        self.assertIn(admin_alerts._HOLD_REASON_DEFAULT, blocks)
        self.assertEqual(len(set(blocks.values())), len(blocks))
        for reason, text in blocks.items():
            with self.subTest(reason=reason):
                self.assertTrue(text.strip())

    def test_e036_empty_markers_render_safely(self):
        msg = admin_alerts.alert_held_for_review("http://u", "T", [])
        self.assertIn("[E036]", msg)
        self.assertNotIn("None", msg)

    def test_e036_long_title_truncated(self):
        msg = admin_alerts.alert_held_for_review(
            "http://u", "T" * 5000, ["poster"])
        self.assertLess(len(msg), 4096)
        self.assertIn("…", msg)
        self.assertNotIn("T" * 500, msg)

    def test_e036_non_string_title_does_not_raise(self):
        msg = admin_alerts.alert_held_for_review("http://u", None, ["poster"])
        self.assertIn("[E036]", msg)
        self.assertIn("poster", msg)


class TestGenreBlockedAlert(unittest.TestCase):
    """[E037] — content-gate DROP: a video review or an event
    announcement is rejected at intake, like a promo post."""

    def test_e037_video_genre(self):
        msg = admin_alerts.alert_genre_blocked(
            "https://example.com/2026/07/video.html",
            "Vídeo: Hot Wheels 2026 linha básica",
            "video", ["vídeo"],
        )
        self.assertIn("[E037]", msg)
        self.assertIn("🚫", msg)
        # Substring anchor for the integration tests — do not change.
        self.assertIn("Отсечён жанр", msg)
        self.assertIn("видео-обзор", msg)
        self.assertIn("https://example.com/2026/07/video.html", msg)
        self.assertIn("Vídeo: Hot Wheels 2026 linha básica", msg)
        self.assertIn("vídeo", msg)
        self.assertIn("Что произошло", msg)
        self.assertIn("Что сделать", msg)
        # Self-diagnosing: the operator is told how to report a bad rule.
        self.assertIn("поправим", msg)
        for token in ("**", "```", "__", "]("):
            self.assertNotIn(token, msg)

    def test_e037_event_genre(self):
        msg = admin_alerts.alert_genre_blocked(
            "https://example.com/2026/07/conv.html",
            "Convenção Hot Wheels 2026: datas e ingressos",
            "event", ["convenção", "ingressos"],
        )
        self.assertIn("[E037]", msg)
        self.assertIn("ивент", msg)
        self.assertIn("convenção", msg)
        self.assertIn("ingressos", msg)

    def test_e037_unknown_genre_falls_back_to_the_raw_key(self):
        """Defensive: a future genre key with no Russian label must still
        render something truthful instead of 'None'."""
        msg = admin_alerts.alert_genre_blocked(
            "http://u", "T", "podcast", ["x"])
        self.assertIn("[E037]", msg)
        self.assertIn("podcast", msg)
        self.assertNotIn("None", msg)

    def test_e037_empty_markers_render_safely(self):
        msg = admin_alerts.alert_genre_blocked("http://u", "T", "video", [])
        self.assertIn("[E037]", msg)
        self.assertNotIn("None", msg)

    def test_e037_long_title_truncated(self):
        msg = admin_alerts.alert_genre_blocked(
            "http://u", "T" * 5000, "video", ["vídeo"])
        self.assertLess(len(msg), 4096)
        self.assertIn("…", msg)
        self.assertNotIn("T" * 500, msg)

    def test_e037_non_string_title_does_not_raise(self):
        msg = admin_alerts.alert_genre_blocked("http://u", None, "event", ["expo"])
        self.assertIn("[E037]", msg)
        self.assertIn("expo", msg)


class TestHoldReviewKeyboard(unittest.TestCase):
    """build_hold_review_keyboard — the [E036] keyboard. Callback grammar
    ``hd:a:<token>`` (approve) / ``hd:r:<token>`` (reject), approve FIRST.
    Coexists with the [E014] ``dd:`` grammar."""

    @staticmethod
    def _flat_buttons(kb):
        return [b for row in kb.inline_keyboard for b in row]

    def test_returns_inline_keyboard_markup(self):
        self.assertIsInstance(
            admin_alerts.build_hold_review_keyboard("tok"), InlineKeyboardMarkup)

    def test_two_buttons_callback_data(self):
        buttons = self._flat_buttons(
            admin_alerts.build_hold_review_keyboard("tok"))
        self.assertEqual(len(buttons), 2)
        self.assertEqual(
            [b.callback_data for b in buttons],
            ["hd:a:tok", "hd:r:tok"],  # approve first, reject second
        )

    def test_button_labels(self):
        texts = [b.text for b in self._flat_buttons(
            admin_alerts.build_hold_review_keyboard("tok"))]
        self.assertIn("✅ Опубликовать", texts[0])
        self.assertIn("🚫 Не публиковать", texts[1])

    def test_callback_data_under_64_bytes(self):
        token = secrets.token_urlsafe(9)
        for b in self._flat_buttons(
                admin_alerts.build_hold_review_keyboard(token)):
            self.assertLessEqual(len(b.callback_data.encode("utf-8")), 64)

    def test_prefix_differs_from_the_dedup_keyboard(self):
        """The two keyboards must never produce the same callback_data —
        a shared prefix would route an approve press into the dedup
        resolver (which would answer «устарела» and lose the decision)."""
        hold = {b.callback_data for b in self._flat_buttons(
            admin_alerts.build_hold_review_keyboard("tok"))}
        dedup = {b.callback_data for b in self._flat_buttons(
            admin_alerts.build_dedup_review_keyboard("tok"))}
        self.assertEqual(hold & dedup, set())


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


class TestHoldCapAlert(unittest.TestCase):
    """[E038] — one article held ``HOLD_CAP`` times in a row and stepped aside.

    This ping exists because the situation is otherwise invisible: a held row
    writes no ``last_error``, never enters the [E034] recap, and the outage
    pings stop after ``ping_count >= 3``.
    """

    def _msg(self, **kw):
        args = dict(link="http://x/stuck", title="Hot Wheels Unboxing",
                    hold_count=6,
                    reason="APIStatusError: Error code: 402 - Insufficient credits",
                    defer_hours=24)
        args.update(kw)
        return admin_alerts.alert_hold_cap_reached(**args)

    def test_carries_code_link_count_and_cause(self):
        # Sentinels, not realistic values. A cause that cannot appear in the
        # static text (the builder's own advice mentions «402 / Insufficient
        # credits», so matching that would pass for a builder that ignores its
        # ``reason``), and two counts that are neither equal nor substrings of
        # each other — with 6 and 24 a builder that SWAPPED the two
        # placeholders still satisfied every assertion.
        msg = self._msg(hold_count=71, reason="ZZTOPMARKER", defer_hours=93)
        self.assertIn("[E038]", msg)
        self.assertIn("http://x/stuck", msg)
        self.assertIn("Hot Wheels Unboxing", msg)
        self.assertIn("ZZTOPMARKER", msg)
        self.assertIn("Придержана подряд: 71", msg)
        self.assertIn("93", msg)
        self.assertNotIn("отложена на 71", msg)

    def test_renders_every_argument(self):
        """Guard against a builder that quietly drops one: each field must
        appear, and an empty title must not silently swallow the rest."""
        msg = self._msg(link="LNKSENTINEL", title="TTLSENTINEL",
                        hold_count=71, reason="RSNSENTINEL", defer_hours=93)
        for sentinel in ("LNKSENTINEL", "TTLSENTINEL", "RSNSENTINEL", "71", "93"):
            self.assertIn(sentinel, msg, sentinel)

    def test_says_the_article_is_not_lost(self):
        """The operator must not read this as «статья потеряна» — the whole
        difference from a strike is that nothing is thrown away."""
        msg = self._msg()
        self.assertIn("НЕ потеряна", msg)
        self.assertIn("Что сделать", msg)

    def test_caps_a_pathological_title_and_cause(self):
        """Title and cause are untrusted upstream text; the ping must stay
        inside Telegram's 4096-char limit (same argument as SEC-PROMO-4)."""
        msg = self._msg(title="Т" * 5000, reason="Ы" * 5000)
        self.assertLess(len(msg), 4096)

    def test_is_pure(self):
        self.assertEqual(self._msg(), self._msg())
