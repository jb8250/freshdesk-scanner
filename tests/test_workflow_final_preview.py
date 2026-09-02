import app


def _ticket(ticket_id, created_at, updated_at):
    return {
        "id": ticket_id,
        "subject": "Photo/Video Request",
        "status": 2,
        "type": "Guest Callback/Follow-Up",
        "group_id": 154000437139,
        "tags": ["PHOTOS"],
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _config(scope="main", tab="main"):
    return dict(app.DEFAULT_FILTERS, queue_scope=scope, workflow_tab=tab,
                photo_video_only=False, hide_reviewed_tags=False)


def test_no_action_persists_after_ticket_update():
    assert app.workflow_destination("No Action", updated=True) == "no_action"
    assert app.workflow_destination("Not Applicable to Me", updated=True) == "no_action"
    assert app.canonical_review_result("Not Applicable to Me") == "No Action"
    assert app.canonical_review_result("No Action Needed") == "No Action"


def test_no_action_is_canonical_new_save(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_db_conn", lambda: None)
    monkeypatch.setattr(app, "iso_now", lambda: "2026-09-02T00:00:00+00:00")
    # Validation accepts only canonical UI state; legacy values normalize before DB work.
    assert "No Action" in app.REVIEW_STATES
    assert "Not Applicable to Me" not in app.REVIEW_STATES


def test_to_review_sort_updated_then_oldest_created(monkeypatch):
    tickets = [
        _ticket(500, "2026-09-03T00:00:00Z", "2026-09-03T00:00:00Z"),
        _ticket(100, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
        _ticket(300, "2026-09-02T00:00:00Z", "2026-09-02T00:00:00Z"),
        _ticket(700, "bad", "2026-09-05T00:00:00Z"),
        _ticket(600, "bad", "2026-09-04T00:00:00Z"),
    ]
    states = {
        700: {"review_result": "Resolved", "reviewed_updated_at": "2026-09-01T00:00:00Z"},
        600: {"review_result": "Resolved", "reviewed_updated_at": "2026-09-01T00:00:00Z"},
    }
    monkeypatch.setattr(app, "load_review_rows", lambda: states)
    monkeypatch.setattr(app, "last_opened_ticket_id", lambda: None)
    rows, *_ = app.build_current_queue_view(tickets, _config())
    assert [row["id"] for row in rows] == [700, 600, 100, 300, 500]


def test_no_action_stays_in_no_action_when_updated_and_scope_changes(monkeypatch):
    ticket = _ticket(900, "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
    states = {900: {"review_result": "No Action", "reviewed_updated_at": "2026-09-01T00:00:00Z"}}
    monkeypatch.setattr(app, "load_review_rows", lambda: states)
    monkeypatch.setattr(app, "last_opened_ticket_id", lambda: None)
    rows, counts, *_ = app.build_current_queue_view([ticket], _config(tab="no_action"))
    assert counts["no_action"] == 1
    assert [row["id"] for row in rows] == [900]
