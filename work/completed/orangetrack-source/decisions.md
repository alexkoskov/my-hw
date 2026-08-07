# Decisions Log: orangetrack-source

Agent reports on completed tasks. Each entry is written by the agent that executed the task.

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

## Task 12: Audit fixes (code M1+M2, test M1+M2+M3)

**Status:** Done
**Commit:** (see git log; this entry covers the post-audit fix wave on `dev`)
**Agent:** main agent
**Summary:** Applied 5 fixes from `logs/audit/code-audit.md` and `logs/audit/test-audit.md` ahead of prod merge. Code M1: `OrangetrackPingAggregator.format_summary()` header now reflects total `add()` event volume (new `_total_added` counter incremented up-front, before the global 500-cap and per-code 50-cap), and per-bucket count includes the per-code truncated overflow — operator severity signal is no longer muted by either cap. Code M2: removed dead `ExpatError` / `SAXParseException` imports from `_fetch_orangetrack_entries` (the structurally-broken nested `from xml.sax.SAXParseException import SAXParseException` chain), updated the bozo-classification comment; `FEED_XML_PARSE` still emits for any non-network bozo. Tests: added `TestDispatcherIntegration` class covering apex / www pass-through, subdomain-attack defense-in-depth, and missing-paragraphs safety check (4 tests, zero HTTP); strengthened `test_total_event_cap_500_silent` with hard `_total_calls == 500`, distinct-stored == 500, and header "600 issues" assertions (litmus: removing the 500-cap guard now fails the test); added `test_multiple_iframes_in_one_paragraph` to `TestPrimaryPath` (regression guard against the parser emitting only the first iframe). Also updated `test_dedup_same_code_same_link` header assertion to match new "events fired" semantic (was distinct-pairs).
**Deviations:** Header semantic shifted from "distinct (code, link) pairs" (per tech-spec line 196) to "total add() events fired". Tech-spec was silent on the per-code-cap overflow case; the natural reading and operational value both argue for true volume, per code-audit M1.

**Reviews:** N/A — fixes against prior audits; no new review round triggered.

**Verification:**
- `pytest tests/ -x -q` → 816 passed (was 810 baseline; +6 new tests: 4 dispatcher + 1 header-truncated + 1 multi-iframe).
- Targeted: `pytest tests/test_orangetrack_source.py -v -k "TestDispatcherIntegration or test_total_event_cap_500 or test_multiple_iframes or test_format_summary or test_header_count or test_dedup or test_per_code_link_cap"` → 12 passed.
- Manual: `format_summary` spot-check via 60-event same-code feed shows header "60 issues this tick" + bullet "(60×)" + "10 more truncated" tail (matches new spec).

---

## Закрытие — 2026-08-04

Фича выкачена и работает; папка перенесена в `work/completed/`.

Четвёртый источник, orangetrackdiecast.com. Полностью описан в PK: architecture.md (реестр источников, блочный рендер), patterns.md (контракт парсера). Пост-деплой проверка на живом проде — 2026-08-03.
