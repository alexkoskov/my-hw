# Deployment & Operations

## Purpose
Deployment process, infrastructure, and production operations for AI agents.

---

> **Status (2026-08-03):** production is a **single** Docker container
> `hw-news-bot` on the Moscow VPS `45.90.216.165` (repo `/root/hw-news`, tracking
> **`main`**; cutover 2026-07-06). Egress routes through the shared `shared-vpn`
> gateway (sing-box VLESS, `172.28.0.2` on the external `vpnnet` network) so the RU
> host reaches Telegram — same pattern as the colocated intake-bot. Compose
> (`docker-compose.yml`): `news-bot` + a `route-setup` sidecar that points the
> default route at the gateway. State lives on a host bind-mount (`./data:/data`):
> `.env` (secrets, hand-managed on the host) + `data/news.db`
> (`DB_FILE=/data/news.db`).
>
> **⚠️ There is exactly ONE instance and it is production. No test bot, no
> staging, no test channel.** Deploy is **manual** and must run **outside the
> 10:00–20:00 МСК publish window**. Both GitHub-Actions deploy workflows are
> disarmed: `.github/workflows/deploy.yml:30` and
> `.github/workflows/deploy_test.yml:26` are `if: false`; a push to `dev`/`main`
> runs `ci.yml` (pytest) only and never touches a server. Everything operational
> lives in the **Серверная шпаргалка** immediately below — read that first.
>
> **History (reference only — NOT runnable).** Prod used to be a `systemd` unit
> (`news_bot.service`) on the Netherlands VPS `148.135.207.54`, with a second
> `news_bot_test.service` instance on the same box as staging. Prod moved to
> Moscow/Docker 2026-07-06; `deploy.yml` was disarmed 2026-07-07 (re-running it
> would have restarted — and thus revived — the disabled NL prod bot →
> double-post); the NL box was decommissioned and `deploy_test.yml` disarmed
> 2026-07-25. The NL IP has since been reassigned by the hoster to another
> customer — never connect to it. Runbooks and decision records:
> `work/MIGRATION-new-server-2026-06-30.md`,
> `work/MIGRATION-docker-vpn-2026-07-03.md`,
> `work/CUTOVER-CHECKLIST-2026-07-06.md`, `work/SESSION-2026-07-25.md`. Why egress
> must leave RU: [[project_host_outside_russia]].

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
callout + Серверная шпаргалка). The app is still a single long-running Python process
(`schedule` in-process, no web framework) — it just runs inside a container now, with
state on the `./data:/data` bind-mount.

**Why:** the RU host reaches Telegram only through the VPN, and Docker gives a
reproducible image + isolated egress routing without changing the bot code.

---

## Access Information

**SSH Access:** `ssh root@45.90.216.165` (Moscow VPS, password auth) — the only host. Repo/deploy dir: `/root/hw-news`; state on `/root/hw-news/data`. This is the sole SSH target in the project; the old NL address `148.135.207.54` now answers with a stranger's sshd (see Серверная шпаргалка).

> Operator runs all server-side ops (SSH, deploy, restart); Claude prepares the commands.

**Credentials:** in the operator's password manager. The bot secrets live only in the server's `.env` (never committed).

---

## Environment Variables

**Canonical list with comments:** [.env.example](../../../../.env.example) in the project root. Only the production-relevant subset is summarised here.

**Required in the prod `.env`:**

- `TELEGRAM_BOT_TOKEN` — bot token from BotFather. Sensitive — never echo/log. Code suppresses `httpx`/`httpcore` INFO to prevent URL-path leak.
- `TELEGRAM_CHANNEL_ID` — numeric channel id; prod = `-1004027529994` (see Серверная шпаргалка). A `@username` form also works for public channels.
- `TELEGRAM_ADMIN_ID` — personal Telegram **numeric** chat_id for admin pings (daily schedule, outage protocol, backlog warning, error digests). The operator must `/start` the bot once for DMs to work. ⚠️ Leaving it unset does not fail loudly: the code falls back to the placeholder `'@sunny413x'` (`news_bot.py:106`), which the Bot API cannot resolve to a private chat — pings are attempted and silently lost, and the review-button listener fail-closes on the non-numeric value.
- `TELEGRAPH_ACCESS_TOKEN` — auto-created by `telegraph_publisher.ensure_access_token` on first run and persisted to `.env` (`telegraph_publisher.py:32`). Also needed as a **GitHub secret** by the uptime watchdog — see § Deployment Triggers → GitHub Secrets.
- `OPENROUTER_API_KEY` — the LLM key production actually runs on. Alias `OPEN_ROUTER_API_KEY` is accepted (`openrouter_transcreation.py:83`, `_API_KEY_ENV_VARS`). **Sensitive** — never echo/log/commit; redacted from stored errors and admin pings via `news_bot._SECRET_ENV_NAMES` (`news_bot.py:229-236`). Without a working LLM key the first API call fails → 2-ping outage protocol → posts are HELD (hold-and-wait, 2026-06-11), nothing published until it recovers.
- `TZ=Europe/Moscow` — process timezone. The daily tick fires at 10:00 МСК via `pytz`-aware `schedule.every().day.at("10:00", tz=...)` (`news_bot.py:4641`), so it's correct regardless of `TZ`. But `os.getenv('TZ')` is checked at startup (Decision 14 health check #2, `news_bot.py:4604`) — if it doesn't equal `'Europe/Moscow'`, the bot sends an admin warning ping ("log timestamps may show non-MSK times"). Note: the Moscow **host** is on UTC; only the container is MSK.

**Optional (tunable defaults):**

- `LLM_PROVIDER` — pins the engine (`openai` | `claude` | `gemini` | `openrouter`); read at `llm_transcreation.py:70`. **Prod sets `openrouter`.** Unset/blank → auto-selection by key presence, in the order **openai → claude → gemini → openrouter** (`llm_transcreation._auto_select_by_key_presence`, `llm_transcreation.py:46-62`) — so dropping any other engine's key into the prod `.env` would silently move production off OpenRouter if the pin were ever removed.
- `OPENROUTER_MODEL` — model spec, `provider/model` form. Code default `openai/gpt-5.4-mini` (`openrouter_transcreation.py:80`); **prod overrides it in the hand-managed `.env`** — see § Cost Monitoring for the current value and the reasoning.
- `OPENROUTER_BASE_URL` — OpenRouter API base; only for pointing at a proxy. Documented in `.env.example`.
- `OPENROUTER_MIN_BALANCE_USD` — threshold for the `[E019]` low-balance admin ping (`news_bot.py:1458`).
- `DEDUP_SERIES_ENABLED` — series/theme pair-rule in the dedup gate. **Default ON** (unset/blank → enabled; only `0/false/no/off` disable it, `news_bot.py:134-136`). See § Feature rollout: `dedup-model-series`.
- `REVIEW_BUTTONS_ENABLED` — review buttons under the `[E014]` dedup ping. **Default OFF** — only an explicit on-word (`1/true/yes/on`) enables it (`news_bot.py:149-151`); prod sets `=1`. Fail-closed double gate: flag + non-empty `TELEGRAM_BOT_TOKEN` + **numeric** `TELEGRAM_ADMIN_ID` enables BOTH the background `get_updates` listener AND keyboard rendering at the `[E014]` send site — an instance that can't listen renders no buttons. Only one process may poll a given bot token (a second poller gets HTTP 409); with a single instance there is nothing to conflict with, but the flag remains the feature's off-switch. See § Feature rollout: dedup-review-buttons.
- `INSTANCE_LABEL` — short label prepended by `send_admin_notification` to every admin-bound message (`news_bot.py:109`). Empty/unset → no prefix. Prod = `prod`; also drives the startup DB re-flood guard below.
- `DB_FILE` — SQLite path (`news_bot.py:120`). **The prod container MUST set `DB_FILE=/data/news.db`** so state lands on the mounted volume, not the ephemeral image layer. Default `"news.db"` (relative) is the local-dev behaviour. A prod instance with a relative `DB_FILE` or an empty `processed_news` triggers a startup `[E018]` admin ping (re-flood guard).
- `HEARTBEAT_FILE` — heartbeat marker path (`news_bot.py:4526`). The prod container sets `/data/last_tick.ts` via `docker-compose.yml:34` (persistent + readable by the `docker exec` watchdog). Default `~/.cache/news_bot/last_tick.ts`.
- `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` (model default `claude-haiku-4-5`, `claude_transcreation.py:380`) — alternate engine, **not used in production**. Only relevant if `LLM_PROVIDER` is repointed at `claude`. The API key is **sensitive** and is additionally redacted from logs by `_TokenRedactingFilter` (pattern `sk-ant-[A-Za-z0-9_=.-]{16,}`). `OPENAI_API_KEY` / `OPENAI_MODEL` and `GEMINI_API_KEY` / `GEMINI_MODEL` are the same story for the other two engines.

**How env reaches prod:** `docker-compose.yml` (`env_file: .env` + `environment: HEARTBEAT_FILE`). The prod `.env` is **hand-edited on the Moscow host** — nothing writes it, and no CI touches it. Changing any variable therefore means: edit `/root/hw-news/.env` by hand, then rebuild (`docker compose up -d --build`) outside the publish window. The archived `hw_review.py` would read a local `.env` if revived (dormant).

---

## Single instance — no staging

There is **one** bot: production, the Docker container on the Moscow VPS. All of its
coordinates (host, login, paths, branch, channel, deploy line, backup and watchdog
crons) are in the **Серверная шпаргалка** above — that table is the single source of
truth, not repeated here.

**Consequences for the way work ships:**

- **There is nowhere to try a change "live."** Verification before prod is `pytest`
  locally; verification after prod is reading `docker logs hw-news-bot` and the next
  publications. Plan every rollout so it can be undone (see § Rollback Procedure).
- **`dev` is not a deploy target.** `git push origin dev` runs `ci.yml` (pytest) and
  nothing else. Promotion is a merge into `main`, then a manual rebuild on the host,
  outside the publish window.
- **`INSTANCE_LABEL=prod` still prefixes every admin ping**, so a ping without the
  prefix means someone is running the bot outside the container (local dev) — worth
  noticing.

*History: from 2026-05 to 2026-07-25 there were two instances (prod + a
`news_bot_test.service` staging bot posting to `@myhwchannel123`), first colocated on
the NL VPS and then split across hosts. Both halves are gone; see the History note in
the Status callout for the runbooks.*

## Deployment Triggers

**Production — MANUAL, no CI:** the only way code reaches prod is the operator
running, outside the 10:00–20:00 МСК window:

`ssh root@45.90.216.165 "cd /root/hw-news && git pull && docker compose up -d --build"`

`git push` to any branch triggers `ci.yml` (pytest) only. Both deploy workflows are
disarmed (`deploy.yml:30`, `deploy_test.yml:26` — `if: false`) and target a host that
no longer exists; they are kept as the skeleton for a future Docker-targeted CI, which
remains a TODO.

**GitHub Secrets** (Settings → Secrets and variables → Actions):

*Live — used by `.github/workflows/uptime.yml`; missing values degrade their own
evidence or notification path without changing the independent SSH verdict (see
§ Monitoring → External uptime watch):*
- `TELEGRAPH_ACCESS_TOKEN` — same value as the server `.env`, but exposed only to
  the dedicated evidence-fetch step. It is not available to checkout, the repository
  classifier or alert bookkeeping. Missing token, API failure or unusable evidence
  yields publication `inconclusive`; it is never treated as healthy recovery.
- `TELEGRAM_BOT_TOKEN` — used to send the outage/recovery message.
- `TELEGRAM_ADMIN_ID` — numeric chat id the message goes to. With either of the last
  two unset, the Telegram send is skipped and only GitHub issue state is updated.

The prod IP the watchdog probes is **not** a secret — it is hardcoded as `PROD_HOST`
in the workflow.

*Dead — left over from the NL deploy workflows, unusable while those are `if: false`;
delete them when convenient:* `SSH_HOST`, `SSH_USER`, `DEPLOY_PATH`,
`DEPLOY_PATH_TEST`, `SSH_PRIVATE_KEY`, `ANTHROPIC_API_KEY`, and the repo `vars`
`ANTHROPIC_MODEL` / `TZ`.

**`deploy.sh` (SCP path) — not used today.** It predates Docker and copies a file list
to a server over `scp`. It stays in the repo as the machine-readable deploy manifest
(see "Files deployed" below) and as the bootstrap route if a new non-Docker host is
ever stood up. It does NOT touch `.env`.

**Files deployed — do not maintain a second copy here.** The manifest is the `FILES`
array in [deploy.sh](../../../../deploy.sh) (25 entries as of 2026-08-03): `news_bot.py`
plus every first-party module it imports at startup, plus `feeds.json`,
`requirements.txt`, `.env.example`, `watchdog.sh` and the LLM system prompt. Read it
there; any enumeration in prose goes stale within a month. The same
array is duplicated in `.github/workflows/deploy.yml` and
`.github/workflows/deploy_test.yml` (three copies, currently identical), but both
workflows are disarmed, so `deploy.sh` is the live one.

Two things about that manifest that the array itself does not say:

- **Files deliberately NOT deployed:** `hw_review.py`, `preview_renderer.py` — the
  manual review path, archived 2026-04-30. Code preserved and tests green for ad-hoc
  revival, but it would run in the operator's local session, never on the server.
- **The LLM system prompt** (`.claude/skills/project-knowledge/references/ux-guidelines.md`)
  ships as a manifest entry, and `scp` is invoked without `-r`, so on the SCP path it
  lands flattened at `$DEPLOY_PATH/ux-guidelines.md`. The loader tries the subdir path
  first and falls back to the flat filename, so both layouts work (Decision 8). On the
  **current Docker path** the file arrives inside the repo checkout at its normal
  subdir path and the fallback never fires. Practical consequence:
  **editing the prompt is a production behaviour change and needs a rebuild** —
  outside the publish window, like any restart. Prompt-only edits ship no code, so
  there is nothing to verify beyond reading the next few publications; LLM output
  cannot be asserted by tests.

**How strong is the invariant, really?** Weaker than it looks, in both directions.
`tests/test_deploy_files_invariant.py` does not compare the three arrays — it asserts
that two hardcoded strings (`"t_hunted_source.py"`, `"watchdog.sh"`) appear in each
file, so a newly added module passes CI unnoticed. And prod does not depend on the
manifest at all: the image is built with `COPY . .` (`Dockerfile:18`), so everything
in the repo ships regardless. The manifest matters only if the SCP path is revived for
a future host — keep it in sync when adding a first-party import, but do not treat the
test as a guard rail.

**Restarting the bot:** `docker compose up -d --build` in `/root/hw-news` rebuilds the
image and recreates the container; code changes go live immediately rather than
waiting for the next 10:00 МСК tick. There is **no privileged step** — the operator is
`root` on the host, there is no `hwbot` user, no `systemd` unit and no `sudoers` rule
anywhere in the current setup. (Until 2026-07-25 the NL host used a narrowly scoped
`/etc/sudoers.d/news_bot` rule for `systemctl restart`, hardened 2026-07-07 after an
S2 privilege-escalation review; the record is in `work/AUDIT-2026-07-07.md:38-39` and
`:117`. Nothing from it applies to the Docker host.)

---

## Scheduling

Daily tick at **10:00 МСК** via `schedule.every().day.at("10:00", tz=pytz.timezone("Europe/Moscow")).do(job)` inside `news_bot.main()`. One `job()` also fires immediately on startup so a deploy doesn't wait until tomorrow's 10:00. The crash-loop guard prevents burst posting on rapid restarts.

**Production scheduler = fixed daily slots** (`compute_fixed_slots`, operator pacing 2026-06-13): at most one post at each of **10:00 / 15:00 / 19:30 МСК**, ≤3/day. A slot more than 5 min past is dropped (grace), so a restart never re-fires an already-published slot. A deferred/held backlog may keep the job alive until later fixed times as conditional release checks even when the operator-facing plan is empty; those checks do not add slots or promised posts. The canonical planning and queue-state flow is in architecture.md § Data Flow.

**Restart-safe by design:** `schedule` runs in-process, so a container restart kills the in-flight `job()`. But `main()` re-runs `job()` immediately on boot, which recomputes the publishable plan and conditional opportunities from a fresh backlog snapshot and today's still-eligible times. Already-published times are dropped by grace, and already-published rows are skipped by the idempotency guard. Verified live on the 2026-07-06 mid-window cutover.

### ⛔ The no-deploy window: 10:00–20:00 МСК

**Never rebuild or restart the container between 10:00 and 20:00 МСК.** Not a style
rule — a mechanism:

- The whole day's schedule lives inside the process (`schedule`, in-memory). A
  container restart destroys it.
- `main()` calls `job()` **immediately** on start (`news_bot.py:4645`, right after the
  in-process schedule registration at `:4641`), so the bot does not wait for the
  next 10:00 — it
  re-plans the day from `now` and publishes into whatever is left of the window.
- The result is a rebuild silently re-shaping today's publications: slots already
  passed are dropped by the 5-minute grace, remaining ones fire on a compressed
  timeline. Nothing crashes and nothing double-posts (idempotency holds), which is
  precisely why the effect is easy to miss.

Deploy before 10:00 or after 20:00 МСК. **The host clock is UTC**, so 10:00–20:00 МСК
= **07:00–17:00 on the host** — check with `date` on the server, not the wall clock in
Moscow, when scripting anything.

---

## Pre-Deploy Checklist

- [ ] `python3 -m pytest tests/ -q` green locally. This is the ONLY pre-prod gate — there is no staging to catch what it misses.
- [ ] Merged into **`main`** (prod deploys from `main`, never from `dev`).
- [ ] Note the current prod commit **before** deploying, so a rollback has a target: `ssh root@45.90.216.165 "cd /root/hw-news && git rev-parse --short HEAD"`.
- [ ] Clock check — outside the 10:00–20:00 МСК window (see § Scheduling → The no-deploy window).
- [ ] Deploy: `ssh root@45.90.216.165 "cd /root/hw-news && git pull && docker compose up -d --build"`.
- [ ] Post-deploy logs: `ssh root@45.90.216.165 "docker logs hw-news-bot --tail 200"` — clean boot, no `[E018]` DB-guard ping, `[E008]` plan-of-day sent.
- [ ] `news.db` present on the mounted volume (`/root/hw-news/data/news.db`). A fresh instance auto-creates it via `init_db()`; the schema is idempotent (see `tests/test_migration.py`).

The prod `.env` (hand-managed, never rewritten by anything) carries `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_ID`, `TELEGRAPH_ACCESS_TOKEN`, `OPENROUTER_API_KEY`, `LLM_PROVIDER=openrouter`, `OPENROUTER_MODEL`, `INSTANCE_LABEL=prod`, `TZ=Europe/Moscow`, `REVIEW_BUTTONS_ENABLED=1` and **`DB_FILE=/data/news.db`**; compose injects `HEARTBEAT_FILE`.

---

## Feature rollout: `dedup-model-series` (cold-DB warm-up + `DEDUP_SERIES_ENABLED` toggle)

One-time staged rollout for the tiered series/theme pair-rule (the layer that
hard-blocks cross-source pop-culture dupes — K-Pop Demon Hunters, Stranger
Things, Top Gun). This is a **feature-specific procedure on top of** the general
Pre-Deploy Checklist above — do not skip that; this only adds the cold-DB
warm-up + dark-deploy sequence. **Operator applies all server commands; Claude
only prepares them.**

**This rollout already happened (2026-07-20, self-warm instead of backfill — see the
superseded note below).** Keep the sequence as the template for the next gated
feature and as the record of why it was staged this way; do not re-run it.

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

Confirm the base is cold — one line, from the operator's own machine:

`ssh root@45.90.216.165 "sqlite3 /root/hw-news/data/news.db \"SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL\""`

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
3. **Build & restart** — `ssh root@45.90.216.165 "cd /root/hw-news && git pull && docker compose up -d --build"`.
   `init_db()` adds the `model_fingerprint` column if the snapshot lacked it
   (idempotent).
4. **Warm-up backfill** — `ssh root@45.90.216.165 "cd /root/hw-news && docker compose exec -T news-bot python3 backfill_fingerprints.py --days 30"`
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

**Already applied on prod** (`REVIEW_BUTTONS_ENABLED=1`, per the Серверная
шпаргалка). The steps below are kept as the procedure for re-enabling after a `.env`
rebuild or on a new host — not something to run again now.

> **⚠️ One bot token, ONE poller.** Telegram allows exactly one `get_updates`
> consumer per bot token — a second poller gets HTTP 409. With a single instance
> there is nothing to collide with, but if a second copy of the bot is ever started
> anywhere (a local run against the prod token, a future staging box), only ONE of
> them may have this flag on.

> **⚠️ Rebuild OUTSIDE the 10:00–20:00 МСК publish window.**
> `docker compose up -d --build` restarts the container and resets the in-process
> daily schedule (slots 10:00 / 15:00 / **19:30**). The Moscow host is on UTC.

### Enable (prod, Moscow host)

1. **Pre-check admin id** — the hand-managed prod `.env` must have a **numeric**
   `TELEGRAM_ADMIN_ID` (personal chat_id, not `@username`). Non-numeric →
   fail-closed: the listener refuses to start and logs a startup warning.
2. **Set the flag** — in the hand-managed prod `.env` add `REVIEW_BUTTONS_ENABLED=1`.
3. **Rebuild** — outside the window: `ssh root@45.90.216.165 "cd /root/hw-news && git pull && docker compose up -d --build"`.
4. **Verify the listener** — `ssh root@45.90.216.165 "docker logs hw-news-bot --tail 200"`
   must show the startup line **«review listener active»**. Missing line = gate closed
   (flag off or non-numeric admin id) — check the `.env`.
5. **Check for 409s** — no HTTP 409 in the logs; a 409 means a second process is
   polling the same bot token, and both listeners become unreliable until one stops.
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

This section is the **runtime Docker rollback**. It does not roll back the external
GitHub Actions watcher; that independent contour is documented under Monitoring →
External uptime watch → Activation and rollback boundaries.

Two routes. **Route A (host-side, fast)** gets prod back to a working state without
touching git history — use it during an incident. **Route B (revert on `main`)** makes
the fix permanent. Doing A then B is normal.

### Route A — put a known-good commit on prod (fastest, no push needed)

Find the commit prod should go back to (the previous deploy's SHA, noted in the
Pre-Deploy Checklist, or from `git log`), then, **outside the 10:00–20:00 МСК window**:

`ssh root@45.90.216.165 "cd /root/hw-news && git fetch && git checkout <good-sha> && docker compose up -d --build"`

This leaves the checkout in **detached HEAD** — a normal state meaning "sitting on a
specific commit instead of following a branch". The container does not care; nothing
else on the host does either. Verify with `docker logs hw-news-bot --tail 200`. When
the fix has landed on `main`, return the host to the branch:

`ssh root@45.90.216.165 "cd /root/hw-news && git checkout main && git pull && docker compose up -d --build"`

~3–4 min including the image build.

### Route B — revert the bad change on `main`

⚠️ **`git revert HEAD` on `main` usually fails, and this is expected.** Promotion from
`dev` to `main` is done with a merge, so `main`'s HEAD is normally a **merge commit**
(two parents) — `git revert` refuses it with *"commit is a merge but no -m option was
given"* because it cannot guess which of the two histories to treat as "the line we
stayed on".

Tell it explicitly with `-m 1`, which means "keep the first parent — the `main` side —
and undo everything the merge brought in from the other branch":

`git revert -m 1 <merge-sha> && git push origin main`

For an ordinary non-merge commit, plain `git revert <sha>` is still correct; `-m 1` on
a non-merge commit is an error, so check first with
`git rev-list --parents -n 1 <sha>` — three SHAs on the line means it is a merge, two
means it is not.

Then redeploy the host as in Route A (`git checkout main` if it was detached, else
`git pull`) plus `docker compose up -d --build`, outside the window.

**Known trap after reverting a merge:** git now considers the branch already merged, so
simply re-merging `dev` later will **not** bring the reverted work back. When the fix
is ready, revert the revert (`git revert <revert-sha>`) or rebuild the change on a
fresh branch — do not expect a plain re-merge to restore it.

### DB rollback

Restore `news.db` from a backup (see § Backups): stop the container, replace
`/root/hw-news/data/news.db`, start it. Losing or truncating this file makes the bot
re-publish months of backlog — treat it as the most dangerous object on the host.

---

## Monitoring & Observability

### Logging

**Where:** stdout, read with `ssh root@45.90.216.165 "docker logs hw-news-bot --tail 200"` (json-file driver, 10 MB × 3 rotation — roughly the last few days).
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
`watchdog.sh` reads config from a colocated `.env` if present, else from the environment.

Host cron runs it via `docker exec` (config inherited from the container's env;
heartbeat on `/data`). Installed 2026-07-07 at `0 22 * * *` (host UTC = 01:00 МСК):
`docker exec hw-news-bot /bin/bash /app/watchdog.sh`. The cron is NOT managed by the
deploy — it was installed once on the host and survives rebuilds.

**Blind spot:** this watchdog runs *on the machine it watches*, so it is silent by
construction when the host itself stops serving — which is exactly what happened on
2026-07-31. That gap is what the external watch below covers.

### External uptime watch (GitHub Actions)

`.github/workflows/uptime.yml` is the **second pair of eyes**, and the only monitoring
that survives the prod host going away. It runs on GitHub's runners and maintains two
independent signals:

1. **Host signal:** `ssh-keyscan` must complete the sshd protocol greeting. It uses no
   credentials and retries three times. This is deliberately stronger than a TCP port
   check: during the 2026-07-31 outage port 22 accepted connections while sshd never
   answered.
2. **Publication signal:** bounded Telegra.ph API evidence is classified as `fresh`,
   `stale` or `inconclusive`. This measures the bot's externally visible outcome; the
   private channel's public web stub is not usable evidence.

**Immutable source and secret boundaries.** The checkout action is pinned to a full
commit SHA and explicitly resolves `github.event.repository.default_branch`, with a
shallow checkout and no persisted credentials. The repository classifier runs only
after that checkout succeeds. A separate fetch step alone receives
`TELEGRAPH_ACCESS_TOKEN`; it writes at most 1 MiB of evidence to a temporary runner
file and exposes neither the token nor raw evidence through logs or step outputs.
`publication_watch.py` receives only that file on stdin, so repository code never sees
the secret.

**Classifier contract.** The stdlib-only CLI accepts at most 1 MiB of UTF-8 Telegraph
JSON and emits exactly one line: `fresh`, `stale` or `inconclusive`. It validates a
successful non-empty page-list response and dates the newest path against one
timezone-aware Moscow clock. A page from today is fresh; yesterday remains fresh until
21:00 МСК and becomes stale at that boundary; older evidence is stale. Invalid,
ambiguous, future-dated, missing, oversized or failed evidence is inconclusive. The
workflow also normalizes a skipped, failed, timed-out or unexpectedly noisy classifier
to inconclusive without printing repository-controlled output.

**State transitions.** With the host up, `stale` opens the separate «🟡 Прод не
публикует» issue/alarm, `fresh` closes an existing one, and `inconclusive`
leaves either open or closed state unchanged. A missing secret, checkout failure, API
failure, malformed response or classifier failure therefore cannot manufacture a
recovery. The SSH verdict remains available in every case. When the host is down, the
host alarm is handled normally and all publication transitions are suppressed because
host failure already explains the silence.

**Alert dedup:** each signal has its own open GitHub issue as durable state: «🔴 Прод
не отвечает» for the host and «🟡 Прод не публикует» for publication
silence. Each sends one Telegram message on opening and one on recovery, with silence
between runs. Closing an issue by hand while its failure remains causes the next
conclusive run to alert again.

**Cadence and limits:** `cron: */30` is best-effort and may slip by 5–15 minutes.
GitHub disables scheduled workflows after 60 days without repository activity, and a
GitHub outage also removes this external observer.

#### Activation and rollback boundaries

- **Watcher workflow:** GitHub schedules execute the workflow from the repository's
  default branch. Merging the workflow/helper change there activates it for subsequent
  runs without a VPS checkout, Docker rebuild or server restart. Roll it back by
  reverting the watcher change in the default branch; this does not alter the running
  bot container.
- **Runtime scheduler:** merging code does not deploy the bot. Scheduler changes reach
  the single production container only after promotion to `main` and a separate
  operator-run `git pull` plus Docker rebuild outside 10:00–20:00 МСК. Roll them back
  with Route A or Route B above and rebuild the container. A runtime rollback does not
  change the GitHub watcher, and a watcher revert does not change runtime code.

**Verifying it is armed:** a future manual run (`Actions → Uptime watch → Run
workflow`) should log `sshd answered` or the bounded retry failure, plus exactly one
`publication state: fresh`, `publication state: stale` or `publication state:
inconclusive` verdict. Missing Telegraph credentials should log that evidence is
unavailable and end in inconclusive; the SSH signal remains active and the publication
issue remains unchanged.

### Metrics

**Analytics:** None
**Key metrics:** N/A

### Alerts

No third-party alerting tool. Everything reaches the operator as a Telegram DM, from
three independent senders: the bot itself (`[E0xx]` admin pings, in-process), the
on-host `watchdog.sh` (`[E099]`, stale heartbeat), and `uptime.yml` (`[watch]`, host or
publication silence). The first two die with the host; only the third does not.

---

## Backups

`news.db` holds three load-bearing tables (`pending_articles`, `published_articles`, `processed_news`) — losing it means the bot re-publishes every URL the RSS feeds still hold (typically months of backlog). Prod state is a **single copy** on the `./data` bind-mount, so a daily backup is load-bearing:

`scripts/backup_db.sh` uses atomic `sqlite3 .backup` (consistent under concurrent
writes), path-parameterized via `DB_FILE`/`$1`/`BACKUP_DIR` (2026-07-07), with an
empty-DB guard and 7-day rotation. The cron is installed once on the host (NOT
deploy-managed); requires the `sqlite3` CLI on the host.

- **Prod (Moscow host):** backs up the bind-mount DB directly on the host (`.backup`
  stays consistent while the container writes). Installed 2026-07-07 at `0 2 * * *`
  (host UTC = 05:00 МСК, before the 10:00 tick):
  `DB_FILE=/root/hw-news/data/news.db BACKUP_DIR=/root/hw-news/backups /bin/bash /root/hw-news/scripts/backup_db.sh`.
  **TODO: copy `/root/hw-news/backups` OFF-box** (single-host copies only, so far).

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
bot). The **code default stays `openai/gpt-5.4-mini`** (`openrouter_transcreation.py:80`);
the prod override lives only in the hand-managed `.env`, not in the repo, so a fresh
checkout run anywhere else uses gpt-5.4-mini. To change the model: edit the `.env` line
on the host and rebuild, outside the window. Reverting is one line.

The dispatcher (`llm_transcreation.py`) picks an engine from `LLM_PROVIDER` when set —
prod pins `openrouter`, so key-presence order never applies there. With `LLM_PROVIDER`
unset it falls back to key presence in the order **openai → claude → gemini →
openrouter** (`llm_transcreation.py:46-62`); note that is *not* Anthropic-first, and
adding an unrelated key to the prod `.env` would move production off OpenRouter if the
pin were ever dropped.

Variant B+ second-pass adds ~$0.005 per long autoevolution article (when `blocks=null` triggers the focused caption-translation call).

**Where to watch:** https://openrouter.ai/activity → daily breakdown by model. (For the legacy Anthropic path: https://console.anthropic.com → Usage.)

**Expected cost.** Prod runs **`google/gemini-2.5-flash`** via OpenRouter — an `OPENROUTER_MODEL` override in the hand-managed server `.env`, NOT the code default (`openai/gpt-5.4-mini`, `openrouter_transcreation._DEFAULT_MODEL`). Measured from prod logs 2026-07-28: **total input 6,727 tokens** on a short article and **7,723** on a long one, output 345 and 1,586 respectively. Since the article itself is the only other input, the system prompt (`ux-guidelines.md`) is **~6k tokens** and the article contributes ~700–1,700. Prompt caching is intentionally NOT used (slot interval ≥ 40 min ≫ 5-min cache TTL — Decision 6).

> **System-prompt size is a live cost line — re-measure when you edit the prompt.** `_build_system_prompt` ships the file's ENTIRE body on every article, so every paragraph added there is billed per article forever. History of this figure: `~3,200` (May 2026, when the file was 13.3 KB) → stale as it grew to 23.4 KB → briefly `~7–8k` on 2026-07-28 from a character-count estimate → now **~6k, measured** against the `input_tokens` the engine logs in prod. **How to re-measure without guessing:** `ssh root@45.90.216.165 "docker logs hw-news-bot | grep input_tokens | tail -5"` and subtract the article. Do that rather than estimating from file size — the character-count method overshot by ~25%.
>
> **Do NOT prune the prompt to save tokens.** Operator decision 2026-07-28: «не экономь токены на перевод». Translation quality is the product; at ~6k tokens and **at most 3 articles a day** the whole system prompt costs cents a month, and a rule dropped to save tokens costs a visibly worse post. Measure the size so cost is not a *surprise*, not so it can be minimised. Remove a rule only when it is wrong or superseded — never because the file got long.

**Sonnet 4.6 override:** ~$15/month for higher quality — but production is not on the
Anthropic engine at all, so switching means setting **both** `LLM_PROVIDER=claude` and
`ANTHROPIC_MODEL=claude-sonnet-4-6` (plus `ANTHROPIC_API_KEY`) by hand in the prod
`.env`, then rebuilding outside the window. The repo `vars` route described here
before is dead — no workflow writes the server `.env` any more.

**Sanity threshold.** The hard ceiling is `MAX_DAILY_POSTS = 3` (`news_bot.py:172`) —
one publication per fixed slot — so a normal day is **at most 3 calls**, measured at
6.7k–7.7k input and 0.3k–1.6k output tokens each: on the order of **25k input tokens a
day**, i.e. cents per month at gemini-2.5-flash rates. Check
https://openrouter.ai/activity → daily breakdown by model against that budget. *The
old rule of thumb here («more than ~$1/day for a week») was derived from a wrong
11-posts-a-day figure and sits roughly an order of magnitude above the real ceiling —
it would not trip on a genuine runaway.* A day materially above the 3-call budget
means the slot cap is being bypassed or a call is looping on retry: read
`ssh root@45.90.216.165 "docker logs hw-news-bot | grep input_tokens | tail -20"` for
repeated calls on the same article, and
`ssh root@45.90.216.165 "sqlite3 /root/hw-news/data/news.db 'SELECT COUNT(*) FROM pending_articles'"`
for a flooded queue — though the AC20 admin warning at `len(pending) > 50` should have
fired first.

**Per-call observability** (AC19): every LLM call logs `model`, `input_tokens`,
`output_tokens` and latency at INFO (for the production engine,
`openrouter_transcreation.py:453-461`). Read today's calls with:

`ssh root@45.90.216.165 "docker logs hw-news-bot --since 24h | grep input_tokens"`
