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

## Task 02: code-audit

**Status:** Done
**Commit:** (audit-only — no code changes)
**Agent:** mattel-code-auditor
**Summary:** Independent audit of Task 01 output (`mattel_news_source.py` + `tests/fixtures/mattel_flight_builder.py` + both test files) confirms all 7 architectural Decisions (1–7) are honored: RSC flight payload parsing replaces `__NEXT_DATA__`, anchors are semantic (`"article2":{"entries":[`, field names — no hardcoded row-IDs), preserved helpers (`_is_hotwheels`, `_build_entry`, `_extract_entries`, `_notify`) match code-research §11.3 signatures, the shared builder module exposes `_make_flight_listing` / `_make_flight_article` with placeholder defaults, atomic single-wave delivery (no half-deleted state), full ES1–ES10 fail-soft notifier matrix verified path-by-path (incl. ES9c body-empty no-notifier path), and anti-drift smoke tests are `pytest.skip`-guarded on `/tmp/mattel_*.html`. Module-level `_FLIGHT_PUSH_RE`, no `requests.Session()` constructor, no module-level mutable shared state. Status: **approved**.
**Deviations:** None.

**Reviews:** N/A (audit task; no reviewers per task frontmatter).

**Verification:**
- Audit report → [logs/tasks/code-audit-report.json](logs/tasks/code-audit-report.json)
- Audit report (working copy) → [logs/working/audit/code-audit-report.json](logs/working/audit/code-audit-report.json)
- Static read of all 4 audit-target files (1538 lines total) against tech-spec Decisions 1–7, code-research §11.3–§11.5, and the 13-item Task 02 acceptance checklist → all pass with file:line evidence in the JSON checklist.
- Forbidden-pattern grep: `__NEXT_DATA__` / `_NEXT_DATA_RE` / `<script id=` / `requests.Session(` / `mattel_news.html` (deleted fixture) → only intentional historical mentions in the migration-note docstring (mattel_news_source.py:11) and `/tmp/mattel_news.html` snapshot path for Decision 7.
- 3 minor non-blocking findings: (1) migration-note docstring still names `__NEXT_DATA__` (intentional, low-stakes); (2) `_resolve_body_html` compiles a row-id-dependent regex per call (could be hoisted to module level via generic pattern); (3) `_extract_entries` is a thin wrapper kept for Decision 3 import stability (clarity note only).

---

## Task 03: security-audit

**Status:** Done
**Commit:** (audit-only — no code changes)
**Agent:** mattel-security-auditor
**Summary:** Audit-only task — read `mattel_news_source.py`, `tests/fixtures/mattel_flight_builder.py`, `tests/test_mattel_news_source.py` (TestSsrfGuard), and `news_bot.py:184/227` log-suppression. All 9 Decision-8 security ACs pass; all 4 round-1 critical+major findings (SSRF, ReDoS, JSON depth, bracket-match) honored at file:line level; round-1 minor #8 (placeholder-defaults) materially mitigated in code despite round-2 spec leaving closure rejected (the as-built builder has zero default URLs). OWASP A03/A05/A09/A10 sweep clean. Status: approved with 2 informational findings (link-echo in ES6/ES7/ES9/ES9b — tech-spec-accepted; per-call regex compile in `_resolve_body_html` — perf-polish, no security impact). No critical or major findings; no fix-task needed.
**Deviations:** None.

**Reviews:** N/A (audit task; no reviewers per task frontmatter).

**Verification:**
- Audit report → [logs/tasks/security-audit-report.json](logs/tasks/security-audit-report.json)
- Audit report (working copy) → [logs/working/audit/security-audit-report.json](logs/working/audit/security-audit-report.json)
- 11/11 checklist items pass; 9/9 AC compliance pass; 4/4 OWASP sweep pass; 7/8 round-1 entries `honored`, 1/8 `closure_rejected` (placeholder-defaults — material code mitigation noted)

---

## Task 04: test-audit

**Status:** Done
**Commit:** (audit-only — no code changes)
**Agent:** mattel-test-auditor
**Summary:** Approved. 41 unit tests in `test_mattel_news_source.py` (target ≥32; round-2 additions for AC3/AC6/AC7/SSRF/anti-drift legitimately exceed pre-round-2 baseline) + 3 integration tests in `test_mattel_integration.py` (all using new `mattel_flight_builder`, no file fixture). Full AC1–AC13 coverage (AC11 transitive via top-of-file imports; AC13 via skip-guarded anti-drift smoke per user-spec delegation) and full ES1–ES10 + ES9b + ES9c coverage. AC3 silent-zero, AC6 dict-form, AC7 url-fallback, AC8 multi-chunk (`body_chunks=3`), AC9 empty + truncated, AC10 thumbnail-only regression, ES10 SSRF guard with no-URL-echo all explicitly verified with meaningful assertions (notifier call counts + structural value asserts). Anti-drift smokes properly skip-guarded (`pytest.skip` at lines 600/616). No `__NEXT_DATA__` literals; the single `mattel_news.html` reference is the `/tmp` live snapshot path, not the deleted fixture. TestBuildEntry = 5 tests; TestIsHotwheels = 5 tests. Zero findings, no fix-task needed.
**Deviations:** None.

**Reviews:** N/A (audit task; no reviewers per task frontmatter).

**Verification:**
- Audit report → [logs/tasks/test-audit-report.json](logs/tasks/test-audit-report.json)
- Audit report (working copy) → [logs/working/audit/test-audit-report.json](logs/working/audit/test-audit-report.json)
- `grep -c "def test_" tests/test_mattel_news_source.py` → 41
- `grep -c "def test_" tests/test_mattel_integration.py` → 3
- `grep -nF "__NEXT_DATA__" tests/test_mattel_news_source.py tests/test_mattel_integration.py tests/fixtures/mattel_flight_builder.py` → 0 hits
- 13/13 AC covered, 12/12 ES covered, 0 findings, 0 weak-assertion tests

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
