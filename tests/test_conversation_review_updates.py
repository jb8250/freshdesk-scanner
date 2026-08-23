"""Phase 2 conversation-aware review update tests."""
from datetime import datetime, timezone

import pytest
import requests

import app


def _ticket(updated="2026-08-20T01:49:13Z", **extra):
    value = {"id": 1, "updated_at": updated, "status": 3, "subject": "Photo", "priority": 1,
             "type": "Question", "group_id": 1, "responder_id": 2, "due_by": None,
             "fr_due_by": None, "tags": ["alpha"], "custom_fields": {"x": "y"}}
    value.update(extra)
    return value


def _conversation(created="2026-08-20T01:49:13Z", **extra):
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


def _prepared_updates(monkeypatch, conversations, current_agent_id=100):
    ticket = _ticket(id=2)
    old = _ticket(id=2, updated="2026-08-19T00:00:00Z")
    rows = {2: {"ticket_id": 2, "review_result": "Resolved", "reviewed_updated_at": old["updated_at"]}}
    monkeypatch.setattr(app, "load_review_rows", lambda: rows)
    monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: (conversations, True, 100))
    return app._prepare_conversation_review_updates(
        [ticket], {"tickets": [old]}, current_agent_id_fetcher=lambda: current_agent_id,
    )


def test_own_private_note_advances_snapshot(monkeypatch):
    updates = _prepared_updates(monkeypatch, [_conversation(user_id=100)])
    advanced = []
    monkeypatch.setattr(app, "_advance_review_snapshot", lambda *args, **kwargs: advanced.append(args) or True)
    updates[1]()
    assert advanced


@pytest.mark.parametrize("author", [200, None, "100", True])
def test_non_own_or_malformed_private_author_does_not_advance(monkeypatch, author):
    updates = _prepared_updates(monkeypatch, [_conversation(user_id=author)])
    advanced = []
    monkeypatch.setattr(app, "_advance_review_snapshot", lambda *args, **kwargs: advanced.append(args) or True)
    updates[1]()
    assert not advanced


def test_identity_failure_or_exception_does_not_advance(monkeypatch):
    for resolver in (lambda: None, lambda: (_ for _ in ()).throw(RuntimeError("offline"))):
        ticket = _ticket(id=2)
        old = _ticket(id=2, updated="2026-08-19T00:00:00Z")
        rows = {2: {"ticket_id": 2, "review_result": "Resolved", "reviewed_updated_at": old["updated_at"]}}
        monkeypatch.setattr(app, "load_review_rows", lambda: rows)
        monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: ([_conversation(user_id=100)], True, 100))
        updates = app._prepare_conversation_review_updates([ticket], {"tickets": [old]}, current_agent_id_fetcher=resolver)
        advanced = []
        monkeypatch.setattr(app, "_advance_review_snapshot", lambda *args, **kwargs: advanced.append(args) or True)
        updates[1]()
        assert not advanced


@pytest.mark.parametrize("conversations", [
    [_conversation(user_id=100), _conversation(user_id=200)],
    [_conversation(user_id=100), _conversation(incoming=True, private=False, user_id=100)],
    [_conversation(user_id=100), _conversation(private=False, user_id=100)],
    [_conversation(user_id=100), {"created_at": "2026-08-20T01:49:13Z", "updated_at": "2026-08-20T01:49:13Z", "incoming": "false", "private": True, "user_id": 100}],
])
def test_mixed_or_ambiguous_activity_does_not_advance(monkeypatch, conversations):
    updates = _prepared_updates(monkeypatch, conversations)
    advanced = []
    monkeypatch.setattr(app, "_advance_review_snapshot", lambda *args, **kwargs: advanced.append(args) or True)
    updates[1]()
    assert not advanced


def test_multiple_own_private_notes_advance_and_share_one_lookup(monkeypatch):
    ticket_one = _ticket(id=2)
    ticket_two = _ticket(id=3)
    old_one = _ticket(id=2, updated="2026-08-19T00:00:00Z")
    old_two = _ticket(id=3, updated="2026-08-19T00:00:00Z")
    rows = {ticket_id: {"ticket_id": ticket_id, "review_result": "Resolved", "reviewed_updated_at": old_one["updated_at"]} for ticket_id in (2, 3)}
    monkeypatch.setattr(app, "load_review_rows", lambda: rows)
    monkeypatch.setattr(app, "fetch_ticket_conversations", lambda *args, **kwargs: ([_conversation(user_id=100)], True, 100))
    lookups, advanced = [], []
    updates = app._prepare_conversation_review_updates([ticket_one, ticket_two], {"tickets": [old_one, old_two]}, current_agent_id_fetcher=lambda: lookups.append(1) or 100)
    monkeypatch.setattr(app, "_advance_review_snapshot", lambda *args, **kwargs: advanced.append(args) or True)
    updates[1]()
    assert len(lookups) == 1
    assert len(advanced) == 2


def test_identity_lookup_is_not_called_without_eligible_fingerprint_candidate(monkeypatch):
    ticket = _ticket(id=2)
    old = _ticket(id=2, updated="2026-08-19T00:00:00Z", status=4)
    rows = {2: {"ticket_id": 2, "review_result": "Resolved", "reviewed_updated_at": old["updated_at"]}}
    monkeypatch.setattr(app, "load_review_rows", lambda: rows)
    lookups = []
    app._prepare_conversation_review_updates([ticket], {"tickets": [old]}, current_agent_id_fetcher=lambda: lookups.append(1) or 100)
    assert not lookups


def test_fetch_current_agent_id_rejects_invalid_and_http_failures(monkeypatch):
    class Response:
        def __init__(self, status_code, payload=None, malformed=False):
            self.status_code, self.payload, self.malformed = status_code, payload, malformed
        def json(self):
            if self.malformed:
                raise ValueError("bad json")
            return self.payload
    calls = []
    monkeypatch.setattr(app.requests, "get", lambda *args, **kwargs: calls.append((args, kwargs)) or Response(200, {"id": 100}))
    assert app.fetch_current_agent_id("key") == 100
    assert len(calls) == 1 and calls[0][0][0].endswith("/api/v2/agents/me")
    for response in [Response(200, {"id": "100"}), Response(200, {"id": True}), Response(200, {}), Response(200, []), Response(200, malformed=True), Response(401), Response(403), Response(429), Response(500)]:
        monkeypatch.setattr(app.requests, "get", lambda *args, response=response, **kwargs: response)
        assert app.fetch_current_agent_id("key") is None
    monkeypatch.setattr(app.requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(requests.RequestException()))
    assert app.fetch_current_agent_id("key") is None
