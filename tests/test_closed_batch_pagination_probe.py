"""Offline contract tests for tools.closed_batch_pagination_probe.

All requests are mocked; conftest's autouse network block remains in force.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from tools import closed_batch_pagination_probe as probe

HOST = probe.ALLOWED_HOST
ENDPOINT = probe.ENDPOINT

LINK_NEXT_2 = f'<https://{HOST}{ENDPOINT}?page=2>; rel="next"'


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None, link=None):
        self._payload = payload if payload is not None else []
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        if link:
            self.headers["Link"] = link

    def json(self):
        return self._payload


def make_ticket(tid=100, status=5, tags=None, closed_at="2026-08-03T23:47:46Z",
                updated_at="2026-08-03T12:00:00Z", tags_missing=False,
                stats=None, include_sensitive=True, created_at=None):
    if stats is None and closed_at is not None:
        stats = {"closed_at": closed_at, "resolved_at": None, "first_responded_at": None}
    elif stats is None:
        stats = {}
    if tags_missing:
        t_tags = None
    else:
        t_tags = tags if tags is not None else []
    t = {
        "id": tid,
        "status": status,
        "created_at": created_at or "2026-08-03T10:00:00Z",
        "updated_at": updated_at,
        "stats": stats,
    }
    if not tags_missing:
        t["tags"] = t_tags
    if include_sensitive:
        t["subject"] = "Secret subject must never appear"
        t["description"] = "Secret description must never appear"
        t["requester"] = {"email": "secret@example.test", "name": "Secret Person"}
        t["custom_fields"] = {"secret_field": "secret_value"}
        t["attachments"] = [{"name": "secret.pdf"}]
    return t


def _script_pages(script, monkeypatch):
    """Run probe with scripted FakeResponse list.

    script: list of dicts with keys: payload (list), status (int), headers (dict),
            link (str or None).
    Returns (result_dict, calls_list).
    """
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        page_num = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
        idx = page_num - 1
        if idx < 0 or idx >= len(script):
            raise AssertionError(f"unexpected page {page_num}")
        entry = script[idx]
        return FakeResponse(
            payload=entry.get("payload", []),
            status=entry.get("status", 200),
            headers=entry.get("headers"),
            link=entry.get("link"),
        )

    monkeypatch.setattr(requests, "get", fake_get)
    result = probe.run_probe(execute=True)
    return result, calls


# ---------------------------------------------------------------------------
# Pagination guardrails
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_zero_http(self, monkeypatch):
        calls = []
        monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append(url))
        result = probe.run_probe(execute=False)
        assert calls == []
        assert result["requests_made"] == 0

    def test_execute_required_for_live(self, monkeypatch):
        calls = []
        monkeypatch.setattr(requests, "get", lambda url, **kw: calls.append(url))
        probe.run_probe(execute=False)
        assert calls == []


class TestURLShape:
    def test_first_page_url_shape(self, monkeypatch):
        _, calls = _script_pages([{"payload": [make_ticket(tid=1, status=4)]}], monkeypatch)
        url = calls[0]["url"]
        assert url.startswith(f"https://{HOST}{ENDPOINT}?")
        assert "include=stats" in url
        assert "per_page=100" in url
        assert "page=1" in url
        assert "updated_since=2026-08-01T00:00:00Z" in url
        assert "order_by=status" in url
        assert "order_type=desc" in url

    def test_pages_sequential_from_1(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(100)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=200, status=4)]},
        ]
        _, calls = _script_pages(script, monkeypatch)
        assert len(calls) == 2
        assert "page=1" in calls[0]["url"]
        assert "page=2" in calls[1]["url"]

    def test_no_skipped_pages(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(100)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=200, status=4)]},
        ]
        _, calls = _script_pages(script, monkeypatch)
        page_nums = [parse_qs(urlparse(c["url"]).query)["page"][0] for c in calls]
        assert page_nums == ["1", "2"]

    def test_max_pages_enforced(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=p * 100 + i, status=2) for i in range(100)],
             "link": f'<https://{HOST}{ENDPOINT}?page={p+2}>; rel="next"'}
            for p in range(1, 17)
        ]
        result, calls = _script_pages(script, monkeypatch)
        assert len(calls) <= 15
        assert result["requests_made"] <= 15

    def test_no_retry_on_error(self, monkeypatch):
        result, calls = _script_pages([{"status": 500}], monkeypatch)
        assert result["requests_made"] == 1
        assert len(calls) == 1
        assert "HTTP 500" in result["stop_reason"]

    def test_no_duplicate_page_fetches(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(100)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=200, status=4)]},
        ]
        _, calls = _script_pages(script, monkeypatch)
        urls = [c["url"] for c in calls]
        assert len(urls) == len(set(urls))

    def test_only_list_endpoint(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        for c in calls:
            assert c["url"].startswith(f"https://{HOST}{ENDPOINT}")

    def test_fixed_include(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        assert all("include=stats" in c["url"] for c in calls)

    def test_fixed_per_page(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        assert all("per_page=100" in c["url"] for c in calls)

    def test_fixed_updated_since(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        assert all("updated_since=2026-08-01T00:00:00Z" in c["url"] for c in calls)

    def test_fixed_order_by(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        assert all("order_by=status" in c["url"] for c in calls)

    def test_fixed_order_type(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        assert all("order_type=desc" in c["url"] for c in calls)

    def test_foreign_redirect_rejected(self, monkeypatch):
        redirect = FakeResponse(status=302, headers={"Location": "https://evil.example.com/api"})
        monkeypatch.setattr(requests, "get", lambda url, **kw: redirect)
        result = probe.run_probe(execute=True)
        assert "foreign redirect" in result["stop_reason"]
        assert result["requests_made"] == 1


# ---------------------------------------------------------------------------
# Sort / state machine
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_five_then_five_then_below(self, monkeypatch):
        payload = [
            make_ticket(tid=1, status=5),
            make_ticket(tid=2, status=5),
            make_ticket(tid=3, status=4),
        ]
        result, _ = _script_pages([{"payload": payload}], monkeypatch)
        assert "closed status block exhausted" in result["stop_reason"]
        assert result["final_state"] == "AFTER_CLOSED_BLOCK"
        assert result["pages"][0]["state_before"] == "BEFORE_CLOSED_BLOCK"

    def test_multiple_pages_before_closed(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=6) for i in range(100)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=200 + i, status=5) for i in range(100)],
             "link": f'<https://{HOST}{ENDPOINT}?page=3>; rel="next"'},
            {"payload": [make_ticket(tid=300 + i, status=4) for i in range(10)]},
        ]
        result, _ = _script_pages(script, monkeypatch)
        assert result["pages"][0]["state_before"] == "BEFORE_CLOSED_BLOCK"
        assert result["pages"][0]["state_after"] == "BEFORE_CLOSED_BLOCK"
        assert result["pages"][1]["state_before"] == "BEFORE_CLOSED_BLOCK"
        assert result["pages"][1]["state_after"] == "IN_CLOSED_BLOCK"
        assert result["pages"][2]["state_before"] == "IN_CLOSED_BLOCK"
        assert result["pages"][2]["state_after"] == "AFTER_CLOSED_BLOCK"

    def test_closed_block_across_pages(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(100)],
             "link": f'<https://{HOST}{ENDPOINT}?page={p+2}>; rel="next"'}
            for p in range(10)
        ]
        script.append({"payload": [make_ticket(tid=999, status=4)]})
        result, calls = _script_pages(script, monkeypatch)
        assert result["final_state"] == "AFTER_CLOSED_BLOCK"
        assert result["requests_made"] == 11

    def test_below_five_before_five_stops(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=4)]}], monkeypatch)
        assert result["final_state"] == "AFTER_CLOSED_BLOCK"
        assert "below 5" in result["stop_reason"] or "closed status block exhausted" in result["stop_reason"]

    def test_sort_violation_detected(self, monkeypatch):
        payload = [
            make_ticket(tid=1, status=5),
            make_ticket(tid=2, status=6),
        ]
        result, _ = _script_pages([{"payload": payload}], monkeypatch)
        assert result["cumulative"]["sort_violations"] == 1
        assert result["verdict"] == probe.VERDICT_SORT_FAILED

    def test_cross_page_sort_violation_detected(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(100)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=200 + i, status=6) for i in range(100)]},
        ]
        result, _ = _script_pages(script, monkeypatch)
        assert result["cumulative"]["sort_violations"] == 1
        assert result["verdict"] == probe.VERDICT_SORT_FAILED

    def test_no_page_after_after_closed_block(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(100)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=200 + i, status=4) for i in range(100)],
             "link": f'<https://{HOST}{ENDPOINT}?page=3>; rel="next"'},
            {"payload": [make_ticket(tid=300 + i, status=4) for i in range(100)]},
        ]
        result, calls = _script_pages(script, monkeypatch)
        assert result["requests_made"] == 2
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Local filtering
# ---------------------------------------------------------------------------

class TestLocalFiltering:
    def test_status_5_only(self, monkeypatch):
        payload = [
            make_ticket(tid=1, status=5),
            make_ticket(tid=2, status=6),
            make_ticket(tid=3, status=4),
        ]
        result, _ = _script_pages([{"payload": payload}], monkeypatch)
        assert result["cumulative"]["status_5_count"] == 1

    def test_tags_empty_list(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, tags=[])]}], monkeypatch)
        assert result["cumulative"]["closed_no_tags_count"] == 1

    def test_nonempty_tags_excluded(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, tags=["a"])]}], monkeypatch)
        assert result["cumulative"]["closed_no_tags_count"] == 0
        assert result["cumulative"]["closed_nonempty_tags_count"] == 1

    def test_malformed_tags_safe(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, tags=["x"], tags_missing=True)]}], monkeypatch)
        assert result["cumulative"]["closed_missing_or_bad_tags_count"] == 1
        assert result["cumulative"]["closed_no_tags_count"] == 0

    def test_valid_closed_at(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at="2026-08-02T12:00:00Z")]}], monkeypatch)
        assert result["cumulative"]["closed_valid_closed_at_count"] == 1

    def test_missing_closed_at_safe(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, stats={})]}], monkeypatch)
        assert result["cumulative"]["closed_invalid_or_missing_closed_at_count"] == 1
        assert result["cumulative"]["closed_valid_closed_at_count"] == 0

    def test_null_closed_at_safe(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at=None)]}], monkeypatch)
        assert result["cumulative"]["closed_invalid_or_missing_closed_at_count"] == 1

    def test_malformed_closed_at_safe(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, stats={"closed_at": "not-a-date"})]}], monkeypatch)
        assert result["cumulative"]["closed_valid_closed_at_count"] == 0
        assert result["cumulative"]["closed_invalid_or_missing_closed_at_count"] == 1

    def test_start_inclusive(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at="2026-08-01T00:00:00Z")]}], monkeypatch)
        assert result["cumulative"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 1

    def test_end_exclusive(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at="2026-08-04T00:00:00Z")]}], monkeypatch)
        assert result["cumulative"]["closed_no_tags_in_aug_1_through_aug_3_count"] == 0

    def test_dedup_across_pages(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=1, status=5)], "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=1, status=5)]},
        ]
        result, _ = _script_pages(script, monkeypatch)
        assert result["cumulative"]["duplicate_ticket_ids"] == 1

    def test_correct_cumulative_totals(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(10)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=10 + i, status=5) for i in range(10)]},
        ]
        result, _ = _script_pages(script, monkeypatch)
        assert result["cumulative"]["status_5_count"] == 20
        assert result["cumulative"]["closed_valid_closed_at_count"] == 20
        assert result["cumulative"]["closed_no_tags_count"] == 20


# ---------------------------------------------------------------------------
# Timestamp relationship
# ---------------------------------------------------------------------------

class TestTimestampRelationship:
    def test_updated_at_equals_closed_at(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at="2026-08-02T12:00:00Z", updated_at="2026-08-02T12:00:00Z")]}], monkeypatch)
        assert result["cumulative"]["updated_at_gte_closed_at_count"] == 1
        assert result["cumulative"]["updated_at_lt_closed_at_count"] == 0

    def test_updated_at_gt_closed_at(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at="2026-08-02T12:00:00Z", updated_at="2026-08-02T12:30:00Z")]}], monkeypatch)
        assert result["cumulative"]["updated_at_gte_closed_at_count"] == 1
        assert result["cumulative"]["updated_at_lt_closed_at_count"] == 0

    def test_updated_at_lt_closed_at_flagged(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at="2026-08-02T12:00:00Z", updated_at="2026-08-02T11:00:00Z")]}], monkeypatch)
        assert result["cumulative"]["updated_at_lt_closed_at_count"] == 1
        assert result["cumulative"]["updated_at_gte_closed_at_count"] == 0

    def test_malformed_timestamps_safe(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, closed_at="2026-08-02T12:00:00Z", updated_at="garbage")]}], monkeypatch)
        assert result["cumulative"]["updated_at_relationship_unknown_count"] == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
    def test_http_errors(self, monkeypatch, status):
        result, calls = _script_pages([{"status": status}], monkeypatch)
        assert result["requests_made"] == 1
        assert f"HTTP {status}" in result["stop_reason"]
        assert len(calls) == 1

    def test_timeout_prevented(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda url, **kw: (_ for _ in ()).throw(requests.Timeout()))
        result = probe.run_probe(execute=True)
        assert "transport error" in result["stop_reason"]

    def test_malformed_json(self, monkeypatch):
        class BadJson:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            def json(self):
                raise ValueError("not json")
        monkeypatch.setattr(requests, "get", lambda url, **kw: BadJson())
        result = probe.run_probe(execute=True)
        assert "malformed JSON" in result["stop_reason"]

    def test_non_list_json(self, monkeypatch):
        result, calls = _script_pages([{"payload": {"error": "bad"}}], monkeypatch)
        assert "non-list JSON" in result["stop_reason"]
        assert len(calls) == 1

    def test_100_records_per_page(self, monkeypatch):
        payload = [make_ticket(tid=i, status=5) for i in range(100)]
        result, _ = _script_pages([{"payload": payload}], monkeypatch)
        assert result["cumulative"]["tickets_returned"] == 100

    def test_request_failure_prevents_next_page(self, monkeypatch):
        def fake_get(url, **kwargs):
            page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
            if page == 1:
                return FakeResponse(
                    payload=[make_ticket(tid=1, status=5)],
                    headers={"X-RateLimit-Remaining": "190"},
                    link=LINK_NEXT_2,
                )
            raise requests.ConnectionError()
        monkeypatch.setattr(requests, "get", fake_get)
        result = probe.run_probe(execute=True)
        assert result["requests_made"] == 1
        assert "transport error" in result["stop_reason"]

    def test_no_retry_on_429(self, monkeypatch):
        result, calls = _script_pages([{"status": 429}], monkeypatch)
        assert result["requests_made"] == 1
        assert len(calls) == 1
        assert "HTTP 429" in result["stop_reason"]


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

class TestRateLimit:
    def test_rate_limit_floor_stops_next_request(self, monkeypatch):
        def fake_get(url, **kwargs):
            page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
            if page == 1:
                return FakeResponse(
                    payload=[make_ticket(tid=1, status=5)],
                    headers={"X-RateLimit-Remaining": "40"},
                    link=LINK_NEXT_2,
                )
            raise AssertionError("should not request page 2")
        monkeypatch.setattr(requests, "get", fake_get)
        result = probe.run_probe(execute=True)
        assert result["requests_made"] == 1
        assert "rate-limit safety floor" in result["stop_reason"]

    def test_rate_limit_sum(self, monkeypatch):
        result, _ = _script_pages([{
            "payload": [make_ticket(tid=1, status=5)],
            "headers": {"X-RateLimit-Used-CurrentRequest": "2"},
        }], monkeypatch)
        assert result["reported_units_used_sum"] == 2


# ---------------------------------------------------------------------------
# Link behavior
# ---------------------------------------------------------------------------

class TestLinkBehavior:
    def test_no_next_link_stops(self, monkeypatch):
        result, calls = _script_pages([{
            "payload": [make_ticket(tid=1, status=5)],
        }], monkeypatch)
        assert result["requests_made"] == 1
        assert "no next-page Link" in result["stop_reason"]

    def test_stops_regardless_of_link_when_block_done(self, monkeypatch):
        script = [
            {"payload": [make_ticket(tid=i, status=5) for i in range(100)],
             "link": LINK_NEXT_2},
            {"payload": [make_ticket(tid=200 + i, status=4) for i in range(100)],
             "link": f'<https://{HOST}{ENDPOINT}?page=3>; rel="next"'},
        ]
        result, calls = _script_pages(script, monkeypatch)
        assert result["requests_made"] == 2
        assert "closed status block exhausted" in result["stop_reason"]


# ---------------------------------------------------------------------------
# Privacy / safety
# ---------------------------------------------------------------------------

class TestPrivacy:
    def test_credential_absent_from_output(self, monkeypatch, capsys):
        _script_pages([{"payload": [make_ticket(tid=1, status=5)]}], monkeypatch)
        out = capsys.readouterr().out
        assert "Basic" not in out
        assert "Authorization" not in out

    def test_no_sensitive_fields_in_samples(self, monkeypatch):
        result, _ = _script_pages([{"payload": [make_ticket(tid=1, status=5, include_sensitive=True)]}], monkeypatch)
        sample_json = json.dumps(result.get("samples", []))
        assert "Secret subject" not in sample_json
        assert "Secret description" not in sample_json
        assert "secret@example.test" not in sample_json
        assert "secret_value" not in sample_json

    def test_no_write_methods(self):
        for method in ("post", "put", "patch", "delete"):
            assert not hasattr(probe, method)

    def test_dashboard_stays_offline(self):
        import sys
        assert "flask" not in sys.modules or "app" not in dir(probe)

    def test_no_search_endpoint(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        for c in calls:
            assert "/api/v2/search" not in c["url"]

    def test_no_view_ticket_endpoint(self, monkeypatch):
        _, calls = _script_pages([{"payload": []}], monkeypatch)
        for c in calls:
            # No /api/v2/tickets/<number> pattern
            assert not c["url"].rstrip().endswith(tuple(str(i) for i in range(10)))

    def test_max_three_samples(self, monkeypatch):
        payload = [make_ticket(tid=i, status=5) for i in range(10)]
        result, _ = _script_pages([{"payload": payload}], monkeypatch)
        assert len(result.get("samples", [])) <= 3
