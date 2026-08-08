"""Offline contract tests for tools.closed_single_ticket_stats_probe.

All requests are mocked; conftest's autouse network block remains in force.

These tests verify the two-stage flow:
  Stage 1: GET /api/v2/search/tickets  (acquire one ticket ID)
  Stage 2: GET /api/v2/tickets/<id>?include=stats  (verify stats)

Hard guarantees enforced:
  - Request budget <= 2
  - Stage 2 never runs without a valid stage-1 ID
  - Stage 1 page fixed to 1
  - No retry on any stage
  - Foreign redirects rejected
  - API key / Authorization never appear in output
  - Stage-1 failure prevents stage 2
  - Invalid/missing ID prevents stage 2
  - Stage-2 failure stops
  - Dashboard remains offline
  - No write methods available
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from tools import closed_single_ticket_stats_probe as probe

SEARCH = probe.SEARCH_ENDPOINT
HOST = probe.ALLOWED_HOST
TID = 436532

SEARCH_HEADERS = {
    "Content-Type": "application/json",
    "X-RateLimit-Total": "1000",
    "X-RateLimit-Remaining": "998",
    "X-RateLimit-Used-CurrentRequest": "2",
}
STATS_HEADERS = {
    "Content-Type": "application/json",
    "X-RateLimit-Total": "1000",
    "X-RateLimit-Remaining": "997",
    "X-RateLimit-Used-CurrentRequest": "1",
}


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, url=None, history=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.url = url or f"https://{HOST}{SEARCH}"
        self.history = history or []

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def search_payload(ticket_id=TID, total=1):
    return {"total": total, "results": [{"id": ticket_id, "subject": "secret subject"}]}


def ticket(stats={"closed_at": "2026-08-03T23:48:26Z", "resolved_at": None, "first_responded_at": None}):
    return {
        "id": TID,
        "subject": "Safe subject",
        "status": 5,
        "created_at": "2026-08-03T23:47:46Z",
        "updated_at": "2026-08-03T23:48:26Z",
        "tags": [],
        "stats": stats,
        "description": "must never be reported",
        "requester": {"email": "secret@example.test"},
    }


def two_stage_mock(monkeypatch, stats_resp=None, search_resp=None):
    """Wire a mock that returns a valid search then a stats response."""
    stats_resp = stats_resp if stats_resp is not None else FakeResponse(
        ticket(), headers=STATS_HEADERS, url=f"https://{HOST}/api/v2/tickets/{TID}")
    if search_resp is None:
        search_resp = FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=f"https://{HOST}{SEARCH}")

    def fake_get(url, **kwargs):
        if SEARCH in url:
            return search_resp
        return stats_resp

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    return fake_get


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


def test_dry_run_makes_zero_requests_and_only_prints_safe_contract(monkeypatch, capsys):
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: pytest.fail("dry run attempted HTTP"))
    assert probe.main([]) == probe.EXIT_DRY_RUN
    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["dry_run"] is True
    assert parsed["actual_requests"] == 0
    assert parsed["request_budget"] == 2
    assert parsed["stage1"]["endpoint"] == SEARCH
    assert parsed["stage1"]["page"] == 1
    assert parsed["method"] == "GET"
    assert parsed["host"] == HOST
    assert "Authorization" not in output
    assert "fake-key" not in output


# ---------------------------------------------------------------------------
# Credential tests
# ---------------------------------------------------------------------------


def test_execute_requires_credential(monkeypatch):
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    monkeypatch.setattr(probe, "KEY_FILE", Path("/nonexistent/missing"))
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert result["error"] == "credential unavailable"


# ---------------------------------------------------------------------------
# Stage-1 tests (search)
# ---------------------------------------------------------------------------


def test_stage1_exact_get_with_page_1_and_no_retry(monkeypatch):
    calls = []
    s1_url = f"https://{HOST}{SEARCH}"

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    probe.run_probe(execute=True)
    assert calls[0][0] == s1_url
    assert calls[0][1]["params"]["page"] == 1
    assert "query" in calls[0][1]["params"]
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == probe.REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_stage1_error_prevents_stage2(monkeypatch, status):
    calls = []
    s1_url = f"https://{HOST}{SEARCH}"
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: (
        calls.append(1) or FakeResponse({}, status=status, url=s1_url)
    ))
    result = probe.run_probe(execute=True)
    assert len(calls) == 1
    assert result["actual_requests"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED
    assert result["stage1"]["error"] == f"HTTP {status}"


def test_stage1_timeout_prevents_stage2(monkeypatch):
    calls = []
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")

    def fail(*_a, **_kw):
        calls.append(1)
        raise requests.Timeout("timeout")

    monkeypatch.setattr(requests, "get", fail)
    result = probe.run_probe(execute=True)
    assert len(calls) == 1
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "Timeout" in result["stage1"]["error"]


def test_stage1_foreign_redirect_rejected(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: FakeResponse(
        {}, status=302, url="https://evil.example/path"
    ))
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    # Foreign-host redirect is caught by hostname guard before redirect guard.
    assert result["stage1"]["error"] in ("unexpected final hostname", "redirect rejected")


def test_stage1_same_host_redirect_rejected(monkeypatch):
    """A same-host 302 is still rejected (allow_redirects=False + status check)."""
    s1_url = f"https://{HOST}{SEARCH}"
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: FakeResponse(
        {}, status=302, url=s1_url
    ))
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert result["stage1"]["error"] == "redirect rejected"


@pytest.mark.parametrize(
    "payload,reason",
    [
        (ValueError("bad JSON"), "invalid JSON"),
        ([], "invalid top-level JSON"),
        ({"total": 1}, "no valid numeric ticket ID"),
        ({"total": 1, "results": []}, "no valid numeric ticket ID"),
        ({"total": 1, "results": [{"id": "not-int"}]}, "no valid numeric ticket ID"),
        ({"total": 1, "results": [{"id": -1}]}, "no valid numeric ticket ID"),
        ({"total": 1, "results": [{"id": True}]}, "no valid numeric ticket ID"),
    ],
)
def test_stage1_malformed_or_invalid_results_prevent_stage2(monkeypatch, payload, reason):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: FakeResponse(payload))
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert reason in result["stage1"].get("error", "")


# ---------------------------------------------------------------------------
# Stage-2 tests (stats)
# ---------------------------------------------------------------------------


def test_stage2_exact_stats_get_for_selected_id_only(monkeypatch):
    calls = []
    stats_url = f"https://{HOST}/api/v2/tickets/{TID}"
    s1_url = f"https://{HOST}{SEARCH}"

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse(ticket(), headers=STATS_HEADERS, url=stats_url)

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    probe.run_probe(execute=True)
    assert len(calls) == 2
    assert calls[1][0] == stats_url
    assert calls[1][1]["params"] == {"include": "stats"}
    assert calls[1][1]["allow_redirects"] is False


def test_stage2_uses_exactly_the_id_from_stage1(monkeypatch):
    calls = []
    custom_id = 999333
    stats_url = f"https://{HOST}/api/v2/tickets/{custom_id}"
    s1_url = f"https://{HOST}{SEARCH}"

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if SEARCH in url:
            return FakeResponse(search_payload(ticket_id=custom_id), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse(ticket(), headers=STATS_HEADERS, url=stats_url)

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert result["stage1"]["selected_ticket_id"] == custom_id
    assert result["stage2"].get("selected_ticket_id") == custom_id
    assert calls[1][0] == stats_url


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_stage2_error_stops_safely(monkeypatch, status):
    calls = []
    s1_url = f"https://{HOST}{SEARCH}"
    stats_url = f"https://{HOST}/api/v2/tickets/{TID}"

    def fake_get(url, **kwargs):
        calls.append(1)
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse({}, status=status, url=stats_url)

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert len(calls) == 2
    assert result["actual_requests"] == 2
    assert result["verdict"] == probe.VERDICT_FAILED
    assert result["stage2"]["error"] == f"HTTP {status}"


def test_stage2_timeout_stops_safely(monkeypatch):
    count = [0]
    s1_url = f"https://{HOST}{SEARCH}"

    def fake_get(url, **kwargs):
        count[0] += 1
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        raise requests.Timeout("timeout")

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert count[0] == 2
    assert result["verdict"] == probe.VERDICT_FAILED
    assert "Timeout" in result["stage2"]["error"]


def test_stage2_foreign_redirect_rejected(monkeypatch):
    s1_url = f"https://{HOST}{SEARCH}"
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")

    def fake_get(url, **kwargs):
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse({}, status=302, url="https://evil.example/stats")

    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert result["stage2"]["error"] in ("unexpected final hostname", "redirect rejected")


# ---------------------------------------------------------------------------
# Budget / retry tests
# ---------------------------------------------------------------------------


def test_total_requests_never_exceeds_budget_of_2(monkeypatch):
    calls = []
    s1_url = f"https://{HOST}{SEARCH}"
    stats_url = f"https://{HOST}/api/v2/tickets/{TID}"

    def fake_get(url, **kwargs):
        calls.append(1)
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse(ticket(), headers=STATS_HEADERS, url=stats_url)

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert len(calls) == 2
    assert result["actual_requests"] == 2
    assert result["request_budget"] == 2


# ---------------------------------------------------------------------------
# Stats validation tests
# ---------------------------------------------------------------------------


def _two_stage(monkeypatch, stats_payload=None, ticket_override=None):
    """Shared: mock search→stats and call run_probe(execute=True)."""
    s1_url = f"https://{HOST}{SEARCH}"
    stats_url = f"https://{HOST}/api/v2/tickets/{TID}"
    t = ticket_override if ticket_override is not None else ticket()
    if stats_payload is not None:
        t["stats"] = stats_payload

    def fake_get(url, **kwargs):
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse(t, headers=STATS_HEADERS, url=stats_url)

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    return probe.run_probe(execute=True)


def test_stats_closed_at_present_offset_aware_is_pass(monkeypatch):
    result = _two_stage(monkeypatch)
    assert result["verdict"] == probe.VERDICT_PASS
    assert result["stats"]["closed_at"]["exists"] is True
    assert result["stats"]["closed_at"]["value"] == "2026-08-03T23:48:26Z"
    assert result["stats"]["closed_at"]["parse_dt_accepted"] is True
    assert result["stats"]["closed_at"]["timezone_aware"] is True


def test_stats_closed_at_null_is_difference(monkeypatch):
    result = _two_stage(monkeypatch, stats_payload={"closed_at": None})
    assert result["stats"]["closed_at"]["exists"] is True
    assert result["stats"]["closed_at"]["value"] is None
    assert result["verdict"] == probe.VERDICT_DIFFERENCES


def test_stats_closed_at_malformed_is_difference(monkeypatch):
    result = _two_stage(monkeypatch, stats_payload={"closed_at": "not-a-date"})
    assert result["stats"]["closed_at"]["parse_dt_accepted"] is False
    assert result["verdict"] == probe.VERDICT_DIFFERENCES


def test_stats_missing_is_difference(monkeypatch):
    t = ticket()
    del t["stats"]
    result = _two_stage(monkeypatch, ticket_override=t)
    assert result["stats"]["exists"] is False
    assert result["verdict"] == probe.VERDICT_DIFFERENCES


@pytest.mark.parametrize(
    "payload,reason",
    [
        (ValueError("bad JSON"), "invalid JSON"),
        ([], "invalid top-level JSON"),
    ],
)
def test_stage2_malformed_response_stops_safely(monkeypatch, payload, reason):
    s1_url = f"https://{HOST}{SEARCH}"

    def fake_get(url, **kwargs):
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse(payload)

    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    assert result["verdict"] == probe.VERDICT_FAILED
    assert reason in result["stage2"]["error"]


# ---------------------------------------------------------------------------
# Credential / leakage tests
# ---------------------------------------------------------------------------


def test_output_never_leaks_key_authorization_or_sensitive_ticket_data(monkeypatch, capsys):
    s1_url = f"https://{HOST}{SEARCH}"
    stats_url = f"https://{HOST}/api/v2/tickets/{TID}"

    def fake_get(url, **kwargs):
        if SEARCH in url:
            return FakeResponse(search_payload(), headers=SEARCH_HEADERS, url=s1_url)
        return FakeResponse(ticket(), headers=STATS_HEADERS, url=stats_url)

    monkeypatch.setenv("FRESHDESK_API_KEY", "super-secret-key-for-tests")
    monkeypatch.setattr(requests, "get", fake_get)
    assert probe.main(["--execute"]) == probe.EXIT_PASS
    output = capsys.readouterr().out
    assert "super-secret-key-for-tests" not in output
    assert "Authorization" not in output
    assert "must never be reported" not in output
    assert "secret@example.test" not in output
    parsed = json.loads(output)
    assert set(parsed["ticket"]) == {"id", "status", "created_at", "updated_at", "tags"}


# ---------------------------------------------------------------------------
# Dashboard isolation tests
# ---------------------------------------------------------------------------


def test_dashboard_routes_remain_offline_and_write_methods_are_absent():
    source = Path("app.py").read_text()
    start = source.index('@app.route("/closed")')
    end = source.index('@app.route("/closed/api/review", methods=["POST"])', start)
    closed_route = source[start:end]
    assert "requests.get(" not in closed_route
    assert "fd_auth(" not in closed_route
    tool_source = Path("tools/closed_single_ticket_stats_probe.py").read_text()
    assert "requests.post(" not in tool_source
    assert "requests.put(" not in tool_source
    assert "requests.patch(" not in tool_source
    assert "requests.delete(" not in tool_source
    assert "requests.request(" not in tool_source
    run_start = tool_source.index("def run_probe")
    run_end = tool_source.index("def _print_json")
    run_body = tool_source[run_start:run_end]
    # Strip comments to avoid false positives on "for " appearing in prose.
    import re
    code_only = re.sub(r"#.*", "", run_body)
    assert "for " not in code_only
    assert "while " not in code_only
    assert "retry" not in code_only.lower()


def test_numeric_ticket_id_and_exact_endpoint_guard():
    assert probe.allowed_endpoint(436532) == "/api/v2/tickets/436532"
    with pytest.raises(ValueError):
        probe.allowed_endpoint("436532")
    with pytest.raises(ValueError):
        probe.allowed_endpoint(-1)
    with pytest.raises(ValueError):
        probe.allowed_endpoint(True)


def test_request_budget_is_exactly_2():
    assert probe.REQUEST_BUDGET == 2


def test_stage1_page_is_exactly_1():
    assert probe.FORCED_PAGE == 1
