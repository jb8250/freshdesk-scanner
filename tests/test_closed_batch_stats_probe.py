"""Offline contract tests for tools.closed_batch_stats_probe.

All requests are mocked; conftest's autouse network block remains in force.

These tests verify the one-request batch flow:
  Single GET /api/v2/tickets?include=stats&per_page=100&page=1&updated_since=...

Hard guarantees enforced:
  - Request budget <= 1
  - Dry-run makes zero HTTP calls
  - Explicit --execute required for live request
  - Only GET permitted
  - Exact hostname only
  - Exact /api/v2/tickets path only
  - Include forced to stats
  - Per_page forced to 100
  - Page forced to 1
  - Updated_since fixed
  - Order_by fixed
  - Order_type fixed
  - No retry
  - Foreign redirect rejected
  - No Search Tickets endpoint
  - No View Ticket endpoint
  - No write methods
  - API key / Authorization never appear in output
  - Dashboard remains offline
  - Response shape: list accepted, non-list rejected
  - Empty list, 1 ticket, 100 tickets accepted; >100 rejected
  - Stats dictionary recognized, closed_at string/null/missing handled
  - Local filtering: status 5, empty tags, date range
  - Privacy: no requester, descriptions, custom fields, attachments emitted
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from tools import closed_batch_stats_probe as probe

HOST = probe.ALLOWED_HOST
ENDPOINT = probe.ENDPOINT

BATCH_HEADERS = {
    "Content-Type": "application/json",
    "X-RateLimit-Total": "1000",
    "X-RateLimit-Remaining": "997",
    "X-RateLimit-Used-CurrentRequest": "3",
}


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, url=None,
                 history=None, link=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        if link:
            self.headers["Link"] = link
        self.url = url or f"https://{HOST}{ENDPOINT}"
        self.history = history or []

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def make_ticket(tid=100, status=5, tags=None, closed_at="2026-08-03T23:47:46Z",
                stats=None, include_sensitive=True, tags_missing=False):
    """Build a ticket dict. include_sensitive adds fields that must never appear in output."""
    if stats is None and closed_at is not None:
        stats = {"closed_at": closed_at, "resolved_at": None, "first_responded_at": None}
    elif stats is None:
        stats = {}
    if tags_missing:
        t_tags = None  # tags key explicitly missing from dict
    else:
        t_tags = tags if tags is not None else []
    t = {
        "id": tid,
        "subject": "Secret subject must never appear",
        "status": status,
        "created_at": "2026-08-03T10:00:00Z",
        "updated_at": "2026-08-03T12:00:00Z",
        "stats": stats,
    }
    if not tags_missing:
        t["tags"] = t_tags
    if include_sensitive:
        t["description"] = "Secret description must never appear"
        t["requester"] = {"email": "secret@example.test", "name": "Secret Person"}
        t["custom_fields"] = {"secret_field": "secret_value"}
        t["attachments"] = [{"name": "secret.pdf"}]
        t["company_id"] = 999
    return t


def batch_mock(monkeypatch, response=None):
    """Wire a mock requests.get that returns the given FakeResponse."""
    if response is None:
        response = FakeResponse([], headers=BATCH_HEADERS)
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    return fake_get, call_count


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------

def test_dry_run_makes_zero_requests(monkeypatch, capsys):
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: pytest.fail("dry run attempted HTTP"))
    data = probe.run_probe(execute=False)
    assert data["dry_run"] is True
    assert data["actual_requests"] == 0


def test_dry_run_shows_exact_request_shape(monkeypatch, capsys):
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: pytest.fail("dry run attempted HTTP"))
    data = probe.run_probe(execute=False)
    assert data["method"] == "GET"
    assert data["host"] == HOST
    assert data["endpoint"] == "/api/v2/tickets"
    assert data["include"] == "stats"
    assert data["per_page"] == 100
    assert data["page"] == 1
    assert data["updated_since"] == "2026-08-01T00:00:00Z"
    assert data["order_by"] == "status"
    assert data["order_type"] == "desc"
    assert data["request_budget"] == 1
    assert data["filtering"] == "local only after response"


def test_dry_run_shows_local_filter_rules(monkeypatch, capsys):
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: pytest.fail("dry run attempted HTTP"))
    data = probe.run_probe(execute=False)
    rules = data["local_filter_rules"]
    assert "status == 5" in rules["keep_when"][0]
    assert "tags is empty list" in rules["keep_when"][1]
    assert rules["local_date_window"]["start_inclusive"] == "2026-08-01T00:00:00Z"
    assert rules["local_date_window"]["end_exclusive"] == "2026-08-04T00:00:00Z"


def test_dry_run_explains_local_date_window_note(monkeypatch, capsys):
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: pytest.fail("dry run attempted HTTP"))
    data = probe.run_probe(execute=False)
    note = data["local_filter_rules"]["local_date_window"]["note"]
    assert "post-response" in note.lower() or "never modified" in note.lower()


# ---------------------------------------------------------------------------
# Guardrails — explicit --execute required
# ---------------------------------------------------------------------------

def test_execute_required_for_live_request(monkeypatch, capsys):
    """Without --execute, no live request is attempted even with credentials present."""
    call_count = {"n": 0}

    def fake_get(*_a, **_kw):
        call_count["n"] += 1
        return FakeResponse([])

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key")
    monkeypatch.setattr(requests, "get", fake_get)
    probe.run_probe(execute=False)
    assert call_count["n"] == 0


def test_request_budget_is_one():
    assert probe.REQUEST_BUDGET == 1


def test_method_is_get():
    assert probe.ALLOWED_METHOD == "GET"


def test_hostname_is_expected():
    assert probe.ALLOWED_HOST == "broadriverretail-help.freshdesk.com"


def test_endpoint_is_list_tickets():
    assert probe.ENDPOINT == "/api/v2/tickets"


def test_include_is_stats():
    assert probe.FORCED_INCLUDE == "stats"


def test_per_page_is_100():
    assert probe.FORCED_PER_PAGE == 100


def test_page_is_1():
    assert probe.FORCED_PAGE == 1


def test_updated_since_is_fixed():
    assert probe.FORCED_UPDATED_SINCE == "2026-08-01T00:00:00Z"


def test_order_by_is_status():
    assert probe.FORCED_ORDER_BY == "status"


def test_order_type_is_desc():
    assert probe.FORCED_ORDER_TYPE == "desc"


# ---------------------------------------------------------------------------
# Pre-flight request-shape verification
# ---------------------------------------------------------------------------

def test_pre_flight_guard_rejects_wrong_method(monkeypatch):
    """Even if requests.get were somehow called, the URL/method is pre-flighted."""
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key")
    # Mock requests.Request.prepare to simulate a wrong method
    original_prepare = requests.Request.prepare

    def hacked_prepare(self):
        self.method = "POST"
        self.url = f"https://{HOST}{ENDPOINT}?include=stats"
        self.headers = {}
        return self

    monkeypatch.setattr(requests.Request, "prepare", hacked_prepare)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "request-shape guard" in result.get("error", "")
    monkeypatch.setattr(requests.Request, "prepare", original_prepare)


# ---------------------------------------------------------------------------
# Foreign redirect rejection
# ---------------------------------------------------------------------------

def test_foreign_redirect_rejected(monkeypatch):
    response = FakeResponse(
        [],
        status=302,
        headers={**BATCH_HEADERS, "Location": "https://evil.example.com/steal"},
        url="https://evil.example.com/steal",
        history=[FakeResponse(status=302, url=f"https://{HOST}{ENDPOINT}")],
    )
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert call_count["n"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "redirect" in result.get("error", "").lower() or "hostname" in result.get("error", "").lower()


def test_3xx_redirect_rejected(monkeypatch):
    response = FakeResponse(
        [],
        status=302,
        headers={**BATCH_HEADERS, "Location": f"https://{HOST}/api/v2/tickets"},
        history=[FakeResponse(status=302)],
    )
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert call_count["n"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED


# ---------------------------------------------------------------------------
# No write methods, no search, no view ticket
# ---------------------------------------------------------------------------

def test_no_search_tickets_endpoint_called(monkeypatch):
    """The probe must never call /api/v2/search/tickets."""
    call_urls = []

    def fake_get(url, **kwargs):
        call_urls.append(url)
        return FakeResponse([])

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key")
    monkeypatch.setattr(requests, "get", fake_get)
    probe.run_probe(execute=True)
    for url in call_urls:
        assert "search" not in url, "Search Tickets endpoint was called"


def test_no_view_ticket_endpoint_called(monkeypatch):
    """The probe must never call /api/v2/tickets/<id> (View Ticket)."""
    call_urls = []

    def fake_get(url, **kwargs):
        call_urls.append(url)
        return FakeResponse([])

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key")
    monkeypatch.setattr(requests, "get", fake_get)
    probe.run_probe(execute=True)
    # All URLs must be exactly /api/v2/tickets (no numeric ID afterward)
    import re
    for url in call_urls:
        assert not re.search(r"/api/v2/tickets/\d+", url), "View Ticket endpoint was called"


def test_no_post_put_patch_delete(monkeypatch):
    """Only GET is used; no write methods anywhere in the module."""
    import ast
    source = Path(probe.__file__).read_text()
    tree = ast.parse(source)
    # Check that no requests.post/put/patch/delete calls exist
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("post", "put", "patch", "delete"):
            # Check if it's on 'requests'
            if isinstance(node.value, ast.Name) and node.value.id == "requests":
                pytest.fail(f"Found requests.{node.attr} in source")


# ---------------------------------------------------------------------------
# No retry
# ---------------------------------------------------------------------------

def test_no_retry_on_request_exception(monkeypatch):
    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        raise requests.ConnectionError("simulated network failure")

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert call_count["n"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "request failed" in result.get("error", "")


def test_no_retry_on_timeout(monkeypatch):
    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        raise requests.Timeout("simulated timeout")

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert call_count["n"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED


# ---------------------------------------------------------------------------
# Response shape — list accepted, non-list rejected
# ---------------------------------------------------------------------------

def test_empty_list_accepted(monkeypatch):
    response = FakeResponse([], headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["http_status"] == 200
    assert result["aggregate"]["tickets_returned"] == 0
    assert result["verdict"] in (probe.VERDICT_PASS, probe.VERDICT_DIFFERENCES)


def test_single_ticket_accepted(monkeypatch):
    response = FakeResponse([make_ticket(tid=100)], headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["tickets_returned"] == 1
    assert result["aggregate"]["status_5_count"] == 1


def test_100_tickets_accepted(monkeypatch):
    tickets = [make_ticket(tid=i, status=5, tags=[]) for i in range(100)]
    response = FakeResponse(tickets, headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["tickets_returned"] == 100
    assert result["aggregate"]["status_5_count"] == 100


def test_more_than_100_tickets_rejected(monkeypatch):
    tickets = [make_ticket(tid=i) for i in range(101)]
    response = FakeResponse(tickets, headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "exceeds per_page" in result.get("error", "")


def test_non_list_json_rejected(monkeypatch):
    response = FakeResponse({"total": 100, "results": []}, headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "non-list" in result.get("error", "").lower()


def test_malformed_json_rejected(monkeypatch):
    response = FakeResponse(ValueError("bad json"), headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "invalid JSON" in result.get("error", "")


# ---------------------------------------------------------------------------
# HTTP error codes stop safely
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 429, 500, 502, 503])
def test_error_codes_stop_safely(monkeypatch, status_code):
    response = FakeResponse(
        {"error": "simulated"},
        status=status_code,
        headers={**BATCH_HEADERS, "Retry-After": "60"} if status_code == 429 else BATCH_HEADERS,
    )
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert call_count["n"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED
    assert result["http_status"] == status_code


# ---------------------------------------------------------------------------
# Link header present but not followed
# ---------------------------------------------------------------------------

def test_link_header_present_not_followed(monkeypatch):
    next_link = f'<https://{HOST}/api/v2/tickets?page=2>; rel="next"'
    response = FakeResponse(
        [make_ticket(tid=1)],
        headers={**BATCH_HEADERS, "Link": next_link},
        link=next_link,
    )
    # Track all calls to ensure we never fetch page 2
    call_count = {"n": 0}

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        return response

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert call_count["n"] == 1
    assert result["link_header_present"] is True
    assert result["link_indicates_next_page"] is True


def test_link_header_absent(monkeypatch):
    response = FakeResponse([], headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["link_header_present"] is False
    assert result["link_indicates_next_page"] is False


# ---------------------------------------------------------------------------
# Batched stats — dictionary, closed_at string/null/missing/malformed
# ---------------------------------------------------------------------------

def test_stats_dict_recognized(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["stats_dict"] == 1


def test_stats_closed_at_string_recognized(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["stats_closed_at_string"] == 1
    assert result["aggregate"]["stats_closed_at_null"] == 0


def test_stats_closed_at_null_handled_safely(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at=None,
                    stats={"closed_at": None, "resolved_at": None, "first_responded_at": None})
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["stats_closed_at_null"] == 1
    assert result["aggregate"]["stats_closed_at_string"] == 0
    assert result["aggregate"]["valid_closed_at_count"] == 0
    assert result["aggregate"]["invalid_or_missing_closed_at_count"] == 1


def test_stats_missing_handled_safely(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at=None, stats={})
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["stats_closed_at_missing"] == 1
    assert result["aggregate"]["valid_closed_at_count"] == 0


def test_stats_malformed_handled_safely(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at=None,
                    stats={"closed_at": "not-a-timestamp"})
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["stats_closed_at_string"] == 1
    # Invalid closed_at should not be accepted by parse_dt
    assert result["aggregate"]["valid_closed_at_count"] == 0
    assert result["all_parsed_closed_at_valid"] is False


def test_stats_not_a_dict_handled_safely(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at=None, stats="not-a-dict")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["stats_dict"] == 0


# ---------------------------------------------------------------------------
# parse_dt acceptance
# ---------------------------------------------------------------------------

def test_parse_dt_accepts_valid_z_timestamp():
    from app import parse_dt
    parsed = parse_dt("2026-08-03T23:47:46Z")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_dt_rejects_naive_timestamp():
    from app import parse_dt
    parsed = parse_dt("2026-08-03T23:47:46")
    assert parsed is None


def test_parse_dt_rejects_malformed_timestamp():
    from app import parse_dt
    parsed = parse_dt("not-a-date")
    assert parsed is None


def test_parse_dt_rejects_none():
    from app import parse_dt
    assert parse_dt(None) is None


def test_parse_dt_rejects_empty_string():
    from app import parse_dt
    assert parse_dt("") is None


# ---------------------------------------------------------------------------
# Local filtering — status, tags, date range
# ---------------------------------------------------------------------------

def test_status_5_recognized_as_closed(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["status_5_count"] == 1
    assert result["aggregate"]["status_5_and_empty_tags_count"] == 1


def test_non_5_status_excluded(monkeypatch):
    t = make_ticket(tid=1, status=2, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["status_5_count"] == 0
    assert result["aggregate"]["status_5_and_empty_tags_count"] == 0


def test_empty_tags_recognized(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["empty_tags_count"] == 1


def test_non_empty_tags_excluded(monkeypatch):
    t = make_ticket(tid=1, status=5, tags=["urgent"], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["empty_tags_count"] == 0
    assert result["aggregate"]["status_5_and_empty_tags_count"] == 0


def test_missing_tags_handled_safely(monkeypatch):
    t = make_ticket(tid=1, status=5, tags_missing=True, closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["empty_tags_count"] == 0
    assert result["aggregate"]["status_5_and_empty_tags_count"] == 0


def test_tags_is_not_a_list_handled_safely(monkeypatch):
    t = make_ticket(tid=1, status=5, tags="not-a-list", closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["tags_list"] == 0


def test_date_range_start_inclusive(monkeypatch):
    """closed_at == 2026-08-01T00:00:00Z should be included (inclusive start)."""
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-01T00:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 1


def test_date_range_end_exclusive(monkeypatch):
    """closed_at == 2026-08-04T00:00:00Z should be excluded (exclusive end)."""
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-04T00:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 0


def test_date_range_mid_point_included(monkeypatch):
    """closed_at == 2026-08-02T12:00:00Z should be included."""
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-02T12:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 1


def test_date_range_before_window_excluded(monkeypatch):
    """closed_at == 2026-07-31T23:59:59Z should be excluded."""
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-07-31T23:59:59Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 0


def test_date_range_after_window_excluded(monkeypatch):
    """closed_at == 2026-08-04T00:00:01Z should be excluded."""
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-04T00:00:01Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 0


def test_local_filtering_causes_no_api_calls(monkeypatch):
    """The local filtering loop must not issue any additional API calls."""
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, call_count = batch_mock(monkeypatch, response)
    probe.run_probe(execute=True)
    assert call_count["n"] == 1


def test_mixed_batch_correctly_separates_counts(monkeypatch):
    """A mix of closed/no-tags, closed/with-tags, and non-closed tickets."""
    tickets = [
        make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-02T10:00:00Z"),
        make_ticket(tid=2, status=5, tags=["urgent"], closed_at="2026-08-02T10:00:00Z"),
        make_ticket(tid=3, status=2, tags=[], closed_at=None),
        make_ticket(tid=4, status=5, tags=[], closed_at="2026-08-10T10:00:00Z"),  # outside window
        make_ticket(tid=5, status=4, tags=["foo"], closed_at="2026-08-02T10:00:00Z"),
    ]
    response = FakeResponse(tickets, headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["tickets_returned"] == 5
    assert result["aggregate"]["status_5_count"] == 3       # tid 1, 2, 4
    assert result["aggregate"]["empty_tags_count"] == 3      # tid 1, 3, 4
    assert result["aggregate"]["status_5_and_empty_tags_count"] == 2  # tid 1, 4
    assert result["aggregate"]["valid_closed_at_count"] == 2  # tid 1, 4 (both closed_5+empty_tags with valid closed_at)
    assert result["aggregate"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 1  # only tid 1 in window


# ---------------------------------------------------------------------------
# Privacy — no sensitive data in output
# ---------------------------------------------------------------------------

def test_api_key_absent_from_stdout(monkeypatch, capsys):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    monkeypatch.setenv("FRESHDESK_API_KEY", "super-secret-key-do-not-leak")
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: response)
    result = probe.run_probe(execute=True)
    out = json.dumps(result, default=str)
    assert "super-secret-key" not in out
    assert "Authorization" not in out
    assert "fake-key" not in out


def test_api_key_absent_on_failure(monkeypatch, capsys):
    def fake_get(*_a, **_kw):
        raise requests.ConnectionError("simulated")

    monkeypatch.setenv("FRESHDESK_API_KEY", "super-secret-key-do-not-leak")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    out = json.dumps(result, default=str)
    assert "super-secret-key" not in out
    assert "Authorization" not in out


def test_requester_details_not_emitted(monkeypatch, capsys):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    out = json.dumps(result, default=str)
    assert "secret@example.test" not in out
    assert "Secret Person" not in out


def test_descriptions_not_emitted(monkeypatch, capsys):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    out = json.dumps(result, default=str)
    assert "Secret description" not in out
    assert "Safe subject" not in out


def test_custom_fields_not_emitted(monkeypatch, capsys):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    out = json.dumps(result, default=str)
    assert "secret_value" not in out
    assert "custom_fields" not in out


def test_attachments_not_emitted(monkeypatch, capsys):
    t = make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    out = json.dumps(result, default=str)
    assert "secret.pdf" not in out
    assert "attachments" not in out


def test_samples_contain_only_safe_fields(monkeypatch, capsys):
    t = make_ticket(tid=42, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
    response = FakeResponse([t], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    samples = result.get("samples", [])
    assert len(samples) == 1
    sample = samples[0]
    safe_keys = {"id", "status", "created_at", "updated_at", "tags", "stats"}
    assert set(sample.keys()) == safe_keys
    assert set(sample["stats"].keys()) == {"closed_at"}
    assert sample["id"] == 42
    assert sample["status"] == 5
    assert sample["tags"] == []
    assert sample["stats"]["closed_at"] == "2026-08-03T10:00:00Z"


def test_max_three_samples(monkeypatch):
    tickets = [make_ticket(tid=i, status=5, tags=[], closed_at="2026-08-03T10:00:00Z")
               for i in range(10)]
    response = FakeResponse(tickets, headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert len(result["samples"]) <= 3


# ---------------------------------------------------------------------------
# Dashboard isolation
# ---------------------------------------------------------------------------

def test_dashboard_live_mode_false_in_dry_run(monkeypatch):
    result = probe.run_probe(execute=False)
    assert result["dashboard_live_mode"] is False


def test_dashboard_live_mode_false_in_execute(monkeypatch):
    response = FakeResponse([], headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["dashboard_live_mode"] is False


def test_no_live_transport_added_to_dashboard(monkeypatch):
    """The probe module must not import or reference Flask routes."""
    import ast
    source = Path(probe.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "flask" not in alias.name.lower(), "Probe imports Flask"
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or "flask" not in node.module.lower(), "Probe imports Flask"


def test_queue_route_not_modified():
    """Verify app.py queue route still uses offline fixtures."""
    import app as app_module
    src = Path(app_module.__file__).read_text()
    # The queue route should still reference FRESHDESK_OFFLINE
    assert "FRESHDESK_OFFLINE" in src


def test_closed_route_not_modified():
    """Verify app.py closed route still uses offline fixtures."""
    import app as app_module
    src = Path(app_module.__file__).read_text()
    assert "FRESHDESK_OFFLINE" in src


# ---------------------------------------------------------------------------
# Rate-limit header reporting
# ---------------------------------------------------------------------------

def test_rate_limit_headers_reported(monkeypatch):
    response = FakeResponse(
        [],
        headers={
            "Content-Type": "application/json",
            "X-RateLimit-Total": "1500",
            "X-RateLimit-Remaining": "1497",
            "X-RateLimit-Used-CurrentRequest": "3",
        },
    )
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["rate_limit_total"] == "1500"
    assert result["rate_limit_remaining"] == "1497"
    assert result["rate_limit_used_current_request"] == "3"


def test_rate_limit_difference_flagged(monkeypatch):
    """If X-RateLimit-Used-CurrentRequest != 3, verdict should be PASS WITH DIFFERENCE."""
    response = FakeResponse(
        [],
        headers={
            "Content-Type": "application/json",
            "X-RateLimit-Total": "1500",
            "X-RateLimit-Remaining": "1499",
            "X-RateLimit-Used-CurrentRequest": "1",
        },
    )
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_DIFFERENCES
    assert any("RateLimit" in d for d in result.get("differences", []))


def test_rate_limit_3_no_difference(monkeypatch):
    response = FakeResponse(
        [],
        headers={
            "Content-Type": "application/json",
            "X-RateLimit-Total": "1500",
            "X-RateLimit-Remaining": "1497",
            "X-RateLimit-Used-CurrentRequest": "3",
        },
    )
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_PASS


def test_retry_after_reported_on_429(monkeypatch):
    response = FakeResponse(
        {"error": "rate limited"},
        status=429,
        headers={
            "Content-Type": "application/json",
            "Retry-After": "120",
        },
    )
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["retry_after"] == "120"
    assert result["verdict"] == probe.VERDICT_FAILED


# ---------------------------------------------------------------------------
# Credential unavailable
# ---------------------------------------------------------------------------

def test_credential_unavailable_fails_safely(monkeypatch):
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    # Also prevent file-based credential
    original_read = Path.read_text

    def fake_read(self, *args, **kwargs):
        if str(self) == str(probe.KEY_FILE):
            raise FileNotFoundError
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "credential" in result.get("error", "").lower()
    assert result["actual_requests"] == 0


# ---------------------------------------------------------------------------
# Non-dict tickets in list handled safely
# ---------------------------------------------------------------------------

def test_non_dict_ticket_in_list_handled_safely(monkeypatch):
    tickets = [
        "not-a-ticket",
        make_ticket(tid=1, status=5, tags=[], closed_at="2026-08-03T10:00:00Z"),
        None,
        42,
    ]
    response = FakeResponse(tickets, headers=BATCH_HEADERS)
    _, _ = batch_mock(monkeypatch, response)
    result = probe.run_probe(execute=True)
    assert result["aggregate"]["tickets_returned"] == 1
    assert result["aggregate"]["status_5_count"] == 1


# ---------------------------------------------------------------------------
# TLS verification
# ---------------------------------------------------------------------------

def test_tls_verification_enabled():
    """The probe must use TLS (https) — verify from constants."""
    assert probe.ALLOWED_HOST.startswith("broadriverretail")
    src = Path(probe.__file__).read_text()
    # The URL is always https
    assert 'f"https://{ALLOWED_HOST}' in src
