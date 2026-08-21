"""Focused Phase 3D1 tests for the isolated queue retention policy."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from queue_retention import (
    CANONICAL_REVIEW_STATES,
    QueueRetentionValidationError,
    apply_queue_retention,
)

REF = "2026-08-21T10:00:00Z"
CUTOFF = "2026-06-22T10:00:00Z"
ACTIVE = ["Opened / In Review", "Needs Follow-Up", "Needs Supervisor Review"]
NON_ACTIVE = ["Resolved", "No Action Needed", "Not Applicable to Me", "Unreviewed"]


def ticket(ticket_id=1, updated_at=CUTOFF, status=2, **extra):
    return {"id": ticket_id, "updated_at": updated_at, "status": status,
            "subject": f"ticket {ticket_id}", "tags": ["photo"],
            "custom_fields": {"nested": [ticket_id]}, **extra}


def ids(result):
    return [row["id"] for row in result.tickets]


def run(rows, states=None, **kwargs):
    return apply_queue_retention(rows, states or {}, reference_time=REF, **kwargs)


def invalid(call):
    with pytest.raises(QueueRetentionValidationError):
        call()


def test_boundary_after_cutoff_is_retained():
    assert ids(run([ticket(updated_at="2026-06-22T10:00:01Z")])) == [1]


def test_exact_cutoff_is_retained_inclusive():
    assert ids(run([ticket()])) == [1]


def test_one_second_before_cutoff_is_pruned():
    result = run([ticket(updated_at="2026-06-22T09:59:59Z")])
    assert result.tickets == []
    assert result.metrics.pruned_expired_count == 1


@pytest.mark.parametrize("state", ACTIVE)
def test_each_active_state_protects_expired_ticket(state):
    assert ids(run([ticket(updated_at="2026-06-21T10:00:00Z")], {1: state})) == [1]


@pytest.mark.parametrize("state", NON_ACTIVE)
def test_each_non_active_state_does_not_protect_expired_ticket(state):
    assert run([ticket(updated_at="2026-06-21T10:00:00Z")], {1: state}).tickets == []


def test_missing_review_row_is_ordinary():
    assert run([ticket(updated_at="2026-06-21T10:00:00Z")]).tickets == []


def test_closed_within_window_is_retained():
    result = run([ticket(updated_at="2026-08-16T10:00:00Z", status=5)])
    assert ids(result) == [1]
    assert result.metrics.closed_within_window_retained_count == 1


def test_closed_at_cutoff_is_retained():
    assert ids(run([ticket(status=5)])) == [1]


def test_closed_expired_without_active_state_is_pruned():
    assert run([ticket(updated_at="2026-06-21T10:00:00Z", status=5)]).tickets == []


def test_closed_expired_active_follow_up_is_retained():
    assert ids(run([ticket(updated_at="2026-05-23T10:00:00Z", status=5)], {1: "Needs Follow-Up"})) == [1]


def test_status_five_is_not_immediate_prune_or_permanent_retention():
    assert ids(run([ticket(updated_at="2026-08-20T10:00:00Z", status=5)])) == [1]
    assert run([ticket(updated_at="2026-01-01T10:00:00Z", status=5)]).tickets == []


def test_offset_timestamp_compares_by_instant_and_preserves_text():
    original = ticket(updated_at="2026-06-22T12:00:00+02:00")
    result = run([original])
    assert ids(result) == [1]
    assert result.tickets[0]["updated_at"] == original["updated_at"]


def test_reference_offset_is_normalized_by_instant():
    result = apply_queue_retention([ticket()], {}, reference_time="ignored") if False else apply_queue_retention(
        [ticket(updated_at="2026-06-22T10:00:00Z")], {},
        reference_time=datetime(2026, 8, 21, 12, 0, tzinfo=timezone(timedelta(hours=2))), retention_days=60)
    assert ids(result) == [1]


def test_order_is_stable():
    rows = [ticket(3), ticket(1, "2026-01-01T00:00:00Z"), ticket(2)]
    assert ids(run(rows)) == [3, 2]


def test_metrics_reconcile_and_classify():
    rows = [ticket(1), ticket(2, "2026-01-01T00:00:00Z"), ticket(3, "2026-01-01T00:00:00Z")]
    result = run(rows, {3: "Needs Follow-Up"})
    m = result.metrics
    assert (m.input_count, m.retained_count, m.pruned_count) == (3, 2, 1)
    assert m.retained_within_window_count == 1
    assert m.retained_active_exception_count == 1
    assert m.active_beyond_window_retained_count == 1
    assert m.pruned_expired_count == 1
    assert m.input_count == m.retained_count + m.pruned_count


def test_empty_input():
    result = run([])
    assert result.tickets == [] and result.metrics.input_count == 0

@pytest.mark.parametrize("value", [None, {}, (), "bad"])
def test_top_level_tickets_must_be_list(value):
    invalid(lambda: apply_queue_retention(value, {}, reference_time=REF))


def test_ticket_must_be_dict(): invalid(lambda: run([None]))
def test_missing_id(): invalid(lambda: run([{"updated_at": REF}]))
@pytest.mark.parametrize("value", [0, -1, True, False, "1", None])
def test_id_must_be_positive_non_bool_integer(value): invalid(lambda: run([ticket(id=value)]))
def test_duplicate_ids_fail(): invalid(lambda: run([ticket(), ticket()]))
def test_missing_updated_at(): invalid(lambda: run([{"id": 1}]))
@pytest.mark.parametrize("value", ["", None, "not-a-date", "2026-06-22"])
def test_updated_at_must_be_valid_aware_iso(value): invalid(lambda: run([ticket(updated_at=value)]))
def test_naive_reference_fails(): invalid(lambda: apply_queue_retention([ticket()], {}, reference_time=datetime(2026, 8, 21, 10)))
@pytest.mark.parametrize("days", [0, -1, True, False, 1.5, "60"])
def test_retention_days_must_be_positive_integer(days): invalid(lambda: run([ticket()], retention_days=days))
@pytest.mark.parametrize("states", [None, [], "states"])
def test_review_states_must_be_mapping(states): invalid(lambda: apply_queue_retention([ticket()], states, reference_time=REF))
def test_invalid_review_state_key_fails(): invalid(lambda: run([ticket()], {0: "Resolved"}))
def test_invalid_review_state_value_type_fails(): invalid(lambda: run([ticket()], {1: 3}))
def test_unknown_review_state_fails(): invalid(lambda: run([ticket()], {1: "Unknown"}))
def test_null_review_state_is_allowed_but_non_active(): assert run([ticket(updated_at="2026-01-01T00:00:00Z")], {1: None}).tickets == []

def test_validation_is_fail_closed_without_partial_result():
    rows = [ticket(1), ticket(2, "bad")]
    with pytest.raises(QueueRetentionValidationError):
        run(rows)


def test_output_and_input_are_deeply_independent():
    rows = [ticket()]
    states = {1: "Resolved"}
    result = run(rows, states)
    result.tickets[0]["custom_fields"]["nested"].append("output")
    rows[0]["tags"].append("input")
    assert rows[0]["custom_fields"]["nested"] == [1]
    assert result.tickets[0]["tags"] == ["photo"]
    assert states == {1: "Resolved"}


def test_repeated_calls_are_deterministic():
    rows = [ticket(1), ticket(2, "2026-01-01T00:00:00Z")]
    first, second = run(rows), run(rows)
    assert first == second


def test_realistic_100_ticket_mix_retains_48():
    rows = [ticket(i, "2026-08-01T00:00:00Z") for i in range(1, 31)]
    states = {}
    for i in range(31, 101):
        rows.append(ticket(i, "2026-01-01T00:00:00Z"))
    for i in range(31, 41): states[i] = "Needs Follow-Up"
    for i in range(41, 46): states[i] = "Opened / In Review"
    for i in range(46, 49): states[i] = "Needs Supervisor Review"
    assert len(run(rows, states).tickets) == 48


def test_existing_old_active_object_is_preserved_for_future_merge_invariant():
    assert ids(run([ticket(123, "2026-05-23T00:00:00Z")], {123: "Needs Follow-Up"})) == [123]


def test_canonical_audit_set_is_exact():
    assert CANONICAL_REVIEW_STATES == frozenset({*ACTIVE, *NON_ACTIVE})
