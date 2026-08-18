#!/usr/bin/env python3
"""Two-request, read-only Freshdesk closed-ticket stats contract probe.

Stage 1: GET /api/v2/search/tickets — acquire exactly one closed-ticket ID.
Stage 2: GET /api/v2/tickets/<id>?include=stats — verify stats.closed_at.

Default operation is dry-run. ``--execute`` is the sole path that may issue
network requests. The request budget is hard-capped at 2 (one search, one
stats GET). No retry, pagination, or follow-up path exists. This module is
intentionally separate from the Flask dashboard and does not alter its
offline-only boundary.
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests

from app import CLOSED_STATUS, closed_query_string, parse_dt

ALLOWED_METHOD = "GET"
ALLOWED_HOST = "broadriverretail-help.freshdesk.com"
SEARCH_ENDPOINT = "/api/v2/search/tickets"
FORCED_PAGE = 1
REQUEST_BUDGET = 2
REQUEST_TIMEOUT_SECONDS = 20
# Default narrow historical range — matches prior successful probe.
DEFAULT_START = date(2026, 8, 1)
DEFAULT_END = date(2026, 8, 3)
KEY_FILE = Path.home() / ".config" / "furtouch" / "freshdesk_api_key"

VERDICT_PASS = "STATS PROBE PASS — CLOSED_AT CONFIRMED"
VERDICT_DIFFERENCES = "STATS PROBE PASS WITH DIFFERENCE — REVIEW REQUIRED"
VERDICT_FAILED = "STATS PROBE FAILED SAFELY — STOP"
EXIT_PASS = 0
EXIT_DRY_RUN = 0
EXIT_CREDENTIAL = 2
EXIT_FAILED = 3


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def allowed_endpoint(ticket_id: int) -> str:
    """Return the one permitted numeric ticket endpoint or reject the input."""
    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
        raise ValueError("ticket ID must be a positive integer")
    return f"/api/v2/tickets/{ticket_id}"


def _credential() -> tuple[str, str] | None:
    """Read a credential only immediately before the explicitly authorized GETs."""
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
        "rate_limit_total": lower.get("x-ratelimit-total"),
        "rate_limit_remaining": lower.get("x-ratelimit-remaining"),
        "rate_limit_used_current_request": lower.get("x-ratelimit-used-currentrequest"),
        "retry_after": lower.get("retry-after"),
    }


def _safe_ticket_view(ticket: dict) -> dict:
    """Whitelist precisely the safe ticket fields authorized for output."""
    if not isinstance(ticket, dict):
        return {"unexpected_ticket_type": type(ticket).__name__}
    return {key: ticket.get(key) for key in (
        "id", "status", "created_at", "updated_at", "tags"
    )}


def _safe_stats_view(stats) -> dict:
    """Whitelist precisely the stats fields authorized for output."""
    if not isinstance(stats, dict):
        return {"exists": True, "type": type(stats).__name__}

    def timestamp(name: str, include_value: bool) -> dict:
        exists = name in stats
        value = stats.get(name) if exists else None
        result = {"exists": exists, "type": type(value).__name__ if exists else None}
        if include_value and exists:
            result["value"] = value
            parsed = parse_dt(value)
            result["parse_dt_accepted"] = parsed is not None
            if parsed is not None:
                result["timezone_aware"] = parsed.tzinfo is not None
        return result

    return {
        "exists": True,
        "type": "dict",
        "closed_at": timestamp("closed_at", include_value=True),
        "resolved_at": timestamp("resolved_at", include_value=False),
        "first_responded_at": timestamp("first_responded_at", include_value=False),
    }


def _base_result() -> dict:
    return {
        "method": ALLOWED_METHOD,
        "host": ALLOWED_HOST,
        "request_budget": REQUEST_BUDGET,
        "actual_requests": 0,
        "dashboard_live_mode": False,
        "stage1": {
            "endpoint": SEARCH_ENDPOINT,
            "page": FORCED_PAGE,
            "missing_tags": True,
            "closed_status": CLOSED_STATUS,
        },
        "stage2": {
            "endpoint_template": "/api/v2/tickets/<id>",
            "include": "stats",
        },
        "verdict": VERDICT_FAILED,
    }


def _dry_run_result() -> dict:
    result = _base_result()
    query = closed_query_string(DEFAULT_START, DEFAULT_END, missing_tags_only=True)
    result["stage1"]["query"] = query
    result["stage1"]["date_range"] = {
        "start": DEFAULT_START.isoformat(),
        "end": DEFAULT_END.isoformat(),
    }
    result["dry_run"] = True
    result["verdict"] = "DRY RUN — NO NETWORK REQUEST"
    return result


def run_probe(*, execute: bool) -> dict:
    """Run dry-run or the two permitted guarded GETs (search then stats)."""
    result = _base_result()
    if not execute:
        return _dry_run_result()

    credential = _credential()
    if credential is None:
        result.update({"verdict": VERDICT_FAILED, "error": "credential unavailable"})
        return result
    api_key, credential_source = credential

    # --- Stage 1: search for one closed ticket ID ---------------------------
    query = closed_query_string(DEFAULT_START, DEFAULT_END, missing_tags_only=True)
    search_url = f"https://{ALLOWED_HOST}{SEARCH_ENDPOINT}"
    search_params = {"query": query, "page": FORCED_PAGE}

    # Pre-flight request shape guard
    prepped = requests.Request(ALLOWED_METHOD, search_url, params=search_params).prepare()
    parsed_req = urlparse(prepped.url)
    req_params = dict(parse_qsl(parsed_req.query, keep_blank_values=True))
    if (
        prepped.method != ALLOWED_METHOD
        or parsed_req.scheme != "https"
        or parsed_req.hostname != ALLOWED_HOST
        or parsed_req.path != SEARCH_ENDPOINT
        or req_params.get("page") != str(FORCED_PAGE)
    ):
        api_key = ""
        result.update({"verdict": VERDICT_FAILED, "error": "stage-1 request guard rejected"})
        return result

    result["actual_requests"] = 1
    t0 = time.monotonic()
    try:
        resp1 = requests.get(
            search_url,
            auth=(api_key, "X"),
            params=search_params,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        api_key = ""
        result["stage1"].update({
            "duration_seconds": round(time.monotonic() - t0, 3),
            "error": f"stage-1 failed: {type(exc).__name__}",
        })
        result["verdict"] = VERDICT_FAILED
        return result

    s1_parsed = urlparse(resp1.url)
    result["stage1"].update({
        "http_status": resp1.status_code,
        "final_hostname": s1_parsed.hostname,
        "redirect_count": len(resp1.history),
        "duration_seconds": round(time.monotonic() - t0, 3),
        **_safe_headers(resp1.headers),
    })

    if s1_parsed.hostname != ALLOWED_HOST:
        api_key = ""
        result["stage1"]["error"] = "unexpected final hostname"
        result["verdict"] = VERDICT_FAILED
        return result
    if resp1.history or 300 <= resp1.status_code < 400:
        api_key = ""
        result["stage1"]["error"] = "redirect rejected"
        result["verdict"] = VERDICT_FAILED
        return result
    if resp1.status_code != 200:
        api_key = ""
        result["stage1"]["error"] = f"HTTP {resp1.status_code}"
        result["verdict"] = VERDICT_FAILED
        return result

    try:
        payload1 = resp1.json()
    except (ValueError, requests.JSONDecodeError):
        api_key = ""
        result["stage1"]["error"] = "invalid JSON"
        result["verdict"] = VERDICT_FAILED
        return result

    if not isinstance(payload1, dict):
        api_key = ""
        result["stage1"]["error"] = "invalid top-level JSON"
        result["verdict"] = VERDICT_FAILED
        return result

    total = payload1.get("total")
    results_list = payload1.get("results")
    result["stage1"]["total"] = total
    result["stage1"]["page_count"] = len(results_list) if isinstance(results_list, list) else None

    # Select first valid numeric ticket ID from results
    selected_id = None
    if isinstance(results_list, list) and len(results_list) > 0:
        first = results_list[0]
        if isinstance(first, dict):
            tid = first.get("id")
            if isinstance(tid, bool) or not isinstance(tid, int) or tid <= 0:
                pass  # invalid
            else:
                selected_id = tid

    if selected_id is None:
        api_key = ""
        result["stage1"]["error"] = "no valid numeric ticket ID in results"
        result["verdict"] = VERDICT_FAILED
        return result

    result["stage1"]["selected_ticket_id"] = selected_id

    # --- Stage 2: single-ticket stats GET -----------------------------------
    stats_endpoint = allowed_endpoint(selected_id)
    stats_url = f"https://{ALLOWED_HOST}{stats_endpoint}"
    stats_params = {"include": "stats"}

    result["actual_requests"] = 2
    t1 = time.monotonic()
    try:
        resp2 = requests.get(
            stats_url,
            auth=(api_key, "X"),
            params=stats_params,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        result["stage2"].update({
            "duration_seconds": round(time.monotonic() - t1, 3),
            "error": f"stage-2 failed: {type(exc).__name__}",
        })
        result["verdict"] = VERDICT_FAILED
        return result
    finally:
        api_key = ""

    s2_parsed = urlparse(resp2.url)
    result["stage2"].update({
        "http_status": resp2.status_code,
        "final_hostname": s2_parsed.hostname,
        "redirect_count": len(resp2.history),
        "selected_ticket_id": selected_id,
        "duration_seconds": round(time.monotonic() - t1, 3),
        **_safe_headers(resp2.headers),
    })

    if s2_parsed.hostname != ALLOWED_HOST:
        result["stage2"]["error"] = "unexpected final hostname"
        result["verdict"] = VERDICT_FAILED
        return result
    if resp2.history or 300 <= resp2.status_code < 400:
        result["stage2"]["error"] = "redirect rejected"
        result["verdict"] = VERDICT_FAILED
        return result
    if resp2.status_code != 200:
        result["stage2"]["error"] = f"HTTP {resp2.status_code}"
        result["verdict"] = VERDICT_FAILED
        return result

    try:
        ticket_data = resp2.json()
    except (ValueError, requests.JSONDecodeError):
        result["stage2"]["error"] = "invalid JSON"
        result["verdict"] = VERDICT_FAILED
        return result

    if not isinstance(ticket_data, dict):
        result["stage2"]["error"] = "invalid top-level JSON"
        result["verdict"] = VERDICT_FAILED
        return result

    result["ticket"] = _safe_ticket_view(ticket_data)

    # Validate stats
    stats = ticket_data.get("stats")
    if stats is None:
        result["stats"] = {"exists": False, "type": None}
        result["verdict"] = VERDICT_DIFFERENCES
        return result
    result["stats"] = _safe_stats_view(stats)

    # Evaluate verdict
    closed_at_info = result["stats"].get("closed_at", {})
    has_closed_at = closed_at_info.get("exists") is True
    closed_at_value = closed_at_info.get("value")
    parse_ok = closed_at_info.get("parse_dt_accepted") is True
    tz_aware = closed_at_info.get("timezone_aware") is True

    if has_closed_at and closed_at_value is not None and parse_ok and tz_aware:
        result["verdict"] = VERDICT_PASS
    else:
        result["verdict"] = VERDICT_DIFFERENCES

    return result


def _print_json(result: dict) -> None:
    print(json.dumps(result, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Two-request closed-ticket stats probe (dry-run by default)."
    )
    parser.add_argument("--execute", action="store_true", help="Issue the two live GET requests.")
    args = parser.parse_args(argv)

    result = run_probe(execute=args.execute)
    _print_json(result)

    if result.get("dry_run"):
        return EXIT_DRY_RUN
    v = result.get("verdict", "")
    if v == VERDICT_PASS:
        return EXIT_PASS
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
