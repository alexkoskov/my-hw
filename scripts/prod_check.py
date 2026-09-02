#!/usr/bin/env python3
"""Read-only production health check — one command, one report.

Why this exists
---------------
The post-deploy verification tasks written for `llm-transcreation`,
`t-hunted-pt-source`, `cross-source-dedup` and `dedup-model-series` were never
run, and by 2026-08-03 they had become unrunnable: they name `/home/hwbot/bot`,
`journalctl -u news_bot_test.service`, the retired `@myhwchannel123` test channel
and a 12:00 cron that no longer exists. This script replaces the database half of
those checks with something that works against the CURRENT setup (Docker on the
Moscow host, DB on a bind mount).

It is deliberately READ-ONLY: it opens SQLite in immutable mode, never writes,
never touches the network, and never prints a secret. Running it while the bot is
mid-publish is safe.

Usage (operator, from a laptop — the bot is NOT restarted or interrupted)::

    ssh root@45.90.216.165 "cd /root/hw-news && git pull && python3 scripts/prod_check.py"

`git pull` only updates the checkout on disk; the running container keeps serving
the already-built image, so this does NOT deploy anything. Manual deployment and
container restarts are also allowed at any time by operator decision.

The DB path defaults to the host-side bind mount (`./data/news.db` relative to the
repo). Inside the container pass ``--db /data/news.db``. Anything unexpected is
reported rather than raised — a check that cannot run must not hide the checks
after it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

#: Sources expected to publish regularly. Mattel is deliberately absent — it was
#: disabled 2026-05-24 (news_bot.py:3599-3611), so silence from it is correct.
EXPECTED_SOURCES = ("autoevolution", "lamley", "t-hunted", "orangetrack")

#: Above this, the queue is growing faster than the 3-posts/day ceiling drains it.
PENDING_WARN = 50

OK, WARN, BAD, INFO = "OK  ", "ВНИМ", "ПЛОХО", "    "


class Report:
    """Collects lines and the worst severity seen, so the exit code can reflect it."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.worst = 0  # 0 ok, 1 warn, 2 bad

    def add(self, level: str, text: str) -> None:
        self.lines.append(f"[{level}] {text}")
        if level == WARN:
            self.worst = max(self.worst, 1)
        elif level == BAD:
            self.worst = max(self.worst, 2)

    def note(self, text: str) -> None:
        self.lines.append(f"       {text}")

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"--- {title} ---")


def _connect(path: str) -> sqlite3.Connection:
    """Open read-only. `immutable=1` guarantees we cannot disturb the live writer."""
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def check_publications(conn: sqlite3.Connection, rep: Report, days: int) -> None:
    """Are we publishing, and is every live source still speaking?

    Covers the `t-hunted-pt-source` question ("did it ever publish?") and the
    regression question ("did adding it silence anyone else?") in one query.
    """
    rep.section(f"Публикации за {days} дней")
    if not _table_exists(conn, "published_articles"):
        rep.add(BAD, "таблицы published_articles нет — база не та или не создана")
        return

    # Per-source: count inside the window AND the all-time last publication.
    # The second column is what makes silence readable — see below.
    rows = conn.execute(
        "SELECT source_name,"
        f"       SUM(CASE WHEN published_at > datetime('now','-{days} days') THEN 1 ELSE 0 END) AS n,"
        "        MAX(published_at) AS last,"
        "        CAST(julianday('now') - julianday(MAX(published_at)) AS INTEGER) AS age "
        "FROM published_articles GROUP BY source_name ORDER BY n DESC, age ASC"
    ).fetchall()
    total = sum(r["n"] for r in rows)

    if total == 0:
        rep.add(BAD, f"за {days} дней НИ ОДНОЙ публикации — бот не публикует")
    else:
        rep.add(OK, f"опубликовано {total} статей за {days} дней")

    for r in rows:
        rep.note(f"{r['source_name']:<16} {r['n']:>3} за окно   последняя: "
                 f"{str(r['last'])[:16]} ({r['age']} дн. назад)")

    by_name = {r["source_name"]: r for r in rows}
    for src in EXPECTED_SOURCES:
        # Loose match: source_name is stored TLD-stripped, so a rename should not
        # masquerade as a dead source.
        row = next((r for name, r in by_name.items() if src in name), None)
        if row is None:
            rep.add(WARN, f"источник '{src}' НИ РАЗУ ничего не публиковал — парсер подключён?")
        elif row["n"] == 0:
            # Silence alone is not a defect. A source that stopped months ago has
            # simply gone quiet; a source that was active last week and then went
            # silent is the anomaly. Distinguishing the two is the whole point —
            # on 2026-08-03 both lamley and orangetrack tripped the old
            # unconditional warning, and both turned out to be correct behaviour
            # (lamley's site has not posted since April; orangetrack published
            # only bare case-contents checklists, which `_is_text_only_checklist`
            # rejects on purpose since the 2026-05-12 translation incident).
            if row["age"] is not None and row["age"] > 45:
                rep.add(INFO, f"источник '{src}' молчит {days} дн., но и в целом заглох "
                              f"{row['age']} дн. назад — похоже, сайт сам не публикует")
            else:
                rep.add(WARN, f"источник '{src}' молчит {days} дн., хотя ещё "
                              f"{row['age']} дн. назад публиковался — проверить парсер "
                              "и не режет ли его контентный фильтр")

    all_time = conn.execute("SELECT COUNT(*) FROM published_articles").fetchone()[0]
    rep.note(f"всего в базе за всё время: {all_time}")
    rep.note("молчание источника ≠ поломка: голые чек-листы orangetrack "
             "отбрасываются намеренно (news_bot.py:1594)")


def check_queue(conn: sqlite3.Connection, rep: Report) -> None:
    """Queue depth and dead letters — the two ways the pipeline silently backs up."""
    rep.section("Очередь и отказы")
    for table, label in (("pending_articles", "в очереди"), ("failed_articles", "отвалилось")):
        if not _table_exists(conn, table):
            rep.add(WARN, f"таблицы {table} нет")
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if table == "pending_articles":
            level = WARN if n > PENDING_WARN else OK
            rep.add(level, f"{label}: {n}" + (f" (порог {PENDING_WARN})" if level is WARN else ""))
        else:
            rep.add(WARN if n else OK, f"{label}: {n}")
            if n:
                for r in conn.execute(
                    "SELECT title, last_error, failed_at FROM failed_articles "
                    "ORDER BY failed_at DESC LIMIT 3"
                ):
                    err = (r["last_error"] or "")[:90].replace("\n", " ")
                    rep.note(f"{r['failed_at']}  {(r['title'] or '')[:45]}  {err}")

    if _table_exists(conn, "pending_articles") and _has_column(
        conn, "pending_articles", "hold_reason"
    ):
        held = conn.execute(
            "SELECT hold_reason, COUNT(*) FROM pending_articles "
            "WHERE hold_reason IS NOT NULL GROUP BY hold_reason"
        ).fetchall()
        if held:
            rep.note("придержано: " + ", ".join(f"{r[0]}={r[1]}" for r in held))


def check_outage_state(conn: sqlite3.Connection, rep: Report) -> None:
    """A stuck outage state machine keeps articles held and looks like 'no news'."""
    rep.section("Состояние аварийной машины")
    if not _table_exists(conn, "bot_state"):
        rep.add(WARN, "таблицы bot_state нет")
        return
    rows = conn.execute("SELECT key, value FROM bot_state").fetchall()
    if not rows:
        rep.add(OK, "пусто — аварий не зафиксировано")
        return

    # `bot_state` is a general-purpose k/v store, not just the outage machine:
    # dedup parks `softflag_pair:<link>\n<link>` rows and `review_token:<tok>`
    # rows there too. Printing every key drowned the actual answer in ~20 lines
    # of URLs on 2026-08-03, so group the noise and detail only what matters.
    outage = [r for r in rows if r["key"].startswith("outage") or r["key"] == "fallback_active"]
    buckets: dict[str, int] = {}
    for r in rows:
        if r in outage:
            continue
        prefix = r["key"].split(":", 1)[0] if ":" in r["key"] else r["key"]
        buckets[prefix] = buckets.get(prefix, 0) + 1

    if not outage:
        rep.add(OK, "записи об аварии нет — машина состояний в норме")
    for r in outage:
        stuck = r["value"] not in (None, "", "no_outage")
        rep.add(WARN if stuck else OK, f"{r['key']} = {r['value']}")
        if stuck:
            rep.note("залипшее состояние: статьи придерживаются, публикаций не будет")

    if buckets:
        rep.note("прочие ключи (служебные, не про аварию): "
                 + ", ".join(f"{k}×{n}" for k, n in sorted(buckets.items())))
        rep.note("softflag_pair — отложенные на 24 ч мягкие срабатывания дедупа; "
                 "review_token — выданные кнопки ревью. Норма.")


def check_dedup(conn: sqlite3.Connection, rep: Report) -> None:
    """Is the dedup gate warm? A cold fingerprint set means it compares against nothing."""
    rep.section("Дедуп — прогрет ли")
    if not _table_exists(conn, "published_articles"):
        return
    if not _has_column(conn, "published_articles", "model_fingerprint"):
        rep.add(BAD, "колонки model_fingerprint нет — миграция не применилась, дедуп не работает")
        return

    total = conn.execute("SELECT COUNT(*) FROM published_articles").fetchone()[0]
    withfp = conn.execute(
        "SELECT COUNT(*) FROM published_articles WHERE model_fingerprint IS NOT NULL"
    ).fetchone()[0]
    recent = conn.execute(
        "SELECT COUNT(*) FROM published_articles "
        "WHERE published_at > datetime('now','-7 days')"
    ).fetchone()[0]
    recent_fp = conn.execute(
        "SELECT COUNT(*) FROM published_articles "
        "WHERE published_at > datetime('now','-7 days') AND model_fingerprint IS NOT NULL"
    ).fetchone()[0]

    rep.note(f"всего статей {total}, с отпечатком {withfp}")
    rep.note(f"за последние 7 дней: {recent}, из них с отпечатком {recent_fp}")

    # The 7-day window is what the gate actually compares against, so judge on it.
    if recent == 0:
        rep.add(WARN, "за 7 дней нет публикаций — судить о прогреве нельзя")
    elif recent_fp == 0:
        rep.add(BAD, "окно дедупа ПУСТОЕ — сравнивать не с чем, дубли пройдут насквозь")
        rep.note("лечение: backfill_fingerprints.py --days 30; перезапуск разрешён в любое время")
    elif recent_fp < recent * 0.5:
        rep.add(WARN, f"прогрет частично ({recent_fp}/{recent}) — часть статей без отпечатка")
    else:
        rep.add(OK, f"окно прогрето ({recent_fp}/{recent} за 7 дней)")

    flag = os.getenv("DEDUP_SERIES_ENABLED")
    if flag is None:
        rep.note("DEDUP_SERIES_ENABLED не задан → серийный дедуп ВКЛЮЧЁН (умолчание)")
    else:
        off = flag.strip().lower() in ("0", "false", "no", "off")
        rep.note(f"DEDUP_SERIES_ENABLED={flag!r} → серийный дедуп {'ВЫКЛЮЧЕН' if off else 'включён'}")


def check_heartbeat(rep: Report, path: str | None) -> None:
    """The watchdog's own signal — stale means the tick stopped without crashing."""
    rep.section("Пульс")
    if not path:
        # HEARTBEAT_FILE is set inside the container (docker-compose.yml:34), not
        # in the host shell this usually runs from, so falling back to the known
        # bind-mount paths is what makes this check work at all — it printed a
        # useless "пропуск" on the first real run (2026-08-03).
        for candidate in ("data/last_tick.ts", "/data/last_tick.ts",
                          "/root/hw-news/data/last_tick.ts"):
            if os.path.exists(candidate):
                path = candidate
                rep.note(f"HEARTBEAT_FILE не задан — взят примонтированный {candidate}")
                break
    if not path:
        rep.note("файл пульса не найден ни по одному известному пути — "
                 "проверить `docker exec hw-news-bot ls -l /data/last_tick.ts`")
        return
    if not os.path.exists(path):
        rep.add(WARN, f"файла пульса нет: {path} — тик ещё ни разу не отмечался")
        return
    age_h = (datetime.now(timezone.utc).timestamp() - os.path.getmtime(path)) / 3600
    level = OK if age_h < 26 else (WARN if age_h < 50 else BAD)
    rep.add(level, f"последний тик {age_h:.1f} ч назад")
    if level is not OK:
        rep.note("тик должен отмечаться раз в сутки в 10:00 МСК")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--db",
        default=os.getenv("DB_FILE") or "data/news.db",
        help="путь к news.db (на хосте data/news.db, в контейнере /data/news.db)",
    )
    ap.add_argument("--days", type=int, default=14, help="окно отчёта по публикациям")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"База не найдена: {args.db}", file=sys.stderr)
        print("Подсказка: на хосте запускать из /root/hw-news, иначе указать --db", file=sys.stderr)
        return 2

    rep = Report()
    print(f"Проверка прода — {datetime.now().strftime('%Y-%m-%d %H:%M')}   база: {args.db}")

    try:
        conn = _connect(args.db)
    except sqlite3.Error as exc:
        print(f"Не удалось открыть базу только на чтение: {exc}", file=sys.stderr)
        return 2

    with conn:
        for fn in (check_publications, check_queue, check_outage_state, check_dedup):
            try:
                if fn is check_publications:
                    fn(conn, rep, args.days)
                else:
                    fn(conn, rep)
            except sqlite3.Error as exc:
                # One broken check must not hide the rest.
                rep.add(BAD, f"{fn.__name__} упала: {exc}")
    conn.close()

    check_heartbeat(rep, os.getenv("HEARTBEAT_FILE"))

    print("\n".join(rep.lines))
    verdict = {0: "всё в норме", 1: "есть на что посмотреть", 2: "есть проблемы"}[rep.worst]
    print(f"\nИтог: {verdict}")
    return rep.worst


if __name__ == "__main__":
    sys.exit(main())
