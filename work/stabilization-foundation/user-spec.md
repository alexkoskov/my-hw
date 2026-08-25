---
# Creation date (YYYY-MM-DD)
created: 2026-08-25

# Status: draft | approved
status: draft

# Work type: feature | bug | refactoring
type: bug

# Feature size: S (1-3 files, local fix) | M (several components) | L (new architecture)
size: L
---

# User Spec: stabilization-foundation

## Что делаем

Закрываем три базовые дыры надёжности: делаем CI безусловным и изолированным от локальных секретов и внешней сети, запрещаем production-процессу стартовать с неверной конфигурацией, БД или VPN-маршрутом и исправляем lifecycle планировщика после блокирующего стартового тика.

Это не переписывание 5200-строчного `news_bot.py`. Пакет создаёт проверяемый фундамент, на котором последующие упрощения и отдельный надёжный delivery административных алертов можно делать без ложнозелёного CI, опасного старта и двойных тиков.

## Зачем

Оператор production-бота должен доверять результату CI и безопасно запускать или перезапускать контейнер, не проверяя вручную каждую скрытую зависимость от `.env`, сети, VPN route и состояния SQLite. Разработчик должен получать локальный красный тест вместо случайного вызова живого или платного API. Подписчики канала косвенно защищены от повторной обработки старых лент, дублей и аварийных публикаций после неверного старта.

Сейчас CI может пропустить тесты после tracked-изменения, а локальный `.env` способен скрыть порядок-зависимый тест или направить тестовый job в настоящий OpenRouter. В production Python стартует раньше, чем sidecar меняет default route; до слабой warning-only проверки БД процесс уже может создать SQLite-файл и обратиться к Telegraph, LLM или Telegram. Планировщик регистрирует cron до блокирующего immediate job: если job пересекает 10:00, просроченный слот выполняется сразу после него вторым тиком.

## Как должно работать

### Сценарий 1. Честный hermetic CI

1. Любое tracked-изменение в push на `main`/`dev` или pull request, нацеленный на `main`/`dev`, запускает полный обязательный test job. Тип файла и глубина commit history не могут превратить job в `skipped`.
2. Test job начинает работу без production-значений из runner или repository `.env`; dotenv отключён для тестового процесса до загрузки runtime-конфигурации.
3. Во время pytest недоступна внешняя сеть независимо от транспорта: Python HTTP-клиенты, нативный `curl_cffi` и случайно запущенный subprocess не могут выйти наружу. Loopback можно разрешить только явному тесту. Попытка внешнего вызова завершается локальной ошибкой и не достигает живого сервиса.
4. Regression-набор для известных process-global утечек — dotenv/import config, Telegraph access token и OpenRouter balance path внутри job — проходит отдельно, внутри своего файла, в двух противоположных порядках и в полном suite. Ни один из этих тестов не получает state от предыдущего.

### Сценарий 2. Fail-closed production startup

#### Route и production mode

1. Production-контейнер до запуска Python ждёт, пока точный default route будет направлен через VPN gateway `172.28.0.2`. Ожидание ограничено по времени; timeout, неверный gateway или отсутствие маршрута завершают контейнер с ненулевым кодом. Прямого fallback нет.
2. Production mode задаётся явно и не выводится из наличия отдельных секретов. Compose закрепляет `INSTANCE_LABEL=prod`, `DB_FILE=/data/news.db`, `TZ=Europe/Moscow`; local/test сохраняет отдельный non-prod режим.

#### Идентичность volume

3. Compose подключает документированный production-каталог `/root/hw-news/data` к `/data` как long-form bind и запрещает автоматическое создание отсутствующего host-каталога. До первого rollout оператор один раз создаёт на существующем volume identity marker `/data/.hw-news-volume-id`; его ожидаемое non-secret значение закреплено Compose.
4. До доступа к БД процесс проверяет отдельную запись mount для `/data`, совпадение identity marker и то, что `/data/news.db` либо отсутствует, либо является обычным файлом без symlink-escape. Флаг bootstrap не обходит эту проверку.

#### Локальная config-валидация

5. После route и volume gates, но до listener-а, SQLite write и API-вызовов, production локально проверяет обязательную конфигурацию:

| Поле | Допустимое локальное значение |
|------|-------------------------------|
| Telegram bot token | Локально соответствует `^[0-9]+:[A-Za-z0-9_-]+$`; online validity здесь не проверяется |
| Telegram channel ID | `^@[A-Za-z][A-Za-z0-9_]{4,31}$` или целочисленный chat/channel ID |
| Telegram admin ID | Положительное целое число |
| Telegraph access token | Непустая строка без whitespace; новый token через сеть на этом шаге не создаётся |
| `LLM_PROVIDER` | Явно одно из `claude`, `openai`, `gemini`, `openrouter`; неизвестное значение fatal в prod |
| Provider key | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` или `OPENROUTER_API_KEY`/`OPEN_ROUTER_API_KEY` строго по выбранному provider-у; непустое, без online probe |
| Provider model | Явно задана непустая trimmed-строка в `ANTHROPIC_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL` или `OPENROUTER_MODEL`; code default в prod не считается конфигурацией |

Если оба OpenRouter key alias заданы, используется canonical `OPENROUTER_API_KEY`; review buttons остаются optional. Fallback на provider по наличию ключа разрешён только в non-prod.

#### Read-only DB preflight

6. Обычный production-start открывает БД без возможности создать или изменить файл. Для успеха нужны: подтверждённый volume, exact path, regular non-symlink file с разрешённым чтением, исправная SQLite, все шесть application tables (`processed_news`, `pending_articles`, `published_articles`, `failed_articles`, `bot_state`, `fetch_failures`) и `processed_news` с count > 0. Дополнительные SQLite internal tables допустимы; частичная или foreign schema — нет.
7. Failed preflight не создаёт DB, journal/WAL/SHM, marker или таблицы и не меняет bytes, metadata или directory entries. Он пишет ровно один redacted fatal event с именем gate, reason-class из таблицы ниже и, для config, именем поля без его значения; затем процесс завершается ненулевым кодом до listener/job/network.

| Gate | Допустимые reason-class |
|------|--------------------------|
| `ROUTE` | `missing_default`, `wrong_gateway`, `timeout` |
| `VOLUME` | `mount_missing`, `identity_missing`, `identity_mismatch`, `path_escape`, `wrong_file_type` |
| `CONFIG` | `missing_field`, `invalid_field`, `provider_mismatch` |
| `DB` | `missing`, `empty`, `unreadable`, `corrupt`, `foreign_or_partial_schema`, `empty_ledger` |
| `BOOTSTRAP` | `missing_flag`, `missing_permit`, `replayed_permit`, `ineligible_state`, `concurrent_claim`, `unexpected_controls` |

#### Одноразовый bootstrap/recovery

8. Empty bootstrap требует одновременно `ALLOW_EMPTY_PROD_DB=1` и active permit `/data/.bootstrap/permit` с новым UUID. Permit находится на уже подтверждённом volume. Один флаг, один permit и повтор использованного UUID по отдельности не дают доступа.
9. Permit может обойти только одну из четырёх форм нового состояния:

   - exact DB path отсутствует;
   - exact path — zero-byte regular file;
   - это исправная SQLite без user tables;
   - это распознанная bot SQLite со всеми шестью application tables, причём каждая из них пуста.

   Valid SQLite с foreign/partial tables, отсутствующим `processed_news` рядом с любым существующим state либо пустым `processed_news` при непустой другой application table остаётся fatal. Corrupt, unreadable, directory/symlink, wrong path/mount/marker и invalid config тоже никогда не становятся bootstrap-eligible.
10. После всех неотключаемых checks и непосредственно перед первой DB-write permit атомарно переводится в replay-proof consumed state. При двух конкурентных попытках победитель ровно один. Ошибка до этой границы permit не расходует; crash, schema-init или любая ошибка после неё требуют нового UUID и нового permit.
11. Permit разрешает один process lifetime, а не объявляет пустую БД навсегда безопасной. Следующий restart проходит обычный путь только когда `processed_news` уже содержит хотя бы одну строку. Если immediate job успешно вернулся, но ledger остался пустым, текущий process может ждать следующих слотов, однако любой restart до появления первой строки требует нового permit. Persistent «bootstrap complete» bypass и искусственная ledger-row запрещены.
12. Только после успешного обычного или авторизованного preflight выполняется idempotent schema initialization, запускается runtime и разрешаются внешние вызовы. Для populated DB присутствующие bootstrap flag/permit считаются ошибкой конфигурации и не расходуются: оператор должен убрать их.
13. Local/test использует относительную пустую БД, не требует VPN, marker или permit и не активирует production gates от одного случайного env-поля.

### Сценарий 3. Lifecycle планировщика

1. После успешного startup blocking immediate job выполняется до регистрации ежедневных cron-слотов.
2. Если immediate job завершился строго до 10:00:00 МСК, канонический сегодняшний 10:00 tick сохраняется: он забирает материалы, появившиеся после быстрого startup-fetch.
3. Если immediate job завершился ровно в 10:00:00 или позже, этот process уже выполнил recovery tick: просроченный 10:00 не запускается следом, а ближайший 10:00 будет завтра.
4. При свежем рестарте в середине дня новый process снова выполняет immediate recovery job и после него вычисляет оставшиеся будущие слоты текущего дня. Прошедшие слоты не догоняются; persistent marker «сегодня уже запускались» не используется.
5. Исключение из immediate job не проглатывается: процесс завершается с ненулевым кодом, Docker может его перезапустить и повторить recovery path.

## Критерии приёмки

- [ ] Для любого tracked-изменения в push/PR-контуре `main` и `dev` GitHub Actions запускает полный test job; обязательный job не имеет content/path/extension skip-ветки.
- [ ] Полный suite проходит без dotenv и production secrets при transport-independent запрете внешней сети; контрольные Python HTTP, native `curl_cffi` и subprocess attempts падают локально, а live endpoint не получает запрос.
- [ ] Regression-набор dotenv/import, Telegraph token и OpenRouter job path проходит standalone, file-level, в двух противоположных порядках и full-suite без state leakage.
- [ ] Python не запускается до default route через `172.28.0.2`; route mismatch/timeout дают `ROUTE` + точный safe reason-class, ненулевой exit и no direct fallback.
- [ ] Static Compose contract содержит три pinned production env, absolute `/root/hw-news/data` → `/data`, запрет auto-create и pinned volume ID; missing/wrong mount или identity marker дают `VOLUME` failure даже с flag+permit.
- [ ] Config matrix принимает каждую разрешённую provider/channel форму и отклоняет missing/unknown/mismatched provider-key-model, nonnumeric admin и implicit model/default; fatal event содержит `CONFIG` и имя поля, но ни одного значения секрета.
- [ ] Read-only DB matrix покрывает populated bot DB, absent/zero-byte DB, no-table SQLite, all-empty bot schema, partial/foreign schema, empty ledger с другим state, corrupt, unreadable, nonregular, symlink и failed integrity; только populated bot DB проходит normal path, а каждый failure оставляет filesystem snapshot неизменным.
- [ ] Bootstrap проходит только для четырёх перечисленных eligible shapes с flag + fresh UUID permit; flag-only, permit-only, replay, populated DB с permit, wrong mount/config/corruption и две concurrent claims дают fail/одного победителя согласно контракту.
- [ ] После consumed permit любой post-claim failure требует новый UUID; quiet successful process с пустым ledger продолжает жить, но его следующий restart без нового permit падает, пока `processed_news` не станет nonempty.
- [ ] Local/test может создать относительную пустую DB без VPN/marker/permit и не включает production gates от одного env-поля.
- [ ] При finish immediate job в 09:59:59 остаётся сегодняшний 10:00; при finish в 10:00:00 или позже job вызван один раз и следующий 10:00 завтра; исключение immediate job даёт ненулевой exit.
- [ ] Mid-day restart выполняет immediate recovery, не догоняет прошедшие slots и регистрирует будущие; persistent daily marker отсутствует.
- [ ] Regression tests подтверждают неизменность reader-visible публикаций, дедуп-вердиктов, alert-текстов и существующих SQLite rows вне новых startup artifacts.

### Краевые случаи

- Developer environment содержит настоящие provider, Telegram и Telegraph credentials — test process их не использует.
- Native `curl_cffi` или subprocess обходит Python monkeypatch — внешний transport boundary всё равно блокирует egress.
- Тест из state-leak regression-набора запускается первым, последним или отдельно — результат одинаков.
- VPN sidecar жив, но route отсутствует, неверен или появляется после timeout — Python не стартует.
- Compose запущен из другого checkout, host data path отсутствует, `/data` подменён или marker скопирован без ожидаемого ID — startup падает до DB access.
- DB — symlink, directory, zero-byte file, foreign SQLite либо bot schema с empty ledger и непустой pending row — normal startup падает; grant разрешает только явно перечисленные новые состояния.
- Permit предъявлен populated DB, использован двумя process одновременно, повторён после crash или испорчен — startup не превращается в reusable bypass.
- Bootstrap process не добавил ни одной `processed_news` row до рестарта — новый process требует новый permit, даже если предыдущий job вернулся без exception.
- Неизвестный provider раньше fallback-ился по найденному ключу — в prod теперь падает; non-prod fallback сохраняется.
- Immediate job заканчивается в 09:59:59, 10:00:00 и 10:00:01 — результат определён разными сторонами строгой границы.

## Ограничения

- Пакет имеет size L из-за трёх независимых failure domains и выполняется инкрементами: unconditional/hermetic CI; scheduler lifecycle; production route/config/volume/DB/bootstrap gates. Каждый инкремент должен быть отдельно зелёным и откатываемым.
- `reliable-admin-alert-delivery` — отдельная следующая задача. Туда входят durable delivery/retry для `[E014]`, `[E016]`, `[E038]` и outage alerts `[E010]`–`[E013]`. Новейшее решение «недоставленный `[E014]` не разрешает автопубликацию через 24 часа» заменяет старую silence-семантику из `dedup-broad-precision`, но в этом пакете alert-код и approved spec не меняются.
- `[E036]` и blind spots принадлежат `operator-blind-spots`; `[E017]` уже следует mark-only-on-success pattern и здесь не меняется.
- Не меняются precision/content-дедуп, форматы публикаций и алертов, publication crash-gap и существующие business rows. Новые state artifacts ограничены volume marker и bootstrap permit/tombstones.
- Не входят большой модульный рефакторинг `news_bot.py`, off-box backup, SSH hardening и новый dependency lockfile.
- Production остаётся одним Docker Compose instance без staging и новой внешней инфраструктуры.
- Production deploy, live Telegram/LLM/Telegraph calls и изменение удалённого хоста в рамках задачи запрещены.

## Риски

- **Fail-closed остановит первый rollout, если volume marker не подготовлен.** Митигация: одноразовый pre-deploy шаг на существующем `/root/hw-news/data`, проверка marker до rebuild и точный `VOLUME` reason без автоматического создания каталога.
- **Постоянно выставленный override превратится в скрытый fail-open.** Митигация: флаг без fresh permit бесполезен, UUID имеет replay tombstone, а populated DB с bootstrap controls намеренно не стартует.
- **Quiet bootstrap оставит ledger пустым и следующий restart уйдёт в loop.** Митигация: это осознанный fail-closed контракт; лог требует новый permit, а runbook заранее предупреждает оператора не считать bootstrap завершённым до первой `processed_news` row.
- **Route barrier создаст restart loop при поломке VPN.** Митигация: bounded wait, точный safe reason и отсутствие direct fallback; исправляется VPN, а не обходится защита.
- **Подавление overdue tick уберёт полезный 10:00 fetch.** Митигация: строгая граница сохраняет сегодняшний tick только при finish `< 10:00:00`, а mid-day recovery не зависит от persistent marker.
- **Hermetic CI даст ложную защиту только для Python sockets.** Митигация: обязательный transport-independent boundary и отдельные red controls для Python, native curl и subprocess.
- **Production-only checks заденут local/test.** Митигация: явный production mode и отдельная negative matrix, не inference по одному secret/config field.

## Технические решения

- Мы решили убрать conditional skip обязательного CI job, потому что текущая классификация уже пропускает runtime-значимые tracked-файлы и multi-commit изменения.
- Мы решили изолировать pytest от внешней сети ниже уровня конкретной HTTP-библиотеки, потому что проект использует и Python transports, и нативный `curl_cffi`.
- Мы решили ставить route barrier до Python, потому что проверка в `main()` уже допускает import/startup side effects.
- Мы решили сохранить bind-based deployment, но закрепить absolute host path, no-auto-create и volume identity marker, потому что простой `ismount('/data')` не отличает правильный state от нового пустого bind.
- Мы решили валидировать production config локально и не делать credential probes до gates, потому что сам preflight не должен создавать внешний трафик.
- Мы решили отделить read-only DB classification от schema initialization и разрешить bootstrap только четырём перечисленным empty shapes.
- Мы решили использовать fresh UUID permit на подтверждённом volume с atomic single-winner claim и replay tombstone, потому что reusable env flag не является одноразовой авторизацией.
- Мы решили не вводить persistent bootstrap-complete bypass: следующему process всё ещё нужен nonempty ledger или новый permit.
- Мы решили выполнять immediate job до cron registration и считать finish `>= 10:00:00` уже выполненным тиком, потому что это устраняет overdue repeat без потери quick pre-10 slot.
- Мы решили не вводить persistent daily job marker, потому что mid-day process после crash обязан выполнить recovery fetch.

## Тестирование

**Unit-тесты:** делаются всегда. Добавляются проверки config matrix, DB-state classifier, permit state machine, safe fatal events и scheduler boundary.

**Интеграционные тесты:** делаем. Нужны clean-env/order regression, transport-independent network red controls, Compose/route/volume contract, filesystem + SQLite matrix, concurrent permit claim и scheduler lifecycle на контролируемом времени.

**E2E-тесты:** live E2E не делаем. Production Telegram, Telegraph, LLM/VPN и deployment несут риск публикации, расходов и изменения удалённого состояния; внешние границы проверяются offline doubles и network-deny controls.

## Деплой

Реализация проходит dev → PR → main только после зелёного безусловного CI. Production deploy остаётся ручным и выполняется пользователем вне окна публикаций 10:00–20:00 МСК; текущая задача его не выполняет.

Перед первым rollout этой защиты оператор один раз создаёт identity marker в существующем `/root/hw-news/data` с ожидаемым Compose ID и проверяет, что текущая populated DB проходит read-only preflight. Для обычных последующих upgrade permit и `ALLOW_EMPTY_PROD_DB` отсутствуют.

Настоящий empty bootstrap/recovery требует fresh UUID permit на volume и `ALLOW_EMPTY_PROD_DB=1` при recreation контейнера. После запуска оператор убеждается, что permit consumed, а до следующего рестарта — что `processed_news` стала nonempty; иначе выпускает новый permit осознанно. Простое редактирование `.env` без recreation уже созданный container не меняет.

## Как проверить

### Агент проверяет

| Шаг | Инструмент | Ожидаемый результат |
|-----|-----------|---------------------|
| 1. Запустить full suite без dotenv/secrets внутри внешне-networkless boundary | `pytest` + isolated runtime | Suite зелёный; Python/native/subprocess red controls не достигают live endpoint |
| 2. Прогнать state-leak regression standalone/file/opposite-order/full | `pytest` | dotenv, Telegraph token и OpenRouter state не протекают между cases |
| 3. Проверить workflow и production Compose contract | workflow fixtures + `docker compose config` без env values | Test job unconditional в оговорённом branch-контуре; exact env/bind/no-create/volume-ID закреплены |
| 4. Прогнать route/config/volume failure matrix | offline process doubles | Python marker отсутствует при failed gate; fatal event имеет gate/reason, exit nonzero, secrets отсутствуют |
| 5. Прогнать DB/bootstrap filesystem matrix | temporary mount model + SQLite + concurrent workers | Normal/eligible/rejected states и permit lifecycle совпадают с критериями; failed preflight не меняет snapshot |
| 6. Прогнать scheduler lifecycle на границах | `pytest`, controlled clock и production schedule library | `<10:00` сохраняет slot; `>=10:00` даёт завтра; mid-day recovery/exception contract соблюдён |
| 7. Запустить весь regression suite по business behavior | `pytest` | Публикации, dedup, alerts и existing rows не изменились вне startup artifacts |

### Пользователь проверяет

- Перед первым будущим rollout создать и сверить volume identity marker на существующем `/root/hw-news/data`; затем убедиться, что `/data/news.db` populated и bootstrap controls отсутствуют.
- После ручного rebuild/restart вне 10:00–20:00 МСК проверить startup events в порядке `ROUTE` → `VOLUME` → `CONFIG` → `DB`, затем ровно один immediate job; в логах нет значений секретов.
- При настоящем empty bootstrap создать fresh UUID permit, recreate container с `ALLOW_EMPTY_PROD_DB=1`, проверить consumed state и наличие первой `processed_news` row до следующего рестарта; live Telegram-кнопки этой задачей не проверяются.
