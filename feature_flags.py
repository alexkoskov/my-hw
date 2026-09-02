"""Kill switch for the source-formatting parity feature (user-spec AC11).

WHY THE FLAG EXISTS. The flag hands the operator a fast way back to flat text
with one environment variable if broken markup reaches production. Applying a
change requires a container restart, which is allowed at any time and recreates
the single in-process daily schedule (slots 10:00 / 15:00 / 19:30).

WHAT "OFF" MEANS. Flat text: paragraphs travel and publish without inline
emphasis. It is NOT a byte-for-byte return to today's output — the measured
drift is −0.10 % on t-hunted and −1.18 % on lamley, and the latter is
WordPress litter being cleaned up, i.e. an improvement.

DEFAULT IS ON. Operator's decision of 2026-07-30, taken deliberately
AGAINST the security review, which argued for opt-in (default OFF). The
reviewer's case is preserved in tech-spec § Risks: a pre-deploy E2E is
impossible and Decision 3b swallows failures silently. The trade-off was
accepted with that argument on the table. Do not flip this default without a
new decision.

READ ONCE, AT IMPORT. Changing the value requires restarting the process.

NO SIDE EFFECTS, STDLIB ONLY. The parsers import this module and
``news_bot`` imports the parsers, so a first-party import here would create
a cycle and pull a 4600-line module into a parser's import tree. There is
deliberately no ``load_dotenv`` call either: ``news_bot.py:33`` loads the
``.env`` BEFORE it imports the parsers (``news_bot.py:53-58``), and the
parsers are what import this module, so by the time the constant below is
read the local ``.env`` is already in the environment. That ORDER is
load-bearing — move ``load_dotenv()`` below the parser imports and this
flag silently stops honouring a local ``.env``.
"""

import os

#: Master switch for source-formatting parity. CRITICAL: the environment
#: variable name is byte-for-byte the constant name — a const↔env drift is
#: how a deploy goes "dark" and silently no-ops (see the warning at
#: ``news_bot.py:125-127``).
#:
#: Grammar matches the project's other flags: unset OR blank → enabled;
#: only the explicit off-words ``0/false/no/off`` (case-insensitive,
#: surrounding whitespace stripped) disable it. Anything else — including a
#: typo like ``disabled`` — leaves the feature ON. That edge is accepted
#: project convention, not an oversight, and it is spelled out in
#: ``.env.example`` because here the next chance to correct a typo is the
#: end of the publication window.
SOURCE_FORMATTING_ENABLED = os.getenv(
    "SOURCE_FORMATTING_ENABLED", "1"
).strip().lower() not in ("0", "false", "no", "off")


def source_formatting_enabled() -> bool:
    """Return whether source-formatting parity is enabled.

    Reads the MODULE attribute through a bare name, which Python resolves to
    the module global at CALL time — so tests can ``monkeypatch.setattr`` the
    constant without ``importlib.reload``, and an operator can flip the flag
    via env + restart without touching code. Same house style as
    ``news_bot.DEDUP_SERIES_ENABLED`` (``news_bot.py:131-133``).
    """
    return SOURCE_FORMATTING_ENABLED
