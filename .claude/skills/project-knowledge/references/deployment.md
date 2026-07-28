# Deployment & Operations

## Purpose
Deployment process, infrastructure, and production operations for AI agents.

---

> **Status (2026-07-07):** ✅ **Prod migrated to a Docker container on the Moscow
> VPS `45.90.216.165`** (cutover 2026-07-06, validated over a full daily cycle —
> 19:30 + next-day 10:00 posts confirmed). The old **Netherlands VPS
> `148.135.207.54`** `news_bot.service` is **stopped + disabled** — no longer prod.
> The NL box is **KEPT (not cancelled)**: it still runs the **test bot**
> (`news_bot_test.service`) and the operator's other bots.
>
> **Current prod runtime:** `hw-news-bot` Docker container on `45.90.216.165`
> (repo at `/root/hw-news`, tracking **`main`**). Egress routes through the shared
> `shared-vpn` gateway (sing-box VLESS, `172.28.0.2` on the external `vpnnet`
> network) so the RU host reaches Telegram — same pattern as the colocated
> intake-bot. Compose (`docker-compose.yml`): `news-bot` + a `route-setup` sidecar
> that points the default route at the gateway. State lives on a host bind-mount
> (`./data:/data`): `.env` (secrets, copied from the old NL server) + `data/news.db`
> (`DB_FILE=/data/news.db` — the DB path is env-overridable as of 2026-07-06; the
> hardcoded default `news.db` remains for NL/test/local).
>
> **Current prod deploy (MANUAL):** on the host — `cd /root/hw-news && git pull &&
> docker compose up -d --build`. Run it **OUTSIDE the 10:00–20:00 МСК publish
> window** (a container restart resets the in-process day schedule). There is **no
> GitHub-Actions prod deploy**: the old `deploy.yml` (SCP + `systemctl restart` to
> NL) is **DISARMED** 2026-07-07 (`workflow_run` trigger removed + job `if: false`)
> because re-running it would `restart` → **revive the disabled NL bot → double-post**.
> A proper Docker-targeted CI (git pull + `docker compose up` over SSH to Moscow)
> is a TODO.
>
> **⚠️ Everything below the "Deployment Platform" heading describes the LEGACY
> NL/systemd path.** It is still accurate for the **test bot** (which remains on NL
> via `deploy_test.yml`) and kept as prod historical reference, but the PROD deploy
> is now the Docker/Moscow process above. See [[project_host_outside_russia]] for
> the why; runbooks: `work/MIGRATION-docker-vpn-2026-07-03.md` +
> `work/CUTOVER-CHECKLIST-2026-07-06.md`.

---

## Серверная шпаргалка (все данные бота — не искать заново)

> Обновлено 2026-07-25. Секретов здесь нет и быть не должно: токены/пароли — в
> менеджере паролей оператора и в серверном `.env` (не коммитится).
>
> **Бот теперь ОДИН — прод.** NL-сервер `148.135.207.54` (DeluxHost) **больше
> не оплачивается**, тест-бот там **удалён** (подтверждено оператором
> 2026-07-25). IP отдан хостером другому клиенту — на нём отвечает ЧУЖОЙ sshd
> («Connection closed» при попытке входа — это он). Никогда не ходить на этот
> адрес и ничего туда не деплоить. Тест-инстанса и staging НЕТ.

| | **ПРОД (единственный инстанс)** |
|---|---|
| Сервер | Москва `45.90.216.165` (Firstbyte) |
| Вход | `ssh root@45.90.216.165` — **root, по паролю** (пароль в менеджере паролей) |
| Как запущен | Docker-контейнер **`hw-news-bot`** (+ sidecar `route-setup`) |
| Папка | `/root/hw-news` |
| Ветка | `main` |
| `.env` | `/root/hw-news/.env` — **правится только руками** |
| База | `/root/hw-news/data/news.db` (в контейнере `/data/news.db`) |
| Логи | `ssh root@45.90.216.165 "docker logs hw-news-bot --tail 200"` |
| Канал | `-1004027529994` (боевой) |
| INSTANCE_LABEL | `prod` |
| Деплой | **только руками, ВНЕ окна 10:00–20:00 МСК**: `ssh root@45.90.216.165 "cd /root/hw-news && git pull && docker compose up -d --build"` |
| Бэкап БД | cron 05:00 МСК → `/root/hw-news/backups` (TODO: копия вне хоста) |
| Watchdog | host cron 01:00 МСК → `docker exec hw-news-bot /bin/bash /app/watchdog.sh` |

**Ключевые факты:** числовой Telegram-id оператора — **`8481233034`**
(`TELEGRAM_ADMIN_ID`; это id, не секрет — аутентификацию делает сам Telegram).
`REVIEW_BUTTONS_ENABLED=1` — включён только здесь (инстанс один, конфликтовать
за getUpdates некому, но флаг остаётся выключателем фичи). Egress — через
VPN-шлюз `shared-vpn` (Москва без VPN не достаёт Telegram). Хост живёт в UTC;
МСК — только внутри контейнера.

**⚠️ Грабли:**
- Оба GitHub-workflow деплоя **обезврежены**: `deploy.yml` (2026-07-07) и
  `deploy_test.yml` (2026-07-25, NL списан). Пуш в `dev`/`main` гоняет ТОЛЬКО
  тесты (ci.yml); на сервера CI не ходит. Прод обновляется только руками.
- Тестировать «на живом» негде — staging нет. Проверка фич: pytest локально +
  аккуратная первая раскатка на прод с готовым откатом.
- В GitHub-секретах остались реквизиты мёртвого NL (`SSH_HOST`,
  `DEPLOY_PATH*`, `SSH_PRIVATE_KEY`) — использоваться не могут (workflows
  выключены); при случае почистить.

---

## Deployment Platform

**Prod (since 2026-07-06):** a **Docker container** (`hw-news-bot`) on the Moscow VPS
`45.90.216.165`, egress routed through the shared non-RU VPN gateway (see the Status
callout + Two-instance topology). The app is still a single long-running Python process
(`schedule` in-process, no web framework) — it just runs inside a container now, with
state on the `./data:/data` bind-mount.

**Test:** the legacy layout — a `systemd`-managed process on the NL VPS `148.135.207.54`.

**Why:** the RU host reaches Telegram only through the VPN, and Docker gives a
reproducible image + isolated egress routing without changing the bot code.

---

## Access Information

**SSH Access:**
- **Prod:** `ssh root@45.90.216.165` (Moscow VPS, password auth). Repo/deploy dir: `/root/hw-news`; state on `/root/hw-news/data`.
- **Test + other bots:** `ssh hwbot@148.135.207.54` (NL VPS, key auth); root exists on the box but is NOT used from the operator's Mac — failed root attempts trigger the fail2ban IP ban (see Шпаргалка → грабли). Test bot dir: `/home/hwbot/bot_test`.

> Operator runs all server-side ops (SSH, deploy, restart); Claude prepares the commands.

**Credentials:** in the operator's password manager / SSH keys. The bot secrets live only in each server's `.env` (never committed).

---

## Environment Variables

**See:** [.env.example](../../.env.example) in project root

**Required for production cron:**

- `TELEGRAM_BOT_TOKEN` — bot token from BotFather. Sensitive — never echo/log. Code suppresses `httpx`/`httpcore` INFO to prevent URL-path leak.
- `TELEGRAM_CHANNEL_ID` — `@myhwchannel123` or numeric ID.
- `TELEGRAM_ADMIN_ID` — personal Telegram numeric chat_id for admin pings (daily schedule, outage protocol, backlog warning, error digests). User must `/start` the bot first for DMs to work.
- `TELEGRAPH_ACCESS_TOKEN` — auto-created by `telegraph_publisher.ensure_access_token` on first run and persisted to `.env`.
- `ANTHROPIC_API_KEY` — Claude API key for the auto-publish path's primary translator (llm-transcreation feature). Get from https://console.anthropic.com → API Keys → Create Key (format `sk-ant-api03-…`). **Sensitive** — redacted from logs by `_TokenRedactingFilter` (pattern `sk-ant-[A-Za-z0-9_=.-]{16,}`) and from admin Telegram pings by the shared `_redact_text` helper. NEVER echo/log/commit. Without this var, `claude_transcreation` hits `AuthenticationError` on the first API call → 2-ping outage protocol fires → posts are HELD (hold-and-wait, 2026-06-11), nothing published until the key/API recovers. (Only relevant when `LLM_PROVIDER` pins Anthropic; production default is OpenRouter.)
- `TZ=Europe/Moscow` — process timezone. The daily tick fires at 10:00 МСК via `pytz`-aware `schedule.every().day.at("10:00", tz=...)`, so it's correct regardless of `TZ`. But `os.getenv('TZ')` is checked at startup (Decision 14 health check #2) — if it doesn't equal `'Europe/Moscow'`, the bot sends an admin warning ping ("log timestamps may show non-MSK times"). Note: the Moscow **host** is on UTC; only the container is MSK.

**Optional (tunable defaults):**

- `ANTHROPIC_MODEL` (default `claude-haiku-4-5`) — Claude model name. Override to `claude-sonnet-4-6` for higher quality at ~5× cost. Best stored as a GitHub Actions repo `var` rather than a secret; safe to log.
- `INSTANCE_LABEL` — short label distinguishing this bot instance in admin pings. When set (e.g. `prod` or `test`), `send_admin_notification` prepends `[<label>] ` to every admin-bound message. Empty / unset → no prefix. Set ONCE manually in each instance's `.env`. Prod = `prod` — also drives the startup DB guard (below). Used by the two-instance topology.
- `DB_FILE` — SQLite path, env-overridable (2026-07-06). **Prod container MUST set `DB_FILE=/data/news.db`** so state lands on the mounted volume, not the ephemeral image layer. Default `"news.db"` (relative) keeps NL/test/local behaviour. A prod instance with a relative `DB_FILE` or an empty `processed_news` triggers a startup `[E018]` admin ping (re-flood guard).
- `HEARTBEAT_FILE` — heartbeat marker path, env-overridable. Prod container sets `HEARTBEAT_FILE=/data/last_tick.ts` (persistent + readable by the `docker exec` watchdog). Default `~/.cache/news_bot/last_tick.ts`.
- `REVIEW_BUTTONS_ENABLED` — review buttons under the `[E014]` dedup ping (dedup-review-buttons feature). **Default off**; enable (`=1`) ONLY in the hand-managed prod `.env`. Double gate: the same effective gate (flag + non-empty `TELEGRAM_BOT_TOKEN` + numeric `TELEGRAM_ADMIN_ID`, fail-closed otherwise) enables BOTH the background `get_updates` listener AND keyboard rendering at the E014 send site — an instance that can't listen renders no buttons. Exactly ONE instance may poll the shared bot token — a second poller gets HTTP 409. See § Feature rollout: dedup-review-buttons.

**How env reaches each instance:** **prod** — `docker-compose.yml` (`env_file: .env` + `environment: HEARTBEAT_FILE`); the prod `.env` is **hand-managed on the Moscow host** (copied once from NL, not CI-written). **test** — CI-written: `deploy_test.yml` writes `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`/`TZ` etc. to the NL server `.env` idempotently on every dev push (TELEGRAM_*, TELEGRAPH_ACCESS_TOKEN, INSTANCE_LABEL, DB_FILE preserved). The archived `hw_review.py` would read a local `.env` if revived (dormant).

---

## Two-instance topology (prod = Moscow Docker, test = NL systemd)

Since the 2026-07-06 migration the two instances run on **different hosts**: prod is
a Docker container on the Moscow VPS `45.90.216.165`; test remains a systemd unit on
the NL VPS `148.135.207.54`. Each has its own `.env`, `news.db`, and Telegram channel.

| Instance | Host | Runtime | Channel | Branch | Deploy |
|---|---|---|---|---|---|
| **prod** | Moscow `45.90.216.165` | Docker `hw-news-bot` (`/root/hw-news`, `data/news.db`) | `-1004027529994` (prod) | `main` | **manual** `git pull && docker compose up -d --build` (deploy.yml DISARMED) |
| **test** | NL `148.135.207.54` | systemd `news_bot_test.service` (`/home/hwbot/bot_test/`) | `@myhwchannel123` (test) | `dev` | `deploy_test.yml` (SCP + systemd) |

The bot Telegram TOKEN is shared (one bot account posts to both channels). The Anthropic / OpenRouter / etc. API keys are shared (CI writes them to both `.env` files via the respective deploy workflow). Per-instance values that differ:

- `TELEGRAM_CHANNEL_ID`
- `INSTANCE_LABEL` (`prod` / `test`) — prepended to every admin ping by `news_bot.send_admin_notification` so the operator can distinguish source.
- `news.db` — independent SQLite files, no contention.

`INSTANCE_LABEL` and `TELEGRAM_CHANNEL_ID` are set ONCE manually on the server and NOT managed by the deploy workflow's strip-then-append rewrite (the workflow's regex strips only LLM-related keys + TZ, leaving everything else untouched).

**Iteration cycle:** `git push origin dev` → CI on dev → `deploy_test.yml` → test instance (NL) updates → operator inspects test channel → on confirmation `git checkout main && git merge dev && git push origin main`. Prod (Moscow) does NOT auto-deploy anymore — update it **manually** on the host: `cd /root/hw-news && git pull && docker compose up -d --build` (**outside the 10:00–20:00 МСК window**). The CI `deploy.yml` prod path is disarmed. Prod's `.env` is hand-managed on the Moscow host (not CI-written); test's `.env` is still CI-written by `deploy_test.yml`.

## Deployment Triggers

**Production (LEGACY — DISARMED 2026-07-07):** ⚠️ The description in this paragraph is the OLD NL/systemd prod deploy and **no longer runs**. `deploy.yml` had its `workflow_run` trigger removed and the job hard-guarded `if: false` because it SCP'd + `systemctl restart news_bot.service` to the OLD NL host — which would revive the stopped+disabled NL prod bot and double-post. **Current prod deploy is manual on the Moscow Docker host** (see the Status callout at the top). Historical behaviour, for reference: `git push origin main` → `ci.yml` pytest → on green, `deploy.yml` via `workflow_run` SCP'd the FILES list to `$DEPLOY_PATH` (= `/home/hwbot/bot/`), `pip install --user -r requirements.txt`, then `sudo systemctl restart news_bot.service`.

**Test / staging (GitHub Actions CI):** `git push origin dev` → `ci.yml` runs pytest → on green, `.github/workflows/deploy_test.yml` triggers via `workflow_run`, SCPs the same FILES list to `$DEPLOY_PATH_TEST` (= `/home/hwbot/bot_test/`), `pip install`, then `sudo systemctl restart news_bot_test.service`. Independent concurrency group from prod (`deploy-test`). Manual run available via `Actions → Deploy test → Run workflow`.

**GitHub Secrets required** (Settings → Secrets and variables → Actions → New repository secret):
- `SSH_HOST` — VPS hostname or IP (e.g. `148.135.207.54`).
- `SSH_USER` — SSH login user on the VPS (`hwbot` for both deploy workflows).
- `DEPLOY_PATH` — prod deploy path on the VPS (= `/home/hwbot/bot/`). Used by `deploy.yml` (main branch).
- `DEPLOY_PATH_TEST` — test deploy path on the VPS (= `/home/hwbot/bot_test/`). Used by `deploy_test.yml` (dev branch). Required only for the two-instance topology; without it `deploy_test.yml` fails fast with a clear error.
- `SSH_PRIVATE_KEY` — full PEM-encoded private key (including `-----BEGIN…END-----` lines) for the deploy account. Generate a dedicated key for CI: `ssh-keygen -t ed25519 -f ~/.ssh/hwbot_deploy -C "github-actions-hwbot"`; append the `.pub` half to the VPS account's `~/.ssh/authorized_keys`; paste the private half into the secret. The same key is used by both prod and test deploy workflows.
- `ANTHROPIC_API_KEY` — Claude API key (format `sk-ant-api03-…`). Get from https://console.anthropic.com → API Keys → Create Key, copy the value, paste it as a new repository secret with name `ANTHROPIC_API_KEY`. The deploy workflow forwards it to the server's `.env` over ssh stdin; values never appear on a command line and are auto-redacted in workflow logs. Both prod and test instances share this key.

**GitHub Variables (optional, non-sensitive)** (Settings → Secrets and variables → Actions → Variables):
- `ANTHROPIC_MODEL` — defaults to `claude-haiku-4-5` if unset. Set this `var` to override (e.g. `claude-sonnet-4-6`).
- `TZ` — defaults to `Europe/Moscow` if unset.

Both are stored as `vars` (not `secrets`) because they aren't sensitive — visible in workflow logs is fine.

**Production (fallback — manual SCP):** `bash deploy.sh` with `SSH_HOST` + `DEPLOY_PATH` env overrides. Same FILES list as the workflow. Use when GitHub Actions is unavailable or for emergency hotfixes that can't go through `main`. Note: `deploy.sh` does NOT update `.env` — the operator must edit the server `.env` directly when bootstrapping a new VPS or rotating `ANTHROPIC_API_KEY`.

**Files deployed** (cron-path only; operator-side modules excluded):
- Core cron-path: `news_bot.py`, `pending_articles_repo.py`, `telegraph_publisher.py`, source parsers (`autoevolution_source.py`, `mattel_news_source.py`, `lamley_source.py`).
- llm-transcreation runtime modules (added by the llm-transcreation feature; all imported by `news_bot.py` at startup — without any of them, `news_bot` crashes with `ImportError` on the first cron tick): `claude_transcreation.py`, `compute_publish_slots.py`, `outage_state.py`.
- Config: `feeds.json`, `requirements.txt`, `.env.example`.
- Claude API system prompt: `.claude/skills/project-knowledge/references/ux-guidelines.md`. **Note (Decision 8 deploy quirk):** `scp` is invoked WITHOUT `-r`, so subdirs are flattened — on the server the file lands at `$DEPLOY_PATH/ux-guidelines.md` (NOT inside a subdir). `claude_transcreation._load_prompt` tries the original subdir path first, then falls back to the flat filename — both layouts work, so the operator should not be surprised to find the file at the top level of `DEPLOY_PATH`.
  - **Current path (Docker prod, added 2026-07-28):** the scp flattening above is the LEGACY route. Today the file reaches prod inside the repo checkout via `git pull && docker compose up -d --build`, at its normal subdir path — the `_load_prompt` subdir attempt wins and the flat fallback never fires. Practical consequence: **editing the prompt is a production behaviour change and needs that rebuild.** Like any restart it resets the single in-process daily schedule, so run it OUTSIDE the 10:00–20:00 МСК publishing window. Prompt-only edits ship no code, so there is nothing to verify beyond reading the next few publications — the LLM output cannot be asserted by tests.

The list lives in two places — `.github/workflows/deploy.yml` and `deploy.sh` — and is asserted byte-for-byte identical by the headline comments. **INVARIANT:** any new first-party import added to `news_bot.py` MUST be mirrored into both FILES arrays. Otherwise the server hits `ImportError` on the next cron tick with no CI signal beforehand.

**Files NOT deployed**: `hw_review.py`, `preview_renderer.py` — manual review path archived 2026-04-30, code preserved + tests green for ad-hoc revival, but never deployed (would run in operator's local Claude Code session, not on the VPS).

**Service auto-restart:** the deploy workflow ends with `ssh ... "sudo systemctl restart news_bot.service"` — code changes go live immediately, NOT deferred to the next 10:00 МСК cron tick. The SSH step depends on the sudoers NOPASSWD rule below; if it's missing, the deploy step prints a `::error::` hint pointing at `/etc/sudoers.d/news_bot`.

**Server-side sudoers** (`/etc/sudoers.d/news_bot`, mode **0440 — anything else and sudo silently ignores the file**):

```
hwbot ALL=(root) NOPASSWD: /usr/bin/systemctl restart news_bot.service, /usr/bin/systemctl restart news_bot_test.service
```

The single rule covers both NL services — the decommissioned `news_bot.service` and the live test `news_bot_test.service` (only `news_bot_test.service` still runs). Install via `ssh root@<host>` (one-time): `visudo -f /etc/sudoers.d/news_bot`, paste the line, `chmod 0440 /etc/sudoers.d/news_bot`, `visudo -c` to verify. Without this rule the deploy's restart step fails with "terminal is required to read the password" — the only command path that needs explicit privilege escalation is the systemd restart.

> **⚠️ Hardened 2026-07-07 (S2 — privilege-escalation fix).** The rule previously
> also granted `systemctl status …` and `journalctl -u …` NOPASSWD "for
> convenience". Both spawn a **pager (`less`) as root** by default — from the
> pager `!sh` gives a **root shell** (GTFOBins). Since the `hwbot` private key is
> a CI secret (`SSH_PRIVATE_KEY`), anyone with it (or any `hwbot` compromise) got
> root on the box that hosts the test bot + the operator's other bots. Removed
> both — the deploy workflows only ever call `restart` (exact-arg match = safe, no
> pager); the operator runs status/journalctl as root directly. Runas tightened
> `(ALL)`→`(root)`. To apply on the NL box (as root):
> `printf 'hwbot ALL=(root) NOPASSWD: /usr/bin/systemctl restart news_bot.service, /usr/bin/systemctl restart news_bot_test.service\n' > /etc/sudoers.d/news_bot && chmod 0440 /etc/sudoers.d/news_bot && visudo -c`

**Rollback:** **prod** — `git revert HEAD && git push origin main`, then on the Moscow host `cd /root/hw-news && git pull && docker compose up -d --build` (outside the publish window). **test** — `git revert HEAD && git push origin dev` → `deploy_test.yml` redeploys the parent commit + restarts `news_bot_test.service`. For DB rollback, restore `news.db` from backups (see Backups).

**Staging:** the `news_bot_test.service` instance on the **NL VPS** is staging. Posts go to `@myhwchannel123` (operator's only-subscriber test channel), not the prod channel `-1004027529994`. Activated by pushing `dev` → `deploy_test.yml`. See "Two-instance topology".

---

## Scheduling

Daily tick at **10:00 МСК** via `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)` inside `news_bot.main()`. One `job()` also fires immediately on startup so a deploy doesn't wait until tomorrow's 10:00. The crash-loop guard prevents burst posting on rapid restarts.

**Production scheduler = fixed daily slots** (`compute_fixed_slots`, operator pacing 2026-06-13): at most one post at each of **10:00 / 15:00 / 19:30 МСК**, oldest-pending first, ≤3/day (excess carries to the next day; AC20 admin warning at `len(pending) > 50`). A slot more than 5 min past is dropped (grace), so a restart never re-fires an already-published slot. The older even-spread `compute_publish_slots` (13:00–20:00, `MIN_INTERVAL_MINUTES=90`) is **DORMANT** — kept only for its unit tests.

**Restart-safe by design:** `schedule` runs in-process, so a container restart kills the in-flight `job()`. But `main()` re-runs `job()` immediately on boot, which recomputes today's remaining slots from `now` — already-published slots are dropped by grace, already-published rows are skipped via Decision 9 idempotency (telegraph_url presence). Verified live on the 2026-07-06 mid-window cutover. Still, prefer rebuilds **outside** the 10:00–20:00 МСК window.

---

## Pre-Deploy Checklist

- [ ] `python3 -m pytest tests/ -q` green locally.
- [ ] Merged to the target branch: **prod = `main`**, test = `dev`.
- [ ] **Prod (Moscow Docker):** outside the 10:00–20:00 МСК window, on the host: `cd /root/hw-news && git pull && docker compose up -d --build`. The prod `.env` (hand-managed) has `TELEGRAM_*`, `TELEGRAPH_ACCESS_TOKEN`, an LLM key (`OPENROUTER_API_KEY`), `INSTANCE_LABEL=prod`, `TZ=Europe/Moscow`, and **`DB_FILE=/data/news.db`** (compose also injects `HEARTBEAT_FILE`).
- [ ] **Test (NL systemd):** `git push origin dev` → `deploy_test.yml` handles SCP + `.env` write + restart automatically.
- [ ] Post-deploy: check `docker logs hw-news-bot` (prod) — clean boot, no `[E018]` DB-guard ping, `[E008]` plan sent — or `journalctl -u news_bot_test.service` (test).
- [ ] `news.db` present on the mounted volume (prod: `/root/hw-news/data/news.db`). Fresh instance auto-creates via `init_db()`; schema is idempotent (see `tests/test_migration.py`).

---

## Feature rollout: `dedup-model-series` (cold-DB warm-up + `DEDUP_SERIES_ENABLED` toggle)

One-time staged rollout for the tiered series/theme pair-rule (the layer that
hard-blocks cross-source pop-culture dupes — K-Pop Demon Hunters, Stranger
Things, Top Gun). This is a **feature-specific procedure on top of** the general
Pre-Deploy Checklist above — do not skip that; this only adds the cold-DB
warm-up + dark-deploy sequence. **Operator applies all server commands; Claude
only prepares them.**

> **⚠️ Everything here runs OUTSIDE the 10:00–20:00 МСК publish window.** Each
> `docker compose up -d --build` restarts the container and resets the
> in-process daily schedule (`compute_fixed_slots`, slots 10:00 / 15:00 / **19:30**).
> The Moscow host is on UTC.

**Why the warm-up is load-bearing:** the dedup gate looks back only **7 days**
through `pending_articles` + `published_articles` fingerprints. The Moscow prod
DB is **cold** — copied from the pre-feature NL snapshot — so every historical
row has `model_fingerprint IS NULL` and the gate has nothing to compare against
for ~the first week. `backfill_fingerprints.py` warms both the base
car-fingerprint AND the new `series`/`pairs` keys so the gate has history to
match on before the pair-rule goes live.

> **⚠️ Superseded (2026-07-20): backfill was SKIPPED for the actual rollout —
> self-warm was used instead.** Two reasons. (1) A bulk backfill (~40 URLs in
> ~1 min) trips autoevolution's Cloudflare **HTTP 403** rate-limit; paced live
> scraping (1 article/slot) does not. (2) The pre-fix `backfill_fingerprints.py`
> mis-handled a 403 (source returns `None`) as a *terminal* computed-empty
> marker, **permanently writing the row off the dedup gate** — fixed on `dev`
> (a no-body fetch is now retryable/NULL). But the deeper point: enabling with a
> partly-cold base is **safe** anyway (a hard-block needs a `|D` pair *match*;
> cold rows have no pairs → cannot false-block), and since every live publish
> already writes a four-key fingerprint, the 7-day window **self-warms** within
> ~7 days of deploy. So the dark-deploy + toggle steps below still apply, but the
> backfill step is optional — prefer self-warm. See
> [[project_backfill_403_trap_selfwarm]].

### Pre-deploy cold-DB check (operator, before touching anything)

On the Moscow host, confirm the base is cold:

```
sqlite3 /root/hw-news/data/news.db "SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL"
```

**Correction (2026-07-20 — the original "expect 0" was wrong).** A SMALL
non-zero count is NORMAL, not a surprise: cross-source-dedup has been live on
this prod since the 2026-07-06 Moscow cutover, so every article published since
carries an OLD two-key fingerprint (`{"strict":[...],"brands":[...]}`, no
`$.pairs`). ~40–70 such rows is expected. `Error: no such column:
model_fingerprint` is **also** fine (snapshot predates cross-source-dedup
entirely; `init_db()` adds the column on next boot). The REAL surprise to stop
on is rows **already carrying a `$.pairs` key** (dedup-model-series somehow
already ran) — verify the shape with
`sqlite3 .../news.db "SELECT model_fingerprint FROM published_articles WHERE model_fingerprint IS NOT NULL ORDER BY rowid DESC LIMIT 3"`
(two-key blobs = normal → proceed; four-key with `series`/`pairs` = investigate).

### Staged rollout (strictly outside the window)

1. **Pre-check** — run the `SELECT COUNT(*)` above; confirm `0` / no-such-column.
2. **Dark deploy** — in the **hand-managed prod `.env`** on the host add
   `DEDUP_SERIES_ENABLED=0`. The toggle is read **once at import**
   (`news_bot.DEDUP_SERIES_ENABLED`, default ON: unset/blank → enabled; only
   `0/false/no/off` disable it), so it takes effect on the next rebuild. With it
   off the gate runs only the legacy set-overlap backstop — the new pair-rule is
   inert and **cannot hard-block**.
3. **Build & restart** — `cd /root/hw-news && git pull && docker compose up -d --build`.
   `init_db()` adds the `model_fingerprint` column if the snapshot lacked it
   (idempotent).
4. **Warm-up backfill** — `docker compose exec -T news-bot python3 backfill_fingerprints.py --days 30`
   (inherits `DB_FILE=/data/news.db` from the container env). Idempotent
   (re-runnable; only touches rows missing the `$.pairs` key), supports
   `--dry-run` for a no-write dress rehearsal, and `--days` is clamped to
   `[1, 90]`. 30 days comfortably covers the gate's 7-day look-back.
5. **Re-count & dark-observe** — re-run the `SELECT COUNT(*)`; it must now be
   `> 0`. Watch `docker logs hw-news-bot` for a day or two on the freshly warmed
   base: fingerprints extracting cleanly on live articles, no `[E018]` DB-guard
   ping. The pair-rule is still OFF, so no hard block can land — that is the
   safety of the dark phase.
6. **Enable** — set `DEDUP_SERIES_ENABLED=1` (or simply **remove the override** —
   default is on) in the prod `.env`, then `docker compose up -d --build` again,
   **outside the window**. The pair-rule is now live: a shared broad pair
   soft-flags (`[E014]` — article still publishes + ping), a shared distinctive
   `|D` pair hard-blocks (`[E015]`, irreversible — no manual re-publish). Watch
   the first days of `[E014]`/`[E015]` pings for false positives.

> **Expected behaviour change once ON (not a regression).** A broad-tier
> republish that the legacy backstop *used* to silently hard-block at ≥50%
> car-overlap — e.g. a recurring **Car Culture** line re-covered by a second
> source — now **SOFT-FLAGS and PUBLISHES** with an `[E014]` ping instead of
> being dropped. This is intended (Decision 3 tiering: only a distinctive `|D`
> pair may hard-block; broad `|B` pairs always publish-and-notify). The `[E014]`
> ping IS the recovery signal — if it turns out to be a genuine dupe, the
> operator taps **«🚫 Не публиковать»** under the ping (requires
> `REVIEW_BUTTONS_ENABLED=1` — see § Feature rollout: dedup-review-buttons; with
> the flag off there is **no** way to pull the article back, the ping is
> informational only and the alert text says so). So expect **more visible
> `[E014]` pings and fewer silent drops** right after enabling; that is the
> feature working, not a false positive.

---

## Feature rollout: `dedup-review-buttons` (`REVIEW_BUTTONS_ENABLED` toggle)

Enables the inline «🚫 Не публиковать» / «👍 Оставить» buttons under the `[E014]`
«Похож на дубль» admin ping + the background listener that receives the presses
(the bot's first inbound Telegram path — see architecture.md § Inbound review
path). This is a **feature-specific procedure on top of** the general Pre-Deploy
Checklist. **Operator applies all server commands; Claude only prepares them.**
No deploy FILES changes — the feature lives entirely in already-deployed files.

> **⚠️ One bot token, ONE poller.** The Telegram bot account is shared prod+test,
> and Telegram allows exactly one `get_updates` consumer per token — a second
> poller gets HTTP 409. Enable `REVIEW_BUTTONS_ENABLED=1` on the **prod instance
> only**, never on test. The flag is default-off, and `deploy_test.yml` does not
> manage this var, so the test instance stays off (no polling, no buttons) unless
> someone hand-edits its `.env` — don't.

> **⚠️ Rebuild OUTSIDE the 10:00–20:00 МСК publish window.**
> `docker compose up -d --build` restarts the container and resets the in-process
> daily schedule (slots 10:00 / 15:00 / **19:30**). The Moscow host is on UTC.

### Enable (prod, Moscow host)

1. **Pre-check admin id** — the hand-managed prod `.env` must have a **numeric**
   `TELEGRAM_ADMIN_ID` (personal chat_id, not `@username`). Non-numeric →
   fail-closed: the listener refuses to start and logs a startup warning.
2. **Set the flag** — in the hand-managed prod `.env` (NOT CI-written) add
   `REVIEW_BUTTONS_ENABLED=1`.
3. **Rebuild** — outside the window: `cd /root/hw-news && git pull && docker compose up -d --build`.
4. **Verify the listener** — `docker logs hw-news-bot` must show the startup line
   **«review listener active»**. Missing line = gate closed (flag off or
   non-numeric admin id) — check the `.env`.
5. **Verify test stays out** — no 409 errors in either instance's logs (a 409
   means two pollers, i.e. the flag leaked onto test); the test channel's
   `[E014]` pings carry **no buttons** (flag off there → keyboard not rendered).
6. **First live press** — on a real `[E014]`, tap «🚫 Не публиковать» → the
   article does not publish in its slot and the buttons become «✅ Отменено
   оператором»; «👍 Оставить» → publishes as usual, «👍 Оставлено». A press
   after the slot already published resolves to «⚠️ Уже опубликовано, отменить
   нельзя» — expected, not a bug.

### Disable / rollback

Remove the `REVIEW_BUTTONS_ENABLED=1` line from the prod `.env` (default is off)
and rebuild outside the window. Buttons stop rendering and the listener does not
start; already-sent buttons become inert (stale tokens are harmless).

---

## Rollback Procedure

**Prod (Moscow Docker):** `git revert HEAD && git push origin main`, then on the host `git pull && docker compose up -d --build` (outside the window). ~3–4 min incl. build.

**Test (NL systemd):** `git revert HEAD && git push origin dev` → `deploy_test.yml` redeploys the parent commit + restarts the service.

**DB rollback:** restore `news.db` from a backup (see Backups) — prod: stop the container, replace `/root/hw-news/data/news.db`, start it.

---

## Environments

**Production:** Docker container on the Moscow VPS (no public URL). Deploys from `main` — manual rebuild on the host (`git pull && docker compose up -d --build`), outside the publish window. **Staging:** `news_bot_test.service` on the NL VPS, deploys from `dev` via `deploy_test.yml`.

---

## Monitoring & Observability

### Logging

**Where:** stdout — prod via `docker logs hw-news-bot` (json-file driver, 10 MB × 3 rotation); test via the systemd journal (`journalctl -u news_bot_test.service`).
**Format:** Plain text with timestamps (`%(asctime)s - %(name)s - %(levelname)s - %(message)s`), in MSK.

**Secret hygiene:** `httpx` and `httpcore` loggers are forced to `WARNING` at startup (see `news_bot._configure_third_party_logging`) because their INFO-level records include full URLs — and Telegram Bot API puts the bot token directly in the URL path (`/bot<TOKEN>/sendMessage`). Without the suppression, every send would leak the token into journal. Regression test: `tests/test_no_token_leak_in_logs.py`.

### Error Tracking

**Tool:** None
**Config:** Not configured

### Health Checks

**Endpoint:** None (no web server)
**Checks:** Manual verification via Telegram channel posts and log inspection.

**Heartbeat watchdog (`watchdog.sh`).** `job()` writes a Unix-timestamp heartbeat
at the end of every tick (`_record_heartbeat`) to `HEARTBEAT_FILE` (env-overridable;
prod = `/data/last_tick.ts` on the mounted volume, default `~/.cache/news_bot/last_tick.ts`).
`watchdog.sh` runs once daily AFTER the publish window; if the heartbeat is older than
26 h it sends `[E099]` via the Telegram Bot API. Catches the **alive-but-stuck** class
(prod 2026-06-08 feedparser hang) that `Restart=on-failure` / `restart: unless-stopped`
cannot — a hung process stays running, so nothing restarts it, but the heartbeat goes stale.
`watchdog.sh` reads config from a colocated `.env` if present, else from the environment
(so it runs both on the NL host and inside the Moscow container).

**Prod (Moscow):** host cron runs it via `docker exec` (config inherited from the
container's env; heartbeat on `/data`). Installed 2026-07-07 at `0 22 * * *` (host UTC =
01:00 МСК):
`docker exec hw-news-bot /bin/bash /app/watchdog.sh`. **Test (NL):** host cron
`/home/hwbot/bot_test/watchdog.sh` (reads the colocated `.env` + `~/.cache` heartbeat).
Neither cron is managed by the deploy — installed once per host.

### Metrics

**Analytics:** None
**Key metrics:** N/A

### Alerts

**Tool:** None
**Rules:** N/A

---

## Backups

`news.db` holds three load-bearing tables (`pending_articles`, `published_articles`, `processed_news`) — losing it means the bot re-publishes every URL the RSS feeds still hold (typically months of backlog). Prod state is a **single copy** on the `./data` bind-mount, so a daily backup is load-bearing:

`scripts/backup_db.sh` uses atomic `sqlite3 .backup` (consistent under concurrent
writes), path-parameterized via `DB_FILE`/`$1`/`BACKUP_DIR` (2026-07-07), with an
empty-DB guard and 7-day rotation. Each host cron is installed once (NOT deploy-managed);
requires the `sqlite3` CLI on the host.

- **Prod (Moscow host):** backs up the bind-mount DB directly on the host (`.backup`
  stays consistent while the container writes). Installed 2026-07-07 at `0 2 * * *`
  (host UTC = 05:00 МСК, before the 10:00 tick):
  `DB_FILE=/root/hw-news/data/news.db BACKUP_DIR=/root/hw-news/backups /bin/bash /root/hw-news/scripts/backup_db.sh`.
  **TODO: copy `/root/hw-news/backups` OFF-box** (single-host copies only, so far).
- **Test (NL host):** legacy `/home/hwbot/bot_test/backup_db.sh` → `/home/hwbot/backup/`.

**Restore (prod):** stop the container, replace the DB on the mounted volume, start it —
`docker compose stop news-bot; cp /root/hw-news/backups/news_<DATE>.db /root/hw-news/data/news.db; docker compose up -d`.

**Manual merge** (e.g. recovering history from a different machine's `news.db` after migration):
```sql
ATTACH '/tmp/other_news.db' AS other;
INSERT OR IGNORE INTO processed_news SELECT * FROM other.processed_news;
DELETE FROM pending_articles WHERE link IN (SELECT link FROM processed_news);
DETACH other;
```

---

## Cost Monitoring

Production runs the auto-publish path through **OpenRouter** (`LLM_PROVIDER=openrouter`).
**Prod model (2026-07-20): `google/gemini-2.5-flash`** — hand-set in the prod `.env`
(`OPENROUTER_MODEL=google/gemini-2.5-flash`), switched from `openai/gpt-5.4-mini` for a
cheaper-but-still-strong-RU transcreation (Claude judged too costly for this hobby-volume
bot). The **code default and CI default var both remain `openai/gpt-5.4-mini`** — so the
**test bot still runs gpt-5.4-mini** (no `vars.OPENROUTER_MODEL` set; the prod override lives
only in the hand-managed prod `.env`, not in the repo). To change the model: prod = edit the
prod `.env` line + rebuild (outside the window); test = set repo var `OPENROUTER_MODEL` +
redeploy. Reverting is one line. The dispatcher (`llm_transcreation.py`) auto-selects an
engine in priority order Anthropic → OpenAI → Gemini → OpenRouter based on which API keys are
present, but the operator override via `LLM_PROVIDER` env var pins it. Variant B+ second-pass adds ~$0.005 per long autoevolution article (when `blocks=null` triggers the focused caption-translation call).

**Where to watch:** https://openrouter.ai/activity → daily breakdown by model. (For the legacy Anthropic path: https://console.anthropic.com → Usage.)

**Expected cost.** Prod runs **`google/gemini-2.5-flash`** via OpenRouter — an `OPENROUTER_MODEL` override in the hand-managed server `.env`, NOT the code default (`openai/gpt-5.4-mini`, `openrouter_transcreation._DEFAULT_MODEL`). Measured from prod logs 2026-07-28: **total input 6,727 tokens** on a short article and **7,723** on a long one, output 345 and 1,586 respectively. Since the article itself is the only other input, the system prompt (`ux-guidelines.md`) is **~6k tokens** and the article contributes ~700–1,700. Prompt caching is intentionally NOT used (slot interval ≥ 40 min ≫ 5-min cache TTL — Decision 6).

> **System-prompt size is a live cost line — re-measure when you edit the prompt.** `_build_system_prompt` ships the file's ENTIRE body on every article, so every paragraph added there is billed per article forever. History of this figure: `~3,200` (May 2026, when the file was 13.3 KB) → stale as it grew to 23.4 KB → briefly `~7–8k` on 2026-07-28 from a character-count estimate → now **~6k, measured** against the `input_tokens` the engine logs in prod. **How to re-measure without guessing:** `ssh root@45.90.216.165 "docker logs hw-news-bot | grep input_tokens | tail -5"` and subtract the article. Do that rather than estimating from file size — the character-count method overshot by ~25%.
>
> **Do NOT prune the prompt to save tokens.** Operator decision 2026-07-28: «не экономь токены на перевод». Translation quality is the product; at ~6k tokens and ~7 articles/day the whole system prompt costs cents a month, and a rule dropped to save tokens costs a visibly worse post. Measure the size so cost is not a *surprise*, not so it can be minimised. Remove a rule only when it is wrong or superseded — never because the file got long.

**Sonnet 4.6 override:** ~$15/month for higher quality. Set `ANTHROPIC_MODEL=claude-sonnet-4-6` in repo `vars` and redeploy.

**Sanity threshold:** if daily cost exceeds **~$1/day** for more than a week, something is wrong. Likely cause: a source has gone runaway (loops, dupes through dedup) and `pending_articles` is full. The bot caps publishes at 11/day so token cost is bounded, but the AC20 admin-warning at `len(pending) > 50` should already have fired. Operator can `sqlite3 news.db 'SELECT COUNT(*) FROM pending_articles'` on the server to confirm and `DELETE` problematic rows manually.

**Per-call observability** (AC19): every Claude call logs `input_tokens`, `output_tokens`, `latency_ms`, `model_version` at INFO. Cross-check against Anthropic console without instrumentation:

```
ssh user@vps "journalctl -u newsbot -S today | grep 'transcreate.*tokens'"
```
