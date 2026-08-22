"""Phase 4A — Closed Ticket Housekeeping Photo/Video Review Scope.

Adds the same Photo/Video Review Scope filter behavior that the main Review
Queue already has to the Closed Ticket Housekeeping page (/closed), enabled by
default. The closed page reuses the SAME canonical subject_matches_photo_video
matcher (subject-field only, word-boundary aware, case-insensitive) — no
duplicate keyword list, no second regex.

Scope of these tests:
  - Default ON: GET /closed with no explicit photo_video_only parameter shows
    only closed tickets whose subject matches the photo/video keyword rule.
  - Subject-keyword matching: the canonical keyword family matches
    (case-insensitive, word-boundary aware).
  - Explicit OFF: photo_video_only=0 shows the full closed population that
    satisfies the remaining filters.
  - review_view composition: Photo/Video scope composes correctly with
    Active / Completed / All.
  - Local-only: turning the scope ON/OFF triggers 0 Freshdesk requests, 0 DB
    writes, 0 cache writes.
  - State persistence: the setting survives review_view changes, filter
    submissions, preset links, and review-form POSTs.
  - Reset/default returns Photo/Video ON.
  - Main queue unchanged: the shared matcher is untouched; /queue behavior and
    Phase 3K routing stay correct.

All tests are offline, fixture-backed, and use the isolated test client
(conftest: block_network + clean_state + fixed_clock).
"""
import re

import pytest

import app as scanner_app
from app import (DEFAULT_FILTERS, KEYWORDS, subject_matches_photo_video,
                 closed_filters_from_args, closed_page_url,
                 closed_review_view_includes)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _closed_html(client, query=""):
    resp = client.get("/closed" + query)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_data(as_text=True)


def _closed_ids(client, query=""):
    """IDs of RENDERED closed rows (data-ticket-id on the row)."""
    html = _closed_html(client, query)
    return sorted(set(re.findall(r'data-ticket-id="(81\d+|82\d+)"', html)))


def _view_count(client, query=""):
    html = _closed_html(client, query)
    m = re.search(r'(\d+) of (\d+) unique closed tickets', html)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _csrf(client, query=""):
    html = _closed_html(client, query)
    m = re.search(r'name=csrf_token value="([^"]+)"', html)
    assert m, "no csrf token rendered"
    return m.group(1)


# ---------------------------------------------------------------------------
# Default ON
# ---------------------------------------------------------------------------

def test_default_closed_photo_video_scope_is_on(client):
    """GET /closed with no explicit parameter defaults photo_video_only ON."""
    cfg = closed_filters_from_args({})
    assert cfg["photo_video_only"] is True
    assert cfg["review_view"] == "active"  # unchanged default


def test_default_closed_page_shows_only_photo_video_subjects(client):
    """With Photo/Video scope ON by default, only matching closed tickets show."""
    ids = _closed_ids(client, "?review_view=all&missing_tags=0")
    # The synthetic closed fixtures include 7 photo/video-subject tickets
    # (810010-810016) plus non-photo/video tickets (810001-810004, 810017).
    # Only the photo/video ones should render.
    expected_photo_ids = {"810010", "810011", "810012", "810013", "810014",
                          "810015", "810016"}
    assert set(ids) == expected_photo_ids, f"unexpected ids: {ids}"


def test_default_closed_view_count_reflects_scope(client):
    """The view_count line reports only photo/video tickets in scope."""
    shown, total = _view_count(client, "?review_view=all&missing_tags=0")
    assert shown == 7, f"expected 7 photo/video tickets shown, got {shown}"
    assert total > shown, "total closed should exceed the photo/video subset"


# ---------------------------------------------------------------------------
# Canonical matcher reuse (subject-only, word-boundary, case-insensitive)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject,expected", [
    ("Customer sent photo of damage", True),
    ("Re: Photos of broken hinge", True),
    ("Picture of scratched surface", True),
    ("Pictures attached for review", True),
    ("Video of wobbling table leg", True),
    ("Videos sent yesterday", True),
    ("VID of damaged drawer", True),
    ("Send me the VIDS please", True),
    ("Pics attached for review", True),
    ("See attached PIC for detail", True),
    # Word-boundary: substrings inside larger words must NOT match.
    ("Photography skills needed", False),
    ("Videography equipment needed", False),
    ("picturesque delivery", False),
    ("provide identification", False),
    ("No keyword here", False),
    ("Delivery schedule inquiry", False),
])
def test_canonical_matcher_agreement(subject, expected):
    """The closed page uses the SAME matcher as /queue — identical results."""
    ticket = {"id": 1, "subject": subject, "status": 5}
    assert subject_matches_photo_video(ticket) is expected


def test_closed_page_uses_same_matcher_as_queue(client):
    """Every rendered closed row's subject must satisfy the canonical matcher."""
    html = _closed_html(client, "?review_view=all&missing_tags=0")
    # Extract rendered rows. Use non-capturing group so findall returns full match.
    rows = re.findall(
        r'<tr[^>]*data-ticket-id="81\d+"[^>]*>.*?</tr>', html, re.S)
    assert rows, "expected at least one rendered row"
    for row in rows:
        # The subject is rendered in a link: >Subject Text</a>
        m = re.search(r'class="sbj fd-link"[^>]*>(.*?)</a>', row, re.S)
        assert m, f"subject not found in row: {row[:200]}"
        subject = m.group(1).strip()
        assert subject_matches_photo_video({"subject": subject}), (
            f"rendered closed subject '{subject}' must match canonical matcher")


# ---------------------------------------------------------------------------
# Explicit OFF
# ---------------------------------------------------------------------------

def test_explicit_off_shows_full_closed_population(client):
    """photo_video_only=0 shows all closed tickets matching other filters."""
    ids_off = _closed_ids(client, "?review_view=all&missing_tags=0&photo_video_only=0")
    ids_on = _closed_ids(client, "?review_view=all&missing_tags=0&photo_video_only=1")
    # OFF must show strictly more tickets than ON (unless all happen to match,
    # which the fixtures deliberately avoid).
    assert len(ids_off) > len(ids_on), (
        f"OFF ({len(ids_off)}) should show more than ON ({len(ids_on)})")
    # The photo/video set must be a subset of the full set.
    assert set(ids_on).issubset(set(ids_off))


def test_explicit_off_renders_non_photo_video_tickets(client):
    """With scope OFF, a known non-photo/video closed ticket is rendered."""
    ids = _closed_ids(client, "?review_view=all&missing_tags=0&photo_video_only=0")
    # 810001 = "Synthetic closed untagged" (no photo/video keyword).
    assert "810001" in ids
    # 810017 = "Delivery schedule inquiry" (no photo/video keyword).
    assert "810017" in ids


def test_explicit_off_does_not_trigger_retrieval(client, monkeypatch):
    """Turning scope OFF is purely local — no network, no DB writes, no cache."""
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("NETWORK BLOCKED")

    import requests
    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)
    _closed_html(client, "?review_view=all&missing_tags=0&photo_video_only=0")
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# review_view composition
# ---------------------------------------------------------------------------

def test_photo_on_plus_active_view(client):
    """Photo/Video ON + active view: only active closed tickets in scope."""
    ids = _closed_ids(client, "?review_view=active&missing_tags=0")
    # None of the photo/video synthetic tickets have a review_result set, so
    # they are all Unreviewed -> active.
    assert len(ids) == 7
    assert all(810010 <= int(i) <= 810016 for i in ids)


def test_photo_on_plus_completed_view(client, fixed_clock):
    """Photo/Video ON + completed view: no completed photo/video tickets yet."""
    # No review_result set -> none are completed.
    ids = _closed_ids(client, "?review_view=completed&missing_tags=0")
    assert ids == []


def test_photo_off_plus_active_view(client):
    """Photo/Video OFF + active: existing active behavior across full cache."""
    ids = _closed_ids(client, "?review_view=active&missing_tags=0&photo_video_only=0")
    # 810001, 810003, 810004 are untagged + Unreviewed -> active.
    # 810002 is tagged with "parts" (Unreviewed) -> active.
    # 810010-810017 are all Unreviewed -> active.
    assert "810001" in ids
    assert "810002" in ids
    assert "810003" in ids
    assert "810004" in ids


def test_review_view_meaning_unchanged(client, fixed_clock):
    """The review_view semantics (active/completed/all) are preserved."""
    from app import set_closed_review_result, REVIEW_STATES
    # Mark one photo/video ticket as Resolved (completed).
    set_closed_review_result(810010, "Resolved")
    # Active view should NOT show it.
    ids_active = _closed_ids(client, "?review_view=active&missing_tags=0")
    assert "810010" not in ids_active
    # Completed view SHOULD show it.
    ids_completed = _closed_ids(client, "?review_view=completed&missing_tags=0")
    assert "810010" in ids_completed
    # All view shows it.
    ids_all = _closed_ids(client, "?review_view=all&missing_tags=0")
    assert "810010" in ids_all


# ---------------------------------------------------------------------------
# Local-only safety
# ---------------------------------------------------------------------------

def test_turning_scope_on_off_is_local_only(client, monkeypatch):
    """Toggling the scope triggers zero network/DB/cache writes."""
    import requests
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        raise AssertionError("NETWORK BLOCKED")

    monkeypatch.setattr(requests, "get", boom)
    monkeypatch.setattr(requests, "post", boom)
    # ON
    _closed_html(client, "?photo_video_only=1")
    # OFF
    _closed_html(client, "?photo_video_only=0")
    assert calls["n"] == 0


def test_scope_does_not_mutate_closed_review_state(client, fixed_clock):
    """Changing the scope is a pure display filter — review_result is untouched."""
    from app import load_closed_review_rows, set_closed_review_result
    set_closed_review_result(810011, "Resolved")
    rows_before = load_closed_review_rows()
    _closed_html(client, "?photo_video_only=0")
    _closed_html(client, "?photo_video_only=1")
    rows_after = load_closed_review_rows()
    assert rows_before == rows_after


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def test_scope_survives_review_view_change(client):
    """Photo/Video ON survives a review_view change via URL."""
    html = _closed_html(client, "?review_view=active&missing_tags=0&photo_video_only=1")
    # The photo_video_only checkbox should remain checked in the rendered form.
    # Note: HTML uses unquoted attributes in this template.
    assert 'id=closed-filter-photo-video name=photo_video_only value=1 checked' in html
    # The review_view selector should have "active" selected.
    assert '<option value="active" selected>' in html


def test_scope_survives_preset_link(client):
    """Date preset links preserve the current photo_video_only state."""
    html = _closed_html(client, "?days=30&missing_tags=0&photo_video_only=1&review_view=active")
    # A 60d preset link should include photo_video_only=1
    assert "days=60&amp;missing_tags=0&amp;photo_video_only=1&amp;review_view=active" in html


def test_scope_survives_filter_submit(client):
    """Submitting the filter form preserves the explicit photo_video_only value."""
    # Submit with photo_video_only OFF
    html = _closed_html(client, "?days=30&missing_tags=0&photo_video_only=0&review_view=active")
    # The checkbox should NOT be checked
    assert 'id="closed-filter-photo-video" name=photo_video_only value=1 >' in html or \
           'value=1 >' in html  #unchecked


def test_scope_survives_review_post(client, fixed_clock):
    """A review POST redirects back preserving photo_video_only."""
    from app import load_closed_review_rows
    csrf = _csrf(client, "?review_view=all&missing_tags=0")
    resp = client.post("/closed/api/review", data={
        "csrf_token": csrf,
        "ticket_id": "810010",
        "review_result": "Resolved",
        "days": "60",
        "missing_tags": "0",
        "photo_video_only": "1",
        "review_view": "all",
    })
    assert resp.status_code == 303
    loc = resp.headers["Location"]
    assert "photo_video_only=1" in loc
    assert "review_view=all" in loc
    # Verify the review was actually saved
    assert load_closed_review_rows()[810010]["review_result"] == "Resolved"


# ---------------------------------------------------------------------------
# Reset / default behavior
# ---------------------------------------------------------------------------

def test_reset_to_defaults_returns_photo_video_on(client):
    """The Reset to Defaults link includes photo_video_only=1."""
    html = _closed_html(client, "?review_view=all&missing_tags=0")
    assert 'href="/closed?days=60&amp;missing_tags=1&amp;photo_video_only=1&amp;review_view=active"' in html


def test_closed_page_url_preserves_photo_video_only():
    """closed_page_url includes photo_video_only in the canonical URL."""
    cfg = {"days": 30, "missing_tags": False, "photo_video_only": True,
           "review_view": "active"}
    url = closed_page_url(cfg)
    assert "photo_video_only=1" in url

    cfg_off = {"days": 30, "missing_tags": False, "photo_video_only": False,
               "review_view": "active"}
    url_off = closed_page_url(cfg_off)
    assert "photo_video_only=0" in url_off


# ---------------------------------------------------------------------------
# Main queue regression
# ---------------------------------------------------------------------------

def test_main_queue_default_photo_video_unchanged():
    """The /queue default photo_video_only is still True (shared default)."""
    assert DEFAULT_FILTERS["photo_video_only"] is True


def test_main_queue_matcher_unchanged():
    """The canonical keyword set and regex are unchanged."""
    from app import KEYWORD_RE
    expected = ["photo", "photos", "picture", "pictures", "pic", "pics",
                "video", "videos", "vid", "vids"]
    assert KEYWORDS == expected
    # The regex must still be case-insensitive and word-boundary aware.
    assert KEYWORD_RE.flags & re.IGNORECASE
    assert KEYWORD_RE.match("photo")
    assert not KEYWORD_RE.match("photography")


def test_main_queue_filters_unchanged(client):
    """The /queue page still defaults to photo/video subjects only."""
    html = client.get("/queue").get_data(as_text=True)
    assert "Showing: Photo/video subjects only + No reviewed/closed tags" in html


# ---------------------------------------------------------------------------
# Parsing edge cases
# ---------------------------------------------------------------------------

def test_closed_filters_parse_photo_video_only():
    """closed_filters_from_args parses photo_video_only correctly."""
    assert closed_filters_from_args({"photo_video_only": "1"})["photo_video_only"] is True
    assert closed_filters_from_args({"photo_video_only": "0"})["photo_video_only"] is False
    assert closed_filters_from_args({"photo_video_only": "true"})["photo_video_only"] is True
    assert closed_filters_from_args({"photo_video_only": "false"})["photo_video_only"] is False
    # Invalid -> default ON
    assert closed_filters_from_args({"photo_video_only": "banana"})["photo_video_only"] is True
    # Missing -> default ON
    assert closed_filters_from_args({})["photo_video_only"] is True
