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
