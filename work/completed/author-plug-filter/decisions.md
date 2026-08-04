# Decisions Log: author-plug-filter

Agent reports on completed tasks. Each entry is written by the agent that executed the task.

---

<!-- Entries are added by agents as tasks are completed.

Format is strict — use only these sections, do not add others.
Do not include: file lists, findings tables, JSON reports, step-by-step logs.
Review details — in JSON files via links. QA report — in logs/working/.

## Task N: [title]

**Status:** Done
**Commit:** abc1234
**Agent:** [teammate name or "main agent"]
**Summary:** 1-3 sentences: what was done, key decisions. Not a file list.
**Deviations:** None / Deviated from spec: [reason], did [what].

**Reviews:**

*Round 1:*
- code-reviewer: 2 findings → [logs/working/task-N/code-reviewer-1.json]
- security-auditor: OK → [logs/working/task-N/security-auditor-1.json]

*Round 2 (after fixes):*
- code-reviewer: OK → [logs/working/task-N/code-reviewer-2.json]

**Verification:**
- `npm test` → 42 passed
- Manual check → OK

-->

## Фича целиком — восстановлено 2026-08-03

**Записано задним числом.** Эта фича была выкачена 2026-05-04, но `decisions.md`
остался нетронутым шаблоном. Аудит гигиены 2026-08-03 заметил расхождение:
техспек описывает модуль `author_plug_filter.py`, которого нет в репозитории.
Ниже — реконструкция по коммитам `2281cd4` / `695b201` / `0ef3ed3` и
`work/SESSION-2026-05-04.md`. Пофазового разбиения на задачи у фичи не было
(папки `tasks/` нет), поэтому запись одна, на всю поставку.

**Status:** Done
**Commit:** `695b201` (реализация), `0ef3ed3` (журнал сессии)
**Agent:** main agent

**Summary:** Двухуровневая защита от утечек авторских соцсетевых плашек вроде
«(подписывайтесь на меня в Instagram @diecast215)» — реальный случай на проде
2026-05-02 14:40. Уровень A: пять новых паттернов в `boilerplate_filter.py`,
снимают плашку целым абзацем ДО перевода, покрывают 10 платформ; порог длины
поднят 80 → 120, легаси-паттерн `^follow us on \w+` удалён как перекрытый.
Уровень B: удаление плашки внутри абзаца ПОСЛЕ перевода — одна точка вызова в
`_fallback_publish` в месте схождения всех движковых веток. Плюс мягкая защита в
промпте: `ux-guidelines.md` явно называет такие вставки допустимым дропом, чтобы
модель отдавала уже чистый текст.

**Deviations:** **Отклонение от техспека, осознанное.** Техспек (`2281cd4`,
раздел Variant B) предписывал новый модуль `author_plug_filter.py` с публичным
контрактом `strip_author_plugs` / `strip_in_paragraphs` / `strip_in_blocks`, плюс
`tests/test_author_plug_filter.py`, плюс добавление модуля в оба манифеста
деплоя. Вместо этого код лёг функциями `_strip_plugs` и `_strip_plugs_in_blocks`
прямо в `news_bot.py` (сегодня — `news_bot.py:1285-1322`), а тесты — в
`tests/test_translation.py` (+14 тестов).

Причина зафиксирована в `work/SESSION-2026-05-04.md:107-112`: реализация должна
была остаться «strictly additive» и следовать конвенциям проекта — «existing
logger style, existing redaction filter, **no new modules**». То есть техспек
оказался тяжелее задачи: 154 строки в одном файле не оправдывали отдельный
модуль, запись в трёх манифестах деплоя и связанный с этим риск R7.

Эта же поставка породила правило `feedback_molyanov_default_with_pushback`
(`work/SESSION-2026-05-04.md:114-118`): для фич размера S и меньше 100 строк
агент обязан предложить короткий путь `/write-code` вместо полного цикла
планирования. То есть расхождение спека и кода здесь — не небрежность, а
зафиксированный урок про избыточный процесс.

**Последствие, которое надо знать.** Техспек `work/author-plug-filter/tech-spec.md`
как описание архитектуры **недостоверен**: `author_plug_filter.py`,
`tests/test_author_plug_filter.py` и записи в манифестах деплоя не существуют и
никогда не существовали. Читать его как исторический документ «что задумывалось»,
а не как карту кода. Актуальное описание уровней фильтрации —
`patterns.md § Service-text stripping — three granularities`.

**Reviews:** JSON-отчётов ревью по этой фиче не сохранилось — `work/*/logs/`
попадал под правило `logs/` в `.gitignore` до 2026-08-03. По телу коммита
`695b201` ревью проводились; отдельных файлов нет.

**Verification:**
- `python3 -m pytest -q` на момент поставки → 740 passed (было 682, +58 новых)
- Явно сохранены (негативные контроли): корпоративное «Follow Mattel on
  Instagram» и журналистское «(see photo on Instagram)» без `@handle`
- Ручной путь `hw_review publish` фичей не затронут — он не проходит через
  `_fallback_publish`; плашки там снимает оператор на стадии `stage`
- Пост-деплой проверка на проде: **не проводилась** (подтверждено оператором
  2026-08-03)

---

## Закрытие — 2026-08-04

Фича выкачена и работает; папка перенесена в `work/completed/`.

Отсев авторских рекламных хвостов из RU-вывода. Модуля `author_plug_filter.py` не существует и не должно: 154 строки не оправдывали отдельный модуль плюс три записи в манифестах деплоя (`SESSION-2026-05-04.md:107-112`). Живёт как `_strip_plugs` в `news_bot.py`. Именно эта поставка породила правило про `/write-code` для мелких фич.
