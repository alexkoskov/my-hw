# Hot Wheels News Bot

A Python bot that collects Hot Wheels news, transcreates each article into
Russian with an LLM (full text — not a summary), publishes the body as a
Telegra.ph long-read, and posts a one-line hashtag card with an Instant View
preview to a Telegram channel.

This file is the cold-start orientation only. Everything operational —
deployment, env vars, monitoring, cost — lives in
`.claude/skills/project-knowledge/references/`.

## Sources

Live (4 sites):

| Site | How it is fetched |
|------|-------------------|
| autoevolution.com (2 tag feeds) | RSS + Cloudflare-bypass scrape (`curl_cffi`) — `autoevolution_source.py` |
| lamleygroup.com | RSS + HTML scrape — `lamley_source.py` |
| t-hunted.blogspot.com | Blogspot RSS + HTML — `t_hunted_source.py` |
| orangetrackdiecast.com | own RSS feed, body from `content:encoded` — `orangetrack_source.py` |

The first three come from `feeds.json`; orangetrack has its own feed constant.
Both fetchers are registered in `news_bot.SOURCES` (news_bot.py:3607-3611);
per-site body parsers are dispatched by hostname in `fetch_full_article`
(news_bot.py:3380-3425).

Disabled: **corporate.mattel.com** — commented out of `SOURCES` on 2026-05-24
(the site moved to client-side rendering). Parser and tests are retained; the
rationale is at news_bot.py:3599-3606.

## Pipeline

1. A single in-process daily tick at 10:00 МСК (`schedule`, news_bot.py:4641)
   fetches all sources and stages articles into a SQLite queue.
2. Publishing happens at three fixed slots — 10:00 / 15:00 / 19:30 МСК,
   one post per slot, hard cap `MAX_DAILY_POSTS = 3` (news_bot.py:172,
   compute_publish_slots.py:49). Surplus carries over to the next day.
3. Each article is transcreated by an LLM. `llm_transcreation.py` is a
   dispatcher over four engines (OpenAI / Anthropic / Gemini / OpenRouter);
   **production runs OpenRouter** (`LLM_PROVIDER=openrouter`). The system
   prompt is `.claude/skills/project-knowledge/references/ux-guidelines.md`.
4. The translated body is published to Telegra.ph
   (`telegraph_publisher.py`), then the channel card is posted to Telegram.
   The Telegra.ph URL is persisted before the Telegram send, so a retry never
   creates a second page.
5. On an API-level LLM outage nothing is published — the article is HELD in
   the queue and retried later (hold-and-wait, 2026-06-11). There is no
   machine-translation fallback.

## Safety layers

Between fetch and publish an article passes several gates, each of which
pings the operator's admin chat with a grepable `[E0XX]` code (all message
texts live in `admin_alerts.py`):

- **Promo filter** `[E035]` — advertising/press-release posts are dropped
  before translation, so no tokens are spent on them.
- **Genre filter** `[E037]` — non-article formats (e.g. pure video reviews).
- **Cross-source dedup** `[E014]` / `[E015]` — the same car covered by two
  sites. A soft match sends the operator inline review buttons
  (`REVIEW_BUTTONS_ENABLED`); a hard match blocks the post.
- **Boilerplate / author-plug stripping** — `boilerplate_filter.py` plus
  `_strip_plugs*` in `news_bot.py`.

A blocked link is pinned in `processed_news` so the same decision is not
re-made — and not re-alerted — on every tick.

## Running it locally

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values, see the comments there
python news_bot.py     # runs one tick immediately, then schedules 10:00 МСК
pytest                 # test suite lives in tests/
```

Careful: `news_bot.py` runs a tick immediately at startup (news_bot.py:4645).
With production credentials in `.env` that publishes to the real channel.

Production is a Docker container on the Moscow host and is deployed manually
by the operator — see
[deployment.md](.claude/skills/project-knowledge/references/deployment.md) for
the redeploy command, restart scheduling effect, logs and monitoring. Do not
follow any systemd/cron instructions found in older notes.

## Where things are documented

| File | Contains |
|------|----------|
| `.claude/skills/project-knowledge/references/project.md` | What the project is, key features, roadmap |
| `.claude/skills/project-knowledge/references/architecture.md` | Tech stack, module map, data model |
| `.claude/skills/project-knowledge/references/patterns.md` | Project-specific code patterns, git workflow, business rules |
| `.claude/skills/project-knowledge/references/deployment.md` | Server, env vars, deploy, monitoring, cost |
| `.claude/skills/project-knowledge/references/ux-guidelines.md` | The transcreation prompt (style contract for Russian output) |
| `.env.example` | The environment variables the code reads, with defaults |
| `work/` | Per-feature folders (user-spec / tech-spec / decisions) and dated `SESSION-*.md` logs |

---

## License

This project is provided as-is for educational and personal use.
