# Decisions Log: mattel-parser-rewrite

Agent reports on completed tasks. Each entry is written by the agent that executed the task.

---

## Task 01: rewrite-parser

**Status:** Done
**Commit:** 8b42b67
**Agent:** mattel-parser-impl
**Summary:** Replaced `<script id="__NEXT_DATA__">` extraction in `mattel_news_source.py` with RSC flight-payload parsing (`self.__next_f.push([1, "..."])` → JSON-unescape → anchor `"article2":{"entries":[` → bracket-match → entries). Article bodies resolved via separate text-row marker `<row-id>:T<hex-len>,<content>` reconstructed across multi-chunk boundaries. Public surface preserved 1:1; all 8 Decision-8 security controls applied (SSRF guard, linear-time string-aware regex, JSON depth catch, hex-length cap, depth+string-aware bracket-match, sanitised notifier, allow_redirects=False). Old fixture deleted; new `tests/fixtures/mattel_flight_builder.py` synthesises HTML matching live format.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved, 3 minor → [logs/working/task-01/code-reviewer-round1.json](logs/working/task-01/code-reviewer-round1.json)
- security-auditor: approved, 2 minor (all 9 Decision-8 ACs pass) → [logs/working/task-01/security-auditor-round1.json](logs/working/task-01/security-auditor-round1.json)
- test-reviewer: approved, 3 minor (44 tests pass; full AC1-AC13 + ES1-ES10 coverage) → [logs/working/task-01/test-reviewer-round1.json](logs/working/task-01/test-reviewer-round1.json)

**Verification:**
- `pytest tests/test_mattel_news_source.py tests/test_mattel_integration.py -q` → 44 passed
- `pytest tests/ -q` → 417 passed, 1 unrelated pre-existing fail in `tests/test_hw_review_retry.py::TestListFooter::test_list_footer_format_exact` (not introduced by task 01)
- `python3 -c "from mattel_news_source import fetch_mattel_news, fetch_mattel_article, MattelNewsError, NEWS_URL, ARTICLE_URL_PREFIX, MAX_RESPONSE_SIZE; print('ok')"` → `ok`
- `python3 -c "from mattel_news_source import fetch_mattel_news; print(fetch_mattel_news())"` → `[]`, STDERR clean

---

<!-- Entries are added by agents as tasks are completed.

Format is strict — use only these sections, do not add others.
Do not include: file lists, findings tables, JSON reports, step-by-step logs.
Review details — in JSON files via links. QA report — in logs/working/.

## Task N: [title]

**Status:** Done
**Commit:** abc1234
**Agent:** [teammate name or "main agent"]
**Summary:** 1-3 sentences: what was done, key decisions. Not a file list.
**Deviations:** None / Deviated from spec: [reason], did [what].

**Reviews:**

*Round 1:*
- code-reviewer: 2 findings → [logs/working/task-N/code-reviewer-1.json]
- security-auditor: OK → [logs/working/task-N/security-auditor-1.json]

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-N/code-reviewer-2.json]

**Verification:**
- `npm test` → 42 passed
- Manual check → OK

-->
