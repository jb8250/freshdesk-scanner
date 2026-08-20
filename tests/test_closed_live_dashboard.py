"""Prompt 24: closed live-cache and refresh integration (fully mocked/offline)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import app
import closed_live

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def raw_ticket(ticket_id=1, **extra):
    row = {
        "id": ticket_id, "subject": "Safe", "status": 5, "tags": [],
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-08-02T02:00:00Z",
        "stats": {"closed_at": "2026-08-02T01:00:00Z", "private": "drop"},
        "requester": {"email": "drop@example.test"}, "description": "drop",
        "custom_fields": {"secret": "drop"}, "attachments": ["drop"],
    }
    row.update(extra)
    return row


def result(*, success=True, complete=True, stop_reason="natural_exhaustion"):
    values = dict(
        tickets=[raw_ticket()], matches=[raw_ticket()], success=success, complete=complete,
        stop_reason=stop_reason, pages_completed=2, http_requests_made=2,
        rows_received=1, unique_ticket_count=1, duplicate_count=0,
        rate_limit_remaining_last=198, status_5_count=1,
        empty_tags_count=1, closed_no_tags_count=1,
        valid_closed_at_count=1, invalid_or_missing_closed_at_count=0,
        closed_no_tags_in_date_window_count=1, next_page_existed_at_cap=False,
        retries=0, http_429_count=0, rate_limit_units_used=2,
    )
    values["to_dict"] = lambda: dict(values)
    return SimpleNamespace(**values)


def test_closed_cache_is_separate_and_gitignored():
    assert "closed_tickets.json" in Path("closed_live.py").read_text()
    # The queue's LIVE cache is a distinct file from the closed cache; the two
    # namespaces must never cross-satisfy a read.
    src = Path("app.py").read_text()
    assert 'LIVE_QUEUE_CACHE_FILE' in src
    assert "queue_live_tickets.json" in src
    assert app.LIVE_QUEUE_CACHE_FILE != closed_live.CLOSED_CACHE_FILE
    assert "CLOSED_CACHE_FILE" in Path("closed_live.py").read_text()
    assert "cache/" in Path(".gitignore").read_text()


def test_cache_allowlist_and_safe_metadata(tmp_path):
    payload = closed_live.build_cache_payload(
        [closed_live.sanitize_ticket(raw_ticket())], days=8,
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 9, tzinfo=timezone.utc),
        summary={"pages_completed": 2, "rows_received": 1,
                 "unique_ticket_count": 1, "duplicate_count": 0}, fetched_at=NOW)
    path = tmp_path / "closed.json"
    closed_live.write_cache_atomic(payload, str(path))
    text = path.read_text()
    assert "drop@example.test" not in text and "description" not in text
    assert "custom_fields" not in text and "attachments" not in text
    assert set(payload["tickets"][0]) == {"id", "subject", "status", "tags", "created_at", "updated_at", "stats"}
    assert set(payload["tickets"][0]["stats"]) == {"closed_at"}
    assert payload["schema_version"] == 1 and payload["complete"] is True


def test_malformed_cache_fails_closed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert closed_live.load_cache(str(path)) is None


def test_success_replaces_cache_atomically_and_uses_five_second_cushion(tmp_path):
    path = tmp_path / "closed.json"
    path.write_text('{"old":true}')
    seen = {}
    def retrieve(config, **kwargs):
        seen["config"] = config
        return result()
    manager = closed_live.RefreshJobManager()
    started, _ = manager.start(days=8, api_key="[REDACTED]", now=NOW,
                               cache_file=str(path), retrieve=retrieve,
                               config_factory=lambda **kw: SimpleNamespace(**kw), join=True)
    assert started and manager.status()["state"] == closed_live.SUCCESS
    payload = json.loads(path.read_text())
    assert payload["tickets"][0]["id"] == 1
    assert seen["config"].updated_since == "2026-07-31T23:59:55Z"
    assert not list(tmp_path.glob(".closed-cache-*.tmp"))


def test_failed_cancelled_and_incomplete_results_keep_last_known_good(tmp_path):
    for outcome in [result(success=False, complete=False, stop_reason="network_error"),
                    result(success=True, complete=False, stop_reason="page_cap"),
                    result(success=False, complete=False, stop_reason="cancelled")]:
        path = tmp_path / (outcome.stop_reason + ".json")
        path.write_text('{"good":"unchanged"}')
        manager = closed_live.RefreshJobManager()
        manager.start(days=8, api_key="[REDACTED]", now=NOW, cache_file=str(path),
                      retrieve=lambda config, value=outcome, **kw: value,
                      config_factory=lambda **kw: SimpleNamespace(**kw), join=True)
        assert path.read_text() == '{"good":"unchanged"}'
        assert manager.status()["state"] in {closed_live.FAILED, closed_live.CANCELLED}


def test_one_job_at_a_time_progress_and_cancel_token(tmp_path):
    observed = {}
    def retrieve(config):
        config.progress_callback({"page": 3, "pages_completed": 2, "rows_received": 200,
                           "unique_tickets": 199, "rate_limit_remaining": 150})
        observed["cancel"] = config.cancel_callback
        return result(success=False, complete=False, stop_reason="cancelled")
    manager = closed_live.RefreshJobManager()
    blocker = SimpleNamespace(is_alive=lambda: True)
    manager._thread = blocker
    assert manager.start(days=8, api_key="x", now=NOW)[0] is False
    manager._thread = None
    manager.reset()
    manager.start(days=8, api_key="x", now=NOW, cache_file=str(tmp_path/"x"),
                  retrieve=retrieve, config_factory=lambda **kw: SimpleNamespace(**kw), join=True)
    status = manager.status()
    assert status["progress"]["unique_tickets"] == 199
    assert callable(observed["cancel"])
    manager._state["state"] = closed_live.RUNNING
    assert manager.cancel() is True and observed["cancel"]() is True


def csrf_client():
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = "token"
    return client


def test_refresh_endpoints_require_csrf_and_offline_rejects_before_key(monkeypatch):
    client = csrf_client()
    assert client.post("/closed/api/refresh").status_code == 403
    assert client.post("/closed/api/refresh/cancel").status_code == 403
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.setattr(app, "load_api_key", lambda: (_ for _ in ()).throw(AssertionError("key read")))
    response = client.post("/closed/api/refresh", data={"csrf_token": "token", "days": "8"})
    assert response.status_code == 409


def test_status_is_payload_and_credential_safe(monkeypatch):
    manager = closed_live.RefreshJobManager()
    manager._state.update({"state": closed_live.RUNNING, "message": "working", "progress": {"page": 1}})
    monkeypatch.setattr(closed_live, "JOB", manager)
    data = app.app.test_client().get("/closed/api/refresh/status").get_json()
    text = json.dumps(data)
    assert "tickets" not in data and "[REDACTED]" not in text
    assert "Authorization" not in text and "requester" not in text


def test_live_page_exact_half_open_boundaries_and_queue_cache_untouched(monkeypatch, tmp_path):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    queue_path = tmp_path / "tickets.json"
    queue_path.write_text("queue-good")
    monkeypatch.setattr(app, "CACHE_FILE", str(queue_path))
    start, end = closed_live.utc_window(8, NOW)
    rows = [
        raw_ticket(1, stats={"closed_at": start.isoformat()}),
        raw_ticket(2, stats={"closed_at": (end.isoformat())}),
    ]
    payload = closed_live.build_cache_payload([closed_live.sanitize_ticket(x) for x in rows],
        days=8, start=start, end=end, summary={"pages_completed": 1}, fetched_at=NOW)
    closed_live.write_cache_atomic(payload)
    monkeypatch.setattr(app, "now_utc", lambda: NOW)
    page = app.app.test_client().get("/closed?days=8&missing_tags=0&review_view=all")
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and 'data-ticket-id="1"' in text and 'data-ticket-id="2"' not in text
    assert queue_path.read_text() == "queue-good"


def test_live_cached_ticket_is_known_to_local_review(monkeypatch):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    start, end = closed_live.utc_window(8, NOW)
    payload = closed_live.build_cache_payload(
        [closed_live.sanitize_ticket(raw_ticket(98765))],
        days=8, start=start, end=end, summary={}, fetched_at=NOW)
    closed_live.write_cache_atomic(payload)
    assert app.closed_ticket_known(98765) is True
    assert app.closed_ticket_known(12345) is False


def test_page_reload_never_reads_key_or_calls_retriever(monkeypatch):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    monkeypatch.setattr(app, "load_api_key", lambda: (_ for _ in ()).throw(AssertionError("key read")))
    monkeypatch.setattr(closed_live.JOB, "start", lambda **kw: (_ for _ in ()).throw(AssertionError("refresh")))
    client = app.app.test_client()
    assert client.get("/closed").status_code == 200
    assert client.get("/closed").status_code == 200


def test_closed_table_and_local_review_controls_preserved(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    text = app.app.test_client().get("/closed?review_view=all").get_data(as_text=True)
    for heading in ["Ticket", "Subject", "Status", "Badges", "Review", "Closed", "Updated", "Created", "Tags"]:
        assert f">{heading}<" in text
    assert "LAST OPENED" in text and "Jump" in text
    assert "Not Applicable to Me" in text and "Needs Follow-Up" in text
    assert "Refresh from Freshdesk" in text and "disabled" in text
