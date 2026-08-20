"""Phase 2 conversation-aware review update tests."""
from datetime import datetime, timezone

import app


def _ticket(updated="2026-08-20T01:49:13Z", **extra):
    value = {"id": 1, "updated_at": updated, "status": 3, "subject": "Photo", "priority": 1,
             "type": "Question", "group_id": 1, "responder_id": 2, "due_by": None,
             "fr_due_by": None, "tags": ["alpha"], "custom_fields": {"x": "y"}}
    value.update(extra)
    return value


def _conversation(created="2026-08-20T01:49:12Z", **extra):
    value = {"created_at": created, "updated_at": created, "incoming": False,
             "private": True, "source": 15, "user_id": None}
    value.update(extra)
    return value


def test_classification_uses_private_and_incoming_only():
    assert app.conversation_classification(_conversation(source=2)) == "private"
    assert app.conversation_classification(_conversation(source=15)) == "private"
    assert app.conversation_classification(_conversation(incoming=True, private=False)) == "customer"
    assert app.conversation_classification(_conversation(private=False)) == "public"
    assert app.conversation_classification({"private": True}) == "ambiguous"
    assert app.conversation_classification(_conversation(incoming="false")) == "ambiguous"


def test_timestamps_and_fingerprint_are_conservative():
    assert app.conversation_activity_timestamp(_conversation()) is not None
    assert app.conversation_activity_timestamp({"created_at": "bad"}) is None
    assert app.review_ticket_fingerprint(_ticket()) == app.review_ticket_fingerprint(_ticket(updated="later"))
    assert app.review_ticket_fingerprint(_ticket(status=4)) != app.review_ticket_fingerprint(_ticket())
    assert app.review_ticket_fingerprint(_ticket(tags=[" B ", "a"])) == app.review_ticket_fingerprint(_ticket(tags=["a", "b"]))
    assert app.review_ticket_fingerprint(_ticket(custom_fields={"x": "z"})) != app.review_ticket_fingerprint(_ticket())


def test_acknowledge_update_is_local_and_preserves_badge(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    ticket = _ticket(id=500001, subject="Photo update")
    monkeypatch.setattr(app, "offline_paginate_tickets", lambda: iter([ticket]))
    app.set_review_result(500001, "Needs Follow-Up", "2026-08-19T00:00:00Z")
    import re
    html = client.get("/queue").get_data(as_text=True)
    token = re.search(r'name=csrf_token value="([^"]+)"', html).group(1)
    response = client.post("/queue/api/acknowledge", data={"csrf_token": token, "ticket_id": "500001"})
    assert response.status_code == 303
    row = app.load_review_rows()[500001]
    assert row["review_result"] == "Needs Follow-Up"
    assert row["reviewed_updated_at"] == ticket["updated_at"]
    assert not app.updated_since_review(ticket, row)


def test_acknowledge_rejects_invalid_token(client):
    response = client.post("/queue/api/acknowledge", data={"csrf_token": "bad", "ticket_id": "1"})
    assert response.status_code == 303
    assert not app.load_review_rows()


def test_candidate_selection_skips_unreviewed_and_unchanged(monkeypatch):
    monkeypatch.setattr(app, "load_review_rows", lambda: {})
    calls = []
    monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: calls.append(args) or ([], False, None))
    app._prepare_conversation_review_updates([_ticket()], {"tickets": [_ticket()]})
    assert calls == []


def test_private_note_only_advances_snapshot_and_mixed_activity_fails_safe(monkeypatch):
    ticket = _ticket(id=2)
    old = _ticket(id=2, updated="2026-08-19T00:00:00Z")
    rows = {2: {"ticket_id": 2, "review_result": "Resolved", "reviewed_updated_at": old["updated_at"]}}
    monkeypatch.setattr(app, "load_review_rows", lambda: rows)
    monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: ([_conversation()], True, 100))
    app._prepare_conversation_review_updates([ticket], {"tickets": [old]})[1]()
    assert app.load_review_rows() is rows
    monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: ([_conversation(), _conversation(incoming=True, private=False)], True, 100))
    updates = app._prepare_conversation_review_updates([ticket], {"tickets": [old]})
    assert updates[1]() is None
