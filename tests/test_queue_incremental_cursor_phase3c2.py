"""Offline Phase 3C2 persistent incremental cursor safety tests."""
import json
from datetime import datetime, timezone

import pytest

import app
import queue_live


START = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)


def ticket(ticket_id, updated_at="2026-08-21T09:00:00Z", **extra):
    value = {"id": ticket_id, "updated_at": updated_at, "status": 2,
             "subject": f"ticket {ticket_id}", "tags": []}
    value.update(extra)
    return value


def cache(cursor="2026-08-21T10:00:00Z", finished="2026-08-21T10:01:00Z", tickets=None):
    return {
        "tickets": [] if tickets is None else tickets,
        "cache_metadata": {
            "schema_version": 2,
            "last_successful_refresh_started_at": cursor,
            "last_successful_refresh_finished_at": finished,
        },
    }


def at(hour, minute=0, second=0):
    return datetime(2026, 8, 21, hour, minute, second, tzinfo=timezone.utc)


def _write_v2(tickets, start):
    app.save_live_queue_cache(tickets, days=7, refresh_started_at=start,
                              refresh_finished_at=start)


def _raw_cache():
    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        return json.load(fh)


def _bytes():
    with open(app.LIVE_QUEUE_CACHE_FILE, "rb") as fh:
        return fh.read()


def _run(old_blob, attempt, retrieve, *, days=7, save=None):
    if save is None:
        def save(tickets, **kwargs):
            kwargs["refresh_finished_at"] = kwargs["refresh_started_at"]
            return app.save_live_queue_cache(tickets, **kwargs)
    manager = queue_live.RefreshJobManager()
    assert manager.start(
        days=days, api_key="fake", retrieve=retrieve, save=save,
        finalize=lambda records, **kwargs: app._reconcile_queue_refresh(old_blob, records, **kwargs),
        plan=lambda requested_days, started: app.queue_refresh_plan(old_blob, requested_days, started),
        attempt_started_at=attempt,
    )[0]
    manager.wait()
    return manager


def test_valid_cursor_uses_exact_two_minute_overlap_and_ignores_days():
    plan = app.queue_refresh_plan(cache(), 7, at(12))
    assert plan["refresh_mode"] == "incremental"
    assert plan["cursor_source"] == "previous_successful_start"
    assert plan["effective_updated_since"] == "2026-08-21T09:58:00Z"
    assert app.queue_refresh_plan(cache(), 45, at(12))["effective_updated_since"] == "2026-08-21T09:58:00Z"


@pytest.mark.parametrize("cursor, mode", [
    ("2026-08-21T09:59:59Z", "incremental"),
    ("2026-08-21T10:00:00Z", "incremental"),
    ("2026-08-21T10:00:01Z", "baseline"),
    ("2026-08-21T10:01:00Z", "baseline"),
    ("2026-08-21T10:02:00Z", "baseline"),
    ("2026-08-21T10:03:00Z", "baseline"),
    ("2026-08-21T10:05:00Z", "baseline"),
])
def test_future_cursor_boundary_policy(cursor, mode):
    plan = app.queue_refresh_plan(cache(cursor, "2026-08-21T10:05:01Z"), 7, START)
    assert plan["refresh_mode"] == mode
    if mode == "incremental":
        assert plan["cursor_source"] == "previous_successful_start"
    else:
        assert plan["cursor_source"] == "days_baseline"
        assert plan["effective_updated_since"] == "2026-08-14T10:00:00Z"
        assert plan["durable_refresh_started_at"] == START


def test_no_cursor_and_invalid_cursor_use_days_baseline():
    assert app.queue_refresh_plan(None, 7, at(12))["effective_updated_since"] == "2026-08-14T12:00:00Z"
    assert app.queue_refresh_plan({"tickets": [], "cache_metadata": {"schema_version": None}}, 45, at(12))["effective_updated_since"] == "2026-07-07T12:00:00Z"
    for bad in (None, "bad", "2026-08-21T10:00:00"):
        assert app.queue_refresh_plan(cache(bad, "2026-08-21T10:01:00Z"), 7, at(12))["refresh_mode"] == "baseline"


def test_future_cursor_production_fallback_passes_days_and_replaces_cursor_after_merge():
    old = [ticket(1, "2020-01-01T00:00:00Z"), ticket(2, "2026-08-21T09:00:00Z")]
    _write_v2(old, at(10, 3))
    old_blob = app.load_live_queue_cache()
    events = []

    def retrieve(**kwargs):
        events.append(kwargs["effective_since"])
        return [ticket(2, "2026-08-21T10:00:00Z", subject="new"), ticket(3)]

    manager = _run(old_blob, START, retrieve)
    saved = _raw_cache()
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert events == ["2026-08-14T10:00:00Z"]
    assert saved["last_successful_refresh_started_at"] == "2026-08-21T10:00:00Z"
    assert [row["id"] for row in saved["tickets"]] == [1, 2, 3]
    assert saved["tickets"][0] == old[0]


def test_attempt_start_precedes_retrieval_and_actual_cursor_horizon_is_passed():
    _write_v2([ticket(1)], at(10))
    old_blob = app.load_live_queue_cache()
    events = []

    def plan(days, started):
        events.append(("plan", started))
        return app.queue_refresh_plan(old_blob, days, started)

    def retrieve(**kwargs):
        events.append(("retrieve", kwargs["effective_since"]))
        return [ticket(1)]

    manager = queue_live.RefreshJobManager()
    assert manager.start(days=45, api_key="fake", retrieve=retrieve,
                         save=app.save_live_queue_cache,
                         finalize=lambda records, **kwargs: app._reconcile_queue_refresh(old_blob, records, **kwargs),
                         plan=plan, attempt_started_at=at(12))[0]
    manager.wait()
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert events == [("plan", at(12)), ("retrieve", "2026-08-21T09:58:00Z")]


def test_multi_refresh_sequence_persists_only_successful_attempt_starts():
    _write_v2([ticket(1)], at(10))

    old_blob = app.load_live_queue_cache()
    seen_b = []
    b = _run(old_blob, at(12), lambda **kwargs: seen_b.append(kwargs["effective_since"]) or [ticket(1)])
    assert b.status()["state"] == queue_live.SUCCEEDED
    assert seen_b == ["2026-08-21T09:58:00Z"]
    assert _raw_cache()["last_successful_refresh_started_at"] == "2026-08-21T12:00:00Z"

    old_blob = app.load_live_queue_cache()
    seen_failed = []
    failed = _run(old_blob, at(13), lambda **kwargs: seen_failed.append(kwargs["effective_since"]) or (_ for _ in ()).throw(RuntimeError("page failed")))
    assert failed.status()["state"] == queue_live.FAILED
    assert seen_failed == ["2026-08-21T11:58:00Z"]
    assert _raw_cache()["last_successful_refresh_started_at"] == "2026-08-21T12:00:00Z"

    old_blob = app.load_live_queue_cache()
    seen_c = []
    c = _run(old_blob, at(14), lambda **kwargs: seen_c.append(kwargs["effective_since"]) or [ticket(1)])
    assert c.status()["state"] == queue_live.SUCCEEDED
    assert seen_c == ["2026-08-21T11:58:00Z"]
    assert _raw_cache()["last_successful_refresh_started_at"] == "2026-08-21T14:00:00Z"


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_real_atomic_writer_failure_preserves_original_envelope(monkeypatch, failure):
    _write_v2([ticket(1)], at(10))
    before = _bytes()
    if failure == "fsync":
        monkeypatch.setattr(app.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync failed")))
    else:
        monkeypatch.setattr(app.os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError):
        app.save_live_queue_cache([ticket(2)], days=7, refresh_started_at=at(12), refresh_finished_at=at(12))
    assert _bytes() == before
    assert _raw_cache()["last_successful_refresh_started_at"] == "2026-08-21T10:00:00Z"


class _Response:
    status_code = 200
    headers = {}

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def test_middle_page_failure_keeps_cache_cursor_and_review_callback_unchanged(monkeypatch):
    _write_v2([ticket(1)], at(10))
    old_blob = app.load_live_queue_cache()
    before = _bytes()
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs["params"]["page"])
        if len(calls) == 1:
            return _Response([ticket(n) for n in range(100, 200)])
        raise RuntimeError("second page failed")

    monkeypatch.setattr(app.requests, "get", fake_get)
    applied = []
    manager = queue_live.RefreshJobManager()
    assert manager.start(
        days=7, api_key="fake", retrieve=app.fetch_live_queue, save=app.save_live_queue_cache,
        finalize=lambda records, **kwargs: (records, lambda: applied.append("advanced")),
        plan=lambda days, started: app.queue_refresh_plan(old_blob, days, started),
        attempt_started_at=at(12),
    )[0]
    manager.wait()
    assert calls == [1, 2]
    assert manager.status()["state"] == queue_live.FAILED
    assert _bytes() == before
    assert applied == []


def test_overlap_effective_change_filtering_avoids_stale_conversation_gets(monkeypatch):
    old = ticket(1, "2026-08-21T09:00:00Z")
    rows = {1: {"review_result": "Resolved", "reviewed_updated_at": "2026-08-21T08:00:00Z"}}
    monkeypatch.setattr(app, "load_review_rows", lambda: rows)
    calls = []
    monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: calls.append(args) or ([], True, 10))

    app._reconcile_queue_refresh({"tickets": [old]}, [dict(old)])
    app._reconcile_queue_refresh({"tickets": [old]}, [ticket(1, "2026-08-21T08:30:00Z")])
    assert calls == []


def test_newer_reviewed_overlap_uses_existing_conversation_analysis(monkeypatch):
    old = ticket(1, "2026-08-21T09:00:00Z")
    newer = ticket(1, "2026-08-21T10:00:00Z")
    rows = {1: {"review_result": "Resolved", "reviewed_updated_at": "2026-08-21T08:00:00Z"}}
    monkeypatch.setattr(app, "load_review_rows", lambda: rows)
    calls = []
    monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: calls.append(args) or ([], False, 10))
    app._reconcile_queue_refresh({"tickets": [old]}, [newer])
    assert len(calls) == 1


def test_timestamp_serialization_is_canonical_whole_second_utc():
    assert app.queue_cache_timestamp(datetime(2026, 8, 21, 10, 0, 0, 999999, tzinfo=timezone.utc)) == "2026-08-21T10:00:00Z"
