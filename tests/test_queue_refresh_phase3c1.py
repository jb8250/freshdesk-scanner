"""Offline Phase 3C1 Refresh reconciliation integration tests."""
import json
from datetime import datetime, timezone

import pytest

import app
import queue_live


OLD = "2025-01-01T00:00:00Z"
CURRENT = "2026-08-20T00:00:00Z"
NEW = "2026-08-21T00:00:00Z"


def ticket(ticket_id, updated_at=CURRENT, **extra):
    value = {"id": ticket_id, "updated_at": updated_at, "status": 2,
             "subject": f"ticket {ticket_id}", "tags": []}
    value.update(extra)
    return value


def write_cache(tickets, legacy=False):
    payload = {"days": 60, "fetched_at": 1, "tickets": tickets}
    if not legacy:
        payload.update({
            "schema_version": 2,
            "last_successful_refresh_started_at": "2026-08-01T00:00:00Z",
            "last_successful_refresh_finished_at": "2026-08-01T00:00:01Z",
            "last_refresh_mode": "reconcile",
            "last_refresh_requested_days": 60,
            "rolling_retention_days": 60,
        })
    with open(app.LIVE_QUEUE_CACHE_FILE, "w") as fh:
        json.dump(payload, fh)


def digest():
    with open(app.LIVE_QUEUE_CACHE_FILE, "rb") as fh:
        return fh.read()


def run_refresh(old_tickets, incoming, *, legacy=False, finalize=None, save=app.save_live_queue_cache):
    if old_tickets is not None:
        write_cache(old_tickets, legacy=legacy)
    old_blob = app.load_live_queue_cache()
    manager = queue_live.RefreshJobManager()
    deferred = []

    def reconcile(records, **kwargs):
        if finalize:
            return finalize(records, **kwargs)
        return app._reconcile_queue_refresh(old_blob, records, **kwargs)

    assert manager.start(days=7, api_key="fake", retrieve=lambda **kwargs: incoming,
                         save=save, finalize=reconcile)[0]
    manager.wait()
    return manager, deferred


def cache_tickets():
    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        return json.load(fh)["tickets"]


def test_short_refresh_reconciles_100_cached_tickets_instead_of_shrinking():
    existing = [ticket(i) for i in range(1, 101)]
    incoming = [ticket(98), ticket(99, NEW, subject="updated"), ticket(101, NEW)]
    manager, _ = run_refresh(existing, incoming)
    result = cache_tickets()
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert [row["id"] for row in result] == list(range(1, 102))
    assert result[98]["subject"] == "updated"
    assert manager.status()["merge_metrics"] == {
        "received_count": 3, "existing_count": 100, "incoming_count": 3, "merged_count": 101,
        "added_count": 1, "updated_count": 1, "unchanged_count": 99,
        "older_incoming_ignored_count": 0, "identical_duplicate_count": 0,
    }


def test_reconciliation_preserves_same_id_policy_and_untouched_very_old_ticket():
    existing = [ticket(1, OLD), ticket(2, CURRENT, subject="equal-old"),
                ticket(3, NEW, subject="newest"), ticket(4, "2020-01-01T00:00:00Z")]
    incoming = [ticket(1, NEW, subject="newer"), ticket(2, CURRENT, subject="equal-incoming"),
                ticket(3, OLD, subject="older"), ticket(5, NEW)]
    manager, _ = run_refresh(existing, incoming)
    result = {row["id"]: row for row in cache_tickets()}
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert set(result) == {1, 2, 3, 4, 5}
    assert result[1]["subject"] == "newer"
    assert result[2]["subject"] == "equal-incoming"
    assert result[3]["subject"] == "newest"
    assert result[4]["updated_at"] == "2020-01-01T00:00:00Z"


def test_legacy_cache_reconciles_to_v2_without_read_rewrite():
    existing = [ticket(1, OLD), ticket(2, "2020-01-01T00:00:00Z")]
    write_cache(existing, legacy=True)
    before = digest()
    assert app.load_live_queue_cache()["tickets"] == existing
    assert digest() == before
    manager, _ = run_refresh(existing, [ticket(1, NEW), ticket(3, NEW)], legacy=True)
    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        payload = json.load(fh)
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert payload["schema_version"] == 2
    assert payload["last_refresh_mode"] == "baseline"
    assert [row["id"] for row in payload["tickets"]] == [1, 2, 3]


def test_no_cache_initializes_reconcile_envelope():
    manager, _ = run_refresh(None, [ticket(1), ticket(2)])
    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        payload = json.load(fh)
    assert manager.status()["state"] == queue_live.SUCCEEDED
    assert payload["last_refresh_mode"] == "baseline"
    assert payload["days"] == payload["last_refresh_requested_days"] == 7


@pytest.mark.parametrize("incoming", [
    [{"id": 1, "updated_at": "bad"}],
    [ticket(1), ticket(1, subject="conflict")],
])
def test_invalid_incoming_never_writes_cache(incoming):
    write_cache([ticket(1)])
    before = digest()
    manager, _ = run_refresh([ticket(1)], incoming)
    assert manager.status()["state"] == queue_live.FAILED
    assert digest() == before


def test_existing_duplicates_and_malformed_existing_fail_closed_without_replacement():
    for existing in ([ticket(1), ticket(1)], [{"id": 1, "updated_at": "bad"}]):
        write_cache(existing)
        before = digest()
        manager, _ = run_refresh(existing, [ticket(1, NEW)])
        assert manager.status()["state"] == queue_live.FAILED
        assert digest() == before


def test_fetch_failure_and_cancel_after_retrieval_preserve_cache():
    write_cache([ticket(1)])
    before = digest()
    manager = queue_live.RefreshJobManager()
    assert manager.start(days=7, api_key="fake", retrieve=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("page failed")),
                         save=app.save_live_queue_cache)[0]
    manager.wait()
    assert manager.status()["state"] == queue_live.FAILED
    assert digest() == before

    old_blob = app.load_live_queue_cache()
    manager = queue_live.RefreshJobManager()
    def finalizer(records, **kwargs):
        result = app._reconcile_queue_refresh(old_blob, records, **kwargs)
        manager.cancel()
        return result
    assert manager.start(days=7, api_key="fake", retrieve=lambda **kwargs: [ticket(2)],
                         save=app.save_live_queue_cache, finalize=finalizer)[0]
    manager.wait()
    assert manager.status()["state"] == queue_live.CANCELLED
    assert digest() == before


def test_save_failure_prevents_deferred_review_advancement(monkeypatch):
    write_cache([ticket(1)])
    old_blob = app.load_live_queue_cache()
    applied = []
    monkeypatch.setattr(app, "_prepare_conversation_review_updates",
                        lambda *args, **kwargs: ([], lambda: applied.append("advanced")))
    manager = queue_live.RefreshJobManager()
    assert manager.start(days=7, api_key="fake", retrieve=lambda **kwargs: [ticket(2)],
                         save=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
                         finalize=lambda records, **kwargs: app._reconcile_queue_refresh(old_blob, records, **kwargs))[0]
    manager.wait()
    assert manager.status()["state"] == queue_live.FAILED
    assert applied == []


def test_only_effective_changes_reach_conversation_preparation(monkeypatch):
    existing = [ticket(1, NEW, subject="reviewed unchanged"), ticket(2, NEW, subject="reviewed stale")]
    old_blob = {"tickets": existing}
    observed = []
    monkeypatch.setattr(app, "_prepare_conversation_review_updates",
                        lambda records, *args, **kwargs: (observed.extend(records), lambda: None))
    result, _, _ = app._reconcile_queue_refresh(old_blob, [ticket(1, NEW, subject="reviewed unchanged"),
                                                             ticket(2, OLD, subject="reviewed stale"),
                                                             ticket(3, NEW)])
    assert [row["id"] for row in result] == [1, 2, 3]
    assert [row["id"] for row in observed] == [3]


def test_days_retrieval_uses_requested_horizon_not_old_start_cursor(monkeypatch):
    observed = {}
    class Response:
        status_code = 200
        headers = {"X-RateLimit-Remaining": "100"}
        def json(self): return []
        def raise_for_status(self): pass
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(app, "now_utc", lambda: now)
    monkeypatch.setattr(app.requests, "get", lambda url, **kwargs: observed.update(kwargs["params"]) or Response())
    list(app.paginate_tickets(days=7, clock=lambda: 0, sleeper=lambda _: None))
    assert observed["updated_since"].startswith("2026-08-14T12:00:00")
    list(app.paginate_tickets(days=45, clock=lambda: 0, sleeper=lambda _: None))
    assert observed["updated_since"].startswith("2026-07-07T12:00:00")
