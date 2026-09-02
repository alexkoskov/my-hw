"""Unit tests for ``feature_flags`` — the source-formatting kill switch.

user-spec AC11 / tech-spec Decision 6. The flag is the operator's only way
back to flat text without a code change, and it is read ONCE at import, so
its grammar and its default are load-bearing: the next chance to fix a wrong
value is a container restart, which is forbidden between 10:00 and 20:00 МСК.
"""

import ast
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feature_flags  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reload_with(monkeyenv):
    """Reload the module with ``monkeyenv`` applied to ``os.environ``."""
    return importlib.reload(feature_flags)


class _ReloadIsolation(unittest.TestCase):
    """Value-parsing tests reload the module, so the last reload's state
    would leak into every other test in a full run. Restore it here."""

    def tearDown(self):
        os.environ.pop("SOURCE_FORMATTING_ENABLED", None)
        importlib.reload(feature_flags)


class TestSourceFormattingFlag(_ReloadIsolation):
    def test_default_is_enabled_when_env_unset(self):
        """AC11 default, and the operator's 2026-07-30 decision AGAINST the
        security review's opt-in recommendation. This test exists so that a
        silent flip of the default cannot pass."""
        os.environ.pop("SOURCE_FORMATTING_ENABLED", None)
        mod = _reload_with(os.environ)
        self.assertTrue(mod.source_formatting_enabled())

    def test_blank_value_is_enabled(self):
        for raw in ("", "   "):
            with self.subTest(raw=repr(raw)):
                os.environ["SOURCE_FORMATTING_ENABLED"] = raw
                self.assertTrue(_reload_with(os.environ).source_formatting_enabled())

    def test_flag_off_words_disable(self):
        for raw in ("0", "false", "no", "off", "OFF", "False", " off ", "No"):
            with self.subTest(raw=repr(raw)):
                os.environ["SOURCE_FORMATTING_ENABLED"] = raw
                self.assertFalse(_reload_with(os.environ).source_formatting_enabled())

    def test_on_words_enable(self):
        for raw in ("1", "true", "yes", "on", "TRUE"):
            with self.subTest(raw=repr(raw)):
                os.environ["SOURCE_FORMATTING_ENABLED"] = raw
                self.assertTrue(_reload_with(os.environ).source_formatting_enabled())

    def test_unrecognized_value_stays_enabled(self):
        """Sharp edge of the project's flag grammar, pinned deliberately: a
        typo while trying to DISABLE leaves the feature enabled. Same
        semantics as ``DEDUP_SERIES_ENABLED``; the cost is higher here, which
        is why ``.env.example`` has to say it out loud."""
        for raw in ("disabled", "нет", "2", "OFFF"):
            with self.subTest(raw=repr(raw)):
                os.environ["SOURCE_FORMATTING_ENABLED"] = raw
                self.assertTrue(_reload_with(os.environ).source_formatting_enabled())

    def test_function_reads_module_attribute(self):
        """Contract the parser tests in Task 7 lean on: patching the module
        attribute takes effect WITHOUT ``importlib.reload``."""
        original = feature_flags.SOURCE_FORMATTING_ENABLED
        try:
            feature_flags.SOURCE_FORMATTING_ENABLED = False
            self.assertFalse(feature_flags.source_formatting_enabled())
            feature_flags.SOURCE_FORMATTING_ENABLED = True
            self.assertTrue(feature_flags.source_formatting_enabled())
        finally:
            feature_flags.SOURCE_FORMATTING_ENABLED = original

    def test_env_name_matches_constant_name(self):
        """const↔env drift makes a deploy silently no-op — the failure mode
        ``news_bot.py:125-127`` warns about."""
        source = open(
            os.path.join(_REPO_ROOT, "feature_flags.py"), encoding="utf-8"
        ).read()
        self.assertIn('os.getenv(\n    "SOURCE_FORMATTING_ENABLED"', source)
        self.assertIn("SOURCE_FORMATTING_ENABLED = os.getenv", source)


class TestModuleHygiene(unittest.TestCase):
    """The module is imported BY the parsers, and ``news_bot`` imports the
    parsers. Anything heavy or first-party here creates an import cycle and
    drags a 4600-line module into a parser's import tree."""

    def _module_ast(self):
        source = open(
            os.path.join(_REPO_ROOT, "feature_flags.py"), encoding="utf-8"
        ).read()
        return ast.parse(source)

    def test_module_imports_only_stdlib(self):
        imported = set()
        for node in ast.walk(self._module_ast()):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"os"})

    def test_module_has_no_side_effects(self):
        """No ``load_dotenv``, no logging setup, no file or network I/O at
        import: only the constant assignment and the function def."""
        tree = self._module_ast()
        for node in tree.body:
            self.assertIsInstance(
                node,
                (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef,
                 ast.Expr),
                f"unexpected top-level statement: {ast.dump(node)[:80]}",
            )
            if isinstance(node, ast.Expr):
                # Only the module docstring may be a bare expression.
                self.assertIsInstance(node.value, ast.Constant)


class TestEnvExampleDocumentsTheFlag(unittest.TestCase):
    def test_env_example_documents_the_same_key(self):
        """Code reading one key while the documentation promises another is
        a "dark" deploy that silently does nothing."""
        text = open(
            os.path.join(_REPO_ROOT, ".env.example"), encoding="utf-8"
        ).read()
        self.assertIn("SOURCE_FORMATTING_ENABLED", text)

    def test_env_example_documents_restart_and_anytime_deploy(self):
        """The flag applies on restart, which is allowed at any time."""
        text = open(
            os.path.join(_REPO_ROOT, ".env.example"), encoding="utf-8"
        ).read()
        block = text[text.index("SOURCE_FORMATTING_ENABLED") - 700:]
        block = block[: block.index("SOURCE_FORMATTING_ENABLED=") + 40]
        self.assertIn("restart", block.lower())
        self.assertIn("allowed at any time", block.lower())


class TestOrangetrackIsNotGatedByTheFlag(_ReloadIsolation):
    """The kill switch must NOT reach orangetrack (tech-spec Decision 6, AC10).

    orangetrack emitted `blocks` before this feature existed — it is the
    reference source the whole thing was extracted FROM. The switch gates
    block emission inside the three new parsers, never inside `dom_blocks`,
    which orangetrack also consumes and where a gate would strip its blocks
    too: an operator reaching for the switch during a t-hunted incident
    would silently take formatting off a source that was working fine.

    This control was measured to be missing: gating orangetrack's blocks on
    the flag failed ZERO tests across six files (Task 12 audit). It lives
    here rather than in `tests/test_orangetrack_source.py`, which AC10
    requires to pass unedited.
    """

    #: Same shape as the primary-path fixture in test_orangetrack_source.py.
    #: Two paragraphs and an image is enough — the claim is about whether
    #: blocks are emitted at all, not about what is in them.
    CONTENT_HTML = (
        "<p>The first paragraph introduces the casting.</p>"
        '<figure class="wp-block-image">'
        '<img src="https://orangetrackdiecast.com/wp-content/uploads/i.jpg?w=1024" />'
        "</figure>"
        "<p>The second paragraph explains its rarity.</p>"
    )

    def _fetch(self):
        import orangetrack_source
        return orangetrack_source.fetch_orangetrack_article({
            "link": "https://orangetrackdiecast.com/post-x",
            "title": "Sample Title",
            "content": [{"value": self.CONTENT_HTML}],
            "summary": "",
            "published": "Mon, 01 Jan 2025 00:00:00 +0000",
        })

    def test_orangetrack_still_emits_blocks_with_the_flag_off(self):
        os.environ["SOURCE_FORMATTING_ENABLED"] = "0"
        _reload_with(os.environ)
        self.assertFalse(
            feature_flags.source_formatting_enabled(),
            "the flag did not actually turn off — the assertion below would "
            "then be measuring the ON state twice")

        article = self._fetch()

        self.assertIsNotNone(article)
        self.assertTrue(
            article.get("blocks"),
            "turning the kill switch off stripped orangetrack's blocks — the "
            "switch reached a source it must never touch (AC10)")

    def test_orangetrack_blocks_are_identical_in_both_flag_states(self):
        """Not merely "non-empty": the switch must be a no-op here, so the
        two block lists have to match element for element."""
        os.environ["SOURCE_FORMATTING_ENABLED"] = "0"
        _reload_with(os.environ)
        off = self._fetch()

        os.environ.pop("SOURCE_FORMATTING_ENABLED", None)
        _reload_with(os.environ)
        self.assertTrue(feature_flags.source_formatting_enabled())
        on = self._fetch()

        self.assertEqual(off["blocks"], on["blocks"])
        self.assertEqual(off["paragraphs"], on["paragraphs"])
        self.assertEqual(off["images"], on["images"])


if __name__ == "__main__":
    unittest.main()
