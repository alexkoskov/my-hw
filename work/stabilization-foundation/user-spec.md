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
3. Во время pytest недоступна внешняя сеть независимо от транспорта: Python HTTP-клиенты, нативный `curl_cffi` и случайно запущенный subprocess не могут выйти наружу. Loopback можно разрешить только явному тесту. Каждый red control получает ожидаемую локальную deny-ошибку до DNS/TCP egress, а принадлежащий тесту sentinel за isolation boundary фиксирует ноль запросов; production endpoint для этой проверки не используется.
4. Regression-набор для известных process-global утечек — dotenv/import config, Telegraph access token и OpenRouter balance path внутри job — проходит отдельно, внутри своего файла, в двух противоположных порядках и в полном suite. Ни один из этих тестов не получает state от предыдущего.

### Сценарий 2. Fail-closed production startup

#### Route и production mode

1. Production-контейнер до запуска Python ждёт, пока точный default route будет направлен через VPN gateway `172.28.0.2`. Ожидание ограничено по времени; timeout, неверный gateway или отсутствие маршрута завершают контейнер с ненулевым кодом. Прямого fallback нет.
2. Единственный mode discriminator — `APP_MODE`. Значение `production` включает все gates; Compose закрепляет `APP_MODE=production`, `INSTANCE_LABEL=prod`, `DB_FILE=/data/news.db`, `TZ=Europe/Moscow`. Значения `local` и `test` включают соответствующий non-prod режим, отсутствие переменной означает `local`, любое другое непустое значение fatal. `INSTANCE_LABEL` остаётся только alert-label и само режим не выбирает.
3. При `APP_MODE=production` missing/wrong invariant всегда даёт fail-closed, а не переключает процесс в local. При non-prod mode сочетание с `INSTANCE_LABEL=prod` или `DB_FILE=/data/news.db` считается `partial_prod_contract` и тоже fatal. Отдельные secrets или московский `TZ` никогда не выбирают production.

#### Идентичность volume

4. Compose подключает документированный production-каталог `/root/hw-news/data` к `/data` и обязан отказать, а не автоматически создать отсутствующий host-каталог. До первого rollout оператор один раз создаёт на существующем volume identity marker `/data/.hw-news-volume-id`; его ожидаемое non-secret значение закреплено Compose.
5. До доступа к БД процесс проверяет отдельную запись mount для `/data`, совпадение identity marker и то, что `/data/news.db` либо отсутствует, либо является обычным файлом без symlink-escape. Флаг bootstrap не обходит эту проверку.

#### Локальная config-валидация

6. После route и volume gates, но до listener-а, SQLite write и API-вызовов, production локально проверяет обязательную конфигурацию:

| Поле | Допустимое локальное значение |
|------|-------------------------------|
| Telegram bot token | Локально соответствует `^[0-9]+:[A-Za-z0-9_-]+$`; online validity здесь не проверяется |
| Telegram channel ID | `^@[A-Za-z][A-Za-z0-9_]{4,31}$` или ненулевой целочисленный chat/channel ID |
| Telegram admin ID | Положительное целое число |
| Telegraph access token | Непустая строка без whitespace; новый token через сеть на этом шаге не создаётся |
| `LLM_PROVIDER` | Явно одно из `claude`, `openai`, `gemini`, `openrouter`; неизвестное значение fatal в prod |
| Provider key | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` или `OPENROUTER_API_KEY`/`OPEN_ROUTER_API_KEY` строго по выбранному provider-у; непустое, без online probe |
| Provider model | Явно задана непустая trimmed-строка в `ANTHROPIC_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL` или `OPENROUTER_MODEL`; code default в prod не считается конфигурацией |

Если оба OpenRouter key alias заданы, используется canonical `OPENROUTER_API_KEY`. Key/model variables невыбранных provider-ов не влияют на выбор и не логируются; review buttons остаются optional. Fallback на provider по наличию ключа разрешён только в non-prod.

#### Read-only DB preflight

7. Обычный production-start открывает БД без возможности создать или изменить файл. Для успеха нужны: подтверждённый volume, exact path, regular non-symlink file с разрешённым чтением, исправная SQLite, отсутствие `-journal`/`-wal`/`-shm` sidecars, все шесть application tables (`processed_news`, `pending_articles`, `published_articles`, `failed_articles`, `bot_state`, `fetch_failures`) и `processed_news` с count > 0. Дополнительные SQLite internal tables допустимы; частичная или foreign schema — нет. Sidecar-bearing DB требует отдельного operator recovery и автоматически не открывается.
8. Failed preflight не создаёт DB, journal/WAL/SHM, marker или таблицы. DB bytes, size, mtime/ctime, mode, inode, schema/rows, существующий sidecar set и directory entries остаются прежними; access time явно исключён из контракта. Failure пишет ровно один redacted fatal event с именем gate, reason-class из таблицы ниже и, для config, именем поля без его значения; permit UUID и secret values не логируются. Затем процесс завершается ненулевым кодом до listener/job/network.

| Gate | Допустимые reason-class |
|------|--------------------------|
| `ROUTE` | `missing_default`, `wrong_gateway`, `timeout` |
| `VOLUME` | `mount_missing`, `identity_missing`, `identity_mismatch`, `path_escape`, `wrong_file_type` |
| `CONFIG` | `missing_field`, `invalid_field`, `unknown_mode`, `partial_prod_contract`, `provider_mismatch` |
| `DB` | `missing`, `empty`, `unreadable`, `corrupt`, `sidecar_present`, `foreign_or_partial_schema`, `empty_ledger` |
| `BOOTSTRAP` | `missing_flag`, `missing_permit`, `replayed_permit`, `ineligible_state`, `concurrent_claim`, `unexpected_controls` |

#### Одноразовый bootstrap/recovery

9. Empty bootstrap требует одновременно `ALLOW_EMPTY_PROD_DB=1` и active permit `/data/.bootstrap/permit` с новым UUID. Permit находится на уже подтверждённом volume. Один флаг, один permit и повтор использованного UUID по отдельности не дают доступа.
10. Permit может обойти только одну из четырёх форм нового состояния:

   - exact DB path отсутствует;
   - exact path — zero-byte regular file;
   - это исправная SQLite без user tables;
   - это распознанная bot SQLite со всеми шестью application tables, причём каждая из них пуста.

   Valid SQLite с foreign/partial tables, отсутствующим `processed_news` рядом с любым существующим state либо пустым `processed_news` при непустой другой application table остаётся fatal. Corrupt, unreadable, directory/symlink, wrong path/mount/marker и invalid config тоже никогда не становятся bootstrap-eligible.
11. После всех неотключаемых checks и непосредственно перед первой DB-write permit переводится в replay-proof consumed state с атомарным single-winner поведением. Ошибка до этой границы permit не расходует. После claim нового UUID недостаточно само по себе: повтор разрешён только если DB всё ещё приведена к одной из четырёх eligible shapes.
12. Permit разрешает один process lifetime, а не объявляет пустую БД навсегда безопасной. Следующий restart проходит normal path только когда `processed_news` уже содержит хотя бы одну строку. Если ledger пуст, но все шесть application tables пусты, новая осознанная попытка может использовать fresh permit. Если partial schema или любая другая application table уже содержит state, generic permit запрещён: текущий process может продолжить работу, но перед рестартом оператор обязан сделать backup и выполнить data-aware recovery — предпочтительно restore последней исправной DB либо явную очистку только после ручной проверки. Persistent bypass и искусственная ledger-row запрещены.
13. Только после успешного обычного или авторизованного preflight выполняется idempotent schema initialization, запускается runtime и разрешаются внешние вызовы. Для populated DB присутствующие bootstrap flag/permit считаются ошибкой конфигурации и не расходуются: оператор должен убрать их.
14. Local/test использует `APP_MODE=local|test` (либо unset для local), относительную пустую БД, не требует VPN, marker или permit. Production gates не включаются от secrets, `TZ` или label/path по отдельности; prod-like partial contract при non-prod mode завершается fatal.

### Сценарий 3. Lifecycle планировщика

1. После успешного startup blocking immediate job выполняется до регистрации ежедневных cron-слотов.
2. Если immediate job завершился строго до 10:00:00 МСК, канонический сегодняшний 10:00 tick сохраняется: он забирает материалы, появившиеся после быстрого startup-fetch.
3. Если immediate job завершился ровно в 10:00:00 или позже, этот process уже выполнил recovery tick: просроченный 10:00 не запускается следом, а ближайший 10:00 будет завтра.
4. При свежем рестарте в середине дня новый process снова выполняет immediate recovery job и после него вычисляет оставшиеся будущие слоты текущего дня. Прошедшие слоты не догоняются; persistent marker «сегодня уже запускались» не используется.
5. Исключение из immediate job не проглатывается: процесс завершается с ненулевым кодом, Docker может его перезапустить и повторить recovery path.

## Критерии приёмки

- [ ] Для любого tracked-изменения в push/PR-контуре `main` и `dev` GitHub Actions запускает полный test job; обязательный job не имеет content/path/extension skip-ветки.
- [ ] Полный suite проходит без dotenv и production secrets внутри transport-independent egress boundary; Python HTTP, native `curl_cffi` и subprocess red controls получают локальную deny-ошибку до DNS/TCP, а test-owned sentinel снаружи фиксирует ноль запросов.
- [ ] Regression-набор dotenv/import, Telegraph token и OpenRouter job path проходит standalone, file-level, в двух противоположных порядках и full-suite без state leakage.
- [ ] Python не запускается до default route через `172.28.0.2`; route mismatch/timeout дают `ROUTE` + точный safe reason-class, ненулевой exit и no direct fallback.
- [ ] Static Compose contract содержит `APP_MODE=production`, три pinned invariants, absolute `/root/hw-news/data` → `/data`, запрет auto-create и pinned volume ID; missing/wrong mount или identity marker дают `VOLUME` failure даже с flag+permit.
- [ ] Mode/config matrix покрывает production, local, test, unset, unknown и partial-prod состояния; разрешённые provider/channel формы принимаются, missing/mismatched provider-key-model, nonnumeric admin и implicit model отклоняются, а dormant provider credentials не влияют на выбор. Fatal event содержит `CONFIG` + exact reason/field, но не значения.
- [ ] Read-only DB matrix покрывает populated bot DB, absent/zero-byte DB, no-table SQLite, all-empty bot schema, partial/foreign schema, empty ledger с другим state, sidecars, corrupt, unreadable, nonregular, symlink и failed integrity. Только populated bot DB проходит normal path; каждый reject даёт nonzero exit и ровно один redacted `DB|reason`, не логирует secrets/UUID и сохраняет named filesystem attributes из контракта.
- [ ] Bootstrap проходит только для четырёх eligible shapes с flag + fresh UUID permit; flag-only, permit-only, replay, populated DB с permit, wrong mount/config/corruption и concurrent claims дают предусмотренный fail/одного победителя. Каждый reject даёт nonzero exit и ровно один redacted `BOOTSTRAP|reason` без secret/UUID values.
- [ ] После consumed permit normal restart разрешён при nonempty ledger; fresh permit разрешает новый empty-ledger attempt только для всё ещё eligible DB. Partial schema или empty ledger с любым другим state остаётся fatal до manual backup + data-aware restore/cleanup.
- [ ] Local/test может создать относительную пустую DB без VPN/marker/permit; только `APP_MODE=production` включает gates, а unknown mode и prod-like partial contract fail closed.
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
- Bootstrap process не добавил `processed_news` row: если все application tables по-прежнему пусты, после рестарта допустим fresh permit; если появился pending/failure/state или partial schema, новый permit сам по себе запрещён и нужен manual data-aware recovery.
- `APP_MODE` отсутствует, неизвестен либо не-production mode смешан с prod label/path — применяется точная mode matrix, а не inference по найденным secrets.
- Неизвестный provider раньше fallback-ился по найденному ключу — в prod теперь падает; non-prod fallback сохраняется.
- Immediate job заканчивается в 09:59:59, 10:00:00 и 10:00:01 — результат определён разными сторонами строгой границы.

## Ограничения

- Пакет имеет size L из-за трёх независимых failure domains и выполняется инкрементами: unconditional/hermetic CI; scheduler lifecycle; production route/config/volume/DB/bootstrap gates. Каждый инкремент должен быть отдельно зелёным и откатываемым.
- `reliable-admin-alert-delivery` — отдельная следующая задача. Туда входят durable delivery/retry для `[E014]`, `[E016]`, `[E038]` и outage alerts `[E010]`–`[E013]`. Новейшее решение «недоставленный `[E014]` не разрешает автопубликацию через 24 часа» заменяет старую silence-семантику из `dedup-broad-precision`, но в этом пакете alert-код и approved spec не меняются.
- `[E036]` и blind spots принадлежат `operator-blind-spots`; `[E017]` уже следует mark-only-on-success pattern и здесь не меняется.
- Не меняются precision/content-дедуп, форматы публикаций и алертов, publication crash-gap и существующие business rows. Новые state artifacts ограничены volume identity и одноразовым bootstrap authorization state.
- Не входят большой модульный рефакторинг `news_bot.py`, off-box backup, SSH hardening и новый dependency lockfile.
- Production остаётся одним Docker Compose instance без staging и новой внешней инфраструктуры.
- Production deploy, live Telegram/LLM/Telegraph calls и изменение удалённого хоста в рамках задачи запрещены.

## Риски

- **Fail-closed остановит первый rollout, если volume marker не подготовлен.** Митигация: одноразовый pre-deploy шаг на существующем `/root/hw-news/data`, проверка marker до rebuild и точный `VOLUME` reason без автоматического создания каталога.
- **Постоянно выставленный override превратится в скрытый fail-open.** Митигация: флаг без fresh permit бесполезен, повтор authorization отвергается, а populated DB с bootstrap controls намеренно не стартует.
- **Quiet bootstrap оставит ledger пустым и другой application state непустым.** Митигация: generic permit намеренно не обходит такое состояние; перед restart оператор делает backup и data-aware restore/cleanup, а fresh permit применяется только после возврата к одной из четырёх eligible shapes.
- **Route barrier создаст restart loop при поломке VPN.** Митигация: bounded wait, точный safe reason и отсутствие direct fallback; исправляется VPN, а не обходится защита.
- **Подавление overdue tick уберёт полезный 10:00 fetch.** Митигация: строгая граница сохраняет сегодняшний tick только при finish `< 10:00:00`, а mid-day recovery не зависит от persistent marker.
- **Hermetic CI даст ложную защиту только для Python sockets.** Митигация: обязательный transport-independent boundary и отдельные red controls для Python, native curl и subprocess.
- **Production-only checks заденут local/test или typo выключит prod-защиту.** Митигация: отдельный `APP_MODE` с закрытым набором значений; unknown и prod-like partial contract fatal, а secrets сами mode не выбирают.

## Технические решения

- Мы решили убрать conditional skip обязательного CI job, потому что текущая классификация уже пропускает runtime-значимые tracked-файлы и multi-commit изменения.
- Мы решили изолировать pytest от внешней сети ниже уровня конкретной HTTP-библиотеки, потому что проект использует и Python transports, и нативный `curl_cffi`.
- Мы решили ставить route barrier до Python, потому что проверка в `main()` уже допускает import/startup side effects.
- Мы решили сохранить bind-based deployment, но требовать exact host path, отказ при отсутствующем source и volume identity, потому что простой факт mount не отличает правильный state от нового пустого bind.
- Мы решили использовать отдельный `APP_MODE` как единственный selector, потому что alert-label или неполная комбинация config-полей не должны случайно включать либо выключать safety gates.
- Мы решили валидировать production config локально и не делать credential probes до gates, потому что сам preflight не должен создавать внешний трафик.
- Мы решили отделить read-only DB classification от schema initialization и разрешить bootstrap только четырём перечисленным empty shapes.
- Мы решили требовать fresh one-use authorization на подтверждённом volume с single-winner и replay protection, потому что reusable env flag не является одноразовым разрешением.
- Мы решили не вводить persistent bootstrap-complete bypass: следующему process нужен nonempty ledger, а fresh permit допустим только после подтверждения одной из четырёх eligible DB shapes.
- Мы решили выполнять immediate job до cron registration и считать finish `>= 10:00:00` уже выполненным тиком, потому что это устраняет overdue repeat без потери quick pre-10 slot.
- Мы решили не вводить persistent daily job marker, потому что mid-day process после crash обязан выполнить recovery fetch.

## Тестирование

**Unit-тесты:** делаются всегда. Добавляются проверки config matrix, DB-state classifier, permit state machine, safe fatal events и scheduler boundary.

**Интеграционные тесты:** делаем. Нужны clean-env/order regression, offline egress red controls, Compose/route/volume contract, filesystem + SQLite matrix, single-winner permit behavior и scheduler lifecycle на граничном времени.

**E2E-тесты:** live E2E не делаем. Production Telegram, Telegraph, LLM/VPN и deployment несут риск публикации, расходов и изменения удалённого состояния; внешние границы проверяются offline doubles и network-deny controls.

## Деплой

Реализация проходит dev → PR → main только после зелёного безусловного CI. Production deploy остаётся ручным и выполняется пользователем вне окна публикаций 10:00–20:00 МСК; текущая задача его не выполняет.

Перед первым rollout этой защиты оператор один раз создаёт identity marker в существующем `/root/hw-news/data` с ожидаемым Compose ID и проверяет, что текущая populated DB проходит read-only preflight. Для обычных последующих upgrade permit и `ALLOW_EMPTY_PROD_DB` отсутствуют.

Настоящий empty bootstrap/recovery требует fresh UUID permit на volume и `ALLOW_EMPTY_PROD_DB=1` при recreation контейнера. После запуска оператор убеждается, что authorization consumed, а до следующего рестарта — что `processed_news` стала nonempty. Если ledger пуста, fresh permit достаточен только пока DB всё ещё соответствует одной из четырёх eligible shapes; при partial schema или другом state оператор сначала делает backup и осознанный restore/cleanup. Простое редактирование `.env` без recreation уже созданный container не меняет.

## Как проверить

### Агент проверяет

| Шаг | Инструмент | Ожидаемый результат |
|-----|-----------|---------------------|
| 1. Запустить full suite без dotenv/secrets внутри offline egress boundary | `pytest` + test-owned sentinel | Suite зелёный; Python/native/subprocess controls получают deny до DNS/TCP, sentinel фиксирует 0 запросов |
| 2. Прогнать state-leak regression standalone/file/opposite-order/full | `pytest` | dotenv, Telegraph token и OpenRouter state не протекают между cases |
| 3. Проверить workflow и production deployment contract | статические offline-проверки | Test job unconditional; APP_MODE, exact env/data source, no-auto-create и volume identity закреплены |
| 4. Прогнать route/config/volume failure matrix | offline process doubles | Python marker отсутствует при failed gate; fatal event имеет gate/reason, exit nonzero, secrets отсутствуют |
| 5. Прогнать DB/bootstrap filesystem matrix | `pytest` в isolated filesystem | Normal/eligible/rejected/recovery states совпадают; каждый reject даёт exact event/exit и сохраняет named file attributes |
| 6. Прогнать scheduler lifecycle на границах | `pytest` с контролируемым временем | `<10:00` сохраняет slot; `>=10:00` даёт завтра; mid-day recovery/exception contract соблюдён |
| 7. Запустить весь regression suite по business behavior | `pytest` | Публикации, dedup, alerts и existing rows не изменились вне startup artifacts |

### Пользователь проверяет

- Перед первым будущим rollout создать и сверить volume identity marker на существующем `/root/hw-news/data`; затем убедиться, что `/data/news.db` populated и bootstrap controls отсутствуют.
- После ручного rebuild/restart вне 10:00–20:00 МСК проверить startup events в порядке `ROUTE` → `VOLUME` → `CONFIG` → `DB`, затем ровно один immediate job; в логах нет значений секретов.
- При настоящем empty bootstrap создать fresh permit, recreate container с `ALLOW_EMPTY_PROD_DB=1`, проверить consumed state и первую `processed_news` row; если до restart появился только другой state, сначала backup + data-aware restore/cleanup. Live Telegram-кнопки этой задачей не проверяются.
