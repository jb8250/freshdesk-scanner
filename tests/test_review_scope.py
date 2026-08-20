"""Phase 1 — Default Review Scope tests.

Covers the two visible scope layers added to the default working review queue:

  1. Photo/video subjects only (subject-field keyword match, word-boundary,
     case-insensitive; "Vids" added to the existing keyword family).
  2. Hide tickets with reviewed/closed tags (the six REVIEWED_EXCLUSION_TAGS,
     case/whitespace insensitive comparison, never mutating stored tags).

Also proves the escape hatches (Show All Cached Tickets / Reset to Default
Review Scope), URL-backed state, manual-filter independence, and the
zero-network guarantees. Everything runs through the autouse conftest network
blocker (any requests.* call fails loudly).
"""
import json
import os
import re

import pytest
import requests

import app as A
from app import (
    DEFAULT_FILTERS,
    KEYWORDS,
    REVIEWED_EXCLUSION_TAGS,
    apply_queue_filters,
    filter_query_string,
    filters_from_args,
    has_reviewed_exclusion_tag,
    keyword_filter_hits,
    normalized_ticket_tags,
    passes_filters,
    passes_review_scope,
    subject_matches_photo_video,
)
from werkzeug.datastructures import MultiDict

SCOPE_ON = dict(DEFAULT_FILTERS)
SCOPE_OFF = dict(DEFAULT_FILTERS, photo_video_only=False, hide_reviewed_tags=False)


def _ticket(**over):
    base = {
        "id": 777001,
        "subject": "Customer sent photo of damage",
        "status": 2,
        "priority": 3,
        "due_by": "2020-06-15T17:00:00Z",
        "created_at": "2026-07-01T10:00:00Z",
        "updated_at": "2026-07-31T12:00:00Z",
        "tags": [],
        "type": "Complaint",
    }
    base.update(over)
    return base


def _fake_get(monkeypatch):
    """Counting requests.get recorder that raises on any real attempt."""
    state = {"calls": 0}

    def blocked(url, *args, **kwargs):
        state["calls"] += 1
        raise AssertionError(f"NETWORK BLOCKED: unexpected request to {url}")

    monkeypatch.setattr(requests, "get", blocked)
    return state


def _ids(text):
    return sorted(set(re.findall(r'data-ticket-id="(5000\d\d)"', text)))


# ---------------------------------------------------------------------------
# Keyword family
# ---------------------------------------------------------------------------


def test_keyword_family_contains_all_expected_terms():
    expected = {
        "photo", "photos", "picture", "pictures", "pic", "pics",
        "video", "videos", "vid", "vids",
    }
    assert set(KEYWORDS) == expected


@pytest.mark.parametrize("subject", [
    "Photo request", "Photos request", "Picture request", "Pictures request",
    "Pic request", "Pics request", "Video request", "Videos request",
    "Vid request", "Vids request",
])
def test_subject_matches_each_keyword(subject):
    assert subject_matches_photo_video(_ticket(subject=subject)) is True


@pytest.mark.parametrize("subject", [
    "PHOTO/video request", "photo REQUEST", "VIDEO/photo request",
    "Need PHOTOS", "Need PICTURES", "CUSTOMER PICS", "PICS REQUESTED",
    "NEED VIDEO", "SEND VIDEOS", "NEED VID", "NEED VIDS",
])
def test_subject_matches_mixed_case(subject):
    assert subject_matches_photo_video(_ticket(subject=subject)) is True


def test_subject_word_boundary_preserved():
    # Exact keyword as its own word matches; longer unrelated words do not.
    assert subject_matches_photo_video(_ticket(subject="Need vids today")) is True
    assert subject_matches_photo_video(_ticket(subject="send a vid now")) is True
    assert subject_matches_photo_video(_ticket(subject="Photography skills needed")) is False
    assert subject_matches_photo_video(_ticket(subject="Videography equipment needed")) is False
    assert subject_matches_photo_video(_ticket(subject="provide identification")) is False
    assert subject_matches_photo_video(_ticket(subject="picturesque delivery")) is False
    assert keyword_filter_hits("vendor") is False


def test_subject_missing_or_none_fails_safely():
    assert subject_matches_photo_video({}) is False
    assert subject_matches_photo_video(_ticket(subject=None)) is False
    assert subject_matches_photo_video(_ticket(subject=12345)) is False
    assert passes_review_scope(_ticket(subject=None), SCOPE_ON) is False


def test_subject_field_only_never_inspects_other_fields():
    # A non-photo subject with photo words in body/notes/conversations must
    # NOT match: the scope rule inspects SUBJECT only.
    ticket = _ticket(subject="Update on table delivery", body="please see photo",
                     description="customer sent videos", note="pics attached")
    assert subject_matches_photo_video(ticket) is False
    assert passes_review_scope(ticket, SCOPE_ON) is False


# ---------------------------------------------------------------------------
# Reviewed/closed tag exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", [
    "parts needed", "Exchange", "No Service Needed", "Closed",
    "Schedule Service", "delivery special needed",
])
def test_each_exclusion_tag_hides(tag):
    assert has_reviewed_exclusion_tag(_ticket(tags=[tag])) is True
    assert passes_review_scope(_ticket(tags=[tag]), SCOPE_ON) is False


def test_exclusion_set_exactly_six_values():
    assert REVIEWED_EXCLUSION_TAGS == frozenset({
        "parts needed",
        "exchange",
        "no service needed",
        "closed",
        "schedule service",
        "delivery special needed",
    })


def test_tag_comparison_case_insensitive():
    for tag in ("CLOSED", "Closed", "CLOSED", "closed"):
        args = []
        assert has_reviewed_exclusion_tag(_ticket(tags=[tag])) is True, tag
    assert has_reviewed_exclusion_tag(_ticket(tags=["EXCHANGE"])) is True
    assert has_reviewed_exclusion_tag(_ticket(tags=["PaRtS nEeDeD"])) is True


def test_tag_comparison_whitespace_insensitive():
    assert has_reviewed_exclusion_tag(_ticket(tags=["  closed  "])) is True
    assert has_reviewed_exclusion_tag(_ticket(tags=["Closed "])) is True
    assert has_reviewed_exclusion_tag(_ticket(tags=["\t schedule service \t"])) is True


def test_one_excluded_tag_among_many_hides():
    ticket = _ticket(subject="Photo/video request",
                     tags=["PHOTOS", "parts needed", "warranty", "email_delivery_failed"])
    assert passes_review_scope(ticket, SCOPE_ON) is False
    assert passes_filters(ticket, SCOPE_ON) is True  # manual layer unaffected


def test_non_excluded_tags_do_not_hide():
    for tags in (["PHOTOS"], ["photo"], ["email_delivery_failed"],
                 ["no guest follow up"], ["PHOTOS", "warranty"]):
        assert passes_review_scope(_ticket(tags=tags), SCOPE_ON) is True, tags


def test_missing_null_and_malformed_tags_do_not_crash():
    assert passes_review_scope(_ticket(subject="Need pics"), SCOPE_ON) is True      # tags key absent
    assert passes_review_scope(_ticket(subject="Need pics", tags=None), SCOPE_ON) is True
    assert passes_review_scope(_ticket(subject="Need pics", tags="notalist"), SCOPE_ON) is True
    assert passes_review_scope(_ticket(subject="Need pics", tags=[42]), SCOPE_ON) is True
    assert has_reviewed_exclusion_tag(_ticket(tags=None)) is False
    assert normalized_ticket_tags(_ticket(tags=None)) == set()
    assert normalized_ticket_tags(_ticket(tags=["closed"])) == {"closed"}


def test_stored_tags_never_mutated():
    ticket = _ticket(tags=["  CLOSED  ", "Exchange"])
    original = ticket["tags"][:]
    passes_review_scope(ticket, SCOPE_ON)
    normalized_ticket_tags(ticket)
    assert ticket["tags"] == original


# ---------------------------------------------------------------------------
# Combined review-scope behavior
# ---------------------------------------------------------------------------


def test_photo_subject_no_exclusion_visible():
    assert passes_review_scope(_ticket(subject="Need pictures", tags=[]), SCOPE_ON) is True


def test_photo_subject_with_exclusion_hidden():
    assert passes_review_scope(_ticket(subject="Need pictures", tags=["closed"]), SCOPE_ON) is False


def test_non_photo_subject_no_exclusion_hidden_by_default_scope():
    assert passes_review_scope(_ticket(subject="Delivery schedule confirmation", tags=[]), SCOPE_ON) is False


def test_non_photo_subject_with_exclusion_hidden():
    assert passes_review_scope(_ticket(subject="Delivery schedule confirmation", tags=["exchange"]), SCOPE_ON) is False


def test_photo_scope_off_tag_scope_on_allows_non_photo():
    cfg = dict(DEFAULT_FILTERS, photo_video_only=False)
    assert passes_review_scope(_ticket(subject="Delivery schedule confirmation", tags=[]), cfg) is True
    assert passes_review_scope(_ticket(subject="Delivery schedule confirmation", tags=["closed"]), cfg) is False


def test_photo_scope_on_tag_scope_off_allows_photo_with_exclusion():
    cfg = dict(DEFAULT_FILTERS, hide_reviewed_tags=False)
    assert passes_review_scope(_ticket(subject="Need pictures", tags=["schedule service"]), cfg) is True


def test_both_scope_controls_off_passes_every_ticket():
    cfg = dict(DEFAULT_FILTERS, photo_video_only=False, hide_reviewed_tags=False)
    for subject, tags in [
        ("Delivery schedule confirmation", []),
        ("Need pictures", ["closed"]),
        ("Anything at all", ["parts needed"]),
        (None, None),
    ]:
        assert passes_review_scope(_ticket(subject=subject, tags=tags), cfg) is True


def test_both_scope_controls_off_complete_cache_via_apply():
    pool = [
        _ticket(id=1, subject="Need pictures", tags=[]),
        _ticket(id=2, subject="Delivery schedule confirmation", tags=[]),
        _ticket(id=3, subject="Invoice for pending order", tags=["closed"]),
    ]
    out = apply_queue_filters(pool, SCOPE_OFF)
    assert [t["id"] for t in out] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Manual filters stay independent (regression under default scope)
# ---------------------------------------------------------------------------


def test_manual_filters_stay_off_by_default():
    cfg = filters_from_args(MultiDict([]))
    assert cfg["overdue"] is False
    assert cfg["responded"] is False
    assert cfg["waiting"] is False
    assert cfg["missing_tags"] is False
    assert cfg["review_view"] == "all"


def test_manual_responded_regression_with_default_scope():
    cfg = dict(DEFAULT_FILTERS, responded=True)
    assert passes_review_scope(_ticket(status=2, subject="Need pics"), cfg) is True
    assert passes_filters(_ticket(status=2, subject="Need pics"), cfg) is True
    assert passes_filters(_ticket(status=6, subject="Need pics"), cfg) is False


def test_manual_waiting_regression_with_default_scope():
    cfg = dict(DEFAULT_FILTERS, waiting=True)
    assert passes_filters(_ticket(status=6, subject="Need pics"), cfg) is True
    assert passes_filters(_ticket(status=2, subject="Need pics"), cfg) is False


def test_manual_responded_plus_waiting_union_with_default_scope():
    cfg = dict(DEFAULT_FILTERS, responded=True, waiting=True)
    assert passes_filters(_ticket(status=2, subject="Need pics"), cfg) is True
    assert passes_filters(_ticket(status=6, subject="Need pics"), cfg) is True
    assert passes_filters(_ticket(status=1, subject="Need pics"), cfg) is False


def test_manual_overdue_regression_with_default_scope():
    cfg = dict(DEFAULT_FILTERS, overdue=True)
    assert passes_filters(_ticket(subject="Need pics", due_by="2020-01-01T00:00:00Z"), cfg) is True
    assert passes_filters(_ticket(subject="Need pics", due_by="2035-01-01T00:00:00Z"), cfg) is False


def test_manual_missing_tags_regression_with_default_scope():
    cfg = dict(DEFAULT_FILTERS, missing_tags=True)
    assert passes_filters(_ticket(subject="Need pics", tags=[]), cfg) is True
    assert passes_filters(_ticket(subject="Need pics", tags=["warranty"]), cfg) is False


def test_manual_scope_intersection_pipeline():
    # Review Scope runs first, then the manual filters narrow the result.
    pool = [
        _ticket(id=1, subject="Need pics", status=2, tags=[]),
        _ticket(id=2, subject="Delivery schedule", status=2, tags=[]),
        _ticket(id=3, subject="Need pics", status=6, tags=[]),
        _ticket(id=4, subject="Need pics", tags=["closed"]),
    ]
    out = apply_queue_filters(pool, dict(DEFAULT_FILTERS, responded=True))
    assert [t["id"] for t in out] == [1]


# ---------------------------------------------------------------------------
# URL state
# ---------------------------------------------------------------------------


def test_initial_url_state_defaults():
    cfg = filters_from_args(MultiDict([]))
    assert cfg["photo_video_only"] is True
    assert cfg["hide_reviewed_tags"] is True
    assert cfg["overdue"] is False
    assert cfg["review_view"] == "all"


def test_url_state_roundtrips_through_canonical_string():
    for state in (SCOPE_ON, SCOPE_OFF):
        cfg = filters_from_args(_args_from(filter_query_string(state)))
        assert cfg == state


def test_show_all_query_string():
    qs = filter_query_string(SCOPE_OFF)
    assert "photo_video_only=0" in qs and "hide_reviewed_tags=0" in qs
    assert "overdue=0&responded=0&waiting=0&missing_tags=0" in qs
    assert "review_view=all" in qs


def _args_from(qs):
    parts = {}
    for pair in qs.split("&"):
        k, _, v = pair.partition("=")
        parts.setdefault(k, []).append(v)
    class _A:
        def getlist(self, key):
            return parts.get(key, [])
    return _A()


# ---------------------------------------------------------------------------
# Navigation / UI (offline fixtures, blocked transport)
# ---------------------------------------------------------------------------


def _html(client, path="/queue"):
    resp = client.get(path)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_initial_get_defaults_scope_on_manual_off(client, monkeypatch):
    state = _fake_get(monkeypatch)
    html = _html(client)
    assert 'name=photo_video_only value=1 checked' in html
    assert 'name=hide_reviewed_tags value=1 checked' in html
    for name in ("overdue", "responded", "waiting", "missing_tags"):
        assert f'name={name} value=1 checked' not in html, name
    assert '<option value=all selected>' in html
    assert "Review Scope" in html
    assert state["calls"] == 0


def test_initial_get_displays_only_default_scope(client):
    ids = _ids(_html(client))
    assert "500006" not in ids  # "Vendor painted..." non-photo subject hidden
    assert "500028" not in ids  # "Delivery schedule confirmation" hidden
    assert "500001" in ids      # "Customer sent photo..." visible
    assert len(ids) == 23


def test_show_all_link_rendered(client):
    html = _html(client)
    assert '>Show All Cached Tickets</a>' in html
    assert 'photo_video_only=0&amp;hide_reviewed_tags=0&amp;overdue=0&amp;responded=0&amp;waiting=0&amp;missing_tags=0' in html
    assert '>Reset to Default Review Scope</a>' in html
    assert 'photo_video_only=1&amp;hide_reviewed_tags=1&amp;overdue=0&amp;responded=0&amp;waiting=0&amp;missing_tags=0' in html


def test_show_all_displays_complete_cache(client, monkeypatch):
    state = _fake_get(monkeypatch)
    html = _html(client, "/queue?photo_video_only=0&hide_reviewed_tags=0&overdue=0&responded=0&waiting=0&missing_tags=0&days=60&review_view=all")
    assert len(_ids(html)) == 28
    assert "Showing: All cached tickets" in html
    assert state["calls"] == 0


def test_reset_restores_default_scope(client, monkeypatch):
    state = _fake_get(monkeypatch)
    html = _html(client, "/queue?photo_video_only=1&hide_reviewed_tags=1&overdue=0&responded=0&waiting=0&missing_tags=0&days=60&review_view=all")
    assert 'name=photo_video_only value=1 checked' in html
    assert 'name=hide_reviewed_tags value=1 checked' in html
    assert len(_ids(html)) == 23
    assert state["calls"] == 0


def test_apply_filters_is_local_only(client, monkeypatch):
    state = _fake_get(monkeypatch)
    html = _html(client, "/queue?photo_video_only=0&hide_reviewed_tags=0&overdue=1&responded=1&waiting=0&missing_tags=0&days=60&review_view=all")
    assert html
    assert state["calls"] == 0


def test_review_scope_toggles_are_local_only(client, monkeypatch):
    state = _fake_get(monkeypatch)
    _html(client, "/queue?photo_video_only=0")
    _html(client, "/queue?hide_reviewed_tags=0")
    _html(client, "/queue?photo_video_only=1&hide_reviewed_tags=0")
    assert state["calls"] == 0


def test_review_view_changes_are_local_only(client, monkeypatch):
    state = _fake_get(monkeypatch)
    for view in ("active", "completed", "all"):
        _html(client, f"/queue?photo_video_only=0&hide_reviewed_tags=0&overdue=0&responded=0&waiting=0&missing_tags=0&days=60&review_view={view}")
    assert state["calls"] == 0


def test_browser_reload_is_local_only(client, monkeypatch):
    state = _fake_get(monkeypatch)
    _html(client)
    _html(client)
    _html(client, "/queue?photo_video_only=0&hide_reviewed_tags=0")
    _html(client, "/queue?photo_video_only=0&hide_reviewed_tags=0")
    assert state["calls"] == 0


def test_status_polling_is_local_only(client, monkeypatch):
    state = _fake_get(monkeypatch)
    resp = client.get("/queue/api/refresh/status")
    assert resp.status_code == 200
    assert state["calls"] == 0


def test_review_view_select_preserved_under_scope(client):
    html = _html(client, "/queue?photo_video_only=0&hide_reviewed_tags=0&overdue=0&responded=0&waiting=0&missing_tags=0&days=60&review_view=active")
    assert '<option value=active selected>' in html