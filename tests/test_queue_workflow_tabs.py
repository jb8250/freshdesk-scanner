import os

import pytest

import app
from app import (
    CLOSED_STATUS,
    WORKFLOW_TABS,
    human_age,
    parse_workflow_tab,
    workflow_destination,
    workflow_tab_includes,
)


def test_workflow_mapping_and_supervisor_state():
    assert WORKFLOW_TABS == ("main", "supervisor", "followup", "resolved", "no_action")
    assert workflow_destination("Needs Supervisor Review") == "supervisor"
    assert workflow_destination("Needs Follow-Up") == "followup"
    assert workflow_destination("Resolved") == "resolved"
    assert workflow_destination("No Action Needed") == "no_action"
    assert workflow_destination("Unreviewed") == "main"
    assert parse_workflow_tab("bogus") == "main"


def test_updated_ticket_returns_to_main():
    assert workflow_destination("Resolved", updated=True) == "main"
    assert workflow_tab_includes({"review_result": "Resolved"}, True, "main")
    assert not workflow_tab_includes({"review_result": "Resolved"}, True, "resolved")


def test_human_age_labels():
    assert human_age(0) == "Just now"
    assert human_age(90) == "1m ago"
    assert human_age(3600) == "1h ago"
    assert human_age(86400 * 2) == "2d ago"
    assert human_age(None) == "Unknown"


def test_queue_tabs_are_local_only(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: calls.append(a) or pytest.fail("network"))
    for tab in WORKFLOW_TABS:
        response = client.get(f"/queue?workflow_tab={tab}")
        assert response.status_code == 200
        assert calls == []


def test_queue_normal_tabs_exclude_closed_and_show_all_can_include(client):
    normal = client.get("/queue?photo_video_only=1&hide_reviewed_tags=1&workflow_tab=main").get_data(as_text=True)
    assert 'data-ticket-id="500005"' not in normal
    assert CLOSED_STATUS == 5
    explicit = client.get("/queue?photo_video_only=0&hide_reviewed_tags=0&overdue=0&responded=0&waiting=0&missing_tags=0&workflow_tab=main").get_data(as_text=True)
    assert 'data-ticket-id="500005"' in explicit


def test_queue_range_controls_render_custom_state_and_safe_wiring(client):
    presets = (7, 14, 30, 60, 90)
    for days in presets:
        html = client.get(f"/queue?days={days}").get_data(as_text=True)
        assert f'aria-current=page>{days}d</a>' in html
        assert 'id=custom-days-toggle' in html
        assert 'type=button' in html
        assert 'id=custom-days-wrap hidden' in html
    html = client.get("/queue?days=45").get_data(as_text=True)
    assert 'id=custom-days-toggle aria-current=page' in html
    assert 'aria-pressed="true"' in html
    assert 'id=custom-days-wrap' in html and 'id=custom-days-wrap hidden' not in html
    assert 'id=custom-days type=number name=days min=1 max=365 value="45" aria-label="Custom days" step=1' in html
    assert "customWrap.classList.remove('hidden')" in html
    assert "customWrap.hidden = false" in html
    assert "customInput.focus()" in html
    assert "function selectCustomRange()" in html
    assert "document.querySelectorAll('.reconcile-panel .preset')" in html
    assert "control.classList.remove('active', 'preset-on')" in html
    assert "control.setAttribute('aria-pressed', 'false')" in html
    # Native hidden state must win over any later display rule in a real browser.
    assert "[hidden]{display:none!important}" in html


def test_queue_range_controls_are_local_and_refresh_uses_custom_value(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "is_offline", lambda: False)
    monkeypatch.setattr(app, "load_api_key", lambda: "test-key")
    monkeypatch.setattr(app.queue_live.JOB, "start", lambda **kwargs: calls.append(kwargs) or (True, "started"))
    with client.session_transaction() as session:
        csrf = session.get("csrf_token")
    assert client.get("/queue?days=45").status_code == 200
    with client.session_transaction() as session:
        csrf = session["csrf_token"]
    response = client.post("/queue/api/refresh", data={"csrf_token": csrf, "days": "45", "mode": "reconcile"})
    assert response.status_code == 202
    assert calls and calls[0]["days"] == 45
