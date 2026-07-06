# Чеклист переезда на вечер 2026-07-06 (копипаст)

Основано на `MIGRATION-docker-vpn-2026-07-03.md`. Здесь — только команды по
порядку, где их выполнять и что ждать в ответ. Значения (IP, пути) уже подставлены.

> **СТАТУС 2026-07-06 ~16:40 МСК: Часть A ВЫПОЛНЕНА и проверена.**
> - A0 ✅ VPN пускает Telegram (выход через Швецию; telegram 302, telegra.ph 200).
> - A1 ✅ код на московском сервере, ветка `dev` @ `fe0b917`, фикс базы на месте,
>   `data/` создана.
> - A2 ✅ `.env` (559 B) перенесён С MAC (в dev-контейнере старый сервер оказался
>   со сменённым host-ключом — легитимная ротация, Mac ему доверяет; перенос
>   сделан с Mac). `DB_FILE=/data/news.db` дописан, `TELEGRAPH_ACCESS_TOKEN` есть,
>   `INSTANCE_LABEL=prod`.
> - Нюанс `.env`: `LLM_PROVIDER` ОТСУТСТВУЕТ — это ОК. Диспетчер авто-выбирает
>   провайдера по наличию ключа; из ключей есть только `OPENROUTER_API_KEY` →
>   openrouter (как на проде). Менять ничего не надо.
> - **Осталась Часть B — только после 20:00 МСК.** Все команды B — с MAC (там
>   доверенны и старый NL, и московский сервер).

**Три машины в игре:**
- 💻 **Mac** — ваш ноутбук, с него есть SSH к обоим серверам.
- 🟢 **NL (старый)** — `hwbot@148.135.207.54`, где бот работает сейчас.
- 🔴 **Москва (новый)** — `root@45.90.216.165`, куда переезжаем (Docker + VPN).

**Правило времени:** Часть B (сам переезд с перезапуском) — только **после 20:00
МСК** (когда пост 19:30 уже вышел) или **до 10:00 МСК**. Часть A безопасна в любое
время — она не трогает работающего бота.

---

## ЧАСТЬ A — подготовка (можно сделать СЕЙЧАС, прод не трогается)

### A0 · Проверить, что Telegram достаётся через VPN — 💻 Mac
```bash
ssh root@45.90.216.165 'docker run --rm --network vpnnet --cap-add NET_ADMIN alpine:latest sh -c "ip route replace default via 172.28.0.2 >/dev/null 2>&1; apk add --no-cache curl >/dev/null 2>&1; echo EXIT_IP:; curl -s --max-time 15 http://ip-api.com/json/?fields=query,country,city; echo; for u in https://api.telegram.org https://api.telegra.ph; do curl -s --max-time 15 -o /dev/null -w \"%{http_code}  \$u\n\" \"\$u\"; done"'
```
✅ Ждём: страна выхода **НЕ Russia**, и telegram/telegra.ph отдают HTTP-код (не `000`).
🛑 Если `000` — стоп, туннель не работает, зовите меня.

### A1 · Забрать код + Docker-файлы на московский сервер — 🔴 Москва
```bash
ssh root@45.90.216.165
git clone https://github.com/alexkoskov/my-hw.git /root/hw-news
cd /root/hw-news && git checkout dev
mkdir -p /root/hw-news/data
```
Затем (там же, на московском сервере) — проверка, что фикс базы на месте:
```bash
grep -q 'os.getenv("DB_FILE"' /root/hw-news/news_bot.py \
  && echo 'DB_FILE env fix present — OK' \
  || echo 'ABORT: DB fix missing — run `git pull` on dev before continuing'
```
✅ Ждём: `DB_FILE env fix present — OK`.
🛑 Если `ABORT...` — на московском сервере `cd /root/hw-news && git pull`, повторить grep.

### A2 · Перенести секреты (.env) со старого сервера на новый — 💻 Mac
```bash
scp hwbot@148.135.207.54:/home/hwbot/bot/.env /tmp/hwenv
scp /tmp/hwenv root@45.90.216.165:/root/hw-news/.env
rm /tmp/hwenv
```
Дописать путь к базе внутри контейнера — 🔴 Москва:
```bash
ssh root@45.90.216.165 "grep -q '^DB_FILE=' /root/hw-news/.env || echo 'DB_FILE=/data/news.db' >> /root/hw-news/.env"
```
Проверить, что токен Telegraph на месте (без него контейнер молча падает в цикле) — 🔴 Москва:
```bash
ssh root@45.90.216.165 "grep -q '^TELEGRAPH_ACCESS_TOKEN=..*' /root/hw-news/.env && echo 'TELEGRAPH_ACCESS_TOKEN present — OK' || echo 'ABORT: TELEGRAPH_ACCESS_TOKEN missing/empty'"
```
✅ Ждём: `TELEGRAPH_ACCESS_TOKEN present — OK`.
🛑 Если `ABORT...` — зовите меня, разберёмся с токеном перед Частью B.

**После Части A всё готово. Ждём 20:00 МСК.**

---

## ЧАСТЬ B — сам переезд (ТОЛЬКО после 20:00 МСК)

### B3 · Cutover — заморозить старого, скопировать свежую базу, запустить новый
Заморозить старого бота на NL (чтобы в канал постил только один) — 💻 Mac:
```bash
ssh hwbot@148.135.207.54 'kill -STOP $(systemctl show -p MainPID --value news_bot.service) && echo OLD_FROZEN'
```
✅ Ждём: `OLD_FROZEN`.

Скопировать самую свежую базу со старого на новый (через Mac) — 💻 Mac:
```bash
scp hwbot@148.135.207.54:/home/hwbot/bot/news.db /tmp/news.db
scp /tmp/news.db root@45.90.216.165:/root/hw-news/data/news.db && rm /tmp/news.db
```

Собрать и запустить контейнер — 🔴 Москва:
```bash
ssh root@45.90.216.165 'cd /root/hw-news && docker compose up -d --build'
```

### B4 · Проверить, что новый бот жив — 🔴 Москва
```bash
ssh root@45.90.216.165 'cd /root/hw-news && docker compose ps && docker logs --tail 40 hw-news-bot'
```
✅ Ждём: `Starting daily cron tick`, админ-пинг `[E008]`/`[E009]` в Telegram
(INSTANCE_LABEL=prod), и при наличии свежих новостей — `Posted to Telegram` +
пост в канале.
🛑 Если контейнер не поднялся / нет пинга — зовите меня, лог покажет причину.
Старого бота НЕ размораживаем, пока новый не подтверждён.

### B5 · Завершение
- Новый бот подтверждённо постит → старый на NL остаётся замороженным.
- ⚠️ `kill -STOP` НЕ переживает перезагрузку: если NL-машина ребутнётся, systemd
  поднимет бота снова → два бота постят в один канал. Поэтому как только Москва
  подтверждена — **выключить NL-машину из панели DeluxHost** (выключенная не
  ребутнётся сама), позже отменить VPS.
- GitHub `SSH_HOST` пока не трогаем. Редеплой теперь = на московском сервере:
  `cd /root/hw-news && git pull && docker compose up -d --build`.

---

## Стоп-условия (когда звать меня, не продолжать)
- A0 вернул `000` (VPN не пускает).
- A1 grep вернул `ABORT` и `git pull` не помог.
- A2 нет токена Telegraph.
- B4 контейнер не поднялся или нет админ-пинга в Telegram.
Во всех случаях: старый бот либо ещё работает (Часть A), либо заморожен, но живой
(его можно вернуть: `ssh hwbot@148.135.207.54 'kill -CONT $(systemctl show -p MainPID --value news_bot.service)'`).
