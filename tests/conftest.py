"""Shared pytest fixtures."""

import os
import sys


# Make the repo-root modules importable from any test file regardless
# of pytest's invocation cwd. This used to live in individual
# ``sys.path.insert`` calls inside each test module — centralising it
# here is harmless for tests that already insert and saves boilerplate
# in new tests.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
