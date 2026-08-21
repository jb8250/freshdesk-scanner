"""Pure, fail-closed ticket merge logic for future queue refreshes.

This module deliberately has no cache, database, logging, or network dependency.
It is not connected to the production Refresh Tickets flow in Phase 3B.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class QueueMergeValidationError(ValueError):
    """Raised when a ticket collection cannot be merged safely."""


class QueueDuplicateTicketError(QueueMergeValidationError):
    """Raised for duplicate ticket IDs that make a merge ambiguous."""


@dataclass(frozen=True)
class QueueMergeMetrics:
    existing_count: int
    incoming_count: int
    merged_count: int
    added_count: int
    updated_count: int
    unchanged_count: int
    older_incoming_ignored_count: int
    identical_duplicate_count: int


@dataclass(frozen=True)
class QueueMergeResult:
    tickets: list[dict[str, Any]]
    metrics: QueueMergeMetrics


def _parse_updated_at(value: Any) -> datetime:
    """Parse one required offset-aware ISO-8601 ticket timestamp."""
    if not isinstance(value, str) or not value:
        raise QueueMergeValidationError("ticket updated_at must be a non-empty ISO-8601 string")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise QueueMergeValidationError("ticket updated_at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QueueMergeValidationError("ticket updated_at must be timezone-aware")
    return parsed


def _ticket_id(ticket: dict[str, Any]) -> int:
    ticket_id = ticket.get("id")
    if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
        raise QueueMergeValidationError("ticket id must be a positive integer")
    return ticket_id


def _validate_collection(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise QueueMergeValidationError(f"{name} tickets must be a list")
    for ticket in value:
        if not isinstance(ticket, dict):
            raise QueueMergeValidationError(f"{name} ticket records must be dictionaries")
    return value


def merge_queue_tickets(existing_tickets: Any, incoming_tickets: Any) -> QueueMergeResult:
    """Return a deterministic, non-aliasing merge of cached and retrieved tickets.

    Existing IDs are validated for safe identity mapping and must be unique.
    Their timestamps are parsed only when an incoming record for the same ID
    requires ordering comparison. Incoming records always require an aware
    ``updated_at`` timestamp. Non-identical incoming duplicate IDs fail closed;
    identical duplicates are coalesced. Output is sorted by ascending ticket ID.
    """
    existing = _validate_collection(existing_tickets, "existing")
    incoming = _validate_collection(incoming_tickets, "incoming")

    existing_by_id: dict[int, dict[str, Any]] = {}
    for ticket in existing:
        ticket_id = _ticket_id(ticket)
        if ticket_id in existing_by_id:
            raise QueueDuplicateTicketError(f"duplicate existing ticket id: {ticket_id}")
        existing_by_id[ticket_id] = ticket

    incoming_by_id: dict[int, tuple[dict[str, Any], datetime]] = {}
    identical_duplicate_count = 0
    for ticket in incoming:
        ticket_id = _ticket_id(ticket)
        updated_at = _parse_updated_at(ticket.get("updated_at"))
        prior = incoming_by_id.get(ticket_id)
        if prior is not None:
            if ticket != prior[0]:
                raise QueueDuplicateTicketError(f"conflicting incoming ticket id: {ticket_id}")
            identical_duplicate_count += 1
            continue
        incoming_by_id[ticket_id] = (ticket, updated_at)

    merged: dict[int, dict[str, Any]] = {ticket_id: deepcopy(ticket) for ticket_id, ticket in existing_by_id.items()}
    added_count = updated_count = unchanged_count = older_incoming_ignored_count = 0

    for ticket_id, (incoming_ticket, incoming_updated_at) in incoming_by_id.items():
        existing_ticket = existing_by_id.get(ticket_id)
        if existing_ticket is None:
            merged[ticket_id] = deepcopy(incoming_ticket)
            added_count += 1
            continue

        existing_updated_at = _parse_updated_at(existing_ticket.get("updated_at"))
        if incoming_updated_at < existing_updated_at:
            older_incoming_ignored_count += 1
            unchanged_count += 1
            continue
        merged[ticket_id] = deepcopy(incoming_ticket)
        if incoming_ticket == existing_ticket:
            unchanged_count += 1
        else:
            updated_count += 1

    # Existing records untouched by incoming retrieval remain unchanged.
    unchanged_count += len(set(existing_by_id) - set(incoming_by_id))
    tickets = [merged[ticket_id] for ticket_id in sorted(merged)]
    return QueueMergeResult(
        tickets=tickets,
        metrics=QueueMergeMetrics(
            existing_count=len(existing),
            incoming_count=len(incoming),
            merged_count=len(tickets),
            added_count=added_count,
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            older_incoming_ignored_count=older_incoming_ignored_count,
            identical_duplicate_count=identical_duplicate_count,
        ),
    )
