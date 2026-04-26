---
created: 2026-04-26
status: draft
type: feature
size: M
---

# User Spec: llm-transcreation-and-distributed-publishing

## Что делаем

Заменяем автоматический путь публикации Hot Wheels-новостей в Telegram-канал. Сегодня бот переводит статьи через Google Translate и постит их в канал «всплесками» — overflow или idle-fallback может выложить 5-20 постов за 8 минут с машинной стилистикой. Подписчики видят разницу между ручными постами оператора (живой русский, редакторский тон) и авто-постами (дословный перевод, канцелярит).

Фича объединяет две части:

**A. LLM-транскреация.** Auto-fallback путь начинает переводить через Claude API по `ux-guidelines.md` — тот же промпт, что использует оператор в локальном `hw_review` сеансе. Качество выравнивается с ручным путём. Google Translate остаётся как fallback на случай 2-часовой недоступности Claude API.

**B. Распределённое расписание.** Cron строго в 12:00 МСК один раз в день фетчит источники и считает расписание. Публикации происходят в окне 13:00–20:00 МСК с интервалом не меньше 40 минут (формула `interval = max(420 / N, 40)`). Максимум 11 постов в день; излишек переносится на следующий день. Никаких burst'ов, всё ровно по дню.

Manual-review путь (`hw_review`) не меняется — оператор в любой момент может локально через свою Claude Code сессию взять статью и опубликовать её сам.

## Зачем

**Качество.** Auto-fallback посты сегодня видимо хуже чем manual. Google Translate выдаёт «была украшена», «был выпущен в количестве», подписчики раздражаются. Claude по `ux-guidelines.md` пишет в редакторском стиле — активные глаголы, цепкие заголовки, идиомы. Канал перестаёт выглядеть фрагментированным.

**Поведение в ленте.** Сейчас при backlog'е (кто-то не следил неделю или источник дал 25 статей) бот выкладывает их пачкой за минуты — в ленте подписчика 20 постов подряд за 10 минут — это спам, отписки. Распределённое расписание гладит подачу: ровно 7 часов с интервалом не меньше 40 минут, в прайм-окно 13–20 МСК. Если backlog большой — растекается на несколько дней.

**Operator workload.** После фичи оператор получает один admin-пинг в сутки в 12:00 МСК с количеством новых статей и расписанием. Дальше — только мониторинг (если Claude API упал — два пинга и инструкция; если HW-релиз от Mattel — приятный пинг). Ручное вмешательство опционально, не обязательно.

## Как должно работать

**Suite of daily events:**

1. **12:00 МСК — cron tick.** Бот фетчит autoevolution + lamley + mattel, дедупит против `processed_news` и `pending_articles`, инсертит новые в `pending`. Шлёт оператору admin-пинг: «Зафетчил N новых статей, в очереди M, расписание: 13:00, 13:40, ..., 19:40».

2. **13:00 МСК — окно открывается.** Бот рассчитывает `compute_publish_slots(N, now=13:00, window_end=20:00, min_interval=40)` где N = текущая длина `pending_articles`. Возвращает список из max(N, 11) datetime-слотов.

3. **13:00, 13:40, 14:20, ... — каждый scheduled слот:** бот берёт самый старый pending row без `ru_paragraphs` → переводит через Claude API по `ux-guidelines.md` → публикует на Telegraph + шлёт teaser в канал. Telegraph-страница содержит маркер `↳ автоперевод` непосредственно перед футером «Источник:».

4. **20:00 МСК — окно закрывается.** Все опубликованные сегодня лежат в `published_articles`, оставшиеся (carry-over) сидят в `pending` до завтрашних 12:00.

5. **На следующий день в 12:00 МСК** — фетч + новые статьи добавляются к carry-over. N = full pending после фетча. Алгоритм рассчитывает заново.

**Manual-review путь (без изменений):**

Оператор в любой момент может локально через `hw_review list / show / stage / preview / publish` взять конкретную статью, перевести её сам через свою Claude Code сессию и опубликовать с пометкой `via_review=1`. Бот эту статью пропустит на следующем тике (фильтр `ru_paragraphs IS NOT NULL`).

**Claude API outage protocol:**

Если в момент scheduled публикации Claude API даёт ошибку (auth, rate-limit, network, server):

1. Бот записывает `claude_api_outage_started_at` в `bot_state` SQLite.
2. **Ping #1** оператору: «Claude API недоступен: <тип ошибки>. Если кончились токены — пополни на console.anthropic.com. Через 1 час повтор пинга.» Текущая публикация откладывается.
3. **Через 1 час — Ping #2:** «Claude API всё ещё down. Через 1 час переключусь на Google Translate fallback.»
4. **Через 2 часа от начала outage:** бот переключается на Google Translate (`transcreate_text`). Все откладываемые + текущие scheduled публикации идут через Google. Маркер `↳ автоперевод` остаётся одинаковым.
5. **Auto-recovery:** на каждой следующей попытке scheduled публикации бот сначала пробует Claude API. Если успех → переключается обратно. Шлёт **switch-back ping** «✓ Claude API recovered, switching back from Google Translate fallback.» Очищает outage state.
6. **Edge case** (outage clears + queue empty в этот момент): бот остаётся в Google fallback режиме до следующего 12:00 МСК cron-тика. Max delay = 24 часа. На первой публикации после cron-тика — Claude probe + recovery.

**Container restart mid-window:**

Если контейнер перезапустился в 16:00 МСК (deploy / VPS reboot) с 5 unpublished:
1. Бот startup → читает `pending_articles`.
2. Crash-loop guard: читает `MAX(published_at)` из `published_articles`. Если < 40 минут назад → ждёт до `last_published_at + 40min` перед первой публикацией.
3. Recompute schedule: `compute_publish_slots(N=5, now=16:00, window_end=20:00, min_interval=40)` → 4 слота (interval 60 мин: 16:00, 17:00, 18:00, 19:00) + 1 carry-over.
4. Уже опубликованные сегодня в `published_articles` не трогаются (Decision 9 idempotency: `telegraph_url` already set → skip republish).

## Критерии приёмки

- [ ] AC1. Cron в `news_bot.main()` использует `schedule.every().day.at("12:00")` + `TZ=Europe/Moscow` env var вместо `every(12).hours`.
- [ ] AC2. После 12:00 МСК фетча отправляется admin-ping с числом новых статей, размером очереди, и timestamps расписания публикаций.
- [ ] AC3. Алгоритм `compute_publish_slots(N, now, window_start, window_end, min_interval=40)` возвращает список datetime слотов: `interval = max((window_end - max(now, window_start)) / N, min_interval)`, `posts_today = min(N, floor(remaining_minutes / interval) + 1)`, `carry_over = N - posts_today`.
- [ ] AC4. Публикации происходят строго в окне 13:00–20:00 МСК. Никаких публикаций до 13:00 или после 20:00.
- [ ] AC5. Минимальный интервал между постами = 40 минут.
- [ ] AC6. Max 11 публикаций в день (floor=40, 7 часов = 420 мин). Excess carry over в pending.
- [ ] AC7. Translation primary path: Claude API через `ux-guidelines.md` prompt. Возвращает RU-словарь с title (с emoji prefix), 2-3 alt titles, subtitle (если есть EN), paragraphs (или blocks для autoevolution).
- [ ] AC8. Title emoji prefix: эмитится через Claude prompt. Если Claude не вставил emoji — regex wrapper добавляет (belt-and-suspenders).
- [ ] AC9. HW-глоссарий (14 терминов из текущего `transcreate_text`) применяется как post-pass safety net на ru-выходе Claude. Bureaucratic regex (19 правил) удалён.
- [ ] AC10. 4000-char body truncation удалена везде (Claude path и Google Translate fallback). Body на Telegraph без ограничений.
- [ ] AC11. Claude API outage detection: при exception от anthropic SDK (auth, rate-limit, network, server, timeout) бот пишет `claude_api_outage_started_at` в `bot_state`.
- [ ] AC12. Outage protocol: Ping #1 при первой ошибке + Ping #2 через 1 час + переключение на Google Translate через 2 часа от начала outage. Все три события сохраняют timestamps в `bot_state`.
- [ ] AC13. Auto-recovery: на каждой следующей попытке публикации бот сначала пробует Claude. Success → switch-back ping + clear outage state. Edge case (empty queue at recovery): wait until next cron tick.
- [ ] AC14. Все auto-published Telegraph-страницы получают одинаковый маркер `↳ автоперевод` независимо от engine (Claude или Google fallback).
- [ ] AC15. Manual-review путь (`hw_review`) untouched. Если оператор публикует статью локально, бот пропускает на следующем тике (filter `ru_paragraphs IS NOT NULL`).
- [ ] AC16. Container restart mid-window: на startup бот recompute_schedule с current_time и оставшимся pending. Идемпотентность Telegraph URL (Decision 9) предотвращает дубли.
- [ ] AC17. Crash-loop guard: на startup бот читает `MAX(published_at)` из `published_articles`. Если < 40 минут назад → пропускает текущую scheduled публикацию, ждёт до `last_published_at + 40min`.
- [ ] AC18. SQLite migration: новая таблица `bot_state(key TEXT PRIMARY KEY, value TEXT)` создаётся в `init_db()` через `CREATE TABLE IF NOT EXISTS`. Idempotent.
- [ ] AC19. Token redaction: `_TokenRedactingFilter` extends с pattern `sk-ant-[A-Za-z0-9_-]{20,}` для редактирования `ANTHROPIC_API_KEY` в логах.
- [ ] AC20. Legacy removed: `QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `FALLBACK_THROTTLE_SECONDS` env vars + их use sites; `_overflow_fast_track` функция (lines ~799-1009); inline idle-fallback в `job()` step 1a/1b; bureaucratic regex (19 правил) в `transcreate_text`. `.env.example` обновлён.
- [ ] AC21. `ux-guidelines.md` добавлена в `deploy.sh` FILES list И в `.github/workflows/deploy.yml` files block — файл должен оказаться на сервере для работы Claude transcreation.
- [ ] AC22. `pytest tests/ -q` зелёный после фичи. Removed: ~28 tests (overflow + idle-fallback + throttle, минус общие invariants). Added: ~30 new tests (compute_publish_slots, claude_transcreation client, outage_state, distributed_schedule integration).
- [ ] AC23. Manual smoke pre-deploy: `python3 -c "from claude_transcreation import transcreate_via_claude; print(transcreate_via_claude(<sample_article>))"` возвращает валидный RU-словарь за < 30 секунд.
- [ ] AC24. Manual smoke post-deploy: первый 12:00 МСК cron tick после deploy → admin-ping приходит → 13:00 первая публикация → Telegraph-страница с emoji-title, без boilerplate, с маркером `↳ автоперевод` перед футером.

## Ограничения

- **Совместимость с manual-review путём.** `hw_review` CLI не меняется. Оператор продолжает работать локально через свою Claude Code сессию точно так же. Auto и manual пути читают/пишут одну и ту же `pending_articles`/`published_articles` БД с фильтрами по `ru_paragraphs` и `via_review`.
- **Channel teaser format locked.** Тизер в канале `#<source> #news` (single line, byte-identical для обоих путей) — Decision 14 из manual-review-workflow tech-spec. Не меняем.
- **Telegraph article body format locked.** `↳ автоперевод` маркер только в auto-fallback, перед `Источник:` футером. Тот же формат после фичи.
- **Boilerplate filter, image policy, hashtag derivation** — все unchanged. `boilerplate_filter` уже фильтрует Share/Tweet/Subscribe и т.п.
- **Без новых тяжёлых зависимостей.** Только `anthropic` SDK добавляется в `requirements.txt`. `zoneinfo` — stdlib (Python 3.9+).
- **Manual-review путь имеет приоритет.** Если оператор в момент scheduled публикации руками опубликовал ту же статью через `hw_review` — бот видит `ru_paragraphs IS NOT NULL` и скипает.
- **Cron container TZ.** Должен быть `TZ=Europe/Moscow`. Иначе `schedule.every().day.at("12:00")` выстрелит в неправильное время. Startup проверяет `os.getenv("TZ") == "Europe/Moscow"`, иначе log warning + admin-ping.
- **Outage state persists across restart.** Если контейнер рестартует во время outage — `bot_state` SQLite сохраняет timestamps, ping count не сбрасывается.
- **Cost ~ $3/месяц при Haiku 4.5** при 10 articles/day. Sonnet 4.6 — ~$15/месяц для лучшего качества (override через `ANTHROPIC_MODEL` env var). Operator выбирает.
- **Без миграции данных.** Существующие `pending_articles` от старого QUEUE_CAP=10 model становятся input для нового distributed schedule на первом cron-тике после deploy.
- **Никаких изменений в `news.db` schema** кроме одной idempotent `CREATE TABLE IF NOT EXISTS bot_state(...)`.

## Риски

- **Риск 1: Operator inattention to outage pings.** Бот тихо переходит на Google Translate через 2ч. Подписчики видят посты в Google-качестве (~машинном), не сразу замечают разницу. **Митигация:** switch-back ping при восстановлении даёт visible signal что что-то было сломано. 24-часовой cron-тик в 12:00 гарантированно проверяет состояние Claude API хотя бы раз в день. Acceptable.
- **Риск 2: Cost spike.** Если источник зацикливается и фетч приносит 100+ статей в день, distribute сам ограничивает 11/день — токены тратятся ровно на 11 публикаций. Backlog в `pending` растёт неограниченно (без QUEUE_CAP). **Митигация:** дополнительный admin-ping когда `len(pending) > 50` — сигнал «pending большой, проверь источники». Включаем как minor enhancement в этой фиче.
- **Риск 3: Claude model output drift.** Anthropic меняет defaults — Claude становится менее агрессивным в редакторстве, тон меняется. **Митигация:** explicit version-pin в `ANTHROPIC_MODEL` (e.g. `claude-haiku-4-5-20251001`), не использовать алиас. Operator контролирует когда мигрировать на новые версии.
- **Риск 4: TZ misconfiguration.** Cron fires в UTC time, посты в неправильное окно (15:00 МСК если TZ не задан). **Митигация:** startup проверяет `os.environ.get('TZ') == 'Europe/Moscow'`, иначе warning в log + admin-ping. Документация `deployment.md` обновлена.
- **Риск 5: SQLite bot_state migration corruption.** Если auto-migrate на сервере по какой-то причине fail'нет — бот crash на startup. **Митигация:** `CREATE TABLE IF NOT EXISTS` идемпотентен; покрываем `tests/test_migration.py`. На первом запуске ловим exception → admin-ping вместо silent crash.
- **Риск 6: ux-guidelines.md не доехала до сервера.** Если deploy bundle не включил файл, Claude получает дефолтный prompt → перевод не по стилю. **Митигация:** AC21 + startup check файла. Fail fast с admin-ping «ux-guidelines.md missing on server, falling back to Google Translate» вместо silent quality regression.

## Технические решения

- Мы решили **подключить Claude API как primary translator** для auto-fallback пути, потому что качество перевода через ux-guidelines.md видимо лучше чем Google Translate, и оператор хочет одинаковую стилистику для manual и auto постов. Cost ~$3/мес — копейки.
- Мы решили **оставить Google Translate как fallback** только для outage ситуаций, потому что отключать его полностью — рисковать остановкой канала при недоступности Claude API. Bypass через 2-часовой grace.
- Мы решили **`schedule.every().day.at("12:00")` с `TZ=Europe/Moscow`** вместо `every(12).hours`, потому что фиксированное время фетча — обязательное условие для предсказуемого 13–20 окна публикации.
- Мы решили **окно 13:00–20:00 МСК** (7 часов = 420 минут), потому что это прайм-аудитория канала Hot Wheels-подписчиков; ночные часы и раннее утро не нужны.
- Мы решили **interval = max(420/N, 40)** с минимумом 40 минут, потому что (а) равномерность во всём окне даёт subscriber-frendly темп, (б) меньше 40 мин = воспринимается как спам, (в) max 11 постов/день — ровный потолок.
- Мы решили **carry-over excess в pending** (никаких потерь), потому что backlog лучше растянуть на несколько дней чем дропать.
- Мы решили **outage protocol с 2 пингами + 2ч grace** перед auto-Gemini, потому что оператор должен иметь shanc исправить проблему (пополнить токены, etc) до того как канал переключится на хуже-качественный fallback.
- Мы решили **passive recovery** (бот пробует Claude на следующей scheduled публикации) вместо активного health-check'а, потому что edge case (outage + empty queue) очень редкий, max delay 24ч приемлемо, +0.7$ в месяц за health-check ping не оправдан.
- Мы решили **crash-loop guard через MAX(published_at) check** на startup, потому что 5 строк кода + 1 тест защищают канал от burst при многократных рестартах.
- Мы решили **single uniform marker `↳ автоперевод`** для Claude и Google fallback одинаково, потому что подписчику разница не важна; оператор узнаёт через admin-пинги.
- Мы решили **HW glossary (14 терминов) keep как post-pass safety net**, потому что Claude может перевести «гараж» дословно вместо «гаражный проект»; bureaucratic regex (19 правил) удаляем — Claude сам не пишет канцелярит.
- Мы решили **outage state в SQLite `bot_state` таблице**, не in-memory, потому что контейнер может рестартнуть посреди outage и состояние ping'ов должно survive. Schedule же в памяти — recomputed каждый cron tick.
- Мы решили **отменить 4000-char body truncation** везде, потому что body публикуется на Telegraph (без лимита), а в канал летит только тизер `#<source> #news` (~30 символов). Truncation — пережиток pre-Telegraph эпохи.
- Мы решили **delete `_overflow_fast_track`, idle-fallback, throttle code** полностью, потому что новый distributed schedule делает их избыточными (дренаж по одной статье в 40 мин = no overflow possible, no idle problem).
- Мы решили **ux-guidelines.md ship в deploy bundle**, потому что Claude API нужен этот промпт; альтернатива (хардкодить в Python) — дубль источника правды.

## Тестирование

**Unit-тесты:** делаются всегда, не обсуждаются. Размер фичи M, тестов планируется ~30:

- `tests/test_compute_publish_slots.py` — 12 тестов: N=0, N=1, N=4, N=7, N=10, N=11, N=15, N=20, N=30; container restart at 14:00/16:00/19:50 with various pending counts.
- `tests/test_claude_transcreation.py` — 8 тестов: success path, anthropic.RateLimitError, AuthenticationError, APIConnectionError, malformed JSON output, refusal, network timeout, ux-guidelines.md missing.
- `tests/test_outage_state.py` — 6 тестов: state machine transitions (no_outage → ping_1 → ping_2 → fallback → recovery → no_outage), persistence across restart, edge case "empty queue at recovery".
- `tests/test_distributed_schedule_integration.py` — 4 тестов: full cron tick → mocked fetch → schedule → mocked Claude publish → DB delta. Outage end-to-end. Container restart end-to-end. Manual operator publish mid-window.

**Интеграционные тесты:** делаем — обновляем `tests/test_integration.py` с новой `_fallback_publish` логикой. Тесты `tests/test_overflow.py` (~13) и `tests/test_idle_fallback.py` (~9) удаляются (legacy code они тестируют). `tests/test_fallback_throttle.py` (~6 of 11) удаляется (throttle-related), остальные 5 — общие invariants о fallback path — остаются с обновлёнными ассертами.

**E2E тесты:** не делаем. Live Anthropic API call из CI burn'ил бы токены без пользы. Manual smoke pre/post-deploy покрывает.

**Существующие тесты должны оставаться зелёными:** ~470 тестов (Mattel parser, hw_review, telegraph publisher, telegram, RSS feed iteration, boilerplate filter и т.д.).

## Как проверить

### Агент проверяет

| Шаг | Инструмент | Ожидаемый результат |
|-----|-----------|---------------------|
| 1. Полный тестовый набор | `pytest tests/ -q` | ~500 тестов зелёные (470 untouched + 30 новых − 28 deleted = ~472). |
| 2. Manual translate smoke | `python3 -c "from claude_transcreation import transcreate_via_claude; print(transcreate_via_claude({'title': '...', 'subtitle': '...', 'paragraphs': [...]}))"` | Возвращает dict с `ru_title` (с emoji prefix), `ru_alts` (2-3), `ru_subtitle`, `ru_paragraphs` (transcreation, не дословно). За < 30 секунд. |
| 3. Outage protocol drill | Запустить `_fallback_publish` с моками, где anthropic SDK всегда raise'ит RateLimitError. Проверить через 2 часа симулированного времени state transitions: ping#1 → ping#2 → fallback. | `bot_state.claude_api_outage_started_at` set; admin notifications mock получил 2 вызова с правильными текстами; 3-я попытка translate ушла через `transcreate_text` Google. |
| 4. Schedule recompute drill | Запустить `compute_publish_slots(N=5, now=16:00, window_end=20:00, min_interval=40)` | Возвращает 5 datetime слотов: 16:00, 16:48, 17:36, 18:24, 19:12. carry_over=0. |
| 5. Crash-loop guard test | Запустить distributed-schedule loop где `MAX(published_at)` = 5 минут назад. | Loop пропускает первую scheduled публикацию, ждёт до `last_published_at + 40 min` перед следующей попыткой. |
| 6. Token redaction test | Лог-сообщение содержит `sk-ant-abc123def456...` (mock key). Проверить что после фильтра в записанных логах ключ редактирован (например, `sk-ant-***REDACTED***`). | Filter работает на `_TokenRedactingFilter`. |
| 7. Smoke на live test channel post-deploy | После первого 12:00 МСК cron-тика на production: дождаться 13:00 → открыть Telegraph URL первой публикации. | Title с emoji, body без boilerplate (никаких `Share on Facebook` etc), маркер `↳ автоперевод` непосредственно перед футером `Источник:`. |

### Пользователь проверяет

- **Что:** в течение 7 дней наблюдать стабильность качества и расписания. **Как:** ежедневно в 12:00 проверять admin-пинг (есть ли он, что в нём); периодически открывать Telegraph-страницы из канала и оценивать качество транскреации. **Зачем:** Claude иногда выдаёт edge-case переводы (стилистический drift, отказ переводить какой-то конкретный article); надо ловить такие случаи руками.
- **Что:** outage drill (опционально). **Как:** временно убрать `ANTHROPIC_API_KEY` из `.env` сервера, дождаться следующего cron-тика, посмотреть что приходят оба ping'а в правильное время, через 2ч переключение на Google Translate происходит без crash'а. **Зачем:** убедиться что fallback chain работает в реальной среде, не только в моках.
- **Что:** cost monitoring в первый месяц после deploy. **Как:** смотреть console.anthropic.com → Usage → daily breakdown. **Зачем:** убедиться что cost действительно ~$3/мес, а не $30 — иначе значит модель/промпт сжигают токены, надо ревизия.
