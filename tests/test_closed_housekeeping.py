"""Offline-only tests for Closed Ticket Housekeeping foundation."""
from datetime import date
from urllib.parse import parse_qs

import pytest

import app
from app import (CLOSED_STATUS, SEARCH_MAX_PAGE, SEARCH_MAX_RESULTS, SEARCH_PAGE_SIZE,
                 ClosedWindow, closed_filters_from_args, closed_query_string,
                 closed_search_url_params, retrieve_closed_tickets, split_closed_window)


def ticket(ticket_id, closed_day, tags=None):
    return {"id": ticket_id, "subject": f"Synthetic {ticket_id}", "status": 5,
            "closed_at": f"{closed_day}T12:00:00Z", "tags": tags or []}


def transport_from(rows, calls):
    def get_page(window, missing_tags_only, page):
        calls.append((window, page, missing_tags_only))
        rows_here = [r for r in rows if window.start <= date.fromisoformat(r["closed_at"][:10]) <= window.end]
        if missing_tags_only:
            rows_here = [r for r in rows_here if not r["tags"]]
        start = (page - 1) * SEARCH_PAGE_SIZE
        return {"total": len(rows_here), "results": rows_here[start:start + SEARCH_PAGE_SIZE]}
    return get_page


def test_contract_constants():
    assert CLOSED_STATUS == 5
    assert (SEARCH_PAGE_SIZE, SEARCH_MAX_PAGE, SEARCH_MAX_RESULTS) == (30, 10, 300)


def test_query_builder_is_fixed_quoted_and_encoded():
    q = closed_query_string(date(2026, 1, 1), date(2026, 1, 31), True)
    assert q == '"status:5 AND tag:null AND closed_at:>\'2026-01-01\' AND closed_at:<\'2026-01-31\'"'
    assert "tag:null" not in closed_query_string(date(2026, 1, 1), date(2026, 1, 31), False)
    params = parse_qs(closed_search_url_params(date(2026, 1, 1), date(2026, 1, 31), True, 1))
    assert params["query"] == [q] and params["page"] == ["1"]
    with pytest.raises(ValueError): closed_query_string(date(2026, 2, 1), date(2026, 1, 1), True)
    with pytest.raises(ValueError): closed_search_url_params(date.today(), date.today(), True, 11)


@pytest.mark.parametrize("raw,expected", [("1", 1), ("3650", 3650), ("0", 60), ("3651", 60), ("1.5", 60), ("x", 60), (None, 60)])
def test_closed_days_are_canonical(raw, expected):
    assert closed_filters_from_args({"days": raw})["days"] == expected


def test_midpoint_is_deterministic_and_inclusive_overlap():
    left, right = split_closed_window(ClosedWindow(date(2026, 1, 1), date(2026, 1, 11)))
    assert (left.start, left.end, right.start, right.end) == (date(2026, 1, 1), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 11))
    assert split_closed_window(ClosedWindow(date(2026, 1, 1), date(2026, 1, 1))) is None


@pytest.mark.parametrize("count", [0, 1, 30, 300])
def test_totals_at_or_under_limit_are_complete(count):
    rows = [ticket(10000+i, "2026-01-01") for i in range(count)]
    calls = []
    result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 1), True, transport_from(rows, calls))
    assert result.complete and result.unique_ticket_count == count
    assert [page for _, page, _ in calls] == list(range(1, max(1, (count + 29)//30) + 1))
    assert all(page <= 10 for _, page, _ in calls)


def test_301_splits_and_deduplicates_overlap():
    rows = [ticket(20000+i, f"2026-01-{1+i//151:02d}") for i in range(301)]
    calls = []
    result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 2), True, transport_from(rows, calls))
    assert result.complete and len(result.windows_planned) == 3
    assert result.unique_ticket_count == 301 and result.duplicate_count == 0
    assert max(page for _, page, _ in calls) <= 10


def test_duplicate_tickets_across_transport_pages_are_removed():
    rows = [ticket(15000 + i, "2026-01-01") for i in range(31)]
    def duplicate_page(window, missing, page):
        if page == 1:
            return {"total": 31, "results": rows[:30]}
        return {"total": 31, "results": [rows[0]]}
    result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 1), True, duplicate_page)
    assert result.complete and result.unique_ticket_count == 30 and result.duplicate_count == 1


def test_single_day_overflow_is_incomplete_not_truncated():
    rows = [ticket(30000+i, "2026-01-01") for i in range(301)]
    result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 1), True, transport_from(rows, []))
    assert not result.complete and result.unique_ticket_count == 0
    assert "More than 300" in result.errors[0]


def test_sorting_status_and_tag_gates_are_safe():
    rows = [ticket(3, "2026-01-02"), ticket(8, "2026-01-02"), ticket(9, "2026-01-03", ["tag"]),
            {"id": 99, "subject": "resolved", "status": 4, "closed_at": "2026-01-03T01:00:00Z", "tags": []}]
    result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 3), True, transport_from(rows, []))
    assert [x["id"] for x in result.tickets] == [8, 3]
    all_tags = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 3), False, transport_from(rows, []))
    assert [x["id"] for x in all_tags.tickets] == [9, 8, 3]


@pytest.mark.parametrize("response,error", [
    ({"results": []}, "Malformed search response"),
    ({"total": "1", "results": []}, "Malformed search response"),
])
def test_malformed_response_is_visible_incomplete(response, error):
    def bad(*_): return response
    result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 1), True, bad)
    assert not result.complete and error in result.errors[0]


def test_page_failure_429_timeout_and_short_page_are_incomplete():
    for problem in (RuntimeError("429 rate limited; Retry-After 5"), TimeoutError("timeout")):
        result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 1), True, lambda *_: (_ for _ in ()).throw(problem))
        assert not result.complete and str(problem) in result.errors[0]
    rows = [ticket(40000+i, "2026-01-01") for i in range(31)]
    def short(window, missing, page):
        return {"total": 31, "results": rows[:30] if page == 1 else []}
    result = retrieve_closed_tickets(date(2026, 1, 1), date(2026, 1, 1), True, short)
    assert not result.complete and "ended before" in result.errors[0]


def test_closed_route_offline_default_and_controls(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    client = app.app.test_client()
    response = client.get("/closed")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    for text in ("Closed Ticket Housekeeping", "OFFLINE MODE — Synthetic fixture data only", 'aria-current=page', 'href="/queue"', 'value="60"', "Missing Tags Only", "Reset to Defaults"):
        assert text in html
    assert 'target=_blank rel="noopener noreferrer"' in html
    # Prompt12: local review workflow is part of the closed page.
    assert "review_result" in html
    assert "Local review result only — does not change Freshdesk." in html


def test_closed_route_invalid_and_toggle(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    client = app.app.test_client()
    assert 'value="60"' in client.get("/closed?days=-1").get_data(as_text=True)
    off = client.get("/closed?days=60&missing_tags=0").get_data(as_text=True)
    assert "Synthetic closed tagged" in off
    submitted_off = client.get("/closed?days=60&missing_tags=0").get_data(as_text=True)
    assert "All tag states" in submitted_off


def test_closed_route_refuses_live_without_key_or_network(monkeypatch):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    monkeypatch.setattr(app, "load_api_key", lambda: (_ for _ in ()).throw(AssertionError("key read")))
    response = app.app.test_client().get("/closed")
    assert response.status_code == 503
    assert "offline-only" in response.get_data(as_text=True)


def test_offline_closed_never_has_a_write_transport():
    source = open(app.__file__).read()
    closed_section = source[source.index("# Closed-ticket housekeeping"):source.index("# Routes", source.index("# Closed-ticket housekeeping"))]
    assert "requests." not in closed_section
    assert "https://" not in closed_section
    for method in ("post(", "put(", "patch(", "delete("):
        assert method not in closed_section
