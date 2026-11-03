"""Behavioral regressions for publication state, startup and log safety."""

import io
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from freezegun import freeze_time

import news_bot
import pending_articles_repo as repo


@pytest.fixture
def queue(monkeypatch, tmp_path):
    path = tmp_path / "news.db"
    monkeypatch.setattr(news_bot, "DB_FILE", str(path))
    news_bot.init_db()
    entry = {
        "link": "https://example.invalid/hardening",
        "source_name": "lamley",
        "title": "Porsche 911",
        "paragraphs": ["Original article."],
        "images": [],
    }
    assert repo.insert_pending(entry)
    monkeypatch.setattr(news_bot, "send_admin_notification", MagicMock(return_value=True))
    monkeypatch.setattr(news_bot, "_maybe_record_recovery", MagicMock())
    monkeypatch.setattr(news_bot.time, "sleep", MagicMock())
    return entry["link"]


@pytest.mark.parametrize("failure_stage", ["page", "teaser"])
def test_retry_reuses_persisted_translation_after_reload(queue, monkeypatch, failure_stage):
    translation = {"title": "🚀 Первая версия", "subtitle": "", "paragraphs": ["Русский текст."], "blocks": None}
    translate = MagicMock(return_value=translation)
    page = MagicMock(return_value="https://telegra.ph/hardening-11-03")
    teaser = MagicMock(return_value=True)
    if failure_stage == "page":
        page.side_effect = [RuntimeError("temporary page failure"), page.return_value]
    else:
        teaser.side_effect = [False, True]
    monkeypatch.setattr(news_bot, "transcreate_via_claude", translate)
    monkeypatch.setattr(news_bot.telegraph_publisher, "publish_article", page)
    monkeypatch.setattr(news_bot, "send_telegraph_teaser", teaser)
    with pytest.raises(RuntimeError):
        news_bot._fallback_publish(repo.get_pending(queue))
    staged = repo.get_pending(queue)
    assert staged["ru_title"] == translation["title"]
    assert staged["ru_paragraphs"] == translation["paragraphs"]
    assert news_bot._fallback_publish(staged)
    assert translate.call_count == 1
    assert repo.get_published(queue)["ru_title"] == translation["title"]
    if failure_stage == "teaser":
        assert page.call_count == 1


def test_retry_crossing_actual_deadline_is_deferred(monkeypatch):
    row = {"link": "x", "_publish_deadline": news_bot.MSK_TZ.localize(datetime(2026, 11, 3, 20))}
    with freeze_time("2026-11-03T16:30:00Z") as clock:
        def fail_after_deadline(*args, **kwargs):
            clock.move_to("2026-11-03T17:10:00Z")
            raise RuntimeError("temporary send failure")
        publish = MagicMock(side_effect=fail_after_deadline)
        monkeypatch.setattr(news_bot, "_fallback_publish", publish)
        monkeypatch.setattr(news_bot.time, "sleep", MagicMock())
        outcome, error = news_bot._publish_with_retries(row, 1, 1)
    assert outcome == "deferred"
    assert isinstance(error, news_bot.PublishWindowClosed)
    assert publish.call_count == 1
    news_bot.time.sleep.assert_not_called()


def test_llm_finishing_after_deadline_saves_stage_without_page(queue, monkeypatch):
    with freeze_time("2026-11-03T16:59:00Z") as clock:
        def translate(_row):
            clock.move_to("2026-11-03T17:01:00Z")
            return {"title": "🚀 Сохранённый перевод", "paragraphs": ["Текст."], "blocks": None}
        monkeypatch.setattr(news_bot, "transcreate_via_claude", translate)
        page = MagicMock()
        monkeypatch.setattr(news_bot.telegraph_publisher, "publish_article", page)
        row = repo.get_pending(queue)
        row["_publish_deadline"] = news_bot.MSK_TZ.localize(datetime(2026, 11, 3, 20))
        outcome, _ = news_bot._publish_with_retries(row, 1, 1)
    assert outcome == "deferred"
    page.assert_not_called()
    assert repo.get_pending(queue)["ru_title"] == "🚀 Сохранённый перевод"
    assert repo.get_pending(queue)["attempt_count"] == 0


def test_already_sent_teaser_finishes_bookkeeping_after_deadline(queue, monkeypatch):
    repo.update_staged(queue, "🚀 Сохранено", "", ["Текст."], None)
    repo.mark_telegraph_published(queue, "https://telegra.ph/a-11-03", "a-11-03")
    row = repo.get_pending(queue)
    row.update(_teaser_sent=True, _publish_deadline=news_bot.MSK_TZ.localize(datetime(2026, 11, 3, 20)))
    translate = MagicMock(side_effect=AssertionError("must reuse staged translation"))
    teaser = MagicMock(side_effect=AssertionError("must not resend teaser"))
    monkeypatch.setattr(news_bot, "transcreate_via_claude", translate)
    monkeypatch.setattr(news_bot, "send_telegraph_teaser", teaser)
    with freeze_time("2026-11-03T17:10:00Z"):
        assert news_bot._publish_with_retries(row, 1, 1) == ("published", None)
    assert repo.get_published(queue) is not None


def test_formatted_chained_exceptions_are_redacted_for_two_handlers(monkeypatch):
    secret = "opaque-fake-telegraph-credential"
    monkeypatch.setenv("TELEGRAPH_ACCESS_TOKEN", secret)
    streams = [io.StringIO(), io.StringIO()]
    logger = logging.getLogger("hardening.traceback")
    old_handlers, old_propagate, old_level = logger.handlers, logger.propagate, logger.level
    logger.handlers, logger.propagate, logger.level = [], False, logging.ERROR
    try:
        for stream in streams:
            handler = logging.StreamHandler(stream)
            handler.addFilter(news_bot._TokenRedactingFilter())
            handler.setFormatter(news_bot._RedactingFormatter(logging.Formatter("%(levelname)s %(message)s")))
            logger.addHandler(handler)
        try:
            try:
                raise ValueError(secret)
            except ValueError as exc:
                raise RuntimeError("upstream failed " + secret) from exc
        except RuntimeError:
            logger.exception("useful diagnostic", stack_info=True)
        for stream in streams:
            output = stream.getvalue()
            assert secret not in output
            assert "useful diagnostic" in output
            assert "ValueError" in output and "RuntimeError" in output
            assert "Traceback" in output
    finally:
        logger.handlers, logger.propagate, logger.level = old_handlers, old_propagate, old_level


def test_cached_exception_and_stack_are_redacted_without_truncation(monkeypatch):
    secret = "opaque-fake-telegraph-credential"
    monkeypatch.setenv("TELEGRAPH_ACCESS_TOKEN", secret)
    record = logging.LogRecord("probe", logging.ERROR, __file__, 1, "safe", (), None)
    record.exc_text = "cached " + "x" * 700 + secret
    record.stack_info = "stack " + secret
    assert news_bot._TokenRedactingFilter().filter(record)
    formatted = news_bot._RedactingFormatter().format(record)
    assert secret not in formatted
    assert "x" * 700 in formatted
    assert "cached" in formatted and "stack" in formatted


def test_redaction_failure_never_returns_original_secret(monkeypatch):
    class BrokenPattern:
        def sub(self, *args):
            raise RuntimeError("broken regex")
    monkeypatch.setattr(news_bot, "_PROXY_CRED_RE", BrokenPattern())
    assert news_bot._redact_text("potentially sensitive input") == "<redaction failed>"


@pytest.mark.parametrize("kind", ["missing", "zero", "malformed", "wrong_schema", "empty_ledger", "wrong_path"])
def test_production_invalid_ledger_stops_before_any_side_effect(kind, monkeypatch, tmp_path):
    path = tmp_path / "news.db"
    if kind == "zero":
        path.touch()
    elif kind == "malformed":
        path.write_bytes(b"not sqlite")
    elif kind in ("wrong_schema", "empty_ledger"):
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE processed_news (link TEXT, title TEXT, pub_date TEXT)" if kind == "empty_ledger" else "CREATE TABLE other (x)")
    monkeypatch.setattr(news_bot, "INSTANCE_LABEL", "prod")
    monkeypatch.setattr(news_bot, "DB_FILE", str(path))
    monkeypatch.setattr(news_bot, "_PROD_DB_PATH", str(tmp_path / "expected.db") if kind == "wrong_path" else str(path))
    monkeypatch.setenv("TELEGRAPH_ACCESS_TOKEN", "fake-env-token")
    checks = []
    for target, name in [(news_bot, "init_db"), (news_bot, "job"), (news_bot, "_maybe_start_review_listener"), (news_bot.telegraph_publisher, "ensure_access_token"), (news_bot.claude_transcreation, "health_check")]:
        mock = MagicMock()
        monkeypatch.setattr(target, name, mock)
        checks.append(mock)
    with pytest.raises(news_bot.ProductionStartupError):
        news_bot.main()
    assert all(not mock.called for mock in checks)
    if kind in ("missing", "wrong_path"):
        assert not path.exists()


def test_existing_production_ledger_passes_and_requires_env_token(monkeypatch, tmp_path):
    path = tmp_path / "news.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE processed_news (link TEXT, title TEXT, pub_date TEXT)")
        conn.execute("INSERT INTO processed_news VALUES ('existing','existing','2026-11-01')")
    monkeypatch.setattr(news_bot, "INSTANCE_LABEL", "prod")
    monkeypatch.setattr(news_bot, "DB_FILE", str(path))
    monkeypatch.setattr(news_bot, "_PROD_DB_PATH", str(path))
    monkeypatch.setenv("TELEGRAPH_ACCESS_TOKEN", "fake-env-token")
    news_bot._production_preflight()
    monkeypatch.delenv("TELEGRAPH_ACCESS_TOKEN")
    with pytest.raises(news_bot.ProductionStartupError, match="TELEGRAPH_ACCESS_TOKEN"):
        news_bot._production_preflight()


@pytest.mark.parametrize("title,model", [("Chevy Camaro", "chevrolet camaro"), ("VW Golf GTI", "volkswagen golf gti"), ("Porsche 911 K-Pop Demon Hunters", "porsche 911")])
def test_title_models_accept_aliases_and_existing_canonical_positives(title, model):
    assert news_bot._title_mentions_model(title, model)


def test_title_models_do_not_match_body_only_or_different_model():
    assert not news_bot._title_mentions_model("Chevy Corvette", "chevrolet camaro")
    assert not news_bot._title_mentions_model("New collection announced", "chevrolet camaro")


@pytest.mark.parametrize("action", ["keep", "cancel"])
def test_e014_immediate_callback_during_send_acts_on_committed_row(queue, monkeypatch, action):
    incoming = "https://example.invalid/new-article"
    entry = {"link": incoming, "title": "Hot Wheels Porsche 911 new release", "source_name": "lamley"}
    article = {"title": entry["title"], "paragraphs": ["Hot Wheels introduces a new Porsche 911 casting with detailed photos."], "images": ["https://example.invalid/photo.jpg"]}
    match = {"link": queue, "source_name": "autoevolution", "overlap_pct": 40, "n_matches": 2, "n_total": 5, "models": ["porsche 911"]}
    monkeypatch.setattr(news_bot, "SOURCES", [lambda notifier=None: [entry]])
    monkeypatch.setattr(news_bot, "fetch_full_article", lambda _entry: article)
    monkeypatch.setattr(news_bot, "_check_cross_source_dedup", lambda *args: ("flag", match, []))
    monkeypatch.setattr(news_bot, "_review_listener_enabled", lambda: True)
    monkeypatch.setattr(news_bot, "TELEGRAM_ADMIN_ID", "12345")
    monkeypatch.setattr(news_bot, "compute_fixed_slots", lambda n, now: ([], n))
    monkeypatch.setattr(news_bot, "remaining_fixed_slots", lambda *args, **kwargs: [])
    monkeypatch.setattr(news_bot, "_maybe_alert_openrouter_balance", lambda: None)
    outcomes = []
    def sender(message, **kwargs):
        if "[E014]" in message:
            assert repo.get_pending(incoming) is not None
            keyboard = kwargs["reply_markup"]
            token = keyboard.inline_keyboard[0][0].callback_data.split(":", 2)[2]
            outcomes.append(news_bot.resolve_dedup_callback(action, token, 12345))
            assert news_bot.resolve_dedup_callback(action, token, 12345)[0] == "⚠️ Кнопка устарела"
        return True
    monkeypatch.setattr(news_bot, "send_admin_notification", sender)
    news_bot.job()
    assert len(outcomes) == 1
    if action == "cancel":
        assert repo.get_pending(incoming) is None
        assert news_bot.is_processed(incoming)
    else:
        assert repo.get_pending(incoming)["publish_after"] is None


@pytest.mark.parametrize("failure", ["false", "exception"])
def test_failed_e014_send_does_not_acknowledge_rate_limit(queue, monkeypatch, failure):
    match = {"link": "https://other.invalid/candidate", "source_name": "autoevolution", "overlap_pct": 40, "n_matches": 2, "n_total": 5, "models": ["porsche 911"]}
    sender = MagicMock(return_value=False)
    if failure == "exception":
        sender.side_effect = RuntimeError("synthetic transport failure")
    monkeypatch.setattr(news_bot, "send_admin_notification", sender)
    monkeypatch.setattr(news_bot, "_review_listener_enabled", lambda: True)
    assert not news_bot._send_staged_dedup_alert(queue, "lamley", match, "overlap")
    conn = repo._connect()
    try:
        assert not repo.is_pair_rate_limited(conn, queue, match["link"])
        assert conn.execute("SELECT COUNT(*) FROM bot_state WHERE key LIKE 'review_token:%'").fetchone()[0] == 0
    finally:
        conn.close()


def test_failed_staging_does_not_issue_e014(queue, monkeypatch):
    incoming = "https://example.invalid/failed-staging"
    entry = {"link": incoming, "title": "Hot Wheels Porsche 911 new release", "source_name": "lamley"}
    match = {"link": queue, "source_name": "autoevolution", "overlap_pct": 40, "n_matches": 2, "n_total": 5, "models": ["porsche 911"]}
    monkeypatch.setattr(news_bot, "SOURCES", [lambda notifier=None: [entry]])
    monkeypatch.setattr(news_bot, "fetch_full_article", lambda _entry: {"title": entry["title"], "paragraphs": ["Hot Wheels introduces a new Porsche 911 with detailed photos."], "images": ["https://example.invalid/photo.jpg"]})
    monkeypatch.setattr(news_bot, "_check_cross_source_dedup", lambda *args: ("flag", match, []))
    monkeypatch.setattr(news_bot, "compute_fixed_slots", lambda n, now: ([], n))
    monkeypatch.setattr(news_bot, "remaining_fixed_slots", lambda *args, **kwargs: [])
    monkeypatch.setattr(news_bot, "_maybe_alert_openrouter_balance", lambda: None)
    monkeypatch.setattr(repo, "insert_pending", MagicMock(side_effect=sqlite3.OperationalError("synthetic insert failure")))
    notify = MagicMock()
    monkeypatch.setattr(news_bot, "_send_staged_dedup_alert", notify)
    news_bot.job()
    notify.assert_not_called()


def test_production_unwritable_ledger_stops_startup(monkeypatch, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("permission failure requires an unprivileged test process")
    path = tmp_path / "news.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE processed_news (link TEXT, title TEXT, pub_date TEXT)")
        conn.execute("INSERT INTO processed_news VALUES ('existing','existing','2026-11-01')")
    monkeypatch.setattr(news_bot, "INSTANCE_LABEL", "prod")
    monkeypatch.setattr(news_bot, "DB_FILE", str(path))
    monkeypatch.setattr(news_bot, "_PROD_DB_PATH", str(path))
    path.chmod(0o400)
    try:
        with pytest.raises(news_bot.ProductionStartupError, match="writable"):
            news_bot._production_preflight()
    finally:
        path.chmod(0o600)


def test_job_deadline_is_actual_moscow_20_and_deferral_never_strikes(queue, monkeypatch):
    monkeypatch.setattr(news_bot, "SOURCES", [])
    monkeypatch.setattr(news_bot, "_maybe_alert_openrouter_balance", lambda: None)
    monkeypatch.setattr(news_bot, "compute_fixed_slots", lambda n, now: ([now], 0))
    publish = MagicMock(return_value=("deferred", news_bot.PublishWindowClosed("closed")))
    monkeypatch.setattr(news_bot, "_publish_with_retries", publish)
    attempt, hold = MagicMock(), MagicMock()
    monkeypatch.setattr(repo, "increment_attempt", attempt)
    monkeypatch.setattr(repo, "increment_hold", hold)
    with freeze_time("2026-11-03T16:30:00Z"):
        news_bot.job()
    deadline = publish.call_args.args[0]["_publish_deadline"]
    assert deadline.hour == 20 and deadline.minute == 0
    assert deadline.utcoffset() == timedelta(hours=3)
    attempt.assert_not_called()
    hold.assert_not_called()
    assert repo.get_pending(queue) is not None


def test_lock_override_serializes_processes_without_truncating_holder_pid(tmp_path):
    lock = tmp_path / "shared.lock"
    env = dict(os.environ, NEWS_BOT_LOCK_FILE=str(lock))
    holder_code = "import sys,news_bot; news_bot._acquire_singleton_lock(); print('locked',flush=True); sys.stdin.read(1)"
    holder = subprocess.Popen([sys.executable, "-c", holder_code], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        pid = lock.read_text()
        contender = subprocess.run([sys.executable, "-c", "import news_bot; news_bot._acquire_singleton_lock()"], env=env, capture_output=True, text=True, timeout=15)
        assert contender.returncode == 1
        assert lock.read_text() == pid
    finally:
        holder.communicate("\n", timeout=15)
