"""Offline-only tests for Prompt 10 — Closed Status Label.

Confirms the /closed Status column shows the label "Closed" instead of the raw
integer 5, that the integer 5 remains the internal/API filter/query value, that
invalid or unexpected statuses are never mislabelled "Closed", and that the
queue page is unchanged.
"""
import re


def _html(extra=""):
    """Render /closed with FRESHDESK_OFFLINE=1 and return its HTML."""
    import app
    client = app.app.test_client()
    resp = client.get("/closed" + extra)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


import pytest
from app import CLOSED_STATUS, STATUS_LABELS, status_label


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")


# --- status_label helper -------------------------------------------------

def test_status_label_maps_closed_5_to_closed():
    assert status_label(5) == "Closed"
    assert status_label(CLOSED_STATUS) == "Closed"


def test_status_label_other_ints_are_not_closed():
    for val in (4, 2, 6, 1, 3, 7, 0, 999):
        assert status_label(val) != "Closed"
    # Known statuses keep their real label.
    assert status_label(4) == "Resolved"
    assert status_label(2) == "Customer responded"
    assert status_label(6) == "Waiting on customer"


def test_status_label_malformed_is_unknown_not_closed():
    for val in (None, "", "5", "nonsense", -1, True, False, 5.0):
        assert status_label(val) != "Closed"


def test_status_label_string_5_is_not_normalised_to_closed():
    # The closed-ticket parser keeps status a strict integer 5; a string "5"
    # is therefore not admitted and must never be labelled Closed.
    assert status_label("5") == "Unknown"
    assert not isinstance(status_label("5"), int)


def test_closed_status_lookup_matches_label_mapping():
    # The "Closed" text comes from the single centralised mapping.
    assert STATUS_LABELS[CLOSED_STATUS] == "Closed"
    assert status_label(5) == STATUS_LABELS[CLOSED_STATUS]


# --- rendered table ---

def test_closed_table_status_cells_show_closed():
    html = _html()
    cells = re.findall(r'<span class="badge b-closed">([^<]*)</span>', html)
    assert cells, "expected at least one status cell"
    assert all(cell == "Closed" for cell in cells)


def test_closed_table_never_shows_raw_5_in_status():
    # Prompt 13 moved the b-closed badge out of its own column into the
    # Subject cell's badge cluster, so the selector no longer scopes to a
    # dedicated <td>. The guarantee is unchanged: the status badge reads
    # "Closed", never the raw integer 5, and no raw 5 is rendered.
    html = _html()
    status_badges = re.findall(r'<span class="badge b-closed">([^<]*)</span>', html)
    assert status_badges
    assert all(badge != "5" for badge in status_badges)
    assert re.search(r">5<", html) is None, "raw status 5 must never be visible"


def test_closed_badge_uses_shared_styling():
    import app
    assert ".b-closed" in app._SHARED_CSS
    assert "badge b-closed" in app.CLOSED_HTML


def test_every_closed_row_uses_label_not_numeric():
    html = _html()
    rows = re.findall(r'<td><a class="?tid.*?</tr>', html)
    assert rows
    for row in rows:
        assert '<span class="badge b-closed">Closed</span>' in row


# --- internal value + filtering --------------------------------------------

def test_internal_status_remains_5():
    assert CLOSED_STATUS == 5
    html = _html()
    # Status-4 fixture ticket (Synthetic resolved excluded) must not appear.
    assert "Synthetic resolved excluded" not in html


def test_query_builder_still_uses_status_5():
    from datetime import date
    from app import closed_query_string
    q = closed_query_string(date(2026, 1, 1), date(2026, 1, 31), True)
    assert "status:5" in q


def test_retrieval_filters_on_internal_status_5():
    from datetime import date
    from app import retrieve_closed_tickets
    res = retrieve_closed_tickets(date(2025, 12, 25), date(2026, 8, 5), True)
    ids = {t["id"] for t in res.tickets}
    assert 810005 not in ids  # the status-4 fixture stays excluded
    assert all(t["status"] == CLOSED_STATUS for t in res.tickets)


def test_queue_page_unchanged():
    import app
    client = app.app.test_client()
    html = client.get("/queue").get_data(as_text=True)
    assert "Freshdesk Review Queue" in html