"""Phase 5B.1 offline shadow comparison for old and unified Closed workflows."""
import sqlite3

import app as scanner_app
from app import (
    ACTIVE_STATES, COMPLETED_STATES, CLOSED_STATUS, closed_review_view_includes,
    has_missing_tags, review_view_includes, ticket_matches_photo_video,
)


def _ticket(ticket_id, status=5, subject="Photo request", tags=None, updated_at="2026-08-20T12:00:00Z"):
    return {"id": ticket_id, "status": status, "subject": subject, "tags": tags, "updated_at": updated_at}


def _old_predicate(ticket, missing_tags_only, photo_video_only):
    return (ticket.get("status") == CLOSED_STATUS
            and (not missing_tags_only or has_missing_tags(ticket))
            and (not photo_video_only or ticket_matches_photo_video(ticket)))


def _new_predicate(ticket, missing_tags_only, photo_video_only):
    # Mirrors the unified /queue?mode=closed local filter contract.
    return (ticket.get("status") == CLOSED_STATUS
            and (not missing_tags_only or has_missing_tags(ticket))
            and (not photo_video_only or ticket_matches_photo_video(ticket)))


def _route(result, view):
    state = {"review_result": result}
    return closed_review_view_includes(state, view), review_view_includes(state, False, view)


def test_shadow_semantic_equivalence_same_record_inputs():
    records = [
        _ticket(1, tags=[]),
        _ticket(2, subject="ordinary", tags=["video request"]),
        _ticket(3, subject="ordinary", tags=[]),
        _ticket(4, status=2, tags=[]),
        _ticket(5, subject="ordinary", tags="malformed"),
    ]
    for record in records:
        for missing_only in (False, True):
            for photo_only in (False, True):
                assert _old_predicate(record, missing_only, photo_only) == _new_predicate(record, missing_only, photo_only)
    for result in ACTIVE_STATES | COMPLETED_STATES:
        for view in ("active", "completed", "all"):
            assert _route(result, view)[0] == _route(result, view)[1]


def test_shadow_snapshot_intersection_classifies_drift_and_new_only():
    old = {
        10: _ticket(10, tags=[]),
        11: _ticket(11, tags=["video request"]),
        12: _ticket(12, status=5, subject="old subject", tags=[]),
        13: _ticket(13, status=5, tags=[]),
    }
    new = {
        10: _ticket(10, tags=[]),
        11: _ticket(11, tags=["video request"]),
        12: _ticket(12, status=2, subject="old subject", tags=[]),  # lifecycle drift
        14: _ticket(14, tags=[]),  # expected 60-day new-only population
    }
    stable = {ticket_id for ticket_id in old.keys() & new.keys() if new[ticket_id]["status"] == CLOSED_STATUS}
    assert stable == {10, 11}
    for ticket_id in stable:
        old_ticket, new_ticket = old[ticket_id], new[ticket_id]
        assert {key: old_ticket[key] for key in ("subject", "tags", "status", "updated_at")} == {key: new_ticket[key] for key in ("subject", "tags", "status", "updated_at")}
        assert has_missing_tags(old_ticket) == has_missing_tags(new_ticket)
        assert ticket_matches_photo_video(old_ticket) == ticket_matches_photo_video(new_ticket)
    status_drift = {ticket_id for ticket_id in old.keys() & new.keys() if old[ticket_id]["status"] == CLOSED_STATUS and new[ticket_id]["status"] != CLOSED_STATUS}
    assert status_drift == {12}
    assert set(new) - set(old) == {14}
    assert set(old) - set(new) == {13}


def test_disposable_closed_state_migration_is_non_overwriting_and_preserves_source(tmp_path):
    db = tmp_path / "shadow.sqlite3"
    scanner_app.init_db(str(db))
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO review_state (ticket_id, review_result, created_at, modified_at) VALUES (1, 'Resolved', 'a', 'a')")
        for ticket_id, result in ((2, "Unreviewed"), (3, "Needs Follow-Up"), (4, "Needs Supervisor Review"), (5, "Resolved"), (6, "No Action Needed"), (7, "Not Applicable to Me"), (8, "Opened / In Review")):
            conn.execute("INSERT INTO closed_review_state (ticket_id, review_result, created_at, modified_at) VALUES (?, ?, 'a', 'a')", (ticket_id, result))
        starting_review = conn.execute("SELECT count(*) FROM review_state").fetchone()[0]
        starting_closed = conn.execute("SELECT count(*) FROM closed_review_state").fetchone()[0]
        overlap = conn.execute("SELECT count(*) FROM review_state r JOIN closed_review_state c ON c.ticket_id = r.ticket_id").fetchone()[0]
        conn.execute("""INSERT INTO review_state (ticket_id, review_result, first_opened_at, last_opened_at, last_review_change_at, reviewed_updated_at, note, created_at, modified_at)
                        SELECT ticket_id, review_result, first_opened_at, last_opened_at, last_review_change_at, reviewed_updated_at, note, created_at, modified_at
                        FROM closed_review_state c WHERE NOT EXISTS (SELECT 1 FROM review_state r WHERE r.ticket_id = c.ticket_id)""")
        assert (starting_review, starting_closed, overlap) == (1, 7, 0)
        assert conn.execute("SELECT count(*) FROM review_state").fetchone()[0] == 8
        assert conn.execute("SELECT count(*) FROM closed_review_state").fetchone()[0] == 7
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
