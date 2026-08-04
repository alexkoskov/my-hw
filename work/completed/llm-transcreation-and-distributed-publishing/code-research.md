# Code Research — llm-transcreation-and-distributed-publishing

Created: 2026-04-26
Author: research agent (single-pass; no prior code-research.md existed)

Scope: Part A (Claude transcreation in auto-fallback path) + Part B (distributed publishing schedule). Operator-curated `hw_review` path remains untouched. All paths absolute. References to `news_bot.py` lines correspond to the file at `/workspaces/debian-2/my-hw/news_bot.py` rev as of 2026-04-26.

---

## 1. Current `transcreate_text` and Google Translate usage

### Function definition

`/workspaces/debian-2/my-hw/news_bot.py:327-439`

```python
def transcreate_text(text, source='auto', target='ru', is_title=False):
```

Returns: a `str`. Never `None`. Pipeline:
1. `GoogleTranslator(source, target).translate(text)` — wrapped in try/except. On exception logs `"Translation failed in transcreation: …"` at ERROR and uses `translated = text` (the original EN string) as the fallback for the rest of the function.
2. If `translated` is empty/whitespace → returns `text` (original EN).
3. Bureaucratic → plain Russian regex substitutions (table at lines 351-371, 19 patterns).
4. Passive → active regex substitutions (lines 376-379, 4 patterns).
5. Hot Wheels glossary regex substitutions (lines 384-400, 14 patterns) — keeps brand names in English, fixes "сборка гаража" → "гаражный проект", "литой автомобиль" → "дайкаст-модель", "Тур легенд" → "Legends Tour", etc.
6. If `is_title=True` (lines 405-423): chooses one emoji prefix from {🏆, 🏎️, 🚀, 💎, 🤝, 📢, 🚗, 🔥} based on regex matches against the lowercased translated text. Returns `f"{emoji} {result}"`.
7. If body (else branch, lines 426-437): truncate at 4000 chars on a sentence boundary (`[.!?][\s\n]`) — falls back to last space, then hard-cut.

### Module-level Google import

`/workspaces/debian-2/my-hw/news_bot.py:29` — `from deep_translator import GoogleTranslator`

`requirements.txt:4` pins `deep-translator==1.11.4`.

A second, non-transcreating translator wrapper exists: `translate_text` at `news_bot.py:317-325`. Returns the translated string or original on failure. Used only by `post_latest_news.py` (a legacy CLI helper not deployed). Not in the cron path. Tests: `tests/test_translation.py` (5 tests) — all target `translate_text`, NOT `transcreate_text`. Verdict: keep `translate_text` (no impact); the 5 tests in `test_translation.py` stay green.

### Call sites of `transcreate_text`

| File | Line | Context |
|---|---|---|
| `news_bot.py` | 668 | `_fallback_publish` — `ru_title = transcreate_text(en_title, is_title=True) if en_title else ''` |
| `news_bot.py` | 669 | `_fallback_publish` — `ru_subtitle = transcreate_text(en_subtitle) if en_subtitle else ''` |
| `news_bot.py` | 670 | `_fallback_publish` — `ru_paragraphs = [transcreate_text(p) for p in en_paragraphs]` |
| `news_bot.py` | 685 | `_fallback_publish` — `new_block['text'] = transcreate_text(new_block['text'])` (per-block) |
| `news_bot.py` | 687 | `_fallback_publish` — `new_block['caption'] = transcreate_text(new_block['caption'])` |
| `post_latest_news.py` | 54, 166, 167 | Legacy CLI, NOT deployed (`deploy.sh` FILES list does not include it) |
| `tests/test_mattel_integration.py` | 103, 174 | `@patch("news_bot.transcreate_text", side_effect=lambda t, **k: t)` — identity stub |
| `tests/test_integration.py` | 64, 130, 160 | `@patch('news_bot.transcreate_text')` |
| `tests/test_idle_fallback.py` | 408, 438, 460 | `@patch('news_bot.transcreate_text', mock_trans)` |

The single auto-publish call site is `_fallback_publish`. All five lines (668-687) need to switch from `transcreate_text` (Google) to a Claude-backed transcreation entry point. Per-paragraph or per-block calls add up to ~25 LLM calls per article — Claude SDK supports a single multi-output call with structured prompt covering title + subtitle + every paragraph + every block.text + every block.caption. Recommendation: collapse to a single Claude call per article with ordered input/output, fall back to per-call only on partial-failure.

### Failure return shape (per `patterns.md` line 37)

Confirmed in code (lines 339-346): on `GoogleTranslator(...).translate()` exception, `translated = text`. If `translated.strip()` is falsy, returns `text` (original EN). If a single paragraph fails, that paragraph appears in EN inside the otherwise-Russian article. The pipeline DOES NOT raise on translator failure — it produces a hybrid output. This is the existing fail-soft behavior we either replicate or improve in the Claude path.

### Glossary application — preserve in Claude prompt

The 14-entry HW glossary (news_bot.py:384-400) and the 19-entry bureaucratic table (351-371) are largely replicated in the ux-guidelines prompt at `.claude/skills/project-knowledge/references/ux-guidelines.md` — the prompt mandates "категорически запрещены пассивный залог, канцеляризмы и громоздкие причастные обороты" and per-source style notes. Claude executes these natively when given the prompt as system message. Recommendation: do NOT post-process Claude output with the regex tables — they were a Google-Translate band-aid and would over-correct an already-styled translation. If we need a safety net for known critical mis-translations (e.g. "сборка гаража"), keep a minimal HW-specific glossary regex (3-5 entries max) as a post-pass. The 19 bureaucratic patterns are subsumed by the prompt and should be dropped.

### Title emoji prefix logic (🏆/🏎️/🚀/💎/🤝/📢/🚗/🔥)

Currently emitted by `transcreate_text(is_title=True)` (lines 405-423). The ux-guidelines prompt (line 54 of ux-guidelines.md) explicitly references this set: "Hot Wheels — see `patterns.md § Transcreation` — emoji set: 🏆 / 🏎️ / 🚀 / 💎 / 🤝 / 📢 / 🚗 / 🔥 fallback". Two design options:

- **Option A — let Claude emit the emoji.** The prompt already covers it; the model has full context of the article. Simpler code path (one Claude call returns the final RU title). Risk: model might pick an unconventional emoji or skip it.
- **Option B — strip emoji from Claude output, wrap with deterministic regex (current logic at lines 406-422).** Predictable, matches existing channel format byte-for-byte. Risk: redundant work.

Recommendation: **Option A** with a smoke-test that asserts the title starts with one of the 8 emoji; on miss, fall through to the regex wrapper as belt-and-suspenders. The transcreation prompt explicitly asks for "punchy" titles + 2-3 alternates, so Claude has been doing this in operator-side reviews already.

### Body truncation at 4000 chars

Currently in `transcreate_text` (lines 425-437). Telegra.ph's per-paragraph hard limit is much higher (≥ 64 KiB per node); the 4000-char cap was a Google Translate workaround for very long source paragraphs. With Claude, the natural answer is "instruct Claude not to exceed 4000 chars per output paragraph" — the prompt can include "max ~3500 chars per paragraph". A post-pass safety check can keep the existing truncation as a fallback if Claude over-runs.

---

## 2. `_fallback_publish` and the auto-publish chain

`/workspaces/debian-2/my-hw/news_bot.py:639-772`

### Signature

```python
def _fallback_publish(row, via_review=False) -> bool:
```

Raises on any step failure; returns `True` on full success. Caller (`job()` step 1b at `news_bot.py:1190-1220`, and `_overflow_fast_track` at 869-905) wraps in try/except, calls `pending_repo.increment_attempt(link, sanitize_error_message(exc))`. On 3rd strike: `pending_repo.move_to_failed(link, safe)`.

### Flow (current implementation)

| Step | Lines | Action |
|---|---|---|
| 1 | 658-670 | Read EN text from `row` (`title`/`subtitle`/`paragraphs`); per-call `transcreate_text` for each (3 + N items + per-block text/caption) |
| 1b | 675-688 | If `row['blocks']` non-empty: clone, transcreate each `text` and `caption` field, build `ru_blocks` |
| 2 | 697-730 | Telegraph publish — reuses `row['telegraph_url']` if already populated (Decision 9 idempotency); else calls `telegraph_publisher.publish_article(..., auto_marker=not via_review)` and persists URL via `pending_repo.mark_telegraph_published(link, telegraph_url, telegraph_path)` BEFORE step 3 |
| 3 | 739-745 | `pending_repo.update_staged(link, ru_title, ru_subtitle, ru_paragraphs, ru_blocks)` — persists RU |
| 4 | 755-759 | `send_telegraph_teaser(telegraph_url, link)` — raises `RuntimeError` on `False` return |
| 5 | 762-764 | `pending_repo.move_to_published(link, telegraph_url, telegraph_path, via_review=via_review)` — atomic txn, deletes pending |
| 6 | 767 | `_cleanup_preview_html(row.get('preview_html_path'))` — best-effort |

### `via_review` flag propagation

- `via_review=False` (default) → `auto_marker=True` is passed to `telegraph_publisher.publish_article` at line 718. The `↳ автоперевод` paragraph node is inserted before the Источник footer. This is the single, locked auto-marker for ALL auto-published posts (per memory `feedback_telegram_longread.md` and `patterns.md:42-45`). Same marker applies whether the engine is Claude or Google fallback — no change needed.
- `via_review=True` → operator path (`hw_review.cmd_publish` at `hw_review.py:655`). No marker.

### Error behavior — current fail-soft

- Translator failure → `transcreate_text` swallows the exception internally and returns the original EN string. So a Google API outage does NOT raise; the article publishes with English paragraphs intermixed. After Claude switch, this becomes deliberate: if Claude raises, we want to either (a) raise to caller (counts a strike → 3 strikes → move_to_failed), or (b) fall through to the legacy Google path. Per operator decision: 2-ping/2-grace-then-fallback. So we need a wrapper `transcreate_with_claude(...)` that catches API errors and either re-raises (if grace exceeded → switch state) or returns identity-fallback (if grace not yet exceeded).
- Telegraph failure → raises `TelegraphError` (publisher internals at `telegraph_publisher.py:43`); caller bumps attempt count.
- Telegram failure → `send_telegraph_teaser` returns `False` → `_fallback_publish` raises `RuntimeError`.

### New error class

Recommend `class ClaudeTranscreationError(Exception)` in a new `claude_transcreation.py` module. Mirrors `MattelNewsError` / `TelegraphError` style — fail-soft pattern intact. Must be sanitised through `sanitize_error_message` (the API key MUST be redacted — see §6).

---

## 3. Current cron and schedule

### `news_bot.main()` — `news_bot.py:1343-1361`

```python
def main():
    init_db()
    telegraph_publisher.ensure_access_token()
    logger.info("News bot started.")
    schedule.every(12).hours.do(job)   # line 1352
    job()                              # immediate first tick
    while True:
        schedule.run_pending()
        time.sleep(60)
```

### Replacing with absolute-time cron

The `schedule` library supports `schedule.every().day.at("HH:MM")` directly; the `at(...)` time is interpreted in **system local time**. Per [schedule library docs] you can pass `at("12:00", "Europe/Moscow")` to override (requires `pytz` or `zoneinfo`). A direct shape:

```python
import zoneinfo  # py3.9+
schedule.every().day.at("12:00", zoneinfo.ZoneInfo("Europe/Moscow")).do(job)
```

`schedule==1.2.1` (pinned in requirements.txt) supports the optional `tz` arg per its 1.2.0 changelog. Verify by running `python -c "import schedule, inspect; print(inspect.signature(schedule.Job.at))"` post-deploy if uncertain.

### Container TZ

- Dev container (per `date` 2026-04-26): UTC. `cat /etc/timezone` → file does not exist; `timedatectl` → not installed. So container is UTC.
- Production VPS (per `deployment.md`): runs as `cron job` on Linux. TZ not specified — likely UTC (default Debian/Ubuntu) but operator-controlled. Cannot rely on container TZ.

**Verdict:** Pin timezone explicitly in code via `zoneinfo.ZoneInfo("Europe/Moscow")`. Independent of container TZ.

### Distributed schedule library

For the 13:00–20:00 publishing window, do NOT use `schedule.every().day.at(...)` for each post — the window is recomputed dynamically per cron tick. Better to compute target timestamps in Python and use a single asyncio-style sleeper or `schedule`'s `do_until` mechanism. See §8 for the algorithm.

---

## 4. Overflow + idle-fallback (legacy to delete)

### `_overflow_fast_track`

`/workspaces/debian-2/my-hw/news_bot.py:799-1009` (210 lines).

Inputs: `new_entries` (list of dict). Returns `(accepted, fast_track_errors)`.

Behavior summary (compressed):
1. `pre_count = pending_repo.count_pending()`; `pool_size = pre_count + len(new_entries)`.
2. If `pool_size <= QUEUE_CAP`: return `(new_entries, [])` — no eviction.
3. Else compute `excess`. Evict oldest `ru_paragraphs IS NULL` rows via `_fallback_publish` (Step A, lines 845-904). Then evict oldest-indexed new entries via `fetch_full_article` → `insert_pending` → `_fallback_publish` (Step B, lines 906-980). Apply throttle `time.sleep(FALLBACK_THROTTLE_SECONDS)` between calls (skip-first, lines 873/961-963).
4. Send admin ping with `auto-published {total} ({old} old + {new} new)` format.

**Removal under new design:** redundant. With distributed publishing, the queue is no longer capped — every fetched article is scheduled into the publish window. If N exceeds 11/day (40-min floor × 7h), the excess carries to the next day (per operator spec line 41-43). No "fast-track to clear" is needed.

### `_idle_fallback_publish` — does not exist as a named function

The "idle-fallback" pass is **inline in `job()`** at `news_bot.py:1172-1220`. It calls `pending_repo.list_notified_overdue(GRACE_WINDOW_HOURS)` then loops `_fallback_publish(row)` per row, with `time.sleep(FALLBACK_THROTTLE_SECONDS)` skip-first throttle.

**Removal under new design:** the new flow scopes "auto-publish" to mean "any post that goes through `_fallback_publish` (= scheduled job, not operator-curated)". The "idle timeout" + "grace window" concept disappears: once the prep tick at 12:00 fetches articles, every one of them gets a slot in the 13:00-20:00 publish window. The operator never sees the "auto-publish coming" admin ping for stale rows.

The heads-up step 1a (`news_bot.py:1145-1170`, `list_pending_stale(IDLE_TIMEOUT_HOURS)` + admin ping + `mark_notified`) also goes away. `IDLE_TIMEOUT_HOURS` and `GRACE_WINDOW_HOURS` env vars: deletable.

### Tests to delete or trim

| File | LoC | Tests | Status under new design |
|---|---|---|---|
| `tests/test_overflow.py` | 772 | 13 (TestOverflowHelper×11 + TestOverflowInJob×2) | **DELETE all 13.** `_overflow_fast_track` removed; no test stays. |
| `tests/test_idle_fallback.py` | 473 | 12 (TestHeadsUp×3 + TestOverdueAutopublish×6 + TestFallbackPublishHelper×3) | **Delete TestHeadsUp×3 and TestOverdueAutopublish×6** (heads-up + grace logic removed). **Keep TestFallbackPublishHelper×3** — they exercise `_fallback_publish` directly (`test_helper_calls_transcreate_on_paragraphs`, `test_helper_cleans_up_preview_html`, `test_helper_teaser_false_raises`); rename + relocate to a new `tests/test_fallback_publish.py`. Of these 3, the first will need rewriting to assert Claude (not Google) is called. |
| `tests/test_fallback_throttle.py` | 390 | 11 (TestFallbackThrottleConstant×2 + TestOverflowThrottle×2 + TestIdleFallbackThrottle×2 + TestTeaserAlwaysSingleLine×2 + TestFallbackPublishPassesAutoMarkerToPublishArticle×1, +0 misc) | **Delete throttle tests (TestFallbackThrottleConstant×2 + TestOverflowThrottle×2 + TestIdleFallbackThrottle×2 = 6 tests).** **Keep TestTeaserAlwaysSingleLine×2** — they verify Decision 14 byte-equality of channel teaser. **Keep TestFallbackPublishPassesAutoMarkerToPublishArticle×1** — verifies marker plumbing through `_fallback_publish`. Rebase the kept 3 onto the new `_fallback_publish` (Claude-backed). |

Net delete: 13 + 9 + 6 = **28 tests removed**. Net keep + adapt: 6 (3 from idle_fallback + 3 from throttle).

### Tests that stay green untouched

- `tests/test_admin_ping.py` (25 tests) — `build_admin_ping` is independent of the cron interval / overflow logic.
- `tests/test_telegram.py` (11 tests) — teaser format locked.
- `tests/test_telegraph_publisher.py` (39 tests) — node tree + auto_marker placement.
- `tests/test_no_token_leak_in_logs.py` (8 tests) — `_TokenRedactingFilter` regression.
- `tests/test_pending_articles_repo.py`, `tests/test_database.py`, `tests/test_migration.py` — DB layer.
- `tests/test_translation.py` (5 tests) — `translate_text`, not `transcreate_text`. Stays.
- `tests/test_hw_review_*.py` (4 files, ~70 tests combined) — operator path untouched.
- `tests/test_mattel_*.py`, `tests/test_lamley_source.py`, `tests/test_autoevolution_source.py`, `tests/test_boilerplate_filter.py`, `tests/test_feed_iteration.py` — source parsers, untouched.
- `tests/test_job_prep_phase.py` — needs review (it tests `job()` end-to-end). Likely needs updating since `job()` will lose 1a/1b heads-up/overdue passes.
- `tests/test_integration.py` (4 tests) — patches `news_bot.transcreate_text`. Will continue working if the patched name still exists; if we rename it to `transcreate_via_claude`, update patch targets.

---

## 5. Throttle (legacy to delete)

### `FALLBACK_THROTTLE_SECONDS`

Defined at `news_bot.py:70`. Use sites:
- `news_bot.py:874` — `_overflow_fast_track` Step A old-evict loop (`time.sleep(FALLBACK_THROTTLE_SECONDS)` between calls, skip-first via `publish_attempts > 0`).
- `news_bot.py:962` — `_overflow_fast_track` Step B new-autopub loop (same pattern).
- `news_bot.py:1194` — `job()` step 1b idle-fallback loop (skip-first via `_idx > 0`).

All 3 use sites disappear when overflow + idle-fallback are removed. The distributed publish schedule is the new pacing mechanism — the gap between posts is `interval_min = max(420/N, 40)` minutes, which subsumes the throttle role.

`patterns.md` lines 47-51 also describe the throttle and need editing alongside.

`tests/test_fallback_throttle.py` — see §4.

---

## 6. `_TokenRedactingFilter` for log hygiene

`/workspaces/debian-2/my-hw/news_bot.py:212-235` (filter class + line 209 regex `_BOT_TOKEN_RE`).

Currently redacts: bot tokens of shape `\d{6,12}:[A-Za-z0-9_-]{30,}` (line 209). Filter installed on root logger (line 243) and on `httpx`, `httpcore`, `urllib3`, `requests` (line 245).

Separate `sanitize_error_message` helper (lines 83-113) covers `_SECRET_ENV_NAMES = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHANNEL_ID', 'TELEGRAM_ADMIN_ID', 'TELEGRAPH_ACCESS_TOKEN')` — substitutes each env value verbatim with `[REDACTED]`. This applies to error strings stored in `pending_articles.last_error` and admin-chat messages.

### Required additions for Anthropic key

1. Append `'ANTHROPIC_API_KEY'` to `_SECRET_ENV_NAMES` tuple (line 75-80). One-line change. Coverage: any `requests`-style HTTP error string carrying the key → redacted.
2. Add a regex pattern to `_TokenRedactingFilter` for `sk-ant-...` shape. Anthropic API keys start with `sk-ant-api03-` followed by ~85 chars `[A-Za-z0-9_-]`. Recommended regex: `re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")`. Add as `_ANTHROPIC_KEY_RE` and apply alongside the existing `_BOT_TOKEN_RE.sub` in `filter()` body.

Test placement: extend `tests/test_no_token_leak_in_logs.py` with parallel `TestAnthropicKeyRedactingFilter` class — copy the 4 tests from `TestTokenRedactingFilter` with `sk-ant-...` synthetic key.

---

## 7. ux-guidelines prompt loading

`/workspaces/debian-2/my-hw/.claude/skills/project-knowledge/references/ux-guidelines.md` — 122 lines.

### Sections (verified)

1. Title (line 1)
2. Scope table (lines 5-13) — distinguishes operator vs auto path
3. **The system prompt** (lines 15-40) — the Russian-language transcreation instruction in blockquote form. This is the body the cron must feed to Claude as system prompt.
4. Operational checklist (lines 42-56) — operator-side, NOT directly applicable to cron path.
5. Length + structure rules (lines 58-69) — applies to all paths; "translate everything, drop only noise".
6. Per-source style notes (lines 71-122) — `🟠 Autoevolution`, `🔵 Lamley`, `🟡 Mattel`. ~17 lines per source. The cron path can pass the relevant per-source section based on `row['source_name']`.

### File size

122 lines, ~10 KB. Fits comfortably in a Claude system prompt with cache savings.

### Loading

The file ships with the operator workspace, **NOT** the deploy bundle. `deploy.sh` FILES list (lines 21-31) and `.github/workflows/deploy.yml` (lines 86-95) ship: `news_bot.py`, `autoevolution_source.py`, `mattel_news_source.py`, `lamley_source.py`, `telegraph_publisher.py`, `pending_articles_repo.py`, `feeds.json`, `requirements.txt`, `.env.example`. **The `.claude/` directory is NOT shipped.**

**Action:** Add `.claude/skills/project-knowledge/references/ux-guidelines.md` to the FILES list in BOTH `deploy.sh` and `.github/workflows/deploy.yml`. Or, simpler: bake the prompt body into the Python module (a string constant in `claude_transcreation.py`). Trade-off: in-Python is operator-painful to edit; file-shipped is one extra deploy entry but matches the existing convention of operator-edits-prompt-once.

**Recommendation:** ship the file. Path on production: `<DEPLOY_PATH>/.claude/skills/project-knowledge/references/ux-guidelines.md`. Loaded at module-import-time via:

```python
PROMPT_PATH = os.path.join(os.path.dirname(__file__),
                            '.claude', 'skills', 'project-knowledge',
                            'references', 'ux-guidelines.md')
```

Cache content; reload on file mtime change (cheap).

### Per-source customization

The operator-side prompt expects per-source style calibration (see lines 71-122). Cron path can either (a) feed full prompt + the article (Claude picks up the `🟠/🔵/🟡 source` section that matches), or (b) crop to "global rules + relevant per-source section" via splitting on `### 🟠 Autoevolution` markers. (a) is simpler, ~3000 input tokens vs ~1500 for (b). At Haiku pricing, the difference is sub-cent per call. Do (a).

---

## 8. Schedule computation function

### Pure function shape

```python
from datetime import datetime, time, timedelta
from typing import List, Tuple

def compute_publish_slots(
    n: int,
    now: datetime,
    window_start: time = time(13, 0),
    window_end: time = time(20, 0),
    min_interval_min: int = 40,
) -> Tuple[List[datetime], int]:
    """
    Compute publish timestamps for n pending articles within today's window.

    Returns (slots_today, carry_over_count).
    - slots_today: datetimes for each post publishable today, in chronological order.
    - carry_over_count: posts that don't fit; rolled to tomorrow's window.

    Algorithm:
      window_minutes = (window_end - window_start) in minutes  # 420 for 13:00-20:00
      raw_interval = window_minutes / n
      interval = max(raw_interval, min_interval_min)
      max_today = floor(window_minutes / interval) + 1  # +1 for the start slot
      effective_start = max(window_start_today, now)    # restart recovery
      remaining_minutes = (window_end_today - effective_start) in minutes
      max_publishable_now = floor(remaining_minutes / interval) + 1
      slots = [effective_start + i*interval for i in range(min(n, max_publishable_now))]
      carry_over = n - len(slots)
    """
```

### Examples (from operator spec lines 32-43)

- N=5, window=420min: `interval = max(420/5, 40) = max(84, 40) = 84`. Slots at 13:00, 14:24, 15:48, 17:12, 18:36. carry=0.
- N=15, window=420min: `interval = max(420/15, 40) = max(28, 40) = 40`. `max_today = floor(420/40)+1 = 11`. Slots at 13:00, 13:40, …, 19:40 (11 slots). carry=4.
- N=5, container restart at 16:00, window 16:00-20:00 (240 min remaining): `interval = max(240/5, 40) = 48`. Slots at 16:00, 16:48, 17:36, 18:24, 19:12. carry=0. Matches operator example (line 75-77).
- N=5, container restart at 19:50: `remaining = 10 min`. `interval = max(10/5, 40) = 40`. `max_publishable_now = floor(10/40) + 1 = 1`. 1 slot at 19:50. carry=4. Matches operator example (line 78).

### Container restart recovery

On `main()` startup, AFTER prep tick (or independently): call `compute_publish_slots(count_pending(), now())`. Schedule the slots in-process. If `now() > 20:00 MSK`, all carry over to tomorrow (slots empty, `n` carried).

### Testing

Pure function — easy to test. Unit tests in a new `tests/test_compute_publish_slots.py`:
- N=0 → empty slots, carry=0.
- N=1 → 1 slot at window_start.
- N=11 (max) → 11 slots at 40-min boundaries.
- N=12 → 11 slots, carry=1.
- N=5 with `now=16:00` → matches restart recovery example.
- N=5 with `now=19:50` → 1 slot, carry=4.
- TZ-aware datetime input (Europe/Moscow) — assert returned slots carry the same tzinfo.
- Window crosses midnight (edge case, deferred — currently 13-20 MSK is intra-day).

---

## 9. Persistence — in-memory vs DB

### Recommendation: **in-memory.**

Pros:
- No schema migration. No new column on `pending_articles` for `scheduled_at`.
- Recovery on container restart is identical to recovery on first start: read `count_pending()`, call `compute_publish_slots`. The fact that the schedule is recomputed (not "resumed from where it left off") is acceptable — published articles already moved to `published_articles` and don't appear in count.
- No race between "scheduled" and "moved to published" — once `_fallback_publish` finishes, the row is gone from `pending_articles`.

Cons:
- A container restart at 14:30 with 5 pending may publish #1 at 14:30 instead of "the 13:00 slot it would have had" — but since #1 wasn't published before the restart, this is correct.
- If the container restarts in a busy loop (e.g. crash-loop), each restart resets the schedule — and if the cron is mistimed, posts can fire bunched. Mitigation: a 30-second startup delay + a "min-time-since-last-publish" guard read from `published_articles.published_at` ORDER BY DESC LIMIT 1.

### Edge cases (operator examples confirmed)

- 16:00 restart with 5 pending → 5 slots fit (16:00, 16:48, 17:36, 18:24, 19:12). All publish today.
- 19:50 restart with 5 pending → only 1 slot fits (19:50). 4 carry to tomorrow's 13:00 anchor.

### Min-time-since-last-publish guard

Read `SELECT MAX(published_at) FROM published_articles`. If the gap from `now` to `last_published` is < 40 minutes AND the next computed slot is "now", delay until `last_published + 40 min`. This protects against crash-loops.

---

## 10. `anthropic` SDK + Claude Agent SDK

### Skill location

`/home/vscode/.claude/skills/claude-api/SKILL.md` does not exist on this system. The skill is referenced in available-skills list (slash-callable) but its file is loaded on-demand. Conservative approach: rely on `context7` MCP for Anthropic SDK docs at implementation time (CLAUDE.md mandates context7 for library docs).

### Model selection

Per Anthropic naming convention used in this codebase context:
- `claude-haiku-4-5` — fastest, cheapest. Good for transcreation per-article: predictable structure, no deep reasoning required.
- `claude-sonnet-4-6` — higher quality, ~5× cost. Better for nuanced editorial tone.

The ux-guidelines prompt (122 lines) is heavyweight, but the actual transcreation task — translate + apply style + emit alts — is pattern matching, not reasoning. **Recommendation: start with Haiku 4.5; reserve Sonnet 4.6 as a fallback for "Claude Haiku output failed quality check" (e.g. doesn't start with one of 8 emoji, body length > 4000 × 1.5 over source).** This is a hedge, not a default.

### Token usage estimate

Per article:
- Input tokens (cached system prompt + per-article message):
  - System prompt = ux-guidelines.md ~10 KB ≈ 3000 tokens (cacheable across all calls — 90% discount on cache hits).
  - User message (EN article) = title + subtitle + 8-50 paragraphs × ~60 tokens/paragraph ≈ 500-3000 tokens.
- Output tokens:
  - RU title + 2-3 alts (~200 tokens) + RU subtitle (~100 tokens) + RU paragraphs (~1.2× EN paragraphs in ru) ≈ 600-3500 tokens.

Realistic averages (per `patterns.md` "Length: 8-50 paragraphs" + per-source notes):
- Input non-cached: ~1500 tokens (per-article message; system cached).
- Output: ~1500 tokens.

Haiku 4.5 pricing (per Anthropic 2026): $1/MTok input, $5/MTok output. Plus first call's $3 input on the cached prompt itself (one-shot, then 90% discount = $0.30/MTok on cache hits).
- Per article cost: ~$0.0015 input + ~$0.0075 output ≈ **$0.01/article**.
- 10 articles/day → ~$0.10/day → **~$3/month**.
- Sonnet 4.6 ≈ $3 input / $15 output → ~$0.05/article → ~$1.50/day → ~$45/month. ~5× Haiku, as expected.

### API key handling

- Env var: `ANTHROPIC_API_KEY`. Load via `python-dotenv` (already used). `.env.example` to be updated with a comment block.
- Redaction: see §6 — add to `_SECRET_ENV_NAMES` and add `_ANTHROPIC_KEY_RE` to `_TokenRedactingFilter`.
- SDK: `anthropic` Python package. Add to `requirements.txt` — pin to current major (e.g. `anthropic>=0.40,<1.0` — confirm via context7 at impl time).

### Retry semantics

The SDK has built-in exponential-backoff retries on 5xx and connection errors (default 2 retries). For our use case, that suffices. We add ONE outer-layer retry tracking:

- API outage detection: 3 consecutive `_fallback_publish` calls fail with `anthropic.APIError` / `anthropic.APIConnectionError` / `anthropic.APIStatusError(status=5xx)` → mark as "Claude unhealthy".
- Admin ping #1 immediately ("Claude API недоступна, переключусь на Google через 1ч если не восстановится").
- Wait 1h, ping #2 ("Claude всё ещё недоступна, через 1ч переключусь на Google").
- Wait 1h, switch state to "use Google fallback" → next `_fallback_publish` calls go through `transcreate_text` (the existing Google function). Admin ping #3 ("Переключился на Google").
- Periodically (every cron tick or every successful Telegraph publish) probe Claude with a 10-token health-check call. On success, restore state; admin ping ("Claude API восстановлена, возвращаюсь на нормальный режим").

State persistence: across container restarts, the "Claude unhealthy" flag should survive. Lightweight: SQLite key-value table `bot_state(key TEXT PRIMARY KEY, value TEXT)`. Or: a tiny JSON file at `<DEPLOY_PATH>/.bot_state.json`. Recommendation: SQLite (we already own a connection).

---

## 11. Existing tests that must stay green

Total tests: ~500 across `tests/*.py` files. Major categories:

| Category | Files | Status |
|---|---|---|
| Mattel parser rewrite (Decision-2 of mattel-parser-rewrite era) | `test_mattel_news_source.py`, `test_mattel_integration.py` | KEEP — untouched |
| `hw_review` operator path | `test_hw_review_*.py` (5 files) | KEEP — untouched |
| Boilerplate filter | `test_boilerplate_filter.py` | KEEP |
| Telegraph publisher | `test_telegraph_publisher.py` (39 tests) | KEEP — `auto_marker` semantics unchanged |
| Telegram | `test_telegram.py` (11 tests) | KEEP — single-line teaser locked |
| RSS / feed iteration | `test_feed_iteration.py` | KEEP |
| Lamley source | `test_lamley_source.py` | KEEP |
| Autoevolution source | `test_autoevolution_source.py` | KEEP |
| Pending repo | `test_pending_articles_repo.py`, `test_database.py`, `test_migration.py` | KEEP |
| Translate (legacy) | `test_translation.py` (5 tests) | KEEP — tests `translate_text` not `transcreate_text` |
| Token leak | `test_no_token_leak_in_logs.py` (8 tests) | KEEP + extend with Anthropic-key class |
| Sources registry | `test_sources_registry.py` | KEEP |
| Admin ping | `test_admin_ping.py` (25 tests) | KEEP |
| Job prep phase | `test_job_prep_phase.py` | UPDATE — assertions on heads-up pass + overdue pass need rewriting; cron-tick model changed |
| Integration | `test_integration.py` (4 tests) | UPDATE — `@patch('news_bot.transcreate_text')` may need to become `@patch('news_bot.transcreate_via_claude')` (or new module path) |
| **Overflow (DELETE)** | `test_overflow.py` (13 tests) | **DELETE** |
| **Idle fallback (PARTIAL DELETE)** | `test_idle_fallback.py` — heads-up + overdue (9 tests) | **DELETE 9 of 12; keep 3 helper tests** |
| **Throttle (PARTIAL DELETE)** | `test_fallback_throttle.py` — throttle-specific (6 tests) | **DELETE 6 of 11; keep 5 (teaser + auto_marker plumbing)** |

Net: ~28 tests removed, 3-5 tests adapted, ~470 tests stay green untouched. New tests to add:
- `tests/test_compute_publish_slots.py` — pure function (~10 tests).
- `tests/test_claude_transcreation.py` — Claude wrapper (~10 tests with mocked `anthropic.Anthropic` client).
- `tests/test_outage_state.py` — outage detection / 2-ping / 2h grace / Google switch / recovery (~6 tests).
- `tests/test_distributed_schedule.py` — cron tick + slot scheduling integration (~5 tests).
- Extend `tests/test_no_token_leak_in_logs.py` with `TestAnthropicKeyRedactingFilter` (~4 tests).

---

## 12. Channel teaser format (locked, must preserve)

`/workspaces/debian-2/my-hw/news_bot.py:540-587` — `send_telegraph_teaser(telegraph_url, source_url)`.

Output (per recent commit per memory `feedback_telegram_longread.md` and `patterns.md:41`):
- For `https://autoevolution.com/...` → text `'#autoevolution #news'`.
- For `https://corporate.mattel.com/...` → `'#mattel #news'`.
- For `https://lamleygroup.com/...` → `'#lamleygroup #news'` (note: TLD-stripped, NOT internal source_name `lamley`).
- For unknown netloc / empty source_url → bare `'#'` only (skips `#news` append).

LinkPreviewOptions: `url=telegraph_url, show_above_text=True` (lines 576-579) — Instant View card above the hashtag line.

Test coverage: `tests/test_telegram.py:73` (`test_mattel_teaser_appends_news_tag`), `:89` (`test_lamley_teaser_appends_news_tag`), `:105` (`test_unknown_source_does_not_emit_bare_news_tag`). And `tests/test_fallback_throttle.py:301-343` `TestTeaserAlwaysSingleLine` — keep both manual and auto paths byte-identical.

Verdict: NEW feature does NOT touch this. Same teaser for ALL auto-published posts (Claude or Google fallback). The ONLY differentiator stays inside the Telegra.ph article body (`↳ автоперевод` paragraph, controlled by `auto_marker=not via_review` at `news_bot.py:718`).

---

## 13. Risks (operator-supplied + research-confirmed)

| Risk | Confirmed in code? | Mitigation |
|---|---|---|
| Operator inattention to outage pings → silent Google fallback for week+ | Yes — outage-detection state is a flag, not a hard fail | Ping operator on switch-back too ("Claude API recovered"). Already in spec §10. |
| Cost spike from runaway fetch (e.g. 100 articles in a day) | Possible — no per-day cap currently. RSS sources `feeds.json` limited to 5 feeds × `feedparser` no per-feed cap; mattel returns up to ~10 entries per call | Hard cap: `MAX_ARTICLES_PER_DAY = 30` (env var). Excess goes to `failed_articles` with `last_error='daily_cap_exceeded'`. Operator-decision: implement now or defer. Cost at $0.01/article × 30/day = $0.30/day worst-case → ~$10/month worst-case. |
| Claude refusal (slur / prompt-injection content in source) | Plausible — Anthropic safety layer applies | `anthropic.BadRequestError`-style → caller's try/except → fall back to Google for THAT article (not whole bot). Don't count toward outage threshold. |
| TZ drift if container UTC and `schedule.every().day.at("12:00")` interpreted as UTC, not MSK | Confirmed — container is UTC; `schedule.at` defaults to local time | Pass `zoneinfo.ZoneInfo("Europe/Moscow")` explicitly; pin in code; smoke-test that asserts `compute_publish_slots(now=...)` returns MSK-tagged datetimes. |
| `schedule==1.2.1` may not support `tz` kwarg on `at()` (added in 1.2.0 per changelog — verify) | Need to verify | Run `python -c "import schedule; help(schedule.Job.at)"` in dev container. If unsupported, drop `schedule` lib for this feature, use a simple asyncio sleep loop or `apscheduler`. |
| Claude SDK retry storms during partial outage | Default backoff is 2 retries — bounded, but 5xx storms could double the retry budget | Set `max_retries=2` explicitly on the `anthropic.Anthropic(...)` client. Outage detection at SDK-level call boundary, not below. |
| ANTHROPIC_API_KEY leaks via Telegraph publisher errors / `requests` traceback | Possible — current `_TokenRedactingFilter` doesn't know about `sk-ant-` shape | Add regex + extend `_SECRET_ENV_NAMES` (see §6). |
| In-memory schedule loses state if `news_bot.py` is restarted mid-window | Yes by design (§9 trade-off) | Recompute on startup via `compute_publish_slots(count_pending(), now())`. ~5-second one-shot cost; acceptable. |
| Google Translate fallback during Claude outage produces inferior posts (bureaucratic Russian) | Yes — that's the whole reason we're replacing it | Operator-acknowledged trade-off. Single-line `↳ автоперевод` marker remains; subscriber sees no distinction. |
| Deploying ux-guidelines.md to server adds risk that operator-edits diverge | Minor — file is single-source-of-truth in repo | Document in `deployment.md`: "ux-guidelines.md is part of the deploy bundle starting from $FEATURE_NAME. Edit in repo, push, deploy — same flow as code." |

---

## Cross-cutting: Files to modify (preliminary, deepens during tech-spec)

| File | Action | Notes |
|---|---|---|
| `news_bot.py` | Modify | Replace `transcreate_text` calls in `_fallback_publish` with Claude wrapper. Remove `_overflow_fast_track` (210 lines). Remove inline idle-fallback in `job()` (~80 lines). Remove env vars `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `FALLBACK_THROTTLE_SECONDS`. Replace `schedule.every(12).hours` cron with `schedule.every().day.at("12:00", tz=...)`. Add scheduling loop + slot dispatcher. Extend `_SECRET_ENV_NAMES` and `_TokenRedactingFilter`. |
| `claude_transcreation.py` | NEW | `ClaudeTranscreationError`, `transcreate_article(en_blob, source_name) -> ru_blob`, `is_claude_healthy()`, system-prompt loader. |
| `outage_state.py` | NEW (or merge into news_bot.py) | Persisted "Claude unhealthy" flag + ping counters. |
| `compute_publish_slots.py` | NEW (or merge into news_bot.py) | Pure function `compute_publish_slots(n, now, ...) -> (slots, carry)`. |
| `pending_articles_repo.py` | Modify (optional) | Add `bot_state` table if persisting outage flag in SQLite. |
| `requirements.txt` | Modify | Add `anthropic` SDK pin. Optionally remove `deep-translator` if Google path is excised entirely (per spec: kept as 2h-grace fallback → KEEP). |
| `.env.example` | Modify | Add `ANTHROPIC_API_KEY=sk-ant-...` + comment. Remove `FALLBACK_THROTTLE_SECONDS`, `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS` blocks (each has a comment block in `.env.example`). |
| `deploy.sh` | Modify | Add `.claude/skills/project-knowledge/references/ux-guidelines.md` to FILES list. (Or a packaged subdirectory.) |
| `.github/workflows/deploy.yml` | Modify | Mirror `deploy.sh` FILES change. |
| `.claude/skills/project-knowledge/references/patterns.md` | Modify | Update §Transcreation, §Auto-fallback throttle, §Scheduling, §Overflow fast-track sections. Section "Auto-fallback throttle" goes away. |
| `.claude/skills/project-knowledge/references/deployment.md` | Modify | Document new `ANTHROPIC_API_KEY` requirement; remove `FALLBACK_THROTTLE_SECONDS`, `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS` from "Optional (tunable defaults)". Replace 12h scheduling line with 12:00 MSK + 13:00-20:00 publish window. |
| `tests/test_overflow.py` | DELETE | 13 tests; whole feature removed. |
| `tests/test_idle_fallback.py` | Modify (remove 9, keep 3) | Rewrite headers/comments to remove "idle" / "Decision 12 / 13" framing. |
| `tests/test_fallback_throttle.py` | Modify (remove 6, keep 5) | Strip throttle constant + sleep tests; rebase teaser + auto_marker tests. Rename file to e.g. `test_fallback_publish_marker.py`. |
| `tests/test_translation.py` | KEEP | Tests `translate_text` not `transcreate_text`; safe. |
| `tests/test_no_token_leak_in_logs.py` | Modify | Add `TestAnthropicKeyRedactingFilter` (~4 new tests). |
| `tests/test_compute_publish_slots.py` | NEW | Pure-function unit tests (~10). |
| `tests/test_claude_transcreation.py` | NEW | Mocked `anthropic.Anthropic` client (~10). |
| `tests/test_outage_state.py` | NEW | Outage detection state machine (~6). |
| `tests/test_distributed_schedule.py` | NEW | Integration (~5). |
| `tests/test_job_prep_phase.py` | Modify | Drop assertions on idle-fallback + overdue passes. |
| `tests/test_integration.py` | Modify | Update patch targets if `transcreate_text` is renamed. |

---

## Updated: 2026-04-24

## 14. Tech-spec implementation details (added for tech-spec phase)

This section builds on §1–§13 with implementation-level detail. Subsections track the tech-spec phase's questions: exact files / API surfaces / state machines / sequencing.

### 14.1 Concrete file change inventory

| Path | Change | Description | LoC delta |
|---|---|---|---|
| `/workspaces/debian-2/my-hw/news_bot.py` | MODIFY | Replace `_fallback_publish` body to call Claude wrapper; remove `_overflow_fast_track` (lines 799-1009 ≈ 210 LoC); inline-delete idle-fallback in `job()` (lines 1145-1220 ≈ 80 LoC); delete bureaucratic + passive + glossary regex blocks in `transcreate_text` (lines 351-403 ≈ 53 LoC); delete 4000-char truncation block (lines 425-437 ≈ 13 LoC); delete env-var loads `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `QUEUE_CAP`, `FALLBACK_THROTTLE_SECONDS` (lines 61-70); add TZ check on startup; add distributed-publish loop in `job()`; add crash-loop guard reading `MAX(published_at)`; extend `_SECRET_ENV_NAMES` and `_TokenRedactingFilter` for Anthropic key. | -350 / +180 |
| `/workspaces/debian-2/my-hw/pending_articles_repo.py` | MODIFY | Add `_BOT_STATE_DDL` constant; call `conn.execute(_BOT_STATE_DDL)` inside `init_schema` (line 156-159 area). Optionally expose `bot_state_get(key)` / `bot_state_set(key, value)` (or leave to `outage_state.py` which owns its own connection). | +25 |
| `/workspaces/debian-2/my-hw/requirements.txt` | MODIFY | Add `anthropic>=0.45.0,<1.0` and `pytz>=2024.1` (the latter required by `schedule==1.2.1.at(..., tz=...)` — see §14.6). | +2 |
| `/workspaces/debian-2/my-hw/.env.example` | MODIFY | Remove the `FALLBACK_THROTTLE_SECONDS` block (lines 17-23). Add `ANTHROPIC_API_KEY=sk-ant-…` (mandatory) + `ANTHROPIC_MODEL=claude-haiku-4-5` (optional default) + `TZ=Europe/Moscow` blocks. | -7 / +12 |
| `/workspaces/debian-2/my-hw/deploy.sh` | MODIFY | Add `.claude/skills/project-knowledge/references/ux-guidelines.md` to FILES list (after line 31). Note: scp source is a path-with-dirs; switch to `scp -r` or to a tarball if subdir creation matters. Recommend tar pre-step (see §14.8). | +1 file entry |
| `/workspaces/debian-2/my-hw/.github/workflows/deploy.yml` | MODIFY | Mirror `deploy.sh` FILES change. Add `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `TZ` to "Verify required secrets" step + new step that writes them into `.env` on the server (or appends to existing `.env`). | +30 |
| `/workspaces/debian-2/my-hw/tests/test_overflow.py` | DELETE | 13 tests, whole feature gone. | -772 |
| `/workspaces/debian-2/my-hw/tests/test_idle_fallback.py` | DELETE | 12 tests; the 3 helper-only tests get rewritten into `tests/test_fallback_publish.py` (see new files). | -473 |
| `/workspaces/debian-2/my-hw/tests/test_fallback_throttle.py` | DELETE | 11 tests; the 3 keepers (teaser-single-line, auto-marker plumbing) move to `tests/test_telegram.py` and `tests/test_telegraph_publisher.py` respectively (or stay in a renamed `tests/test_fallback_publish.py`). | -390 |
| `/workspaces/debian-2/my-hw/tests/test_integration.py` | MODIFY | Update `@patch('news_bot.transcreate_text')` → `@patch('news_bot.transcreate_via_claude')` (or `@patch('claude_transcreation.transcreate_via_claude')`). Update `test_full_pipeline_with_multiple_feeds` to assert articles flow through the new auto-publish path (not heads-up + grace). | +20 / -10 |
| `/workspaces/debian-2/my-hw/tests/test_job_prep_phase.py` | MODIFY | Drop assertions on `list_pending_stale` / `list_notified_overdue`. Add assertions: schedule computed for the day, admin ping shows N planned slots. | +15 / -25 |
| `/workspaces/debian-2/my-hw/tests/test_no_token_leak_in_logs.py` | MODIFY | Add `TestAnthropicKeyRedactingFilter` (4 tests parallel to `TestTokenRedactingFilter`). | +60 |
| `/workspaces/debian-2/my-hw/tests/test_migration.py` | MODIFY | Add `test_bot_state_table_exists` + assertions on its columns (`key TEXT PRIMARY KEY, value TEXT`). | +20 |
| `/workspaces/debian-2/my-hw/claude_transcreation.py` | CREATE | Anthropic SDK wrapper. Public API in §14.2. | +220 |
| `/workspaces/debian-2/my-hw/compute_publish_slots.py` | CREATE | Pure scheduling algorithm. Public API in §14.2. | +90 |
| `/workspaces/debian-2/my-hw/outage_state.py` | CREATE | Persisted outage flag + ping counters. Public API in §14.2. | +120 |
| `/workspaces/debian-2/my-hw/tests/test_compute_publish_slots.py` | CREATE | 12 tests for the pure algorithm. | +180 |
| `/workspaces/debian-2/my-hw/tests/test_claude_transcreation.py` | CREATE | 8 tests, mocked `anthropic.Anthropic`. | +220 |
| `/workspaces/debian-2/my-hw/tests/test_outage_state.py` | CREATE | 6 tests for state machine + persistence. | +160 |
| `/workspaces/debian-2/my-hw/tests/test_distributed_schedule_integration.py` | CREATE | 4 tests for full flow (12:00 fetch → schedule → publish-loop). | +200 |
| `/workspaces/debian-2/my-hw/tests/test_fallback_publish.py` | CREATE | 3 keeper tests rewritten for Claude-backed `_fallback_publish` + auto-marker plumbing. | +120 |

Net: ~+1500 LoC of new code/tests, ~-1700 LoC of removed legacy. Net negative on LoC.

### 14.2 Module API contracts

#### `claude_transcreation.py`

```python
# Module-level state
_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__),
    '.claude', 'skills', 'project-knowledge', 'references', 'ux-guidelines.md'
)
_PROMPT_CACHE: dict = {'mtime': None, 'body': None}  # mtime-keyed cache

class ClaudeTranscreationError(Exception):
    """Raised on any per-article Claude failure that should not classify as
    an API-level outage. Examples: BadRequestError (refusal), schema-mismatch
    parse failure, token-budget overrun. Caller treats this as a per-article
    strike — falls back to Google for THIS article only."""

class ClaudeOutageError(Exception):
    """Raised on API-level errors (5xx, connection refused, auth failure).
    Caller increments the outage counter; on threshold, switches to Google
    fallback globally."""

def _load_prompt(path: str = _PROMPT_PATH) -> str:
    """Read ux-guidelines.md, cache by mtime. Reload on file change."""

def transcreate_via_claude(
    article: dict,
    *,
    prompt_path: str = _PROMPT_PATH,
    model: str = None,        # default from ANTHROPIC_MODEL env, fallback claude-haiku-4-5
    max_tokens: int = 4096,
    timeout_s: int = 60,
    client: 'anthropic.Anthropic' = None,  # injectable for tests
) -> dict:
    """Translate one article via Claude.

    article: {
        'source_name': str,         # 'autoevolution' | 'lamley' | 'mattel'
        'title': str,
        'subtitle': str,
        'paragraphs': list[str],
        'blocks': list[dict] | None  # each block: {type, text?, caption?, ...}
    }

    Returns: {
        'title': str,                # RU title, emoji-prefixed
        'alts': list[str],           # 2-3 alternate RU titles
        'subtitle': str,             # RU subtitle
        'paragraphs': list[str],     # RU paragraphs, same order/length as input
        'blocks': list[dict] | None, # cloned, with text/caption translated
    }

    Raises:
        ClaudeOutageError    — on anthropic.APIConnectionError / APITimeoutError
                               / InternalServerError / RateLimitError (5xx).
        ClaudeTranscreationError — on BadRequestError, response parse failure,
                                   schema mismatch (e.g. paragraph-count drift),
                                   PermissionDeniedError, NotFoundError(model).
    """

def is_outage_error(exc: Exception) -> bool:
    """Classify exception. True for API-level outages (caller increments
    outage counter)."""
    # anthropic.APIConnectionError, APITimeoutError, InternalServerError,
    # RateLimitError, APIStatusError(status>=500) → True

def is_per_article_error(exc: Exception) -> bool:
    """True for per-article failures (caller falls back to Google ONLY for
    this article, doesn't increment outage counter)."""
    # anthropic.BadRequestError, schema mismatches, ClaudeTranscreationError → True

def health_check(client: 'anthropic.Anthropic' = None) -> bool:
    """10-token probe call to test recovery after outage. Returns True on
    a successful response. Used by job() to decide when to re-enable Claude."""
```

#### `compute_publish_slots.py`

```python
from datetime import datetime, time, timedelta
from typing import List, Tuple

WINDOW_START = time(13, 0)
WINDOW_END = time(20, 0)
MIN_INTERVAL_MINUTES = 40

def compute_publish_slots(
    n: int,
    now: datetime,                     # MUST be tz-aware (Europe/Moscow)
    window_start: time = WINDOW_START,
    window_end: time = WINDOW_END,
    min_interval_min: int = MIN_INTERVAL_MINUTES,
) -> Tuple[List[datetime], int]:
    """Compute publish timestamps for n pending articles within today's window.

    Returns (slots_today, carry_over_count).

    Algorithm (from §8 + operator examples):
      1. window_minutes = (window_end - window_start).total_minutes  # 420 for 13-20
      2. raw_interval = window_minutes / n  if n > 0 else 0
      3. interval = max(raw_interval, min_interval_min)
      4. effective_start = max(combine(now.date(), window_start), now)
      5. remaining_min = (combine(now.date(), window_end) - effective_start).total_minutes
      6. max_publishable_now = floor(remaining_min / interval) + 1 if remaining_min >= 0 else 0
      7. slots = [effective_start + i*interval for i in range(min(n, max_publishable_now))]
      8. carry_over = n - len(slots)

    Edge cases:
      - n == 0 → ([], 0)
      - n == 1, now < window_start → ([combine(now.date(), window_start)], 0)
      - now > window_end → ([], n) (all carry to tomorrow)
      - container restart at 16:00 with n=5 → 5 slots starting 16:00, interval=48 min
      - container restart at 19:50 with n=5 → 1 slot at 19:50, carry=4
      - now naive (no tzinfo) → raise ValueError (tz-required invariant)

    All returned datetimes carry the same tzinfo as `now`.
    """
```

#### `outage_state.py`

```python
import sqlite3
from datetime import datetime
from typing import Optional

# All functions open their own short-lived sqlite3.connect(news_bot.DB_FILE)
# connection — same pattern as pending_articles_repo. Reads `bot_state` table
# created by pending_articles_repo.init_schema (extended in this feature).

# Keys used in bot_state table:
#   'outage_started_at'   — ISO timestamp when outage was first detected
#   'last_ping_sent_at'   — ISO timestamp of most recent admin warning ping
#   'ping_count'          — '0' / '1' / '2' (string) — number of warnings sent
#   'fallback_active'     — '0' / '1' — whether Google fallback is the active engine
#   'last_health_check_at'— ISO timestamp of most recent health probe

def get_outage_started_at() -> Optional[datetime]:
    """Read 'outage_started_at' as tz-aware MSK datetime, or None."""

def set_outage_started_at(dt: datetime) -> None:
    """Set; UPSERT semantics."""

def clear_outage_state() -> None:
    """Delete all outage-related keys atomically (single txn).
    Called on successful health_check after recovery."""

def get_last_ping_sent_at() -> Optional[datetime]: ...
def set_last_ping_sent_at(dt: datetime) -> None: ...

def get_ping_count() -> int: ...
def increment_ping_count() -> int: ...

def is_fallback_active() -> bool: ...
def set_fallback_active(active: bool) -> None: ...

def get_last_health_check_at() -> Optional[datetime]: ...
def set_last_health_check_at(dt: datetime) -> None: ...

# Higher-level state machine helper:
def record_outage_event(now: datetime) -> dict:
    """Called from _fallback_publish after a Claude outage error. Implements
    the state machine in §14.4. Returns:
        {
            'state': str,          # 'no_outage' | 'ping_1_sent' | ...
            'pings_to_send': list[str],  # admin messages to post
            'fallback_now': bool,  # caller switches to Google for THIS article
        }
    Atomic: opens connection, BEGIN IMMEDIATE, reads + writes, commits.
    """

def record_recovery_event(now: datetime) -> dict:
    """Called from health_check on success. Returns:
        {
            'was_active': bool,
            'pings_to_send': list[str],  # ['Claude API recovered, switching back']
        }
    Clears outage state in single txn.
    """
```

Transaction semantics: each setter wraps `BEGIN IMMEDIATE` + `UPDATE/INSERT` + `COMMIT`. The two state-machine functions (`record_outage_event`, `record_recovery_event`) use `BEGIN IMMEDIATE` to lock the database for the read-then-write sequence so two concurrent fallback-publish calls can't double-increment the ping_count. The `pending_articles` writers in `pending_articles_repo` use the default `DEFERRED` mode — `BEGIN IMMEDIATE` here will queue them until the state-machine txn commits (typically <50ms).

### 14.3 Prompt construction for Claude

#### System prompt

The full ux-guidelines.md body, loaded once at module import, cached by mtime. Per §7, the file is 122 lines / ~3000 tokens — well within prompt-cache break-even.

System prompt = `_load_prompt()` output verbatim. Do not truncate, do not paraphrase. Per the file (line 17): "Treat the text below as your role for the duration of any translation work in this project."

A small _technical envelope_ is added to system prompt to enforce JSON output (the prompt body itself targets a human-readable Telegram-paste format). The envelope is appended AFTER the ux-guidelines body, separated by a horizontal rule:

```
---

## Output format (technical envelope)

Output a single JSON object — no markdown fence, no commentary. Schema:

{
  "title": "<RU title with emoji prefix from {🏆,🏎️,🚀,💎,🤝,📢,🚗,🔥}>",
  "alts": ["<alt RU title 1>", "<alt RU title 2>", "<alt RU title 3>"],
  "subtitle": "<RU subtitle>",
  "paragraphs": ["<RU paragraph 1>", "<RU paragraph 2>", ...],
  "blocks": [{"type": "...", "text": "<RU>", "caption": "<RU>"}, ...] | null
}

The output JSON MUST contain `paragraphs` of EXACTLY the same length as
the input EN paragraphs, in the same order. Do not merge or split. If a
block was provided, the `blocks` array must mirror its length and types.
```

#### User message format

The article is passed as a structured JSON in the user message:

```
{
  "source_name": "autoevolution",
  "title": "New Hot Wheels Chase Car ...",
  "subtitle": "...",
  "paragraphs": ["P1 EN text", "P2 EN text", ...],
  "blocks": [{"type": "image", "url": "...", "caption": "Photo: Mattel"}, ...]
}
```

This is preferred over a markdown rendering because:
- The model already sees structure → easier 1:1 mapping enforcement.
- We can inject metadata (`source_name`) so per-source tone calibration (§7) fires correctly.
- Keeps the user message ~500-3000 tokens.

#### Response parsing strategy

```python
def _parse_response(text: str, expected_paragraph_count: int,
                    expected_block_count: int) -> dict:
    """Strip leading/trailing whitespace + optional ```json fences.
    json.loads. Validate keys: title, alts (list of 2-3), subtitle,
    paragraphs (list, len == expected). On any mismatch raise
    ClaudeTranscreationError.

    Belt-and-suspenders: if title doesn't start with one of the 8 emojis,
    apply the legacy emoji-prefix regex from old transcreate_text(is_title=True)
    as post-pass.
    """
```

Strategy: JSON. Markdown fenced JSON (```json ...```) is tolerated and stripped. If the response is plain prose (model ignored the envelope), raise `ClaudeTranscreationError` and let caller fall back to Google for this article.

#### Token budget per call

Per §10:
- Cached system prompt (ux-guidelines + envelope): ~3200 tokens. With prompt caching (5-min TTL), ~$0.30/MTok on hits.
- Per-article user message: ~500-3000 tokens. Average ~1500.
- Output: ~600-3500 tokens. Average ~1500.

Average cost per Haiku 4.5 article: ~$0.01 (per §10). Daily budget at 30 articles: $0.30. Sonnet 4.6 worst-case: $1.50/day.

### 14.4 Outage state machine

#### States

| State | Persisted shape | Meaning |
|---|---|---|
| `no_outage` | (no rows in bot_state OR `outage_started_at IS NULL`) | Healthy. Claude is the engine. |
| `ping_1_sent` | `outage_started_at=t0`, `ping_count=1`, `last_ping_sent_at=t0` | First failure detected. Admin notified. Still trying Claude per slot. |
| `ping_2_sent` | `ping_count=2`, `last_ping_sent_at=t0+1h` | Outage continues for 1h+. Second admin warning. Still on Claude. |
| `google_fallback_active` | `fallback_active=1`, `ping_count=2` (or 3 if a switch-ping is sent) | 2h elapsed; Google is the engine. Periodic health checks. |
| `recovery_pending` | (transient only — no separate persistence; identical to `google_fallback_active` between health-check trigger and result) | Health probe in flight. |

#### Transition table

| Current state | Trigger | Next state | Side effects |
|---|---|---|---|
| `no_outage` | Claude call raises outage error | `ping_1_sent` | Set outage_started_at=now; ping_count=1; send admin ping #1 ("Claude API недоступна, через 1ч переключусь на Google если не восстановится"); for THIS article: try Claude once more (retry) — if fails, fall back to Google for THIS article only. |
| `no_outage` | Claude call succeeds | `no_outage` | nothing. |
| `ping_1_sent` | Claude call raises outage error AND now - outage_started_at >= 1h | `ping_2_sent` | ping_count=2; last_ping_sent_at=now; send admin ping #2 ("Claude всё ещё недоступна, через 1ч переключусь на Google"); fall back to Google for THIS article. |
| `ping_1_sent` | Claude call raises outage error AND now - outage_started_at < 1h | `ping_1_sent` | (no extra ping); fall back to Google for THIS article. |
| `ping_1_sent` | Claude call succeeds | `no_outage` | clear_outage_state(); send admin ping ("Claude API восстановлена"). |
| `ping_2_sent` | Claude call raises outage error AND now - outage_started_at >= 2h | `google_fallback_active` | fallback_active=1; send admin ping #3 ("Переключился на Google"); fall back to Google for THIS article. |
| `ping_2_sent` | Claude call raises outage error AND now - outage_started_at < 2h | `ping_2_sent` | (no extra ping); fall back to Google for THIS article. |
| `ping_2_sent` | Claude call succeeds | `no_outage` | clear_outage_state(); send admin ping ("Claude API восстановлена"). |
| `google_fallback_active` | (next slot or scheduled probe) | `recovery_pending` (in-process) | call health_check(). |
| `recovery_pending` | health_check returns True | `no_outage` | clear_outage_state(); send admin ping ("Claude API восстановлена, возвращаюсь на нормальный режим"). |
| `recovery_pending` | health_check raises | `google_fallback_active` | last_health_check_at=now (rate-limit probes). |
| any | per-article error (BadRequestError) | (state unchanged) | fall back to Google for THIS article only. |

Note: per-article errors do NOT advance the state machine. Only API-level outages do.

#### Storage

`bot_state` table:
```sql
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

Active keys: `outage_started_at`, `last_ping_sent_at`, `ping_count`, `fallback_active`, `last_health_check_at`. Values stored as ISO-8601 strings (timestamps) or `'0'`/`'1'`/`'2'` (counters/flags). Reading a missing key returns Python `None`.

### 14.5 Distributed-publish loop in job()

Pseudocode for the new `job()` function. Replaces the prep+heads-up+overdue model with prep+schedule+publish-loop.

```python
def job():
    """Fetch + schedule + distributed-publish tick.

    Fires at 12:00 MSK daily. Steps:
      1. Init DB; load env (already done at import).
      2. Crash-loop guard: read MAX(published_at) FROM published_articles.
         If now - last_published < MIN_INTERVAL_MINUTES, log + skip publish
         until that gap elapses. Prevents bunched-publishes after a crash-loop.
      3. Fetch all sources (existing SOURCES registry).
      4. Filter (existing).
      5. fetch_full_article + insert_pending for each (existing).
      6. Compute schedule:
            now_msk = datetime.now(zoneinfo.ZoneInfo('Europe/Moscow'))
            n = pending_repo.count_pending()
            slots, carry = compute_publish_slots(n, now_msk)
      7. Send admin ping with the day's plan ("12 articles fetched, 11 scheduled
         today at 13:00, 13:40, ..., 19:40, 1 carries to tomorrow"). Suppress
         on n=0.
      8. Distributed-publish loop:
            for slot_at in slots:
                wait_until(slot_at)        # time.sleep((slot_at - now).total_seconds())
                row = pending_repo.list_pending()[0]   # oldest pending
                if row is None: break       # all published or moved to failed
                try:
                    if outage_state.is_fallback_active():
                        _fallback_publish_via_google(row, via_review=False)
                    else:
                        _fallback_publish(row, via_review=False)
                                         # _fallback_publish internally tries
                                         # Claude; on per-article error, falls
                                         # back to Google for THIS article.
                except OutageError as exc:
                    # State machine kicks in (record_outage_event); we
                    # already fell back to Google for this article.
                    ...
                except Exception as exc:
                    safe = sanitize_error_message(exc)
                    new_count = pending_repo.increment_attempt(row['link'], safe)
                    if new_count >= 3:
                        pending_repo.move_to_failed(row['link'], safe)
            # On startup mid-window: same loop, but slots are computed from
            # `now` not from window_start.
      9. (Optional) carry-over admin ping if carry > 0.
    """
```

Key design points:
- The loop uses `time.sleep()` not `schedule` library between slots — `schedule` is only used for the daily 12:00 trigger.
- A SIGTERM during sleep is interpreted as container restart → on next startup, recompute slots from `now`.
- `_fallback_publish` is renamed/refactored: still handles one row, but now (a) tries Claude first, (b) on per-article error falls back to Google, (c) on outage error raises `ClaudeOutageError` for the loop to handle. The existing `via_review=False` plumbing is preserved (`auto_marker=True` still applied).
- Crash-loop guard at step 2: `SELECT MAX(published_at) FROM published_articles`. If `now - last_published < 40min`, sleep until `last_published + 40min` before the loop starts. Protects against systemd/cron rapid-restart loops.

### 14.6 TZ handling

#### Library API confirmed

`schedule==1.2.1` `Job.at(time_str, tz=None)` accepts:
- `tz: str` — IANA zone name parsed via `pytz.timezone(...)`.
- `tz: pytz.BaseTzInfo` — direct pytz timezone object.
- NOT supported: stdlib `zoneinfo.ZoneInfo` (TypeError). Verified by reading `inspect.getsource(schedule.Job.at)`.

This means option (a) "use stdlib `zoneinfo`" from the prompt is moot for the cron-trigger path: `schedule` requires `pytz`. Two viable approaches remain:

#### Option (a): `pytz` + schedule.at(tz=...)

```python
import pytz
schedule.every().day.at("12:00", tz="Europe/Moscow").do(job)
```

Add `pytz>=2024.1` to requirements.txt. Inside `job()` and `compute_publish_slots`, also use `pytz.timezone('Europe/Moscow')` to keep types consistent. Container TZ irrelevant.

#### Option (b): UTC offset in code

```python
schedule.every().day.at("09:00").do(job)   # 09:00 UTC == 12:00 MSK (standard time)
```

Brittle: MSK is UTC+3 year-round (Russia abolished DST in 2014), but if Russia ever re-introduces DST, this breaks. Skip.

#### Option (c): apscheduler

More control + native zoneinfo support, but adds a heavier dep + we'd lose `schedule`'s simplicity. Reject for this feature.

#### Recommendation: Option (a)

Add `pytz` to `requirements.txt`. Use `pytz.timezone('Europe/Moscow')` end-to-end. Set `TZ=Europe/Moscow` in the server's `.env` for log readability and so any `datetime.now()` (without tz arg) is also MSK. Add a startup assertion:

```python
def main():
    tz_env = os.environ.get('TZ')
    if tz_env != 'Europe/Moscow':
        logger.warning(
            f"TZ env var is {tz_env!r}, expected 'Europe/Moscow'. "
            f"Cron will still fire correctly (explicit pytz), but logs may "
            f"show non-MSK timestamps."
        )
```

### 14.7 Anthropic SDK call patterns

#### Prompt caching

The ux-guidelines.md system prompt is ~3200 tokens. Anthropic prompt-caching break-even is at 2 calls within a 5-minute TTL. In our distributed schedule, slots are 40+ minutes apart — TTL expires between calls. **Verdict: prompt caching does NOT pay off** for our workload (one call per 40+ minutes). Skip caching for now.

Exception: on an initial 12:00 fetch, if the schedule packs 11 slots into 7h, the first 5 slots are 40 min apart — still outside TTL. Caching only helps if we batched all transcreations at fetch time; we deliberately don't (we want to delay translation cost until publish to avoid wasted work on rows the operator might intercept). **Final: no caching.**

#### Streaming vs block

Block. Output is ~1500 tokens; streaming complicates parsing of the JSON envelope (need to buffer until JSON closes). Block lets us call `response.content[0].text` and parse atomically.

#### Retry

Use SDK default `max_retries=2` on the `anthropic.Anthropic()` client. SDK retries on `429` (RateLimitError) + `5xx` + connection errors with exponential backoff. Our outer retry is the state machine (next slot 40 min later). No custom retry layer needed.

#### Error class hierarchy

| Anthropic class | Our classification | Notes |
|---|---|---|
| `anthropic.APIConnectionError` | outage | network unreachable |
| `anthropic.APITimeoutError` | outage | response timeout |
| `anthropic.RateLimitError` (429) | outage | sustained rate-limit = effective outage |
| `anthropic.InternalServerError` (5xx) | outage | server problem |
| `anthropic.AuthenticationError` (401) | outage | misconfigured key — admin must intervene; treat as outage so admin sees ping |
| `anthropic.PermissionDeniedError` (403) | outage | same — admin issue |
| `anthropic.NotFoundError` (404) | outage | model name wrong → admin issue |
| `anthropic.BadRequestError` (400) | per-article | content refused / malformed prompt — fall back to Google for THIS article |
| `anthropic.UnprocessableEntityError` (422) | per-article | rare, usually content issue |
| `anthropic.APIStatusError` (other) | per-article | conservative default — don't escalate to outage on novel codes |
| `ClaudeTranscreationError` (parse failure) | per-article | malformed JSON from model |

Implementation in `is_outage_error` / `is_per_article_error`. Tests in `test_claude_transcreation.py` cover each branch.

### 14.8 SQLite migration safety

#### Idempotency

`pending_articles_repo.init_schema` (line 150-159) uses `CREATE TABLE IF NOT EXISTS`. Adding the `_BOT_STATE_DDL` line follows the same pattern — idempotent. `init_db` in news_bot.py calls `init_schema` on every cron tick (line 1137 — `init_db()` at top of `job()`); the no-op is sub-millisecond.

#### Concurrent access

The codebase uses two writers against `news.db`:
1. `news_bot.py` cron (`pending_articles`, `published_articles`, `failed_articles`, `processed_news`).
2. `hw_review.py` operator CLI (same tables).

Both use short-lived `sqlite3.connect()` per call. SQLite default `journal_mode=delete` + serial-write semantics — `BEGIN IMMEDIATE` blocks concurrent writers (5s default lock timeout, configurable). Our `bot_state` operations are sub-50ms. Risk: low. The `outage_state` writes touch a different table from `pending_articles_repo`, so even within a single SQLite-level lock window, the logical conflict is null.

Recommendation: don't change journal mode. Add `PRAGMA busy_timeout = 5000;` to `_connect()` if not already present (verify in `pending_articles_repo._connect`). If present, no change.

#### Migration test

Update `tests/test_migration.py`:
```python
def test_bot_state_table_exists(self):
    """bot_state table is created by init_db with key/value columns."""
    self.assertIn('bot_state', self._get_tables())
    cols = self._get_columns('bot_state')
    self.assertEqual({c['name'] for c in cols}, {'key', 'value'})
    pk_cols = [c['name'] for c in cols if c['pk']]
    self.assertEqual(pk_cols, ['key'])
```

### 14.9 Implementation task breakdown estimate

12 atomic tasks proposed. Dependencies expressed as task numbers.

| # | Name | Scope | Deps | Skill | Reviewers |
|---|---|---|---|---|---|
| 1 | Add `bot_state` migration | Add `_BOT_STATE_DDL` to `pending_articles_repo`; extend `init_schema`; add migration test | (none) | code-writing | code-reviewing, test-master |
| 2 | Create `outage_state.py` + tests | KV wrapper around bot_state; state-machine helpers | 1 | code-writing | code-reviewing, test-master |
| 3 | Create `compute_publish_slots.py` + tests | Pure scheduling algorithm (12 tests) | (none — parallel with 1) | code-writing | code-reviewing, test-master |
| 4 | Create `claude_transcreation.py` + tests | Anthropic SDK wrapper, mocked tests (8 tests) | (none — parallel) | code-writing, claude-api | code-reviewing, security-auditor |
| 5 | Add `ANTHROPIC_API_KEY` redaction to `_TokenRedactingFilter` + tests | Update `_SECRET_ENV_NAMES`, add `_ANTHROPIC_KEY_RE`, extend `test_no_token_leak_in_logs` | (none — parallel) | code-writing | security-auditor |
| 6 | Refactor `_fallback_publish` for Claude primary + Google fallback | Replace transcreate_text calls with `transcreate_via_claude`; per-article fallback path | 4 | code-writing | code-reviewing, test-master |
| 7 | Refactor `job()` for distributed-publish loop | Replace `schedule.every(12).hours` with `schedule.every().day.at("12:00", tz="Europe/Moscow")`; add publish-loop; add crash-loop guard | 2, 3, 6 | code-writing | code-reviewing |
| 8 | Delete legacy: `_overflow_fast_track`, idle-fallback inline, throttle, env vars | Pure deletion + companion test deletes | 7 | code-writing | code-reviewing |
| 9 | Update `transcreate_text` — strip bureaucratic regex + 4000-char cap | Keep function for translate_text-style fallback only; minimal HW glossary post-pass | 8 | code-writing | code-reviewing |
| 10 | Update integration tests (`test_integration.py`, `test_job_prep_phase.py`) | New patch targets, new assertions on schedule | 7, 8 | code-writing, test-master | code-reviewing |
| 11 | Create `test_distributed_schedule_integration.py` | 4 end-to-end flow tests with mocked anthropic + telegraph + telegram | 7 | code-writing, test-master | code-reviewing |
| 12 | Update deploy: `deploy.sh`, `.github/workflows/deploy.yml`, `.env.example`, `requirements.txt` | Add ux-guidelines.md to FILES, add ANTHROPIC_* secrets, pin anthropic + pytz | (none — but ship LAST) | deploy-pipeline | code-reviewing |

#### Alternative decomposition (cleaner sequencing)

The proposed 12 → consider merging 8 + 9 (both pure deletes) into one task. And split 7 into 7a (cron trigger TZ change) + 7b (publish-loop scaffold) + 7c (per-slot publish + crash-loop guard) for tighter PR review. That's still 12 tasks — net similar burden, better PR-reviewability.

Recommendation: **stick with the 12 above** — boundaries are clean and parallel work is maximized. Tasks 1, 3, 4, 5 are independent and can run in wave 1 (4 parallel). Tasks 2 + 12 are wave 2. Task 6 is wave 3. Tasks 7, 8, 9 are wave 4 (sequential because they all touch news_bot.py). Tasks 10, 11 are wave 5 (parallel).

### 14.10 Risks per implementation phase

| Task # | Risk | Severity | Mitigation |
|---|---|---|---|
| 1 | Schema-migration test must lock new column shape; if ordering of CREATE TABLE statements matters for tests, init_schema callers may break | Low | `_BOT_STATE_DDL` added at end of init_schema, after the existing 3. Test asserts presence not order. |
| 2 | `BEGIN IMMEDIATE` could deadlock with hw_review CLI under heavy concurrent load | Low | hw_review is operator-paced (one txn / few seconds); 5s busy_timeout absorbs contention. Test with simulated concurrent writer. |
| 3 | Pure algorithm — easy. Risk: TZ-naive datetime input slips through to caller | Low | Function asserts `now.tzinfo is not None`, raises ValueError. |
| 4 | Mocked anthropic tests may drift from real SDK API; SDK's exception classes change between versions | Medium | Pin `anthropic>=0.45.0,<1.0`. Use Context7 MCP at impl-time to verify exception class names for the pinned version. |
| 5 | Regex for `sk-ant-` keys may not match all key shapes Anthropic uses (e.g. test keys vs prod keys) | Low | Liberal regex `sk-ant-[A-Za-z0-9_-]{20,}`. Tests cover both shapes. |
| 6 | `_fallback_publish` is heavy, called from multiple sites; refactor risk of breaking idempotency invariants (Decision 9 of mrw) | **High** | Keep step 2-5 (Telegraph + persist + Telegram + move) untouched; only step 1 (transcreate) changes. Add a dedicated `test_fallback_publish_claude_path` + `test_fallback_publish_google_fallback_path` to lock both branches. |
| 7 | news_bot.py refactor is the largest single touch; risk of breaking 100+ existing tests via `schedule.every(12).hours` removal alone (operators may still patch it) | **High — dependency-heavy** | Sequence after Tasks 2, 3, 6 are complete. Run full test suite incrementally — fix red, commit, advance. Don't combine with task 8 in same PR (separate the refactor from the deletes). |
| 8 | Pure deletes — but: deletes affect `test_overflow.py`, `test_idle_fallback.py`, `test_fallback_throttle.py`. If task 7 left any reference to the deleted symbols, the tests break before task 8 runs | Medium | Run task 8 in a single PR with the deletes; tests deleted same commit as functions. CI green = sequencing correct. |
| 9 | `transcreate_text` is also used by `post_latest_news.py` (legacy CLI) and 5 tests in `test_translation.py` (which target `translate_text`, not `transcreate_text` — but worth re-checking). Stripping it might break post_latest_news | Low | post_latest_news.py is undeployed (see §1, deploy.sh FILES list). Keep `translate_text` unchanged (it's a separate function). Verify no `transcreate_text` tests rely on the regex output. |
| 10 | Integration tests reference `news_bot.transcreate_text` via `@patch`. Renaming the symbol breaks patches | Medium | Either keep `transcreate_text` as a thin shim → `transcreate_via_claude` (one-line, deprecation note), OR update the patches in the same PR as the rename. Recommend latter. |
| 11 | New integration tests need to mock anthropic, telegraph, telegram, time.sleep, and schedule simultaneously — heavy mock setup | Medium | Use a `@pytest.fixture` (or unittest setUp) that builds a "fake distributed schedule env" once and reuses across tests. |
| 12 | Deploy step: ux-guidelines.md is at a path with subdirs (`.claude/skills/...`). `scp file1 file2 file3 host:dest/` flattens by default — the file lands at `dest/ux-guidelines.md`, not preserving the `.claude/...` subdir | **High** | Either: (a) tar the .claude/skills/project-knowledge/references subtree on the deployer, scp the tarball, untar on the server (preserves subdirs); OR (b) hardcode `_PROMPT_PATH` to the flat filename `<DEPLOY_PATH>/ux-guidelines.md` (operator simplicity wins). Recommendation: (b) — add a `_FLAT_PROMPT_PATH` fallback in `claude_transcreation._load_prompt`, try the subdir path first then the flat path. Document in deployment.md. |

#### Dependency-heavy tasks (need careful sequencing)

- **Task 7** (job() refactor) depends on 2, 3, 6 — must wait. Schedule it as wave 4.
- **Task 6** (_fallback_publish refactor) depends on 4 — wait for the wrapper to exist.
- **Task 11** (integration tests) depends on 7 — wait for the new flow.
- **Task 12** (deploy) ships LAST, after all tests green and all code merged.

#### Wave plan

- Wave 1 (parallel): 1, 3, 4, 5
- Wave 2 (parallel): 2, 12-prep (just `requirements.txt` + `.env.example` updates)
- Wave 3: 6
- Wave 4 (sequential, same touch point): 7, then 8, then 9
- Wave 5 (parallel): 10, 11
- Wave 6: 12 (final deploy YAML/script + ux-guidelines.md shipping)

End of section 14.
