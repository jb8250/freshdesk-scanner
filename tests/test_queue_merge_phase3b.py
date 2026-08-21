"""Focused, offline tests for the Phase 3B pure queue merge engine."""
from __future__ import annotations

import builtins
import copy
import random
import socket

import pytest

from queue_merge import (
    QueueDuplicateTicketError,
    QueueMergeValidationError,
    merge_queue_tickets,
)


OLD = "2025-04-08T10:00:00Z"
CURRENT = "2026-08-20T10:00:00Z"
NEW = "2026-08-21T10:00:00+00:00"


def ticket(ticket_id, updated_at=CURRENT, **extra):
    return {"id": ticket_id, "updated_at": updated_at, "status": 2,
            "subject": f"photo {ticket_id}", "tags": ["open"], **extra}


def ids(result):
    return [row["id"] for row in result.tickets]


def test_empty_collections_merge_to_empty_result():
    result = merge_queue_tickets([], [])
    assert result.tickets == []
    assert result.metrics.merged_count == 0


def test_existing_only_is_preserved_and_sorted():
    result = merge_queue_tickets([ticket(3), ticket(1)], [])
    assert ids(result) == [1, 3]
    assert result.metrics.unchanged_count == 2


def test_incoming_only_added_and_sorted():
    result = merge_queue_tickets([], [ticket(3), ticket(1)])
    assert ids(result) == [1, 3]
    assert result.metrics.added_count == 2


def test_untouched_existing_and_new_incoming_are_preserved():
    result = merge_queue_tickets([ticket(n) for n in range(1, 6)], [ticket(4, NEW), ticket(5, NEW), ticket(6)])
    assert ids(result) == [1, 2, 3, 4, 5, 6]
    assert len(ids(result)) == len(set(ids(result)))


def test_newer_same_id_replaces_existing():
    result = merge_queue_tickets([ticket(1, OLD, subject="old")], [ticket(1, NEW, subject="new")])
    assert result.tickets[0]["subject"] == "new"
    assert result.metrics.updated_count == 1


def test_equal_timestamp_uses_incoming_authoritatively():
    result = merge_queue_tickets([ticket(1, CURRENT, subject="cached")], [ticket(1, CURRENT, subject="fresh")])
    assert result.tickets[0]["subject"] == "fresh"
    assert result.metrics.updated_count == 1


def test_older_same_id_preserves_existing():
    result = merge_queue_tickets([ticket(1, NEW, subject="cached")], [ticket(1, OLD, subject="older")])
    assert result.tickets[0]["subject"] == "cached"
    assert result.metrics.older_incoming_ignored_count == 1


def test_identical_incoming_duplicates_are_coalesced():
    duplicate = ticket(1)
    result = merge_queue_tickets([], [duplicate, copy.deepcopy(duplicate)])
    assert ids(result) == [1]
    assert result.metrics.identical_duplicate_count == 1


@pytest.mark.parametrize("second", [ticket(1, NEW), ticket(1, CURRENT, subject="different")])
def test_conflicting_incoming_duplicates_fail_closed(second):
    with pytest.raises(QueueDuplicateTicketError):
        merge_queue_tickets([], [ticket(1), second])


def test_existing_duplicates_fail_closed():
    with pytest.raises(QueueDuplicateTicketError):
        merge_queue_tickets([ticket(1), ticket(1)], [])


@pytest.mark.parametrize("bad_id", [None, "1", 0, -1, True])
def test_invalid_incoming_ids_fail_closed(bad_id):
    with pytest.raises(QueueMergeValidationError):
        merge_queue_tickets([], [{"id": bad_id, "updated_at": CURRENT}])


@pytest.mark.parametrize("bad_timestamp", [None, "", "bad", "2026-08-20T10:00:00"])
def test_invalid_incoming_timestamps_fail_closed(bad_timestamp):
    with pytest.raises(QueueMergeValidationError):
        merge_queue_tickets([], [{"id": 1, "updated_at": bad_timestamp}])


def test_existing_malformed_record_and_comparison_timestamp_fail_safely():
    with pytest.raises(QueueMergeValidationError):
        merge_queue_tickets([{"id": "bad"}], [])
    with pytest.raises(QueueMergeValidationError):
        merge_queue_tickets([{"id": 1, "updated_at": "bad"}], [ticket(1)])


@pytest.mark.parametrize("existing,incoming", [({}, []), ([], {}), ([], [None])])
def test_invalid_collection_shapes_fail_closed(existing, incoming):
    with pytest.raises(QueueMergeValidationError):
        merge_queue_tickets(existing, incoming)


def test_inputs_and_nested_data_do_not_mutate_or_alias_result():
    existing = [ticket(1, tags=["cached"], custom_fields={"nested": [1]})]
    incoming = [ticket(2, tags=["new"], custom_fields={"nested": [2]})]
    before_existing, before_incoming = copy.deepcopy(existing), copy.deepcopy(incoming)
    result = merge_queue_tickets(existing, incoming)
    assert existing == before_existing
    assert incoming == before_incoming
    result.tickets[0]["tags"].append("changed")
    result.tickets[1]["custom_fields"]["nested"].append(3)
    assert existing == before_existing
    assert incoming == before_incoming


def test_100_existing_and_three_incoming_does_not_shrink_cache():
    existing = [ticket(n, OLD) for n in range(1, 101)]
    result = merge_queue_tickets(existing, [ticket(98, NEW), ticket(99, NEW), ticket(100, NEW)])
    assert result.metrics.merged_count == 100
    assert ids(result) == list(range(1, 101))


def test_500_day_old_untouched_ticket_remains_without_retention():
    result = merge_queue_tickets([ticket(1, "2025-04-08T10:00:00Z")], [ticket(2)])
    assert ids(result) == [1, 2]


def test_metrics_are_complete_and_correct():
    result = merge_queue_tickets(
        [ticket(1, CURRENT), ticket(2, NEW), ticket(3, CURRENT)],
        [ticket(1, NEW, subject="changed"), ticket(2, OLD), ticket(4), ticket(4)],
    )
    assert result.metrics.existing_count == 3
    assert result.metrics.incoming_count == 4
    assert result.metrics.merged_count == 4
    assert result.metrics.added_count == 1
    assert result.metrics.updated_count == 1
    assert result.metrics.unchanged_count == 2
    assert result.metrics.older_incoming_ignored_count == 1
    assert result.metrics.identical_duplicate_count == 1


def test_merge_performs_no_file_network_or_sqlite_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure merge attempted prohibited I/O")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = merge_queue_tickets([ticket(1)], [ticket(2)])
    assert ids(result) == [1, 2]


def test_generated_invariants_are_deterministic():
    randomizer = random.Random(34802)
    for _ in range(30):
        existing_ids = randomizer.sample(range(1, 300), 40)
        incoming_ids = randomizer.sample(range(1, 300), 20)
        existing = [ticket(value, NEW if value % 2 else CURRENT) for value in existing_ids]
        incoming = [ticket(value, OLD if value % 2 else NEW) for value in incoming_ids]
        before_existing, before_incoming = copy.deepcopy(existing), copy.deepcopy(incoming)
        result = merge_queue_tickets(existing, incoming)
        output = {row["id"]: row for row in result.tickets}
        assert len(output) == result.metrics.merged_count
        assert set(existing_ids) <= set(output)
        assert set(incoming_ids) - set(existing_ids) <= set(output)
        for row in existing:
            if row["id"] in output and row["id"] in incoming_ids and row["id"] % 2:
                assert output[row["id"]]["updated_at"] == NEW
        assert existing == before_existing
        assert incoming == before_incoming
