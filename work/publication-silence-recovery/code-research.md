# Code Research: publication-silence-recovery

Дата исследования: 2026-08-25. Снимок: локальная ветка `dev` на `102e029`,
только локальные файлы; production, SSH, сеть и deploy не использовались.

## 1. Entry Points

### Runtime scheduler

- `news_bot.py:4010` — `def job()` выполняет один дневной тик: intake, расчёт
  слотов, plan ping, блокирующий slot loop, recap, dry-spell check и heartbeat.
- `news_bot.py:4644-4679` — единственная точка расчёта дневного списка:
  `compute_fixed_slots(queue_size + deferred_backlog, now_msk)`.
- `news_bot.py:4774-4800` — slot loop. Перед каждым слотом он спит, заново читает
  `pending_repo.list_pending()`, но при первом пустом чтении делает `break`.
- `news_bot.py:5077` — `def main()`: запускает review-listener до первого
  `job()`, регистрирует `schedule.every().day.at("10:00", tz=Europe/Moscow)` и
  сразу вызывает `job()` на старте контейнера (`news_bot.py:5154-5172`).
- `compute_publish_slots.py:52` —
  `compute_fixed_slots(n: int, now: datetime, daily_times=..., grace_minutes=5)
  -> (list[datetime], int)`. Возвращает не более `n` из оставшихся фиксированных
  времён `10:00/15:00/19:30`; `carry_over = n - len(slots)`.

### Review callbacks

- `news_bot.py:697` — `resolve_dedup_callback(action, token, from_user_id)`.
  Ветка `keep` вызывает `pending_repo.clear_deferral(link)` и отвечает
  «выйдет в ближайший слот» (`news_bot.py:764-820`).
- `news_bot.py:823` — `resolve_hold_callback(action, token, from_user_id)`.
  Ветка `approve` вызывает `pending_repo.clear_hold(link)` и даёт то же обещание
  (`news_bot.py:870-892`).
- `news_bot.py:958` — `_parse_review_callback_data(data)` принимает только
  `dd:<c|k>:<token>` и `hd:<a|r>:<token>`.
- `news_bot.py:1037` — `_handle_review_update(update)` проверяет администратора,
  вызывает resolver, логирует решение и редактирует Telegram-сообщение.
- `news_bot.py:1124` — `_run_review_listener(stop_event=None)`: отдельный daemon
  thread с long-poll Telegram; именно он может изменить SQLite, пока main thread
  спит до следующего слота.

### External publication watch

- `.github/workflows/uptime.yml:56-280` — внешний GitHub Actions watchdog каждые
  30 минут. Он отдельно проверяет SSH greeting и последнюю страницу Telegraph.
- `.github/workflows/uptime.yml:99-162` — publication probe: берёт newest page из
  `getPageList(limit=1)`, извлекает `MM-DD` из path, восстанавливает год и считает
  stale как `days > 1 or (days == 1 and msk_hour >= 21)`.
- `.github/workflows/uptime.yml:185-280` — issue-backed dedup и Telegram alert /
  recovery. Publication alarm подавляется, пока активен host alarm.
- `watchdog.sh:42-73` и `news_bot.py:5053` — отдельный внутренний heartbeat
  contour. Он проверяет завершение `job()`, а не наличие публикации, поэтому не
  заменяет внешний Telegraph probe.

## 2. Data Layer

### `pending_articles`

DDL находится в `pending_articles_repo.py:70-93`; дополнительные колонки
добавляются в `_COLUMN_MIGRATIONS` (`pending_articles_repo.py:304-344`). Для
этого бага значимы:

- `link TEXT PRIMARY KEY` — идентификатор строки и review target;
- `fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP` — порядок очереди;
- `attempt_count` / `last_error` — publish strike state;
- `hold_reason TEXT NULL` — `NULL` означает обычную очередь, non-NULL означает
  бессрочное ожидание `[E036]`;
- `publish_after TIMESTAMP NULL` — UTC `YYYY-MM-DD HH:MM:SS`; future value
  временно скрывает строку, `NULL`/elapsed делает её публикуемой;
- `hold_count` — независимый счётчик LLM holds.

Значимые запросы и переходы:

- `list_pending()` (`pending_articles_repo.py:983`) включает только
  `hold_reason IS NULL` и `publish_after IS NULL OR <= CURRENT_TIMESTAMP`;
- `count_pending()` (`pending_articles_repo.py:1133`) использует тот же eligibility
  predicate и участвует в sizing слотов;
- `count_deferred()` (`pending_articles_repo.py:664`) считает только future
  `publish_after` при `hold_reason IS NULL`;
- `list_held()` (`pending_articles_repo.py:1096`) читает non-NULL `hold_reason`;
- `clear_deferral(link) -> bool` (`pending_articles_repo.py:575`) атомарно ставит
  `publish_after=NULL`, не меняя `hold_reason` и `hold_count`;
- `clear_hold(link) -> bool` (`pending_articles_repo.py:1410`) атомарно ставит
  `hold_reason=NULL`, не публикуя строку;
- `skip_pending(link)` (`pending_articles_repo.py:1377`) в одной транзакции
  записывает `processed_news` и удаляет pending row; в `published_articles` не
  пишет;
- `_connect()` (`pending_articles_repo.py:203`) открывает отдельный SQLite
  connection с `timeout=5.0`, что позволяет listener thread и publish loop
  переждать короткую конкурирующую запись.

### Publication and review bookkeeping

- `published_articles.published_at` — SQLite UTC timestamp; `MAX` читается через
  `get_max_published_at()` (`pending_articles_repo.py:1153`) для crash-loop guard и
  внутреннего `[E017]`.
- `bot_state` хранит `review_token:<token> -> <kind>|<link>`; helpers находятся в
  `pending_articles_repo.py:1698-1824`. Kind разделяет dedup и hold keyboards.
- Схема уже содержит все состояния, нужные для восстановления слотов. Сам по себе
  баг не требует новой таблицы или миграции.

## 3. Current Timeline and Root Cause

Фактический production timeline из incident triage:

1. В `10:00:31` intake поставил единственной `[E014]`-строке future
   `publish_after`; она стала видимой для `get_pending()` и невидимой для
   `list_pending()` / `count_pending()`.
2. `queue_size == 0`, `deferred_backlog == 1`; вызов
   `compute_fixed_slots(1, ~10:00)` вернул только `[10:00]`.
3. В `10:00:32` этот слот перечитал `list_pending()`, получил `[]` и выполнил
   `break` (`news_bot.py:4787-4793`). `job()` завершился; будущих 15:00/19:30
   opportunities в списке не существовало.
4. В `10:00:42` listener обработал `keep`, `clear_deferral()` сделал строку
   публикуемой и callback сообщил «выйдет в ближайший слот».
5. Scheduler уже не выполнялся, поэтому готовая строка осталась до следующего
   запуска. Cancel/reject в этом incident не происходили.

Корень состоит из двух независимых условий:

- список слотов ограничен числом строк на момент одного sizing (`eligible[:n]`);
- пустой slot read завершает весь loop, а не только текущую opportunity.

Изменение только `break -> continue` не исправляет incident: при `n=1` в списке
по-прежнему ровно один, уже прошедший 10:00 slot. Изменение только sizing тоже
недостаточно, если первый пустой read продолжает завершать loop.

Смежный текущий путь имеет ту же форму: `[E036] approve` освобождает
`hold_reason`, но held rows не входят ни в `count_pending`, ни в
`count_deferred`. Если других строк нет, `job()` получает zero slots и обещание
«в ближайший слот» также не обеспечено scheduler-ом.

## 4. Similar Features and Reusable Patterns

- `work/hold-cap/decisions.md:62-80` уже зафиксировал, что deferred rows должны
  «покупать» дневные slots. Реализация добавила `count_deferred()` в `n`, но
  исходила из того, что over-allocation безопасен, потому что empty queue делает
  `break`. Нынешний incident показывает конфликт этого допущения с асинхронным
  review release.
- `tests/test_integration.py:910-1001` и
  `tests/test_distributed_schedule_integration.py:599+` проверяют обратную гонку:
  оператор удалил/опубликовал row между слотами, следующий слот перечитал очередь
  и выбрал survivor. Это готовый pattern реальной tempfile SQLite + mid-loop DB
  mutation.
- `tests/test_job_distributed_publish.py:1428` проверяет только аргумент sizing:
  fully-deferred backlog должен попасть в `compute_fixed_slots`. Он не проверяет
  ожидание будущего слота и последующую публикацию.
- `news_bot._publish_with_retries` и slot-loop counters показывают принятый
  invariant: публикацию выполняет только main thread в фиксированном slot loop.
  Callback resolvers меняют состояние очереди, но сами не вызывают publish path.
- Внутренний `_maybe_ping_dry_spell()` (`news_bot.py:4966`) читает локальную БД,
  а внешний workflow — Telegraph outcome. Это намеренно разные сигналы: здоровый
  завершившийся tick может не создать публикацию.

## 5. Integration Points and Dependency Chain

### Scheduler/review path

```text
job intake
  -> insert_pending(publish_after and/or hold_reason)
  -> count_pending + count_deferred + list_held
  -> compute_fixed_slots (one time)
  -> main thread sleeps

review daemon thread
  -> parse callback -> auth -> token lookup
  -> clear_deferral / clear_hold / skip_pending
  -> edit + answer + decision log

main thread at each retained opportunity
  -> list_pending fresh read
  -> _publish_with_retries -> _fallback_publish
  -> move_to_published
```

Контракт фиксированных времён задаётся в
`compute_publish_slots.DAILY_PUBLISH_TIMES`; окно insurance —
`news_bot.WINDOW_END_TIME=20:00`; дневной максимум естественно ограничен тремя
элементами списка.

Если runtime получает дополнительные wake opportunities, три значения нужно
различать явно:

- publishable rows сейчас (`queue_size`);
- rows, которые callback/timer может освободить (`deferred` и, если scope
  включает `[E036]`, held);
- retained slot opportunities для повторного чтения очереди.

`carry_over` и E008 `Слоты сегодня` сейчас получают тот же `slots`, что runtime
loop (`admin_alerts.py:348-384`). Любое разведение plan slots и wake slots должно
оставить операторский текст и арифметику согласованными с фактическим loop.

### Watch path

```text
GitHub schedule / dispatch
  -> ssh-keyscan
  -> curl Telegraph getPageList(limit=1)
  -> inline Python path-date classifier
  -> verdict outputs
  -> Telegram send + open/close GitHub issue
```

Commit `066500b` уже изменил classifier на один `now_msk` для date и hour.
Локальный `origin/dev` содержит commit; локальный `origin/main` остановлен на
`2f31ab0` и его не содержит. Поэтому production workflow на `main` не получит
этот fix до promotion `dev -> main` независимо от server deploy.

Classifier сейчас встроен heredoc-ом и не импортируется тестами. Если production
workflow начнёт вызывать repo helper, ему потребуется checkout step: текущий
`uptime.yml` checkout намеренно не делает и задаёт `GH_REPO` явно
(`uptime.yml:185-195`). Это отдельная dependency/supply-chain поверхность.

## 6. Existing Tests

Framework: stdlib `unittest` style под `pytest`; реальная tempfile SQLite,
`unittest.mock.patch`, `freezegun` и `pytz`. CI на Python 3.13 запускает
`python -m pytest tests/ -v` (`.github/workflows/ci.yml:27-42`).

### Уже покрыто

- `tests/test_compute_fixed_slots.py::TestComputeFixedSlots` — eligibility,
  grace, cap, carry-over, timezone awareness. Представитель:
  `test_n1_at_10_00_single_slot`, который прямо фиксирует `[10:00]` для `n=1`.
- `tests/test_pending_articles_repo.py::TestPublishAfter` — фильтрация,
  `defer_publish`, `clear_deferral`, independence от hold и `count_deferred`.
- `tests/test_integration.py::TestResolveDedupCallback.test_keep_lifts_the_soft_flag_deferral`
  (`tests/test_integration.py:3350`) —
  проверяет только `publish_after is None`, `count_deferred()==0` и появление в
  `list_pending`; будущий реальный slot не запускает.
- `tests/test_job_distributed_publish.py::TestHoldCapFailurePaths.test_deferred_backlog_still_buys_the_day_its_slots`
  (`:1428`) — проверяет
  `compute_fixed_slots` input `2`, но mock возвращает `([], 0)`.
- `tests/test_integration.py::TestManualReviewPreemption.test_manual_review_preemption_skips_published_row`
  — production loop с
  mid-loop mutation и реальной SQLite.
- `tests/test_distributed_schedule_integration.py::TestDistributedSchedule.test_full_happy_path_three_articles_three_slots_three_publishes`
  — весь
  `job -> slot -> Telegraph/Telegram -> move_to_published` при замоканных внешних
  сервисах.
- `tests/test_integration.py::TestResolveHoldCallback` — approve/reject state
  transitions и status text, но не последующий scheduler slot.

### Непокрытые regression seams

- Полный incident: E014 row deferred перед sizing, первый 10:00 read пуст,
  `keep` после этого read, публикация в 15:00.
- Cancel после первого empty read: row не должна публиковаться ни в одной
  оставшейся opportunity.
- Silence по E014: future defer остаётся непубликуемым все оставшиеся slots и
  автоматически возвращается после expiry.
- Approve/reject `[E036]` после утреннего empty read, если обещание этой кнопки
  входит в scope.
- Решение после последнего 19:30 slot и точность callback status.
- Взаимодействие retained opportunities с `carry_over`, E008/E009, E034,
  `_maybe_ping_dry_spell()` и `_record_heartbeat()`.
- В `tests/` нет тестов classifier-а из `.github/workflows/uptime.yml`; не
  закреплены границы 20:59/21:00 MSK, 00:00-02:59, 03:00 и Dec/Jan rollover.

Для timeline test подходит существующий `_DistribLoopBase`, но простой frozen
clock не двигается после mocked `time.sleep`. Надёжный тест должен использовать
side effect первого slot wait / `list_pending` read для реального
`resolve_dedup_callback`, затем убедиться, что следующий выбранный row дошёл до
publish path. Positive control обязателен: только `call_count` не доказывает,
какая ссылка опубликована.

## 7. Shared Utilities

- `compute_fixed_slots` — чистая wall-clock арифметика; естественная точка unit
  coverage для retained opportunities, если это правило будет вынесено из
  `job()`.
- `pending_repo.list_pending()` — единый eligibility truth source; slot loop уже
  перечитывает его, поэтому callback не нужен отдельный in-memory signal.
- `pending_repo.count_deferred()` / `list_held()` — существующие чтения для
  определения потенциально освобождаемого backlog; нового persistent state нет.
- `sanitize_error_message` и fail-open wrappers в `job()` — обязательный pattern
  для DB/alert failures.
- `admin_alerts.alert_plan_of_day` / `alert_quiet_day` — чистые builders,
  покрываемые unit tests; сюда попадают slots/carry/held/deferred counts.
- Для watcher подходящего чистого helper сейчас нет; production logic живёт
  только внутри workflow heredoc.

## 8. Potential Problems and Integration Risks

1. **Partial scheduler fix.** `break -> continue` без резервирования оставшихся
   времён и резервирование без изменения empty-queue branch по отдельности не
   закрывают production timeline.
2. **Scope asymmetry.** Исправление только `publish_after` оставляет аналогичное
   ложное обещание у `[E036] approve`; включение held rows, напротив, означает,
   что тихий held backlog удерживает `job()` живым до последней opportunity.
3. **Heartbeat/recap timing.** `_maybe_ping_dry_spell()` и `_record_heartbeat()`
   выполняются только после slot loop (`news_bot.py:4925-4963`). Retained empty
   opportunities продлевают tick до 19:30; это уже происходит для обычного
   трёхслотового дня, но станет новым поведением для полностью withheld queue.
4. **Operator-plan drift.** Передача искусственного `n=MAX_DAILY_POSTS` прямо в
   текущий helper меняет `carry_over` и может представить wake opportunities как
   запланированные посты. Runtime opportunities, queue count и carry arithmetic
   нельзя молча считать одним числом.
5. **No callback-side publish.** Вызов publish прямо из listener создаст второго
   concurrent publisher, обойдёт fixed-time pacing, crash-loop guard и
   slot-derived `<=3/day`. Текущий безопасный invariant — callback только меняет
   SQLite, публикует main thread.
6. **Boundary decision.** После 19:30 освобождённая строка физически может выйти
   только на следующий день; текущая строка «в ближайший слот» не сообщает день.
7. **Existing mock weakness.** Часть `TestDistributedPublishLoop` мокает
   `_fallback_publish` без удаления pending row и утверждает только call count;
   такие тесты не доказывают row identity и могут пропустить повторный выбор head.
8. **Watch deployment skew.** Same-clock fix `066500b` есть в `dev`, но нет в
   `origin/main`. Локальные тесты нового helper-а сами по себе не меняют
   исполняемый scheduled workflow, пока `dev -> main` не смержен.
9. **Watch classifier shape.** Он видит только `MM-DD`, восстанавливает год по
   правилу `last > today`, fail-open трактует пустой список/API/JSON/path errors
   как inconclusive healthy. Boundary tests должны сохранять эти сознательные
   fail-open ветки отдельно от stale arithmetic.
10. **Workflow extraction coupling.** Helper в repo требует checkout; дублирование
    формулы в helper и heredoc создаст два источника истины и тесты не будут
    проверять production body.
11. **Security invariants.** Review auth, token kind check и parameterized SQL уже
    находятся до state change; scheduler fix не должен переносить URL/token в
    callback payload/log или ослаблять эти gates. Watcher secrets должны оставаться
    в env и не попадать в вывод тестовых fixtures.

## 9. Constraints and Infrastructure

- Фиксированные publish times: 10:00, 15:00, 19:30 Europe/Moscow; grace 5 минут;
  максимум три channel publications в день.
- `publish_after` хранится и сравнивается в UTC через SQLite
  `CURRENT_TIMESTAMP`; slot wall clock — timezone-aware MSK.
- E014 silence означает auto-release после 24 часов; E036 silence означает
  бессрочный hold. Cancel/reject не публикуют.
- Production — единственный bot/channel; staging и test bot отсутствуют. Live
  E2E и искусственный channel post небезопасны; проверка до deploy — pytest на
  tempfile DB с mocked Telegram/Telegraph/LLM.
- Ветка разработки `dev`; promotion в `main` только PR. Merge не deploy-ит
  runtime bot. Server rebuild/restart выполняет оператор вручную вне окна
  10:00-20:00 MSK.
- Изменение `.github/workflows/uptime.yml` начинает действовать после merge в
  default branch и не требует server rebuild.
- Runtime: Python 3.13, `schedule==1.2.1`, `pytz`, SQLite, long-lived Docker
  container. Новая внешняя библиотека для этого contour не требуется.
- Pre-commit проверяет secrets, whitespace, conflicts, YAML; CI запускает весь
  `tests/` для `.py`/workflow changes.

## 10. Minimal Files Likely to Change

Обязательный runtime/test contour:

- `news_bot.py` — sizing/retained-opportunity logic и empty-read branch;
- `tests/test_job_distributed_publish.py` **или** `tests/test_integration.py` —
  production timeline на tempfile SQLite;
- `compute_publish_slots.py` + `tests/test_compute_fixed_slots.py` — только если
  правило retained opportunities оформляется как чистая scheduling policy, а не
  orchestration внутри `job()`.

Условные файлы по принятым решениям:

- `admin_alerts.py` + `tests/test_admin_alerts.py` — если меняется значение
  `Слоты сегодня` или wording про день следующего слота;
- `pending_articles_repo.py` + `tests/test_pending_articles_repo.py` — только если
  понадобится единый aggregate query; текущих `count_deferred()` и `list_held()`
  достаточно без миграции;
- отдельный stdlib helper для publication staleness + его unit test,
  `.github/workflows/uptime.yml` — если production heredoc заменяется
  импортируемой/testable логикой; workflow тогда должен получить checkout;
- project-knowledge docs — если фактическая scheduler/watch architecture или
  deploy surface изменится.

## 11. External Libraries

Новые external APIs или библиотеки для реализации не нужны. Задействованы только
уже закреплённые зависимости (`schedule`, `pytz`, python-telegram-bot) и stdlib /
SQLite. Watcher использует предустановленные на GitHub runner `python3`, `curl`,
`ssh-keyscan` и `gh`; исследование внешней документации не требовалось.
