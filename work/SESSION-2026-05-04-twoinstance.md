# Session 2026-05-04 — two-instance topology (prod + test)

## Context

Operator wanted a way to test code changes without hitting the live channel.
Previous setup: single `news_bot.service` on VPS, single channel
`@myhwchannel123`. Now: two systemd services on the same VPS, each posting
to its own Telegram channel and deploying from its own git branch.

## What shipped

**On the VPS** (`148.135.207.54`):

- Renamed existing `/home/hwbot/bot/` → `/home/hwbot/bot_test/`. The directory
  kept its `news.db` (full prod history of `processed_news`) and stayed
  attached to channel `@myhwchannel123` — became the **test** instance.
- Created fresh `/home/hwbot/bot/` for the new **prod** instance. Seeded it
  with a copy of the test instance's `news.db` (so prod inherits the
  same dedup history → won't re-publish the current RSS backlog as "new").
  New channel id `-1004027529994`.
- New systemd unit `news_bot_test.service` for the test instance. Existing
  `news_bot.service` continues to point at `/home/hwbot/bot/` (now prod).
- Updated `/etc/sudoers.d/news_bot` — operator allowed to passwordlessly
  restart both `news_bot.service` and `news_bot_test.service`.
- Both `.env` files got `INSTANCE_LABEL=prod` and `INSTANCE_LABEL=test`
  respectively. The label is prepended to every admin ping — operator can
  now distinguish source in the same admin chat.

**In the repo:**

- `.github/workflows/ci.yml` now triggers on push/PR to both `main` and
  `dev` (was main-only).
- `.github/workflows/deploy_test.yml` (new): mirror of `deploy.yml`
  targeting the test instance. Triggers on workflow_run from CI on `dev`,
  uses `DEPLOY_PATH_TEST` repo secret (= `/home/hwbot/bot_test/`),
  restarts `news_bot_test.service`.
- `news_bot.py` reads optional env var `INSTANCE_LABEL` and prepends
  `[<label>] ` to every admin ping when set. Empty / unset → no prefix
  (backward compatible).
- `dev` branch on origin force-reset to match `main` (old `ci(fix)`
  history compacted away — dubs of merged commits, no real loss).
- Repo secret added: `DEPLOY_PATH_TEST=/home/hwbot/bot_test/`.

## Iteration cycle going forward

```
push to dev  → CI on dev   → deploy_test.yml → /home/hwbot/bot_test/    → @myhwchannel123 (test)
                                              → news_bot_test.service restart

merge dev→main + push main → CI on main → deploy.yml      → /home/hwbot/bot/         → -1004027529994 (prod)
                                                          → news_bot.service restart
```

## Verification

End-to-end confirmed working:

- `Deploy test #1` workflow run completed (37s, green) on dev push.
- Both admin pings arrived in operator's admin chat with correct
  prefixes:
  - `[prod] 🟢 Бот сработал, новых статей нет.` (13:30 МСК)
  - `[test] 🟢 Бот сработал, новых статей нет.` (14:29 МСК)

## Items not done / left for later

- Memory file `project_observation_mode.md` snapshot lists only one
  channel; with two instances now in place, it's slightly stale but the
  important info (server, schedule, stack) still applies.
- Doc updates to `architecture.md` for the two-instance topology — only
  `deployment.md` got the update in this session. Operator can extend
  `architecture.md` later if the layout proves stable.

## Lesson re-applied

Operator's frustration earlier in the day with the over-process on the
author-plug-filter feature led to the new memory rule
(`feedback_molyanov_default_with_pushback.md`). This session was sized
upfront as ad-hoc infrastructure (no Molyanov flow), kept lean, and
shipped in ~2 hours of operator-paced steps.
