# Tech-spec self-validation — dedup-review-buttons

**Дата:** 2026-07-22
**Причина ручного ревью:** валидаторы-агенты (skeptic, completeness-validator,
security-auditor, test-reviewer, tech-spec-validator) недоступны — инфраструктура
запуска субагентов падает на 401/403 (та же проблема, что и на этапе user-spec).
Ревью выполнено основной моделью по тем же 5 измерениям.

## 1. Mirage detector (ссылки на код) — PASS

Все ссылки подтверждены в коде:
- `pending_articles_repo.skip_pending` — L727; `_connect` — L174; `get_published`
  — L404; `list_pending` — L432.
- `news_bot.send_admin_notification(message, *, max_attempts=…)` — L469;
  E014 send site — L2374-2397; `main()` loop + singleton flock — L2820/2841;
  `INSTANCE_LABEL` L105, `TELEGRAM_ADMIN_ID` L102; imports L46.
- `admin_alerts.alert_cross_source_dupe` — L702. `bot_state` table — init_schema.
- PTB `==21.10` (requirements.txt). `secrets` — stdlib.
- Тест-файлы существуют: test_admin_alerts.py, test_admin_ping.py,
  test_integration.py, test_pending_articles_repo.py.

## 2. Completeness + adequacy — PASS

- Все требования user-spec покрыты (кнопки только под E014, cancel→очередь,
  keep, статус-фидбэк, missed-window, идемпотентность, admin-only, prod-only,
  параллельность, финальность отмены, регресс-совместимость, логирование решения).
- Не переусложнено: переиспользуем `skip_pending`, без новых таблиц/модулей,
  без фреймворка Application, без webhook. Не недоделано: конкуренция
  (busy_timeout), auth (fail-closed), изоляция ошибок listener, гейт.
- Размер M адекватен.

## 3. Security — PASS (с заметками)

- Auth: числовой admin, fail-closed при не-числовом id.
- Нет утечки секретов: текст сообщения = оригинал + статус; токен не секрет;
  send уже проходит `_redact_text`.
- callback_data парсится строго по префиксу `dd:<c|k>:<token>`; чужие/битые —
  игнор. allowed_updates=['callback_query'] отсекает произвольные апдейты.
- DoS-поверхность: get_updates обрабатывает входящие, но действие — только на
  admin; не-admin молча игнорируется (без ответа), краха на мусорных апдейтах нет.

## 4. Testing strategy — PASS

- M: unit (клавиатура, форвардинг reply_markup, токен-стор, все ветки
  resolve_dedup_callback, гейт/fail-closed) + integration на реальной SQLite
  (cancel→не опубликовано; post-publish→«уже опубликовано»; busy_timeout под
  конкуренцией). E2E автоматом нет — обосновано (нужен живой бот; слушает только
  прод) → ручная проверка оператором.

## 5. Template + waves — PASS

- Все секции на месте; frontmatter (created/status=draft/branch=dev/size=M).
- Каждое решение якорится на user-spec либо помечено [TECHNICAL].
- User-Spec Deviations заполнены (env-флаг + рендер кнопок на тесте — оба
  [PENDING USER APPROVAL]).
- 12 задач (≤15). Исправлен дублирующий заголовок Wave 2.

**Вердикт:** tech-spec готов к утверждению. Два пункта из "User-Spec Deviations"
требуют явного согласия оператора (см. ниже в сообщении).
