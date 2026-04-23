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

## Task 3: admin-ping + source vocabulary + sanitize_error_message

**Status:** Done
**Commit:** c3858cc
**Agent:** teammate (task-3), resumed by main agent after rate-limit
**Summary:** Added three foundational helpers to `news_bot.py` consumed by later waves (Decisions 4/11/12): `SOURCE_EMOJI` / `SOURCE_LABEL` dicts keyed by `autoevolution`/`mattel`/`lamley` (no `rss`, no `other`), `build_admin_ping(rows)` emitting byte-exact `"N ждут review: 🟠 autoevolution ×K, 🟣 mattel ×M, 🟢 lamley ×L"` with stable literal-tuple ordering and `None` on empty queue, and `sanitize_error_message(exc)` that redacts the four env-secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_ID`, `TELEGRAPH_ACCESS_TOKEN`) with an explicit `if value and value.strip()` guard against the `str.replace('', ...)` character-interleaving pathology, plus an outer try/except so sanitizer bugs can never break the caller's error path. Also introduced env-overridable knobs `IDLE_TIMEOUT_HOURS` / `GRACE_WINDOW_HOURS` / `QUEUE_CAP` (consumed by Task 6). 25 new pytest cases, stdlib-only.
**Deviations:** None.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 4 minor findings, all recommend "no action" → [logs/working/task-3/code-reviewer-round1.json](logs/working/task-3/code-reviewer-round1.json)
- security-auditor: ok, 5 findings (1 low, 1 low, 3 info), all recommend "no action" → [logs/working/task-3/security-auditor-round1.json](logs/working/task-3/security-auditor-round1.json)
- test-reviewer: ok-with-nits, 5 minor findings, all recommend "no action" → [logs/working/task-3/test-reviewer-round1.json](logs/working/task-3/test-reviewer-round1.json)

*Round 2:* Skipped. All 14 findings are minor/low/info with the reviewers themselves recommending "no action" — either matches spec explicitly (CR-1 graceful-degradation wrapper, CR-3 literal-tuple ordering, CR-4 strict `Counter` subscript per task-3 Edge Cases, SEC-2/3/4 secret-redaction guards, TR-2/TR-3/TR-5 coverage confirmations) or is explicitly out of scope for task 3 (CR-2 / SEC-5 env-knob parsing — Task 6 job loop owns those; SEC-1 secret substring overlap — 30-50 char opaque tokens make overlap astronomically unlikely, documented as accept-as-is; TR-1 pre-existing `[REDACTED]` marker — optional test, not in acceptance criteria). No HIGH/CRITICAL / no MEDIUM requiring action. Per Step 5 of the task runbook, round 1 is final.

**Verification:**
- `pytest tests/test_admin_ping.py -v` → 25 passed (0.77s)
- `pytest tests/ -q` → 237 passed (full suite, no regression from parallel tasks 1/2/4)
- Smoke 1 (`sanitize_error_message(Exception('api abc xyz'))` with `TELEGRAM_BOT_TOKEN='abc'`) → `api [REDACTED] xyz`, exit 0
- Smoke 2 (`build_admin_ping([])`) → `None`, exit 0
- Smoke 3 (`build_admin_ping([...2×autoevolution, 1×mattel])`) → `3 ждут review: 🟠 autoevolution ×2, 🟣 mattel ×1`, exit 0
- Smoke 4 (`set(SOURCE_EMOJI) == set(SOURCE_LABEL) == {'autoevolution', 'mattel', 'lamley'}`) → `ok`, exit 0

## Task 1: `pending_articles_repo` DAO

**Status:** Done
**Commit:** 553710f
**Agent:** teammate (task-1), reviews resumed by main agent after rate-limit
**Summary:** New stdlib-only `pending_articles_repo.py` implementing `init_schema` plus the 19 CRUD / list / transactional-move functions prescribed by Task 1 scope. DDL uses `CREATE TABLE IF NOT EXISTS` for all three new tables (`pending_articles`, `published_articles`, `failed_articles`) with columns matching tech-spec §Data Models via `PRAGMA table_info` dict-literal assertions. JSON fields (`paragraphs`, `images`, `blocks`, `ru_paragraphs`, `ru_blocks`) round-trip through `json.dumps(..., ensure_ascii=False)` / `json.loads`, distinguishing NULL from `'[]'`. Transactional moves (`move_to_published`, `move_to_failed`, `skip_pending`, `retry_from_failed`) hold one connection with explicit `try` / `commit()` / `except: rollback(); raise` / `finally: close()`. All SQL uses `?` placeholders (grep-style audit test enforces this). 31 pytest cases, all green.
**Deviations:** `preview_html_path` column and `set_preview_path` helper — listed in tech-spec §Data Models and §Repo module interface — are intentionally NOT in this commit. Per Task 7 scope (tasks/7.md §Details and §What-to-do step linking `set_preview_path`), Task 7 extends `pending_articles_repo.py` with the column and helper at the time the preview CLI needs them. Task 1's task-file What-to-do lists exactly 19 functions; the implementation matches that list.

**Reviews:**

*Round 1:*
- code-reviewer: ok-with-nits, 5 findings (3 low, 2 info; all "no action" or docs-only nits — unused module-level logger, undocumented `increment_attempt` missing-row return, `news_bot` cyclic-import awareness, SQLite TOCTOU theoretical window) → [logs/working/task-1/code-reviewer-round1.json](logs/working/task-1/code-reviewer-round1.json)
- security-auditor: ok, 5 findings (all info, no action) — confirmed parameterised SQL, safe `int()`-coerced datetime modifier, intentional raw-`last_error` storage per Decision 11 / AC L103, empirical rollback verification, no JSON-deserialisation attack surface → [logs/working/task-1/security-auditor-round1.json](logs/working/task-1/security-auditor-round1.json)
- test-reviewer: ok-with-nits, 6 findings (3 low, 3 info — all optional: missing-link no-op tests for `increment_attempt` / `move_to_failed`, entry-dict key-omission coverage, WrappingConn execute-count coupling note, stubbed `processed_news` DDL drift awareness, regex-audit scope) → [logs/working/task-1/test-reviewer-round1.json](logs/working/task-1/test-reviewer-round1.json)

*Round 2:* Skipped. All 16 findings across three reviewers are low/info with explicit "no action" or docs-only recommendations; no HIGH/CRITICAL, no correctness defect, no security vulnerability. Per Step 4 of the task runbook (all status `ok`/`ok-with-nits` and no HIGH/CRITICAL → break out), round 1 is final.

**Verification:**
- `pytest tests/test_pending_articles_repo.py -v` → 31 passed (2.08s)
- Smoke 1 (`python3 -c "import pending_articles_repo; pending_articles_repo.init_schema(__import__('sqlite3').connect(':memory:'))"`) → exit 0
- Smoke 2 (tempfile DB → `init_schema` → `insert_pending({link, source_name, title, paragraphs=['p'], images=[], blocks=None, pub_date=None})` → `count_pending()`) → prints `1`, exit 0

