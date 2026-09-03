"""Pure, fail-closed retention policy for validated queue ticket objects.

Phase 3D1 defines policy only.  This module has no application, database,
filesystem, environment, logging, or network dependencies and is not wired
into Refresh Tickets.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from typing import Any


CANONICAL_REVIEW_STATES = frozenset({
    "Unreviewed",
    "Opened / In Review",
    "Needs Supervisor Review",
    "Resolved",
    "No Action",
    "Not Applicable to Me",
    "No Action Needed",
    "Needs Follow-Up",
})
ACTIVE_EXCEPTION_STATES = frozenset({
    "Opened / In Review",
    "Needs Follow-Up",
    "Needs Supervisor Review",
})


class QueueRetentionValidationError(ValueError):
    """Raised when retention cannot safely classify the complete input."""


@dataclass(frozen=True)
class QueueRetentionMetrics:
    input_count: int
    retained_count: int
    pruned_count: int
    retained_within_window_count: int
    retained_active_exception_count: int
    pruned_expired_count: int
    closed_within_window_retained_count: int
    active_beyond_window_retained_count: int


@dataclass(frozen=True)
class QueueRetentionResult:
    tickets: list[dict[str, Any]]
    metrics: QueueRetentionMetrics


def _valid_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise QueueRetentionValidationError(f"{label} must be a non-empty ISO-8601 string")
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise QueueRetentionValidationError(f"{label} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QueueRetentionValidationError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validate_inputs(
    tickets: Any, review_states: Any, reference_time: Any, retention_days: Any
) -> tuple[list[tuple[dict[str, Any], int, datetime, str | None]], datetime]:
    if not isinstance(tickets, list):
        raise QueueRetentionValidationError("tickets must be a list")
    if not isinstance(review_states, Mapping):
        raise QueueRetentionValidationError("review_states must be a mapping")
    if isinstance(reference_time, datetime):
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise QueueRetentionValidationError("reference_time must be timezone-aware")
        reference_utc = reference_time.astimezone(timezone.utc)
    elif isinstance(reference_time, str):
        reference_utc = _parse_timestamp(reference_time, "reference_time")
    else:
        raise QueueRetentionValidationError("reference_time must be a timezone-aware datetime or ISO-8601 string")
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days <= 0:
        raise QueueRetentionValidationError("retention_days must be a positive integer")

    checked_states: dict[int, str | None] = {}
    for ticket_id, state in review_states.items():
        if not _valid_id(ticket_id):
            raise QueueRetentionValidationError("review state keys must be positive integer IDs")
        if state is not None and (not isinstance(state, str) or state not in CANONICAL_REVIEW_STATES):
            raise QueueRetentionValidationError("review state contains an unknown or invalid state")
        checked_states[ticket_id] = state

    checked_tickets: list[tuple[dict[str, Any], int, datetime, str | None]] = []
    seen: set[int] = set()
    for ticket in tickets:
        if not isinstance(ticket, dict):
            raise QueueRetentionValidationError("each ticket must be a dictionary")
        ticket_id = ticket.get("id")
        if not _valid_id(ticket_id):
            raise QueueRetentionValidationError("ticket id must be a positive integer")
        if ticket_id in seen:
            raise QueueRetentionValidationError("duplicate ticket IDs are not safe to retain")
        seen.add(ticket_id)
        if "updated_at" not in ticket:
            raise QueueRetentionValidationError("ticket updated_at is required")
        updated_at = _parse_timestamp(ticket["updated_at"], "ticket updated_at")
        checked_tickets.append((ticket, ticket_id, updated_at, checked_states.get(ticket_id)))
    return checked_tickets, reference_utc


def apply_queue_retention(
    tickets: Any,
    review_states: Any,
    *,
    reference_time: Any,
    retention_days: int = 60,
) -> QueueRetentionResult:
    """Return a stable, deep-copied filter result under the rolling policy.

    All inputs are validated before any ticket is classified.  A validation
    error therefore never produces a partially successful destructive result.
    """
    checked, reference_utc = _validate_inputs(tickets, review_states, reference_time, retention_days)
    cutoff = reference_utc - timedelta(days=retention_days)
    retained: list[dict[str, Any]] = []
    within = active_exception = pruned_expired = closed_within = active_beyond = 0

    for ticket, _ticket_id, updated_at, state in checked:
        is_within = updated_at >= cutoff
        is_active_exception = not is_within and state in ACTIVE_EXCEPTION_STATES
        if is_within or is_active_exception:
            retained.append(deepcopy(ticket))
            if is_within:
                within += 1
                if ticket.get("status") == 5:
                    closed_within += 1
            else:
                active_exception += 1
                active_beyond += 1
        else:
            pruned_expired += 1
    metrics = QueueRetentionMetrics(
        input_count=len(tickets), retained_count=len(retained),
        pruned_count=pruned_expired,
        retained_within_window_count=within,
        retained_active_exception_count=active_exception,
        pruned_expired_count=pruned_expired,
        closed_within_window_retained_count=closed_within,
        active_beyond_window_retained_count=active_beyond,
    )
    return QueueRetentionResult(tickets=retained, metrics=metrics)


# Descriptive aliases for callers that prefer the conceptual names.
apply_queue_retention_policy = apply_queue_retention
QueueRetentionError = QueueRetentionValidationError

__all__ = [
    "ACTIVE_EXCEPTION_STATES", "CANONICAL_REVIEW_STATES", "QueueRetentionError",
    "QueueRetentionMetrics", "QueueRetentionResult", "QueueRetentionValidationError",
    "apply_queue_retention", "apply_queue_retention_policy",
]
