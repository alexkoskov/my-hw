---
created: 2026-04-26
status: draft
type: feature
size: L
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
3. Recompute schedule: `compute_publish_slots(N=5, now=16:00, window_end=20:00, min_interval=40)` → interval = max(240/5, 40) = max(48, 40) = 48 мин → 5 слотов: 16:00, 16:48, 17:36, 18:24, 19:12. carry_over=0.
4. Уже опубликованные сегодня в `published_articles` не трогаются (Decision 9 idempotency: `telegraph_url` already set → skip republish).

## Критерии приёмки

**Расписание:**

- [ ] AC1. Cron срабатывает один раз в сутки в 12:00 МСК (а не каждые 12 часов с произвольной отсчётной точки).
- [ ] AC2. После фетча отправляется admin-ping оператору с числом новых статей, размером очереди и timestamps запланированных публикаций.
- [ ] AC3. Публикации происходят строго в окне 13:00–20:00 МСК. До 13:00 или после 20:00 ни одна публикация не происходит.
- [ ] AC4. Минимальный интервал между публикациями = 40 минут. Расчётный интервал capped at 40 если расчётное значение меньше.
- [ ] AC5. Максимум 11 публикаций в день. Излишек carry over в pending до следующего дня.
- [ ] AC6. Алгоритм распределения: для N статей в очереди после фетча, equally spaced в окне с floor=40 минут. Конкретные комбинации (N=7 → 7 постов через час; N=15 → 11 сегодня + 4 завтра) отражены в edge cases.
- [ ] AC7. Container restart mid-window: бот перерасчитывает расписание из текущего времени до 20:00 МСК и оставшегося pending. Уже опубликованные сегодня (по `published_articles`) не дублируются.
- [ ] AC8. Crash-loop guard: на startup бот не публикует следующую статью раньше чем через 40 минут после последней опубликованной. Защита от burst при многократных рестартах.

**Перевод:**

- [ ] AC9. Auto-публикации идут через Claude API с промптом из `ux-guidelines.md` (тем же, что использует manual-review путь).
- [ ] AC10. Возвращаемая Claude транскреация содержит RU title с emoji prefix, 2-3 alt titles, RU subtitle (если EN был), и RU paragraphs (или blocks для autoevolution).
- [ ] AC11. Title всегда содержит emoji prefix. Если Claude не вставил — добавляется regex wrapper'ом как safety net.
- [ ] AC12. Hot Wheels-глоссарий (брендовые термины и идиомы) применяется как post-pass safety net на выходе Claude. Bureaucratic regex (канцелярит) удалён — Claude сам не пишет его.
- [ ] AC13. Body на Telegraph публикуется без обрезки (отменена 4000-char truncation; Telegraph принимает любой объём).

**Outage protocol (распознаём API-level vs per-article):**

- [ ] AC14. **API-level outage** (auth error, rate-limit, network timeout, server error от Claude API) запускает 2-ping protocol: Ping #1 сразу + Ping #2 через 1 час + переключение на Google Translate через 2 часа от первой ошибки. Все события сохраняют timestamps в state БД.
- [ ] AC15. **Per-article problem** (Claude refuse'ит конкретный article — safety filter; malformed JSON; нерелевантный output) — fallback'ит ТОЛЬКО эту статью на Google Translate. Не запускает outage protocol. Бот продолжает следующие публикации через Claude.
- [ ] AC16. Auto-recovery: на каждой следующей попытке публикации после API-level outage бот сначала пробует Claude. Success → switch-back ping оператору («✓ Claude API recovered») + clear outage state.
- [ ] AC17. Edge case (outage clears + queue empty в этот момент): бот остаётся в Google fallback mode до следующего cron tick'а. Recovery probe выполняется на первой публикации после 12:00 МСК.

**Visibility и observability:**

- [ ] AC18. Все auto-published Telegraph-страницы получают одинаковый маркер `↳ автоперевод` независимо от engine (Claude или Google Translate fallback).
- [ ] AC19. Logs содержат рудиментарную observability на каждый Claude API call: input/output token counts, latency, model version. Это позволяет оператору проверять cost вручную через сравнение с Anthropic console.
- [ ] AC20. Backlog admin-ping: если `len(pending_articles) > 50`, после фетча оператор получает дополнительное warning «pending очередь большая (N), проверь источники». Защита от silent runaway.

**Совместимость:**

- [ ] AC21. Manual-review путь (`hw_review` CLI) остаётся unchanged. Если оператор публикует статью локально, бот пропускает её на следующем cron tick'е.
- [ ] AC22. Channel teaser format остаётся `#<source> #news` (one line, byte-identical для обоих путей). Decision 14 из manual-review-workflow tech-spec preserved.
- [ ] AC23. Boilerplate filter, image policy, hashtag derivation, telegraph node tree builder — все без изменений.

**Migration и cleanup:**

- [ ] AC24. SQLite migration: новая таблица для outage state создаётся idempotent'но при первом cron-тике после deploy. Существующие таблицы не трогаются.
- [ ] AC25. ANTHROPIC_API_KEY редактируется в логах (как сейчас редактируется TELEGRAM_BOT_TOKEN).
- [ ] AC26. Legacy code удалён: overflow fast-track functionality, inline idle-fallback, throttle между сessions, неактуальные env vars (`QUEUE_CAP`, `IDLE_TIMEOUT_HOURS`, `GRACE_WINDOW_HOURS`, `FALLBACK_THROTTLE_SECONDS`). `.env.example` обновлён.
- [ ] AC27. `ux-guidelines.md` добавлена в deploy bundle (manual `deploy.sh` и GitHub Actions workflow). На сервере файл должен присутствовать к моменту первого Claude API call.
- [ ] AC28. Архитектурный сдвиг (ux-guidelines.md теперь runtime cron-side dependency, не только operator-side) задокументирован в `architecture.md`.

**Verification:**

- [ ] AC29. `pytest tests/ -q` зелёный после фичи. Test inventory delta учитывается (старые legacy-tests удалены, новые добавлены для distributed schedule, claude transcreation, outage state, recovery).
- [ ] AC30. Manual smoke pre-deploy: вызов claude transcreation против sample article возвращает валидный RU-словарь за разумное время (<30 секунд для типичной статьи).
- [ ] AC31. Manual smoke post-deploy: первый 12:00 МСК cron tick после deploy → admin-ping приходит → 13:00 первая публикация → Telegraph-страница соответствует AC10/AC11/AC18 (emoji-title, без boilerplate, с маркером перед футером).

## Ограничения

- **Совместимость с manual-review путём.** `hw_review` CLI не меняется. Оператор продолжает работать локально через свою Claude Code сессию точно так же. Auto и manual пути читают/пишут одну и ту же БД с фильтрами по статусу перевода и source flag'у.
- **Channel teaser format locked.** Тизер в канале `#<source> #news` (single line, byte-identical для обоих путей) — Decision 14 из manual-review-workflow tech-spec. Не меняем.
- **Telegraph article body format locked.** `↳ автоперевод` маркер только в auto-fallback, перед `Источник:` футером. Тот же формат после фичи. Маркер одинаковый независимо от engine (Claude или Google Translate).
- **Boilerplate filter, image policy, hashtag derivation** — все unchanged. Уже работают, не трогаем.
- **Без новых тяжёлых зависимостей.** Только `anthropic` Python SDK. Все остальное — stdlib либо уже в `requirements.txt`.
- **Manual-review путь имеет приоритет.** Если оператор в момент scheduled публикации руками опубликовал ту же статью — бот её скипает.
- **Cron container timezone.** Cron должен срабатывать в 12:00 МСК. Реализация может использовать либо TZ-aware schedule (если поддерживается scheduler-библиотекой), либо UTC-equivalent (MSK = UTC+3 круглогодично с 2014 в РФ). Startup проверяет конфигурацию и предупреждает оператора если несоответствие.
- **Outage state persists across restart.** Контейнер может рестартнуть во время outage — состояние ping'ов сохраняется в БД и при возобновлении бот не сбрасывает счётчик.
- **Cost ~ $3/месяц** при дефолтной модели Claude Haiku 4.5 и ~10 articles/day. Sonnet 4.6 — ~$15/месяц для лучшего качества (override через env var). Operator выбирает.
- **Архитектурный сдвиг: ux-guidelines.md становится runtime cron-side dependency.** Ранее этот файл был только operator-side (загружался в Claude Code сессии). Теперь его читает Claude API call на сервере. Соответствующее изменение нужно зафиксировать в `architecture.md`.
- **Без миграции данных.** Существующие `pending_articles` от старого QUEUE_CAP=10 model становятся input для нового distributed schedule на первом cron-тике после deploy.
- **Минимальная schema migration.** Одна новая таблица для outage state, idempotent.
- **Backlog не имеет hard cap.** Алгоритм distribute сам ограничивает 11 публикаций/день (floor=40). Pending может расти — operator получает warning при `>50` (AC20).

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
- Мы решили **outage protocol с 2 пингами + 2ч grace** перед auto-Google-Translate, потому что оператор должен иметь шанс исправить проблему (пополнить токены, etc) до того как канал переключится на хуже-качественный fallback.
- Мы решили **passive recovery** (бот пробует Claude на следующей scheduled публикации) вместо активного health-check'а, потому что edge case (outage + empty queue) очень редкий, max delay 24ч приемлемо, +0.7$ в месяц за health-check ping не оправдан.
- Мы решили **crash-loop guard через MAX(published_at) check** на startup, потому что 5 строк кода + 1 тест защищают канал от burst при многократных рестартах.
- Мы решили **single uniform marker `↳ автоперевод`** для Claude и Google fallback одинаково, потому что подписчику разница не важна; оператор узнаёт через admin-пинги.
- Мы решили **HW glossary keep как post-pass safety net** (брендовые термины и идиомы), потому что Claude может перевести «гараж» дословно вместо «гаражный проект»; bureaucratic regex удаляем — Claude сам не пишет канцелярит.
- Мы решили **outage state persisted в БД** (новая таблица), не in-memory, потому что контейнер может рестартнуть посреди outage и состояние ping'ов должно survive. Schedule же в памяти — recomputed каждый cron tick (no migration needed for it).
- Мы решили **отменить 4000-char body truncation** везде, потому что body публикуется на Telegraph (без лимита), а в канал летит только тизер `#<source> #news` (~30 символов). Truncation — пережиток pre-Telegraph эпохи.
- Мы решили **delete legacy auto-publish code** полностью (overflow fast-track, inline idle-fallback, throttle между batch'ами), потому что новый distributed schedule делает их избыточными.
- Мы решили **ux-guidelines.md ship в deploy bundle**, потому что Claude API нужен этот промпт; альтернатива (хардкодить в Python) — дубль источника правды. Это инвертирует прежнюю operator-side-only convention для этого файла — фиксируем в architecture.md.
- Мы решили **различать API-level outage и per-article failure**: catastrophic API errors (auth/rate-limit/network) запускают 2-ping protocol; per-article problems (refusal/malformed JSON) fallback'ятся только для этой статьи. Иначе один странный article мог бы триггернуть ложный outage state.

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
