"""Phase 5B unified Closed Ticket Housekeeping queue tests (offline only)."""
import json

import app as scanner_app
from werkzeug.datastructures import MultiDict
from app import filters_from_args, ticket_matches_photo_video


def _ticket(ticket_id, *, status=5, subject="Photo request", tags=None):
    return {
        "id": ticket_id, "status": status, "subject": subject, "tags": tags,
        "priority": 2, "updated_at": "2026-08-20T12:00:00Z",
    }


def _write_master_cache(path, tickets):
    path.write_text(json.dumps({"tickets": tickets, "days": 60, "updated_at": "2026-08-22T00:00:00Z"}))


def test_mode_defaults_normal_and_legacy_params_are_inert():
    assert filters_from_args(MultiDict())["mode"] == "normal"
    config = filters_from_args(MultiDict({"overdue": "1", "responded": "1", "waiting": "1"}))
    assert (config["overdue"], config["responded"], config["waiting"]) == (True, True, True)
    # Values remain serializable for bookmarked URLs; route-level regression
    # coverage below proves they do not restrict the queue.


def test_closed_mode_uses_only_master_cache_and_local_toggles(client, monkeypatch, tmp_path):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    master = tmp_path / "master.json"
    _write_master_cache(master, [
        _ticket(701, subject="Photo needed", tags=[]),
        _ticket(702, subject="ordinary", tags=[]),
        _ticket(703, subject="ordinary", tags=["video request"]),
        _ticket(704, subject="Photo needed", tags=["assigned"]),
        _ticket(705, status=2, subject="Photo needed", tags=[]),
    ])
    monkeypatch.setattr(scanner_app, "LIVE_QUEUE_CACHE_FILE", str(master))
    monkeypatch.setattr(scanner_app, "CACHE_FILE", str(master))
    monkeypatch.setattr(scanner_app.closed_live, "load_cache", lambda: (_ for _ in ()).throw(AssertionError("closed cache used")))

    default = client.get("/queue?mode=closed").get_data(as_text=True)
    assert 'data-ticket-id="701"' in default
    assert 'data-ticket-id="702"' not in default
    assert 'data-ticket-id="704"' not in default
    assert "Missing Tags Only" in default and "Photo/Video Review Scope" in default

    no_missing = client.get("/queue?mode=closed&missing_tags=0").get_data(as_text=True)
    assert 'data-ticket-id="704"' in no_missing
    no_scope = client.get("/queue?mode=closed&photo_video_only=0").get_data(as_text=True)
    assert 'data-ticket-id="702"' in no_scope
    all_closed = client.get("/queue?mode=closed&photo_video_only=0&missing_tags=0").get_data(as_text=True)
    for ticket_id in (701, 702, 703, 704):
        assert f'data-ticket-id="{ticket_id}"' in all_closed
    assert 'data-ticket-id="705"' not in all_closed


def test_each_legacy_param_is_inert_in_queue_route(client):
    baseline = client.get("/queue?photo_video_only=0&hide_reviewed_tags=0").get_data(as_text=True)
    baseline_ids = {line.split('data-ticket-id="', 1)[1].split('"', 1)[0] for line in baseline.splitlines() if 'data-ticket-id="' in line}
    for query in ("overdue=1", "responded=1", "waiting=1", "overdue=1&responded=1&waiting=1"):
        html = client.get(f"/queue?photo_video_only=0&hide_reviewed_tags=0&{query}").get_data(as_text=True)
        ids = {line.split('data-ticket-id="', 1)[1].split('"', 1)[0] for line in html.splitlines() if 'data-ticket-id="' in line}
        assert ids == baseline_ids


def test_shared_matcher_subject_tags_and_malformed_tags():
    assert ticket_matches_photo_video(_ticket(1, subject="Pictures requested", tags=[]))
    assert ticket_matches_photo_video(_ticket(2, subject="ordinary", tags=["VIDS"]))
    assert not ticket_matches_photo_video(_ticket(3, subject="ordinary", tags="video"))


def test_removed_selectors_are_not_visible_and_badges_remain(client):
    html = client.get("/queue").get_data(as_text=True)
    assert 'id=filter-overdue' not in html
    assert 'id=filter-responded' not in html
    assert 'id=filter-waiting' not in html
    assert "OVERDUE" in html
    assert "CUSTOMER RESPONDED" in html
    assert "WAITING ON CUSTOMER" in html


def test_closed_mode_review_posts_to_unified_review_state(client):
    page = client.get("/queue?mode=closed&photo_video_only=0&missing_tags=0")
    html = page.get_data(as_text=True)
    token = html.split('name=csrf_token value="', 1)[1].split('"', 1)[0]
    response = client.post("/queue/api/review", data={
        "csrf_token": token, "ticket_id": "500005", "review_result": "Resolved",
        "mode": "closed", "photo_video_only": "0", "missing_tags": "0",
    })
    assert response.status_code == 303
    assert scanner_app.load_review_rows()[500005]["review_result"] == "Resolved"
    assert scanner_app.load_closed_review_rows().get(500005) is None
