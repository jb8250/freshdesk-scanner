"""Offline contract tests for tools.closed_live_probe.

Every request surface is mocked; conftest.py's autouse network blocker remains
active, so this test module can never contact Freshdesk.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests

from tools import closed_live_probe as probe


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, url=None, history=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.url = url or f"https://{probe.ALLOWED_HOST}{probe.ALLOWED_ENDPOINT}"
        self.history = history or []

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@pytest.fixture
def args():
    return ["--start", "2026-08-01", "--end", "2026-08-03"]


def test_dry_run_is_zero_network_and_contains_only_safe_contract(monkeypatch, capsys, args):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry run attempted HTTP")

    monkeypatch.setattr(requests, "get", forbidden)
    assert probe.main(args) == probe.EXIT_DRY_RUN
    output = capsys.readouterr().out
    assert "Method: GET" in output
    assert "Host: broadriverretail-help.freshdesk.com" in output
    assert "Endpoint: /api/v2/search/tickets" in output
    assert "Page: 1" in output
    assert "Request budget: 1" in output
    assert "Closed status: 5" in output
    assert "Missing tags: ON" in output
    assert "Date range: 2026-08-01 to 2026-08-03" in output
    assert "Authorization" not in output
    assert "fake-key" not in output


def test_execute_requires_available_credential(monkeypatch, capsys, args, tmp_path):
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    monkeypatch.setattr(probe, "KEY_FILE", tmp_path / "missing")
    assert probe.main([*args, "--execute"]) == probe.EXIT_CREDENTIAL
    assert "credential unavailable" in capsys.readouterr().out.lower()


def test_execute_uses_one_get_to_exact_host_endpoint_page_one_no_retry(monkeypatch, args):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"total": 1, "results": [ticket()]})

    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(date(2026, 8, 1), date(2026, 8, 3), execute=True)
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == f"https://{probe.ALLOWED_HOST}{probe.ALLOWED_ENDPOINT}"
    assert kwargs["params"]["page"] == 1
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == probe.REQUEST_TIMEOUT_SECONDS
    assert result["actual_requests"] == 1
    assert result["method"] == "GET"
    assert result["final_hostname"] == probe.ALLOWED_HOST


def test_constants_lock_down_method_host_endpoint_budget_page_and_timeout():
    assert probe.ALLOWED_METHOD == "GET"
    assert probe.ALLOWED_HOST == "broadriverretail-help.freshdesk.com"
    assert probe.ALLOWED_ENDPOINT == "/api/v2/search/tickets"
    assert probe.REQUEST_BUDGET == 1
    assert probe.FORCED_PAGE == 1
    assert 15 <= probe.REQUEST_TIMEOUT_SECONDS <= 30


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_http_errors_stop_after_one_request(monkeypatch, status):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    calls = []
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: (calls.append(1) or FakeResponse({}, status=status)))
    result = probe.run_probe(date(2026, 8, 1), date(2026, 8, 3), execute=True)
    assert result["actual_requests"] == 1
    assert result["http_status"] == status
    assert result["verdict"] == probe.VERDICT_FAILED
    assert len(calls) == 1


def test_429_records_retry_after_without_waiting_or_retry(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    calls = []
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: (calls.append(1) or FakeResponse({}, status=429, headers={"Retry-After": "47"})))
    result = probe.run_probe(date(2026, 8, 1), date(2026, 8, 3), execute=True)
    assert result["retry_after"] == "47"
    assert len(calls) == 1


def test_timeout_stops_without_retry(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    calls = []
    def fail(*_a, **_kw):
        calls.append(1)
        raise requests.Timeout("timeout")
    monkeypatch.setattr(requests, "get", fail)
    result = probe.run_probe(date(2026, 8, 1), date(2026, 8, 3), execute=True)
    assert result["actual_requests"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED
    assert len(calls) == 1


def test_foreign_redirect_is_rejected_without_following(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    response = FakeResponse({}, status=302, url="https://evil.example/path")
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: response)
    result = probe.run_probe(date(2026, 8, 1), date(2026, 8, 3), execute=True)
    assert result["actual_requests"] == 1
    assert result["redirect_count"] == 0
    assert result["final_hostname"] == "evil.example"
    assert result["verdict"] == probe.VERDICT_FAILED


def test_malformed_response_stops_safely(monkeypatch):
    cases = [
        (ValueError("bad json"), "invalid JSON"),
        ({"results": []}, "missing total"),
        ({"total": "1", "results": []}, "invalid total"),
        ({"total": 1}, "missing results"),
        ({"total": 1, "results": {}}, "invalid results"),
        ({"total": 31, "results": [ticket(i) for i in range(31)]}, "more than 30"),
    ]
    for payload, reason in cases:
        _assert_malformed_response_stops_safely(monkeypatch, payload, reason)


def _assert_malformed_response_stops_safely(monkeypatch, payload, reason):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: FakeResponse(payload))
    result = probe.run_probe(date(2026, 8, 1), date(2026, 8, 3), execute=True)
    assert result["actual_requests"] == 1
    assert result["verdict"] == probe.VERDICT_FAILED
    assert reason in result["error"]


def test_valid_200_sanitizes_ticket_fields_and_hides_key(monkeypatch, capsys, args):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    payload = {"total": 1, "results": [ticket()]}
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: FakeResponse(payload, headers={"Content-Type": "application/json", "X-RateLimit-Remaining": "99"}))
    assert probe.main([*args, "--execute"]) == probe.EXIT_PASS
    output = capsys.readouterr().out
    assert "fake-key-for-tests" not in output
    assert "Authorization" not in output
    assert "requester" not in output
    parsed = json.loads(output)
    assert parsed["verdict"] == probe.VERDICT_PASS
    assert parsed["tickets"] == [{"id": 101, "subject": "Safe subject", "status": 5, "closed_at": "2026-08-02T01:00:00Z", "updated_at": "2026-08-02T02:00:00Z", "created_at": "2026-07-01T01:00:00Z", "tags": []}]


def test_missing_closed_at_is_a_contract_difference_not_a_second_request(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    payload = {"total": 1, "results": [{key: value for key, value in ticket().items() if key != "closed_at"}]}
    calls = []

    def fake_get(*_args, **_kwargs):
        calls.append(1)
        return FakeResponse(payload)

    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(date(2026, 8, 1), date(2026, 8, 3), execute=True)
    assert len(calls) == 1
    assert result["field_compatibility"]["closed_at"]["status"] == "ABSENT"
    assert result["verdict"] == probe.VERDICT_DIFFERENCES


def test_dashboard_routes_remain_offline_source_only():
    source = Path("app.py").read_text()
    route_start = source.index('@app.route("/closed")')
    route_end = source.index('@app.route("/closed/api/review", methods=["POST"])', route_start)
    closed_route = source[route_start:route_end]
    # The /closed page may call the synthetic fixture retrieval function, but
    # must never directly create a network request or choose live mode.
    assert "requests.get(" not in closed_route
    assert "load_api_key(" not in closed_route
    assert "retrieve_closed_tickets(" in closed_route
    assert "closed_live_result(config)" in closed_route
    assert "fd_auth(" not in closed_route


def ticket(ticket_id=101):
    return {
        "id": ticket_id,
        "subject": "Safe subject",
        "status": 5,
        "closed_at": "2026-08-02T01:00:00Z",
        "updated_at": "2026-08-02T02:00:00Z",
        "created_at": "2026-07-01T01:00:00Z",
        "tags": [],
        "requester": {"email": "must-not-appear@example.test"},
        "description": "must-not-appear",
    }
