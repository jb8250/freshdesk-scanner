"""Focused offline tests for the Phase 3A versioned queue-cache envelope."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import app
import queue_live


def _tickets():
    return [{"id": 101, "status": 2, "subject": "photo", "tags": []}]


def _legacy_payload():
    return {"days": 7, "fetched_at": 1787250300.0, "tickets": _tickets()}


def _write_cache(payload):
    with open(app.LIVE_QUEUE_CACHE_FILE, "w") as fh:
        json.dump(payload, fh)


def _digest():
    with open(app.LIVE_QUEUE_CACHE_FILE, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def test_legacy_envelope_loads_unchanged_without_rewrite():
    payload = _legacy_payload()
    _write_cache(payload)
    before = _digest()

    cache = app.load_live_queue_cache()

    assert cache["tickets"] == payload["tickets"]
    assert cache["days"] == 7
    assert cache["fetched_at"] == payload["fetched_at"]
    assert cache["cache_metadata"] == {
        "schema_version": None,
        "last_successful_refresh_started_at": None,
        "last_successful_refresh_finished_at": None,
        "last_refresh_mode": "legacy",
        "last_refresh_requested_days": None,
        "rolling_retention_days": None,
    }
    assert _digest() == before


def test_versioned_envelope_round_trip_persists_contract():
    start = datetime(2026, 8, 21, 0, 12, 34, 987654, tzinfo=timezone.utc)
    finish = start + timedelta(seconds=8, microseconds=99)

    app.save_live_queue_cache(_tickets(), days=60, refresh_started_at=start, refresh_finished_at=finish)

    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        raw = json.load(fh)
    cache = app.load_live_queue_cache()
    assert raw["schema_version"] == app.QUEUE_CACHE_SCHEMA_VERSION == 2
    assert raw["rolling_retention_days"] == app.ROLLING_RETENTION_DAYS == 60
    assert raw["days"] == raw["last_refresh_requested_days"] == 60
    assert raw["last_refresh_mode"] == "baseline"
    assert raw["tickets"] == _tickets()
    assert raw["last_successful_refresh_started_at"] == "2026-08-21T00:12:34Z"
    assert raw["last_successful_refresh_finished_at"] == "2026-08-21T00:12:42Z"
    assert cache["cache_metadata"]["schema_version"] == 2
    assert not os.path.exists(app.LIVE_QUEUE_CACHE_FILE + ".metadata.json")


def test_timestamp_helper_is_utc_whole_seconds_and_trailing_z():
    eastern = timezone(timedelta(hours=-4))
    assert app.queue_cache_timestamp(datetime(2026, 8, 20, 20, 12, 34, 123456, tzinfo=eastern)) == "2026-08-21T00:12:34Z"


def test_malformed_or_unsupported_envelopes_fail_closed_without_rewrite():
    cases = [
        [],
        {"tickets": {}},
        {"schema_version": 3, "tickets": []},
        {"schema_version": 2, "days": 60, "fetched_at": 1, "tickets": []},
        {"schema_version": 2, "days": 60, "fetched_at": 1, "tickets": [],
         "last_successful_refresh_started_at": "2026-08-21T00:00:00Z",
         "last_successful_refresh_finished_at": "2026-08-21T00:00:01Z",
         "last_refresh_mode": "baseline", "last_refresh_requested_days": "60",
         "rolling_retention_days": 60},
    ]
    for payload in cases:
        _write_cache(payload)
        before = _digest()
        assert app.load_live_queue_cache() is None
        assert _digest() == before


def test_queue_renders_both_legacy_and_versioned_live_caches(monkeypatch, client):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    monkeypatch.setattr(app, "load_api_key", lambda: "key")
    _write_cache(_legacy_payload())
    assert client.get("/queue").status_code == 200

    app.save_live_queue_cache(_tickets(), days=365,
                              refresh_started_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
                              refresh_finished_at=datetime(2026, 8, 21, 0, 0, 1, tzinfo=timezone.utc))
    response = client.get("/queue?days=365")
    assert response.status_code == 200
    assert b"365" in response.data


def test_failed_and_cancelled_refresh_leave_cache_and_metadata_unchanged():
    app.save_live_queue_cache(_tickets(), days=7,
                              refresh_started_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
                              refresh_finished_at=datetime(2026, 8, 21, 0, 0, 1, tzinfo=timezone.utc))
    before = _digest()

    failed = queue_live.RefreshJobManager()
    assert failed.start(days=7, api_key="fake", retrieve=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")), save=app.save_live_queue_cache)[0]
    failed.wait()
    assert failed.status()["state"] == queue_live.FAILED
    assert _digest() == before

    cancelled = queue_live.RefreshJobManager()

    def retrieve(**kwargs):
        cancelled.cancel()
        return _tickets()

    assert cancelled.start(days=7, api_key="fake", retrieve=retrieve, save=app.save_live_queue_cache)[0]
    cancelled.wait()
    assert cancelled.status()["state"] == queue_live.CANCELLED
    assert _digest() == before


def test_refresh_metadata_captures_start_before_retrieval_and_finish_after_success():
    observed = {}
    manager = queue_live.RefreshJobManager()

    def retrieve(**kwargs):
        observed["retrieval"] = datetime.now(timezone.utc)
        return _tickets()

    def save(tickets, **kwargs):
        observed.update(kwargs)

    assert manager.start(days=7, api_key="fake", retrieve=retrieve, save=save)[0]
    manager.wait()
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert observed["refresh_started_at"] <= observed["retrieval"] <= observed["refresh_finished_at"]
