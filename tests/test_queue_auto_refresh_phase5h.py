"""Phase 5H-B automatic queue-refresh scheduler coverage."""
import time

import app
from auto_refresh import AUTO_REFRESH_INTERVAL_SECONDS, AutoRefreshScheduler


def wait_for(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_scheduler_waits_one_interval_and_is_daemon():
    calls = []
    scheduler = AutoRefreshScheduler(lambda: calls.append("started") or "started", interval_seconds=0.04)
    assert scheduler.start() is True
    assert scheduler._thread.daemon is True
    assert calls == []
    assert wait_for(lambda: calls == ["started"])


def test_default_interval_is_thirty_minutes():
    assert AUTO_REFRESH_INTERVAL_SECONDS == 1800
    assert AutoRefreshScheduler(lambda: None).interval_seconds == 1800


def test_reset_postpones_and_multiple_starts_do_not_duplicate_threads():
    calls = []
    scheduler = AutoRefreshScheduler(lambda: calls.append(time.monotonic()) or "started", interval_seconds=0.08)
    assert scheduler.start() is True
    thread = scheduler._thread
    assert scheduler.start() is False
    time.sleep(0.04)
    scheduler.reset()
    scheduler.reset()
    time.sleep(0.05)
    assert calls == []
    assert scheduler._thread is thread
    assert wait_for(lambda: len(calls) == 1)


def test_status_never_reports_negative_countdown():
    scheduler = AutoRefreshScheduler(lambda: "cancelled", interval_seconds=0.02)
    assert scheduler.start()
    assert scheduler.status()["enabled"] is True
    assert scheduler.status()["seconds_until_next"] >= 0
    assert wait_for(lambda: scheduler.status()["last_result"] == "cancelled")
    assert scheduler.status()["seconds_until_next"] >= 0


def test_automatic_callback_skips_busy_job(monkeypatch):
    monkeypatch.setattr(app, "is_offline", lambda: False)
    monkeypatch.setattr(app.queue_live.JOB, "status", lambda: {"running": True})
    assert app._automatic_queue_refresh() == "skipped_busy"


def test_automatic_callback_uses_shared_normal_starter(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "is_offline", lambda: False)
    monkeypatch.setattr(app.queue_live.JOB, "status", lambda: {"running": False})
    monkeypatch.setattr(app, "_start_normal_queue_refresh", lambda: calls.append(True) or (True, "Refresh started."))
    assert app._automatic_queue_refresh() == "started"
    assert calls == [True]


def test_offline_scheduler_does_not_start(monkeypatch):
    monkeypatch.setattr(app, "is_offline", lambda: True)
    monkeypatch.setattr(app, "_auto_refresh_scheduler", None)
    assert app.start_auto_refresh_scheduler() is False
    assert app.initialize_live_auto_refresh() is False
    assert app.auto_refresh_status()["enabled"] is False


def test_live_startup_hook_starts_once_without_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "is_offline", lambda: False)
    monkeypatch.setattr(app, "_auto_refresh_scheduler", None)
    monkeypatch.setattr(app, "_automatic_queue_refresh", lambda: calls.append(True) or "started")
    assert app.initialize_live_auto_refresh() is True
    assert app.initialize_live_auto_refresh() is False
    status = app.auto_refresh_status()
    assert status["enabled"] is True
    assert status["interval_seconds"] == 1800
    assert status["seconds_until_next"] > 0
    assert calls == []


def test_live_launcher_executes_app_as_main():
    from pathlib import Path
    launcher = Path(app.BASE_DIR, "Freshdesk_Scanner_LIVE_Start.command").read_text()
    downloads_launcher = Path(app.BASE_DIR).parent.parent / "Downloads" / "Freshdesk_Scanner_LIVE_Start.command"
    expected = 'HOST=127.0.0.1 PORT="$PORT" exec "$PROJECT/.venv/bin/python" "$PROJECT/app.py"'
    for path in (Path(app.BASE_DIR, "Freshdesk_Scanner_LIVE_Start.command"), downloads_launcher):
        contents = path.read_text()
        assert expected in contents
        assert 'flask" --app app run' not in contents


def test_accepted_manual_and_reconcile_reset_countdown(client, monkeypatch):
    class Scheduler:
        def __init__(self): self.resets = 0
        def reset(self): self.resets += 1
        def status(self):
            return {"enabled": True, "interval_seconds": 1800, "seconds_until_next": 1800,
                    "last_attempt_at": None, "last_result": None}
    scheduler = Scheduler()
    monkeypatch.setattr(app, "is_offline", lambda: False)
    monkeypatch.setattr(app, "_auto_refresh_scheduler", scheduler)
    monkeypatch.setattr(app, "load_api_key", lambda: "test-key")
    monkeypatch.setattr(app, "load_live_queue_cache", lambda: {"tickets": []})
    monkeypatch.setattr(app.queue_live.JOB, "start", lambda **kwargs: (True, "started"))
    assert client.get("/queue").status_code == 200
    with client.session_transaction() as session:
        csrf = session["csrf_token"]
    assert client.post("/queue/api/refresh", data={"csrf_token": csrf, "mode": "normal"}).status_code == 202
    assert client.post("/queue/api/refresh", data={"csrf_token": csrf, "mode": "reconcile", "days": "60"}).status_code == 202
    assert scheduler.resets == 2


def test_queue_page_uses_successful_cache_timestamp_for_last_refreshed(client, monkeypatch):
    monkeypatch.setattr(app, "is_offline", lambda: False)
    app.save_live_queue_cache([], refresh_started_at=app.now_utc(), refresh_finished_at=app.now_utc(),
                              refresh_mode="incremental")
    html = client.get("/queue").get_data(as_text=True)
    assert "Last refreshed Never" not in html
    assert "Last refreshed Just now" in html


def test_queue_page_without_successful_cache_shows_never(client, monkeypatch):
    monkeypatch.setattr(app, "is_offline", lambda: False)
    assert "Last refreshed Never" in client.get("/queue").get_data(as_text=True)


def test_status_endpoint_preserves_job_status_without_key_or_network_reads(client, monkeypatch):
    monkeypatch.setattr(app, "load_api_key", lambda: (_ for _ in ()).throw(AssertionError("key read")))
    response = client.get("/queue/api/refresh/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert "state" in payload and "running" in payload
    assert payload["auto_refresh"]["enabled"] is False
