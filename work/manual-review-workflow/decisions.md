# Decisions Log: manual-review-workflow

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

## Task 4: Public `preview_nodes` wrapper in `telegraph_publisher`

**Status:** Done
**Commit:** 570dac4
**Agent:** teammate (task-4)
**Summary:** Added public `preview_nodes(title, paragraphs, images, source_url, subtitle, blocks)` in `telegraph_publisher.py` — offline mirror of the Telegraph node tree that `publish_article` uploads, with no network call and no `TELEGRAPH_ACCESS_TOKEN` required. Refactored `publish_article` to delegate node building to `preview_nodes`, making the new wrapper the single source of truth and eliminating any drift risk between preview and real publish. Private `_build_content` / `_build_content_from_blocks` left untouched per spec.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: approved, 1 minor suggestion (documented-as-designed `title` parameter is unused inside the body — expected per task spec) → [logs/working/task-4/code-reviewer-round1.json](logs/working/task-4/code-reviewer-round1.json)
- test-reviewer: passed, 0 findings, 12/12 litmus → [logs/working/task-4/test-reviewer-round1.json](logs/working/task-4/test-reviewer-round1.json)

**Verification:**
- `pytest tests/test_telegraph_publisher.py -v` → 32 passed (20 existing + 12 new `TestPreviewNodes`)
- `pytest tests/ --ignore=tests/test_pending_articles_repo.py --ignore=tests/test_admin_ping.py -q` → 173 passed (other ignored files are new in-progress work from parallel teammates)
- Smoke 1: `python3 -c "import telegraph_publisher; print(telegraph_publisher.preview_nodes(title='t', paragraphs=['p'])[0]['tag'])"` → `p`, exit 0
- Smoke 2: `python3 -c "... preview_nodes(..., paragraphs=['a','b'], images=[...], source_url=..., subtitle='sub'); assert all('tag' in n for n in nodes); print('OK, nodes:', len(nodes))"` → `OK, nodes: 6`, exit 0

## Task 2: `preview_renderer` module

**Status:** Done
**Commits:** bcf8121 (initial), 55df5b7 (round-2 fix)
**Agent:** teammate (task-2), resumed by main agent after rate-limit
**Summary:** New stdlib-only `preview_renderer.py` (pure `render_html(nodes, title) -> str`) implementing tech-spec Decision 1's three hardening layers: tag allowlist (frozenset of the 10 tags `telegraph_publisher` emits), URL-scheme allowlist (`^https?://` IGNORECASE on `img src`, `iframe src`, `a href` — drops javascript/data/file/vbscript/relative/leading-whitespace), and an exact CSP meta (`default-src 'none'; img-src https:; frame-src https:; style-src 'unsafe-inline'`) in `<head>`. All text and retained attribute values go through `html.escape(quote=True)`; Cyrillic preserved. 63 pytest cases, stdlib-only, no `requirements.txt` change. Round-2 fix added `_SAFE_ATTR_NAME_RE = ^[A-Za-z][A-Za-z0-9_-]*$` to drop malformed attribute names (e.g. `'x" onerror="alert(1)'`) — covered by 8 new assertions plus a full `html.parser.HTMLParser` round-trip structural check.
**Deviations:** None (`_ALLOWED_TAGS` is a `frozenset` rather than a plain `set`; functionally equivalent, arguably safer — acknowledged as a nit in round-1 code-review and left as-is).

**Reviews:**

*Round 1:*
- code-reviewer: OK, 2 nits (frozenset choice, `quote=True` on title — both "no action") → [logs/working/task-2/code-reviewer-round1.json](logs/working/task-2/code-reviewer-round1.json)
- security-auditor: OK (1 HIGH finding was fixed in-review in the initial commit: attribute-name injection guard `_SAFE_ATTR_NAME_RE` added) → [logs/working/task-2/security-auditor-round1.json](logs/working/task-2/security-auditor-round1.json)
- test-reviewer: OK, 1 nit (documented `</style>` carve-out in `test_other_unknown_tags_dropped`) → [logs/working/task-2/test-reviewer-round1.json](logs/working/task-2/test-reviewer-round1.json)

*Round 2 (after fixes):*
- code-reviewer: OK, 0 findings → [logs/working/task-2/code-reviewer-round2.json](logs/working/task-2/code-reviewer-round2.json)
- security-auditor: OK, 0 findings → [logs/working/task-2/security-auditor-round2.json](logs/working/task-2/security-auditor-round2.json)
- test-reviewer: OK, 0 findings → [logs/working/task-2/test-reviewer-round2.json](logs/working/task-2/test-reviewer-round2.json)

Nits deferred (all from round 1, judged not worth fixing): frozenset vs set (safer as-is), `quote=True` on `<title>` text (defensive uniformity), `</style>` carve-out in unknown-tag test (the tested invariant — payload absence — holds unconditionally; the carve-out is inline-documented).

**Verification:**
- `pytest tests/test_preview_renderer.py -v` → 63 passed (0.04s)
- `pytest tests/ -q` → 237 passed (full suite, no regression)
- Smoke 1 (`render_html([{'tag':'p','children':['test']}], 'T')[:60]` starts with `<!DOCTYPE html>`) → OK, exit 0
- Smoke 2 (URL-scheme filter drops `javascript:` from `img src`) → OK, exit 0
- Smoke 3 (CSP meta contains `default-src 'none'`, `img-src https:`, `frame-src https:`) → OK, exit 0

