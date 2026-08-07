"""Prompt 12 tests: local review workflow on the /closed housekeeping page.

The /closed page gains local-only review state (separate namespace from
/queue): per-ticket Review Result selector, a one-way Unreviewed ->
Opened / In Review transition on ticket click, and a single Last Opened
marker preserved across reloads. All state is stored in the local
closed_review_state table; no Freshdesk interaction of any kind occurs.

Autouse fixtures (network block + isolated DB + pinjeal clock) keep every
test offline and deterministic.
"""
import json
import re

import pytest

import app as scanner_app
from app import (app, closed_filters_from_args, closed_last_opened_ticket_id,
                 closed_page_url, closed_review_view_includes,
                 closed_ticket_known, get_csrf_token, load_closed_review_rows,
                 load_review_rows, mark_closed_opened, set_closed_review_result,
                 CLOSED_STATUS, REVIEW_STATES)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _page(client, query=""):
    resp = client.get("/closed" + query)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_data(as_text=True)


def _csrf_from(client, query=""):
    html = _page(client, query)
    m = re.search(r'name=csrf_token value="([^"]+)"', html)
    assert m, "no csrf token rendered"
    return m.group(1)


def _post_opened(client, ticket_id, csrf=None):
    csrf = csrf or _csrf_from(client)
    return client.post("/closed/api/opened",
                       json={"ticket_id": str(ticket_id)},
                       headers={"X-CSRF-Token": csrf})


def _post_review(client, ticket_id, result, csrf=None, extra=None):
    csrf = csrf or _csrf_from(client)
    data = {"csrf_token": csrf, "ticket_id": str(ticket_id),
            "review_result": result, "days": "60", "missing_tags": "0",
            "review_view": "all"}
    if extra:
        data.update(extra)
    return client.post("/closed/api/review", data=data)


@pytest.fixture
def fixed_clock_fix(monkeypatch):
    from datetime import datetime, timezone
    ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scanner_app, "now_utc", lambda: ref)
    return ref


# ---------------------------------------------------------------------------
# storage namespace
# ---------------------------------------------------------------------------

def test_closed_review_state_is_separate_namespace(fixed_clock_fix):
    """Closed review rows live in closed_review_state only; the queue's
    review_state table must never contain them."""
    set_closed_review_result(810001, "Resolved")
    closed_rows = load_closed_review_rows()
    assert closed_rows[810001]["review_result"] == "Resolved"
    queue_rows = load_review_rows()
    assert 810001 not in queue_rows
    # Queue writes do not appear in the closed namespace either.
    mark_closed_opened(810002)  # uses the *closed* table


def test_mark_closed_opened_uses_closed_table_only(fixed_clock_fix):
    mark_closed_opened(810001)
    assert list(load_review_rows()) == []          # queue untouched
    assert 810001 in load_closed_review_rows()     # closed table written


def test_closed_ticket_known_synthetic_ids():
    assert closed_ticket_known(810001)
    assert closed_ticket_known(820300)
    assert not closed_ticket_known(999999)
    assert not closed_ticket_known(810005)  # status 4, excluded corpus


# ---------------------------------------------------------------------------
# /closed/api/opened
# ---------------------------------------------------------------------------

def test_opened_unreviewed_becomes_in_review_and_returns_json(client, fixed_clock_fix):
    resp = _post_opened(client, 810001)
    assert resp.status_code == 200
    body = json.loads(resp.get_data(as_text=True))
    assert body["ok"] is True
    assert body["review_result"] == "Opened / In Review"
    assert body["last_opened_id"] == 810001
    assert closed_last_opened_ticket_id() == 810001


def test_opened_preserves_existing_review_result(client, fixed_clock_fix):
    set_closed_review_result(810001, "Resolved")
    resp = _post_opened(client, 810001)
    body = json.loads(resp.get_data(as_text=True))
    assert body["review_result"] == "Resolved"   # deliberate state preserved
    assert body["last_opened_id"] == 810001      # marker still moves


def test_opened_moves_last_opened_on_second_click(client, fixed_clock_fix):
    _post_opened(client, 810001)
    _post_opened(client, 810002)
    assert closed_last_opened_ticket_id() == 810002


def test_opened_requires_csrf(client):
    resp = client.post("/closed/api/opened",
                       json={"ticket_id": "810001"})
    assert resp.status_code == 403


def test_opened_unknown_ticket_404(client):
    resp = _post_opened(client, 999999)
    assert resp.status_code == 404


def test_opened_rejects_missing_and_malformed_ids(client):
    csrf = _csrf_from(client)
    for payload in ({}, {"ticket_id": "abc"}):
        resp = client.post("/closed/api/opened", json=payload,
                           headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 400, payload


# ---------------------------------------------------------------------------
# /closed/api/review
# ---------------------------------------------------------------------------

def test_review_saved_and_linked_state(fixed_clock_fix):
    set_closed_review_result(810001, "Needs Follow-Up")
    row = load_closed_review_rows()[810001]
    assert row["review_result"] == "Needs Follow-Up"
    assert row["last_review_change_at"]


def test_review_states_all_roundtrip(fixed_clock_fix):
    for state in REVIEW_STATES:
        set_closed_review_result(810002, state)
        assert load_closed_review_rows()[810002]["review_result"] == state


def test_review_post_redirects_preserving_filters(client):
    csrf = _csrf_from(client)  # rows exist on the default/all view
    resp = _post_review(client, 810001, "Resolved", csrf=csrf,
                        extra={"days": "90", "missing_tags": "1",
                               "review_view": "completed"})
    assert resp.status_code == 303
    loc = resp.headers["Location"]
    assert "days=90" in loc
    assert "missing_tags=1" in loc
    assert "review_view=completed" in loc
    assert load_closed_review_rows()[810001]["review_result"] == "Resolved"


def test_review_result_renders_badge_and_selected(client, fixed_clock_fix):
    csrf = _csrf_from(client)
    _post_review(client, 810001, "Not Applicable to Me", csrf=csrf)
    set_closed_review_result(810001, "Not Applicable to Me")
    html = _page(client, "?review_view=all&missing_tags=0")
    assert 'class="badge b-review rv-na"' in html
    assert re.search(r'<option value="Not Applicable to Me" selected>', html)


def test_review_requires_csrf(client):
    resp = client.post("/closed/api/review",
                       data={"ticket_id": "810001", "review_result": "Resolved"})
    assert resp.status_code == 303
    html = _page(client)
    assert "invalid security token" in html
    assert 810001 not in load_closed_review_rows()


def test_review_rejects_unknown_result(client):
    csrf = _csrf_from(client)
    resp = _post_review(client, 810001, "Bogus", csrf=csrf)
    assert resp.status_code == 303
    assert "unknown review result" in _page(client)
    assert 810001 not in load_closed_review_rows()


def test_review_rejects_unknown_ticket(client):
    csrf = _csrf_from(client)
    resp = _post_review(client, 999999, "Resolved", csrf=csrf)
    assert resp.status_code == 303
    assert "unknown ticket" in _page(client)


# ---------------------------------------------------------------------------
# review_view filtering + canonical URL
# ---------------------------------------------------------------------------

def test_review_view_includes_active_completed_all(fixed_clock_fix):
    set_closed_review_result(810001, "Resolved")          # completed
    set_closed_review_result(810002, "Opened / In Review")  # active
    # By review_result only (no updated_flag on the closed page).
    assert closed_review_view_includes({"review_result": "Unreviewed"}, "active")
    assert not closed_review_view_includes({"review_result": "Resolved"}, "active")
    assert closed_review_view_includes({"review_result": "Resolved"}, "completed")
    assert not closed_review_view_includes({"review_result": "Unreviewed"}, "completed")
    assert closed_review_view_includes({"review_result": "Resolved"}, "all")


def test_closed_filters_parse_review_view():
    cfg = closed_filters_from_args({"review_view": "completed"})
    assert cfg["review_view"] == "completed"
    cfg = closed_filters_from_args({"review_view": "bogus"})
    assert cfg["review_view"] == "active"  # invalid -> default
    cfg = closed_filters_from_args({})
    assert cfg["review_view"] == "active"


def test_closed_page_url_preserves_review_view():
    cfg = closed_filters_from_args({"days": "30", "missing_tags": "0",
                                    "review_view": "completed"})
    url = closed_page_url(cfg)
    assert "days=30" in url and "review_view=completed" in url


def test_review_view_filters_rendered_rows(client, fixed_clock_fix):
    csrf = _csrf_from(client, "?review_view=all&missing_tags=0")
    _post_review(client, 810003, "Resolved", csrf=csrf)
    html_active = _page(client, "?review_view=active&missing_tags=0")
    # 810003 is now completed -> hidden from active view.
    assert re.search(r'data-ticket-id="810003"', html_active) is None
    html_completed = _page(client, "?review_view=completed&missing_tags=0")
    assert re.search(r'data-ticket-id="810003"', html_completed)


# ---------------------------------------------------------------------------
# Last Opened marker + jump control
# ---------------------------------------------------------------------------

def test_last_opened_marker_renders_jump(client, fixed_clock_fix):
    _post_opened(client, 810001)
    html = _page(client, "?missing_tags=0")
    assert "Jump to Last Opened" in html
    assert re.search(r'class="[^"]*\brv-last-opened\b[^"]*" data-ticket-id="810001"', html)
    assert 'id=last-opened-jump' in html


def test_no_jump_before_any_open(client, fixed_clock_fix):
    html = _page(client)
    # The JS strings mention LAST OPENED; the actual controls render server-side.
    assert 'class="badge b-last-opened"' not in html
    assert 'id=last-opened-jump' not in html
    assert 'id=last-opened-hidden' not in html


def test_last_opened_survives_reload(client, fixed_clock_fix):
    """Marker persists on a fresh request — state lives in the DB, not the
    page or session."""
    _post_opened(client, 810002)
    html = _page(client, "?missing_tags=0")
    assert re.search(r'class="[^"]*\brv-last-opened\b[^"]*" data-ticket-id="810002"', html)
    html2 = _page(client, "?missing_tags=0")  # reload
    assert re.search(r'class="[^"]*\brv-last-opened\b[^"]*" data-ticket-id="810002"', html2)


def test_last_opened_moves_on_new_click(client, fixed_clock_fix):
    _post_opened(client, 810001)
    _post_opened(client, 810002)
    html = _page(client, "?missing_tags=0")
    assert re.search(r'class="[^"]*\brv-last-opened\b[^"]*" data-ticket-id="810002"', html)
    assert re.search(r'data-ticket-id="810001"', html)  # still listed


def test_last_opened_hidden_banner_when_filtered_out(client, fixed_clock_fix):
    # 810002 is tagged; the default Missing Tags Only view hides it, so the
    # marker banner explains the hidden state instead of showing the row.
    _post_opened(client, 810002)
    html = _page(client)  # default missing_tags=1 hides tagged 810002
    assert "Last opened ticket is hidden by the current filters." in html


# ---------------------------------------------------------------------------
# route exposure / offline-only
# ---------------------------------------------------------------------------

def test_closed_api_routes_are_post_only():
    rules = {r.rule: sorted((r.methods or set()) - {"HEAD", "OPTIONS"})
             for r in app.url_map.iter_rules()}
    assert rules["/closed/api/review"] == ["POST"]
    assert rules["/closed/api/opened"] == ["POST"]
    assert rules["/closed"] == ["GET"]


def test_closed_review_api_offline_only(monkeypatch):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    client = app.test_client()
    resp = client.post("/closed/api/review",
                       data={"ticket_id": "810001", "review_result": "Resolved"})
    assert resp.status_code == 503
    resp2 = client.post("/closed/api/opened", json={"ticket_id": "810001"})
    assert resp2.status_code == 503


def test_closed_section_has_no_http_verb():
    """The closed housekeeping + review section keeps zero Freshdesk
    transport: no requests.*, no https URLs, no non-GET localspells."""
    source = open(scanner_app.__file__).read()
    start = source.index("# Closed-ticket housekeeping")
    end = source.index("# Routes", start)
    section = source[start:end]
    assert "requests." not in section
    assert "https://" not in section
    for verb in ("post(", "put(", "patch(", "delete("):
        assert verb not in section, verb