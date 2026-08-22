"""Phase 4B — Photo/Video Review Scope qualifies ticket subject OR tags.

All route coverage is offline and uses conftest's temporary cache/database and
network blocker. No test touches the operator's production state.
"""
import json

import pytest

import app as scanner_app
from app import (
    DEFAULT_FILTERS,
    has_reviewed_exclusion_tag,
    passes_review_scope,
    text_matches_photo_video,
    ticket_matches_photo_video,
)


def _ticket(subject="Ivana davis 3984335285", tags=None, ticket_id=940001):
    ticket = {
        "id": ticket_id,
        "subject": subject,
        "status": 2,
        "priority": 3,
        "due_by": "2026-08-01T12:00:00Z",
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
    }
    if tags is not None:
        ticket["tags"] = tags
    return ticket


@pytest.mark.parametrize("tag", [
    "PHOTOS",
    "Product Issue Video Request",
    "Photo/video request",
    "Photo request",
    "Video/ Photos",
    "Video/ photo request",
    "Video/ Photo",
    "PHOTO REQUEST",
    "Photos requested",
    "Video Request",
    "Video-photo request",
    "Video_photo request",
    "Pictures",
    "VIDS",
])
def test_required_and_reasonable_tag_variations_match(tag):
    assert ticket_matches_photo_video(_ticket(tags=[tag])) is True


@pytest.mark.parametrize("value", [
    "Photography", "Videography", "picturesque", "picnic", "videochat",
])
def test_text_matcher_preserves_word_boundaries(value):
    assert text_matches_photo_video(value) is False


@pytest.mark.parametrize("subject,tags,expected", [
    ("Photo request", [], True),
    ("Ivana davis 3984335285", ["PHOTOS"], True),
    ("Video request", ["PHOTOS"], True),
    ("Ivana davis 3984335285", ["GENERAL"], False),
])
def test_ticket_subject_tag_qualification_matrix(subject, tags, expected):
    assert ticket_matches_photo_video(_ticket(subject, tags)) is expected


@pytest.mark.parametrize("ticket", [
    {},
    {"subject": None},
    {"subject": "Ivana davis 3984335285", "tags": None},
    {"subject": "Ivana davis 3984335285", "tags": []},
    {"subject": "Ivana davis 3984335285", "tags": "PHOTOS"},
    {"subject": "Ivana davis 3984335285", "tags": [None, 4, {}, "GENERAL"]},
    None,
])
def test_malformed_tags_fail_safely(ticket):
    assert ticket_matches_photo_video(ticket) is False


def test_queue_scope_uses_tag_qualification_and_keeps_exclusion_authoritative():
    config = dict(DEFAULT_FILTERS)
    tagged = _ticket(tags=["PHOTOS"])
    assert passes_review_scope(tagged, config) is True

    excluded = _ticket(tags=["PHOTOS", "Schedule Service"])
    assert ticket_matches_photo_video(excluded) is True
    assert has_reviewed_exclusion_tag(excluded) is True
    assert passes_review_scope(excluded, config) is False


def test_offline_queue_real_gap_tag_is_displayed_then_general_is_not(client, monkeypatch, tmp_path):
    cache = tmp_path / "queue_fixture.json"
    monkeypatch.setattr(scanner_app, "FIXTURES_FILE", str(cache))

    matching = _ticket(tags=["PHOTOS"])
    cache.write_text(json.dumps({"pages": [[matching]]}))
    html = client.get("/queue").get_data(as_text=True)
    assert 'data-ticket-id="940001"' in html

    nonmatching = _ticket(tags=["GENERAL"])
    cache.write_text(json.dumps({"pages": [[nonmatching]]}))
    html = client.get("/queue").get_data(as_text=True)
    assert 'data-ticket-id="940001"' not in html


def test_offline_closed_real_gap_tag_is_displayed_then_general_is_not(client, monkeypatch):
    matching = _ticket(tags=["PHOTOS"], ticket_id=940002)
    matching.update({"status": 5, "closed_at": "2026-08-04T09:00:00Z"})
    monkeypatch.setattr(scanner_app, "_synthetic_closed_tickets", lambda: [matching])
    html = client.get("/closed?review_view=all&missing_tags=0").get_data(as_text=True)
    assert 'data-ticket-id="940002"' in html

    nonmatching = dict(matching, tags=["GENERAL"])
    monkeypatch.setattr(scanner_app, "_synthetic_closed_tickets", lambda: [nonmatching])
    html = client.get("/closed?review_view=all&missing_tags=0").get_data(as_text=True)
    assert 'data-ticket-id="940002"' not in html
