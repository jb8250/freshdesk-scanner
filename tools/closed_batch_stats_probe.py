#!/usr/bin/env python3
"""One-request, read-only Freshdesk batch ticket-stats contract probe.

Single GET /api/v2/tickets?include=stats&per_page=100&page=1&updated_since=...
Returns up to 100 tickets with embedded stats. All closed-status, missing-tags,
and date-range filtering is applied locally to the response — never as query
parameters.

Default operation is dry-run. ``--execute`` is the sole path that may issue
network requests. The request budget is hard-capped at 1. No retry, pagination,
or follow-up path exists. This module is intentionally separate from the Flask
dashboard and does not alter its offline-only boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests

from app import CLOSED_STATUS, parse_dt

# ---------------------------------------------------------------------------
# Fixed request parameters — no CLI overrides permitted
# ---------------------------------------------------------------------------
ALLOWED_METHOD = "GET"
ALLOWED_HOST = "broadriverretail-help.freshdesk.com"
ENDPOINT = "/api/v2/tickets"
FORCED_INCLUDE = "stats"
FORCED_PER_PAGE = 100
FORCED_PAGE = 1
FORCED_UPDATED_SINCE = "2026-08-01T00:00:00Z"
FORCED_ORDER_BY = "status"
FORCED_ORDER_TYPE = "desc"
REQUEST_BUDGET = 1
REQUEST_TIMEOUT_SECONDS = 30

# Local date-window for post-response comparison (Aug 1 through Aug 3 inclusive)
LOCAL_WINDOW_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
LOCAL_WINDOW_END = datetime(2026, 8, 4, tzinfo=timezone.utc)

KEY_FILE = Path.home() / ".config" / "furtouch" / "freshdesk_api_key"

VERDICT_PASS = "BATCH STATS PROBE PASS — ONE-PAGE BATCH CONTRACT CONFIRMED"
VERDICT_DIFFERENCES = "BATCH STATS PROBE PASS WITH DIFFERENCE — REVIEW REQUIRED"
VERDICT_FAILED = "BATCH STATS PROBE FAILED SAFELY — STOP"
EXIT_DRY_RUN = 0
EXIT_CREDENTIAL = 2
EXIT_FAILED = 3


# ---------------------------------------------------------------------------
# Credential handling — read only at execution time, never logged
# ---------------------------------------------------------------------------
def _credential() -> tuple[str, str] | None:
    """Read a credential only immediately before the explicitly authorized GET."""
    value = os.environ.get("FRESHDESK_API_KEY", "")
    if value:
        return value, "environment"
    try:
        value = KEY_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return (value, "external file") if value else None


# ---------------------------------------------------------------------------
# Safe-header extraction — credential-free
# ---------------------------------------------------------------------------
def _safe_headers(headers) -> dict[str, str | None]:
    lower = {str(key).lower(): str(value) for key, value in headers.items()}
    return {
        "rate_limit_total": lower.get("x-ratelimit-total"),
        "rate_limit_remaining": lower.get("x-ratelimit-remaining"),
        "rate_limit_used_current_request": lower.get("x-ratelimit-used-currentrequest"),
        "retry_after": lower.get("retry-after"),
    }


def _link_info(headers) -> dict[str, bool]:
    """Inspect Link header for next-page presence without following it."""
    lower = {str(key).lower(): str(value) for key, value in headers.items()}
    link = lower.get("link", "")
    if not link:
        return {"link_header_present": False, "link_indicates_next_page": False}
    has_next = "rel=\"next\"" in link or "rel=next" in link
    return {"link_header_present": True, "link_indicates_next_page": has_next}


# ---------------------------------------------------------------------------
# Safe field views — whitelist only authorized output
# ---------------------------------------------------------------------------
SAFE_SAMPLE_FIELDS = ("id", "status", "created_at", "updated_at", "tags")


def _safe_ticket_sample(ticket: dict) -> dict:
    """Whitelist precisely the safe fields authorized for sample output."""
    if not isinstance(ticket, dict):
        return {"unexpected_ticket_type": type(ticket).__name__}
    sample = {key: ticket.get(key) for key in SAFE_SAMPLE_FIELDS}
    stats = ticket.get("stats")
    if isinstance(stats, dict):
        sample["stats"] = {"closed_at": stats.get("closed_at")}
    else:
        sample["stats"] = {"closed_at": None}
    return sample


def _analyze_ticket(ticket) -> dict:
    """Analyze a single ticket for field-coverage and local-filter counts."""
    if not isinstance(ticket, dict):
        return {"valid_dict": False}

    tid = ticket.get("id")
    status = ticket.get("status")
    created = ticket.get("created_at")
    updated = ticket.get("updated_at")
    tags = ticket.get("tags")
    stats = ticket.get("stats")

    id_present = isinstance(tid, int) and not isinstance(tid, bool) and tid > 0
    status_present = status is not None
    created_present = created is not None
    updated_present = updated is not None
    tags_is_list = isinstance(tags, list)
    stats_is_dict = isinstance(stats, dict)

    stats_closed_at_present = stats_is_dict and "closed_at" in stats
    stats_closed_at_value = stats.get("closed_at") if stats_closed_at_present else None
    stats_closed_at_string = isinstance(stats_closed_at_value, str)
    stats_closed_at_null = stats_closed_at_value is None
    stats_closed_at_missing = not stats_closed_at_present

    status_is_5 = (
        status == CLOSED_STATUS
        and isinstance(status, int)
        and not isinstance(status, bool)
    )
    empty_tags = tags_is_list and len(tags) == 0
    closed_and_empty_tags = status_is_5 and empty_tags

    valid_closed_at = False
    parsed_closed_at = None
    in_local_window = False
    if stats_closed_at_string and not stats_closed_at_null:
        parsed_closed_at = parse_dt(stats_closed_at_value)
        valid_closed_at = parsed_closed_at is not None
        if valid_closed_at:
            in_local_window = (
                LOCAL_WINDOW_START <= parsed_closed_at < LOCAL_WINDOW_END
            )

    return {
        "valid_dict": True,
        "id_present": id_present,
        "status_present": status_present,
        "created_at_present": created_present,
        "updated_at_present": updated_present,
        "tags_list": tags_is_list,
        "stats_dict": stats_is_dict,
        "stats_closed_at_present": stats_closed_at_present,
        "stats_closed_at_string": stats_closed_at_string,
        "stats_closed_at_null": stats_closed_at_null,
        "stats_closed_at_missing": stats_closed_at_missing,
        "status_5": status_is_5,
        "empty_tags": empty_tags,
        "status_5_and_empty_tags": closed_and_empty_tags,
        "valid_closed_at": valid_closed_at,
        "invalid_or_missing_closed_at": closed_and_empty_tags and not valid_closed_at,
        "in_local_window": closed_and_empty_tags and valid_closed_at and in_local_window,
    }


def _aggregate_analysis(tickets: list) -> dict:
    """Aggregate per-ticket analysis into reportable counts."""
    counts = {
        "tickets_returned": 0,
        "id_present": 0,
        "status_present": 0,
        "created_at_present": 0,
        "updated_at_present": 0,
        "tags_list": 0,
        "stats_dict": 0,
        "stats_closed_at_present": 0,
        "stats_closed_at_string": 0,
        "stats_closed_at_null": 0,
        "stats_closed_at_missing": 0,
        "status_5_count": 0,
        "empty_tags_count": 0,
        "status_5_and_empty_tags_count": 0,
        "valid_closed_at_count": 0,
        "invalid_or_missing_closed_at_count": 0,
        "closed_no_tags_in_aug_1_through_aug_3_count": 0,
    }
    samples = []
    sample_count = 0

    _bool_keys = [
        "id_present", "status_present", "created_at_present",
        "updated_at_present", "tags_list", "stats_dict",
        "stats_closed_at_present", "stats_closed_at_string",
        "stats_closed_at_null", "stats_closed_at_missing",
    ]

    for ticket in tickets:
        a = _analyze_ticket(ticket)
        if not a.get("valid_dict"):
            continue
        counts["tickets_returned"] += 1

        for key in _bool_keys:
            if a.get(key):
                counts[key] += 1

        if a["status_5"]:
            counts["status_5_count"] += 1
        if a["empty_tags"]:
            counts["empty_tags_count"] += 1
        if a["status_5_and_empty_tags"]:
            counts["status_5_and_empty_tags_count"] += 1
            if a["valid_closed_at"]:
                counts["valid_closed_at_count"] += 1
            else:
                counts["invalid_or_missing_closed_at_count"] += 1
            if a["in_local_window"]:
                counts["closed_no_tags_in_aug_1_through_aug_3_count"] += 1

        if a["status_5_and_empty_tags"] and sample_count < 3:
            samples.append(_safe_ticket_sample(ticket))
            sample_count += 1

    return {"counts": counts, "samples": samples}


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------
def _base_result() -> dict:
    return {
        "method": ALLOWED_METHOD,
        "host": ALLOWED_HOST,
        "endpoint": ENDPOINT,
        "include": FORCED_INCLUDE,
        "per_page": FORCED_PER_PAGE,
        "page": FORCED_PAGE,
        "updated_since": FORCED_UPDATED_SINCE,
        "order_by": FORCED_ORDER_BY,
        "order_type": FORCED_ORDER_TYPE,
        "request_budget": REQUEST_BUDGET,
        "actual_requests": 0,
        "dashboard_live_mode": False,
        "verdict": VERDICT_FAILED,
    }


def _dry_run_result() -> dict:
    result = _base_result()
    result["dry_run"] = True
    result["filtering"] = "local only after response"
    result["local_filter_rules"] = {
        "keep_when": [
            "status == 5 (Closed)",
            "tags is empty list []",
            "stats.closed_at is valid (non-null string, parseable by parse_dt)",
        ],
        "local_date_window": {
            "start_inclusive": "2026-08-01T00:00:00Z",
            "end_exclusive": "2026-08-04T00:00:00Z",
            "note": "Represents calendar dates August 1 through August 3 inclusive. Post-response comparison only; request is never modified based on this range.",
        },
    }
    result["verdict"] = "DRY RUN — NO NETWORK REQUEST"
    return result


def run_probe(*, execute: bool) -> dict:
    """Run dry-run or the single permitted guarded GET."""
    if not execute:
        return _dry_run_result()

    result = _base_result()
    result["filtering"] = "local only after response"

    credential = _credential()
    if credential is None:
        result.update({"verdict": VERDICT_FAILED, "error": "credential unavailable"})
        return result
    api_key, _credential_source = credential

    url = f"https://{ALLOWED_HOST}{ENDPOINT}"
    params = {
        "include": FORCED_INCLUDE,
        "per_page": FORCED_PER_PAGE,
        "page": FORCED_PAGE,
        "updated_since": FORCED_UPDATED_SINCE,
        "order_by": FORCED_ORDER_BY,
        "order_type": FORCED_ORDER_TYPE,
    }

    # Pre-flight request-shape guard
    prepped = requests.Request(ALLOWED_METHOD, url, params=params).prepare()
    parsed_req = urlparse(prepped.url)
    req_params = dict(parse_qsl(parsed_req.query, keep_blank_values=True))

    if (
        prepped.method != ALLOWED_METHOD
        or parsed_req.scheme != "https"
        or parsed_req.hostname != ALLOWED_HOST
        or parsed_req.path != ENDPOINT
        or req_params.get("include") != FORCED_INCLUDE
        or req_params.get("per_page") != str(FORCED_PER_PAGE)
        or req_params.get("page") != str(FORCED_PAGE)
        or req_params.get("updated_since") != FORCED_UPDATED_SINCE
        or req_params.get("order_by") != FORCED_ORDER_BY
        or req_params.get("order_type") != FORCED_ORDER_TYPE
    ):
        api_key = ""
        result.update({"verdict": VERDICT_FAILED, "error": "request-shape guard rejected"})
        return result

    result["actual_requests"] = 1
    t0 = time.monotonic()

    try:
        resp = requests.get(
            url,
            auth=(api_key, "X"),
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        api_key = ""
        result.update({
            "duration_seconds": round(time.monotonic() - t0, 3),
            "verdict": VERDICT_FAILED,
            "error": f"request failed: {type(exc).__name__}",
        })
        return result

    api_key = ""

    parsed_resp = urlparse(resp.url)
    result.update({
        "http_status": resp.status_code,
        "content_type": resp.headers.get("Content-Type"),
        "duration_seconds": round(time.monotonic() - t0, 3),
        "redirect_count": len(resp.history),
        "final_hostname": parsed_resp.hostname,
        **_safe_headers(resp.headers),
        **_link_info(resp.headers),
    })

    # Foreign redirect rejection
    if parsed_resp.hostname != ALLOWED_HOST:
        result["error"] = "unexpected final hostname"
        result["verdict"] = VERDICT_FAILED
        return result
    if resp.history or 300 <= resp.status_code < 400:
        result["error"] = "redirect rejected"
        result["verdict"] = VERDICT_FAILED
        return result
    if resp.status_code != 200:
        result["error"] = f"HTTP {resp.status_code}"
        result["verdict"] = VERDICT_FAILED
        return result

    # Parse JSON response — must be a list
    try:
        payload = resp.json()
    except (ValueError, requests.JSONDecodeError):
        result["error"] = "invalid JSON"
        result["verdict"] = VERDICT_FAILED
        return result

    if not isinstance(payload, list):
        result["error"] = "non-list JSON response"
        result["verdict"] = VERDICT_FAILED
        return result

    # Validate per_page consistency
    if len(payload) > FORCED_PER_PAGE:
        result["error"] = f"returned {len(payload)} tickets, exceeds per_page={FORCED_PER_PAGE}"
        result["verdict"] = VERDICT_FAILED
        return result

    # Local analysis
    analysis = _aggregate_analysis(payload)
    counts = analysis["counts"]
    result["aggregate"] = counts
    result["samples"] = analysis["samples"]

    # Track parse_dt acceptance across all populated closed_at values
    all_parsed_valid = True
    any_parsed = False
    for ticket in payload:
        if not isinstance(ticket, dict):
            continue
        stats = ticket.get("stats")
        if isinstance(stats, dict):
            ca = stats.get("closed_at")
            if isinstance(ca, str) and ca:
                any_parsed = True
                parsed = parse_dt(ca)
                if parsed is None:
                    all_parsed_valid = False

    result["all_parsed_closed_at_valid"] = all_parsed_valid
    result["any_parsed_attempted"] = any_parsed

    # Determine verdict
    differences = []

    # Check rate-limit header
    used_header = result.get("rate_limit_used_current_request")
    if used_header is not None and used_header != "3":
        differences.append(f"X-RateLimit-Used-CurrentRequest={used_header} (docs say 3)")

    if differences:
        result["differences"] = differences
        result["verdict"] = VERDICT_DIFFERENCES
    else:
        result["verdict"] = VERDICT_PASS

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-request batch ticket-stats probe for Freshdesk."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Issue the single permitted live GET. Default is dry-run (zero network).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    result = run_probe(execute=args.execute)
    indent = 2 if args.pretty else None
    print(json.dumps(result, indent=indent, default=str, sort_keys=True))

    if result.get("verdict") == VERDICT_FAILED:
        return EXIT_FAILED
    return EXIT_DRY_RUN


if __name__ == "__main__":
    sys.exit(main())
