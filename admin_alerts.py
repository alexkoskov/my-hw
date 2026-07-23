"""Центральный каталог админ-алертов: тексты + коды для дебага.

Все сообщения, которые бот отправляет в админ-чат, собраны здесь как
builder-функции с уникальным кодом ошибки [E0XX]. Цели:

* Один источник правды — править формулировки в одном месте.
* Грепаемые коды — `journalctl … | grep E001` находит все срабатывания.
* Юнит-тестируемо — каждая функция чистая (str -> str).
* Единый формат для админа — severity-эмодзи, заголовок, "Причина:", "Что
  сделать:". Несрочный info (heartbeat) — короткий и без советов.

Severity-эмодзи (используются в первой строке):
    🟢  info / штатное событие, действие не требуется
    🟡  warning — что-то не идеально, но бот работает
    🔴  critical — деградация или полная остановка функционала

Подстроки, на которых висят интеграционные тесты, сохранены дословно:
    'План на сегодня', 'Бот сработал', 'Принято свежих',
    '⚠️ Пропущен дубль публикации'.
"""
from __future__ import annotations

from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ---------------------------------------------------------------------------
# E001 — feeds.json пуст / сломан / отсутствует
# ---------------------------------------------------------------------------
def alert_no_rss_feeds(reason: str) -> str:
    return (
        f"[E001] 🔴 Нет RSS-источников\n\n"
        f"Причина: {reason}\n\n"
        f"Что сделать: проверь feeds.json в репо — формат, валидность JSON, "
        f"наличие хотя бы одного URL."
    )


# ---------------------------------------------------------------------------
# E002 — RSS-источник упал на fetch
# ---------------------------------------------------------------------------
def alert_source_fetch_failed(source_name: str, error: str) -> str:
    return (
        f"[E002] 🟡 Источник {source_name} не отвечает\n\n"
        f"Ошибка: {error}\n\n"
        f"Что сделать: бот попробует снова в следующем cron-тике. Если "
        f"повторяется — проверь доступность сайта или его HTML-структуру "
        f"(парсер мог сломаться)."
    )


# ---------------------------------------------------------------------------
# E003 — очередь слишком большая
# ---------------------------------------------------------------------------
def alert_backlog_warning(queue_size: int, threshold: int, carry_over: int) -> str:
    return (
        f"[E003] 🟡 Очередь распухла\n\n"
        f"В очереди: {queue_size} (порог: {threshold})\n"
        f"Перенесено на завтра: {carry_over}\n\n"
        f"Что сделать: либо опубликовать вручную через hw_review.py, "
        f"либо подождать — окно публикаций «съест» очередь за несколько дней."
    )


# ---------------------------------------------------------------------------
# E004 — Claude probe упал на старте; посты придержаны до восстановления
# ---------------------------------------------------------------------------
def alert_claude_probe_failed_at_startup() -> str:
    return (
        f"[E004] 🟡 Claude недоступен на старте\n\n"
        f"Что произошло: проверка Claude API при запуске не прошла. "
        f"Посты придержаны — опубликую их нормально, как только Claude "
        f"вернётся (автопереводом через Google ничего не публикуется).\n\n"
        f"Что сделать: ничего — бот сам пробует Claude на каждом слоте. "
        f"Если ситуация повторяется несколько дней, проверь "
        f"ANTHROPIC_API_KEY и статус api.anthropic.com."
    )


# ---------------------------------------------------------------------------
# E005 — TZ контейнера ≠ Europe/Moscow
# ---------------------------------------------------------------------------
def alert_tz_mismatch(actual_tz: str | None) -> str:
    return (
        f"[E005] 🟡 TZ контейнера = {actual_tz!r}, ожидалось 'Europe/Moscow'\n\n"
        f"Что произошло: переменная окружения TZ на сервере не Europe/Moscow.\n"
        f"На cron-расписание это не влияет (явная pytz-таймзона), но логи "
        f"будут со «странным» временем.\n\n"
        f"Что сделать: проверь конфигурацию TZ в systemd-юните или .env "
        f"на сервере."
    )


def alert_prod_db_guard(warnings: list[str]) -> str:
    """[E018] prod DB integrity guard fired at startup (B2). ``warnings`` is a
    non-empty list of human-readable problems (relative DB_FILE and/or empty
    processed_news)."""
    body = "\n".join(f"• {w}" for w in warnings)
    return (
        f"[E018] 🔴 Проверка БД на старте не прошла\n\n"
        f"Что произошло:\n{body}\n\n"
        f"Почему важно: если это не первый деплой, значит контейнер поднялся с "
        f"ПУСТОЙ или временной базой (не на смонтированном /data). Тогда бот "
        f"перезальёт канал старыми статьями.\n\n"
        f"Что сделать СРОЧНО: проверь на сервере, что в .env есть строка "
        f"DB_FILE=/data/news.db и что /root/hw-news/data/news.db на месте и не "
        f"пустой. При сомнении останови контейнер (docker compose stop), пока "
        f"не разберёшься — чтобы не залить канал."
    )


def alert_openrouter_low_balance(remaining: float, threshold: float) -> str:
    """[E019] OpenRouter account balance dropped below the alert threshold.
    Proactive warning so the operator tops up BEFORE credits hit zero and
    transcreation starts failing."""
    return (
        f"[E019] 🟡 OpenRouter: осталось ~${remaining:.2f}\n\n"
        f"Что произошло: баланс OpenRouter опустился ниже порога ${threshold:.2f}.\n\n"
        f"Что сделать: пополни баланс на https://openrouter.ai/ (Settings → "
        f"Credits). Иначе кредиты кончатся, переводы начнут падать и посты "
        f"придержатся до пополнения."
    )


# ---------------------------------------------------------------------------
# E006 — idempotency-guard поймал зомби-строку (дубль публикации)
# ---------------------------------------------------------------------------
def alert_duplicate_publish_skipped(link: str) -> str:
    # Сохраняем дословно строку «⚠️ Пропущен дубль публикации» — на ней
    # висит test_fallback_publish_paths.py:771.
    return (
        f"[E006] ⚠️ Пропущен дубль публикации\n\n"
        f"Ссылка:\n{link}\n\n"
        f"Что произошло:\n"
        f"статья уже опубликована,\n"
        f"зомби-строка убрана из очереди.\n\n"
        f"Что сделать:\n"
        f"расследовать, откуда взялась зомби-строка\n"
        f"(crontab, backup_db.sh, journalctl)."
    )


# ---------------------------------------------------------------------------
# E007 — не удалось убрать зомби-строку
# ---------------------------------------------------------------------------
def alert_zombie_cleanup_failed(link: str, error_type: str) -> str:
    return (
        f"[E007] 🟡 Не удалось снять зомби-строку\n\n"
        f"Ссылка:\n{link}\n\n"
        f"Ошибка: {error_type}\n\n"
        f"Что сделать:\n"
        f"ничего — повторим на следующем слоте."
    )


# ---------------------------------------------------------------------------
# Intake-funnel diagnostic (watchdog) — shared by E008/E009.
#
# ``news_bot.job()`` step (b) accumulates a plain-int funnel dict per tick.
# These helpers render it for the operator ping so a quiet day pinpoints
# WHERE intake collapsed (fetch / filters / dedup) instead of the opaque
# «новых статей нет». Contract: PURE, DETERMINISTIC, plain-text (no markdown
# / parse_mode), and NEVER raises — a non-dict funnel degrades to "" so the
# ping falls back to its legacy single line (a dict with bad-valued fields
# instead renders a zeroed breakdown). Covers INTAKE/STAGING only;
# translation/posting happen later at slots and are shown as not-applicable
# on a quiet day.
# ---------------------------------------------------------------------------
def _funnel_int(funnel: dict, key: str) -> int:
    """Best-effort non-negative int read from the funnel dict; never raises.

    Counters in ``job()`` only ever increment, so a negative value would be a
    bug upstream — clamp to 0 to honour the "non-negative" contract regardless.
    """
    try:
        return max(0, int(funnel.get(key, 0) or 0))
    except Exception:
        return 0


def _funnel_collapse_note(
    sources: int, failed: int, new: int,
    no_article: int, checklist: int, block: int, staged: int,
) -> str:
    """One-line pinpoint of the stage where intake collapsed. Returns "" when
    something WAS staged (no collapse to report). ``dedup_degraded`` is not a
    drop (those articles still publish) so it is never a collapse cause."""
    try:
        if staged > 0:
            return ""
        if sources == 0:
            if failed > 0:
                return f"Где схлопнулось: источники не ответили ({failed})"
            return "Где схлопнулось: источники не дали новых записей"
        if new == 0:
            return "Где схлопнулось: все записи уже известны (фильтры отсеяли всё)"
        # new > 0 but nothing staged — the loop dropped every candidate.
        drops = (
            ("дубль-блок", block),
            ("нет статьи/текста", no_article),
            ("чеклист без текста", checklist),
        )
        stage, count = max(drops, key=lambda kv: kv[1])
        if count > 0:
            return f"Где схлопнулось: {stage} ({count})"
        return "Где схлопнулось: обработка статей (детали в логах)"
    except Exception:
        return ""


def _format_funnel(funnel: dict) -> str:
    """Render the intake-funnel breakdown as a plain-text multi-line block.

    Fail-safe: a non-dict ``funnel`` returns "" so the caller falls back to
    the legacy single-line ping. A dict with bad-valued fields does NOT fall
    back — each bad field is coerced to 0 and a zeroed breakdown is rendered.
    Never raises.
    """
    if not isinstance(funnel, dict):
        return ""
    try:
        sources = _funnel_int(funnel, "sources_fetched")
        failed = _funnel_int(funnel, "sources_failed")
        new = _funnel_int(funnel, "new_count")
        no_article = _funnel_int(funnel, "dropped_no_article")
        checklist = _funnel_int(funnel, "dropped_checklist")
        block = _funnel_int(funnel, "dropped_dedup_block")
        degraded = _funnel_int(funnel, "dedup_degraded")
        staged = _funnel_int(funnel, "staged")

        lines = [
            "Воронка приёма за тик:",
            # ``sources`` is len(all_entries) — total items fetched across all
            # sources, NOT a source count — so label it as records to avoid the
            # "N sources responded" misreading. ``failed`` IS a source count.
            f"• получено записей: {sources} (источников не ответило: {failed})",
            f"• новых после фильтров: {new}",
            f"• отсеяно: нет статьи {no_article}, "
            f"чеклист {checklist}, дубль-блок {block}",
            f"• дедуп degraded (всё равно опубликованы): {degraded}",
            f"• добавлено в очередь: {staged}",
        ]
        note = _funnel_collapse_note(
            sources, failed, new, no_article, checklist, block, staged,
        )
        if note:
            lines.append(note)
        return "\n".join(lines)
    except Exception:
        return ""


def _format_funnel_line(funnel: dict) -> str:
    """Compact one-line intake summary for the busy-day plan-of-day ping.

    Fail-safe like ``_format_funnel``: non-dict / malformed → "".
    """
    if not isinstance(funnel, dict):
        return ""
    try:
        sources = _funnel_int(funnel, "sources_fetched")
        failed = _funnel_int(funnel, "sources_failed")
        new = _funnel_int(funnel, "new_count")
        staged = _funnel_int(funnel, "staged")
        dropped = (
            _funnel_int(funnel, "dropped_no_article")
            + _funnel_int(funnel, "dropped_checklist")
            + _funnel_int(funnel, "dropped_dedup_block")
        )
        failed_part = f", источники-сбои {failed}" if failed else ""
        # ``sources`` is the fetched-item count (len(all_entries)), not a source
        # count — label it "получено" so the compact line matches the full
        # breakdown's first bullet. ``источники-сбои`` IS a source count.
        return (
            f"Приём: получено {sources} → новых {new} → "
            f"в очередь {staged} (отсеяно {dropped}{failed_part})"
        )
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# E008 — heartbeat: busy day (план на сегодня)
# ---------------------------------------------------------------------------
def alert_plan_of_day(
    inserted: int, queue_size: int, slots: List, carry_over: int,
    funnel: Optional[dict] = None,
) -> str:
    # Сохраняем подстроки 'План на сегодня', 'Принято свежих' —
    # на них висят test_distributed_schedule_integration / test_job_prep_phase.
    #
    # ``funnel`` (optional, backward-compatible) добавляет компактную строку
    # воронки приёма. Легаси-вызов без funnel рендерит прежний текст один-в-один.
    slot_strs = ", ".join(s.strftime("%H:%M") for s in slots) or "—"
    base = (
        f"[E008] 🟢 План на сегодня\n\n"
        f"Принято свежих: {inserted}\n"
        f"Всего в очереди: {queue_size}\n"
        f"Слоты сегодня: {slot_strs}\n"
        f"Перенесено на завтра: {carry_over}"
    )
    try:
        line = _format_funnel_line(funnel) if funnel is not None else ""
    except Exception:
        line = ""
    if line:
        return f"{base}\n{line}"
    return base


# ---------------------------------------------------------------------------
# E009 — heartbeat: quiet day (новых статей нет)
# ---------------------------------------------------------------------------
def alert_quiet_day(funnel: Optional[dict] = None) -> str:
    # Сохраняем подстроку 'Бот сработал' — на ней висит test_job_prep_phase.
    #
    # ``funnel`` (optional, backward-compatible): при наличии дописываем
    # читаемую воронку приёма — где именно схлопнулся intake (fetch/фильтры/
    # дедуп) — плюс scope-заметку, что перевод/пост неприменимы (очередь пуста).
    # Легаси-вызов без funnel возвращает прежнюю однострочную формулировку.
    base = "[E009] 🟢 Бот сработал, новых статей нет."
    try:
        block = _format_funnel(funnel) if funnel is not None else ""
    except Exception:
        block = ""
    if block:
        return f"{base}\n\n{block}\nперевод/пост — очередь пуста"
    return base


# ---------------------------------------------------------------------------
# E034 — end-of-tick PUBLISH-stage recap (companion to the E008/E009 intake
# funnel).
#
# ``news_bot.job()`` step (e) accumulates plain-int outcome counters across the
# day's publish slots (published / held / failed / moved_to_failed) plus a
# de-duped, capped list of ``(link, reason)`` for the per-article failures that
# survived retries. This builder renders them so the operator sees WHAT posted
# and WHY a post failed — before this, a 'failed' reason went to the LOG only.
# Contract mirrors the funnel helpers: PURE, DETERMINISTIC, plain-text (no
# markdown / parse_mode), reasons already sanitized upstream
# (``sanitize_error_message``), and NEVER raises — a non-dict / malformed recap
# degrades to a minimal safe line. The caller ALSO wraps build+send in
# try/except and runs this AFTER all publishing, so a recap fault can never
# touch a post.
# ---------------------------------------------------------------------------
#: Cap on failure reasons rendered in the recap (``job()`` also caps its list).
#: SINGLE SOURCE OF TRUTH for this cap — ``news_bot.PUBLISH_RECAP_MAX_FAILURES``
#: is derived from this constant (imports it) so the two can never drift. The
#: builder still re-clamps defensively here (defence-in-depth: the caller's
#: collected list is already capped, but a hand-built recap dict might not be).
RECAP_MAX_FAILURES = 5
#: Defensive per-reason length clamp. Reasons are pre-sanitized, but this keeps
#: the ping compact and blunts any accidental blob from an odd exception.
_RECAP_REASON_MAXLEN = 200


def _recap_failure_lines(failures) -> List[str]:
    """Render up to ``RECAP_MAX_FAILURES`` «провал: <link> — <reason>» lines
    from a list of ``(link, reason)`` pairs. Reasons are already sanitized
    upstream; this only clamps length + shape. Never raises — a malformed entry
    is skipped."""
    out: List[str] = []
    if not isinstance(failures, (list, tuple)):
        return out
    for entry in failures:
        if len(out) >= RECAP_MAX_FAILURES:
            break
        try:
            link, reason = entry
        except Exception:
            continue
        link_s = str(link) if link is not None else "?"
        reason_s = str(reason) if reason is not None else ""
        if len(reason_s) > _RECAP_REASON_MAXLEN:
            reason_s = reason_s[:_RECAP_REASON_MAXLEN] + "…"
        out.append(f"провал: {link_s} — {reason_s}")
    return out


def alert_publish_recap(recap: dict) -> str:
    """Render the end-of-tick publish recap ([E034]).

    ``recap`` is a plain dict built by ``news_bot.job()`` step (e):
        published        int  — posts published this tick
        held             int  — slots HELD on a Claude/LLM outage (nothing posted)
        failed           int  — per-article failures that survived retries
        moved_to_failed  int  — subset of ``failed`` moved to failed_articles (≥3 strikes)
        failures         list[(link, reason)] — capped, already sanitized

    All-clean (held == failed == 0) → compact 🟢 one-liner «опубликовано N/N».
    Any held/failed → expanded 🟡 with the tally + a held note + the per-failure
    list. ``_funnel_int`` is reused as the shared best-effort non-negative int
    reader (same increment-only-counter contract as the funnel dict).
    """
    code = "[E034]"
    if not isinstance(recap, dict):
        # A malformed/non-dict recap is a degraded state, not an all-clear:
        # use 🟡 to match the inner-exception fallback below (both mean "recap
        # unusable" and both warrant operator attention, not a green tick).
        return f"{code} 🟡 Публикация: отчёт недоступен"
    try:
        published = _funnel_int(recap, "published")
        held = _funnel_int(recap, "held")
        failed = _funnel_int(recap, "failed")
        moved = _funnel_int(recap, "moved_to_failed")
        total = published + held + failed

        if held == 0 and failed == 0:
            # Clean tick — compact heartbeat line (denominator == published).
            return f"{code} 🟢 Публикация: опубликовано {published}/{total}"

        lines = [
            f"{code} 🟡 Публикация: опубликовано {published}/{total}",
            "",
        ]
        if held > 0:
            lines.append(f"придержано {held} (Claude недоступна)")
        if failed > 0:
            tail = f" (снято после 3 промахов: {moved})" if moved > 0 else ""
            lines.append(f"провалов: {failed}{tail}")
            lines.extend(_recap_failure_lines(recap.get("failures")))
        return "\n".join(lines)
    except Exception:
        # Belt-and-suspenders: the field reads above already fail safe, but a
        # recap ping must never raise — the caller treats this as best-effort.
        return f"{code} 🟡 Публикация: отчёт частично недоступен"


# ---------------------------------------------------------------------------
# E010 — outage ping #1 (Claude API упала, первое уведомление)
# ---------------------------------------------------------------------------
def alert_outage_first_ping() -> str:
    return (
        f"[E010] 🟡 Claude API недоступна\n\n"
        f"Что произошло: транскреация через Claude вернула ошибку.\n"
        f"Посты придерживаются — опубликую нормально, как только Claude "
        f"вернётся.\n\n"
        f"Что сделать: ничего — через 1 час пришлю ещё один пинг "
        f"со статусом."
    )


# ---------------------------------------------------------------------------
# E011 — outage ping #2 (прошёл час, всё ещё недоступна)
# ---------------------------------------------------------------------------
def alert_outage_second_ping() -> str:
    return (
        f"[E011] 🔴 Claude API всё ещё недоступна (1 час)\n\n"
        f"Что произошло: прошёл час с первого пинга, Claude API не "
        f"вернулась в строй.\n\n"
        f"Что сделать: ничего — посты придержаны и выйдут автоматически, "
        f"как только Claude восстановится. Если хочешь — глянь статус "
        f"api.anthropic.com."
    )


# ---------------------------------------------------------------------------
# E012 — outage: прошло 2 часа, всё ещё недоступна; посты придержаны
# ---------------------------------------------------------------------------
def alert_outage_still_down() -> str:
    return (
        f"[E012] 🔴 Claude API недоступна более 2 часов\n\n"
        f"Что произошло: Claude API не отвечает уже более 2 часов. Посты "
        f"придержаны до восстановления — машинного автоперевода не будет.\n\n"
        f"Что сделать: ничего — на следующем успешном Claude-вызове бот "
        f"сам опубликует придержанные посты (E013)."
    )


# ---------------------------------------------------------------------------
# E013 — outage recovery (Claude API ожила)
# ---------------------------------------------------------------------------
def alert_outage_recovery() -> str:
    return (
        f"[E013] 🟢 Claude API восстановилась\n\n"
        f"Что произошло: Claude снова отвечает успехом — публикую "
        f"придержанные посты.\n\n"
        f"Что сделать: ничего — бот сам."
    )


# ---------------------------------------------------------------------------
# E017 — канал молчит несколько дней (бот жив, но ничего не публикуется)
# ---------------------------------------------------------------------------
def alert_channel_silent(days: int) -> str:
    return (
        f"[E017] ⚠️ Канал молчит {days}+ дней\n\n"
        f"Что произошло: за последние {days} дней не вышло ни одного поста. "
        f"Бот работает, но публиковать нечего — возможно, фильтр отсекает "
        f"всё, иссяк источник, или мешает сеть сервера.\n\n"
        f"Что сделать: глянь логи "
        f"(`sudo journalctl -u news_bot.service | tail -50`) — ищи "
        f"«Filtered out», «held», ошибки сети."
    )


# ---------------------------------------------------------------------------
# Source-fetcher alerts (E020-E030).
# Эти точки шлют алерт изнутри парсеров источников через notifier-callback,
# в отличие от E001-E013 которые живут в news_bot.py / outage_state.py.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# E020 — Mattel: HTTP-ошибка при запросе списка новостей
# ---------------------------------------------------------------------------
def alert_mattel_news_http_error(error_type: str) -> str:
    return (
        f"[E020] 🟡 Mattel: HTTP-ошибка при запросе ленты новостей\n\n"
        f"Ошибка: {error_type}\n\n"
        f"Что сделать: ничего — бот попробует снова в следующем cron-тике. "
        f"Если повторяется — проверь доступность corporate.mattel.com."
    )


# ---------------------------------------------------------------------------
# E021 — Mattel: ошибка парсинга страницы новостей
# ---------------------------------------------------------------------------
def alert_mattel_news_parsing_error(detail: str) -> str:
    return (
        f"[E021] 🔴 Mattel: парсер не нашёл данные на странице\n\n"
        f"Детали: {detail}\n\n"
        f"Что сделать: вероятно, изменилась структура DOM на "
        f"corporate.mattel.com. Нужно обновить парсер в "
        f"mattel_news_source.py (искал якорь \"article2\":{{\"entries\":[\")."
    )


# ---------------------------------------------------------------------------
# E022 — Mattel: общая ошибка фида (raised сообщение)
# ---------------------------------------------------------------------------
def alert_mattel_news_generic(message: str) -> str:
    return (
        f"[E022] 🟡 Mattel: ошибка получения новостей\n\n"
        f"Сообщение: {message}\n\n"
        f"Что сделать: бот пропустит этот источник на этом тике."
    )


# ---------------------------------------------------------------------------
# E023 — Mattel: некорректный URL статьи (allowlist)
# ---------------------------------------------------------------------------
def alert_mattel_article_invalid_link() -> str:
    return (
        f"[E023] 🟡 Mattel: некорректный URL статьи\n\n"
        f"Что произошло: ссылка статьи не прошла allowlist-проверку "
        f"(не начинается с https://corporate.mattel.com/news/).\n\n"
        f"Что сделать: проверь, не подменился ли источник или что-то "
        f"странное в RSS-данных."
    )


# ---------------------------------------------------------------------------
# E024 — Mattel: ошибка fetch'а конкретной статьи
# ---------------------------------------------------------------------------
def alert_mattel_article_fetch_error(link: str, detail: str) -> str:
    return (
        f"[E024] 🟡 Mattel: не удалось получить статью\n\n"
        f"Ссылка: {link}\n"
        f"Детали: {detail}\n\n"
        f"Что сделать: ничего — бот пропустит эту статью и пойдёт дальше."
    )


# ---------------------------------------------------------------------------
# E025 — Lamley: ссылка не прошла allowlist хостов
# ---------------------------------------------------------------------------
def alert_lamley_host_rejected(link: str) -> str:
    return (
        f"[E025] 🟡 Lamley: ссылка отклонена allowlist'ом хостов\n\n"
        f"Ссылка: {link}\n\n"
        f"Что произошло: домен не входит в список разрешённых для "
        f"Lamley-парсера.\n\n"
        f"Что сделать: проверь, не появилась ли новая Lamley-площадка, "
        f"которую нужно добавить в allowlist."
    )


# ---------------------------------------------------------------------------
# E026 — Lamley: статья слишком большая
# ---------------------------------------------------------------------------
def alert_lamley_article_too_large(content_length: int) -> str:
    return (
        f"[E026] 🟡 Lamley: статья превышает лимит размера\n\n"
        f"Размер: {content_length} байт\n\n"
        f"Что сделать: ничего — статья пропущена. Если кейс реальный "
        f"(не parser DOM-эксплойт), можно поднять лимит в lamley_source.py."
    )


# ---------------------------------------------------------------------------
# E027 — Lamley: ошибка скачивания статьи
# ---------------------------------------------------------------------------
def alert_lamley_fetch_error(link: str, error: str) -> str:
    return (
        f"[E027] 🟡 Lamley: ошибка скачивания статьи\n\n"
        f"Ссылка: {link}\n"
        f"Ошибка: {error}\n\n"
        f"Что сделать: ничего — бот пропустит эту статью."
    )


# ---------------------------------------------------------------------------
# E028 — Lamley: в статье нет распознаваемого тела
# ---------------------------------------------------------------------------
def alert_lamley_no_body(link: str) -> str:
    return (
        f"[E028] 🟡 Lamley: не нашёл тело статьи\n\n"
        f"Ссылка: {link}\n\n"
        f"Что произошло: парсер не нашёл блок .entry-content. Возможно, "
        f"изменилась вёрстка lamleygroup.com.\n\n"
        f"Что сделать: проверь страницу руками; если вёрстка новая — "
        f"обновить селектор в lamley_source.py."
    )


# ---------------------------------------------------------------------------
# E031 — T-Hunted: ссылка не прошла allowlist хостов
# ---------------------------------------------------------------------------
def alert_t_hunted_host_rejected(link: str) -> str:
    return (
        f"[E031] 🟡 T-Hunted: ссылка отклонена allowlist'ом хостов\n\n"
        f"Ссылка: {link}\n\n"
        f"Что произошло: хост не входит в список разрешённых для "
        f"t-hunted-парсера (ожидаем t-hunted.blogspot.com).\n\n"
        f"Что сделать: проверь, не сменился ли домен T-Hunted; если нужно — "
        f"добавь новый хост в allowlist t_hunted_source.py."
    )


# ---------------------------------------------------------------------------
# E032 — T-Hunted: ошибка скачивания статьи
# ---------------------------------------------------------------------------
def alert_t_hunted_fetch_error(link: str, error: str) -> str:
    return (
        f"[E032] 🟡 T-Hunted: ошибка скачивания статьи\n\n"
        f"Ссылка: {link}\n"
        f"Ошибка: {error}\n\n"
        f"Что сделать: ничего — бот пропустит эту статью."
    )


# ---------------------------------------------------------------------------
# E033 — T-Hunted: в статье нет распознаваемого тела
# ---------------------------------------------------------------------------
def alert_t_hunted_no_body(link: str) -> str:
    return (
        f"[E033] 🟡 T-Hunted: не нашёл тело статьи\n\n"
        f"Ссылка: {link}\n\n"
        f"Что произошло: парсер не нашёл блок <div class=\"post-body\">. "
        f"Возможно, изменилась вёрстка t-hunted.blogspot.com.\n\n"
        f"Что сделать: проверь страницу руками; если вёрстка новая — "
        f"обновить селектор в t_hunted_source.py."
    )


# ---------------------------------------------------------------------------
# E030 — Orangetrack: сводный алерт по тику (агрегатор)
# Возвращает шапку без префикса инстанса — префикс (([prod]/[test])) добавит
# news_bot.send_admin_notification по INSTANCE_LABEL.
# ---------------------------------------------------------------------------
def alert_orangetrack_summary_header(total_events: int) -> str:
    return f"[E030] 🟡 Orangetrack: {total_events} проблем за тик"


# ---------------------------------------------------------------------------
# Cross-source dedup alerts (E014, E015, E016).
# Гейт fingerprint-сравнения живёт в news_bot.job() между
# _is_text_only_checklist и insert_pending (tech-spec Decision 7).
# Все три builder'а — pure (str-формат); rate-limit и I/O — в news_bot.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pair-key rendering (shared by E014 broad-tier и E015 pair-block).
# ---------------------------------------------------------------------------
def _render_pair(raw: str) -> str:
    """Раскодировать сырой ключ пары `"<model>|<series>|<tier>"` в читаемый
    operator-facing вид: убрать суффикс тира `|D`/`|B`, заменить `|` на ` + `,
    тему-only `*` (нет конкретной модели) отрендерить как серию без модели.

    Примеры:
        'porsche 911|k-pop demon hunters|D' -> 'porsche 911 + k-pop demon hunters'
        '*|stranger things|B'               -> 'stranger things'
    """
    parts = raw.split("|")
    # Сбросить хвостовой тег тира (D/B), если он есть.
    if parts and parts[-1] in ("D", "B"):
        parts = parts[:-1]
    # Тема-only: ведущая '*' означает отсутствие конкретной модели.
    parts = [p for p in parts if p and p != "*"]
    return " + ".join(parts)


def _render_pairs_block(pairs: List[str]) -> str:
    """Отрендерить список сырых ключей пар в читаемый блок (по строке на пару,
    детерминированный порядок через сортировку сырых ключей)."""
    return "\n".join(_render_pair(p) for p in sorted(pairs))


# ---------------------------------------------------------------------------
# E014 — soft-flag: похож на дубль (set-overlap 30-49% ИЛИ broad-пара)
# ---------------------------------------------------------------------------
def alert_cross_source_dupe(
    new_link: str,
    existing_link: str,
    new_source: str,
    existing_source: str,
    overlap_pct: Optional[int] = None,
    n_matches: Optional[int] = None,
    n_total: Optional[int] = None,
    models: Optional[List[str]] = None,
    *,
    pairs: Optional[List[str]] = None,
) -> str:
    # Подстрока 'Похож на дубль' — substring-якорь для интеграционных
    # тестов Wave 2 и rate-limit-логики news_bot. Не менять.
    #
    # Обратная совместимость: set-overlap soft-flag зовёт билдер с модельными
    # параметрами (overlap_pct/n_matches/n_total/models) — рендерим блок
    # моделей. Новый broad-тир парного правила передаёт `pairs` (серия/тема,
    # может быть тема-only без модели) — рендерим блок серии/темы.
    #
    # `pairs=[]` трактуем как отсутствие пар (falsy → legacy-ветка). Легаси-блок
    # моделей рендерим ТОЛЬКО при реальном `overlap_pct`; если его нет — опускаем
    # блок, чтобы никогда не показать оператору `Совпадение моделей: None%`.
    if pairs:
        match_block = "Совпавшая серия/тема:\n" + _render_pairs_block(pairs)
    elif overlap_pct is not None:
        model_list = "\n".join(models or [])
        match_block = (
            f"Совпадение моделей: {overlap_pct}% ({n_matches}/{n_total})\n"
            f"Общие модели:\n{model_list}"
        )
    else:
        match_block = ""
    return (
        f"[E014] 🤔 Похож на дубль\n\n"
        f"Новая статья:\n{new_link}\n\n"
        f"Похож на:\n{existing_link}\n\n"
        f"Источник новой: {new_source}\n"
        f"Источник существующей: {existing_source}\n"
        f"{match_block}\n\n"
        f"Что произошло:\n"
        f"статья прошла в очередь, потому что\n"
        f"порог автоблокировки (50%) не достигнут.\n\n"
        f"Что сделать:\n"
        f"посмотри обе статьи — если это явно\n"
        f"дубль, удали лишнюю через hw_review.py;\n"
        f"если разные — игнорируй пинг."
    )


def build_dedup_review_keyboard(token: str) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура для E014-пинга «Похож на дубль» (фича
    dedup-review-buttons): две кнопки решения оператора.

    ``callback_data`` — грамматика Decision 3: ``dd:c:<token>`` (cancel)
    и ``dd:k:<token>`` (keep). Токен короткий (``secrets.token_urlsafe(9)``,
    ~12 символов, минтится отправителем), поэтому payload заведомо
    укладывается в лимит Telegram 64 байта — URL статьи (PK
    ``pending_articles``) туда бы не влез.

    Порядок кнопок — контракт: cancel ПЕРВОЙ, keep второй. Функция чистая
    (str -> InlineKeyboardMarkup), без I/O — как все билдеры модуля.
    """
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Не публиковать", callback_data=f"dd:c:{token}"),
        InlineKeyboardButton("👍 Оставить", callback_data=f"dd:k:{token}"),
    ]])


# ---------------------------------------------------------------------------
# E015 — hard-block visibility: дубль заблокирован
# (set-overlap ≥50% ИЛИ совпавшая distinctive-пара)
# ---------------------------------------------------------------------------
def alert_cross_source_blocked(
    new_link: str,
    existing_link: str,
    overlap_pct: Optional[int] = None,
    *,
    pairs: Optional[List[str]] = None,
) -> str:
    # Подстрока 'Заблокирован дубль' — substring-якорь для интеграционных
    # тестов Wave 2. Формат сознательно короткий: действие оператора
    # опциональное (статья уже отброшена), блок «Что сделать» отсутствует.
    #
    # Обратная совместимость: `overlap_pct` остаётся 3-м позиционным — set-overlap
    # block-путь зовёт билдер с процентом. Новое парное правило передаёт `pairs`
    # (совпавшие distinctive-пары model+series) — тогда рендерим блок пар вместо
    # строки процента (осмысленного set-overlap % там нет).
    #
    # `pairs=[]` трактуем как отсутствие пар (falsy → legacy-ветка). Легаси-строку
    # процента рендерим ТОЛЬКО при реальном `overlap_pct`; если его нет — опускаем
    # строку, чтобы никогда не показать оператору `Совпадение: None%`.
    if pairs:
        match_block = "Совпавшие пары:\n" + _render_pairs_block(pairs)
    elif overlap_pct is not None:
        match_block = f"Совпадение: {overlap_pct}%"
    else:
        match_block = ""
    return (
        f"[E015] 🚫 Заблокирован дубль\n\n"
        f"Новая (отброшена):\n{new_link}\n\n"
        f"Существующая (канон):\n{existing_link}\n\n"
        f"{match_block}"
    )


# ---------------------------------------------------------------------------
# E016 — AC9 fallback: дедуп в degraded mode (extractor crash и т.п.)
# ---------------------------------------------------------------------------
def alert_dedup_degraded(reason: str) -> str:
    # Подстрока 'Дедуп в degraded mode' — substring-якорь по tech-spec
    # Decision 7. Шаблон в code-research §14.K.3 устарел (имя/эмодзи/
    # заголовок другие) — НЕ копировать оттуда.
    # Rate-limit на 1 час делается в news_bot.job() через bot_state,
    # сам builder rate-limit не знает (pure str).
    return (
        f"[E016] ⚠️ Дедуп в degraded mode\n\n"
        f"Причина: {reason}\n\n"
        f"Что произошло:\n"
        f"экстрактор моделей крашнулся,\n"
        f"статья опубликована как обычно\n"
        f"(fingerprint сохранён как NULL).\n\n"
        f"Что сделать:\n"
        f"посмотри traceback в логах,\n"
        f"починим хотфиксом."
    )
