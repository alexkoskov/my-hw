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
