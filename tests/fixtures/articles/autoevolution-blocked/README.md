# These are NOT article fixtures

Four Cloudflare challenge pages (~6 KB each), captured 2026-08-05 while
collecting the corpus. They are what autoevolution answers on article URLs —
a challenge page, not a 200 with content. Nothing here can be parsed, and no
test should treat them as input.

They are kept because they are the EVIDENCE behind a decision that shapes the
whole feature: the corpus cannot be re-fetched. autoevolution answers like
this, and lamley hands out a 24-hour blacklist for pacing violations. That is
why the 14 real pages in the sibling directories are committed as bytes
instead of being downloaded on demand — see `tests/test_orangetrack_golden.py`
and `.pre-commit-config.yaml`, where two hooks are explicitly told not to
rewrite captured HTML.

If autoevolution ever becomes fetchable again, delete this directory — but
delete it deliberately, and say so in the feature's `decisions.md`.
