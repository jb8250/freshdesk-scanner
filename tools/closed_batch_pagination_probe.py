#!/usr/bin/env python3
"""Multi-page, read-only Freshdesk batch pagination contract probe.

Sequentially paginates GET /api/v2/tickets with fixed parameters:
  include=stats, per_page=100, updated_since=2026-08-01T00:00:00Z,
  order_by=status, order_type=desc, page=1..15

Stops when the FIRST of:
  - Closed status block exhausted (status < 5 seen after status 5 block)
  - No next-page Link
  - Page 15 reached (safety cap)
  - X-RateLimit-Remaining <= 40 (conservative floor)
  - Sort violation detected
  - HTTP/JSON/transport error

Default operation is dry-run. ``--execute`` is the sole path that may issue
network requests. No retries, no skips, no duplicate pages, no writes.
No Search Tickets, no View Ticket, no conversations/requesters/attachments.
This module is separate from the Flask dashboard and does not alter its
offline-only boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
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
FORCED_UPDATED_SINCE = "2026-08-01T00:00:00Z"
FORCED_ORDER_BY = "status"
FORCED_ORDER_TYPE = "desc"

MAX_PAGES = 15
MAX_REQUESTS = 15
MIN_REMAINING_BEFORE_NEXT_REQUEST = 40
REQUEST_TIMEOUT_SECONDS = 30

# Local date-window for post-response comparison (Aug 1 through Aug 3 inclusive)
LOCAL_WINDOW_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
LOCAL_WINDOW_END = datetime(2026, 8, 4, tzinfo=timezone.utc)

KEY_FILE = Path.home() / ".config" / "furtouch" / "freshdesk_api_key"

# State machine
BEFORE_CLOSED_BLOCK = "BEFORE_CLOSED_BLOCK"
IN_CLOSED_BLOCK = "IN_CLOSED_BLOCK"
AFTER_CLOSED_BLOCK = "AFTER_CLOSED_BLOCK"

VERDICT_PASS = "PAGINATION PROBE PASS — CLOSED BLOCK COMPLETENESS CONFIRMED"
VERDICT_INCOMPLETE = "PAGINATION PROBE INCOMPLETE — CLOSED BLOCK EXCEEDS SAFETY CAP"
VERDICT_SORT_FAILED = "PAGINATION PROBE FAILED — STATUS EARLY-STOP NOT SAFE"
VERDICT_FAILED = "PAGINATION PROBE FAILED SAFELY — OTHER"
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
    has_next = 'rel="next"' in link or "rel=next" in link
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


# ---------------------------------------------------------------------------
# Per-ticket analysis
# ---------------------------------------------------------------------------
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
    tags_is_list = isinstance(tags, list)
    stats_is_dict = isinstance(stats, dict)

    stats_closed_at_value = stats.get("closed_at") if stats_is_dict else None
    stats_closed_at_string = isinstance(stats_closed_at_value, str)
    stats_closed_at_null = stats_closed_at_value is None

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

    # Timestamp relationship: updated_at vs closed_at
    parsed_updated_at: datetime | None = None
    if isinstance(updated, str) and updated:
        parsed_updated_at = parse_dt(updated)
    updated_at_gte_closed_at: bool | None = None
    if valid_closed_at and parsed_updated_at is not None and parsed_closed_at is not None:
        updated_at_gte_closed_at = parsed_updated_at >= parsed_closed_at
    elif valid_closed_at and parsed_updated_at is None:
        updated_at_gte_closed_at = None  # malformed updated_at

    return {
        "valid_dict": True,
        "id": tid if id_present else None,
        "id_present": id_present,
        "status": status,
        "status_present": status_present,
        "created_at_present": created is not None,
        "updated_at_present": updated is not None,
        "tags_list": tags_is_list,
        "tags": tags if tags_is_list else None,
        "stats_dict": stats_is_dict,
        "stats_closed_at_string": stats_closed_at_string,
        "stats_closed_at_null": stats_closed_at_null,
        "status_5": status_is_5,
        "empty_tags": empty_tags,
        "status_5_and_empty_tags": closed_and_empty_tags,
        "valid_closed_at": valid_closed_at,
        "invalid_or_missing_closed_at": closed_and_empty_tags and not valid_closed_at,
        "in_local_window": closed_and_empty_tags and valid_closed_at and in_local_window,
        "parsed_closed_at": parsed_closed_at.isoformat() if parsed_closed_at else None,
        "parsed_updated_at": parsed_updated_at.isoformat() if parsed_updated_at else None,
        "updated_at_gte_closed_at": updated_at_gte_closed_at,
        "updated_at_lt_closed_at": (
            updated_at_gte_closed_at is False
            if updated_at_gte_closed_at is not None
            else None
        ),
    }


# ---------------------------------------------------------------------------
# Status-block state machine
# ---------------------------------------------------------------------------
def _update_state(state: str, status: int) -> str:
    """Transition state machine based on current state and record status.

    BEFORE_CLOSED_BLOCK:
      status > 5  -> stays BEFORE_CLOSED_BLOCK
      status == 5 -> IN_CLOSED_BLOCK
      status < 5  -> AFTER_CLOSED_BLOCK (no closed block found)

    IN_CLOSED_BLOCK:
      status == 5 -> stays IN_CLOSED_BLOCK
      status < 5  -> AFTER_CLOSED_BLOCK
      status > 5  -> violation (handled by sort checker)

    AFTER_CLOSED_BLOCK:
      stays AFTER_CLOSED_BLOCK (no more requests allowed)
    """
    if state == BEFORE_CLOSED_BLOCK:
        if status == CLOSED_STATUS:
            return IN_CLOSED_BLOCK
        elif status < CLOSED_STATUS:
            return AFTER_CLOSED_BLOCK
        else:
            return BEFORE_CLOSED_BLOCK
    elif state == IN_CLOSED_BLOCK:
        if status < CLOSED_STATUS:
            return AFTER_CLOSED_BLOCK
        else:
            return IN_CLOSED_BLOCK
    else:
        return AFTER_CLOSED_BLOCK


# ---------------------------------------------------------------------------
# Sort monotonicity checking
# ---------------------------------------------------------------------------
def _check_sort(prev_status: int | None, curr_status: int) -> tuple[int | None, bool]:
    """Check that status is monotonically non-increasing.

    Returns (new_prev_status, is_violation).
    """
    if prev_status is not None and prev_status < curr_status:
        return curr_status, True
    return curr_status, False


# ---------------------------------------------------------------------------
# Request URL construction
# ---------------------------------------------------------------------------
def _build_url(page: int) -> str:
    """Build the fixed-parameter request URL for a given page number."""
    return (
        f"https://{ALLOWED_HOST}{ENDPOINT}"
        f"?include={FORCED_INCLUDE}"
        f"&per_page={FORCED_PER_PAGE}"
        f"&page={page}"
        f"&updated_since={FORCED_UPDATED_SINCE}"
        f"&order_by={FORCED_ORDER_BY}"
        f"&order_type={FORCED_ORDER_TYPE}"
    )


def _validate_url(url: str) -> bool:
    """Verify the constructed URL matches the required shape."""
    parsed = urlparse(url)
    if parsed.hostname != ALLOWED_HOST:
        return False
    if parsed.path != ENDPOINT:
        return False
    if parsed.scheme != "https":
        return False
    params = dict(parse_qsl(parsed.query))
    if params.get("include") != FORCED_INCLUDE:
        return False
    if params.get("per_page") != str(FORCED_PER_PAGE):
        return False
    if params.get("updated_since") != FORCED_UPDATED_SINCE:
        return False
    if params.get("order_by") != FORCED_ORDER_BY:
        return False
    if params.get("order_type") != FORCED_ORDER_TYPE:
        return False
    page_str = params.get("page", "")
    try:
        page_num = int(page_str)
    except (ValueError, TypeError):
        return False
    return 1 <= page_num <= MAX_PAGES


# ---------------------------------------------------------------------------
# Redirect safety
# ---------------------------------------------------------------------------
def _check_redirect(response) -> str | None:
    """Return redirect destination if redirected to a foreign host, else None."""
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("Location", "")
        if location:
            parsed = urlparse(location)
            if parsed.hostname != ALLOWED_HOST:
                return location
    return None


# ---------------------------------------------------------------------------
# Probe execution
# ---------------------------------------------------------------------------
def run_probe(execute: bool = False, pretty: bool = False) -> dict:
    """Execute the guarded pagination probe.

    Returns a dict with all results. When execute=False, no HTTP requests
    are made and the result describes what *would* happen.
    """
    result = {
        "probe": "closed_batch_pagination_probe",
        "executed": execute,
        "max_pages": MAX_PAGES,
        "max_requests": MAX_REQUESTS,
        "min_remaining_before_next_request": MIN_REMAINING_BEFORE_NEXT_REQUEST,
        "fixed_params": {
            "include": FORCED_INCLUDE,
            "per_page": FORCED_PER_PAGE,
            "updated_since": FORCED_UPDATED_SINCE,
            "order_by": FORCED_ORDER_BY,
            "order_type": FORCED_ORDER_TYPE,
        },
        "local_window": {
            "start": LOCAL_WINDOW_START.isoformat(),
            "end": LOCAL_WINDOW_END.isoformat(),
        },
        "pages": [],
        "requests_made": 0,
        "reported_units_used_sum": 0,
        "stop_reason": None,
        "final_state": None,
        "verdict": None,
        # Cumulative counts
        "cumulative": {
            "tickets_returned": 0,
            "status_5_count": 0,
            "empty_tags_count": 0,
            "status_5_and_empty_tags_count": 0,
            "closed_stats_dict_count": 0,
            "closed_valid_closed_at_count": 0,
            "closed_invalid_or_missing_closed_at_count": 0,
            "closed_no_tags_count": 0,
            "closed_no_tags_in_aug_1_through_aug_3_count": 0,
            "closed_nonempty_tags_count": 0,
            "closed_missing_or_bad_tags_count": 0,
            "updated_at_gte_closed_at_count": 0,
            "updated_at_lt_closed_at_count": 0,
            "updated_at_relationship_unknown_count": 0,
            "unique_ticket_ids": 0,
            "duplicate_ticket_ids": 0,
            "sort_comparisons": 0,
            "sort_violations": 0,
            "closed_updated_at_lt_closed_at_violations": [],
        },
        "samples": [],
    }

    if not execute:
        result["stop_reason"] = "dry-run — no requests made"
        result["verdict"] = None
        result["final_state"] = BEFORE_CLOSED_BLOCK
        return result

    # --- Live execution path ---
    cred = _credential()
    if not cred:
        result["stop_reason"] = "credential not found"
        result["verdict"] = VERDICT_FAILED
        return result

    api_key, cred_source = cred
    auth = (api_key, "X")

    state = BEFORE_CLOSED_BLOCK
    prev_page_last_status: int | None = None  # for cross-page sort checking
    seen_ids: set[int] = set()
    duplicate_ids: list[int] = []
    sample_count = 0
    last_rate_limit_remaining: str | None = None
    reported_units_used_sum = 0

    for page_num in range(1, MAX_PAGES + 1):
        # Rate-limit floor check before requesting (skip on page 1)
        if page_num > 1 and last_rate_limit_remaining is not None:
            try:
                remaining_int = int(float(last_rate_limit_remaining))
            except (ValueError, TypeError):
                remaining_int = 0
            if remaining_int <= MIN_REMAINING_BEFORE_NEXT_REQUEST:
                result["stop_reason"] = (
                    f"rate-limit safety floor reached "
                    f"(remaining={last_rate_limit_remaining} <= {MIN_REMAINING_BEFORE_NEXT_REQUEST})"
                )
                result["verdict"] = _determine_verdict(
                    state, result["cumulative"]["sort_violations"],
                    page_num - 1, result["requests_made"]
                )
                result["final_state"] = state
                result["reported_units_used_sum"] = reported_units_used_sum
                result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
                result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
                return result

        # State-based early stop: AFTER_CLOSED_BLOCK should never request
        if state == AFTER_CLOSED_BLOCK:
            result["stop_reason"] = (
                "closed status block exhausted — stopped after processing previous page"
            )
            result["verdict"] = _determine_verdict(
                state, result["cumulative"]["sort_violations"],
                page_num - 1, result["requests_made"]
            )
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # Build and validate URL
        url = _build_url(page_num)
        if not _validate_url(url):
            result["stop_reason"] = f"URL validation failed for page {page_num}"
            result["verdict"] = VERDICT_FAILED
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # Issue the request (GET only, no retry, no allow_redirects to foreign)
        try:
            response = requests.get(
                url,
                auth=auth,
                timeout=REQUEST_TIMEOUT_SECONDS,
                allow_redirects=False,
                verify=True,
            )
        except requests.RequestException as exc:
            result["stop_reason"] = f"transport error on page {page_num}: {type(exc).__name__}"
            result["verdict"] = VERDICT_FAILED
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        result["requests_made"] += 1

        # Check for foreign redirect
        foreign = _check_redirect(response)
        if foreign:
            result["stop_reason"] = f"foreign redirect rejected on page {page_num}"
            result["verdict"] = VERDICT_FAILED
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # Check HTTP status
        if response.status_code != 200:
            hdrs = _safe_headers(response.headers)
            result["stop_reason"] = (
                f"HTTP {response.status_code} on page {page_num}"
            )
            result["verdict"] = VERDICT_FAILED
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # Parse JSON
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            result["stop_reason"] = f"malformed JSON on page {page_num}"
            result["verdict"] = VERDICT_FAILED
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        if not isinstance(body, list):
            result["stop_reason"] = f"non-list JSON body on page {page_num}"
            result["verdict"] = VERDICT_FAILED
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # Extract safe headers
        hdrs = _safe_headers(response.headers)
        link = _link_info(response.headers)
        used = hdrs.get("rate_limit_used_current_request")
        if used is not None:
            try:
                reported_units_used_sum += int(float(used))
            except (ValueError, TypeError):
                pass
        last_rate_limit_remaining = hdrs.get("rate_limit_remaining")

        # Process tickets on this page
        page_info = {
            "page": page_num,
            "http_status": response.status_code,
            "tickets_returned": len(body),
            "first_status": None,
            "last_status": None,
            "status_counts": {},
            "contains_status_5": False,
            "state_before": state,
            "state_after": state,
            "rate_limit_total": hdrs.get("rate_limit_total"),
            "rate_limit_remaining": hdrs.get("rate_limit_remaining"),
            "rate_limit_used_current_request": used,
            "link_header_present": link["link_header_present"],
            "link_indicates_next_page": link["link_indicates_next_page"],
            "closed_count": 0,
            "closed_no_tags_count": 0,
            "closed_no_tags_in_window_count": 0,
            "sort_violation_on_page": False,
            "cross_page_sort_violation": False,
        }

        sort_violation_found = False
        prev_status_on_page: int | None = prev_page_last_status  # carry across pages

        for ticket in body:
            analysis = _analyze_ticket(ticket)
            if not analysis.get("valid_dict"):
                continue

            status = analysis.get("status")

            # Status counts
            if status is not None:
                sk = str(status)
                page_info["status_counts"][sk] = page_info["status_counts"].get(sk, 0) + 1
                if page_info["first_status"] is None:
                    page_info["first_status"] = status
                page_info["last_status"] = status

            # Sort monotonicity check
            if status is not None:
                prev_status_on_page, is_violation = _check_sort(prev_status_on_page, status)
                result["cumulative"]["sort_comparisons"] += 1
                if is_violation:
                    result["cumulative"]["sort_violations"] += 1
                    sort_violation_found = True
                    page_info["sort_violation_on_page"] = True

            # State machine transition
            if status is not None and isinstance(status, int) and not isinstance(status, bool):
                state = _update_state(state, status)
                if status == CLOSED_STATUS:
                    page_info["contains_status_5"] = True

            # Dedup by ticket ID
            tid = analysis.get("id")
            if tid is not None:
                if tid in seen_ids:
                    duplicate_ids.append(tid)
                else:
                    seen_ids.add(tid)

            # Cumulative counts
            result["cumulative"]["tickets_returned"] += 1

            if analysis.get("status_5"):
                result["cumulative"]["status_5_count"] += 1
                page_info["closed_count"] += 1

                if analysis.get("empty_tags"):
                    result["cumulative"]["status_5_and_empty_tags_count"] += 1
                    result["cumulative"]["closed_no_tags_count"] += 1
                    page_info["closed_no_tags_count"] += 1
                elif analysis.get("tags_list"):
                    result["cumulative"]["closed_nonempty_tags_count"] += 1
                else:
                    result["cumulative"]["closed_missing_or_bad_tags_count"] += 1

                if analysis.get("stats_dict"):
                    result["cumulative"]["closed_stats_dict_count"] += 1

                if analysis.get("valid_closed_at"):
                    result["cumulative"]["closed_valid_closed_at_count"] += 1

                    # Timestamp relationship check
                    rel = analysis.get("updated_at_gte_closed_at")
                    if rel is True:
                        result["cumulative"]["updated_at_gte_closed_at_count"] += 1
                    elif rel is False:
                        result["cumulative"]["updated_at_lt_closed_at_count"] += 1
                        result["cumulative"]["closed_updated_at_lt_closed_at_violations"].append({
                            "id": tid,
                            "updated_at": analysis.get("parsed_updated_at"),
                            "closed_at": analysis.get("parsed_closed_at"),
                        })
                    else:
                        result["cumulative"]["updated_at_relationship_unknown_count"] += 1
                else:
                    result["cumulative"]["closed_invalid_or_missing_closed_at_count"] += 1

                if analysis.get("in_local_window"):
                    result["cumulative"]["closed_no_tags_in_aug_1_through_aug_3_count"] += 1
                    page_info["closed_no_tags_in_window_count"] += 1

            if analysis.get("empty_tags"):
                result["cumulative"]["empty_tags_count"] += 1

            # Collect up to 3 safe samples from matching records
            if (
                analysis.get("in_local_window")
                and sample_count < 3
            ):
                result["samples"].append(_safe_ticket_sample(ticket))
                sample_count += 1

        page_info["state_after"] = state
        result["pages"].append(page_info)

        # Stop on sort violation
        if sort_violation_found:
            result["stop_reason"] = (
                f"sort violation detected on page {page_num} — "
                "status early-stop not safe, stopping immediately"
            )
            result["verdict"] = VERDICT_SORT_FAILED
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # State-based stop: if we entered AFTER_CLOSED_BLOCK, stop
        if state == AFTER_CLOSED_BLOCK:
            result["stop_reason"] = (
                f"closed status block exhausted on page {page_num} — "
                "status dropped below 5, stopped after processing this page"
            )
            result["verdict"] = _determine_verdict(
                state, result["cumulative"]["sort_violations"],
                page_num, result["requests_made"]
            )
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # No next-page Link → dataset exhausted
        if not link["link_indicates_next_page"]:
            result["stop_reason"] = (
                f"no next-page Link on page {page_num} — dataset exhausted"
            )
            result["verdict"] = _determine_verdict(
                state, result["cumulative"]["sort_violations"],
                page_num, result["requests_made"]
            )
            result["final_state"] = state
            result["reported_units_used_sum"] = reported_units_used_sum
            result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
            result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
            return result

        # Carry last status to next page for cross-page sort check
        prev_page_last_status = page_info["last_status"]

    # Reached MAX_PAGES
    result["stop_reason"] = (
        f"reached safety cap of {MAX_PAGES} pages while state={state}"
    )
    result["verdict"] = _determine_verdict(
        state, result["cumulative"]["sort_violations"],
        MAX_PAGES, result["requests_made"]
    )
    result["final_state"] = state
    result["reported_units_used_sum"] = reported_units_used_sum
    result["cumulative"]["unique_ticket_ids"] = len(seen_ids)
    result["cumulative"]["duplicate_ticket_ids"] = len(duplicate_ids)
    return result


def _determine_verdict(
    state: str, sort_violations: int, last_page: int, requests_made: int
) -> str:
    """Determine the final verdict based on probe end state."""
    if sort_violations > 0:
        return VERDICT_SORT_FAILED
    if state == AFTER_CLOSED_BLOCK:
        return VERDICT_PASS
    if state == IN_CLOSED_BLOCK and last_page >= MAX_PAGES:
        return VERDICT_INCOMPLETE
    if state == BEFORE_CLOSED_BLOCK and last_page >= MAX_PAGES:
        return VERDICT_INCOMPLETE
    return VERDICT_FAILED


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Guarded closed batch pagination probe (max 15 pages, GET only)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute real HTTP requests (default: dry-run, zero network calls)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    args = parser.parse_args(argv)

    result = run_probe(execute=args.execute)

    output = json.dumps(result, indent=2 if args.pretty else None, default=str)
    print(output)

    if not args.execute:
        return EXIT_DRY_RUN
    if result.get("verdict") == VERDICT_PASS:
        return 0
    if result.get("verdict") in (VERDICT_INCOMPLETE, VERDICT_SORT_FAILED):
        return 0  # completed successfully, just diagnostic outcome
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
