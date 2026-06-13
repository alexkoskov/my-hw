# Code Research: cross-source-dedup

Feature: extract car-model fingerprint from article body, compare across sources via Jaccard similarity, block hard duplicates and soft-flag near-duplicates inside `news_bot.job()` between `_is_text_only_checklist` and `pending_repo.insert_pending`.

Cross-referenced against:
- `news_bot.py` (2072 LoC)
- `pending_articles_repo.py` (786 LoC)
- `admin_alerts.py` (366 LoC)
- `boilerplate_filter.py` (309 LoC)
- `t_hunted_source.py`, `autoevolution_source.py`, `lamley_source.py`
- `tests/test_*` (39 test files)
- `.claude/skills/project-knowledge/references/{architecture,patterns,ux-guidelines}.md`

---

## 1. Entry Points — `job()` pipeline (news_bot.py)

### 1.1 The exact insert point

The new dedup-by-fingerprint stage MUST live in **`news_bot.py:1810-1815`**, between:

- `_is_text_only_checklist(entry, article)` at **`news_bot.py:1810`** — the closest pattern we'll mirror.
- `pending_repo.insert_pending(row)` at **`news_bot.py:1829`** — the gate it must precede.

Existing skeleton (`news_bot.py:1795-1836`):

```
1795: for entry in new_entries:
1796:     link = entry.get('link')
1797:     if not link: ... continue
1801:     article = fetch_full_article(entry)
1802:     if not article or not article.get('paragraphs'): ... continue
1810:     if _is_text_only_checklist(entry, article):                   ← pattern A (drop)
1811:         logger.info("Skipping checklist-only article ...")
1815:         continue                                                  ← no mark_processed call
1817:     row = { ... }                                                 ← ← INSERT new dedup STEP here
1829:     if pending_repo.insert_pending(row): inserted += 1
```

The two natural insert sites are:

- **Site A (between 1815 and 1817).** Mirrors `_is_text_only_checklist` exactly: after body is available, run the predicate, on True `continue` (skip) or send admin-ping + still proceed (soft flag).
- **Site B (between fetch_full_article result and checklist check, lines 1801-1810).** Cheaper if you want to short-circuit BEFORE checklist filter — but checklist filter is already O(1) on body length, so save Site A: it keeps the data-flow linear (dedup is the last gate before persist).

### 1.2 `_is_text_only_checklist` — the pattern to mirror

Defined at **`news_bot.py:667-694`**:
```
def _is_text_only_checklist(entry, article):
    link = entry.get('link') or ''
    if _CHECKLIST_URL_RE.search(link): return True
    title = entry.get('title') or (article or {}).get('title') or ''
    if not _CHECKLIST_TITLE_RE.search(title): return False
    paragraphs = (article or {}).get('paragraphs') or []
    total_text = sum(len(p) for p in paragraphs if isinstance(p, str))
    return total_text < _CHECKLIST_BODY_TEXT_FLOOR
```

Contract: pure predicate `(entry, article) -> bool`. Module-level regex constants defined at **`news_bot.py:643, 664`** with `_CHECKLIST_BODY_TEXT_FLOOR = 500` at **`news_bot.py:652`**. Tests live in `tests/test_news_bot_dispatcher.py` / `test_relevance_filter.py`.

### 1.3 What happens when an article is rejected (no `mark_processed` today)

Critical observation: `_is_text_only_checklist` returns True → `continue` at **`news_bot.py:1815`** WITHOUT calling `mark_processed`. The same link will be re-fetched on the next tick (and dropped again).

**Implication for dedup:** if we do the same (`continue` without `mark_processed`), every tick re-fetches the duplicate. For an EN duplicate of a PT t-hunted post, this means re-fetching from autoevolution every tick. The user-spec explicitly says "mark_processed" on hard-block path — that's an explicit divergence from the checklist pattern.

To implement `mark_processed` after dedup-block, use **`news_bot.py:593`**:
```
def mark_processed(link, title, pub_date):  # writes to processed_news table
```
Wire as:
```python
if _is_cross_source_dupe(article, fingerprint, threshold=0.5):
    logger.info("Skipping cross-source duplicate ...")
    mark_processed(link, article.get('title') or entry.get('title') or '',
                   entry.get('published') or '')
    continue
```

Title-source mirror at **news_bot.py:1821**: `article.get('title') or entry.get('title') or ''`.

### 1.4 Adjacent filters in the same pipeline

- `is_processed(link)` at **`news_bot.py:584-591`** — only check against `processed_news` PRIMARY KEY (URL-only dedup, what we're extending).
- `filter_new_entries(entries)` at **`news_bot.py:697-723`** — drops `is_processed` matches BEFORE fetch (cheap).
- `_is_hot_wheels_relevant(entry)` at **`news_bot.py:615-636`** — sibling-brand filter, BEFORE fetch.
- `_is_text_only_checklist(entry, article)` at **`news_bot.py:667-694`** — AFTER fetch.
- Inline `get_pending(e.get('link'))` filter at **`news_bot.py:1779-1782`** — drops links already in `pending_articles`.

Order in `job()`:
1. **`news_bot.py:1755-1769`** — fetch from `SOURCES`.
2. **`news_bot.py:1777`** — `filter_new_entries` (processed_news + sibling-brand).
3. **`news_bot.py:1779-1787`** — inline pending_articles filter.
4. **`news_bot.py:1801`** — `fetch_full_article` per entry.
5. **`news_bot.py:1810`** — checklist filter (drop).
6. **← NEW STAGE — cross-source dedup-by-fingerprint.**
7. **`news_bot.py:1817-1829`** — insert into `pending_articles`.

---

## 2. Data Layer — `pending_articles_repo.py`

### 2.1 DDL location

- `pending_articles` DDL at **`pending_articles_repo.py:45-68`** (`_PENDING_DDL`).
- `published_articles` DDL at **`pending_articles_repo.py:70-81`** (`_PUBLISHED_DDL`).
- `failed_articles` DDL at **`pending_articles_repo.py:83-98`** (`_FAILED_DDL`).
- `bot_state` DDL at **`pending_articles_repo.py:108-113`** (`_BOT_STATE_DDL`).

DDL bodies are bare `CREATE TABLE IF NOT EXISTS …` — schema definition is single-statement Python triple-quoted strings, NOT externalised migrations.

### 2.2 `init_schema(conn)` — `pending_articles_repo.py:165-195`

Idempotent. Executes:
1. The four `CREATE TABLE IF NOT EXISTS` statements (lines 175-178).
2. A migration block (lines 185-194): `ALTER TABLE failed_articles ADD COLUMN telegraph_url TEXT` / `… telegraph_path TEXT`, wrapped in `try / except sqlite3.OperationalError: pass`.

This is **the exact pattern to use for the new fingerprint column.** Idempotent ALTER via `try/except OperationalError`:

```python
# Migration (2026-06-XX): model_fingerprint JSON list (cross-source-dedup feature).
for ddl in (
    "ALTER TABLE pending_articles ADD COLUMN model_fingerprint TEXT",
    "ALTER TABLE published_articles ADD COLUMN model_fingerprint TEXT",
):
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError:
        pass
```

Caller: `news_bot.init_db()` at **`news_bot.py:561-582`** is the single entry point; calls `pending_repo.init_schema(conn)` at line 579, then `conn.commit()`. On a populated prod DB, the new ALTERs are executed once (silently no-op on subsequent ticks).

### 2.3 Prior column-addition migration

The only historical SQLite migration in the project is at **`pending_articles_repo.py:185-194`** (telegraph_url/telegraph_path on `failed_articles`, dated 2026-04-30). All other tables were defined freshly in the DDL block — no prior ALTER on `pending_articles` or `published_articles`. **The cross-source-dedup feature is the first ALTER on these two tables.**

### 2.4 `_PENDING_JSON_COLS` registration

**`pending_articles_repo.py:118`** declares the column-name tuple used by `_row_to_dict` to auto-deserialise JSON columns:
```python
_PENDING_JSON_COLS = ('paragraphs', 'images', 'blocks', 'ru_paragraphs', 'ru_blocks')
```

If we store `model_fingerprint` as JSON list (per user-spec), add to this tuple:
```python
_PENDING_JSON_COLS = ('paragraphs', 'images', 'blocks', 'ru_paragraphs', 'ru_blocks', 'model_fingerprint')
```
Same for `_FAILED_JSON_COLS` at line 119 if we ever propagate the column there (not needed by spec).

### 2.5 `insert_pending(entry)` — `pending_articles_repo.py:202-240`

Hardcoded column list at line 218-220:
```
INSERT INTO pending_articles
(link, source_name, feed_url, title, subtitle, paragraphs, images, blocks, pub_date)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
```

To persist `model_fingerprint`, add the column to the column list AND the values tuple (line 221-231). Use `_dumps()` for JSON encoding (line 126-131 — `ensure_ascii=False`).

### 2.6 Pending/Published row reads for fingerprint comparison

For the 7-day window comparison the dedup module needs:

- **From `pending_articles`:** all rows with `fetched_at >= datetime('now', '-7 days')` and `model_fingerprint IS NOT NULL`.
- **From `published_articles`:** all rows with `published_at >= datetime('now', '-7 days')` and `model_fingerprint IS NOT NULL`.

No existing helper does this. Closest patterns:
- `list_pending_stale(hours)` at **`pending_articles_repo.py:433-450`** — parameterised hours filter.
- `list_failed()` at **`pending_articles_repo.py:490-501`** — full table scan with ORDER BY.

Recommended: add two new repo functions, e.g. `list_recent_pending_fingerprints(days=7)` and `list_recent_published_fingerprints(days=7)`. Return shape: `list[dict]` with `(link, source_name, model_fingerprint, fetched_at/published_at)` projection. Keep them inside `pending_articles_repo` for SQL-locality (existing convention — all `SELECT * FROM pending_articles` lives here).

### 2.7 Test schema-pin

Tests pin column shape twice (drift guards):
- `tests/test_pending_articles_repo.py:30-51` — `EXPECTED_PENDING` dict, `test_pragma_table_info_matches_spec`.
- `tests/test_migration.py:38-59` — `EXPECTED_PENDING_COLUMNS`, `test_pending_articles_has_expected_columns`.

Both will fail if `model_fingerprint` is added without updating both expectations. Mandatory change set:
- Add `'model_fingerprint': {'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 0}` to both dicts.

---

## 3. Admin-ping infrastructure

### 3.1 `send_admin_notification(message)` — `news_bot.py:377-412`

Plain str → bool. Reads `TELEGRAM_BOT_TOKEN` / `TELEGRAM_ADMIN_ID` env, runs `_redact_text()` first (token redaction, **news_bot.py:269-323**), prepends `[INSTANCE_LABEL]` if set (line 391-392), then `asyncio.run(_send())`. Plain-text mode (`parse_mode=None`, comment lines 396-402).

### 3.2 Admin-alert builders — `admin_alerts.py`

Catalog of E0XX codes; each builder is `pure (...) -> str`. The full feature inventory:

- E001-E013 (lines 29-198): platform-level alerts (RSS, Claude outage, quiet day).
- E020-E024 (lines 210-265): Mattel parser.
- E025-E028 (lines 271-317): Lamley parser.
- E030 (lines 365-366): orangetrack aggregator header.
- E031-E033 (lines 323-357): t-hunted parser.

**The slot for cross-source-dedup is E014 or E040 (or anywhere unclaimed — they're sparse).** Convention: use E014 (continues the platform sequence since it's a fetch-time filter, not a per-source parser issue).

### 3.3 Columnar Russian admin-ping format (the pattern to mirror)

The columnar-multiline format is documented at `.claude/skills/project-knowledge/references/patterns.md:122-132`. Two concrete templates:

**E008 «План на сегодня» (busy day) — `admin_alerts.py:126-138`:**
```
[E008] 🟢 План на сегодня

Принято свежих: {inserted}
Всего в очереди: {queue_size}
Слоты сегодня: {slot_strs}
Перенесено на завтра: {carry_over}
```

**E006 «Пропущен дубль публикации» (idempotency-guard hit) — `admin_alerts.py:95-107`:**
```
[E006] ⚠️ Пропущен дубль публикации

Ссылка:
{link}

Что произошло:
статья уже опубликована,
зомби-строка убрана из очереди.

Что сделать:
расследовать, откуда взялась зомби-строка
(crontab, backup_db.sh, journalctl).
```

### 3.4 Template for the new «🤔 Похож на дубль» soft-flag

Mirror the E006 shape (`⚠️` → `🤔`), but two links (new + matched-existing) and a similarity score. Suggested:

```
[E014] 🤔 Похож на дубль

Новая статья:
{new_link}

Похож на:
{existing_link}

Источник новой: {new_source}
Источник существующей: {existing_source}
Совпадение моделей: {overlap_pct}% ({n_matches}/{n_total})
Общие модели:
{model_list}

Что произошло:
статья прошла в очередь, потому что
порог автоблокировки (50%) не достигнут.

Что сделать:
посмотри обе статьи — если это явно
дубль, удали лишнюю через hw_review.py;
если разные — игнорируй пинг.
```

### 3.5 Tests for the new alert

Pattern at **`tests/test_admin_alerts.py:25-60`** (one test per E0XX code). Each test:
1. Calls the builder with sample inputs.
2. `assertIn` the code prefix, severity emoji, key strings.
3. `assertIn` "Что сделать" for actionable alerts.

For the soft-flag, integration tests will rely on substring `'Похож на дубль'` — keep it verbatim in builder docstring like the existing `'План на сегодня'` / `'Пропущен дубль публикации'` markers do.

---

## 4. Boilerplate filter — the pattern for `model_extractor.py`

### 4.1 Structure of `boilerplate_filter.py`

The single closest neighbour for the new module — 309 LoC. Structure (see lines 26-309):

1. **Module docstring** describing purpose, callers, design (lines 1-24).
2. **Module-level constants** — regex lists with descriptive comments per pattern (lines 31-227). Each regex has provenance: incident date, source quirk it catches, ReDoS-safety note.
3. **Two thin public helpers** (lines 230-309):
   - `is_boilerplate(text) -> bool` — single-item predicate.
   - `filter_boilerplate(paragraphs) -> List[str]` — collection filter.
   - `filter_blocks(blocks) -> List[dict]` — block-list variant.

### 4.2 Conventions to reuse for `model_extractor.py` / `dedup_fingerprint.py`

- **Compile regex at module load** (not inside function calls). See `_BOILERPLATE_PATTERNS` at line 81.
- **Bounded quantifiers + `^`-anchor everywhere** — ReDoS-safe (the docstrings emphasise this verbatim).
- **Pure functions, no I/O.** Caller (parser or `news_bot.job()`) owns DB/network.
- **One-line per-pattern comment + provenance** — when adding new car-models or brands, log when/why (matches the boilerplate-filter style at lines 116-128).
- **Lowercase compare** (every regex uses `re.I`) — title and body in any language match the same brand-name pattern.

### 4.3 Tests for the new module

Mirror `tests/test_boilerplate_filter.py`:

```
tests/test_boilerplate_filter.py:
  TestIsBoilerplate         — positive matches per language
  TestFilterBoilerplate     — list-level filter preserves order
  TestFilterBlocks          — block-level variant
```

For `model_extractor`:
- `TestExtractFingerprint` — known-corpus inputs (e.g. autoevolution Boulevard Mix lead) → expected model set.
- `TestJaccardSimilarity` — symmetric, known overlaps (50% / 0% / 100%).
- `TestExtractFromBody` — paragraph list input → fingerprint set.

### 4.4 Existing brand mentions / lexicons in the repo

Searched: there is **NO existing brand/model lexicon** in the codebase. Closest:
- `_is_hot_wheels_relevant` sibling-brand tuple at **`news_bot.py:633`** — single string `('matchbox',)`.
- HW glossary in `transcreate_text` at **`news_bot.py:757-773`** — translation fixers (NOT brand list).
- Brand examples scattered in LLM prompt strings: `claude_transcreation.py:150`, `gemini_transcreation.py:123`, `openrouter_transcreation.py:153`, `openai_transcreation.py:120` — all read `"Brand and model names (Hot Wheels, Mattel, Nissan GT-R, Bugatti, ...)"` as a prompt cue.
- `ux-guidelines.md:112-133` — PT/EN/RU collector-jargon glossary (NOT car-brand list).

**Conclusion:** the ~30-brand lexicon is greenfield. Define inline in `model_extractor.py` as a module-level list, similar to how `_PLUG_PLATFORMS` is defined at **`boilerplate_filter.py:73-77`**.

---

## 5. Existing dedup-mechanics

### 5.1 The three current dedup gates

| Gate | Function | Location | Scope |
|------|----------|----------|-------|
| URL-equality (processed_news) | `is_processed(link)` | `news_bot.py:584-591` | Pre-fetch |
| URL-equality (pending_articles) | `pending_repo.get_pending(link) is None` | `news_bot.py:1779-1782` | Pre-fetch |
| Sibling-brand title filter | `_is_hot_wheels_relevant(entry)` | `news_bot.py:615-636` | Pre-fetch |
| Bare-checklist body filter | `_is_text_only_checklist(entry, article)` | `news_bot.py:667-694` | Post-fetch |

The new fingerprint-dedup gate joins this list AFTER `_is_text_only_checklist` and BEFORE `insert_pending`.

### 5.2 Filter contract

All four are pure predicates `(...) -> bool`. None modify the article dict. None touch the DB (read-only via processed_news for `is_processed`). When True → caller `continue`s the loop.

Fingerprint-dedup is the first dedup that needs to **read from the DB** (`pending_articles` + `published_articles` for 7-day window). Two options:

- **Option A (recommended):** new `model_extractor.py` exposes `extract_fingerprint(article)` + `jaccard(a, b)`, and `news_bot.job()` does the SQL via `pending_repo.list_recent_*_fingerprints(...)`. Separation: extractor is pure, repo owns SQL.
- **Option B:** new combined `dedup_fingerprint.py` module that exposes a higher-level `find_duplicate(article) -> (DupeStatus, matches)` which internally calls the repo. Higher coupling but cleaner call site.

### 5.3 Other writes to `processed_news`

- `mark_processed(link, title, pub_date)` at **`news_bot.py:593-601`** — public helper, currently called only by `_fallback_publish` paths (not directly by `job()`).
- `move_to_published()` at **`pending_articles_repo.py:603-606`** — INSERT OR IGNORE into processed_news during publish.
- `move_to_published` defensive re-check at **`pending_articles_repo.py:621-637`** — checks processed_news got the row and dozapis if missing.
- `skip_pending(link)` at **`pending_articles_repo.py:691-723`** — writes to processed_news, deletes from pending. The dormant hw_review path uses it.

For the hard-block path (dedup-block-drop), `mark_processed(link, title, pub_date)` at `news_bot.py:593` is the simplest call.

---

## 6. Body content shape after `fetch_full_article`

### 6.1 Return contract by source

`fetch_full_article(entry)` at **`news_bot.py:1440-1481`** dispatches by domain. Per-source return shapes:

| Source | Function | Title | Subtitle | Paragraphs | Images | Blocks |
|--------|----------|-------|----------|-----------|--------|--------|
| autoevolution | `fetch_autoevolution_article` (`autoevolution_source.py:329-335`) | str | str | list[str] | list[str] | list[dict] ✓ |
| lamley | `fetch_lamley_article` (`lamley_source.py:407-412`) | str | str | list[str] | list[str] | ✗ (absent) |
| mattel | `fetch_mattel_article` | str | str | list[str] | list[str] | (variable) |
| t-hunted | `fetch_t_hunted_article` (`t_hunted_source.py:252-257`) | str | str | list[str] | list[str] | ✗ (absent) |
| orangetrack | pre-populated, pass-through (`news_bot.py:1456-1468`) | str | str | list[str] | list[str] | list[dict] |

**Key invariants for fingerprint extraction:**
1. `paragraphs: list[str]` is present and non-empty for every source. Guaranteed by the guard at `news_bot.py:1802` (`not article.get('paragraphs')` → skip).
2. `title: str` is present (may be empty string for orangetrack-style title-less posts; always defaulted to `''` not missing).
3. `subtitle: str` is present, may be empty.
4. `blocks` is OPTIONAL — present on autoevolution + orangetrack, absent on lamley + t-hunted. **The fingerprint extractor should work on `title + subtitle + paragraphs` only, NOT on blocks.** This keeps it source-agnostic.

### 6.2 Paragraph content quality

After `filter_boilerplate(paragraphs)` runs inside each parser, paragraph text is normal prose (no UI labels). For t-hunted specifically, the first body paragraph is lifted into `subtitle` (when ≥ 2 paragraphs exist) — see `t_hunted_source.py:216-220`. So the editorial lead lives in `subtitle` for t-hunted, in body for autoevolution. **The fingerprint extractor should concatenate `title + subtitle + paragraphs` to avoid source-specific bias.**

### 6.3 Title case-handling

All raw — no normalization. For brand-matching regex, use `re.I` (case-insensitive) like `boilerplate_filter.py` does. No additional lowercase step needed.

---

## 7. Sources registry

### 7.1 `SOURCES` at `news_bot.py:1654-1658`

```python
SOURCES = [
    _fetch_rss_entries,
    # _fetch_mattel_entries,  # disabled — see comment above
    _fetch_orangetrack_entries,
]
```

Each callable: `fetcher(notifier=send_admin_notification) -> list[dict]`. Currently active: `_fetch_rss_entries` (covers autoevolution × 2 + lamley + t-hunted via `feeds.json`), `_fetch_orangetrack_entries`. Mattel disabled (lines 1646-1653).

### 7.2 `_resolve_source_name(link)` — `news_bot.py:867-882`

```python
def _resolve_source_name(link):
    netloc = urlparse(link or '').netloc.lower()
    return NETLOC_TO_SOURCE.get(netloc, 'other')
```

`NETLOC_TO_SOURCE` mapping at **`news_bot.py:835-844`**:
```
'www.autoevolution.com':     'autoevolution'
'autoevolution.com':         'autoevolution'
'lamleygroup.com':           'lamley'
'www.lamleygroup.com':       'lamley'
'corporate.mattel.com':      'mattel'
't-hunted.blogspot.com':     't-hunted'
'orangetrackdiecast.com':    'orangetrack'
'www.orangetrackdiecast.com':'orangetrack'
```

### 7.3 Should fingerprint comparison filter by source_name?

User-spec example: t-hunted (PT) duplicate of autoevolution (EN). The intent is **cross-source comparison** — we want to find matches BETWEEN sources, not within (within-source URL duplicates already caught by `is_processed`).

Two valid approaches:
- **Approach 1:** compare against all sources unconditionally. A within-source match is also suspicious (same source republishing). Simple, no filter needed.
- **Approach 2:** filter `WHERE source_name != current_source`. Avoids self-match noise but misses within-source republishes.

Recommended: **Approach 1 (no filter)** — `is_processed` and `pending_repo.get_pending(link)` already prevent same-URL self-match (lines 706 + 1779-1782 in news_bot.py). A within-source non-URL match (e.g. autoevolution running two articles about the same Boulevard Mix on consecutive days) is worth flagging too.

---

## 8. Test infrastructure

### 8.1 Test files relevant to this feature

- `tests/conftest.py` — `sys.path.insert` only (line 12); no shared fixtures.
- `tests/test_integration.py` — full `news_bot.job()` flow with mocked sources (lines 144-225 show prep-phase pattern).
- `tests/test_pending_articles_repo.py` — schema-pin (`EXPECTED_PENDING` at line 30) + CRUD round-trip.
- `tests/test_migration.py` — `init_db` migration pin (`EXPECTED_PENDING_COLUMNS` at line 38).
- `tests/test_admin_alerts.py` — per-builder substring tests (line 25-60 example).
- `tests/test_boilerplate_filter.py` — single-module filter pattern.
- `tests/test_t_hunted_source.py` — per-source parser tests with `SAMPLE_HTML` fixtures (line 73).
- `tests/test_relevance_filter.py` — tests for `_is_hot_wheels_relevant` + `_is_text_only_checklist` (closest neighbour to the new dedup filter).
- `tests/test_sources_registry.py` — `NETLOC_TO_SOURCE` + `_resolve_source_name` (line 42-71).

### 8.2 Source-parser mocking pattern

From `tests/test_integration.py:154-219`, the integration test mocking surface is:
- `news_bot.load_feeds` (returns feed URL list).
- `news_bot.fetch_rss` (returns RSS entries per feed).
- `news_bot.fetch_full_article` (returns body dict).
- `news_bot.transcreate_via_claude` (publish path, neutered in prep-phase).
- `news_bot.send_admin_notification` (patched at `_IntegrationBase.setUp`, line 88-89).

For dedup integration tests:
- Patch `news_bot.fetch_full_article` to return two distinct articles with identical body model-references.
- Assert `pending_articles_repo.count_pending() == 1` (only first article persisted, second dropped).
- Assert `mock_notify` called with admin-ping for the soft-flag case.

### 8.3 Tempfile DB pattern

`_IntegrationBase.setUp` at **`tests/test_integration.py:69-101`**:
1. `tempfile.mkstemp(suffix='.db')`.
2. `patch('news_bot.DB_FILE', self.db_path)`.
3. `news_bot.init_db()`.
4. Patch token / channel / admin env.
5. Patch `news_bot.send_admin_notification`.
6. Patch `news_bot.fetch_mattel_news` (no-op).
7. Patch `news_bot.SOURCES` to narrow list.

`_PrepPhaseBase` extends with `time.sleep` + `_fallback_publish` patches (lines 115-136) so the publish loop is neutered.

For dedup tests reuse `_PrepPhaseBase` directly — the dedup gate fires entirely in the prep phase before any publish.

### 8.4 Existing t-hunted body fixture

`tests/test_t_hunted_source.py:73-84` provides a complete `SAMPLE_HTML` (PT-language Pop Culture article) that can be reused — the dedup test can mock `fetch_full_article` directly with a body dict matching the autoevolution Boulevard Mix shape from `tests/test_autoevolution_source.py:18+`.

There is no shared body-content fixture file. The cross-source-dedup tests will likely need 2-4 hand-crafted body dicts (autoevolution Hot Wheels Car Culture, t-hunted equivalent PT version, an unrelated lamley article, an unrelated autoevolution article).

---

## 9. Risks — code mirages

### 9.1 No existing dedup-fingerprint code

Searched the repo for `fingerprint`, `model_extractor`, `dedup_fingerprint`, `similarity`, `jaccard`:
- Only 5 hits for "fingerprint" — all about TLS/JA3 fingerprinting in `autoevolution_source.py`, `lamley_source.py`, `t_hunted_source.py` (Cloudflare bypass). NOT related to content fingerprints.
- Zero hits for `model_extractor`, `dedup_fingerprint`, `Jaccard`, `jaccard`, `similarity`.

**No naming collisions.** `model_extractor.py` and `dedup_fingerprint.py` are both free filenames.

### 9.2 No prior SQL migrations to worry about

Only one historical migration (`pending_articles_repo.py:185-194`, telegraph_url/path on `failed_articles`). The cross-source-dedup migration will be the second `ALTER TABLE` in the project's history, and the first one to touch `pending_articles` or `published_articles`. The idempotent `try/except OperationalError` pattern is proven.

### 9.3 `model_fingerprint` column name — no conflicts

`grep -n "model_fingerprint" /workspaces/debian-2/my-hw/*.py` returns nothing. Safe to introduce.

### 9.4 Race conditions

Two `news_bot.job()` invocations on overlapping ticks (e.g. systemd restart-loop) could both extract fingerprint for the same article and both attempt insert. The existing `pending_repo.insert_pending` UNIQUE-conflict handling (lines 235-238, returns False) catches the second insert. Fingerprint-dedup itself reads pending+published before the insert, so:
- T1 starts dedup for article A → reads existing fingerprints → no match → inserts A.
- T2 starts dedup for article A → reads existing fingerprints (now includes A from T1) → URL-match A → already-pending guard at `news_bot.py:1779-1782` catches it BEFORE fetch_full_article.

Race resolved by URL-filter at line 1779-1782 firing before the dedup gate.

### 9.5 Empty fingerprint case

An article whose body has zero recognized car models (e.g. industry news, retrospective, Wheelhouse profile) will produce an empty fingerprint set. Jaccard(∅, ∅) is undefined. Spec the extractor:
- Empty fingerprint → store `'[]'` in the JSON column (NOT NULL).
- Empty new-article fingerprint → skip dedup gate entirely (no match possible), let it through to `insert_pending`.

This is a critical edge case — without it, every "industry news" article would Jaccard-match every other "industry news" article (both empty sets).

### 9.6 Title hashing already done by Cloudflare bypass code

`lamley_source.py`, `autoevolution_source.py`, `t_hunted_source.py` use `curl_cffi` (Cloudflare bypass via TLS fingerprinting). This is unrelated to content fingerprinting but bears the same name. **Don't confuse the user.**

---

## 10. Constraints & Infrastructure

### 10.1 Python / SQLite versioning

- Python: implied 3.8+ (uses `from __future__ import annotations`, dict-syntax type hints, `Optional`).
- SQLite: stdlib `sqlite3` — no extra deps. No `ALTER TABLE … IF NOT EXISTS` (the project uses `try / except OperationalError` instead).
- No SQLAlchemy / Alembic — direct DDL strings.

### 10.2 Dependencies

`requirements.txt` (267B, line 1-?):
<minimal set>: schedule, feedparser, deep-translator, python-telegram-bot, pytz, dotenv, requests, beautifulsoup4, anthropic, curl_cffi.

No regex extras (uses stdlib `re`). No similarity libraries (`fuzzywuzzy`, `rapidfuzz`). **The new code should stay stdlib-only** — Jaccard on Python set is 1 line.

### 10.3 Pre-commit hooks

`.pre-commit-config.yaml` — examine before adding new files. Includes gitleaks (block secrets) but no formatter forced.

### 10.4 Deployment

Per `CLAUDE.md` user-memory entry «Operator handles production ops; Claude prepares, operator applies»: don't attempt SSH. Migration runs automatically via `news_bot.init_db()` on the first cron tick after deploy. No manual ALTER TABLE step needed.

### 10.5 Env vars

- `DB_FILE` is module-level in `news_bot.py` — defaulted, no env override beyond `.env`.
- `INSTANCE_LABEL` env var (line 391) prepends to admin ping for test vs prod.
- No new env vars needed for this feature.

### 10.6 Existing module imports for new dedup module

`news_bot.py` already imports the patterns it'll need:
- `import json` (line 11) — for fingerprint serialisation.
- `import re` (line 9) — for brand-name regex.
- `from datetime import datetime, timedelta, timezone` (line 14) — for 7-day window logic.

---

## 11. External Libraries

No new external libraries needed. Stdlib `re` + `set` operations cover everything. Context7 query not applicable here.

If we ever wanted production-grade fuzzy matching, the closest library that fits the existing dep philosophy would be `rapidfuzz` (no compile, fast, MIT). But the user-spec explicitly says regex + Jaccard — keep it stdlib.

---

## 12. Putting it together — proposed file changes

| File | Change | Lines |
|------|--------|-------|
| `pending_articles_repo.py` | Add migration ALTERs in `init_schema` (after line 194); add `model_fingerprint` to `_PENDING_JSON_COLS` (line 118); add `model_fingerprint` parameter to `insert_pending` (lines 218-231); add new `list_recent_pending_fingerprints(days)` + `list_recent_published_fingerprints(days)` helpers. | ~80 LoC added |
| `model_extractor.py` | NEW. Brand lexicon (~30 entries), regex patterns, `extract_fingerprint(article) -> list[str]`, `jaccard(a, b) -> float`. Pattern: mirror `boilerplate_filter.py`. | ~150 LoC |
| `news_bot.py` | New stage in `job()` after line 1815; calls `model_extractor.extract_fingerprint(article)`, queries repo, calls `jaccard`, takes hard-block / soft-flag branch. Also pass fingerprint into `row` dict at line 1817-1827. | ~30 LoC added |
| `admin_alerts.py` | Add `alert_cross_source_dupe(new_link, existing_link, new_source, existing_source, overlap_pct, n_matches, n_total, model_list)` builder. E014 code. | ~25 LoC |
| `tests/test_pending_articles_repo.py` | Update `EXPECTED_PENDING` (line 30-51) — add `model_fingerprint` key. Add roundtrip test. | ~10 LoC |
| `tests/test_migration.py` | Update `EXPECTED_PENDING_COLUMNS` (line 38-59) — add `model_fingerprint` key. Add `EXPECTED_PUBLISHED_COLUMNS` if not already there. | ~10 LoC |
| `tests/test_model_extractor.py` | NEW. Brand-extraction + Jaccard tests. Mirror `tests/test_boilerplate_filter.py` shape. | ~150 LoC |
| `tests/test_admin_alerts.py` | New `test_e014_cross_source_dupe` test. | ~15 LoC |
| `tests/test_integration.py` | New `TestCrossSourceDedup` class — two-article mock, assert hard-block + soft-flag branches. Reuse `_PrepPhaseBase`. | ~80 LoC |

Total greenfield: ~2 new modules (`model_extractor.py`, `tests/test_model_extractor.py`), ~7 edited files. Aligns with user-spec `size: S` if the brand lexicon stays at ~30 entries.

---

## 13. Open questions for tech-spec

These weren't fully closed in code research and need tech-spec-level decisions:

1. **Model-vs-brand granularity.** "Subaru Legacy GT" — is the fingerprint token `subaru-legacy-gt`, `subaru legacy`, `subaru`, or all three? Affects Jaccard math: too granular → no matches across sources (PT spells "Subaru Legacy GT" verbatim, EN spells "Legacy Turbo wagon"). Too coarse → over-match (every Toyota article matches every Toyota article).
2. **Year tokens.** "2018 Toyota 4Runner" — is `2018` part of the fingerprint? PT often drops the year, EN keeps it.
3. **Multi-language sets.** PT version uses Portuguese trim names sometimes (e.g. "Camionete" vs "Pickup"). Whether the lexicon needs PT-EN aliases.
4. **Threshold tuning.** Spec says 50% hard-block / 30-49% soft-flag. Empirically validate against 5-10 known duplicate pairs from prod before locking thresholds.
5. **`published_articles` 7-day query.** The table currently has `published_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP` (`pending_articles_repo.py:78`). Index on `published_at` doesn't exist — for a window of 7 days × ~7 posts/day = ~50 rows, full scan is fine. No index needed initially.

---

## 14. Tech-spec-level deepening

## Updated: 2026-06-04

This section drills down to the implementation-decision level for tech-spec drafting. Each subsection answers one of the open question buckets and proposes concrete code shapes ready to lift into the spec.

### 14.A — Lexicon: brand entries derived from real source samples

#### A.1 — Brands actually mentioned in test-fixture HTML

Scanned all four source-fixture files for car-brand strings:

- `tests/test_autoevolution_source.py:18` — "rare **Porsche**" (editorial lead, `SAMPLE_ARTICLE_HTML`); line 90 — "**Ford** &amp; Chevy" (entity-decode test). Image filenames reference "porsche" repeatedly.
- `tests/test_t_hunted_source.py:74-83` — PT body «Caça ao tesouro Pop Culture 2026», zero brand mentions in fixture body — fixture is generic. Real prod t-hunted posts (per user-spec example) reference brands inline in English (Subaru Legacy GT, Land Rover S2, 2018 Toyota 4Runner).
- `tests/test_lamley_source.py:50-68` (`SAMPLE_HTML`) — zero brand mentions, fixture text is "Sample Hot Wheels Post" generic.
- `tests/test_orangetrack_source.py:52` — "1995 **Honda** NSX"; line 54 — "1969 **Dodge** Charger"; line 69 — "**Honda** NSX"; line 1020 — "'15 **Toyota** Alphard"; lines 1250-1299 — "**Ferrari**", "**Porsche**", "**Mercedes** — быстрая машина".

**Real-content brand mentions across fixtures (deduplicated):**
- Ford, Porsche, Honda, Dodge, Toyota, Ferrari, Mercedes.

**Insight:** the existing test-fixture corpus is *thin* for brand extraction. Most fixtures use placeholder text. Calibration cannot be done from existing fixtures alone — synthetic representative bodies are required (covered in §14.F).

#### A.2 — General Hot Wheels mainline coverage

Hot Wheels mainline + premium lines (Boulevard, Car Culture, Pop Culture, Premium, Fast & Furious) and HW Boulevard typically pull from these brand families:

**Tier 1 — JDM (the dominant HW segment, ~30% of mainline castings):**
1. Toyota
2. Nissan
3. Honda
4. Mazda
5. Subaru
6. Mitsubishi
7. Datsun (vintage JDM — historically frequent in Boulevard / Car Culture vintage mixes)
8. Lexus
9. Acura

**Tier 2 — American muscle / classic (high HW presence):**
10. Ford (Mustang, F-150, Bronco — perpetual mainline)
11. Chevrolet (Camaro, Corvette, Chevelle)
12. Dodge (Charger, Challenger, Viper)
13. Plymouth (Barracuda, Road Runner)
14. Pontiac (Firebird, GTO)
15. Buick (Grand National, Regal)
16. Cadillac
17. Chrysler
18. AMC (vintage JDM-collector niche)
19. Jeep (mainline regular)
20. Hudson (Premium / vintage segment)

**Tier 3 — European (premium-line dominant):**
21. BMW
22. Mercedes (also matches "Mercedes-Benz" — handled in regex §14.B.4)
23. Audi
24. Volkswagen (also matches "VW" — handled as alias)
25. Porsche
26. Ferrari
27. Lamborghini
28. McLaren
29. Bugatti
30. Aston Martin (multi-word — see §14.B.1)
31. Land Rover (multi-word — including Range Rover variant)
32. Lotus
33. Koenigsegg

**Tier 4 — niche/edge but appears in 2026 mixes:**
34. Hispano-Suiza (premium niche, ~2% of HW Boulevard mixes)
35. Pagani (premium)

**Proposed final ~35 brand entries** (alphabetically ordered for diff-readability in source):

```
Acura, AMC, Aston Martin, Audi, BMW, Bugatti, Buick, Cadillac,
Chevrolet, Chrysler, Datsun, Dodge, Ferrari, Ford, Honda, Hudson,
Jeep, Koenigsegg, Lamborghini, Land Rover, Lexus, Lotus, Mazda,
McLaren, Mercedes, Mitsubishi, Nissan, Pagani, Plymouth, Pontiac,
Porsche, Range Rover, Subaru, Toyota, Volkswagen
```

35 entries. Hispano-Suiza explicitly excluded from MVP (PR-additive per user-spec Risk 3).

#### A.3 — Quirky brand names — collisions and gotchas

| Brand | Quirk | Mitigation |
|-------|-------|-----------|
| Aston Martin | Two words separated by space | Regex escapes space: `aston\s+martin` (see §14.B.1) |
| Land Rover | Two words; also occurs as "Range Rover" sub-brand | Two separate entries — both extract as distinct brand tokens. Land Rover ≠ Range Rover for fingerprint purposes (model lineage differs). |
| Range Rover | Sub-brand of Land Rover, but HW treats as separate marque (e.g. "Range Rover Classic" castings) | Separate entry. |
| Lotus | Risk: false-match unrelated "lotus" prose ("lotus flower", "lotus position") | Acceptable noise — HW articles rarely use the word in non-car sense. Spot-check first 2 weeks per user-spec. |
| Lexus | Risk: low — uncommon word | Safe. |
| AMC | Three-letter brand, risk of false-match ("AMC theatre", "AMC stock") | Bound with `\b` and case-sensitive — most prose uses "amc" lowercase, brand always uppercase: `\bAMC\b` (no `re.I`). |
| Mercedes | Often "Mercedes-Benz" — hyphen variant | Pattern: `mercedes(?:-benz)?` matches both. |
| Volkswagen | Frequently "VW" in EN, "Volks" in DE/PT casual prose | Pattern: `(?:volkswagen|\bvw\b)` (VW only with word-bound). |
| Chevrolet | Frequently "Chevy" in EN | Pattern: `(?:chevrolet|chevy)`. |
| BMW | Three-letter, always uppercase | `\bBMW\b` no-case-fold like AMC. |
| Honda | Risk: matches "Honda Civic" but also non-car names. Low IRL risk. | Safe with `\bhonda\b`. |
| Datsun | Vintage-only; HW Boulevard / Car Culture vintage mixes | Safe — no collision. |
| Hudson | Risk: false-match "Hudson river", "Hudson Hawk" | Lower-volume; accept noise. |
| Pagani | Italian premium | Safe — uncommon prose word. |

#### A.4 — Mainline-frequent vs edge cases

- **Mainline-frequent (every mix has ≥1):** Toyota, Nissan, Honda, Ford, Chevrolet, Dodge, BMW, Porsche, Mazda, Subaru, Mitsubishi, Volkswagen.
- **Premium-line dominant:** Ferrari, Lamborghini, McLaren, Bugatti, Aston Martin, Koenigsegg, Pagani, Land Rover, Range Rover, Mercedes, Audi, Lexus.
- **Vintage / collector niche:** Datsun, AMC, Plymouth, Pontiac, Buick, Cadillac, Chrysler, Hudson, Jeep.
- **Sport / curio:** Lotus, Acura.

For the Subaru Legacy GT × Toyota 4Runner × Land Rover S2 (real 2026-06-03 pair) — Tier 1 + Tier 3 hits. All present.

### 14.B — Exact regex patterns

#### B.1 — Multi-word brand pattern, ReDoS-safe

```python
# Multi-word brands — escaped space, bounded {1,2} whitespace between tokens.
_MULTIWORD_BRANDS_RE = re.compile(
    r'\b(aston\s{1,2}martin|land\s{1,2}rover|range\s{1,2}rover)\b',
    re.I,
)
```

- `{1,2}` bounded quantifier (ReDoS-safe).
- `\b` word-boundary on both sides — prevents "playstation aston martina" mid-string matches.
- `re.I` case-insensitive — matches "Land Rover", "land rover", "LAND ROVER".

#### B.2 — Single-word brand pattern, bounded

```python
# Single-word, case-insensitive, word-bound. Brands listed in lowercase for
# inline reading. Order: longest first within alternation isn't required for
# correctness (alternation is greedy left-to-right but each is anchored by \b).
_SINGLE_BRANDS_RE = re.compile(
    r'\b('
    r'acura|audi|bugatti|buick|cadillac|chevrolet|chevy|chrysler|'
    r'datsun|dodge|ferrari|ford|honda|hudson|jeep|koenigsegg|lamborghini|'
    r'lexus|lotus|mazda|mclaren|mercedes(?:-benz)?|mitsubishi|nissan|'
    r'pagani|plymouth|pontiac|porsche|subaru|toyota|volkswagen|vw'
    r')\b',
    re.I,
)

# Always-uppercase brands — separate compile, no re.I, to avoid noise from
# common lowercase prose ("amc theatre", "bmw" the abbreviation is fine).
_UPPERCASE_BRANDS_RE = re.compile(r'\b(AMC|BMW)\b')
```

- Alternation is bounded (no `*`, no `+` quantifiers inside).
- `\b` anchors both ends.
- `mercedes(?:-benz)?` — non-capturing optional suffix.

#### B.3 — Brand + model token (the heart of fingerprint extraction)

After identifying a brand position, look ahead for an optional year + model token:

```python
# Year prefix (optional) + model token. Year is dropped from the fingerprint
# (per user-spec — PT often omits year). Model token: starts with capital
# letter or digit, contains [A-Za-z0-9-], length bounded 2..25 to avoid
# matching prose runs.
_MODEL_AFTER_BRAND_RE = re.compile(
    r'\b'
    r'(?:(?:19|20)\d{2}\s+)?'                  # optional year 1900-2099
    r'(?P<brand>' + _BRAND_ALTERNATION + r')'  # captured brand
    r'(?:[\s\-]+'                              # one+ space-or-hyphen separator
    r'(?P<model>[A-Z0-9][A-Za-z0-9\-]{1,24})'  # model token, 2-25 chars
    r')?',                                     # model is OPTIONAL — bare brand counts too
    re.I,
)
```

Where `_BRAND_ALTERNATION` is the same alternation string as in `_SINGLE_BRANDS_RE` plus multi-word variants escaped.

- All quantifiers bounded: `\d{2}`, `\d{4}` (via `(?:19|20)\d{2}`), `{1,24}`, `[\s\-]+` is bounded in practice by the surrounding context but technically unbounded — replace with `[\s\-]{1,3}` to be safe:

```python
r'(?:[\s\-]{1,3}'                              # 1-3 space-or-hyphen separators
```

**Final hardened regex (ReDoS-safe):**

```python
_MODEL_AFTER_BRAND_RE = re.compile(
    r'\b'
    r'(?:(?:19|20)\d{2}\s{1,3})?'
    r'(?P<brand>aston\s{1,2}martin|land\s{1,2}rover|range\s{1,2}rover|'
    r'acura|audi|bugatti|buick|cadillac|chevrolet|chevy|chrysler|'
    r'datsun|dodge|ferrari|ford|honda|hudson|jeep|koenigsegg|lamborghini|'
    r'lexus|lotus|mazda|mclaren|mercedes(?:-benz)?|mitsubishi|nissan|'
    r'pagani|plymouth|pontiac|porsche|subaru|toyota|volkswagen|vw|'
    r'amc|bmw)'
    r'(?:[\s\-]{1,3}(?P<model>[A-Za-z0-9][A-Za-z0-9\-]{1,24}))?',
    re.I,
)
```

#### B.4 — Edge-case patterns: hyphenated models, alphanumeric (911, GT-R)

Already covered by `[A-Za-z0-9][A-Za-z0-9\-]{1,24}`:
- "911" (3 chars) — passes.
- "GT-R" (4 chars, hyphen) — passes.
- "4Runner" (7 chars) — passes.
- "F-150" (5 chars, hyphen) — passes.
- "Legacy GT" — extractor will match "Legacy" only; "GT" stays as next-iteration token via post-processing OR is captured by greediness if separator regex allows space. **Decision: keep extractor at one-token-after-brand to avoid over-greedy capture. "Legacy GT" → token "subaru-legacy" (brand+first-word), trim-suffix dropped.** Trade-off documented in user-spec Q1.

#### B.5 — Module-level compile pattern (mirror boilerplate_filter.py:81)

```python
# Each pattern compiled once at module load — re.compile inside a function
# would re-compile every call (boilerplate_filter.py:80 convention).
_BRAND_MODEL_PATTERNS = (
    _MODEL_AFTER_BRAND_RE,
    _UPPERCASE_BRANDS_RE,
)
```

### 14.C — Similarity formula codification (AC8 + AC10)

User-spec ACs in plain code:

```python
def jaccard(a: set, b: set) -> float:
    """Pure Jaccard. Empty intersection AND empty union → 0.0 (never undefined)."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def similarity(fp_new: dict, fp_existing: dict) -> float:
    """
    Combined similarity per AC8 + AC10.

    fp_new / fp_existing structure:
        {'strict': {'subaru-legacy', 'toyota-4runner', ...},  # brand+model tokens
         'brands': {'subaru', 'toyota', ...}}                 # brand-only tokens

    Corner cases (in this exact order):
      1. AC6: empty fp on EITHER side → 0.0 (caller skips dedup gate).
      2. AC8 part 1: if EITHER strict set has |fp| == 1 → skip brands fallback,
         use jaccard(strict, strict) only.
      3. AC8 part 2: brands-only fallback only when BOTH brands sets have ≥2
         distinct entries.
      4. Otherwise: max(jaccard(strict), jaccard(brands)).
    """
    s_new, s_old = fp_new.get('strict') or set(), fp_existing.get('strict') or set()
    b_new, b_old = fp_new.get('brands') or set(), fp_existing.get('brands') or set()

    # AC6 — empty fp on either side
    if not s_new or not s_old:
        return 0.0

    strict_sim = jaccard(s_new, s_old)

    # AC8 — 1-token-degeneracy guard
    if len(s_new) == 1 or len(s_old) == 1:
        return strict_sim

    # AC8 — brands-only fallback gated on ≥2 brands BOTH sides
    if len(b_new) >= 2 and len(b_old) >= 2:
        brands_sim = jaccard(b_new, b_old)
        return max(strict_sim, brands_sim)

    return strict_sim
```

**Worked corner cases:**

| fp_new | fp_existing | Branch | Result |
|--------|------------|--------|--------|
| `{strict:{}, brands:{}}` | any | AC6 | 0.0 |
| `{strict:{toyota-4runner}}` | `{strict:{toyota-4runner}}` | AC8 1-token | 1.0 (correct — identical 1-tok) |
| `{strict:{subaru-brz}}` | `{strict:{subaru-wrx}}` | AC8 1-token, no brand fallback | 0.0 (correct — guards against false 100%) |
| `{strict:{toyota-4runner, subaru-legacy}, brands:{toyota,subaru}}` × 2 | identical | AC8 ≥2 brands | 1.0 (correct) |
| `{strict:{ford-mustang}, brands:{ford}}` | `{strict:{ford-bronco}, brands:{ford}}` | AC8 ≥2 brands fails (1 brand each) | 0.0 strict, no fallback — returns strict_sim = 0.0 (correct — different Ford models, blocks would be wrong) |

### 14.D — Soft-flag rate-limit (AC5): bot_state vs new table

**Existing prior art:**
- `bot_state` already used for outage state machine (`outage_state.py:81-153`). Generic k/v with BEGIN IMMEDIATE wrappers.
- No precedent for per-pair compound keys in `bot_state`, but `outage_state.py:127` pattern would extend trivially:
  ```python
  conn.execute("SELECT value FROM bot_state WHERE key=?", (f"softflag:{new}|{existing}",))
  ```

**Option 1 — `bot_state` k/v with compound key.**

| Pros | Cons |
|------|------|
| Zero schema changes — leverages existing DDL at `pending_articles_repo.py:108-113`. | Compound key formatting is ad-hoc — `softflag:{new_link}:{existing_link}` collides if any link contains `:` (URLs don't, but fragile). Use `\n` separator to be safer. |
| Existing `_get` / `_set` helpers in `outage_state.py` are reusable shape. | Cannot efficiently expire old entries — `DELETE FROM bot_state WHERE key LIKE 'softflag:%' AND value < datetime('now', '-30 days')` needs a full scan, but the table is tiny so trivial. |
| Migration-free (no ALTER, no risk of OperationalError edge cases). | Mixes concerns — bot_state grows with per-pair entries forever unless explicitly pruned. |

**Option 2 — new dedicated `softflag_pings` table.**

| Pros | Cons |
|------|------|
| Explicit PK `(new_link, existing_link)` — schema-level guarantee. | Requires a 3rd table in the dedup feature — more migration surface. |
| Pruning by `last_pinged_at < now - 7d` is a single indexed scan. | Adds new file with DDL + helpers. |
| Self-documenting — table name says what it stores. | Slight code-bloat for what is operationally a tiny dataset (≤ 50 active pairs at any time per user-spec scaling math in §J). |

**Recommendation: Option 1 (`bot_state` with compound key).**

Justification:
1. **Volume is trivial** — at ~7 articles/day × 1-2 soft-flag triggers/week (per user-spec Risk 7 lamley-roundup case), the table will accumulate ~5-10 keys/week. After 6 months: ~150-300 keys. No performance issue.
2. **The dedup feature already adds a migration** (`model_fingerprint` columns × 2 tables). Adding a 3rd table is moving against the "minimize migration surface" pattern.
3. **Existing precedent in the codebase** — `bot_state` is the canonical "lazy k/v" store, exactly for use cases like this.
4. **Pruning is opportunistic** — at every dedup-soft-flag tick, before write, `DELETE FROM bot_state WHERE key LIKE 'softflag:%' AND value < datetime('now', '-30 days')`. Bounded table size.

**Proposed key format:**
```python
# Use '\n' as separator — URLs never contain newlines.
_softflag_key = lambda new, old: f"softflag:{new}\n{old}"
```

**Helper signatures (mirror `outage_state.py:_get/_set`):**

```python
def softflag_pinged_recently(new_link: str, existing_link: str, days: int = 7) -> bool:
    """True iff a soft-flag ping for this (new, existing) pair was sent within
    the last `days` days. AC5: prevents the same pair from spamming admin."""

def mark_softflag_pinged(new_link: str, existing_link: str) -> None:
    """Stamp ISO-timestamp in bot_state for the pair. AC5."""
```

Both live in the dedup module (or in `pending_articles_repo` next to `bot_state` DDL).

### 14.E — Backfill script structure

#### E.1 — Repo conventions for one-shot scripts

`scripts/` folder exists but contains only `backup_db.sh`. **No precedent for one-shot Python scripts in `scripts/`.** Existing CLI conventions live at top-level (`hw_review.py` with `argparse.ArgumentParser`, dispatch table, `main(argv=None)`, `if __name__ == '__main__': sys.exit(main())`).

**Recommendation: top-level `backfill_fingerprints.py`**, mirroring `hw_review.py` shape. Reasons:
1. Discoverability — operator runs `python3 backfill_fingerprints.py` from project root, same as `python3 hw_review.py list`.
2. Shared sys.path semantics — `from pending_articles_repo import ...` works without path-hacks.
3. The deploy.sh / git-clone workflow assumes top-level Python files.
4. No new directory convention to introduce.

#### E.2 — CLI surface (argparse)

```python
import argparse, sys, logging

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='backfill_fingerprints',
        description='One-shot backfill: extract model_fingerprint for '
                    'published_articles rows in the last N days. Idempotent.',
    )
    p.add_argument('--days', type=int, default=14,
                   help='backfill window in days (default 14, per user-spec AC11)')
    p.add_argument('--dry-run', action='store_true',
                   help='compute fingerprints but do not write to DB')
    p.add_argument('--verbose', action='store_true',
                   help='log every row processed (default: summary only)')
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    ...
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

#### E.3 — Reading `published_articles.paragraphs`

`published_articles` does NOT store `paragraphs` — its DDL at `pending_articles_repo.py:70-81` only has `(link, title, ru_title, telegraph_url, telegraph_path, source_name, published_at, via_review)`. **Paragraphs are NOT preserved into published_articles after `move_to_published`.**

**This is a blocker for the backfill plan as user-spec described it.** The backfill cannot compute fingerprints from `published_articles.paragraphs` because that column does not exist.

**Two recovery paths (escalate to tech-spec for decision):**

**Path A — backfill from `pending_articles` instead.**
- Pending rows still have paragraphs. For rows still in queue, backfill works.
- But the user-spec scenario (warm the 7-day window so PT republishes of past EN posts are caught) requires already-published rows.
- Pending queue typically <10 rows. Won't cover the 14-day window.

**Path B — fetch HTML again from URL during backfill.**
- Use `news_bot.fetch_full_article(entry_stub)` per published row.
- Network calls × ~50 rows × ~3s each = ~2.5 min total — within the user-spec ~5 min budget.
- Risk: t-hunted/autoevolution rate-limit; Cloudflare bypass via `curl_cffi` (already done in source modules).
- Risk: published article may be DELETED upstream; fingerprint = empty → store `[]`.

**Path C — add `paragraphs TEXT` column to `published_articles` going forward.**
- New articles (post-deploy) have paragraphs persisted at `move_to_published` time — `pending_articles_repo.py:546-613` would need updating.
- Past articles still missing — backfill remains stuck for them.

**Recommendation for tech-spec: Path B** (re-fetch from URL during backfill). Path C is also worth doing for future-proofing but doesn't fix the cold-start. Path B is the pragmatic ~5 min one-shot.

**Modified backfill flow:**

```python
def backfill_one(row: dict, *, dry_run: bool) -> str:
    """Returns one of: 'updated', 'skipped', 'error', 'empty-fp'."""
    if row.get('model_fingerprint') is not None:
        return 'skipped'  # idempotency — already set
    entry_stub = {'link': row['link'], 'source_name': row['source_name'],
                  'title': row.get('title') or '', 'published': ''}
    try:
        article = fetch_full_article(entry_stub)
        if not article or not article.get('paragraphs'):
            fp = {'strict': [], 'brands': []}
            if not dry_run:
                update_published_fingerprint(row['link'], fp)
            return 'empty-fp'
        fp = extract_fingerprint(article)
        if not dry_run:
            update_published_fingerprint(row['link'], fp)
        return 'updated'
    except Exception as exc:
        logger.error("backfill failed for %s: %s", row['link'], exc)
        return 'error'
```

#### E.4 — Idempotency check

```sql
SELECT link, source_name, title FROM published_articles
WHERE published_at >= datetime('now', ? || ' days')
  AND model_fingerprint IS NULL
```

- `IS NULL` is the canonical idempotency marker. **`'[]'` empty-list value means "computed but no brands found" — must NOT be re-processed.**
- Backfill writes `'[]'` for empty-fp result; subsequent runs see `'[]'` (not NULL) and skip.
- This matches the user-spec AC6 contract for runtime extractor (`'[]'` ≠ NULL).

#### E.5 — Summary output (mirrors operator UX in admin pings)

```
Backfill complete:
  Window:    14 days (228 rows scanned)
  Processed: 47 (computed fingerprint)
  Skipped:   181 (already had fingerprint)
  Empty fp:  3 (industry news, no brands found)
  Errors:    0
  Duration:  4m 23s
```

Logged to stdout for operator visibility (per user-spec Сценарий 4).

### 14.F — Calibration fixture data (8 pairs)

#### F.1 — Fixture location

`tests/fixtures/` exists with `mattel_flight_builder.py` (current sole inhabitant). Convention: Python module with fixture data exported as module-level constants.

**Recommendation: new `tests/fixtures/cross_source_dedup_pairs.py`** — single module exporting `DUPE_PAIRS` and `NON_DUPE_PAIRS` lists of pair dicts.

Pair-dict shape:
```python
{
    'label': 'pair-1-real-2026-06-03',
    'a': {'title': '...', 'subtitle': '...', 'paragraphs': [...], 'source_name': 'autoevolution'},
    'b': {'title': '...', 'subtitle': '...', 'paragraphs': [...], 'source_name': 't-hunted'},
    'expected_verdict': 'duplicate' | 'non-duplicate' | 'soft-flag',
    'expected_overlap_min': 0.50,  # for duplicates
    'expected_overlap_max': 0.30,  # for non-duplicates
    'note': 'Real 2026-06-03 pair (synthetic for AE side — Cloudflare).',
}
```

#### F.2 — The 4 duplicate pairs

**Pair 1 — Real 2026-06-03 (Car Culture Road Trip Mix):**
- A (autoevolution EN): title "New Hot Wheels Car Culture Road Trip Mix preorders start now"; subtitle synthesized from URL slug; paragraphs: synthesized 4-paragraph editorial mentioning "Subaru Legacy GT", "Land Rover S2", "2018 Toyota 4Runner", "Range Rover Classic". Marked as **synthetic representative** — real fetch blocked by Cloudflare.
- B (t-hunted PT): title "Um novo lote da série Car Culture com carros de viagem"; subtitle/paragraphs: real ~553-char body extracted in user-spec interview. Brand mentions (EN-named even in PT body): Subaru Legacy GT, Land Rover S2, Toyota 4Runner.
- Expected: 3 brand+model overlaps / ~4 total → ~75% strict-jaccard → **duplicate**.

**Pair 2 — Premium / Boulevard mix overlap (synthesized):**
- A (autoevolution): "Hot Wheels Boulevard Mix N reveals — Camaro Z28, Ford Mustang Boss, Datsun 510". 3 brands × 3 models.
- B (lamley): "Boulevard preview — Camaro Z28, Mustang Boss, Datsun 510 spotted". Same 3 models.
- Expected: 100% strict-jaccard → **duplicate**.

**Pair 3 — Pop Culture pair across sources (synthesized):**
- A (autoevolution): Pop Culture announcement with Lamborghini Countach, Porsche 911, Ferrari Testarossa.
- B (t-hunted PT): "Pop Culture nova série" mentioning Lamborghini Countach, Porsche 911, Ferrari Testarossa.
- Expected: 100% strict-jaccard → **duplicate**.

**Pair 4 — JDM Premium pair (synthesized, partial overlap = soft-flag region):**
- A (autoevolution): "JDM Premium reveal — Nissan Skyline GT-R, Toyota Supra, Mazda RX-7, Subaru WRX".
- B (lamley): "JDM heavy hitters — Skyline GT-R and Supra spotted in Premium".
- Expected: 2/4 overlap = 50% jaccard — sits exactly at hard-block boundary. **Annotate as edge-case calibration; expected_verdict='duplicate' (≥50%).**

#### F.3 — The 4 non-duplicate pairs

**Pair 5 — AC8 shape: same brand, different models (Subaru BRZ vs Subaru WRX):**
- A: "New Subaru BRZ casting in Boulevard mix". Single brand+model.
- B: "Lamley reviews the Subaru WRX premium release". Single brand+model.
- Expected: strict-jaccard {subaru-brz} vs {subaru-wrx} = 0%, brand-jaccard = 100% BUT AC8 guard (1-token strict) blocks brand fallback. Final: 0%. **non-duplicate.**

**Pair 6 — 1-token guard probe (single Toyota each):**
- A: "Quick look: 2018 Toyota 4Runner premium release".
- B: "Toyota Supra Premium variant announcement".
- Expected: strict {toyota-4runner} vs {toyota-supra} = 0%, AC8 blocks fallback. **non-duplicate.**

**Pair 7 — Related but different series (Boulevard mix vs Premium):**
- A (autoevolution): "Boulevard Mix N reveals — Camaro Z28, Mustang Boss, Datsun 510" (3 brands).
- B (lamley): "Premium Q3 lineup announced — Ferrari Testarossa, Porsche 911, Lamborghini Countach" (3 brands).
- Expected: 0 strict overlap, 0 brand overlap. **non-duplicate.**

**Pair 8 — Industry vs car review (completely unrelated):**
- A (autoevolution): "Mattel Q3 earnings exceed expectations" — empty fingerprint.
- B (lamley): "Hands-on with the Nissan GT-R Premium" — `{strict:{nissan-gt-r}, brands:{nissan}}`.
- Expected: AC6 empty-fp on A → similarity = 0. **non-duplicate** (also confirms dedup is skipped when fp empty).

#### F.4 — Source for synthesized bodies

Each synthesized body is ~150-250 words in the source language, structured as:
1. Title (~70 chars).
2. Subtitle / lead (~150 chars).
3. 3-5 paragraphs of editorial copy with brand+model mentions inline.

Authoring guidance noted in fixture docstring: "Synthesized to be representative of real HW source-page content shape. Brand+model mentions are real HW castings from public 2026 mainline data; surrounding prose is reconstructed."

### 14.G — Schema migration SQL (drop-in block)

#### G.1 — Exact insertion point

`pending_articles_repo.py:185-194` — the existing migration block. Insert the new ALTERs as a second iteration after the telegraph_url block:

```python
# Migration (2026-06-XX): model_fingerprint JSON list — cross-source-dedup
# feature. Catches PT-EN republishes that bypass URL-only dedup. Stored as
# JSON object {"strict": [...], "brands": [...]}; empty fp persisted as
# {"strict": [], "brands": []} to distinguish "computed empty" from "not yet
# processed" (NULL). Idempotent ALTER pattern — see telegraph_url precedent
# above.
for ddl in (
    "ALTER TABLE pending_articles ADD COLUMN model_fingerprint TEXT",
    "ALTER TABLE published_articles ADD COLUMN model_fingerprint TEXT",
):
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError:
        # Column already exists — idempotent on subsequent init_schema calls.
        pass
```

Place this block **inside the existing `init_schema()` function**, after line 194 (the closing `pass` of the telegraph migration loop), before the final `conn.commit()` at line 195.

#### G.2 — Expected `PRAGMA table_info` row

For migration tests (`tests/test_migration.py` + `tests/test_pending_articles_repo.py`):

```python
'model_fingerprint': {'type': 'TEXT', 'notnull': 0, 'dflt_value': None, 'pk': 0}
```

Adds to:
- `EXPECTED_PENDING` (`tests/test_pending_articles_repo.py:30`)
- `EXPECTED_PUBLISHED` (`tests/test_pending_articles_repo.py:53`)
- `EXPECTED_PENDING_COLUMNS` (`tests/test_migration.py:38`)
- `EXPECTED_PUBLISHED_COLUMNS` (if present in `test_migration.py`; otherwise create it).

### 14.H — New repo helpers (exact signatures + SQL)

All four live in `pending_articles_repo.py` after `list_failed()` (~line 502) — grouped with other list/state-read helpers.

```python
def list_recent_pending_fingerprints(days: int = 7) -> list[dict]:
    """Pending rows fetched within the last `days` days whose
    `model_fingerprint` is non-NULL. Projection: link, source_name,
    model_fingerprint (deserialised JSON object).

    Returns rows in fetched_at-ascending order (oldest first) — irrelevant
    for the dedup gate (it ORs all matches) but deterministic for tests.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT link, source_name, model_fingerprint "
            "FROM pending_articles "
            "WHERE model_fingerprint IS NOT NULL "
            "AND fetched_at >= datetime('now', ? || ' days') "
            "ORDER BY fetched_at ASC",
            (f"-{int(days)}",),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {'link': r[0], 'source_name': r[1],
         'model_fingerprint': _loads_or_none(r[2])}
        for r in rows
    ]


def list_recent_published_fingerprints(days: int = 7) -> list[dict]:
    """Published rows published within the last `days` days whose
    `model_fingerprint` is non-NULL. Same projection as
    list_recent_pending_fingerprints."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT link, source_name, model_fingerprint "
            "FROM published_articles "
            "WHERE model_fingerprint IS NOT NULL "
            "AND published_at >= datetime('now', ? || ' days') "
            "ORDER BY published_at ASC",
            (f"-{int(days)}",),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {'link': r[0], 'source_name': r[1],
         'model_fingerprint': _loads_or_none(r[2])}
        for r in rows
    ]


def update_published_fingerprint(link: str, fingerprint: dict) -> bool:
    """Used by `backfill_fingerprints.py`. Writes JSON-encoded fingerprint
    onto an existing published row. Returns True iff the row existed.

    Idempotency: callers should check WHERE model_fingerprint IS NULL first
    (per backfill flow §14.E.4); this helper does NOT enforce that — it
    overwrites unconditionally."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE published_articles SET model_fingerprint=? WHERE link=?",
            (_dumps(fingerprint), link),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
```

`update_pending_fingerprint` — **dropped per the briefing**: backfill only touches `published_articles`. The runtime extractor writes via `insert_pending` directly.

### 14.I — insert_pending signature change

#### I.1 — Backward compatibility for callers

`insert_pending(entry)` is dict-driven (`pending_articles_repo.py:202-240`). Adding `model_fingerprint` as a new optional key in `entry` is **backward-compatible at the Python level** — old callers omit the key, helper uses `entry.get('model_fingerprint')` which returns None.

But SQL-level: `INSERT INTO pending_articles (..., model_fingerprint) VALUES (..., ?)` with NULL is fine for the new schema (column is nullable per migration G.1).

**Modified SQL (line 218-220 + 221-231):**

```python
conn.execute(
    "INSERT INTO pending_articles "
    "(link, source_name, feed_url, title, subtitle, paragraphs, images, "
    " blocks, pub_date, model_fingerprint) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (
        entry['link'],
        entry['source_name'],
        entry.get('feed_url'),
        entry['title'],
        entry.get('subtitle') or '',
        _dumps(entry.get('paragraphs') or []),
        _dumps(entry.get('images') or []),
        _dumps(entry.get('blocks')),
        entry.get('pub_date'),
        _dumps(entry.get('model_fingerprint')),  # NULL-preserving — None → NULL
    ),
)
```

`_dumps(None)` returns None (`pending_articles_repo.py:129-131`) → SQLite NULL. So old callers that omit the key → NULL → safe.

#### I.2 — Tests that hit `insert_pending`

Inventoried 8 test files calling `insert_pending` (from earlier grep, lines bottom):

| File | Lines | Action needed |
|------|-------|--------------|
| `tests/test_pending_articles_repo.py` | 220-290, 443-469 | Update `_sample_entry` helper at line 84 to include `model_fingerprint` kwarg default `None`. Add 1-2 round-trip tests for `model_fingerprint` JSON roundtrip. |
| `tests/test_integration.py` | 322 | No change — uses dict literals; None default fine. |
| `tests/test_distributed_schedule_integration.py` | 505, 761, 783 | No change — dict literals; None default fine. |
| `tests/test_fallback_throttle.py` | 181 | No change. |
| `tests/test_hw_review_retry.py` | 77-79, 221, 242, 265 | No change — `_insert_pending` helper passes kwargs; None default fine. |
| `tests/test_hw_review_publish_flow.py` | 77 | No change. |
| `tests/test_news_bot_dispatcher.py` (if it touches insert_pending) | — | Check; probably no change. |

**Risk: schema-pin tests** (`test_pending_articles_repo.py:30`, `test_migration.py:38`) — **MUST** update both `EXPECTED_PENDING` and `EXPECTED_PUBLISHED` dicts. This is the only mandatory test edit caused by I.1.

### 14.J — Performance / scaling

#### J.1 — Jaccard ops volume

Per user-spec scaling math:
- ~10 articles fetched per day (per news_bot tick cadence; ~3 ticks/day × ~3-4 articles/tick).
- ~50 fingerprints in 7-day window (pending + published combined; pending has ≤10 rows, published has ~40 rows in a 7-day window).
- Per new article: 1 fingerprint extract + ~50 Jaccard computations + 1 max() reduction.
- Per day: 10 × 50 = 500 Jaccard ops.

Jaccard on 5-token Python sets: `len(a & b) / len(a | b)` ≈ ~1µs each. 500 ops/day = 500µs = 0.5ms/day. **Trivial. No optimization needed.**

#### J.2 — Regex extract on ~5KB body × 35 brand patterns

`re` module performance characteristics for the proposed regex shape:
- `_MODEL_AFTER_BRAND_RE` is a single compiled regex with alternation of ~30 brands. `re.finditer()` does one linear scan.
- All quantifiers bounded → linear time guarantee.
- 5KB text × single regex scan ≈ ~0.5-1ms on modern hardware (CPython 3.11+).
- Plus `_UPPERCASE_BRANDS_RE` (AMC|BMW only) = another ~0.5ms scan.

**Estimated total extractor time: ~1-3ms per article.** Well under the 100ms budget from user-spec.

#### J.3 — Repo query

`SELECT ... WHERE model_fingerprint IS NOT NULL AND fetched_at >= datetime(...)`:
- No index on `fetched_at` or `published_at`.
- Table sizes: pending ≤ 10 rows, published ≤ 5000 rows after years of operation. 7-day filter on published ≈ ~40 rows post-filter.
- Full scan with `WHERE` filter: ~5ms on 5000 rows. **OK.**
- If table ever exceeds 50000 rows: add `CREATE INDEX IF NOT EXISTS idx_published_published_at ON published_articles(published_at)`. **Not needed for MVP.**

#### J.4 — End-to-end dedup gate cost per article

- Extract: ~1-3ms.
- Query pending + published: ~5-10ms.
- Up to 50 similarity() computations: ~0.5ms total.
- Total: ~10-15ms per article. Within budget.

### 14.K — AC9 fallback degraded mode — exception coverage

#### K.1 — Where exceptions can escape

1. **`extract_fingerprint(article)`** — regex compile failure (compile-time, not runtime, so caught at module load — would crash news_bot import. Mitigation: pin regex syntax in test_model_extractor unit tests that compile the regex). Runtime: malformed paragraphs (non-string elements), `re.error` on edge cases. Plus AttributeError if article dict is missing keys.
2. **`similarity(fp_a, fp_b)`** — type errors (e.g. fp stored as list instead of dict — backwards-compat with old rows). KeyError on missing 'strict'/'brands' keys.
3. **`list_recent_pending_fingerprints` / `list_recent_published_fingerprints`** — `sqlite3.Error` (locked DB, disk full), `json.JSONDecodeError` on malformed historical row (e.g. backfill wrote partial data and was interrupted).
4. **`softflag_pinged_recently` / `mark_softflag_pinged`** — SQLite errors, datetime parse errors on corrupted bot_state values (pattern already handled in `outage_state.py:171,216`).
5. **`alert_cross_source_dupe` / `alert_blocked_dupe`** builders — string formatting on None / unexpected types.

#### K.2 — Wrap order recommendation

**Single outer try/except over the whole dedup-gate block, with `Exception` catch.** This matches the user-spec AC9 ("любое исключение → статья публикуется"). Per-call try/except would leak partial state (e.g. ping sent but article blocked).

```python
# At news_bot.py ~line 1816 (after _is_text_only_checklist returns False):
try:
    fp = model_extractor.extract_fingerprint(article)
    if fp['strict']:  # non-empty new fp; else skip (AC6)
        existing = (
            pending_repo.list_recent_pending_fingerprints(days=7)
            + pending_repo.list_recent_published_fingerprints(days=7)
        )
        verdict = dedup_evaluate(fp, existing)
        if verdict.action == 'hard-block':
            logger.info("Skipping cross-source duplicate %s; matched %s (overlap %d%%)",
                        link, verdict.match_link, int(verdict.overlap * 100))
            mark_processed(link,
                           article.get('title') or entry.get('title') or '',
                           entry.get('published') or '')
            send_admin_notification(admin_alerts.alert_blocked_dupe(
                new_link=link, existing_link=verdict.match_link,
                overlap_pct=int(verdict.overlap * 100)))
            continue  # AC3 — drop the article
        if verdict.action == 'soft-flag':
            if not softflag_pinged_recently(link, verdict.match_link, days=7):
                send_admin_notification(admin_alerts.alert_cross_source_dupe(
                    new_link=link, existing_link=verdict.match_link,
                    new_source=row['source_name'],
                    existing_source=verdict.match_source,
                    overlap_pct=int(verdict.overlap * 100),
                    n_matches=verdict.n_matches, n_total=verdict.n_total,
                    model_list=verdict.shared_models))
                mark_softflag_pinged(link, verdict.match_link)
            # fall through — article publishes as normal (AC4)
    # Pass fp into the row, regardless of branch (AC1):
    row['model_fingerprint'] = fp
except Exception as exc:
    # AC9 — degraded mode. Log full traceback, single admin ping with E016
    # code (silent on subsequent crashes within the same hour — rate-limited
    # via bot_state, same pattern as outage_state). Article still publishes.
    logger.error("dedup gate failed for %s: %s", link, exc, exc_info=True)
    try:
        send_admin_notification(
            admin_alerts.alert_dedup_extractor_crash(link, type(exc).__name__))
    except Exception:
        # If the alert itself crashes — give up silently. Log already emitted.
        pass
    # row['model_fingerprint'] stays unset → stored as NULL — flagged for
    # backfill on next operator pass.
```

#### K.3 — Log + admin-ping policy

- **Full traceback to ERROR log** via `exc_info=True` (Python logging convention).
- **Admin ping E016 «🟡 Дедуп упал»** — short, includes link + exception type. Use rate-limit via `bot_state` key `dedup_crash_last_pinged_at` (mirror outage_state pattern at `outage_state.py:199-204`) — max 1 ping per hour, otherwise log-only.
- **Severity:** 🟡 (yellow), not 🔴 — channel doesn't suffer; bug is in optional gate.

**E016 builder template** (drop into `admin_alerts.py` after `alert_cross_source_dupe` and `alert_blocked_dupe`):

```python
def alert_dedup_extractor_crash(link: str, error_type: str) -> str:
    return (
        f"[E016] 🟡 Дедуп упал (degraded mode)\n\n"
        f"Ссылка:\n{link}\n\n"
        f"Ошибка: {error_type}\n\n"
        f"Что произошло:\n"
        f"экстрактор фингерпринта или сравнение крашнулось.\n"
        f"Статья опубликована как обычно (fallback).\n\n"
        f"Что сделать:\n"
        f"посмотри traceback в логе — починим в hotfix.\n"
        f"Канал не страдает."
    )
```

#### K.4 — Code-code 보호 ordering for admin codes

Updated E0XX inventory after this feature:
- E014 — soft-flag «🤔 Похож на дубль» (user-spec AC4)
- E015 — hard-block «🚫 Заблокирован дубль» (user-spec AC3, short ping)
- E016 — extractor crash «🟡 Дедуп упал» (this section K.3, new — fills out AC9 visibility)

Tests required (mirror `test_admin_alerts.py:25-60`):
- `test_e014_cross_source_dupe`
- `test_e015_blocked_dupe`
- `test_e016_dedup_extractor_crash`

---

End of section 14 — tech-spec ready.
