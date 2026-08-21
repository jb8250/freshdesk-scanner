"""Offline Phase 3D2 integration tests for refresh-path retention activation."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import app
import queue_live


START = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
CUTOFF = "2026-06-22T10:00:00Z"
EXPIRED = "2026-06-22T09:59:59Z"
RECENT = "2026-08-20T10:00:00Z"
ACTIVE = ["Opened / In Review", "Needs Follow-Up", "Needs Supervisor Review"]


def ticket(ticket_id, updated_at=RECENT, *, status=2, **extra):
    row = {"id": ticket_id, "updated_at": updated_at, "status": status,
           "subject": f"ticket {ticket_id}", "tags": []}
    row.update(extra)
    return row


def write_cache(tickets, cursor="2026-08-20T10:00:00Z"):
    app.save_live_queue_cache(tickets, days=7,
                              refresh_started_at=datetime.fromisoformat(cursor.replace("Z", "+00:00")),
                              refresh_finished_at=START)


def cache_rows():
    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        return json.load(fh)["tickets"]


def cache_bytes():
    with open(app.LIVE_QUEUE_CACHE_FILE, "rb") as fh:
        return fh.read()


def run(old_tickets, incoming, *, attempt=START, save=None):
    write_cache(old_tickets)
    old_blob = app.load_live_queue_cache()
    manager = queue_live.RefreshJobManager()
    if save is None:
        save = app.save_live_queue_cache
    assert manager.start(
        days=7, api_key="fake", retrieve=lambda **kwargs: incoming, save=save,
        finalize=lambda records, **kwargs: app._reconcile_queue_refresh(old_blob, records, **kwargs),
        plan=lambda days, started: app.queue_refresh_plan(old_blob, days, started),
        attempt_started_at=attempt,
    )[0]
    manager.wait()
    return manager


@pytest.mark.parametrize("state", ACTIVE)
def test_expired_active_cached_ticket_absent_from_incoming_survives_merge_and_retention(state):
    app.set_review_result(123, state, reviewed_updated_at="2026-01-01T00:00:00Z")
    manager = run([ticket(123, EXPIRED)], [ticket(999)])
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert {row["id"] for row in cache_rows()} == {123, 999}


@pytest.mark.parametrize("state", ["Resolved", "No Action Needed", "Not Applicable to Me", "Unreviewed"])
def test_expired_non_active_cached_ticket_is_pruned_without_deleting_review_row(state):
    app.set_review_result(456, state, reviewed_updated_at="2026-01-01T00:00:00Z")
    before = app.load_review_rows()[456].copy()
    manager = run([ticket(456, EXPIRED)], [ticket(999)])
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert {row["id"] for row in cache_rows()} == {999}
    assert app.load_review_rows()[456] == before


def test_expired_no_review_row_prunes_and_cutoff_is_inclusive():
    manager = run([ticket(1, EXPIRED), ticket(2, CUTOFF)], [ticket(3)])
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert {row["id"] for row in cache_rows()} == {2, 3}


@pytest.mark.parametrize("updated_at,status,state,expected", [
    (RECENT, 5, None, True),
    (EXPIRED, 5, None, False),
    (EXPIRED, 5, "Needs Follow-Up", True),
])
def test_closed_follows_normal_retention_policy(updated_at, status, state, expected):
    if state:
        app.set_review_result(50, state, reviewed_updated_at="2026-01-01T00:00:00Z")
    manager = run([ticket(50, updated_at, status=status)], [ticket(999)])
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert (50 in {row["id"] for row in cache_rows()}) is expected


def test_retention_reference_is_exact_attempt_start_and_metrics_are_exposed(monkeypatch):
    observed = {}
    real = app.apply_queue_retention

    def wrapped(tickets, states, **kwargs):
        observed["ids"] = [row["id"] for row in tickets]
        observed["reference"] = kwargs["reference_time"]
        return real(tickets, states, **kwargs)

    monkeypatch.setattr(app, "apply_queue_retention", wrapped)
    manager = run([ticket(10, EXPIRED), ticket(11, RECENT)], [ticket(12)])
    status = manager.status()
    assert observed == {"ids": [10, 11, 12], "reference": START}
    assert status["merge_metrics"]["merged_count"] == 3
    assert status["retention_metrics"] == {
        "input_count": 3, "retained_count": 2, "pruned_count": 1,
        "retained_within_window_count": 2, "retained_active_exception_count": 0,
        "pruned_expired_count": 1, "closed_within_window_retained_count": 0,
        "active_beyond_window_retained_count": 0,
    }


def test_retention_validation_failure_preserves_cache_cursor_and_review_state(monkeypatch):
    app.set_review_result(1, "Resolved", reviewed_updated_at="2026-01-01T00:00:00Z")
    write_cache([ticket(1, RECENT)])
    before_cache = cache_bytes()
    before_row = app.load_review_rows()[1].copy()
    old_blob = app.load_live_queue_cache()
    manager = queue_live.RefreshJobManager()
    real_load_review_rows = app.load_review_rows
    monkeypatch.setattr(app, "load_review_rows", lambda: {1: {"review_result": "Unknown"}})
    assert manager.start(
        days=7, api_key="fake", retrieve=lambda **kwargs: [ticket(2)], save=app.save_live_queue_cache,
        finalize=lambda records, **kwargs: app._reconcile_queue_refresh(old_blob, records, **kwargs),
        plan=lambda days, started: app.queue_refresh_plan(old_blob, days, started),
        attempt_started_at=START,
    )[0]
    manager.wait()
    assert manager.status()["state"] == queue_live.FAILED
    assert cache_bytes() == before_cache
    assert real_load_review_rows()[1] == before_row


def test_pruned_ticket_reattaches_to_existing_review_state_on_later_refresh():
    app.set_review_result(456, "Resolved", reviewed_updated_at="2026-01-01T00:00:00Z")
    first = run([ticket(456, EXPIRED)], [ticket(999)])
    assert first.status()["state"] == queue_live.SUCCEEDED
    assert 456 not in {row["id"] for row in cache_rows()}
    old_blob = app.load_live_queue_cache()
    manager = queue_live.RefreshJobManager()
    assert manager.start(
        days=7, api_key="fake", retrieve=lambda **kwargs: [ticket(456, RECENT)], save=app.save_live_queue_cache,
        finalize=lambda records, **kwargs: app._reconcile_queue_refresh(old_blob, records, **kwargs),
        plan=lambda days, started: app.queue_refresh_plan(old_blob, days, started),
        attempt_started_at=START,
    )[0]
    manager.wait()
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert 456 in {row["id"] for row in cache_rows()}
    assert app.load_review_rows()[456]["review_result"] == "Resolved"


def test_large_merged_cache_is_retained_not_replaced_by_small_incoming():
    recent = [ticket(i, RECENT) for i in range(1, 401)]
    active = [ticket(i, EXPIRED) for i in range(401, 601)]
    expired = [ticket(i, EXPIRED) for i in range(601, 901)]
    for row in active:
        app.set_review_result(row["id"], "Needs Supervisor Review", reviewed_updated_at="2026-01-01T00:00:00Z")
    manager = run(recent + active + expired, [ticket(1, RECENT, subject="changed"), ticket(901)])
    assert manager.status()["state"] == queue_live.SUCCEEDED
    rows = cache_rows()
    assert len(rows) == 601
    assert {row["id"] for row in rows} == set(range(1, 601)) | {901}


def test_save_failure_after_retention_preserves_old_cache(monkeypatch):
    write_cache([ticket(1, RECENT)])
    before = cache_bytes()
    old_blob = app.load_live_queue_cache()
    manager = queue_live.RefreshJobManager()
    observed = []
    real = app.apply_queue_retention
    monkeypatch.setattr(app, "apply_queue_retention", lambda *args, **kwargs: observed.append(True) or real(*args, **kwargs))
    assert manager.start(
        days=7, api_key="fake", retrieve=lambda **kwargs: [ticket(2)],
        save=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
        finalize=lambda records, **kwargs: app._reconcile_queue_refresh(old_blob, records, **kwargs),
        plan=lambda days, started: app.queue_refresh_plan(old_blob, days, started),
        attempt_started_at=START,
    )[0]
    manager.wait()
    assert observed == [True]
    assert manager.status()["state"] == queue_live.FAILED
    assert cache_bytes() == before
