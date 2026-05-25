# Code research: t-hunted-pt-source

Investigation grounded in current `dev` HEAD (commit `07fc6c9`). All file:line references are absolute against repo root `/Users/alex/MyFiles/ai-projects/my-hw/my-hw-bot/`.

---

## 0. Headline findings (cycle 2 input)

1. **`source_name` already flows into the LLM prompt.** `_llm_common._build_user_message` (`_llm_common.py:119-131`) serialises `source_name` into the user payload alongside title/subtitle/paragraphs. Adding a 4th `### 🟤 t-hunted` block to `ux-guidelines.md § Per-source style notes` will be visible to every engine without any code change. No new wiring needed.
2. **`ux-guidelines.md` line 22 hardcodes "входящий **английский** текст".** This is the only place the prompt asserts English input. Widening to "входящий текст (английский или португальский)" is the minimal-risk wording.
3. **`_is_mostly_russian` (`_llm_common.py:134-153`) runs on OUTPUT only.** PT input does not trigger it. No risk of false-positive rejection from the 30% Cyrillic floor.
4. **Telegram channel hashtag will default to `#t-hunted` if no special-case is added.** `_source_hashtag` (`news_bot.py:799-808`) does `netloc.split('.')[-2]`. For `t-hunted.blogspot.com`: parts=`['t-hunted','blogspot','com']`, `-2 → 'blogspot'`, so the hashtag becomes `#blogspot` — **not** `#t-hunted`. This is the only place a naive netloc fallback breaks for this source. See Section 1.B for impact.
5. **`feeds.json` is hard-capped at 5 entries** by `news_bot.load_feeds` (`news_bot.py:196`). Current file has 3 entries. Adding the t-hunted RSS URL is well under the cap.
6. **Boilerplate filter is monolithic, not language-tagged.** All RU patterns currently apply unconditionally to every source's paragraph list. PT patterns added now will also apply to EN/RU streams. Length-bounded at 120 chars — same gate that protects RU patterns from EN false positives will protect PT patterns. See Section 4.
7. **Article dispatcher (`news_bot.py:1405-1444`) is a substring-on-domain if/elif chain.** Adding a `'blogspot.com' in domain` branch is mechanical; SSRF guard inside the source module is the security gate (see lamley `_ALLOWED_HOSTS` pattern at `lamley_source.py:213-226`).
8. **No language detection exists in the codebase.** Confirmed via `grep -rn "langdetect|detect.*lang|portuguese"`. The LLM determines input language entirely from content. PT input is handled implicitly once the system prompt is widened.
9. **Mattel deploy slot is dead code but kept.** `mattel_news_source.py` is still in deploy.sh/deploy.yml/deploy_test.yml `FILES` lists despite being commented out of `SOURCES` (`news_bot.py:1619`). New `t_hunted_source.py` must be added to **all three** FILES lists or the cron will `ImportError` on first tick.

---

## 1. Source-name resolution and dispatch glue

### 1.A `_resolve_source_name` + `NETLOC_TO_SOURCE`

**Location:** `news_bot.py:823-849`

```python
NETLOC_TO_SOURCE = {
    'www.autoevolution.com':         'autoevolution',
    'autoevolution.com':             'autoevolution',
    'lamleygroup.com':               'lamley',
    'www.lamleygroup.com':           'lamley',
    'corporate.mattel.com':          'mattel',
    'orangetrackdiecast.com':        'orangetrack',
    'www.orangetrackdiecast.com':    'orangetrack',
}

def _resolve_source_name(link):
    try:
        netloc = urlparse(link or '').netloc.lower()
    except Exception:
        return 'other'
    return NETLOC_TO_SOURCE.get(netloc, 'other')
```

**Required edits:**
- Add `'t-hunted.blogspot.com': 't-hunted'` to the map.
- Decide if `www.t-hunted.blogspot.com` should also map (Blogger doesn't use www, but defensive symmetry with other entries suggests yes).

**Impact on `SOURCE_LABEL` / `SOURCE_EMOJI` (`news_bot.py:857-868`):**
Both dicts need a `'t-hunted'` key. SOURCE_EMOJI currently uses Unicode circles (orange/purple/green/blue). Brown circle `\U0001F7E4` (🟤) is the only remaining "warm" circle that doesn't clash. Consumer is `hw_review.py:198` only — admin-ping plan-of-day pings do NOT enumerate per-source counts (`admin_alerts.alert_plan_of_day` `admin_alerts.py:126-138` is source-agnostic), so the SOURCE_LABEL/SOURCE_EMOJI extension is cosmetic for archived `hw_review` only. Safe to add for completeness; not strictly required for runtime.

### 1.B `_source_hashtag` → Telegram channel post

**Location:** `news_bot.py:799-808`

```python
def _source_hashtag(source_url):
    netloc = urlparse(source_url).netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    parts = netloc.split('.')
    label = parts[-2] if len(parts) >= 2 else netloc
    return f"#{label}"
```

**Bug-for-this-feature:** For `t-hunted.blogspot.com`, `parts = ['t-hunted','blogspot','com']`, `parts[-2] = 'blogspot'`. The channel post would render `#blogspot #news` — wrong attribution. **Two options:**

1. **Special-case** in `_source_hashtag`: detect blogspot subdomain pattern and return `parts[-3]` (the subdomain), or hard-code `'t-hunted.blogspot.com' → '#t-hunted'`. Simplest.
2. **Refactor** to use the `NETLOC_TO_SOURCE` map. But this would change existing hashtags: lamley posts currently emit `#lamleygroup` (the TLD-stripped form, per the comment at `news_bot.py:819-822`), and the `news_bot.py:822` comment explicitly calls out keeping `#lamleygroup` not `#lamley` for "continuity with the existing channel format". So a registry-driven refactor would break the existing contract.

**Recommended:** Option 1 — small targeted special case for blogspot subdomain extraction, OR a tiny per-source override map keyed by `source_name`.

**Test invariants on hashtag format** (`tests/test_telegram.py:19-39, 47-100`):
- `test_autoevolution` (line 21): `'#autoevolution'` — TLD-stripped.
- `test_mattel_corporate` (line 26): `'#mattel'` — TLD-stripped.
- `test_lamley` (line 32): `'#lamleygroup'` — confirmed TLD-stripped (not the source_name `'lamley'`).
- `test_strips_www_prefix` (line 38): confirms `www.` stripping is the only normalisation.

The locked format is `'#{brand} #news'` (line 65) — pipeline must produce exactly `'#t-hunted #news'`. The hyphen in `t-hunted` is acceptable in Telegram hashtags (Telegram allows `[a-zA-Z0-9_]`, NOT hyphen — **caveat**: this might render the hashtag broken). Verify Telegram hashtag rules before locking the label. If hyphen is rejected, alternatives: `#thunted`, `#tHunted`, or `#t_hunted`.

### 1.C Article dispatcher: `fetch_full_article`

**Location:** `news_bot.py:1405-1444`

```python
def fetch_full_article(entry):
    link = entry.get('link') or ''
    domain = urlparse(link).netloc.lower()
    try:
        if 'orangetrackdiecast.com' in domain:
            ...
        if 'corporate.mattel.com' in domain:
            return fetch_mattel_article(link, notifier=send_admin_notification)
        if 'lamleygroup.com' in domain:
            return lamley_source.fetch_lamley_article(link, notifier=send_admin_notification)
        if 'autoevolution.com' in domain:
            return autoevolution_source.fetch_autoevolution_article(entry)
    except Exception as exc:
        logger.exception(f"Source fetcher failed for {link}: {exc}")
        return None
    logger.warning(f"No source handler for domain: {domain}")
    return None
```

**Contract:**
- On unmatched netloc → returns `None`. Caller (`news_bot.py:1764`) drops the entry: `if not article or not article.get('paragraphs'): ... continue`. No exception raised. The entry never enters `pending_articles`.
- Returns `{title, subtitle, paragraphs, images[, blocks]}` or `None` on fetch failure.
- New branch must use `'blogspot.com' in domain` (catches both `t-hunted.blogspot.com` and potential future blogspot sources) — but combine with strict SSRF allowlist inside the new parser to prevent `lamleygroup.com.blogspot.com.attacker.example` shaped attacks.

**Important:** The substring check is permissive on purpose to handle www/subdomain variance; security relies on the per-source `_is_allowed_*_url` allowlist inside each parser. New source must follow this convention.

---

## 2. RSS fetcher chain and `source_name` flow into LLM

### 2.A `_fetch_rss_entries`

**Location:** `news_bot.py:1458-1502`

For each feed URL in `load_feeds()`:
- calls `fetch_rss(url)` → `feedparser.parse(url).entries` (`news_bot.py:603-612`),
- normalises each entry into a plain dict `{link, title, published, summary, feed_url}`,
- stamps `source_name = _resolve_source_name(link or url)`,
- WARNs on `'other'`.

**Path for t-hunted:** Add `t-hunted.blogspot.com` to `NETLOC_TO_SOURCE` → RSS entries get `source_name='t-hunted'` automatically. No additional fetcher function (`_fetch_t_hunted_entries`) needed — Blogger RSS goes through the universal RSS path.

**However:** As you noted, the t-hunted RSS has no `<content:encoded>`. `_fetch_rss_entries` does not populate `paragraphs` / `images` / `blocks` — those come later from `fetch_full_article` (`news_bot.py:1764`) via dispatcher → new `fetch_t_hunted_article` parser. This matches the lamley path exactly. Confirmed by the `_resolve_source_name` test `tests/test_sources_registry.py:113-137`.

### 2.B Flow into LLM

**`_build_user_message` (`_llm_common.py:119-131`):**

```python
def _build_user_message(article: dict) -> str:
    payload = {
        "source_name": article.get("source_name"),
        "title": article.get("title"),
        "subtitle": article.get("subtitle"),
        "paragraphs": article.get("paragraphs") or [],
    }
    return json.dumps(payload, ensure_ascii=False)
```

This is shared by `claude_transcreation.py:384`, `openrouter_transcreation.py`, `gemini_transcreation.py`, `openai_transcreation.py`. The LLM sees `"source_name": "t-hunted"` and can branch on the Per-source style notes block in `ux-guidelines.md`.

**Path from staging → LLM:** `news_bot.py:1782` stages the row with `source_name`. `pending_articles_repo` persists it. `_fallback_publish` later reads the row and calls `llm_transcreation.transcreate(article)` which forwards the dict containing `source_name`. The chain is complete; no edits required in any of the 4 `*_transcreation.py` files.

### 2.C Output guard `_is_mostly_russian`

**Location:** `_llm_common.py:134-153, 242-246`

Counts Cyrillic letters in the LLM's RU paragraph response. Threshold 30%. Input language is irrelevant. PT input → RU output goes through the same gate. **Risk:** Portuguese share some Latin diacritics that look "Cyrillic-adjacent" only visually — no false positives, `0x0400-0x04FF` Unicode range is strict.

### 2.D No language detection in the codebase

Confirmed via `grep -rn "langdetect|detect.*lang|portuguese" --include="*.py"`. The only "language" string in production code is `claude_transcreation.py:149` ("idiomatic Russian") describing the **output** target, not input parsing. PT input is handled implicitly by the LLM once the prompt is widened (Section 5).

---

## 3. Lamley as blueprint for the blogger scraper

**File:** `lamley_source.py` (380 lines total).

### 3.A Imports (`lamley_source.py:17-27`)

```python
import logging, threading, time
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from boilerplate_filter import filter_boilerplate
import admin_alerts
```

Plus optional `curl_cffi` for Cloudflare bypass (`lamley_source.py:36-41`).

**For Blogger:** `curl_cffi` is **not needed** — Blogger doesn't sit behind Cloudflare bot management. Plain `requests` will work. Drop the whole `_CFFI_AVAILABLE` block. Drop the `BROWSER_HEADERS` complexity too — a basic User-Agent suffices for Blogger.

### 3.B Public signature (`lamley_source.py:229-233`)

```python
def fetch_lamley_article(
    link: str,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
```

Returns `{'title', 'subtitle', 'paragraphs', 'images'}` or `None`. New `fetch_t_hunted_article` should mirror this signature 1:1 for the dispatcher to call it identically. **No `blocks` field needed** — Blogger posts are simple HTML, not structurally rich like autoevolution.

### 3.C WAF/cooldown/blacklist — SKIPPABLE

Lines `lamley_source.py:80-330` (most of the file) handle 429 throttling, cool-downs, per-URL blacklists. **All skippable for Blogger.** Blogger is hosted on Google infrastructure with no observable rate limiting on individual blogs at our volume (~1-3 requests/day). Drop the entire throttling apparatus.

If a paranoid baseline is desired: keep just the `_throttle_wait` with a tiny 1-2s interval. Even that may be overkill.

### 3.D HTML extraction (`lamley_source.py:328-372`)

```python
soup = BeautifulSoup(response.text, "html.parser")
for junk in soup(["script", "style", "noscript"]):
    junk.decompose()

title_tag = soup.find("h1", class_="entry-title") or soup.find("h1")
title = title_tag.get_text(" ", strip=True) if title_tag else ""

body = (
    soup.find("div", class_="entry-content")
    or soup.find("article")
)
...
paragraphs: List[str] = []
for tag in body.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
    text = tag.get_text(" ", strip=True)
    if text and text != title:
        paragraphs.append(text)

paragraphs = filter_boilerplate(paragraphs)

subtitle = paragraphs[0] if paragraphs else ""
paragraphs = paragraphs[1:]

images: List[str] = []
seen_bases = set()
for img in body.find_all("img"):
    src = img.get("src") or ""
    if not src.startswith("http"):
        continue
    base = src.split("?", 1)[0]
    if base in seen_bases:
        continue
    seen_bases.add(base)
    images.append(src)
    if len(images) >= IMAGE_LIMIT:
        break
```

**For Blogger, change two selectors:**
- Title: `<h3 class="post-title entry-title">` (canonical Blogger v3 template) or `<h1>` fallback.
- Body: `<div class="post-body entry-content">` is Blogger's canonical wrapper. Both classes exist together; either matches. Fallback to `<div class="entry-content">` for theme variants and `<article>` as last resort.

Everything else (paragraph walk, subtitle-from-first-paragraph, image dedup-by-base) ports unchanged.

### 3.E Image dedup behaviour

Lamley's `?resize=1024` / `?resize=500` produces two URLs for the same image; dedup by `src.split("?", 1)[0]` keeps only one. Blogger uses `=s1600` / `=s640` size suffixes attached to the path itself (e.g. `https://blogger.googleusercontent.com/img/.../s1600/photo.jpg`), not query strings. **The lamley dedup will not collapse Blogger size variants** — needs adjustment if dupes show up in practice. Acceptable to start with the lamley logic verbatim and tighten if needed.

### 3.F filter_boilerplate call site

Line `lamley_source.py:352`: `paragraphs = filter_boilerplate(paragraphs)` — applied BEFORE picking subtitle (lifted to line 357). The same order is mandatory for t-hunted: drops the "Compartilhar no" / "Marcadores:" footer before it becomes a subtitle.

### 3.G Test coverage `tests/test_lamley_source.py` (351 lines)

Classes:
- `TestFetchLamleyArticle` (line 71): basic parse, HTTP error, missing body, image limit. **Replicate all 4 for t-hunted.**
- `TestRateLimitHandling` (line 118): 429 + throttle. **Skip entirely for t-hunted (no WAF).**
- `TestWAFProtection` (line 205): cool-down/blacklist. **Skip entirely.**
- `TestHostAllowlist` (line 299): SSRF guard. **Replicate** for t-hunted with `t-hunted.blogspot.com` as the only allowed host.

Representative signature pattern (line 71-87):
```python
def test_parses_title_subtitle_paragraphs_images(self):
    session = MagicMock()
    session.get.return_value = _make_response(text=SAMPLE_HTML)
    out = lamley_source.fetch_lamley_article("http://lamleygroup.com/x", session=session)
    assert out["title"] == "Sample Hot Wheels Post"
    ...
```

Test should inject a `session` so the parser bypasses real HTTP. `_make_response` helper at line 37 is reusable verbatim.

### 3.H Host allowlist pattern (`lamley_source.py:209-226`)

```python
_ALLOWED_HOSTS = ('lamleygroup.com', 'www.lamleygroup.com')

def _is_allowed_lamley_url(link: str) -> bool:
    try:
        parsed = urlparse(link)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    return host in _ALLOWED_HOSTS
```

**For t-hunted:** Exact match on `('t-hunted.blogspot.com',)`. Do NOT use a glob like `*.blogspot.com` — that opens the door to any blogspot blog and defeats the SSRF protection (the dispatcher's `'blogspot.com' in domain` substring check is permissive on purpose; the parser allowlist is the hard gate). If more blogspot sources are added later, extend `_ALLOWED_HOSTS` then.

---

## 4. boilerplate_filter extension

**File:** `boilerplate_filter.py` (229 lines).

### 4.A Structure (`boilerplate_filter.py:37-158`)

- `_MAX_BOILERPLATE_LEN = 120` — length bound on the whole paragraph. Lines longer than 120 chars are NEVER filtered, even if they start with a boilerplate marker. This is the safety against "real prose that incidentally mentions Share on Facebook".
- `_BOILERPLATE_PATTERNS` is a single flat list of `re.compile`'d regexes. No language tags — all patterns are applied to every paragraph.
- English patterns first (lines 51-130), Russian patterns at the tail (lines 132-157) explicitly tagged as **"defence in depth in case translated text ever reaches us"**.

### 4.B Where PT patterns should land

A `# Portuguese — defence in depth for t-hunted.blogspot.com` block at the end of `_BOILERPLATE_PATTERNS`, paralleling the Russian block at line 132. No code structure change required. Patterns to add (length-anchored at `^`, ReDoS-safe):

- `^compartilhar\s+(no|em|via|por)\s+(facebook|twitter|x|whatsapp|telegram|email)\b` — covers "Compartilhar no Facebook" etc.
- `^marcadores\s*:` — Blogger's labels footer "Marcadores: foo, bar".
- `^postado\s+por\b` / `^postagem` — Blogger byline ("Postado por Author").
- `^enviar\s+por\s+email\b` — Blogger "Email this" PT label.
- `^postagens\s+mais\s+(antigas|recentes)$` — older/newer post nav.
- `^assinar\s*:` — "Assinar: Postagens (Atom)" subscribe label.
- `^leia\s+mais$` / `^ler\s+mais$` — read more standalone.
- `^postar\s+(um\s+)?coment[áa]rio$` — "Postar um comentário" (post-a-comment label).
- `^nenhum\s+coment[áa]rio$` / `^coment[áa]rios?$` — comment thread headers.
- `^p[aá]gina\s+(inicial|principal)$` — "Página inicial" home link.

Add a parallel safety-net entry in the test file: `tests/test_boilerplate_filter.py` `TestIsBoilerplatePositive` has explicit `test_english_patterns_filtered` (line 71) and `test_russian_patterns_filtered` (line 94) parametrised classes — add `test_portuguese_patterns_filtered` mirroring those.

### 4.C Length bound suitability

The PT boilerplate strings listed above range from ~10 to ~30 chars — well under the 120-char floor. No tuning of `_MAX_BOILERPLATE_LEN` needed. The bound is the same protection RU patterns rely on; same calibration applies.

### 4.D Side-effect risk: PT patterns applied to EN/RU streams

The 2026-05-08 Russian-patterns precedent (`boilerplate_filter.py:143-157`) confirms the project's policy is "add patterns globally, rely on length bound + word boundaries for false-positive protection". Most PT trigger words (`compartilhar`, `marcadores`, `postado`, etc.) do not collide with EN or RU vocabulary. **Sole concern:** `^ler\s+mais$` — `'mais'` is a real Russian/EN substring but the full string `'Ler mais'` won't appear as a standalone <=120-char paragraph in EN/RU prose. Acceptable risk per the existing precedent.

### 4.E `filter_blocks` (`boilerplate_filter.py:185-229`)

Used by autoevolution's structural blocks. **Not relevant for t-hunted** — Blogger posts are flat paragraphs, no `blocks` field returned. No edits to `filter_blocks` needed.

---

## 5. ux-guidelines.md edits

**File:** `.claude/skills/project-knowledge/references/ux-guidelines.md` (133 lines).

### 5.A Widen input-language assertion

Line 22 currently:
> Ты — ведущий редактор и локализатор контента для популярного Telegram-канала. Твоя единственная задача: преобразовывать входящий **английский** текст в высококлассный русскоязычный контент.

Edit to widen, e.g. "входящий **англоязычный или португалоязычный** текст". Keep the rest of the prompt verbatim — the prompt blockquote is the LLM's role and the file's invariant per the comment at line 132.

### 5.B Per-source note block

Section `## Per-source style notes` (lines 74-103) has three blocks: 🟠 Autoevolution, 🔵 Lamley, 🟡 Mattel. Add a 4th `### 🟤 t-hunted` block in the same shape:

- **Voice:** [hobbyist Brazilian Portuguese blog on Hot Wheels Treasure Hunts/Super Treasure Hunts — to be characterised by operator after a few sample articles]
- **Tone dial:** lean into "друг по хобби" — informal collector talk
- **Length:** [TBD from samples]
- **Structure quirks:** Blogger template artefacts (Marcadores, Compartilhar) handled at parser layer
- **Good/bad title examples:** TBD

### 5.C PT-EN-RU glossary block

User-spec calls for a glossary block. Suggested home: a new `## Glossary — PT/EN/RU` section between `## Per-source style notes` and `## Red flags to self-check before stage` (line 104). Keep entries terse — collector jargon that the LLM must NOT calque:

| PT | EN | RU (preferred) |
|----|----|----|
| Caça | Hunt | Хант / охота |
| Super Caça / Super-T | Super Treasure Hunt (Super-T) | Super-T |
| Caça ao Tesouro | Treasure Hunt | Treasure Hunt (T-Hunt) |
| Linha principal | Mainline | Mainline |
| Edição limitada | Limited edition | Лимитированная серия |
| Coleção | Collection | Коллекция / серия |
| Premiê / Premium | Premium | Premium |
| Modelo | Casting | Кастинг / модель |
| Pintura | Paint / deco | Окрас |
| Decalque | Tampo / decal | Декаль |

Operator should review/correct this glossary from actual t-hunted articles — it's a starting baseline.

### 5.D Deploy: file is bundled

Line 9 of ux-guidelines.md asserts "Deploy bundle ships it to the server" — see `deploy.sh:58`, `deploy.yml:144`, `deploy_test.yml:115`. The file is **already** in all three FILES lists. No deploy-list edit needed for the prompt change.

---

## 6. Deploy plumbing

**Three FILES lists must be updated in lockstep** (per the INVARIANT comments at `deploy.sh:23-24` and `deploy.yml:109-113`):

1. `deploy.sh:37-59` — add `"t_hunted_source.py"` to the FILES array.
2. `.github/workflows/deploy.yml:123-145` — same addition.
3. `.github/workflows/deploy_test.yml:94-116` — same addition.

The INVARIANT is documented: *"any new first-party import added to news_bot.py MUST be mirrored into FILES here AND in .github/workflows/deploy.yml. Otherwise the server will hit ImportError on the next cron tick with no CI signal."* Same warning applies to `deploy_test.yml` (mirrors deploy.yml byte-for-byte).

**Suggested file naming:** `t_hunted_source.py` (underscore, matching `mattel_news_source.py` / `lamley_source.py` / `orangetrack_source.py` / `autoevolution_source.py` convention). The hyphen in `t-hunted` doesn't translate to a valid Python module name.

---

## 7. RSS-side risks for Blogger feeds

### 7.A `feeds.json` cap

**Location:** `news_bot.py:184-206`

```python
def load_feeds():
    try:
        with open('feeds.json', 'r') as f:
            data = json.load(f)
    ...
    for item in data[:5]:  # limit to first 5
```

Hard cap is 5. Current `feeds.json` has 3 entries (autoevolution Hot Wheels feed, autoevolution Hot Wheels News feed, lamley Hot Wheels category feed). Adding t-hunted = 4 entries. **No edit to the cap needed.**

If the cap ever needs to grow (orangetrack uses a separate non-feeds.json path, so adding a 5th general RSS feed would still fit), edit `news_bot.py:196` only.

### 7.B No language-tagging per feed

`feeds.json` is a flat list of URL strings. No `{"url": ..., "lang": "pt"}` shape. **Not needed** for this feature — `_resolve_source_name` infers the source from netloc, and the LLM auto-detects language from content. If you ever want explicit per-feed language tags, that's a separate refactor (would also touch the `load_feeds` JSON validator).

### 7.C feedparser on Blogger Atom/RSS

`feedparser` is the universal parser (`news_bot.py:603-612`). Blogger publishes both Atom (`/feeds/posts/default`) and RSS-aliased (`/feeds/posts/default?alt=rss`) variants. **Use the `?alt=rss` URL** — `feedparser` handles both, but the existing `_fetch_rss_entries` extracts `entry.summary`, not `entry.content`. Blogger Atom puts the body in `entry.content[0].value`; the RSS alias maps it into `entry.summary` instead. Confirmed via the user-spec note that t-hunted RSS has no `<content:encoded>` — fine, the body comes from `fetch_t_hunted_article` HTML scrape, not the feed.

For your test smoke: `feedparser.parse('https://t-hunted.blogspot.com/feeds/posts/default?alt=rss').entries[0].keys()` will surface what feedparser populates. Expect `title`, `link`, `published`, `summary` at minimum — exactly the fields `_fetch_rss_entries` selects (`news_bot.py:1488-1495`). No code change needed in the RSS path.

### 7.D Bozo flag

`fetch_rss` (`news_bot.py:606-608`) logs `feed.bozo_exception` as a warning but proceeds. Blogger occasionally serves slightly malformed XML; the bot will WARN but continue parsing — same behaviour as autoevolution's feed has shown in production. No action needed.

---

## 8. Existing tests — t-hunted parallel needs

| Existing test | Needs t-hunted parallel? | Notes |
|----|----|----|
| `tests/test_lamley_source.py` (351 lines) | **Yes — new file `test_t_hunted_source.py`** | Mirror `TestFetchLamleyArticle` + `TestHostAllowlist`. Skip WAF/throttle classes. |
| `tests/test_sources_registry.py` (351 lines) | **Yes — extend** | `TestNetlocToSource.test_has_exactly_the_five_keys` (line 45) is currently locked to the 7-element set; add `'t-hunted.blogspot.com'`. `test_values_are_only_the_three_source_names` (line 56) is locked to `{'autoevolution','lamley','mattel','orangetrack'}`; add `'t-hunted'`. `TestResolveSourceName` (line 77) gets a new t-hunted case. `TestSourcesRegistry.test_sources_registry_shape` (line 324) currently locks `SOURCES = [_fetch_rss_entries, _fetch_orangetrack_entries]` — **does NOT need updating** if t-hunted uses the universal RSS path (it should). |
| `tests/test_telegram.py` (187 lines) | **Yes — extend** | Add a `test_t_hunted_teaser_appends_news_tag` paralleling `test_lamley_teaser_appends_news_tag` (line 89). The hashtag-shape test will be the key check (see Section 1.B caveat — depends on hashtag spelling). |
| `tests/test_boilerplate_filter.py` | **Yes — extend** | New `test_portuguese_patterns_filtered` parametrised class. |
| `tests/test_config_loader.py` (107 lines) | No | `feeds.json` cap is 5; new total is 4. Existing tests cover up-to-5 and truncate-on-more-than-5 already. |
| `tests/test_feed_iteration.py` | Possibly extend | Check if it covers all sources; mostly a smoke for `_fetch_rss_entries` shape. |
| `tests/test_translation.py` / `test_*_transcreation.py` | No | LLM-prompt edits go through `ux-guidelines.md`; transcreation tests use `_load_prompt` stubs (`tests/test_gemini_transcreation.py:89` etc.). No new LLM tests required unless you want explicit "PT input → RU output" coverage (recommended but optional). |
| `tests/test_admin_alerts.py` | No (only if you add new t-hunted-specific alerts à la lamley E025-E028) | Lamley has 4 dedicated alerts (`admin_alerts.py:271-317`). For Blogger, decide: reuse `alert_source_fetch_failed` generic alert (admin_alerts.py:41) vs. add t-hunted-specific alerts. Generic is simpler. |
| `tests/test_relevance_filter.py` | Maybe | `_is_hot_wheels_relevant` (`news_bot.py:614-635`) currently filters by EN keyword `'hot wheels'` in title. PT-language t-hunted titles will use `'hot wheels'` brand verbatim (it's a proper noun and untranslated in Brazilian Portuguese collector blogs — confirmed by spec), so this should pass as-is. Worth a smoke test against actual t-hunted titles. |

---

## 9. Data layer

**No schema changes.** `pending_articles` table accepts `source_name` as a free-text column (`pending_articles_repo.py` does no validation against an allowlist). News.db schema unchanged per user-spec.

**Pending row shape (`news_bot.py:1780-1790`):**
```python
row = {
    'link': link,
    'source_name': entry.get('source_name') or _resolve_source_name(link),
    'feed_url': entry.get('feed_url'),
    'title': article.get('title') or entry.get('title') or '',
    'subtitle': article.get('subtitle') or '',
    'paragraphs': article.get('paragraphs') or [],
    'images': article.get('images') or [],
    'blocks': article.get('blocks'),
    'pub_date': entry.get('published') or entry.get('pub_date') or '',
}
```

`source_name='t-hunted'` flows transparently through this row, into SQLite, out to `_fallback_publish`, into `transcreate_via_*`, into `_build_user_message`, and into the LLM payload.

---

## 10. Integration points — module imports

`news_bot.py:34-37` imports the source modules:
```python
from mattel_news_source import fetch_mattel_news, fetch_mattel_article
import autoevolution_source
import lamley_source
import orangetrack_source
```

Add: `import t_hunted_source` (assuming snake_case module name) at line 38.

Article dispatcher branch (Section 1.C) calls `t_hunted_source.fetch_t_hunted_article(link, notifier=send_admin_notification)`.

---

## 11. Constraints & infrastructure

- **Python deps:** `requests`, `bs4` (already in `requirements.txt`). No new deps. `curl_cffi` not needed for Blogger.
- **MAX_DAILY_POSTS = 4** (from PR #10, news_bot.py constant). Hard cap stays. With 4 sources active, on a busy day the cap may bind more often — operator should monitor `carry_over` in plan-of-day pings.
- **MIN_INTERVAL_MINUTES = 40** between publishes. With 4 sources potentially producing 4+ articles/day, the publish-window scheduler (`compute_publish_slots`) is already saturated; adding t-hunted increases queue pressure but does not change the publish cadence (cap is on output, not input).
- **No new env vars.** Anthropic/OpenAI/Gemini/OpenRouter keys already cover all four sources via the shared LLM dispatcher.
- **CI:** No pre-commit hook surface to update. `gitleaks` runs at commit time; t-hunted source has no secrets.
- **systemd restart** in deploy.yml (line 248-250) will pick up the new module on next deploy. No additional restart wiring.

---

## 12. Potential problems

1. **Hashtag rendering for `#t-hunted`.** Telegram's hashtag character set excludes hyphens (verified by Telegram docs: hashtags are `\w+` only). `'#t-hunted #news'` will be parsed as `'#t'` followed by `'-hunted'` text. **Mitigation:** choose hashtag label `'#thunted'`, `'#tHunted'`, or `'#t_hunted'`. Decision belongs in user-spec.
2. **Bare-subdomain hashtag collision risk.** If `_source_hashtag` is left untouched, t-hunted posts will tag `#blogspot` — diluting attribution and clashing with any future blogspot-hosted source. **Mitigation:** explicit special case in `_source_hashtag` OR new `SOURCE_HASHTAG = {...}` override map.
3. **Boilerplate cross-language false positives.** PT pattern `^marcadores\s*:` shouldn't collide with EN/RU. But the global pattern list is a coupling hazard. **Mitigation:** length bound + word boundaries + the operator-tested precedent of the RU defence-in-depth block.
4. **Per-paragraph cap (`_PARAGRAPH_MAX_CHARS = 4000`).** Some Blogger posts are long single-paragraph essays without `<br>` breaks — risk of truncation at 4000 chars. `_truncate_paragraphs` (`_llm_common.py:303-318`) logs a warning but does not error. **Mitigation:** monitor first 10 t-hunted publishes; if truncation is common, raise the cap OR split paragraphs at `<br>` boundaries inside the parser.
5. **Telegraph rendering on PT-style image embeds.** Blogger inlines images via `<a href="...s1600/x.jpg"><img src="...s640/x.jpg" /></a>` wrappers. Image dedup-by-base (`lamley_source.py:366`) won't collapse `s1600` vs `s640` because they're in the path. **Mitigation:** in the new parser, dedup by stripping `=s\d+(-c)?` size suffix OR by `os.path.basename(url).split('=')[0]`. Or accept dupe images and rely on `IMAGE_LIMIT=10` to bound damage.
6. **Sibling-brand filter.** `_is_hot_wheels_relevant` (`news_bot.py:614-635`) skips entries whose title mentions Matchbox without Hot Wheels. Brazilian collector blogs occasionally cover Matchbox sister releases; filter behaviour is identical for EN/PT (substring match on `'hot wheels'` and `'matchbox'` — both untranslated brand names). No change needed.
7. **Checklist filter.** `_CHECKLIST_TITLE_RE = r'\bcheck[\s-]?list\b'` (`news_bot.py:642`) is EN-only. PT collector posts might use `'lista'` / `'checklist'` (English loanword common) — the EN regex catches the loanword case. If PT-native `'lista'` posts need filtering, that's a separate enhancement; not in scope per user-spec.
8. **`source_name = 'other'` warning noise.** If `t-hunted.blogspot.com` is forgotten in `NETLOC_TO_SOURCE`, every t-hunted entry logs a WARNING (`news_bot.py:1499-1500`). Visible smoke signal — acceptable.

---

## 13. Open questions / Risks summary

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R1 | Hashtag spelling — `#t-hunted` invalid in Telegram | High | Decide spelling in user-spec; add override |
| R2 | `_source_hashtag` returns `#blogspot` for t-hunted | High | Special-case the helper OR add override map |
| R3 | Blogger image URLs not dedup'd by lamley logic | Med | Adjust dedup or accept |
| R4 | Long PT essay → 4000-char truncation | Low | Monitor; raise cap if needed |
| R5 | PT boilerplate cross-applied to EN/RU streams | Low | Length-bounded; same as RU precedent |
| R6 | Bozo-flagged Blogger XML | Low | feedparser handles it; warns only |
| R7 | New module forgotten in one of 3 FILES lists | Med | INVARIANT comment + CI ImportError on test smoke |
| R8 | Glossary PT-EN-RU is guesswork until samples seen | Med | Iterate after first 5-10 publishes |

---

## 14. Files-to-touch summary (cycle 2 anchor)

**New files:**
- `t_hunted_source.py` — new parser, ~150 lines (lamley_source.py minus WAF/throttle)
- `tests/test_t_hunted_source.py` — ~80 lines (TestFetch + TestHostAllowlist)

**Modified files:**
- `news_bot.py:34-37` — add import
- `news_bot.py:799-808` — `_source_hashtag` override for blogspot subdomain
- `news_bot.py:823-831` — `NETLOC_TO_SOURCE` entry for `t-hunted.blogspot.com`
- `news_bot.py:857-868` — `SOURCE_EMOJI` + `SOURCE_LABEL` (optional, archived-path-only consumer)
- `news_bot.py:1414-1439` — `fetch_full_article` new branch for `'blogspot.com' in domain`
- `feeds.json` — append t-hunted RSS URL (4 entries total, under cap of 5)
- `boilerplate_filter.py:51-158` — append `# Portuguese — defence in depth` block in `_BOILERPLATE_PATTERNS`
- `.claude/skills/project-knowledge/references/ux-guidelines.md:22` — widen input-language assertion
- `.claude/skills/project-knowledge/references/ux-guidelines.md:74-103` — add `### 🟤 t-hunted` block + PT-EN-RU glossary section
- `tests/test_sources_registry.py:45-58, 77-103` — extend NETLOC_TO_SOURCE assertions and `_resolve_source_name` cases
- `tests/test_telegram.py` — add `test_t_hunted_teaser_appends_news_tag` (hashtag-format-dependent)
- `tests/test_boilerplate_filter.py:28-95` — extend `TestIsBoilerplatePositive` with PT cases
- `deploy.sh:37-59` — add `t_hunted_source.py` to FILES
- `.github/workflows/deploy.yml:123-145` — same
- `.github/workflows/deploy_test.yml:94-116` — same

**No-change files (verified):**
- `_llm_common.py` — `source_name` already plumbed
- `claude_transcreation.py`, `openrouter_transcreation.py`, `gemini_transcreation.py`, `openai_transcreation.py` — all read prompt via `_load_prompt`, no per-source code
- `pending_articles_repo.py` — schema agnostic to source_name values
- `compute_publish_slots.py` — source-blind
- `telegraph_publisher.py` — source-blind
- `admin_alerts.py` — generic `alert_source_fetch_failed` covers t-hunted fetch failures (decision point: do we want t-hunted-specific E029-E032 alerts? lamley has 4; bare-minimum is the generic one)

---

## 15. Patterns to reuse (decision-grade)

| Pattern | Source | Reuse-vs-new |
|----|----|----|
| `fetch_*_article(link, session, notifier) -> dict | None` signature | `lamley_source.py:229-233` | **Reuse 1:1** |
| `_is_allowed_*_url(link)` SSRF allowlist | `lamley_source.py:209-226` | **Reuse 1:1**, new constant |
| `BeautifulSoup → soup.find(body wrapper) → walk p/li/h2-4/blockquote` | `lamley_source.py:328-358` | **Reuse**, change two selector strings |
| Image dedup-by-base + IMAGE_LIMIT | `lamley_source.py:360-372` | **Reuse**, possibly tighten for Blogger size suffixes |
| Subtitle-from-first-paragraph | `lamley_source.py:357-358` | **Reuse 1:1** |
| `filter_boilerplate` call BEFORE subtitle lift | `lamley_source.py:352` | **Reuse 1:1** |
| RU defence-in-depth boilerplate block | `boilerplate_filter.py:132-157` | **New PT block in same shape** |
| Per-source style notes block in ux-guidelines.md | lines 78-102 | **New 🟤 t-hunted block in same shape** |
| `NETLOC_TO_SOURCE` map | `news_bot.py:823-831` | **Extend with one entry** |
| Test class structure `TestFetch* + TestHostAllowlist` | `tests/test_lamley_source.py:71, 299` | **Mirror** in new test file |
| WAF/cool-down/throttle apparatus | `lamley_source.py:80-330` | **Do NOT reuse** — Blogger doesn't need it |
| `curl_cffi` Chrome impersonation | `lamley_source.py:36-41` | **Do NOT reuse** |
| `BROWSER_HEADERS` full set | `lamley_source.py:55-75` | **Reduce** to plain User-Agent |

---

## 16. Quick smoke commands (for parser author)

```bash
# 1. Inspect RSS payload
python3 -c "import feedparser; p = feedparser.parse('https://t-hunted.blogspot.com/feeds/posts/default?alt=rss'); print(len(p.entries), p.entries[0].keys())"

# 2. Smoke the parser against one real article
python3 -c "import t_hunted_source; r = t_hunted_source.fetch_t_hunted_article('<real URL>'); print(r['title']); print(len(r['paragraphs']))"

# 3. Smoke the boilerplate filter against a known PT footer
python3 -c "from boilerplate_filter import is_boilerplate; print(is_boilerplate('Compartilhar no Facebook'))"

# 4. Smoke the dispatcher (via news_bot)
python3 -c "from news_bot import _resolve_source_name; print(_resolve_source_name('https://t-hunted.blogspot.com/2026/05/post.html'))"

# 5. Confirm hashtag rendering
python3 -c "from news_bot import _source_hashtag; print(_source_hashtag('https://t-hunted.blogspot.com/2026/05/post.html'))"
# expect '#blogspot' on unpatched code — confirms R2 is real
```

---

End of research. Total file:line refs: ~70. Cycle 2 author can start at Section 14 (Files-to-touch) and work outward.

---

## 17. Tech-Spec Deepening

## Updated: 2026-05-25

Implementation-level deepening for the tech-spec phase. Anchored on `dev` HEAD `f7bf56c` (post-user-spec-approval). Line numbers below are verified against the current tree, not §14's anchor.

User-spec deltas since cycle-2 research:
- **Hashtag locked as `#thunted`** (AC6, §Decisions L106). No more open question on spelling.
- **R1/R2 mitigation locked at AC6 level** — exact technique deferred to this tech-spec (§F below).
- **PT-EN-RU glossary ≥10 entries** required as structural element (AC7). §E below proposes 14.
- **Quality abort path explicit:** if 7-day RU quality < baseline, source stays off prod — not a code rollback, just a feed.json removal (low blast radius).

---

### A. Exact patching locations (line-precise as of `f7bf56c`)

#### A.1 `news_bot.py`

| Patch | Current line(s) | Action |
|---|---|---|
| Import `t_hunted_source` | After `news_bot.py:37` (`import orangetrack_source`) | Insert new line `import t_hunted_source` at line 38 (before `import telegraph_publisher`). Alphabetical ordering with sibling source modules. |
| `_source_hashtag` override | `news_bot.py:799-808` | See §F for committed technique — adds a 1-line lookup before the netloc-split fallback. |
| `NETLOC_TO_SOURCE` entry | `news_bot.py:823-831` (map literal) | Insert `'t-hunted.blogspot.com': 't-hunted',` between `'corporate.mattel.com':` (line 828) and `'orangetrackdiecast.com':` (line 829) — keeping the existing visual grouping by source. No `www.` variant (Blogger doesn't serve `www.t-hunted.blogspot.com`; manually visiting it 301s to the bare form). |
| `SOURCE_EMOJI` + `SOURCE_LABEL` | `news_bot.py:857-868` | Add `'t-hunted': '\U0001F7E4',  # brown circle` to `SOURCE_EMOJI` and `'t-hunted': 't-hunted',` to `SOURCE_LABEL`. Optional (hw_review.py:198 archived path only), but cheap. |
| `fetch_full_article` branch | `news_bot.py:1416-1439` (if/elif chain) | Insert a new branch **after** the `'autoevolution.com' in domain` check at line 1438-1439, **before** the trailing `except Exception` at 1440. Branch body: `if 'blogspot.com' in domain: return t_hunted_source.fetch_t_hunted_article(link, notifier=send_admin_notification)`. Condition `'blogspot.com' in domain` is intentionally broad — the SSRF gate lives inside the parser's `_ALLOWED_HOSTS` (§B). |

#### A.2 `feeds.json` (current 3 entries)

```json
[
    "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml",
    "https://www.autoevolution.com/rss/tag-Hot+Wheels+News.xml",
    "https://lamleygroup.com/category/hot-wheels/feed/"
]
```

Append `"https://t-hunted.blogspot.com/feeds/posts/default?alt=rss"` as the 4th entry (under the cap of 5 at `news_bot.py:196`). Recommended position: line 4 (after lamley, before closing `]`). Convention is "group by source", not alphabetical.

#### A.3 `boilerplate_filter.py`

- `_MAX_BOILERPLATE_LEN = 120` confirmed at `boilerplate_filter.py:37`. **No tuning needed** — all PT patterns proposed in §E.3 fit under 50 chars.
- PT block insertion point: **after** the RU CTA pattern that ends at `boilerplate_filter.py:157` (`re.compile(r'^\s*(найти|купить)\b...')`), **before** the closing `]` at line 158. Add a header comment `# ------------------------------------------------------------------\n# Portuguese — defence in depth for t-hunted.blogspot.com` paralleling the RU header at line 131-132.

#### A.4 `ux-guidelines.md`

| Edit | Current line | Action |
|---|---|---|
| Widen input-language assertion | Line 22 | Replace `входящий **английский** текст` with `входящий текст (английский или португальский)`. Verbatim-blockquote invariant (file:132) preserved — only the inner adjective changes. |
| Add 🟤 t-hunted per-source block | Line 102 (end of Mattel block) | Insert new `### 🟤 t-hunted` block (skeleton in §D below) between Mattel block end (line 102) and `## Red flags...` heading (line 104). |
| Add PT-EN-RU glossary section | Between line 103 (Mattel block trailing blank) and line 104 (`## Red flags...` heading) | New `## Glossary — PT/EN/RU` H2 section (table from §E below) inserted **after** the 🟤 t-hunted block and **before** Red flags. Order matters: glossary should follow per-source notes, not interleave. |

#### A.5 `admin_alerts.py` — new builder functions

Current last alert code: **E030** (`alert_orangetrack_summary_header`, line 325-326). Next available: **E031** onwards.

Three new builders to add, paralleling the lamley E025-E028 block (lines 269-317):

| Code | Function name | Insertion position | Purpose |
|---|---|---|---|
| E031 | `alert_t_hunted_host_rejected(link: str) -> str` | After E028 (`alert_lamley_no_body`, line 317), **before** the E030 separator-comment block at line 320 | SSRF allowlist failure |
| E032 | `alert_t_hunted_fetch_error(link: str, error: str) -> str` | Immediately after E031 | HTTP/network error |
| E033 | `alert_t_hunted_no_body(link: str) -> str` | Immediately after E032 | Parser couldn't find `<div class="post-body">` |

Skip dedicated rate-limit / cool-down alerts (E029-style not needed — Blogger has no WAF; §B drops the lamley throttle apparatus). Skip a `_too_large` alert — Blogger posts are small (<200KB observed). If size becomes an issue in production, add E034 then.

Insertion ordering rationale: keep E030 (Orangetrack) **last** in the file — it's the aggregator-pattern outlier, structurally different from per-error alerts.

#### A.6 Deploy FILES arrays

Convention check: deploy.sh:37-59, deploy.yml:123-145, deploy_test.yml:94-116 are **not alphabetical** — they're ordered roughly by lifecycle role (entry-point first, then sources, then publishers, then config). Current source-module cluster is lines 39-42 (autoevolution / mattel / lamley / orangetrack — itself not alphabetical, looks like "addition order").

Insert `"t_hunted_source.py"` **after** `"orangetrack_source.py"` in all three lists. Exact lines:
- `deploy.sh:42` → new line at 43
- `.github/workflows/deploy.yml:128` → new line at 129
- `.github/workflows/deploy_test.yml:99` → new line at 100

**INVARIANT REMINDER:** all three files must change in the same commit. CI doesn't catch a missed file; production cron tick will `ImportError`. The deploy.sh comment at lines 22-24 explicitly documents this.

---

### B. Parser implementation blueprint (`t_hunted_source.py`)

Target size: **130-170 LOC** (lamley_source.py is 379, but WAF/throttle/cool-down apparatus drops ~210 lines).

**Module structure:**

```
# Header docstring (10 lines)
# Imports (8 lines)
# Constants (8 lines)
# Helper: _is_allowed_t_hunted_url (12 lines)
# Public: fetch_t_hunted_article (90-120 lines)
```

**Imports (line-by-line):**
```python
import logging
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from boilerplate_filter import filter_boilerplate
import admin_alerts
```

Notable omissions vs lamley_source.py:17-41:
- No `threading`, no `time` — no throttle state.
- No `curl_cffi` / `_CFFI_AVAILABLE` — Blogger does not Cloudflare-fingerprint.
- No `BROWSER_HEADERS` block — plain `User-Agent` string suffices.

**Constants:**

```python
logger = logging.getLogger(__name__)

_ALLOWED_HOSTS = ('t-hunted.blogspot.com',)  # exact-match, no glob
_TIMEOUT_SECONDS = 15
_IMAGE_LIMIT = 10  # matches lamley_source.IMAGE_LIMIT
_MAX_BYTES = 2_000_000  # 2MB hard cap on response body — Blogger posts are tiny
_USER_AGENT = 'Mozilla/5.0 (compatible; HotWheelsNewsBot/1.0)'
```

**Helper:**

```python
def _is_allowed_t_hunted_url(link: str) -> bool:
    """Return True iff *link* targets the t-hunted Blogger host over http(s).

    Defence against SSRF: the dispatcher's `'blogspot.com' in domain`
    substring check (news_bot.py:fetch_full_article) is permissive — this
    function is the hard gate inside the parser. No glob like `*.blogspot.com`:
    that would allow any Blogger blog and defeat the allowlist's purpose.
    """
    try:
        parsed = urlparse(link)
    except (ValueError, AttributeError):
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    return host in _ALLOWED_HOSTS
```

Verbatim from `lamley_source.py:213-226`, with two changes: `_ALLOWED_HOSTS` constant value, function name suffix.

**Public function:**

```python
def fetch_t_hunted_article(
    link: str,
    session: Optional[requests.Session] = None,
    notifier: Optional[Callable[[str], None]] = None,
) -> Optional[Dict]:
    """Fetch and parse a single t-hunted Blogger article.

    Returns a dict with keys ``title`` (str), ``subtitle`` (str — first
    paragraph after boilerplate filter), ``paragraphs`` (List[str]),
    ``images`` (List[str], max IMAGE_LIMIT). Returns ``None`` on any
    fetch / parse failure; admin alert fired via ``notifier`` callback
    if provided.
    """
```

Identical signature to `lamley_source.fetch_lamley_article` (lamley_source.py:229-233) so the dispatcher branch in §A.1 calls it transparently.

**HTTP fetch shape (no curl_cffi):**

```python
if not _is_allowed_t_hunted_url(link):
    logger.warning(f"t-hunted: host not allowed: {link}")
    if notifier:
        notifier(admin_alerts.alert_t_hunted_host_rejected(link))
    return None

session = session or requests.Session()
try:
    response = session.get(
        link,
        timeout=_TIMEOUT_SECONDS,
        headers={'User-Agent': _USER_AGENT},
    )
    response.raise_for_status()
except requests.RequestException as exc:
    logger.error(f"t-hunted: fetch failed for {link}: {exc}")
    if notifier:
        notifier(admin_alerts.alert_t_hunted_fetch_error(link, str(exc)))
    return None

if len(response.content) > _MAX_BYTES:
    logger.warning(f"t-hunted: response too large ({len(response.content)}) for {link}")
    return None
```

**HTML parse (BeautifulSoup):**

```python
soup = BeautifulSoup(response.text, "html.parser")
for junk in soup(["script", "style", "noscript"]):
    junk.decompose()

# Blogger canonical title selector: <h3 class="post-title entry-title">.
# Some themes use <h1>; some legacy posts use <h2>. Walk the fallback chain.
title_tag = (
    soup.find("h3", class_="post-title")
    or soup.find("h1", class_="entry-title")
    or soup.find("h1")
)
title = title_tag.get_text(" ", strip=True) if title_tag else ""

# Blogger canonical body wrapper: <div class="post-body entry-content">.
body = (
    soup.find("div", class_="post-body")
    or soup.find("div", class_="entry-content")
    or soup.find("article")
)
if body is None:
    logger.warning(f"t-hunted: no body wrapper found for {link}")
    if notifier:
        notifier(admin_alerts.alert_t_hunted_no_body(link))
    return None

paragraphs: List[str] = []
for tag in body.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
    text = tag.get_text(" ", strip=True)
    if text and text != title:
        paragraphs.append(text)

# CRITICAL ORDER: boilerplate filter BEFORE subtitle lift.
# Otherwise "Marcadores: ..." footer becomes the subtitle when title-only
# posts (rare but real) hit the parser.
paragraphs = filter_boilerplate(paragraphs)

subtitle = paragraphs[0] if paragraphs else ""
paragraphs = paragraphs[1:]
```

**Image extraction (Blogger-aware dedup):**

```python
images: List[str] = []
seen_bases = set()
for img in body.find_all("img"):
    src = img.get("src") or ""
    if not src.startswith("http"):
        continue
    # Blogger size-variant collapse: strip the "=sNNN" or "=sNNN-c" suffix
    # so https://.../=s1600/x.jpg and https://.../=s640/x.jpg dedup to the
    # same base. Falls back to lamley's "?param" stripping for safety.
    base = re.sub(r'=s\d+(-c)?$', '', src.split("?", 1)[0])
    if base in seen_bases:
        continue
    seen_bases.add(base)
    images.append(src)
    if len(images) >= _IMAGE_LIMIT:
        break
```

Requires `import re` at top. Adjustment from cycle-2 §3.E.

**Return:**

```python
return {
    "title": title,
    "subtitle": subtitle,
    "paragraphs": paragraphs,
    "images": images,
}
```

**No `blocks` key.** Blogger posts are flat — no structural-block path through the LLM (autoevolution exclusive).

**Return shape contract:**
- `title: str` — empty string if not found (never `None`).
- `subtitle: str` — empty string if no paragraphs (never `None`).
- `paragraphs: List[str]` — possibly empty list.
- `images: List[str]` — possibly empty list.
- The whole dict is `None` on host-rejection, fetch failure, missing body. Caller (`news_bot.py:1764`) drops on `not article or not article.get('paragraphs')`.

---

### C. Test plan: concrete test names

#### C.1 `tests/test_t_hunted_source.py` (new file, ~120 LOC)

Mirror `tests/test_lamley_source.py` structure minus WAF/throttle classes. Reuse `_make_response` helper from `test_lamley_source.py:37` verbatim or copy into a `conftest.py`-style local helper.

```python
SAMPLE_HTML = """<html><body>
<h3 class="post-title entry-title">Caça ao tesouro Pop Culture 2026</h3>
<div class="post-body entry-content">
  <p>Mais um lançamento que vai agitar a galera dos colecionadores.</p>
  <p>Os modelos chegam em packs de seis.</p>
  <img src="https://blogger.googleusercontent.com/img/abc/=s1600/photo1.jpg" />
  <img src="https://blogger.googleusercontent.com/img/abc/=s640/photo1.jpg" />
  <img src="https://blogger.googleusercontent.com/img/def/=s1600/photo2.jpg" />
  <p>Compartilhar no Facebook</p>
  <p>Marcadores: Pop Culture, 2026</p>
</div>
</body></html>"""

class TestFetchTHuntedArticle:
    def test_parses_title_subtitle_paragraphs_images(self):
    def test_http_error_returns_none_and_notifies(self):
    def test_missing_body_returns_none_and_notifies(self):
    def test_image_limit_applied(self):
    def test_boilerplate_filter_strips_pt_footer(self):
    def test_blogger_size_variants_deduplicated(self):
    def test_request_timeout_returns_none(self):

class TestHostAllowlist:
    def test_exact_t_hunted_host_allowed(self):
    def test_attacker_dot_blogspot_dot_com_rejected(self):
    def test_unrelated_blogspot_host_rejected(self):   # e.g. otherblog.blogspot.com
    def test_non_http_scheme_rejected(self):           # file:// ftp:// gopher://
    def test_fetch_returns_none_and_pings_when_host_not_allowed(self):
```

Total: **12 test methods**, mirroring lamley's TestFetchLamleyArticle (4) + TestHostAllowlist (6) = 10 + 2 PT/Blogger-specific cases.

**Skip entirely:** `TestRateLimitHandling` (5 methods), `TestWAFProtection` (7 methods). No retry/cool-down apparatus in t_hunted_source.py.

#### C.2 `tests/test_sources_registry.py` — extensions

Update locked-set tests:

| Test method | Current line | Edit |
|---|---|---|
| `test_has_exactly_the_five_keys` | 45 | Rename to `test_has_exactly_the_eight_keys` (current=7 + new t-hunted=8); update set literal to include `'t-hunted.blogspot.com'`. |
| `test_values_are_only_the_three_source_names` | 56 | Rename to `..._the_five_source_names`; update value set to `{'autoevolution','lamley','mattel','orangetrack','t-hunted'}`. |
| `test_resolve_source_name_known_netlocs` | 80 | Add assertion `_resolve_source_name('https://t-hunted.blogspot.com/2026/05/post.html') == 't-hunted'`. |

**New test (add to `TestResolveSourceName`):**
- `test_t_hunted_netloc_resolves_to_t_hunted` — explicit single-case test mirroring `test_lamley_both_netlocs` (line 65).

`TestSourcesRegistry.test_sources_registry_shape` (line 324) does **not** need editing — t-hunted goes through the universal `_fetch_rss_entries` path, no new entry in `SOURCES` list.

#### C.3 `tests/test_telegram.py` — extensions

Add two tests:

| New test | Position | Assertion |
|---|---|---|
| `test_t_hunted_hashtag` | After `test_lamley` (line 32, in `TestSourceHashtag`) | `_source_hashtag('https://t-hunted.blogspot.com/2026/05/post.html') == '#thunted'` (locks the §F technique). |
| `test_t_hunted_teaser_appends_news_tag` | After `test_lamley_teaser_appends_news_tag` (line 89, in `TestSendTelegraphTeaser`) | End-to-end teaser-render assert: posted text contains `'#thunted #news'` exactly. |

Both tests guard AC6 from regression.

#### C.4 `tests/test_boilerplate_filter.py` — extensions

Add new parametrised test in existing `TestIsBoilerplatePositive` class (or new class for cleanliness):

```python
class TestPortuguesePatterns:
    @pytest.mark.parametrize("text", [
        "Compartilhar no Facebook",
        "Compartilhar no Twitter",
        "Compartilhar no WhatsApp",
        "Marcadores: Pop Culture, 2026",
        "Postado por Admin",
        "Postagem mais recente",
        "Postagens mais antigas",
        "Enviar por email",
        "Assinar: Postagens (Atom)",
        "Ler mais",
        "Postar um comentário",
        "Nenhum comentário",
        "Página inicial",
    ])
    def test_portuguese_patterns_filtered(self, text):
        assert is_boilerplate(text) is True
```

Plus one negative test to confirm cross-language safety:
- `test_pt_pattern_does_not_match_long_en_prose` — assert `is_boilerplate('I shared with friends about the latest Marcadores series')` is `False` (length > 120 OR no `^marcadores\s*:` boundary).

#### C.5 `tests/test_admin_alerts.py` — extensions

Three new builder tests, paralleling lamley E025-E028 tests (lines 183-205):

| Test name | Position | Asserts |
|---|---|---|
| `test_e031_t_hunted_host_rejected` | After `test_e028_lamley_no_body` (line 202-206) | Output contains `[E031]`, `🟡`, `'t-hunted'`, the input link. |
| `test_e032_t_hunted_fetch_error` | After E031 | Contains `[E032]`, error string, link. |
| `test_e033_t_hunted_no_body` | After E032 | Contains `[E033]`, link, hint about Blogger DOM change. |

`test_all_alerts_have_unique_codes` (line 216) will automatically validate uniqueness — no edit needed beyond adding the three builders to whatever list it iterates.

#### C.6 Integration smoke `pytest -k integration_t_hunted`

User-spec §Test plan row 2 names this exact marker. **Recommendation:** **new file** `tests/test_integration_t_hunted.py` rather than extending `test_distributed_schedule_integration.py` — the distributed-schedule tests are scenario-locked (3-article happy path, outage recovery, restart, etc.) and don't model per-source feeds.

Single test in the new file:

```python
class TestIntegrationTHunted:
    def test_t_hunted_article_flows_through_full_pipeline(self, monkeypatch):
        """integration_t_hunted — RSS → parser → boilerplate → source_name='t-hunted'
        → LLM payload → pending_articles row. No real HTTP; mocks at session level."""
```

Assertions:
1. `_resolve_source_name` stamps `'t-hunted'` on the entry.
2. `fetch_full_article` dispatches to `t_hunted_source.fetch_t_hunted_article` (via patch of dispatcher).
3. Returned `article` dict has paragraphs without `Compartilhar`/`Marcadores` strings.
4. `pending_articles_repo.insert_pending` is called with `source_name='t-hunted'`.
5. Hashtag rendering: `_source_hashtag` returns `'#thunted'` for the entry's URL.
6. (Optional) The mocked LLM `_build_user_message` receives `source_name='t-hunted'` in the JSON payload.

Smoke also covers the cross-language interference check (AC2): run an `'autoevolution.com'` entry through the same `monkeypatch`'d session in the same test (or paired test) and assert both succeed independently.

#### C.7 Summary

| File | Status | New / changed methods |
|---|---|---|
| `tests/test_t_hunted_source.py` | **NEW** | 12 |
| `tests/test_sources_registry.py` | extend | 3 changed + 1 new |
| `tests/test_telegram.py` | extend | 2 new |
| `tests/test_boilerplate_filter.py` | extend | 1 parametrized (13 cases) + 1 negative |
| `tests/test_admin_alerts.py` | extend | 3 new |
| `tests/test_integration_t_hunted.py` | **NEW** | 1 (matching `-k integration_t_hunted`) |

Total new test surface: ~32 cases. Expected runtime overhead: <1s (all mocked).

---

### D. Per-source style note skeleton for ux-guidelines.md

Drop verbatim into `.claude/skills/project-knowledge/references/ux-guidelines.md` between line 102 (end of 🟡 Mattel block) and line 104 (`## Red flags...` heading):

```markdown
### 🟤 t-hunted

- **Voice:** независимый бразильский блог про Hot Wheels — коллекционерская community-журналистика. Автор — фанат, не журналист, не пресс-служба. [TBD operator после первых 5-10 публикаций — точная характеристика регистра, является ли блог one-author или несколько голосов]
- **Tone dial:** «друг по хобби» — слегка ближе к autoevolution-баровому регистру, чем к mattel-пресс-релизу. Allow informal collector vocabulary. Используй PT-EN-RU глоссарий ниже — НЕ калькировать «caça» → «охота» (правильно «хант»), НЕ калькировать «Super Caça» → «Супер-охота» (правильно «Super-T»).
- **Length:** [TBD operator] — большинство постов короткие, до 5-10 параграфов; иногда длинные deep-dive обзоры новых линеек.
- **Structure quirks:** Blogger-шаблонные артефакты (Compartilhar, Marcadores, Postar comentário) уже отрезаны парсером — в LLM payload они НЕ попадают. Если что-то Blogger-ное всё-таки прорвалось — это сигнал к расширению `boilerplate_filter.py` PT-блока.
- **Good/bad title examples:** [TBD operator — добавить после первых 5-10 публикаций]
```

The `[TBD operator]` markers are intentional — user-spec AC7 commits to **structural presence**, not prose content. Operator refines after 5-10 real articles ship.

---

### E. PT-EN-RU glossary baseline (14 entries)

Drop verbatim into `ux-guidelines.md` as a new `## Glossary — PT/EN/RU` H2 section between 🟤 t-hunted block and `## Red flags`. Operator-tunable; `[VERIFY operator]` flags entries where Brazilian collector usage is uncertain.

```markdown
## Glossary — PT/EN/RU

Hot Wheels collector jargon: переводы канонические для канала. LLM использует
эту таблицу для PT→RU транскреации t-hunted статей, для EN→RU других источников
— как референс по согласованности терминов.

| PT | EN | RU (preferred) | Notes |
|---|---|---|---|
| Caça | (Treasure) Hunt | Хант | НЕ «охота» — устоявшийся коллекционерский сленг |
| Super Caça | Super Treasure Hunt | Super-T (Супер-хант) | НЕ «Супер-охота» |
| Caça ao Tesouro | Treasure Hunt | T-Hunt | Полная форма |
| Linha principal | Mainline | Mainline | Не переводить — кастинговая категория |
| Linha premium | Premium line | Premium | Не переводить |
| Edição limitada | Limited edition | Лимитка / лимитированная серия | |
| Coleção | Collection / series | Серия / коллекция | По контексту |
| Modelo | Casting | Кастинг | Не «модель» (заводит в путаницу с «model car») |
| Pintura | Paint / deco | Окрас / расцветка | |
| Decalque | Tampo / decal | Тампо / декаль | «Тампо» — заводская печать; «декаль» — отдельная наклейка [VERIFY operator] |
| Roda | Wheel (variant) | Колёса / диски | Указывать тип: RR (Real Riders), 5SP, etc. |
| Lançamento | Release / drop | Релиз / релиз новой серии | |
| Carrinho | Diecast car (lit. "little car") | Машинка / даикаст | «Carrinho» — общий collectible-сленг, не уменьшительное [VERIFY operator] |
| Série | Series (e.g. Pop Culture, Boulevard) | Серия | Заглавный регистр у названия серии: «серия Pop Culture» |

Operator: после первых 5-10 публикаций пересмотри `[VERIFY operator]` пункты против
реальных t-hunted постов и поправь предпочитаемый RU-перевод там, где LLM
систематически промахивается.
```

14 entries — exceeds AC7's "≥10". Two `[VERIFY operator]` flags on entries where Brazilian-specific usage is uncertain (`decalque` ambiguity between tampo print vs sticker; `carrinho` register).

---

### F. Hashtag override mechanism — committed technique

**Decision: Option (b) — per-source override map.**

**Code shape:**

Add a new module-level constant in `news_bot.py` immediately after `NETLOC_TO_SOURCE` (line 831), before `_resolve_source_name`:

```python
# Per-source override for the channel hashtag (Decision: t-hunted-pt-source).
# Defaults: `_source_hashtag` returns the TLD-stripped netloc (e.g.
# `autoevolution.com` → `#autoevolution`). This map handles outliers where
# the default would produce a wrong tag — namely Blogger-hosted sources
# whose subdomain is the brand, but the TLD-strip would lift `'blogspot'`.
# Add an entry here when adding a source whose netloc's `parts[-2]` is NOT
# the brand. Telegram hashtags accept only [a-zA-Z0-9_], so dashes are
# stripped at definition time (not by Telegram's parser).
SOURCE_HASHTAG_OVERRIDE = {
    't-hunted.blogspot.com': '#thunted',
}
```

Then modify `_source_hashtag` (`news_bot.py:799-808`) to check the override before falling through to TLD-strip logic:

```python
def _source_hashtag(source_url):
    """Return a Telegram hashtag for the source: `#{brand}` from the URL's
    netloc, stripping `www.` and the TLD. Example: `corporate.mattel.com`
    → `#mattel`, `autoevolution.com` → `#autoevolution`.

    For sources whose default netloc-strip produces a wrong tag (e.g.
    `t-hunted.blogspot.com` would yield `#blogspot`), consult
    `SOURCE_HASHTAG_OVERRIDE` first.
    """
    netloc = urlparse(source_url).netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    override = SOURCE_HASHTAG_OVERRIDE.get(netloc)
    if override is not None:
        return override
    parts = netloc.split('.')
    label = parts[-2] if len(parts) >= 2 else netloc
    return f"#{label}"
```

**Reasoning (why (b) over (a) — special-case in helper):**

1. **Extensibility.** Adding `b-side.blogspot.com` later or any other Blogger-hosted source is a one-line dict edit, not a re-edit of `_source_hashtag` logic.
2. **Locality of decision.** The decision "this source uses an unusual hashtag" lives in one literal next to `NETLOC_TO_SOURCE` — easy to find when onboarding a new source.
3. **Test surface.** A single test asserts `SOURCE_HASHTAG_OVERRIDE['t-hunted.blogspot.com'] == '#thunted'` (literal-data test) plus the existing `_source_hashtag('...')` round-trip — both small.
4. **Reversibility.** If we later refactor to a master `SOURCE_REGISTRY` (single source of truth merging `NETLOC_TO_SOURCE` + `SOURCE_EMOJI` + `SOURCE_HASHTAG`), the dict literal is trivially migratable.
5. **No regression to existing sources.** Lamley still emits `#lamleygroup`, autoevolution still emits `#autoevolution`, mattel still emits `#mattel` — none enter the override map. The `news_bot.py:819-822` comment about continuity with channel format is preserved untouched.

**Why NOT option (a) — special-case for blogspot subdomain extraction:**

A `if netloc.endswith('.blogspot.com'): return f'#{parts[0]}'` would work today but:
- Couples the helper to a specific TLD shape.
- Doesn't handle the `t-hunted` → `thunted` dash-strip (Telegram hashtag rule).
- Doesn't generalise — if `something.wordpress.com` joins later, we add another endswith branch.

The dict-override is strictly more general for the same LOC budget.

---

### G. Wave structure recommendation

12 implementation tasks grouped into 3 implementation waves + audit + final. All tasks use **`code-writing`** skill unless flagged otherwise.

#### Wave 1 — Foundation (parallel)

Independent modules. Both can be written in parallel by separate agents.

| Task | Description | Reviewers | Files to modify | Files to read |
|---|---|---|---|---|
| **T1: parser module `t_hunted_source.py`** | Create new parser following §B blueprint. Strict SSRF allowlist on `('t-hunted.blogspot.com',)`, BS4 selectors for Blogger DOM, boilerplate filter call before subtitle lift, Blogger-aware image dedup, plain `requests` (no curl_cffi/throttle). Return shape matches `fetch_lamley_article` 1:1. | `code-reviewer` + `security-auditor` (SSRF + URL parsing) + `test-reviewer` | `t_hunted_source.py` (NEW) | `lamley_source.py`, `boilerplate_filter.py`, `admin_alerts.py`, code-research.md §B |
| **T2: parser tests `tests/test_t_hunted_source.py`** | 12 test methods per §C.1: TestFetchTHuntedArticle (7) + TestHostAllowlist (5). Mock at `session.get` level. Reuse `_make_response` pattern from `test_lamley_source.py:37`. | `test-reviewer` + `code-reviewer` | `tests/test_t_hunted_source.py` (NEW) | `tests/test_lamley_source.py`, T1's `t_hunted_source.py` |
| **T3: admin alerts E031-E033** | Add 3 builder functions to `admin_alerts.py` between E028 (line 317) and E030 (line 320). Pattern matches lamley E025-E028 styling. | `code-reviewer` | `admin_alerts.py` | `admin_alerts.py:271-317` (lamley pattern) |
| **T4: admin alerts tests** | 3 builder tests in `tests/test_admin_alerts.py` after E028 tests. Verify code, severity emoji, link substring. | `test-reviewer` | `tests/test_admin_alerts.py` | `tests/test_admin_alerts.py:183-205` |

**Wave 1 entry criterion:** none — pure-new files / additive edits. No dependency on `dev` HEAD changes.

**Wave 1 exit criterion:** all 4 tasks pass review; T1's parser is importable; T2's tests green against T1 in isolation; T4's tests green against T3.

#### Wave 2 — Wiring (parallel, after Wave 1)

Connects the parser into the bot. Five small mechanical changes.

| Task | Description | Reviewers | Files to modify | Files to read |
|---|---|---|---|---|
| **T5: news_bot.py import + dispatcher** | Add `import t_hunted_source` at line 38. Add new dispatcher branch at line 1438-1439 (after autoevolution): `if 'blogspot.com' in domain: return t_hunted_source.fetch_t_hunted_article(...)`. | `code-reviewer` + `security-auditor` (dispatcher branch ordering) | `news_bot.py` | `news_bot.py:34-38, 1405-1444`, T1 output |
| **T6: NETLOC_TO_SOURCE + SOURCE_HASHTAG_OVERRIDE + emoji/label maps** | Add `'t-hunted.blogspot.com': 't-hunted'` to NETLOC_TO_SOURCE (line 828 area). Add new `SOURCE_HASHTAG_OVERRIDE` dict per §F. Modify `_source_hashtag` to consult it. Add `'t-hunted'` entries to SOURCE_EMOJI (brown circle U+1F7E4) and SOURCE_LABEL. | `code-reviewer` | `news_bot.py` | `news_bot.py:799-868`, code-research.md §F |
| **T7: sources_registry tests** | Update locked-set test names + assertions per §C.2 (3 changed tests + 1 new). | `test-reviewer` | `tests/test_sources_registry.py` | `tests/test_sources_registry.py:42-103` |
| **T8: telegram hashtag tests** | Add `test_t_hunted_hashtag` + `test_t_hunted_teaser_appends_news_tag` per §C.3. Locks AC6 format `#thunted #news`. | `test-reviewer` | `tests/test_telegram.py` | `tests/test_telegram.py:19-105`, T6 output |
| **T9: feeds.json + boilerplate_filter PT block** | Append t-hunted RSS URL to `feeds.json` (4th entry). Append PT pattern block (10 regexes per §E of cycle-2 / §A.3 here) at end of `_BOILERPLATE_PATTERNS`. Add `TestPortuguesePatterns` parametrised test class to `tests/test_boilerplate_filter.py`. | `code-reviewer` + `test-reviewer` | `feeds.json`, `boilerplate_filter.py`, `tests/test_boilerplate_filter.py` | `boilerplate_filter.py:51-158`, `tests/test_boilerplate_filter.py:71-94` |
| **T10: deploy FILES lists** | Add `"t_hunted_source.py"` to all three FILES arrays — `deploy.sh:42`, `.github/workflows/deploy.yml:128`, `.github/workflows/deploy_test.yml:99` — same commit. | `code-reviewer` | `deploy.sh`, `.github/workflows/deploy.yml`, `.github/workflows/deploy_test.yml` | All 3 files |

**Wave 2 entry criterion:** Wave 1 merged-equivalent (T1 + T3 modules importable).

**Wave 2 exit criterion:** `pytest` green; `python3 -c "import news_bot"` succeeds (catches import order issues); end-to-end mocked smoke from Wave 1 / Wave 2 combination green.

#### Wave 3 — Prompt + integration (parallel)

| Task | Description | Reviewers | Files to modify | Files to read |
|---|---|---|---|---|
| **T11: ux-guidelines.md edits** | Widen line 22 PT-input assertion. Insert 🟤 t-hunted block from §D. Insert PT-EN-RU glossary section from §E. Preserve blockquote-verbatim invariant (file line 132). | `code-reviewer` (markdown structure) + manual operator review (Russian prose) | `.claude/skills/project-knowledge/references/ux-guidelines.md` | Full file, §D + §E here |
| **T12: integration smoke `tests/test_integration_t_hunted.py`** | New file with single test `test_t_hunted_article_flows_through_full_pipeline`. Asserts end-to-end PT entry passes through resolver / dispatcher / parser / hashtag / pending_repo without cross-language interference with EN entries in the same tick. Matches `-k integration_t_hunted` per user-spec test plan row 2. | `test-reviewer` + `code-reviewer` | `tests/test_integration_t_hunted.py` (NEW) | `tests/test_distributed_schedule_integration.py` (shape reference), all Wave 1+2 outputs |

**Wave 3 entry criterion:** Wave 2 merged-equivalent.

**Wave 3 exit criterion:** full `pytest` green; `pytest -k integration_t_hunted` green; deploy_test.yml dry-run finds all FILES.

#### Audit wave (post-Wave 3)

Owned by orchestrator. Suggested checks:
- `code-reviewer` final pass on the diff as a whole (cross-file coherence).
- `security-auditor` final SSRF audit on parser + dispatcher.
- Manual: visual diff of `ux-guidelines.md` to confirm system-prompt blockquote unchanged in body, only the adjective at line 22 widened.

#### Final wave (post-audit)

- Single PR / merge.
- Operator-side: pre-deploy QA per user-spec §Acceptance procedure (live RSS smoke against real t-hunted feed, hashtag rendering check in Telegram test channel).
- Post-deploy 7-day quality watch per user-spec §Test plan rows 6-7.

---

### H. Open implementation questions

All major decisions locked by user-spec or this deepening. Two minor questions remain for tech-spec author to confirm with the user:

1. **`SOURCE_EMOJI` brown circle (U+1F7E4) acceptance.** Per §A.1 / T6, cosmetic but archived-path-only. **Recommend just-do-it**, no user gate. Confirmation: «оставляем brown circle 🟤 или брать другой emoji?» — но 🟤 уже использован в user-spec/code-research как маркер блока ux-guidelines, так что консистентность за него.
2. **PT glossary verification timing.** §E flags `decalque` and `carrinho` as `[VERIFY operator]`. Should the tech-spec require operator review of glossary **before** PR merge, or **post-deploy** during the 7-day quality watch? **Recommend post-deploy** — these entries don't block AC7 (glossary ≥10 structural presence), and refining them needs samples that don't exist pre-launch. User-spec §Quality watch row 1 already covers this implicitly.

Items **already resolved** (no question to user needed):
- ✓ Hashtag spelling → `#thunted` (user-spec L106).
- ✓ Hashtag mechanism → SOURCE_HASHTAG_OVERRIDE dict (§F).
- ✓ curl_cffi → not needed (cycle-2 §3.A).
- ✓ Module file naming → `t_hunted_source.py` (cycle-2 §6 + Python module-name rule).
- ✓ Dispatcher condition → `'blogspot.com' in domain` (cycle-2 §1.C + §A.1 here).
- ✓ Image dedup strategy → strip `=s\d+(-c)?` suffix (cycle-2 §3.E + §B here).
- ✓ Admin alerts naming → E031-E033 (§A.5 here, next-free after E030).
- ✓ Glossary entry count → 14 entries (§E, exceeds AC7's ≥10).
- ✓ Wave count → 3 implementation waves + audit + final (§G).

---

End of deepening. Total deepening LOC: ~480 (under the 500-line budget). New file:line refs introduced this section: ~25. Tech-spec author may proceed directly to §G for the task breakdown and §A/B/F for code-shape decisions.
