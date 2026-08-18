#!/usr/bin/env python3
"""One-request, read-only Freshdesk closed-ticket contract probe.

Default operation is dry-run.  ``--execute`` is the sole path that may issue a
network request, and its guard rails admit only one GET to the fixed search
endpoint.  This module intentionally does not integrate with Flask routes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

# Permit direct `python tools/closed_live_probe.py ...` execution while keeping
# imports deterministic when the module is tested as `tools.closed_live_probe`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests

from app import CLOSED_STATUS, closed_query_string

ALLOWED_METHOD = "GET"
ALLOWED_HOST = "broadriverretail-help.freshdesk.com"
ALLOWED_ENDPOINT = "/api/v2/search/tickets"
FORCED_PAGE = 1
REQUEST_BUDGET = 1
REQUEST_TIMEOUT_SECONDS = 20
KEY_FILE = Path.home() / ".config" / "furtouch" / "freshdesk_api_key"

VERDICT_PASS = "LIVE PROBE PASS — CONTRACT COMPATIBLE"
VERDICT_DIFFERENCES = "LIVE PROBE PASS WITH DIFFERENCES — REVIEW BEFORE DASHBOARD INTEGRATION"
VERDICT_FAILED = "LIVE PROBE FAILED SAFELY — NO DASHBOARD INTEGRATION"
EXIT_PASS = 0
EXIT_DRY_RUN = 0
EXIT_CREDENTIAL = 2
EXIT_FAILED = 3


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _credential_for_single_request() -> tuple[str, str] | None:
    """Read a credential only immediately before the explicitly requested GET."""
    value = os.environ.get("FRESHDESK_API_KEY", "")
    if value:
        return value, "environment"
    try:
        value = KEY_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return (value, "external file") if value else None


def _safe_headers(headers) -> dict[str, str | None]:
    lower = {str(key).lower(): str(value) for key, value in headers.items()}
    return {
        "content_type": lower.get("content-type"),
        "api_version": lower.get("x-api-version"),
        "rate_limit_total": lower.get("x-ratelimit-total"),
        "rate_limit_remaining": lower.get("x-ratelimit-remaining"),
        "rate_limit_used_current_request": lower.get("x-ratelimit-used-currentrequest"),
        "retry_after": lower.get("retry-after"),
    }


def _ticket_view(ticket: object) -> dict:
    """Whitelists the only response fields allowed in CLI output/reporting."""
    if not isinstance(ticket, dict):
        return {"unexpected_ticket_type": type(ticket).__name__}
    return {key: ticket.get(key) for key in (
        "id", "subject", "status", "closed_at", "updated_at", "created_at", "tags"
    )}


def _base_result(start: date, end: date) -> dict:
    query = closed_query_string(start, end, missing_tags_only=True)
    # The official search endpoint documents a fixed 30-result page; ``page``
    # alone is the supported pagination control for this narrow first-page probe.
    params = {"query": query, "page": FORCED_PAGE}
    return {
        "method": ALLOWED_METHOD,
        "host": ALLOWED_HOST,
        "endpoint": ALLOWED_ENDPOINT,
        "page": FORCED_PAGE,
        "request_budget": REQUEST_BUDGET,
        "actual_requests": 0,
        "closed_status": CLOSED_STATUS,
        "missing_tags": True,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "query": query,
        "params": params,
        "redirect_count": 0,
        "verdict": VERDICT_FAILED,
    }


def run_probe(start: date, end: date, *, execute: bool) -> dict:
    """Run dry-run or, only when requested, the one permitted guarded GET."""
    result = _base_result(start, end)
    if not execute:
        result["dry_run"] = True
        return result

    credential = _credential_for_single_request()
    if credential is None:
        result["error"] = "credential unavailable"
        return result
    api_key, credential_source = credential

    # This code deliberately has one requests call site.  No loop/retry path.
    url = f"https://{ALLOWED_HOST}{ALLOWED_ENDPOINT}"
    request = requests.Request(
        ALLOWED_METHOD,
        url,
        auth=(api_key, "X"),
        params={"query": result["query"], "page": FORCED_PAGE},
    ).prepare()
    parsed_request = urlparse(request.url)
    request_params = dict(parse_qsl(parsed_request.query, keep_blank_values=True))
    if (
        request.method != ALLOWED_METHOD
        or parsed_request.scheme != "https"
        or parsed_request.hostname != ALLOWED_HOST
        or parsed_request.path != ALLOWED_ENDPOINT
        or set(request_params) != {"query", "page"}
        or request_params.get("page") != str(FORCED_PAGE)
    ):
        result["error"] = "request guard rejected unexpected request shape"
        return result

    result["actual_requests"] = 1
    started = time.monotonic()
    try:
        # One verified GET call only: redirects are refused and requests has no
        # retry argument/path here. No pagination or follow-up request exists.
        response = requests.get(
            url,
            auth=(api_key, "X"),
            params={"query": result["query"], "page": FORCED_PAGE},
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        result.update({"duration_seconds": round(time.monotonic() - started, 3), "error": f"request failed: {type(exc).__name__}"})
        return result
    finally:
        # Avoid retaining a credential beyond the one request's call frame.
        api_key = ""

    result["duration_seconds"] = round(time.monotonic() - started, 3)
    parsed_url = urlparse(response.url)
    result.update({
        "http_status": response.status_code,
        "final_hostname": parsed_url.hostname,
        "redirect_count": len(response.history),
        **_safe_headers(response.headers),
    })
    if parsed_url.hostname != ALLOWED_HOST:
        result["error"] = "unexpected final hostname"
        return result
    if response.history or 300 <= response.status_code < 400:
        result["error"] = "redirect rejected"
        return result
    if response.status_code != 200:
        result["error"] = f"HTTP {response.status_code}"
        return result

    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        result["error"] = "invalid JSON"
        return result
    result["top_level_type"] = type(payload).__name__
    if not isinstance(payload, dict):
        result["error"] = "invalid top-level JSON"
        return result
    if "total" not in payload:
        result["error"] = "missing total"
        return result
    total = payload["total"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        result["error"] = "invalid total"
        return result
    if "results" not in payload:
        result["error"] = "missing results"
        return result
    tickets = payload["results"]
    if not isinstance(tickets, list):
        result["error"] = "invalid results"
        return result
    if len(tickets) > 30:
        result["error"] = "more than 30 page results"
        return result

    required = ("id", "subject", "status", "closed_at", "updated_at", "created_at", "tags")
    tickets_view = [_ticket_view(ticket) for ticket in tickets]
    compatibility = {}
    for field in required:
        values = [ticket[field] for ticket in tickets if isinstance(ticket, dict) and field in ticket]
        absent = sum(not isinstance(ticket, dict) or field not in ticket for ticket in tickets)
        types = sorted({type(value).__name__ for value in values})
        compatibility[field] = {
            "absent_count": absent,
            "types": types,
            "status": "ABSENT" if absent else "MATCHES FIXTURE EXPECTATION",
        }
        if absent or (field == "tags" and any(not isinstance(value, list) for value in values)):
            compatibility[field]["status"] = "DIFFERS FROM FIXTURE" if values else "ABSENT"

    statuses = [ticket.get("status") for ticket in tickets if isinstance(ticket, dict)]
    status_compatible = all(value == CLOSED_STATUS for value in statuses)
    if not status_compatible:
        compatibility["status"]["status"] = "DIFFERS FROM FIXTURE"
    result.update({
        "total": total,
        "results_type": type(tickets).__name__,
        "page_result_count": len(tickets),
        "tickets": tickets_view,
        "field_compatibility": compatibility,
        "status_compatible_with_closed": status_compatible,
        "credential_source": credential_source,
    })
    result["verdict"] = (
        VERDICT_PASS
        if status_compatible and all(item["status"] == "MATCHES FIXTURE EXPECTATION" for item in compatibility.values())
        else VERDICT_DIFFERENCES
    )
    return result


def _print_dry_run(result: dict) -> None:
    print("Dry run: no network request made")
    print(f"Method: {result['method']}")
    print(f"Host: {result['host']}")
    print(f"Endpoint: {result['endpoint']}")
    print(f"Page: {result['page']}")
    print(f"Request budget: {result['request_budget']}")
    print(f"Closed status: {result['closed_status']}")
    print("Missing tags: ON")
    print(f"Date range: {result['date_range']['start']} to {result['date_range']['end']}")
    print(f"Query: {result['query']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_date, default=date(2026, 8, 1))
    parser.add_argument("--end", type=_parse_date, default=date(2026, 8, 3))
    parser.add_argument("--execute", action="store_true", help="make the single permitted GET request")
    ns = parser.parse_args(argv)
    result = run_probe(ns.start, ns.end, execute=ns.execute)
    if not ns.execute:
        _print_dry_run(result)
        return EXIT_DRY_RUN
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("error") == "credential unavailable":
        return EXIT_CREDENTIAL
    return EXIT_PASS if result["verdict"] == VERDICT_PASS else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
