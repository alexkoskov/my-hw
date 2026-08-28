# Code Research: stabilization-foundation

Дата исследования: 2026-08-25
Ветка: `dev`
Режим: read-only; production-код и тесты не менялись.

## Краткий вывод

Подтверждены четыре независимых класса дефектов:

1. CI может не запускать тесты для runtime-изменений, а локальный suite зависит
   от `.env`, порядка тестов и способен обращаться в live OpenRouter.
2. Production начинает Python и сетевые вызовы до установки VPN route; DB guard
   запускается уже после создания потенциально неверной БД и только предупреждает.
3. Cron регистрируется до blocking immediate `job()`, поэтому просроченный tick
   10:00 может выполниться второй раз после возврата первого.
4. Stateful admin alerts местами записываются как доставленные после
   `send_admin_notification() == False`; E014 дополнительно имеет crash/callback
   race, потому что сообщение и token появляются до самой pending-row.

Первые три потока образуют одну runtime-foundation. Четвёртый требует durable
delivery state и пересекается с двумя уже утверждёнными specs; это не локальный
`if send(...):`.

## 1. Entry Points

### `.github/workflows/ci.yml`

- `check-skip` (`:10-25`) делает diff только `HEAD~1..HEAD` при `fetch-depth: 2`.
- Regex на `:21` считает docs все `*.txt`, всю `.claude/`, `.spec/` и `docs/`.
- `test` зависит от результата skip-job (`:27-42`).
- Полный suite устанавливает оба requirements-файла и запускает
  `python -m pytest tests/ -v` на Python 3.13.

Следствия: multi-commit push оценивается по последнему commit; изменения
`requirements.txt`, `requirements-dev.txt` и runtime prompt
`.claude/skills/project-knowledge/references/ux-guidelines.md` могут не запускать
тесты.

### `Dockerfile` и `docker-compose.yml`

- `Dockerfile:20` сразу выполняет `python3 news_bot.py`.
- `news-bot` получает `.env`, heartbeat и `./data:/data`, но Compose не закрепляет
  `DB_FILE` и `INSTANCE_LABEL` (`docker-compose.yml:20-38`).
- `route-setup` присоединяется к namespace уже запущенного `news-bot`
  (`:45-55`) и только затем делает
  `ip route replace default via 172.28.0.2` (`:52`).

Первый Python import и startup-код могут выполняться до смены default route.

### `news_bot.main()`

Текущий порядок (`news_bot.py:5077-5172`):

1. `init_db()` (`:5096`);
2. `telegraph_publisher.ensure_access_token()` (`:5097`);
3. live LLM health probe и возможный Telegram alert (`:5100-5123`);
4. TZ warning (`:5125-5139`);
5. warning-only `_prod_db_guard()` (`:5141-5152`);
6. review listener (`:5154-5159`);
7. cron registration (`:5161-5163`);
8. blocking immediate `job()` (`:5165-5167`);
9. `schedule.run_pending()` loop (`:5169-5172`).

Значит DB/VPN/config protection сегодня не является барьером перед side effects.

### `news_bot.job()`

- Повторно и безусловно вызывает `init_db()` (`news_bot.py:4034-4036`).
- Каждый tick запускает OpenRouter balance probe (`:4068-4071`).
- Объединяет intake, plan alert и blocking publish window в одном вызове
  (`:4010-4032`, `:4644-4959`).
- Restart mid-window намеренно запускает новый immediate `job()`, который
  пересчитывает оставшиеся fixed slots. Это recovery-механизм, а не случайный
  дубль; persistent once-per-day marker вокруг всего `job()` его сломает.

### `send_admin_notification()`

`news_bot.py:601-671` возвращает `True` после успешного Telegram call и `False`
после отсутствующих credentials или исчерпания трёх попыток. Часть callers
проверяет bool, часть только ловит исключения и игнорирует `False`.

## 2. Data Layer

### Production DB guard

- `DB_FILE` читается при import и молча падает в relative `news.db`
  (`news_bot.py:113-121`).
- `_prod_db_guard()` включён только при `INSTANCE_LABEL == 'prod'`
  (`:1590-1603`).
- Guard проверяет absolute path и `processed_news` count, но возвращает только
  список warnings (`:1604-1621`).
- `_count_processed_news()` использует обычный `sqlite3.connect` (`:1579-1587`),
  который способен создать отсутствующий файл.
- К моменту guard `init_db()` уже создал schema по выбранному пути.

Read-only preflight должен отличать: неверный путь, отсутствующий mount/file,
пустой ledger, отсутствующую таблицу, corrupt SQLite и permission error. Простой
boolean override не должен разрешать все эти классы одновременно.

### `pending_articles` и `bot_state`

- Pending schema и verified column migrations принадлежат
  `pending_articles_repo.py:70-93, 320-369`.
- `publish_after` — существующая timed deferral; NULL означает publishable,
  future timestamp скрывает row из очереди (`:329-336, 543-572`).
- `hold_reason` — отдельная бессрочная content hold (`:321-328`).
- `bot_state` хранит pair rate limits, global alert markers, outage state и
  review tokens (`pending_articles_repo.py:42-63`).
- Review token хранится как `review_token:<token> -> <kind>|<link>`
  (`:1702-1817`).

### E014 current ordering

В `news_bot.job()` soft flag:

1. вычисляет `publish_after = now + 24h` (`news_bot.py:4420-4429`);
2. проверяет pair rate limit (`:4430-4443`);
3. mint/persist token (`:4461-4468`);
4. отправляет E014, но не читает bool (`:4472-4493`);
5. безусловно пишет pair marker и commit (`:4499-4502`);
6. только позже строит и вставляет pending row (`:4543-4571`).

Это создаёт четыре состояния повреждения:

- быстрый callback видит token, но row ещё не существует;
- crash после send до insert оставляет сообщение с мёртвой кнопкой;
- `False` расходует семидневный rate limit;
- pending row больше не проходит intake gate на следующем tick, поэтому E014
  сам не повторяется; через 24 часа row становится publishable.

Для подтверждённой пользователем семантики до visible send должны durable
существовать row, safety hold/defer, retry intent и один reusable token. Deadline
решения начинается от успешной доставки, а не от первой неудачной попытки.

### Другие delivery markers

- E016: `False` не мешает `mark_dedup_degraded_pinged()` и commit
  (`news_bot.py:4517-4532`). Business fail-open publication должна сохраняться.
- E038: queue defer записывается правильно независимо от Telegram
  (`:4843-4858`), но `False` всё равно вызывает `mark_hold_cap_pinged()`
  (`:4873-4882`). Defer нельзя откатывать; delivery rate limit — нельзя ставить.
- E010-E012: `outage_state.record_outage_event()` commit-ит `ping_count` и
  `last_ping_sent_at` до Telegram (`outage_state.py:309-338, 349-405`;
  send site `news_bot.py:3488-3498`).
- E013: recovery удаляет outage keys до Telegram
  (`outage_state.py:408-467`; send site `news_bot.py:3351-3368`).
- E017 уже следует правильному паттерну: marker ставится только после `True`
  (`news_bot.py:5024-5029`).

## 3. Similar Features and Reusable Patterns

### E017 delivered-only marker

`_maybe_ping_dry_spell()` разделяет detection, delivery и marker. Неудачная
доставка не расходует дневной alert, а marker живёт в DB и переживает restart.
Это ближайший рабочий образец для E016/E038, но E014 сложнее из-за row/token и
24-hour decision window.

### Verified schema migrations

`pending_articles_repo._COLUMN_MIGRATIONS` + `_ensure_column` применяет nullable
columns idempotently и перепроверяет результат. Любое новое E014 delivery field
должно использовать тот же backward-compatible pattern; старые rows получают
безопасное NULL-состояние.

### Fail-closed singleton

`news_bot._acquire_singleton_lock()` (`news_bot.py:5184-5223`) уже применяет
понятный для проекта fail-fast pattern: точный log, exit 1, отсутствие
best-effort продолжения. Startup preflight должен иметь такую же наблюдаемую
семантику и redaction.

### Existing restart recovery

`compute_fixed_slots()` (`compute_publish_slots.py:52-108`) исключает уже
прошедшие fixed times кроме пятиминутного grace. Fresh process после crash
может запустить immediate job и продолжить оставшиеся slots. Эту совместимость
нельзя потерять ради глобального once/day marker.

## 4. Integration Flows

### Safe production startup

Нужный порядок поведения:

1. container-level barrier подтверждает, что default route указывает именно на
   `172.28.0.2`; до этого Python не запускается;
2. production mode, DB path и TZ закреплены Compose, а не optional `.env`;
3. syntactic required-config validation без network;
4. read-only DB preflight без создания/изменения файла;
5. только затем schema migrations, Telegraph/LLM probes, Telegram listener и job.

Timeout route barrier должен завершать container non-zero. Reachability одного
gateway недостаточна: проверяем именно default-route invariant. Direct-network
fallback запрещён deployment architecture.

`ALLOW_EMPTY_PROD_DB=1` подтверждён только для first bootstrap/recovery. Постоянно
оставленный env flag снова открывает flood-risk, поэтому разрешение должно быть
одноразовым и снимать только missing/empty-ledger condition; wrong path, missing
mount, corrupt DB и invalid config остаются fatal.

### Scheduler lifecycle

Фактически воспроизведено на `schedule==1.2.1`: process стартует 09:55, cron
получает `next_run=10:00`, immediate job блокирует до вечера, первый
`run_pending()` после возврата выполняет overdue entry — второй полный tick.

Минимальная совместимая граница исправления:

- blocking immediate `job()` выполняется до регистрации cron;
- если он вернулся уже после 10:00, следующий cron назначается на завтра;
- если он быстро вернулся до 10:00, canonical 10:00 tick остаётся полезным: он
  подхватит feeds, появившиеся после раннего restart;
- fresh process после mid-window crash снова выполняет immediate recovery job.

Разделение daily intake и restart-only resume требует декомпозиции 954-line
`job()` и durable phase marker; это отдельный architectural follow-up.

### Alert delivery

Business state и delivery state должны различаться:

- E016 остаётся fail-open даже без alert;
- E038 сохраняет queue defer;
- outage сохраняет HOLD/detection;
- E014 остаётся непубликуемым до подтверждённой доставки и затем даёт полные
  24 часа на решение.

Ambiguous timeout допускает duplicate Telegram message, потому что false
«доставлено» опаснее; callbacks обязаны оставаться идемпотентными, а retry должен
переиспользовать один token.

## 5. Existing Tests and Gaps

### CI/test isolation

- `tests/conftest.py:1-12` только добавляет repo root в `sys.path`; dotenv и
  network не блокируются.
- `tests/test_telegraph_publisher.py:1213-1225` патчит
  `ensure_access_token`, хотя `publish_article()` его не вызывает и напрямую
  читает env (`telegraph_publisher.py:712-714`).
- Ранние tests `test_telegraph_publisher.py:57-80` оставляют token в
  `os.environ`, поэтому полный файл маскирует изолированный failure.
- `job()` tests часто не патчат `_maybe_alert_openrouter_balance()`; при локальном
  ключе `openrouter_transcreation.get_remaining_credits()` делает два live GET
  (`openrouter_transcreation.py:116-188`).

Нужны инварианты: любой tracked change запускает suite; dotenv выключен;
production secrets очищены; network запрещён по умолчанию; порядок/одиночный
запуск тестов не меняет результат.

### Startup/DB

`tests/test_database.py:174-299` покрывает import-time DB_FILE и warning-only
guard, но не проверяет fatal order/no side effects. Нужна table-driven матрица
wrong/missing/empty/corrupt DB, override, local non-prod compatibility и
сохранение bytes/rows при preflight failure.

### Scheduler

- `tests/test_job_distributed_publish.py:230-292` проверяет live schedule API и
  source regex registration.
- Main tests (`:299-432`) мокают весь schedule/job и не моделируют clock crossing.
- Restart tests (`tests/test_integration.py:793-903`) проверяют slot recompute,
  но не `main()` + `run_pending()` lifecycle.

Нужен real `schedule.Scheduler` + controlled clock test: blocking immediate
crosses 10:00, после возврата underlying job вызван ровно один раз; fresh 16:00
process не подавляет recovery.

### Alerts

Existing E017 false/retry test (`tests/test_job_distributed_publish.py:1110-1126`)
служит contract anchor. Новые tests должны наблюдать DB прямо, а не спрашивать
тот же helper, который записывал marker.

Критические E014 scenarios: row/token существуют внутри send mock; False не
ставит pair marker и переживает restart; False→True запускает 24h от второго
вызова; callback выполняет действие один раз; crash boundaries не выпускают row.

## 6. Shared Utilities

- `sanitize_error_message()` / `_redact_text()` в `news_bot.py` — secret-safe
  fatal и alert logs.
- `pending_articles_repo._connect()` и verified migration helpers — SQLite
  persistence pattern.
- `compute_fixed_slots()` — pure remaining-slot computation.
- `send_admin_notification()` — единый bool delivery contract; caller обязан
  различать `False` и exception.
- `outage_state._compute_next_state()` — pure transition logic, но текущая
  persist model смешивает detected и delivered state.

## 7. Potential Problems

1. Fail-closed restart loop не сможет отправить Telegram alert, если route/config
   сломаны; primary signal должен быть redacted container log/external watchdog,
   иначе попытка уведомить создаёт тот же egress-risk или spam loop.
2. Env-only empty-DB override опасен, если оператор забудет его убрать.
3. In-memory/persistent once-per-day scheduler guard легко ломает recovery или
   пропускает feeds после раннего quick startup; критерий должен описывать
   overdue blocking case, а не обещать глобальный один вызов при всех restarts.
4. E014 send-before-row — security/UX race: authenticated callback всё равно не
   может выполнить обещанное действие без row.
5. Outage recovery во время Telegram outage может накопить устаревшие warnings;
   delivery model должен coalesce к актуальному состоянию, а не слать историю
   как будто она происходит сейчас.
6. Добавление safety logic прямо в 5223-line `news_bot.py` усилит исходную
   проблему монолита. User-spec фиксирует WHAT; tech-spec должен выбрать
   пропорциональную границу без большого refactor и без дальнейшего раздувания
   `job()`.

## 8. Constraints and Infrastructure

- Единственный production — Moscow Docker Compose; staging/test instance нет.
- Deploy ручной, вне 10:00-20:00 МСК; merge сам ничего не deploy-ит.
- VPN `vpnnet` внешний, gateway `172.28.0.2`; direct fallback запрещён.
- SQLite — единственное durable storage; новый внешний queue/cache не нужен.
- Python 3.13, `schedule==1.2.1`, `pytz`, pytest/freezegun уже установлены.
- Runtime prompt находится под `.claude/`, поэтому path-based docs skip для CI
  несовместим с архитектурой.
- Normal production upgrade уже имеет populated `/data/news.db`; override нужен
  только bootstrap/recovery.
- Tests не должны использовать production credentials или live paid services.

## 9. Scope and Approved-Spec Overlaps

### `operator-blind-spots`

- E017 уже реализован правильным delivered-only marker и остаётся regression
  baseline.
- Increment B владеет recovery/digest для потерянных E036 и требует не менять
  исходную E036 логику.
- Поэтому E036 и повторная реализация E017 исключены.

### `dedup-broad-precision`

- Approved spec меняет precision/content reason E014 и сейчас закрепляет старое
  «silence -> publish after 24h».
- Последнее подтверждённое пользователем решение для failed delivery это
  поведение supersedes: недоставленный E014 не разрешает публикацию; после
  successful delivery начинается новое полное 24h window.
- Precision algorithm и reader/admin wording не входят в stabilization. Перед
  его tech-spec baseline должен быть явно обновлён под новую delivery contract.

### Size implication

CI + startup/DB/VPN + scheduler — M-sized runtime foundation. Persistent
E014/E016/E038/outage delivery добавляет самостоятельный stateful flow,
migration и cross-spec sequencing; совмещённый пакет становится L и превышает
обычный порог одной user-spec (>3 flows, >5 integrations, >10 criteria).

Code-informed recommendation для следующего интервью: либо явно утвердить один
L-пакет с независимыми increments, либо вынести alert workflow в
`reliable-admin-alert-delivery`, сохранив уже принятые продуктовые решения как
handoff.

### Validation addendum (round 1)

После выноса alert workflow scope остался тем же, но adequacy-проверка раскрыла
две ранее недооценённые части: transport-independent network isolation должен
перекрывать native `curl_cffi`, а безопасный empty-DB override требует identity
volume, явной DB-state matrix и одноразового persistent permit protocol. Поэтому
актуальная оценка `stabilization-foundation` — L с тремя независимыми
increments; прежняя M-оценка выше сохранена как исходный вывод исследования.
