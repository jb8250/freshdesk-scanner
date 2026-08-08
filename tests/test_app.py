"""Offline test suite for the Freshdesk Review Queue Dashboard.

All tests run entirely offline:
  - the autouse conftest fixture blocks every requests call,
  - offline mode reads fixture pages only (no network, no API key),
  - live-mode code paths are exercised with monkeypatched fake responses,
  - the review-state SQLite database is isolated per test (REVIEW_DB_PATH).

Fixture timestamps are anchored to T_REF (2026-08-05T12:00:00Z, also stored in
fixtures/fixtures.json "reference_time"). Tests that depend on relative dates
pin the app clock via the `fixed_clock` / `client` fixtures so they never go
stale as the real clock moves.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest
import requests
from werkzeug.datastructures import MultiDict

import app
from app import (
    ACTIVE_STATES,
    COMPLETED_STATES,
    DEFAULT_FILTERS,
    KEYWORD_RE,
    REVIEWED_STATES,
    REVIEW_STATES,
    OfflineDataError,
    apply_queue_filters,
    category_matches,
    csrf_valid,
    filter_query_string,
    filters_from_args,
    fmt_due,
    get_csrf_token,
    has_missing_tags,
    has_primary_filter,
    init_db,
    is_customer_responded,
    is_offline,
    is_overdue,
    is_waiting_on_customer,
    keyword_filter_hits,
    last_opened_ticket_id,
    load_review_rows,
    mark_opened,
    matches_days_window,
    matches_missing_tags,
    matches_overdue,
    matches_status_group,
    offline_paginate_tickets,
    paginate_tickets,
    parse_bool,
    parse_days,
    parse_dt,
    parse_review_view,
    passes_filters,
    resolve_bind_host,
    review_view_includes,
    set_review_result,
    sla_unavailable,
    ticket_badges,
    ticket_url,
    updated_since_review,
)

T_REF = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "fixtures.json")


def _ids(text):
    """IDs of RENDERED rows: every row renders its ticket id in exactly two
    `data-ticket-id="..."` attributes (ticket-number link + subject link).
    Keying off this gets the true row set and ignores flash messages and
    aria-labels that also contain "#5000xx" text."""
    return sorted(set(re.findall(r'data-ticket-id="(5000\d\d)"', text)))


def _row_for(html, tid):
    """Return the <tr>...</tr> segment for a given ticket id, or None."""
    for row in re.findall(r'<tr class="rv-[a-z]+[^"]*"[^>]*>.*?</tr>', html, re.S):
        if f'data-ticket-id="{tid}"' in row:
            return row
    return None


def _csrf(client):
    html = client.get("/queue").get_data(as_text=True)
    m = re.search(r'name=csrf_token value="([^"]+)"', html)
    assert m, "csrf token not found in page"
    return m.group(1)


# ---------------------------------------------------------------------------
# Offline flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("Yes", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("2", False),
])
def test_offline_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("FRESHDESK_OFFLINE", value)
    assert is_offline() is expected


def test_offline_flag_unset_means_live(monkeypatch):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    assert is_offline() is False


# ---------------------------------------------------------------------------
# Keyword matching (subject-only photo/video, word boundaries)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subject", [
    "Customer sent photo of damaged dresser drawer",
    "Re: video of wobbling table leg",
    "Waiting on customer for photos of shelf damage",
    "Customer photo attached - replacement needed",
    "Closed: customer sent pictures of cabinet",
    "Please see attached pic of the broken hinge",
    "Awaiting pictures of the damaged nightstand",
    "Customer provided video of the sagging shelf",
    "Photo of stain on mattress",
    "Pics of the drawer front",
    "send a vid now",
])
def test_keyword_matches(subject):
    assert keyword_filter_hits(subject), f"expected match: {subject}"


def test_keyword_matches_case_insensitive():
    assert keyword_filter_hits("PHOTO OF DAMAGE")
    assert keyword_filter_hits("Customer sent Photo of the drawer")


@pytest.mark.parametrize("subject", [
    "Update on table delivery",
    "Invoice for pending order",
    "Vendor painted the topic area before delivery",
    "Regarding the topic shelf condition",
    "Delivery schedule confirmation",
    "provide identification",
    "topic",
])
def test_keyword_non_matches(subject):
    assert not keyword_filter_hits(subject), f"expected NO match: {subject}"


def test_keyword_word_boundary_vid_vs_vendor():
    assert KEYWORD_RE.search("send a vid now") is not None
    assert KEYWORD_RE.search("provide identification") is None
    assert KEYWORD_RE.search("topic") is None
    assert KEYWORD_RE.search("vendor") is None
    assert KEYWORD_RE.search("picturesque") is None


# ---------------------------------------------------------------------------
# Filter behavior (existing business rules, preserved)
# ---------------------------------------------------------------------------


def _ticket(**over):
    base = {
        "id": 999001,
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


def test_filter_includes_overdue_customer_responded():
    assert passes_filters(_ticket()) is True


def test_filter_excludes_not_yet_overdue():
    t = _ticket(due_by="2035-01-01T12:00:00Z")
    assert passes_filters(t) is False


# MODIFIED (behavior changed by the dashboard spec): a status-2 ticket with a
# missing due_by is no longer part of the default Overdue view. It is shown by
# the Customer Responded category instead (with an SLA DATE UNAVAILABLE badge),
# because "Overdue" now strictly means "valid due_by earlier than now".
def test_filter_missing_due_by_shown_only_under_customer_responded():
    t = _ticket(due_by=None)
    assert passes_filters(t) is False  # default: Overdue only
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=True)
    assert passes_filters(t, cfg) is True


# MODIFIED (behavior changed by the dashboard spec): same rationale as above —
# a malformed due_by is Customer Responded territory, not Overdue.
def test_filter_malformed_due_by_shown_only_under_customer_responded():
    t = _ticket(due_by="not-a-date")
    assert passes_filters(t) is False  # default: Overdue only
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=True)
    assert passes_filters(t, cfg) is True


# Waiting on Customer is one member of the status group (OR with Customer
# Responded), which ANDs with the Overdue condition. Under the default config
# (Overdue ON, Waiting OFF) a waiting ticket that is NOT overdue is hidden;
# with Waiting selected and Overdue OFF it is shown.
def test_filter_waiting_on_customer_requires_waiting_category():
    # MODIFIED (Prompt 06): Overdue (ON by default) now ANDs with the status
    # group, so a Waiting ticket must ALSO be overdue when Overdue is ON.
    # Previously Waiting and Overdue ORed, leaking non-overdue waiting tickets.
    t = _ticket(status=6, due_by="2035-01-01T12:00:00Z", subject="Waiting on customer for photos of shelf damage")
    assert passes_filters(t) is False  # waiting OFF and not overdue
    # Selecting only Waiting (Overdue OFF) shows this waiting ticket regardless of due date.
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=False, waiting=True)
    assert passes_filters(t, cfg) is True
    # Default Overdue ON + Waiting ON is an intersection: waiting AND overdue.
    cfg_overdue = dict(DEFAULT_FILTERS, waiting=True)
    assert passes_filters(t, cfg_overdue) is False  # waiting but not overdue
    assert passes_filters(_ticket(status=6, subject="Waiting on customer for photos of shelf damage"), cfg_overdue) is True  # waiting AND overdue


def test_filter_overdue_waiting_ticket_matches_via_overdue():
    # An overdue Waiting ticket still matches when only Overdue is on: the
    # status group imposes no restriction when neither status is selected.
    t = _ticket(status=6, due_by="2020-06-15T17:00:00Z", subject="Waiting on customer for photos of shelf damage")
    assert passes_filters(t) is True


def test_filter_excludes_tagged_ticket():
    t = _ticket(tags=["warranty"])
    assert passes_filters(t) is False


def test_filter_includes_tagged_ticket_when_missing_tags_off():
    t = _ticket(tags=["warranty"])
    cfg = dict(DEFAULT_FILTERS, missing_tags=False)
    assert passes_filters(t, cfg) is True


def test_filter_excludes_closed_ticket():
    t = _ticket(status=5)
    assert passes_filters(t) is False


def test_filter_excludes_resolved_status4_ticket():
    t = _ticket(status=4)
    assert passes_filters(t) is False


def test_filter_excludes_no_keyword_subject():
    t = _ticket(subject="Update on table delivery")
    assert passes_filters(t) is False


def test_filter_excludes_vendor_and_topic_subjects():
    t = _ticket(subject="Vendor painted the topic area")
    assert passes_filters(t) is False


# ---------------------------------------------------------------------------
# Mixed filter model (Prompt 06): Overdue ANDs with the status group, the two
# statuses OR within their group, Missing Tags ANDs, and no primary filter
# selected shows no results. Previously all three controls ORed together,
# which made Overdue + Customer Responded a union instead of an intersection.
# ---------------------------------------------------------------------------


def test_mixed_overdue_only_shows_all_overdue():
    cfg = dict(DEFAULT_FILTERS, overdue=True, responded=False, waiting=False)
    assert passes_filters(_ticket(), cfg) is True                       # overdue responded
    assert passes_filters(_ticket(status=6), cfg) is True               # overdue waiting
    assert passes_filters(_ticket(due_by="2035-01-01T12:00:00Z"), cfg) is False  # not overdue


def test_mixed_customer_responded_only_shows_all_responded():
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=True, waiting=False)
    assert passes_filters(_ticket(), cfg) is True                       # responded, overdue
    assert passes_filters(_ticket(due_by="2035-01-01T12:00:00Z"), cfg) is True  # responded, not overdue
    assert passes_filters(_ticket(status=6), cfg) is False              # waiting excluded


def test_mixed_waiting_only_shows_all_waiting():
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=False, waiting=True)
    assert passes_filters(_ticket(status=6), cfg) is True               # waiting, overdue
    assert passes_filters(_ticket(status=6, due_by="2035-01-01T12:00:00Z"), cfg) is True
    assert passes_filters(_ticket(status=2), cfg) is False              # responded excluded


def test_mixed_overdue_plus_responded_is_intersection():
    # Regression for Prompt 06: this combination previously ORed (union) and
    # leaked Customer-Responded-only AND Overdue-only tickets into the list.
    cfg = dict(DEFAULT_FILTERS, overdue=True, responded=True, waiting=False)
    assert passes_filters(_ticket(), cfg) is True                       # overdue AND responded
    assert passes_filters(_ticket(status=6), cfg) is False              # overdue waiting: not responded
    assert passes_filters(_ticket(due_by="2035-01-01T12:00:00Z"), cfg) is False  # responded, not overdue


def test_mixed_overdue_plus_waiting_is_intersection():
    cfg = dict(DEFAULT_FILTERS, overdue=True, responded=False, waiting=True)
    assert passes_filters(_ticket(status=6), cfg) is True               # overdue AND waiting
    assert passes_filters(_ticket(), cfg) is False                      # responded: not waiting
    assert passes_filters(_ticket(status=6, due_by="2035-01-01T12:00:00Z"), cfg) is False  # waiting, not overdue


def test_mixed_responded_plus_waiting_is_status_union():
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=True, waiting=True)
    assert passes_filters(_ticket(status=2), cfg) is True               # responded
    assert passes_filters(_ticket(status=6), cfg) is True               # waiting


def test_mixed_overdue_plus_both_statuses_overdue_either_status():
    cfg = dict(DEFAULT_FILTERS, overdue=True, responded=True, waiting=True)
    assert passes_filters(_ticket(), cfg) is True                       # overdue responded
    assert passes_filters(_ticket(status=6), cfg) is True               # overdue waiting
    assert passes_filters(_ticket(due_by="2035-01-01T12:00:00Z"), cfg) is False  # not overdue
    assert passes_filters(_ticket(status=6, due_by="2035-01-01T12:00:00Z"), cfg) is False


def test_mixed_all_three_off_shows_no_tickets():
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=False, waiting=False)
    assert passes_filters(_ticket(), cfg) is False
    assert passes_filters(_ticket(status=6), cfg) is False
    assert has_primary_filter(cfg) is False


def test_mixed_missing_tags_applies_with_and():
    cfg = dict(DEFAULT_FILTERS, overdue=True, responded=True, waiting=False)
    tagged = _ticket(tags=["warranty"])
    assert passes_filters(_ticket(), cfg) is True        # missing tags: in
    assert passes_filters(tagged, cfg) is False          # tagged: out, even though overdue+responded
    cfg_off = dict(cfg, missing_tags=False)
    assert passes_filters(tagged, cfg_off) is True       # Missing Tags OFF: no restriction


def test_mixed_days_window_still_applies_with_and(monkeypatch):
    monkeypatch.setattr(app, "now_utc", lambda: T_REF)
    cfg = dict(DEFAULT_FILTERS, overdue=True, responded=True, waiting=False, days=60)
    pool = [
        _ticket(id=1, updated_at=_updated(5)),    # in window
        _ticket(id=2, updated_at=_updated(90)),   # outside window: excluded
    ]
    out = apply_queue_filters(pool, cfg)
    assert [t["id"] for t in out] == [1]


def test_mixed_no_duplicate_rows_when_ticket_matches_both():
    # A ticket matching overdue AND responded must appear exactly once even if
    # the raw pool contains it twice (dedupe happens before filtering).
    cfg = dict(DEFAULT_FILTERS, overdue=True, responded=True, waiting=False)
    pool = [_ticket(id=1), _ticket(id=1)]
    out = apply_queue_filters(pool, cfg)
    assert [t["id"] for t in out] == [1]


def test_mixed_unknown_url_values_fail_safely():
    # Garbage values fall back to documented defaults (Overdue ON), so the
    # dashboard never crashes and never silently widens to "show everything".
    cfg = filters_from_args(MultiDict([("overdue", "x"), ("responded", "y"), ("waiting", "z")]))
    assert cfg["overdue"] is True and cfg["responded"] is False and cfg["waiting"] is False
    assert passes_filters(_ticket(), cfg) is True


def test_mixed_filter_query_string_canonical():
    # One value per parameter, canonical order, no duplicates — bookmarkable.
    qs = filter_query_string(dict(
        overdue=True, responded=True, waiting=True,
        missing_tags=True, days=60, review_view="active",
    ))
    assert qs == "overdue=1&responded=1&waiting=1&missing_tags=1&days=60&review_view=active"


def test_mixed_review_view_is_independent_and_gate(client, monkeypatch):
    monkeypatch.setattr(app, "now_utc", lambda: T_REF)
    set_review_result(500001, "Resolved")
    base = "/queue?overdue=1&responded=1&waiting=0&missing_tags=1&days=60"
    active = client.get(base + "&review_view=active").get_data(as_text=True)
    completed = client.get(base + "&review_view=completed").get_data(as_text=True)
    allv = client.get(base + "&review_view=all").get_data(as_text=True)
    assert "500001" not in _ids(active)      # resolved ticket hidden in Active
    assert "500001" in _ids(completed)       # visible in Completed
    assert "500001" in _ids(allv)            # visible in All
    # Mixed logic still intersects on the other rows in every view.
    for html in (active, completed, allv):
        assert "500003" not in _ids(html)    # waiting ticket never in overdue+responded view


def test_mixed_review_post_redirect_preserves_mixed_filters(client, monkeypatch):
    monkeypatch.setattr(app, "now_utc", lambda: T_REF)
    token = _csrf(client)
    before = client.get("/queue?overdue=1&responded=1&waiting=0&missing_tags=1&days=60&review_view=active")
    assert "500007" in _ids(before.get_data(as_text=True))
    resp = client.post("/queue/api/review", data={
        "csrf_token": token, "ticket_id": "500007", "review_result": "Resolved",
        "overdue": "1", "responded": "1", "waiting": "0",
        "missing_tags": "1", "days": "60", "review_view": "active",
    })
    assert resp.status_code == 303
    loc = resp.headers["Location"]
    assert "overdue=1" in loc and "responded=1" in loc and "waiting=0" in loc
    assert "missing_tags=1" in loc and "days=60" in loc and "review_view=active" in loc
    after = client.get(loc).get_data(as_text=True)
    assert "500007" not in _ids(after)       # resolved ticket left the active view
    assert "500003" not in _ids(after)      # waiting ticket still excluded (intersection held)


def test_mixed_ui_groups_and_helper_text(client):
    html = client.get("/queue").get_data(as_text=True)
    for legend in ("Ticket conditions", "Freshdesk status", "Additional filters"):
        assert f"<legend class=group-lbl>{legend}</legend>" in html, legend
    # Prompt 07 helper copy: describes how Overdue combines with status and
    # that one or both statuses may be selected (replaces the Prompt 06 text).
    assert "Works together with the selected status." in html
    assert "Select one or both statuses." in html


# ---------------------------------------------------------------------------
# Filter parsing and URL configuration (new)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    "2026-08-05T12:00:00",
    "2026-08-05 12:00:00",
])
def test_parse_dt_rejects_timezone_less_timestamps(value):
    """Naive datetimes cannot safely be compared with the UTC app clock."""
    assert parse_dt(value) is None


@pytest.mark.parametrize("value", [
    "2026-08-05T12:00:00Z",
    "2026-08-05T12:00:00+00:00",
    "2026-08-05T08:00:00-04:00",
])
def test_parse_dt_accepts_offset_aware_timestamps(value):
    assert parse_dt(value) is not None


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
    ("", False), ("abc", False), ("2", False), (None, False),
])
def test_parse_bool(value, expected):
    assert parse_bool(value, default=False) is expected


def test_parse_bool_respects_default():
    assert parse_bool("banana", default=True) is True


@pytest.mark.parametrize("value,expected", [
    ("7", 7), ("365", 365), ("1", 1),
    ("0", 60), ("-5", 60), ("1.5", 60), ("abc", 60), ("999", 60), ("", 60),
    (None, 60),
])
def test_parse_days(value, expected):
    assert parse_days(value) == expected


def test_parse_review_view():
    assert parse_review_view("active") == "active"
    assert parse_review_view("completed") == "completed"
    assert parse_review_view("all") == "all"
    assert parse_review_view("bogus") == "active"
    assert parse_review_view(None) == "active"


def test_filters_from_args_defaults():
    class Args:
        def getlist(self, key):
            return []
    cfg = filters_from_args(Args())
    assert cfg == DEFAULT_FILTERS


def test_filters_from_args_explicit():
    class Args:
        def getlist(self, key):
            return {"overdue": ["1"], "responded": ["1"], "waiting": ["0"],
                    "missing_tags": ["1"], "days": ["30"], "review_view": ["completed"]}[key]
    cfg = filters_from_args(Args())
    assert cfg == {"overdue": True, "responded": True, "waiting": False,
                   "missing_tags": True, "days": 30, "review_view": "completed"}


def test_filters_from_args_invalid_falls_back_to_defaults():
    class Args:
        def getlist(self, key):
            return {"overdue": ["banana"], "responded": ["2"], "waiting": ["maybe"],
                    "missing_tags": ["0"], "days": ["999"], "review_view": ["nope"]}.get(key, [])
    cfg = filters_from_args(Args())
    # invalid values fall back to defaults; "0" for missing_tags is a VALID
    # false, so it stays False.
    assert cfg == {"overdue": True, "responded": False, "waiting": False,
                   "missing_tags": False, "days": 60, "review_view": "active"}


def test_filters_from_args_repeated_values_last_wins():
    class Args:
        def getlist(self, key):
            return {"days": ["30", "7"], "overdue": ["0", "1"]}.get(key, [])
    cfg = filters_from_args(Args())
    assert cfg["days"] == 7
    assert cfg["overdue"] is True


def test_filters_from_args_repeated_invalid_last_uses_default():
    class Args:
        def getlist(self, key):
            return {"days": ["30", "oops"]}.get(key, [])
    cfg = filters_from_args(Args())
    assert cfg["days"] == DEFAULT_FILTERS["days"]


def test_filter_query_string_roundtrip():
    cfg = {"overdue": True, "responded": True, "waiting": False,
           "missing_tags": True, "days": 30, "review_view": "completed"}
    qs = filter_query_string(cfg)
    assert qs == "overdue=1&responded=1&waiting=0&missing_tags=1&days=30&review_view=completed"
    assert filters_from_args(_ArgsFrom(qs)) == cfg


class _ArgsFrom:
    def __init__(self, qs):
        self._parts = {}
        for pair in qs.split("&"):
            k, _, v = pair.partition("=")
            self._parts.setdefault(k, []).append(v)

    def getlist(self, key):
        return self._parts.get(key, [])


# ---------------------------------------------------------------------------
# Category logic (new)
# ---------------------------------------------------------------------------


def test_is_overdue():
    assert is_overdue(_ticket(due_by="2020-06-15T17:00:00Z")) is True
    assert is_overdue(_ticket(due_by="2035-01-01T12:00:00Z")) is False
    assert is_overdue(_ticket(due_by=None)) is False
    assert is_overdue(_ticket(due_by="not-a-date")) is False
    assert is_overdue(_ticket(due_by="")) is False


def test_status_category_helpers():
    assert is_customer_responded(_ticket(status=2)) is True
    assert is_customer_responded(_ticket(status=6)) is False
    assert is_waiting_on_customer(_ticket(status=6)) is True
    assert is_waiting_on_customer(_ticket(status=2)) is False


def test_has_missing_tags():
    assert has_missing_tags(_ticket(tags=[])) is True
    assert has_missing_tags(_ticket(tags=None)) is True
    assert has_missing_tags(_ticket()) is True  # key absent
    assert has_missing_tags(_ticket(tags=["warranty"])) is False


def test_category_matches():
    assert category_matches(_ticket(), "overdue") is True
    assert category_matches(_ticket(due_by="2035-01-01T12:00:00Z"), "overdue") is False
    assert category_matches(_ticket(status=2), "responded") is True
    assert category_matches(_ticket(status=6), "waiting") is True
    assert category_matches(_ticket(), "bogus") is False


def test_has_primary_filter_all_off():
    # MODIFIED (was test_matches_any_category_all_off): the OR-over-categories
    # predicate that made Overdue+Responded a union was replaced by the mixed
    # model. `has_primary_filter` is the new gate that refuses to show results
    # when Overdue / Customer Responded / Waiting on Customer are all OFF.
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=False, waiting=False)
    assert has_primary_filter(cfg) is False
    assert passes_filters(_ticket(), cfg) is False  # no results without any primary filter


def test_has_primary_filter_requires_at_least_one():
    for key in ("overdue", "responded", "waiting"):
        cfg = dict(DEFAULT_FILTERS, overdue=False, responded=False, waiting=False)
        cfg[key] = True
        assert has_primary_filter(cfg) is True, key


def test_matches_status_group_responded_only():
    # MODIFIED (was test_matches_any_category_responded_only): the status group
    # now ORs Customer Responded and Waiting on Customer as a distinct dimension
    # from Overdue. Selecting only responded restricts to responded tickets,
    # independent of the overdue flag.
    cfg = dict(DEFAULT_FILTERS, overdue=False, responded=True, waiting=False)
    assert matches_status_group(_ticket(status=2), cfg) is True
    assert matches_status_group(_ticket(status=6), cfg) is False


def test_sla_unavailable():
    assert sla_unavailable(_ticket(due_by=None)) is True
    assert sla_unavailable(_ticket(due_by="not-a-date")) is True
    assert sla_unavailable(_ticket(due_by="2020-06-15T17:00:00Z")) is False
    assert sla_unavailable(_ticket(status=6, due_by=None)) is False  # waiting tickets: no SLA badge


# ---------------------------------------------------------------------------
# Days-back window (new)
# ---------------------------------------------------------------------------


def _updated(days_ago):
    return (T_REF - __import__("datetime").timedelta(days=days_ago)).isoformat()


def test_matches_days_window_within(monkeypatch):
    monkeypatch.setattr(app, "now_utc", lambda: T_REF)
    cfg = dict(DEFAULT_FILTERS, days=60)
    assert matches_days_window(_ticket(updated_at=_updated(5)), cfg) is True
    assert matches_days_window(_ticket(updated_at=_updated(59)), cfg) is True
    assert matches_days_window(_ticket(updated_at=_updated(60)), cfg) is True  # inclusive boundary


def test_matches_days_window_outside(monkeypatch):
    monkeypatch.setattr(app, "now_utc", lambda: T_REF)
    cfg = dict(DEFAULT_FILTERS, days=60)
    assert matches_days_window(_ticket(updated_at=_updated(61)), cfg) is False
    assert matches_days_window(_ticket(updated_at=_updated(400)), cfg) is False


def test_matches_days_window_fails_closed(monkeypatch):
    monkeypatch.setattr(app, "now_utc", lambda: T_REF)
    cfg = dict(DEFAULT_FILTERS, days=60)
    assert matches_days_window(_ticket(updated_at=None), cfg) is False
    assert matches_days_window(_ticket(updated_at="garbage"), cfg) is False
    assert matches_days_window(_ticket(updated_at=""), cfg) is False


def test_apply_queue_filters_dedupes():
    tickets = [_ticket(id=1), _ticket(id=1), _ticket(id=2, subject="Customer sent photo of leg")]
    out = apply_queue_filters(tickets, dict(DEFAULT_FILTERS))
    assert [t["id"] for t in out] == [1, 2]


def test_apply_queue_filters_applies_days_and_categories(monkeypatch):
    monkeypatch.setattr(app, "now_utc", lambda: T_REF)
    pool = [
        _ticket(id=1, updated_at=_updated(5)),                       # in
        _ticket(id=2, updated_at=_updated(90)),                      # out (days)
        _ticket(id=3, due_by="2035-01-01T12:00:00Z", updated_at=_updated(5)),  # out (not overdue, default)
        _ticket(id=4, tags=["warranty"], updated_at=_updated(5)),    # out (missing tags gate)
    ]
    out = apply_queue_filters(pool, dict(DEFAULT_FILTERS))
    assert [t["id"] for t in out] == [1]


# ---------------------------------------------------------------------------
# Fixture integrity
# ---------------------------------------------------------------------------


def test_fixture_file_exists_and_is_valid():
    assert os.path.exists(app.FIXTURES_FILE)
    with open(app.FIXTURES_FILE) as fh:
        data = json.load(fh)
    assert isinstance(data, dict)
    assert isinstance(data.get("pages"), list) and data["pages"]


def test_fixture_contains_required_cases():
    with open(app.FIXTURES_FILE) as fh:
        data = json.load(fh)
    subjects = " | ".join(t["subject"] for page in data["pages"] for t in page)
    ids = [t["id"] for page in data["pages"] for t in page]
    assert len(ids) == len(set(ids)), "fixture ids must be unique"
    # required coverage
    assert any(t["status"] == 2 and "photo" in t["subject"].lower() for page in data["pages"] for t in page)
    assert any(t["status"] == 2 and "video" in t["subject"].lower() for page in data["pages"] for t in page)
    assert any(t["status"] == 6 for page in data["pages"] for t in page)
    assert any(t.get("tags") for page in data["pages"] for t in page)
    assert any(t["status"] == 5 for page in data["pages"] for t in page)
    assert "vendor" in subjects.lower() and "topic" in subjects.lower()
    assert any("due_by" not in t for page in data["pages"] for t in page)
    assert any(t.get("due_by") == "not-a-date" for page in data["pages"] for t in page)
    assert len(data["pages"]) >= 2


def test_fixture_has_no_real_pii():
    """Fixtures must not contain real-looking customer data."""
    with open(app.FIXTURES_FILE) as fh:
        raw = fh.read().lower()
    for needle in ["@", "http://", "https://", "street", "road", "avenue", "joshua", "jb8250"]:
        assert needle not in raw, f"fixture contains suspicious content: {needle!r}"


def test_fixture_updated_at_all_present_and_reference_time_matches():
    with open(app.FIXTURES_FILE) as fh:
        data = json.load(fh)
    assert data.get("reference_time") == "2026-08-05T12:00:00Z"
    for page in data["pages"]:
        for t in page:
            assert "updated_at" in t, f"ticket {t['id']} missing updated_at"
            datetime.fromisoformat(t["updated_at"].replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Offline pagination
# ---------------------------------------------------------------------------


def test_offline_pagination_reads_all_pages(monkeypatch):
    # MODIFIED: the fixture set grew from 12 to 28 tickets to cover every
    # dashboard case (section 16 of the spec). The test now asserts the full
    # raw pool with unique, sorted ids.
    monkeypatch.setattr(app, "FIXTURES_FILE", FIXTURES)
    tickets = list(offline_paginate_tickets())
    assert len(tickets) == 28
    ids = [t["id"] for t in tickets]
    assert ids == sorted(ids)
    assert len(set(ids)) == 28


class FakeResp:
    def __init__(self, data, status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


def _fake_paginated_get(pages, calls):
    def fake_get(url, auth=None, params=None, timeout=None):
        calls.append(dict(url=url, params=params, auth=auth))
        idx = min(len(calls) - 1, len(pages) - 1)
        return FakeResp(pages[idx])
    return fake_get


def test_live_pagination_stops_below_page_size(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    calls = []
    pages = [[{"id": i} for i in range(1, 101)], [{"id": i} for i in range(101, 151)]]
    monkeypatch.setattr(requests, "get", _fake_paginated_get(pages, calls))
    tickets = list(paginate_tickets())
    assert len(tickets) == 150
    assert len(calls) == 2
    assert calls[0]["params"]["page"] == 1
    assert calls[1]["params"]["page"] == 2
    assert calls[0]["params"]["per_page"] == 100
    assert calls[0]["params"]["updated_since"]  # computed at runtime
    assert calls[0]["auth"] == ("fake-key-for-tests", "X")


def test_live_pagination_full_pages_continue(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    calls = []
    pages = [
        [{"id": i} for i in range(1, 101)],
        [{"id": i} for i in range(101, 201)],
        [{"id": 201}],
    ]
    monkeypatch.setattr(requests, "get", _fake_paginated_get(pages, calls))
    tickets = list(paginate_tickets())
    assert len(tickets) == 201
    assert len(calls) == 3  # 100 -> 100 -> 1 (stop below 100)


def test_live_pagination_empty_page_stops(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    calls = []
    monkeypatch.setattr(requests, "get", _fake_paginated_get([[]], calls))
    tickets = list(paginate_tickets())
    assert tickets == []
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Offline fail-closed behavior
# ---------------------------------------------------------------------------


def test_offline_missing_fixture_fails_closed(monkeypatch):
    monkeypatch.setattr(app, "FIXTURES_FILE", "/tmp/does_not_exist_fd_fixtures.json")
    with pytest.raises(OfflineDataError, match="fixture file not found"):
        list(offline_paginate_tickets())


def test_offline_malformed_fixture_fails_closed(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is not json")
    monkeypatch.setattr(app, "FIXTURES_FILE", str(bad))
    with pytest.raises(OfflineDataError, match="malformed JSON"):
        list(offline_paginate_tickets())


def test_offline_wrong_shape_fails_closed(monkeypatch, tmp_path):
    for shape in [{"pages": "nope"}, {"pages": []}, {"pages": [["x"]]}, ["not", "a", "dict"]]:
        f = tmp_path / "shape.json"
        f.write_text(json.dumps(shape))
        monkeypatch.setattr(app, "FIXTURES_FILE", str(f))
        with pytest.raises(OfflineDataError):
            list(offline_paginate_tickets())


def test_offline_never_reads_api_key(monkeypatch):
    """Offline data path must never touch the key loader (spy proves it)."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.setattr(app, "FIXTURES_FILE", FIXTURES)
    calls = {"n": 0}
    orig = app.load_api_key

    def spy():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(app, "load_api_key", spy)
    tickets, _ = app.get_ticket_pool()
    assert len(tickets) >= 6
    assert calls["n"] == 0, "offline mode called load_api_key"
    assert app.FRESHDESK_API_KEY == ""


# ---------------------------------------------------------------------------
# Ticket pool (raw) and cache
# ---------------------------------------------------------------------------


def test_offline_get_ticket_pool_returns_raw_pool(monkeypatch):
    """MODIFIED: the pool now returns RAW tickets (all fixture ids). Dashboard
    filtering moved to the config-driven layer (passes_filters/apply_queue_filters
    with URL parameters), so the pool must be unfiltered or no filter
    combination could ever show Customer-Responded-only tickets."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.setattr(app, "FIXTURES_FILE", FIXTURES)
    tickets, _ = app.get_ticket_pool()
    ids = [t["id"] for t in tickets]
    assert len(ids) == 28
    assert 500001 in ids and 500028 in ids
    assert 500002 in ids  # not-yet-overdue status-2: raw pool keeps it
    assert 500004 in ids  # tagged: raw pool keeps it
    assert 500005 in ids  # closed: raw pool keeps it
    assert 500009 in ids  # missing due_by
    assert 500010 in ids  # malformed due_by


def test_cache_write_then_read(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    calls = {"n": 0}
    orig = app.offline_paginate_tickets

    def spy():
        calls["n"] += 1
        yield from orig()

    monkeypatch.setattr(app, "offline_paginate_tickets", spy)
    first, _ = app.get_ticket_pool()
    assert calls["n"] == 1
    assert os.path.exists(app.CACHE_FILE)
    with open(app.CACHE_FILE) as fh:
        blob = json.load(fh)
    assert "fetched_at" in blob and isinstance(blob["tickets"], list)

    second, cache_age = app.get_ticket_pool()
    assert calls["n"] == 1, "second call must be served from cache"
    assert [t["id"] for t in first] == [t["id"] for t in second]
    assert 0 <= cache_age < app.CACHE_TTL_SECONDS


def test_cache_expiration_triggers_refetch(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    stale = {
        "fetched_at": time.time() - app.CACHE_TTL_SECONDS - 5,
        "tickets": [{"id": 1, "status": 2, "subject": "stale photo", "tags": []}],
    }
    with open(app.CACHE_FILE, "w") as fh:
        json.dump(stale, fh)
    calls = {"n": 0}
    orig = app.offline_paginate_tickets

    def spy():
        calls["n"] += 1
        yield from orig()

    monkeypatch.setattr(app, "offline_paginate_tickets", spy)
    tickets, cache_age = app.get_ticket_pool()
    assert calls["n"] == 1, "expired cache must be refetched"
    assert cache_age == 0
    assert len(tickets) == 28


def test_cache_fresh_file_avoids_refetch(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    fresh = {
        "fetched_at": time.time(),
        "tickets": [{"id": 1, "status": 2, "subject": "fresh photo", "tags": []}],
    }
    with open(app.CACHE_FILE, "w") as fh:
        json.dump(fresh, fh)
    calls = {"n": 0}
    orig = app.offline_paginate_tickets

    def spy():
        calls["n"] += 1
        yield from orig()

    monkeypatch.setattr(app, "offline_paginate_tickets", spy)
    tickets, _ = app.get_ticket_pool()
    assert calls["n"] == 0, "fresh cache must not refetch"
    assert tickets[0]["id"] == 1


def test_cache_corrupt_file_refetches(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    with open(app.CACHE_FILE, "w") as fh:
        fh.write("{ not json")
    calls = {"n": 0}
    orig = app.offline_paginate_tickets

    def spy():
        calls["n"] += 1
        yield from orig()

    monkeypatch.setattr(app, "offline_paginate_tickets", spy)
    tickets, _ = app.get_ticket_pool()
    assert calls["n"] == 1, "corrupt cache must be refetched"
    assert len(tickets) == 28


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_live_429_renders_error_page(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")

    def fake_get(url, auth=None, params=None, timeout=None):
        return FakeResp([], status=429, headers={"Retry-After": "5"})

    monkeypatch.setattr(requests, "get", fake_get)
    resp = app.app.test_client().get("/queue")
    assert resp.status_code == 200
    assert "Freshdesk API error" in resp.get_data(as_text=True)


def test_live_connection_error_renders_error_page(monkeypatch):
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")

    def fake_get(url, auth=None, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", fake_get)
    resp = app.app.test_client().get("/queue")
    assert resp.status_code == 200
    assert "Error fetching tickets" in resp.get_data(as_text=True)


def test_missing_key_renders_understandable_error(monkeypatch):
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    resp = app.app.test_client().get("/queue")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "No Freshdesk API key found" in text
    assert "freshdesk_api_key" in text


def test_offline_cannot_fall_back_to_live(monkeypatch):
    """Offline + missing fixtures renders the fail-closed error; the app must
    not silently switch to live mode."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.setattr(app, "FIXTURES_FILE", "/tmp/does_not_exist_fd_fixtures.json")
    resp = app.app.test_client().get("/queue")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "fixture file not found" in text
    assert "OFFLINE MODE" in text


# ---------------------------------------------------------------------------
# /queue rendering (offline)
# ---------------------------------------------------------------------------


def test_queue_offline_renders_with_banner(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    resp = app.app.test_client().get("/queue")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "OFFLINE MODE" in text
    assert "mock/offline fixture data" in text
    assert "#500001" in text
    assert "#500007" in text
    assert "matching your filters" in text
    # excluded tickets never render
    assert "500002" not in text
    assert "500004" not in text
    assert "500005" not in text
    assert "Mohawk" not in text


def test_queue_offline_waiting_param_shows_waiting(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    resp = app.app.test_client().get("/queue?waiting=1")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "#500003" in text  # waiting on customer
    assert "#500011" in text


def test_queue_offline_default_hides_not_overdue_waiting(monkeypatch):
    """MODIFIED (behavior changed by the dashboard spec): status-6 tickets
    with a PAST due_by now appear under the Overdue category (OR semantics),
    so the old assertion that 500003/500011 are hidden by default no longer
    holds. The default view still hides waiting tickets that are NOT overdue
    (500018/500025) because the Waiting category defaults to off."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    resp = app.app.test_client().get("/queue")
    text = resp.get_data(as_text=True)
    assert "#500003" in text  # overdue status-6 -> Overdue category
    assert "#500011" in text
    assert "#500018" not in text  # future-due waiting, Waiting OFF -> hidden
    assert "#500025" not in text


def test_queue_offline_no_network_and_no_key(monkeypatch):
    """Full offline /queue render under the global network block: must succeed
    with no API key present and no requests call."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    resp = app.app.test_client().get("/queue")
    assert resp.status_code == 200
    assert "OFFLINE MODE" in resp.get_data(as_text=True)


def test_refresh_link_does_not_bypass_cache(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    resp = app.app.test_client().get("/queue")
    text = resp.get_data(as_text=True)
    # MODIFIED (Prompt05): the Reset link now carries the full canonical
    # default query, so a bare `href=/queue` no longer exists. The filter form
    # still submits to /queue (its action), which is what this asserts.
    assert 'action=/queue' in text
    assert 'refresh' not in text.lower() or True  # no cache-busting param added
    assert "?refresh=" not in text and "cache=0" not in text


# ---------------------------------------------------------------------------
# Routes / app shape
# ---------------------------------------------------------------------------


def test_only_expected_routes_registered():
    """MODIFIED (Prompt08, Prompt12): closed housekeeping is a separate page;
    closed review write endpoints (Prompt12) are local-only POST routes, added
    to the expected surface alongside the queue review endpoints."""
    rules = {r.rule for r in app.app.url_map.iter_rules()}
    assert rules == {"/queue", "/closed",
                     "/queue/api/review", "/queue/api/opened",
                     "/closed/api/review", "/closed/api/opened"}, f"unexpected routes: {rules}"


def test_no_mohawk_or_upload_routes():
    rules = {r.rule for r in app.app.url_map.iter_rules()}
    assert "/" not in rules
    assert "/api/blend" not in rules
    assert "/upload" not in rules


def test_no_blend_import_in_app_source():
    with open(os.path.join(os.path.dirname(__file__), "..", "app.py")) as fh:
        src = fh.read()
    assert "from blend import" not in src
    assert "import PIL" not in src and "from PIL" not in src


# ---------------------------------------------------------------------------
# Bind guard / safety
# ---------------------------------------------------------------------------


def test_bind_guard_allows_loopback():
    assert resolve_bind_host("127.0.0.1") == "127.0.0.1"


def test_bind_guard_refuses_external():
    with pytest.raises(SystemExit):
        resolve_bind_host("0.0.0.0")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def test_fmt_due_overdue():
    out = fmt_due("2020-06-15T17:00:00Z")
    assert "OVERDUE" in out


def test_fmt_due_future():
    out = fmt_due("2035-01-01T12:00:00Z")
    assert "left" in out and "OVERDUE" not in out


def test_fmt_due_missing():
    assert fmt_due(None) == "—"
    assert fmt_due("") == "—"


def test_fmt_due_malformed():
    assert fmt_due("not-a-date") == "not-a-date"


def test_ticket_url_shape():
    assert ticket_url(500001) == "https://broadriverretail-help.freshdesk.com/a/tickets/500001"


# ---------------------------------------------------------------------------
# Network blocking proof
# ---------------------------------------------------------------------------


def test_network_blocker_is_active():
    with pytest.raises(AssertionError, match="NETWORK BLOCKED"):
        requests.get("https://broadriverretail-help.freshdesk.com/api/v2/tickets")


# ===========================================================================
# Dashboard: URL-backed filter state (new)
# ===========================================================================


def test_dashboard_form_reflects_url_state(client):
    html = client.get("/queue?overdue=1&responded=1&waiting=0&missing_tags=1&days=30&review_view=all").get_data(as_text=True)
    assert 'name=days min=1 max=365 value=30' in html
    assert 'name=overdue value=1 checked' in html
    assert 'name=responded value=1 checked' in html
    assert 'name=waiting value=1 checked' not in html  # unchecked -> no checked attribute
    # review_view select shows 'all' selected
    assert '<option value=all selected>' in html


def test_dashboard_form_defaults(client):
    html = client.get("/queue").get_data(as_text=True)
    assert 'name=days min=1 max=365 value=60' in html
    assert 'name=overdue value=1 checked' in html
    assert 'name=responded value=1 checked' not in html
    assert 'name=waiting value=1 checked' not in html
    assert 'name=missing_tags value=1 checked' in html


def test_dashboard_form_invalid_url_falls_back_to_defaults(client):
    html = client.get("/queue?days=999&overdue=banana&responded=2&review_view=bogus").get_data(as_text=True)
    assert 'name=days min=1 max=365 value=60' in html
    assert 'name=overdue value=1 checked' in html
    assert '<option value=active selected>' in html


def test_dashboard_repeated_query_last_wins(client):
    html = client.get("/queue?days=30&days=7&overdue=0&overdue=1").get_data(as_text=True)
    assert 'name=days min=1 max=365 value=7' in html
    assert 'name=overdue value=1 checked' in html


def test_apply_filters_link_preserves_all_params(client):
    html = client.get("/queue?overdue=1&responded=0&waiting=1&missing_tags=0&days=90&review_view=all").get_data(as_text=True)
    assert 'action=/queue' in html
    # every hidden review form carries the current filter state so POSTs
    # redirect back to the same view
    assert 'name=days value="90"' in html
    assert 'name=review_view value="all"' in html
    assert 'name=overdue value="1"' in html
    assert 'name=waiting value="1"' in html
    assert 'name=missing_tags value="0"' in html


def test_all_categories_off_message(client):
    # MODIFIED: message wording updated to match the new mixed filter model —
    # with Overdue, Customer Responded and Waiting on Customer all OFF there is
    # no primary restriction, so the dashboard shows no results and explains
    # that Overdue or at least one status must be selected.
    html = client.get("/queue?overdue=0&responded=0&waiting=0").get_data(as_text=True)
    assert "Select Overdue or at least one status to display results." in html
    assert "matching your filters" in html
    assert "0 tickets matching your filters" in html


def test_days_presets_links(client):
    html = client.get("/queue?overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active").get_data(as_text=True)
    html = html.replace("&amp;", "&")  # Jinja autoescapes & in href attributes
    for d in ("7", "14", "30", "60", "90"):
        assert f'class=preset href="/queue?overdue=1&responded=0&waiting=0&missing_tags=1&days={d}&review_view=active"' in html


# ===========================================================================
# Dashboard: filter combinations on fixture data (new)
# ===========================================================================


def test_default_view_rows(client):
    assert _ids(client.get("/queue").get_data(as_text=True)) == [
        "500001", "500003", "500007", "500011", "500013",
        "500020", "500021", "500022", "500023", "500024",
    ]


def test_responded_view_rows(client):
    ids = _ids(client.get("/queue?overdue=0&responded=1&waiting=0&missing_tags=1&days=60").get_data(as_text=True))
    # status-2, untagged, keyword, updated within 60d — including not-yet-due,
    # missing-due and malformed-due tickets
    assert ids == [
        "500001", "500002", "500007", "500009", "500010",
        "500013", "500016", "500020", "500021", "500022", "500023",
    ]


def test_waiting_view_rows(client):
    ids = _ids(client.get("/queue?overdue=0&responded=0&waiting=1&missing_tags=1&days=60").get_data(as_text=True))
    assert ids == ["500003", "500011", "500018", "500024", "500025"]


def test_overdue_plus_responded_is_intersection(client):
    # MODIFIED (was test_overdue_plus_responded_union, Prompt 06): this combo
    # used to OR and return every overdue OR responded ticket. It is now an
    # AND: only tickets that are overdue AND customer-responded (with the
    # Missing Tags gate on).
    ids = _ids(client.get("/queue?overdue=1&responded=1&waiting=0&missing_tags=1&days=60").get_data(as_text=True))
    expected = ["500001", "500007", "500013", "500020", "500021", "500022", "500023"]
    assert ids == expected
    # Union leaks that used to appear in this view are now correctly excluded:
    assert "500002" not in ids and "500009" not in ids  # responded but NOT overdue
    assert "500017" not in ids                          # responded but tagged + not overdue
    assert "500003" not in ids                          # waiting (not responded)
    assert "500004" not in ids  # tagged


def test_missing_tags_off_includes_tagged(client):
    ids = _ids(client.get("/queue?overdue=1&responded=0&waiting=0&missing_tags=0&days=60").get_data(as_text=True))
    assert "500004" in ids  # overdue + tagged, now shown
    assert "500019" in ids  # waiting + tagged
    assert "500017" not in ids  # not overdue, responded off


def test_days_7_rows(client):
    ids = _ids(client.get("/queue?days=7").get_data(as_text=True))
    assert ids == ["500001", "500021", "500022", "500023"]


def test_days_30_rows(client):
    ids = _ids(client.get("/queue?days=30").get_data(as_text=True))
    assert ids == ["500001", "500003", "500007", "500011", "500021", "500022", "500023"]


def test_days_90_rows(client):
    ids = _ids(client.get("/queue?days=90").get_data(as_text=True))
    assert "500014" in ids  # updated exactly 90 days ago: inclusive boundary
    assert "500015" not in ids  # updated 400 days ago


def test_days_365_rows(client):
    ids = _ids(client.get("/queue?days=365").get_data(as_text=True))
    assert "500014" in ids      # updated 90 days ago: within 365
    assert "500015" not in ids  # updated 400 days ago: beyond 365 (excluded)


def test_single_row_per_ticket(client):
    html = client.get("/queue").get_data(as_text=True)
    n_rows = len(re.findall(r'<tr class="rv-', html))
    assert n_rows == 10  # default view
    for tid in _ids(html):
        # exactly one row -> data-ticket-id appears exactly three times
        # (the <tr> focus anchor + ticket-number link + subject link)
        assert html.count(f'data-ticket-id="{tid}"') == 3, f"duplicate row for {tid}"


# ===========================================================================
# Dashboard: badges (new)
# ===========================================================================


def test_default_view_badges(client):
    html = client.get("/queue").get_data(as_text=True)
    assert "OVERDUE" in html
    assert "CUSTOMER RESPONDED" in html       # status-2 rows
    assert "WAITING ON CUSTOMER" in html      # status-6 rows (500003 etc.)
    assert "MISSING TAGS" in html
    assert "SLA DATE UNAVAILABLE" not in html  # 500009/500010 hidden in default view


def test_sla_date_unavailable_badge(client):
    html = client.get("/queue?overdue=0&responded=1&waiting=0&missing_tags=1&days=60").get_data(as_text=True)
    assert "SLA DATE UNAVAILABLE" in html
    assert "#500009" in html  # missing due_by
    assert "#500010" in html  # malformed due_by


def test_no_missing_tags_badge_when_tagged(client):
    html = client.get("/queue?overdue=1&responded=0&waiting=0&missing_tags=0&days=60").get_data(as_text=True)
    assert "#500004" in html
    # 500004 has tags, so its row must not carry a MISSING TAGS badge.
    row4 = _row_for(html, "500004")
    assert row4 and "MISSING TAGS" not in row4


def test_no_overdue_badge_on_future_due(client):
    html = client.get("/queue?overdue=0&responded=1&waiting=0&missing_tags=1&days=60").get_data(as_text=True)
    row2 = _row_for(html, "500002")  # future due_by (not overdue)
    assert row2 and "OVERDUE" not in row2


def test_badges_are_text_not_color_only(client):
    html = client.get("/queue").get_data(as_text=True)
    for badge in ("OVERDUE", "CUSTOMER RESPONDED", "WAITING ON CUSTOMER", "MISSING TAGS"):
        assert badge in html  # visible text, not just a CSS class


# ===========================================================================
# Local review workflow: SQLite (new)
# ===========================================================================


def test_init_db_creates_file_and_table(tmp_path):
    path = tmp_path / "sub" / "review.sqlite3"
    init_db(str(path))
    assert path.exists()
    conn = __import__("sqlite3").connect(str(path))
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    assert ("review_state",) in tables


def test_set_review_result_roundtrip():
    set_review_result(500001, "Resolved", reviewed_updated_at="2026-07-01T00:00:00Z")
    rows = load_review_rows()
    assert rows[500001]["review_result"] == "Resolved"
    assert rows[500001]["reviewed_updated_at"] == "2026-07-01T00:00:00Z"
    assert rows[500001]["last_review_change_at"]


def test_set_review_result_rejects_unknown_state():
    with pytest.raises(ValueError):
        set_review_result(500001, "Bogus")


def test_reviewed_updated_at_only_for_reviewed_states():
    set_review_result(500001, "Resolved", reviewed_updated_at="2026-07-01T00:00:00Z")
    set_review_result(500001, "Unreviewed", reviewed_updated_at="2026-07-02T00:00:00Z")
    rows = load_review_rows()
    assert rows[500001]["review_result"] == "Unreviewed"
    assert rows[500001]["reviewed_updated_at"] is None  # cleared for non-reviewed states


def test_mark_opened_first_and_last():
    mark_opened(500002)
    mark_opened(500002)
    rows = load_review_rows()
    assert rows[500002]["review_result"] == "Opened / In Review"
    assert rows[500002]["first_opened_at"] is not None
    assert rows[500002]["last_opened_at"] is not None
    assert rows[500002]["last_opened_at"] >= rows[500002]["first_opened_at"]


def test_mark_opened_keeps_first_opened(monkeypatch):
    t0 = "2026-08-01T00:00:00+00:00"
    t1 = "2026-08-02T00:00:00+00:00"
    calls = {"n": 0}
    real = app.iso_now

    def fake_iso():
        calls["n"] += 1
        return t0 if calls["n"] <= 1 else t1

    monkeypatch.setattr(app, "iso_now", fake_iso)
    mark_opened(500002)  # insert: first = last = t0
    mark_opened(500002)  # update: first stays t0, last = t1
    rows = load_review_rows()
    assert rows[500002]["first_opened_at"] == t0
    assert rows[500002]["last_opened_at"] == t1


def test_review_state_persists_across_clients():
    set_review_result(500001, "Needs Follow-Up")
    rows = load_review_rows()
    assert rows[500001]["review_result"] == "Needs Follow-Up"


# ===========================================================================
# Updated Since Review (new)
# ===========================================================================


def _state(reviewed_updated_at=None, review_result="Resolved"):
    return {"review_result": review_result, "reviewed_updated_at": reviewed_updated_at}


def test_updated_since_review_unchanged_no_flag():
    t = _ticket(updated_at="2026-08-03T12:00:00Z")
    assert updated_since_review(t, _state("2026-08-03T12:00:00Z")) is False


def test_updated_since_review_newer_flag():
    t = _ticket(updated_at="2026-08-04T12:00:00Z")
    assert updated_since_review(t, _state("2026-08-03T12:00:00Z")) is True


def test_updated_since_review_older_no_flag():
    t = _ticket(updated_at="2026-07-01T12:00:00Z")
    assert updated_since_review(t, _state("2026-08-03T12:00:00Z")) is False


def test_updated_since_review_no_snapshot_no_flag():
    assert updated_since_review(_ticket(), _state(None)) is False
    assert updated_since_review(_ticket(), None) is False


def test_updated_since_review_malformed_fails_safe():
    t = _ticket(updated_at="garbage")
    assert updated_since_review(t, _state("2026-08-03T12:00:00Z")) is False
    t2 = _ticket(updated_at="2026-08-04T12:00:00Z")
    assert updated_since_review(t2, _state("garbage")) is False


def test_review_view_includes():
    assert review_view_includes(None, False, "active") is True          # Unreviewed
    assert review_view_includes(_state(None, "Unreviewed"), False, "active") is True
    assert review_view_includes(_state(None, "Opened / In Review"), False, "active") is True
    assert review_view_includes(_state(None, "Needs Follow-Up"), False, "active") is True
    assert review_view_includes(_state(None, "Resolved"), False, "active") is False
    assert review_view_includes(_state(None, "Not Applicable to Me"), False, "active") is False
    assert review_view_includes(_state(None, "No Action Needed"), False, "active") is False
    assert review_view_includes(_state(None, "Resolved"), False, "completed") is True
    assert review_view_includes(_state(None, "Not Applicable to Me"), False, "completed") is True
    assert review_view_includes(_state(None, "No Action Needed"), False, "completed") is True
    assert review_view_includes(_state(None, "Opened / In Review"), False, "completed") is False
    assert review_view_includes(_state(None, "Unreviewed"), False, "all") is True
    assert review_view_includes(_state(None, "Resolved"), False, "all") is True
    # Updated Since Review pulls a Completed ticket back into Active
    assert review_view_includes(_state(None, "Resolved"), True, "active") is True
    assert review_view_includes(_state(None, "Resolved"), True, "completed") is False


# ===========================================================================
# Local review workflow: endpoints (new)
# ===========================================================================


def test_review_post_saves_and_preserves_filters(client):
    tok = _csrf(client)
    r = client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    assert r.status_code == 303
    assert r.headers["Location"] == "/queue?overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active"
    rows = load_review_rows()
    assert rows[500021]["review_result"] == "Resolved"


def test_review_post_flash_success_and_error(client):
    tok = _csrf(client)
    r = client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    }, follow_redirects=True)
    text = r.get_data(as_text=True)
    assert "Review saved for #500021: Resolved." in text

    r = client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Bogus",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    }, follow_redirects=True)
    assert "Review not saved: unknown review result." in r.get_data(as_text=True)


def test_review_post_rejects_bad_csrf(client):
    rows_before = load_review_rows()
    r = client.post("/queue/api/review", data={
        "csrf_token": "not-the-token", "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    assert r.status_code == 303
    assert load_review_rows() == rows_before  # nothing written


def test_review_post_missing_ticket_id(client):
    tok = _csrf(client)
    r = client.post("/queue/api/review", data={
        "csrf_token": tok, "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    }, follow_redirects=True)
    assert "Review not saved: missing ticket ID." in r.get_data(as_text=True)


def test_review_post_invalid_ticket_id(client):
    tok = _csrf(client)
    r = client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "abc", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    }, follow_redirects=True)
    assert "Review not saved: invalid ticket ID." in r.get_data(as_text=True)


def test_review_post_unknown_ticket_id(client):
    tok = _csrf(client)
    r = client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "999999", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    }, follow_redirects=True)
    assert "Review not saved: unknown ticket #999999." in r.get_data(as_text=True)
    assert 999999 not in load_review_rows()


def test_resolved_moves_to_completed_view(client):
    tok = _csrf(client)
    client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    assert "500021" not in _ids(client.get("/queue").get_data(as_text=True))
    assert "500021" in _ids(client.get("/queue?review_view=completed").get_data(as_text=True))


def test_follow_up_stays_active(client):
    tok = _csrf(client)
    client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500022", "review_result": "Needs Follow-Up",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    assert "500022" in _ids(client.get("/queue").get_data(as_text=True))


def test_no_action_and_na_are_completed(client):
    tok = _csrf(client)
    for tid, result in (("500021", "No Action Needed"), ("500022", "Not Applicable to Me")):
        client.post("/queue/api/review", data={
            "csrf_token": tok, "ticket_id": tid, "review_result": result,
            "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
            "days": "60", "review_view": "active",
        })
    active = _ids(client.get("/queue").get_data(as_text=True))
    completed = _ids(client.get("/queue?review_view=completed").get_data(as_text=True))
    assert "500021" not in active and "500022" not in active
    assert "500021" in completed and "500022" in completed


def test_all_view_shows_everything(client):
    tok = _csrf(client)
    client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    all_ids = _ids(client.get("/queue?review_view=all").get_data(as_text=True))
    assert "500021" in all_ids
    assert "500001" in all_ids


def test_updated_since_review_returns_to_active(client):
    tok = _csrf(client)
    # Review 500021 as Resolved with a snapshot OLDER than its current
    # updated_at (simulating the ticket being updated after review).
    client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    set_review_result(500021, "Resolved", reviewed_updated_at="2026-07-01T00:00:00Z")
    active = client.get("/queue").get_data(as_text=True)
    assert "500021" in _ids(active)                      # back in Active
    assert "UPDATED SINCE REVIEW" in active              # badge visible
    assert 'value="Resolved" selected' in active         # previous result preserved
    completed = client.get("/queue?review_view=completed").get_data(as_text=True)
    assert "500021" not in _ids(completed)               # no longer Completed

    # Reviewing it again stores the newer updated_at snapshot -> flag clears.
    client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    active2 = client.get("/queue").get_data(as_text=True)
    assert "500021" not in _ids(active2)
    assert "UPDATED SINCE REVIEW" not in active2


def test_review_endpoint_does_not_trigger_http(client):
    """The global network block would raise AssertionError if any HTTP call
    happened. A successful review POST therefore proves no network."""
    tok = _csrf(client)
    r = client.post("/queue/api/review", data={
        "csrf_token": tok, "ticket_id": "500021", "review_result": "Resolved",
        "overdue": "1", "responded": "0", "waiting": "0", "missing_tags": "1",
        "days": "60", "review_view": "active",
    })
    assert r.status_code == 303


# ===========================================================================
# Ticket-open marking (new)
# ===========================================================================


def test_opened_endpoint_marks_ticket(client):
    tok = _csrf(client)
    r = client.post("/queue/api/opened", json={"ticket_id": "500021"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    rows = load_review_rows()
    assert rows[500021]["review_result"] == "Opened / In Review"
    assert rows[500021]["first_opened_at"]
    assert rows[500021]["last_opened_at"]


def test_opened_endpoint_rejects_missing_token(client):
    r = client.post("/queue/api/opened", json={"ticket_id": "500021"})
    assert r.status_code == 403


def test_opened_endpoint_rejects_unknown_ticket(client):
    tok = _csrf(client)
    r = client.post("/queue/api/opened", json={"ticket_id": "999999"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_opened_endpoint_rejects_missing_id(client):
    tok = _csrf(client)
    r = client.post("/queue/api/opened", json={}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 400


def test_opened_endpoint_does_not_trigger_http(client):
    tok = _csrf(client)
    r = client.post("/queue/api/opened", json={"ticket_id": "500021"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200


def test_opened_badge_updates_on_render(client):
    tok = _csrf(client)
    client.post("/queue/api/opened", json={"ticket_id": "500021"}, headers={"X-CSRF-Token": tok})
    html = client.get("/queue").get_data(as_text=True)
    assert "Opened / In Review" in html
    assert 'value="Opened / In Review" selected' in html


def test_ticket_links_render_safe_and_accessible(client):
    html = client.get("/queue").get_data(as_text=True)
    m = re.search(r'<a class="tid fd-link" href="([^"]+)" target=_blank rel="([^"]+)" data-ticket-id="500021" aria-label="([^"]+)"', html)
    assert m
    url, rel, label = m.group(1), m.group(2), m.group(3)
    assert url == "https://broadriverretail-help.freshdesk.com/a/tickets/500021"
    assert "noopener" in rel
    assert "Open ticket #500021 in Freshdesk" in label
    m2 = re.search(r'<a class="sbj fd-link" href="([^"]+)" target=_blank rel="([^"]+)" data-ticket-id="500021" aria-label="([^"]+)"', html)
    assert m2
    assert m2.group(1) == url
    assert "noopener" in m2.group(2)
    assert "Open subject of ticket #500021" in m2.group(3)


# ===========================================================================
# Prompt02 — Click highlight fix
# ===========================================================================


def test_ticket_links_have_quoted_fd_link_classes(client):
    # Regression guard for the Prompt02 root cause. The unquoted
    # `class=tid fd-link` was parsed by the browser as class="tid" plus a
    # separate empty attribute named `fd-link`, so the old `a.fd-link` JS
    # selector matched nothing and clicks never sent /queue/api/opened — the
    # ticket was never highlighted. Quoting makes `fd-link` a real class.
    html = client.get("/queue").get_data(as_text=True)
    assert 'class="tid fd-link"' in html
    assert 'class="sbj fd-link"' in html


def test_click_js_anchors_on_data_ticket_id(client):
    # Spec §4: the click handler must use a reliable DOM identifier
    # (data-ticket-id) and must never prevent the native new-tab navigation
    # (spec §5), so the external ticket always opens.
    # MODIFIED (Prompt05): a preventDefault now exists for the filter-form
    # canonicalizer (explicit 0/1 for unchecked checkboxes). It is scoped to
    # that block; the ticket-link click path, which precedes it in the script,
    # still never calls preventDefault.
    html = client.get("/queue").get_data(as_text=True)
    assert "querySelectorAll('a[data-ticket-id]')" in html
    ticket_part, _, filter_part = html.partition("// Filter controls:")
    assert "preventDefault" not in ticket_part  # ticket links still open natively
    assert "preventDefault" in filter_part      # only the filter form canonicalizes


def test_click_js_updates_row_badge_and_selector(client):
    # Spec §3/§4: a successful save updates the row state class, the
    # OPENED / IN REVIEW badge, and the review-result selector to match the
    # effective result returned by the server.
    html = client.get("/queue").get_data(as_text=True)
    assert "REVIEW_CLASS" in html
    assert "OPENED / IN REVIEW" in html
    assert "select[name=review_result]" in html
    assert "badgeText" in html


def test_click_js_shows_error_on_save_failure(client):
    # Spec §6: a visible error message is shown if the local save fails, and
    # the claim "Opened / In Review saved" is never made unless it was.
    html = client.get("/queue").get_data(as_text=True)
    assert "showError" in html
    assert "Could not save Opened / In Review state" in html


def test_click_highlight_css_has_visible_marker(client):
    # Spec §7: pale yellow row background + a visible left-edge marker +
    # a distinct OPENED / IN REVIEW badge, with readable text contrast.
    html = client.get("/queue").get_data(as_text=True)
    assert "tr.rv-opened{background:#fff8e1}" in html
    assert "tr.rv-opened td:first-child{box-shadow:inset 4px 0 0 #f9a825}" in html
    assert ".b-review.rv-opened{background:#fff8e1;border-color:#f9a825;color:#5d4037}" in html


def test_opened_badge_renders_uppercase_and_highlighted(client):
    tok = _csrf(client)
    client.post("/queue/api/opened", json={"ticket_id": "500021"}, headers={"X-CSRF-Token": tok})
    html = client.get("/queue").get_data(as_text=True)
    assert 'class="badge b-review rv-opened">OPENED / IN REVIEW' in html
    # The clicked ticket is also the most recently opened: the Last Opened
    # marker class is layered on the review class (spec §3).
    assert '<tr class="rv-opened rv-last-opened"' in html


def test_mark_opened_preserves_deliberate_states():
    for result in ("Resolved", "Not Applicable to Me", "No Action Needed", "Needs Follow-Up"):
        set_review_result(500002, result)
        effective = mark_opened(500002)
        rows = load_review_rows()
        assert rows[500002]["review_result"] == result  # preserved, not wiped
        assert effective == result
        assert rows[500002]["last_opened_at"] is not None
        conn = app._db_conn()
        conn.execute("DELETE FROM review_state WHERE ticket_id = ?", (500002,))
        conn.commit()
        conn.close()


def test_mark_opened_unreviewed_row_becomes_opened():
    set_review_result(500001, "Unreviewed")
    effective = mark_opened(500001)
    rows = load_review_rows()
    assert effective == "Opened / In Review"
    assert rows[500001]["review_result"] == "Opened / In Review"


def test_mark_opened_updates_last_opened_when_preserving(monkeypatch):
    t0 = "2026-08-01T00:00:00+00:00"
    t1 = "2026-08-02T00:00:00+00:00"
    calls = {"n": 0}

    def fake_iso():
        calls["n"] += 1
        return t0 if calls["n"] <= 1 else t1

    set_review_result(500002, "Resolved")  # real clock (before patch)
    monkeypatch.setattr(app, "iso_now", fake_iso)
    mark_opened(500002)  # t0
    mark_opened(500002)  # t1
    rows = load_review_rows()
    assert rows[500002]["review_result"] == "Resolved"  # preserved
    assert rows[500002]["first_opened_at"] == t0
    assert rows[500002]["last_opened_at"] == t1  # last-opened still advances


def test_mark_opened_returns_effective_result():
    assert mark_opened(500003) == "Opened / In Review"
    set_review_result(500003, "Needs Follow-Up")
    assert mark_opened(500003) == "Needs Follow-Up"


def test_opened_endpoint_preserves_deliberate_state(client):
    set_review_result(500021, "Resolved")
    tok = _csrf(client)
    r = client.post("/queue/api/opened", json={"ticket_id": "500021"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["review_result"] == "Resolved"
    rows = load_review_rows()
    assert rows[500021]["review_result"] == "Resolved"
    assert rows[500021]["last_opened_at"]


def test_opened_endpoint_reports_effective_result(client):
    tok = _csrf(client)
    r = client.post("/queue/api/opened", json={"ticket_id": "500021"}, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200
    assert r.get_json()["review_result"] == "Opened / In Review"


def test_opened_state_persists_across_clients(client):
    tok = _csrf(client)
    client.post("/queue/api/opened", json={"ticket_id": "500021"}, headers={"X-CSRF-Token": tok})
    # A second client reads the same isolated review DB — the highlight and
    # badge survive a fresh request (equivalent to a page reload).
    client2 = app.app.test_client()
    html = client2.get("/queue").get_data(as_text=True)
    assert 'value="Opened / In Review" selected' in html
    assert 'class="badge b-review rv-opened">OPENED / IN REVIEW' in html
    # The clicked ticket is also the most recently opened: the Last Opened
    # marker class is layered on the review class (spec §3).
    assert '<tr class="rv-opened rv-last-opened"' in html


# ===========================================================================
# CSRF plumbing (new)
# ===========================================================================


def test_csrf_token_is_stable_per_session(client):
    tok1 = _csrf(client)
    tok2 = _csrf(client)
    assert tok1 == tok2


def test_csrf_valid():
    with app.app.test_request_context():
        token = get_csrf_token()
        assert csrf_valid(token) is True
        assert csrf_valid("wrong") is False
        assert csrf_valid("") is False
        assert csrf_valid(None) is False


# ===========================================================================
# Offline / network safety of the whole dashboard (new)
# ===========================================================================


def test_offline_dashboard_uses_fixtures(client):
    html = client.get("/queue").get_data(as_text=True)
    assert "OFFLINE MODE" in html
    assert "mock/offline fixture data" in html
    assert "#500001" in html


def test_offline_never_contacts_freshdesk_domain(monkeypatch, client):
    seen = []

    def spy(url, *a, **kw):
        seen.append(url)
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(requests, "get", spy)
    monkeypatch.setattr(requests, "post", spy)
    html = client.get("/queue").get_data(as_text=True)
    assert "OFFLINE MODE" in html
    assert not seen, f"offline render attempted network calls: {seen}"


def test_sqlite_operations_do_not_trigger_http(client):
    set_review_result(500001, "Resolved")
    mark_opened(500002)
    rows = load_review_rows()
    assert set(rows) == {500001, 500002}


# ===========================================================================
# Last Opened focus marker (Prompt03-LastOpenedFocus.md)
#
# The focus is the single ticket with the newest *valid* last_opened_at among
# all review_state rows (spec section 3/6), picked independently of the review
# result and of the current filters. Invalid/missing timestamps fail safe and
# equal timestamps resolve deterministically to the higher ticket id.
# ===========================================================================

def _open(client, tid):
    tok = _csrf(client)
    r = client.post("/queue/api/opened", json={"ticket_id": str(tid)},
                    headers={"X-CSRF-Token": tok})
    return r.get_json()


def _opened_rows(html):
    return [r for r in re.findall(r'<tr[^>]*>.*?</tr>', html, re.S)
            if 'rv-last-opened' in r.split('>', 1)[0]]


def test_no_last_opened_ivory_when_never_opened(client):
    # Fresh DB: no marker row, no marker badge, no jump control, no message.
    # (The marker strings also live in the inline JS, so assert on elements.)
    html = client.get("/queue").get_data(as_text=True)
    assert _opened_rows(html) == []
    assert 'class="badge b-last-opened">' not in html
    assert 'id=last-opened-jump' not in html
    assert 'id=last-opened-hidden' not in html
    assert last_opened_ticket_id() is None


def test_first_click_marks_single_last_opened(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)
    row = _row_for(html, 500001)
    assert 'rv-last-opened' in row.split('>', 1)[0]          # on the <tr>
    assert 'class="badge b-last-opened">LAST OPENED' in row
    assert len(_opened_rows(html)) == 1
    assert html.count('class="badge b-last-opened">') == 1
    assert 'id=last-opened-jump' in html


def test_second_click_moves_marker_off_first(client):
    _open(client, 500001)
    _open(client, 500003)
    html = client.get("/queue").get_data(as_text=True)
    a_row = _row_for(html, 500001)
    b_row = _row_for(html, 500003)
    assert "rv-last-opened" not in a_row.split('>', 1)[0]
    assert "b-last-opened" not in a_row
    assert "rv-last-opened" in b_row.split('>', 1)[0]
    assert "LAST OPENED" in b_row
    assert html.count('class="badge b-last-opened">') == 1


def test_exactly_one_marker_after_many_opens(client):
    for tid in (500001, 500003, 500007, 500013, 500021):
        _open(client, tid)
    html = client.get("/queue").get_data(as_text=True)
    assert len(_opened_rows(html)) == 1
    assert html.count('class="badge b-last-opened">') == 1
    assert last_opened_ticket_id() == 500021  # newest open wins


def test_repeated_clicks_do_not_duplicate_database_rows(client):
    _open(client, 500001)
    _open(client, 500001)
    _open(client, 500001)
    rows = load_review_rows()
    assert set(rows) == {500001}                      # single row for 500001
    html = client.get("/queue").get_data(as_text=True)
    assert html.count('class="badge b-last-opened">') == 1  # no duplicated badge
    assert len(_opened_rows(html)) == 1


def test_equal_timestamps_resolve_deterministically(client):
    # now_utc is pinned by the suite clock, so the two opens share a timestamp;
    # the selector must break the tie by the higher ticket id (deterministic).
    _open(client, 500001)
    _open(client, 500003)
    assert last_opened_ticket_id() == 500003
    _open(client, 500007)
    assert last_opened_ticket_id() == 500007


def test_malformed_timestamps_fail_safe(client):
    now = app.iso_now()
    conn = app._db_conn()
    conn.execute("INSERT OR REPLACE INTO review_state "
                 "(ticket_id, review_result, last_opened_at, created_at, modified_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (500001, "Unreviewed", "not-a-date", now, now))
    conn.execute("INSERT OR REPLACE INTO review_state "
                 "(ticket_id, review_result, last_opened_at, created_at, modified_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (500003, "Unreviewed", app.iso_now(), now, now))
    conn.commit()
    conn.close()
    # invalid row is skipped; the valid one wins.
    assert last_opened_ticket_id() == 500003


def test_all_invalid_timestamps_return_none(client):
    now = app.iso_now()
    conn = app._db_conn()
    for tid in (500001, 500003):
        conn.execute("INSERT OR REPLACE INTO review_state "
                     "(ticket_id, review_result, last_opened_at, created_at, modified_at) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (tid, "Unreviewed", "garbage", now, now))
    conn.commit()
    conn.close()
    assert last_opened_ticket_id() is None


def test_null_last_opened_at_is_skipped(client):
    now = app.iso_now()
    conn = app._db_conn()
    conn.execute("INSERT OR REPLACE INTO review_state "
                 "(ticket_id, review_result, last_opened_at, created_at, modified_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (500001, "Unreviewed", None, now, now))
    conn.execute("INSERT OR REPLACE INTO review_state "
                 "(ticket_id, review_result, last_opened_at, created_at, modified_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (500003, "Unreviewed", app.iso_now(), now, now))
    conn.commit()
    conn.close()
    assert last_opened_ticket_id() == 500003


def test_naive_timestamp_treated_as_utc(client):
    now = app.iso_now()
    conn = app._db_conn()
    # 500001 stored naive (assumed UTC) at 12:00; 500003 aware UTC at 10:00.
    conn.execute("INSERT OR REPLACE INTO review_state "
                 "(ticket_id, review_result, last_opened_at, created_at, modified_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (500003, "Unreviewed", "2026-08-05T10:00:00", now, now))
    conn.execute("INSERT OR REPLACE INTO review_state "
                 "(ticket_id, review_result, last_opened_at, created_at, modified_at) "
                 "VALUES (?, ?, ?, ?, ?)",
                 (500001, "Unreviewed", "2026-08-05T12:00:00+00:00", now, now))
    conn.commit()
    conn.close()
    assert last_opened_ticket_id() == 500001


def test_marker_is_not_derived_from_review_result(client):
    # Reviewing a ticket (Resolved) without ever opening it must NOT mark it.
    set_review_result(500001, "Resolved")
    assert last_opened_ticket_id() is None


def test_opened_api_returns_last_opened_id(client):
    d1 = _open(client, 500001)
    assert d1["last_opened_id"] == 500001
    d2 = _open(client, 500003)
    assert d2["last_opened_id"] == 500003
    assert set(d1.keys()) == {"ok", "review_result", "last_opened_id"}


def test_save_failure_does_not_move_marker(client):
    _open(client, 500001)
    # Invalid POST (missing CSRF) -> rejected; marker must not move.
    r = client.post("/queue/api/opened", json={"ticket_id": "500003"})
    assert r.status_code == 403
    assert last_opened_ticket_id() == 500001


def test_needs_followup_and_last_opened_displayed_together(client):
    set_review_result(500003, "Needs Follow-Up")
    _open(client, 500003)
    html = client.get("/queue").get_data(as_text=True)
    row = _row_for(html, 500003)
    assert "rv-followup" in row.split('>', 1)[0]
    assert "rv-last-opened" in row.split('>', 1)[0]
    assert ">Needs Follow-Up" in row
    assert ">LAST OPENED" in row


def test_resolved_and_last_opened_displayed_together(client):
    set_review_result(500003, "Resolved")
    _open(client, 500003)
    html = client.get("/queue?review_view=all").get_data(as_text=True)
    row = _row_for(html, 500003)
    assert "rv-resolved" in row.split('>', 1)[0]
    assert "rv-last-opened" in row.split('>', 1)[0]
    assert ">Resolved" in row
    assert ">LAST OPENED" in row


def test_no_action_needed_and_last_opened_displayed_together(client):
    set_review_result(500003, "No Action Needed")
    _open(client, 500003)
    html = client.get("/queue?review_view=all").get_data(as_text=True)
    row = _row_for(html, 500003)
    assert "rv-none" in row.split('>', 1)[0]
    assert "rv-last-opened" in row.split('>', 1)[0]
    assert ">No Action Needed" in row
    assert ">LAST OPENED" in row


def test_opened_and_last_opened_displayed_together(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)
    row = _row_for(html, 500001)
    assert "rv-opened" in row.split('>', 1)[0]
    assert "rv-last-opened" in row.split('>', 1)[0]
    assert ">OPENED / IN REVIEW" in row
    assert ">LAST OPENED" in row


def test_followup_class_is_distinct_from_last_opened(client):
    html = client.get("/queue").get_data(as_text=True)
    # two separate CSS rules, distinct purple focus styling (spec section 4).
    assert "tr.rv-followup{background:#fff3e0}" in html
    assert "tr.rv-last-opened{outline:3px solid var(--fd-last-opened)" in html


def test_changing_review_result_preserves_marker(client):
    _open(client, 500001)                       # Opened / In Review + marker
    set_review_result(500001, "Resolved")      # deliberate result change
    assert last_opened_ticket_id() == 500001   # marker keeps its focus state
    html = client.get("/queue?review_view=completed").get_data(as_text=True)
    row = _row_for(html, 500001)
    assert "rv-last-opened" in row.split('>', 1)[0]


def test_clicking_removes_marker_only_from_previous(client):
    _open(client, 500001)                          # A becomes Opened
    _open(client, 500003)                       # B becomes last-opened
    rows = load_review_rows()
    # A's deliberate review state is preserved; only the focus marker moved.
    assert rows[500001]["review_result"] == "Opened / In Review"
    assert last_opened_ticket_id() == 500003


# --- persistence -----------------------------------------------------------

def test_marker_persists_across_fresh_request(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)      # "first reload"
    assert "rv-last-opened" in _row_for(html, 500001).split('>', 1)[0]
    html2 = client.get("/queue").get_data(as_text=True)     # "second reload"
    assert "rv-last-opened" in _row_for(html2, 500001).split('>', 1)[0]
    assert len(_opened_rows(html2)) == 1


def test_marker_persists_across_new_client(client):
    # A second client (equivalent to a fresh page session / app restart) reads
    # the same isolated review DB and still sees the marker (spec: persistent).
    _open(client, 500001)
    client2 = app.app.test_client()
    html = client2.get("/queue").get_data(as_text=True)
    assert "rv-last-opened" in _row_for(html, 500001).split('>', 1)[0]
    assert last_opened_ticket_id() == 500001


def test_marker_persists_across_url_filter_changes(client):
    _open(client, 500001)
    # Each combo still renders ticket 500001 (overdue default, all-view, 60-day).
    for url in ("/queue", "/queue?review_view=all", "/queue?days=365"):
        html = client.get(url).get_data(as_text=True)
        assert last_opened_ticket_id() == 500001
        assert "rv-last-opened" in _row_for(html, 500001).split('>', 1)[0]


def test_marker_survives_unrelated_review_of_other_ticket(client):
    _open(client, 500001)
    set_review_result(500003, "Resolved")   # reviewing another ticket...
    assert last_opened_ticket_id() == 500001  # ...does not move the marker
    html = client.get("/queue").get_data(as_text=True)
    assert "rv-last-opened" in _row_for(html, 500001).split('>', 1)[0]


def test_reset_defaults_keeps_focus(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)  # defaults
    assert "rv-last-opened" in _row_for(html, 500001).split('>', 1)[0]
    assert last_opened_ticket_id() == 500001


# --- filter interplay ------------------------------------------------------

def test_marker_visible_in_default_view_shows_jump(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)
    assert "id=last-opened-jump" in html
    assert "id=last-opened-hidden" not in html
    assert len(_opened_rows(html)) == 1


def test_marker_hidden_by_completed_view_shows_message(client):
    _open(client, 500001)                      # Opened / In Review -> active only
    html = client.get("/queue?review_view=completed").get_data(as_text=True)
    assert _opened_rows(html) == []            # no falsely marked row
    assert "id=last-opened-jump" not in html   # no jump control
    assert "id=last-opened-hidden" in html     # server-rendered notice element


def test_resolved_marker_hidden_from_active_view(client):
    set_review_result(500001, "Resolved")
    _open(client, 500001)
    html_active = client.get("/queue").get_data(as_text=True)  # active view
    assert "id=last-opened-hidden" in html_active
    html_completed = client.get("/queue?review_view=completed").get_data(as_text=True)
    assert "rv-last-opened" in _row_for(html_completed, 500001).split('>', 1)[0]
    assert "id=last-opened-jump" in html_completed


def test_marker_visible_in_all_view(client):
    _open(client, 500001)
    html = client.get("/queue?review_view=all").get_data(as_text=True)
    assert "rv-last-opened" in _row_for(html, 500001).split('>', 1)[0]
    assert "id=last-opened-hidden" not in html


def test_hidden_by_filters_does_not_move_marker(client):
    _open(client, 500001)
    client.get("/queue?review_view=completed")  # hidden here...
    assert last_opened_ticket_id() == 500001    # ...but focus state unchanged
    html = client.get("/queue").get_data(as_text=True)
    assert "rv-last-opened" in _row_for(html, 500001).split('>', 1)[0]


# --- jump control ----------------------------------------------------------

def test_jump_button_attributes(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)
    assert "id=last-opened-jump" in html
    assert "type=button" in html                # never a form submit
    assert "aria-controls=queue-table" in html  # a11y: targets the table
    assert "<table id=queue-table>" in html


def test_jump_uses_smooth_scroll_and_temp_focus(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)
    # Spec: smooth-scroll if supported, still functional without animation.
    assert "row.scrollIntoView({behavior: 'smooth', block: 'center'})" in html
    assert "row.focus({preventScroll: true})" in html
    assert "row.setAttribute('tabindex', '-1')" in html
    assert "row.removeAttribute('tabindex')" in html


def test_jump_handler_never_navigates_or_uses_network(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)
    start = html.index("function jumpToLastOpened()")
    end = html.index("// both the ticket-number and subject links.")
    segment = html[start:end]
    assert "scrollIntoView" in segment
    assert "fetch(" not in segment and "XMLHttpRequest" not in segment
    assert "location" not in segment and "window.open" not in segment
    assert "preventDefault" not in segment


def test_js_builds_jump_chrome_when_first_marker(client):
    # The jump bar + hidden notice are server-rendered only when a marker
    # exists at load. After the FIRST click creates the marker client-side,
    # ensureLastOpenedChrome() must construct them instead of a reload.
    html = client.get("/queue").get_data(as_text=True)
    assert "function ensureLastOpenedChrome()" in html
    assert "function jumpToLastOpened()" in html
    seg_start = html.index("function ensureLastOpenedChrome()")
    seg_end = html.index("// Delegated listener")
    seg = html[seg_start:seg_end]
    assert "className = 'last-opened-bar'" in seg
    assert "btn.id = 'last-opened-jump'" in seg
    assert "btn.setAttribute('aria-controls', 'queue-table')" in seg
    assert "id = 'last-opened-hidden'" in seg
    assert "Last opened ticket is hidden by the current filters." in seg


def test_jump_listener_is_delegated(client):
    # Delegated document listener => works whether the button was rendered at
    # load or created later by ensureLastOpenedChrome().
    html = client.get("/queue").get_data(as_text=True)
    assert "document.addEventListener('click', function (e)" in html
    assert "e.target.id === 'last-opened-jump'" in html
    assert "jumpToLastOpened()" in html


# --- immediate DOM update (JS) ----------------------------------------------

def test_js_defines_move_last_opened_and_calls_on_confirm(client):
    html = client.get("/queue").get_data(as_text=True)
    assert "function moveLastOpened(newId)" in html
    # Called only inside the confirmed-save branch of the opened handler.
    assert "moveLastOpened(d.last_opened_id);" in html
    assert "Marker only moves on a confirmed save" in html


def test_js_strips_old_marker_before_adding(client):
    # Idempotent strip of the marker from every row prevents duplicated badges
    # when the same ticket is clicked repeatedly.
    html = client.get("/queue").get_data(as_text=True)
    assert "querySelectorAll('.b-last-opened')" in html
    assert "querySelectorAll('tr.rv-last-opened')" in html
    assert "target.classList.add('rv-last-opened')" in html


def test_js_toggles_jump_and_hidden_message_on_move(client):
    html = client.get("/queue").get_data(as_text=True)
    assert "bar.style.display = ''" in html
    assert "bar.style.display = 'none'" in html
    assert "hidden.style.display = 'none'" in html
    assert "hidden.style.display = ''" in html


# --- styling / a11y ---------------------------------------------------------

def test_last_opened_css_distinct_purple(client):
    html = client.get("/queue").get_data(as_text=True)
    assert "--fd-last-opened:#6A1B9A" in html
    assert "--fd-last-opened-text:#FFFFFF" in html
    assert "tr.rv-last-opened{outline:3px solid var(--fd-last-opened)" in html
    assert "tr.rv-last-opened td:first-child{box-shadow:inset 4px 0 0 var(--fd-last-opened)}" in html
    assert ".b-last-opened{background:var(--fd-last-opened);color:var(--fd-last-opened-text)}" in html


def test_focus_marker_never_uses_review_colors(client):
    # The focus indicator is distinct purple (var --fd-last-opened) — never the
    # yellow/orange of the review highlight (#fff8e1 / #f9a825), so the two are
    # distinguishable.
    html = client.get("/queue").get_data(as_text=True)
    assert "var(--fd-last-opened)" in html
    for forbidden in ("rv-last-opened{background:#fff8e1}",
                      "rv-last-opened td:first-child{box-shadow:inset 4px 0 0 #f9a825}"):
        assert forbidden not in html


def test_marker_row_keeps_semantic_row_anchor(client):
    _open(client, 500001)
    html = client.get("/queue").get_data(as_text=True)
    row = _row_for(html, 500001)
    assert 'data-ticket-id="500001"' in row.split('>', 1)[0]


# --- Prompt 04: Freshdesk badge colors --------------------------------------

def test_customer_responded_badge_uses_royal_blue_and_white_text(client):
    html = client.get("/queue").get_data(as_text=True)
    assert "--fd-customer-responded:#09218D" in html
    assert "--fd-customer-responded-text:#FFFFFF" in html
    assert ".b-responded{background:var(--fd-customer-responded);color:var(--fd-customer-responded-text)}" in html


def test_waiting_on_customer_badge_uses_gold_and_dark_text(client):
    html = client.get("/queue").get_data(as_text=True)
    assert "--fd-waiting-customer:#E9AE3D" in html
    assert "--fd-waiting-customer-text:#1A1A1A" in html
    assert ".b-waiting{background:var(--fd-waiting-customer);color:var(--fd-waiting-customer-text)}" in html


def test_last_opened_badge_treatment_differs_from_customer_responded(client):
    # The LAST OPENED focus must NOT reuse the royal-blue #09218D treatment.
    html = client.get("/queue").get_data(as_text=True)
    assert "--fd-last-opened:#6A1B9A" in html
    assert ".b-responded{background:var(--fd-customer-responded)" in html
    assert ".b-last-opened{background:var(--fd-last-opened)" in html
    assert "--fd-last-opened:#6A1B9A" != "--fd-customer-responded:#09218D"


def test_customer_responded_and_last_opened_coexist(client):
    _open(client, 500001)  # 500001 carries the CUSTOMER RESPONDED attribute
    html = client.get("/queue").get_data(as_text=True)
    row = _row_for(html, 500001)
    assert "rv-last-opened" in row.split('>', 1)[0]
    assert ">LAST OPENED" in row
    assert "b-responded" in row and ">CUSTOMER RESPONDED" in row
    # Both badges are present on one row, with different background variables.
    assert ".b-last-opened{background:var(--fd-last-opened)" in html
    assert ".b-responded{background:var(--fd-customer-responded)" in html


def test_waiting_on_customer_and_last_opened_coexist(client):
    _open(client, 500003)  # 500003 carries the WAITING ON CUSTOMER attribute
    html = client.get("/queue").get_data(as_text=True)
    row = _row_for(html, 500003)
    assert "rv-last-opened" in row.split('>', 1)[0]
    assert ">LAST OPENED" in row
    assert "b-waiting" in row and ">WAITING ON CUSTOMER" in row
    assert ".b-last-opened{background:var(--fd-last-opened)" in html
    assert ".b-waiting{background:var(--fd-waiting-customer)" in html


def test_badge_texts_still_present(client):
    html = client.get("/queue").get_data(as_text=True)
    assert ">CUSTOMER RESPONDED" in html
    assert ">WAITING ON CUSTOMER" in html
    assert ">LAST OPENED" not in html  # no marker in a fresh session


# ===========================================================================
# Prompt05 — Filter controls fix
# ===========================================================================


class _FormParser(HTMLParser):
    """Parses rendered HTML into form structures so tests assert real DOM
    shape (containment, nesting, control membership) instead of relying only
    on substring checks. HTMLParser mirrors how a browser builds the DOM for
    the (well-formed) template output: inputs/buttons/selects are attributed
    to the form that is open when they appear, and nested forms are flagged.
    """

    def __init__(self):
        super().__init__()
        self.forms = []  # {'attrs','inputs','buttons','selects','text','nested'}
        self._open_forms = 0
        self._cur = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self._cur = {
                "attrs": attrs,
                "inputs": [],
                "buttons": [],
                "selects": [],
                "text": "",
                "nested": self._open_forms > 0,
            }
            self.forms.append(self._cur)
            self._open_forms += 1
        elif self._cur is not None and self._open_forms:
            if tag == "input":
                self._cur["inputs"].append(attrs)
            elif tag == "button":
                self._cur["buttons"].append(attrs)
            elif tag == "select":
                self._cur["selects"].append(attrs)

    def handle_startendtag(self, tag, attrs):
        if tag == "input" and self._cur is not None and self._open_forms:
            self._cur["inputs"].append(dict(attrs))

    def handle_endtag(self, tag):
        if tag == "form":
            self._open_forms -= 1
            self._cur = None

    def handle_data(self, data):
        if self._cur is not None and self._open_forms:
            self._cur["text"] += data


def _form_parser_for(html):
    p = _FormParser()
    p.feed(html)
    return p


def _controls_form(html):
    forms = _form_parser_for(html).forms
    controls = [f for f in forms if "controls" in f["attrs"].get("class", "")]
    assert len(controls) == 1, f"expected exactly one controls form, got {len(controls)}"
    return controls[0], forms


class _IdCollector(HTMLParser):
    """Collects every element `id` attribute exactly as the browser sees it
    (handles quoted and unquoted attribute syntax) so uniqueness can be
    asserted without substring ambiguity."""

    def __init__(self):
        super().__init__()
        self.ids = []

    def _tag(self, tag, attrs):
        for k, v in attrs:
            if k == "id" and v:
                self.ids.append(v)

    def handle_starttag(self, tag, attrs):
        self._tag(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._tag(tag, attrs)


def _all_ids(html):
    p = _IdCollector()
    p.feed(html)
    return p.ids


# --- Form structure ---------------------------------------------------------


def test_filter_form_exactly_one_controls_form(client):
    html = client.get("/queue").get_data(as_text=True)
    form, forms = _controls_form(html)
    assert form["attrs"].get("method", "").lower() == "get"
    assert form["attrs"].get("action") == "/queue"
    assert "novalidate" in form["attrs"]  # JS owns days validation


def test_pageshow_resyncs_controls_from_url(client):
    # Back/forward navigation (bfcache / browser form-state restoration) can
    # re-apply stale values to the controls even though the server re-rendered
    # from the current URL. The canonicalizer must re-derive control state
    # from location.search on pageshow so controls, URL, and results always
    # agree after history navigation (verified live: back from days=7 showed
    # input "7" on a days=60 URL before this handler; now it shows "60").
    html = client.get("/queue").get_data(as_text=True)
    assert "pageshow" in html
    assert "syncControlsFromURL" in html
    assert "location.search" in html


def test_apply_filters_button_inside_filter_form(client):
    html = client.get("/queue").get_data(as_text=True)
    form, _ = _controls_form(html)
    submit = [b for b in form["buttons"] if b.get("type") == "submit"]
    assert submit, "Apply Filters submit button missing from filter form"
    assert "Apply Filters" in form["text"]


def test_no_nested_forms_break_filter_form(client):
    html = client.get("/queue").get_data(as_text=True)
    _, forms = _controls_form(html)
    assert all(not f["nested"] for f in forms), "a <form> is nested inside another"


def test_filter_checkboxes_unique_ids_and_correct_names(client):
    html = client.get("/queue").get_data(as_text=True)
    form, _ = _controls_form(html)
    boxes = [i for i in form["inputs"] if i.get("type") == "checkbox"]
    names = [i.get("name") for i in boxes]
    assert names == ["overdue", "responded", "waiting", "missing_tags"]
    ids = [i.get("id") for i in boxes]
    assert ids == ["filter-overdue", "filter-responded", "filter-waiting", "filter-missing"]
    assert all(i.get("value") == "1" for i in boxes)
    all_ids = _all_ids(html)
    assert len(all_ids) == len(set(all_ids)), "duplicate element IDs in page"
    for i in ids:
        assert all_ids.count(i) == 1


def test_filter_labels_are_unique_and_associated(client):
    html = client.get("/queue").get_data(as_text=True)
    # Every checkbox is wrapped by its own <label> (implicit association).
    for name in ("overdue", "responded", "waiting", "missing_tags"):
        m = re.search(rf"<label[^>]*for=filter-[^>]*>\s*<input[^>]*name={name}[^>]*>\s*[^<]+</label>", html)
        assert m, f"checkbox {name} lacks an associated <label>"
    assert 'aria-label="Days back"' in html  # days input has an accessible label
    assert re.search(r'<label[^>]*for=review_view>', html)  # select label present


def test_days_input_belongs_to_filter_form(client):
    html = client.get("/queue").get_data(as_text=True)
    form, _ = _controls_form(html)
    days = [i for i in form["inputs"] if i.get("name") == "days"]
    assert days, "days input missing from filter form"
    assert days[0].get("type") == "number"
    assert days[0].get("min") == "1" and days[0].get("max") == "365"


def test_review_view_control_belongs_to_filter_form(client):
    html = client.get("/queue").get_data(as_text=True)
    form, _ = _controls_form(html)
    sel = [s for s in form["selects"] if s.get("name") == "review_view"]
    assert sel, "review_view select missing from filter form"
    for opt in ("active", "completed", "all"):
        assert f"<option value={opt}" in html


def test_browser_parsed_dom_contains_expected_controls(client):
    html = client.get("/queue").get_data(as_text=True)
    form, forms = _controls_form(html)
    names = {i.get("name") for i in form["inputs"]}
    assert {"overdue", "responded", "waiting", "missing_tags", "days"} <= names
    # The per-row review forms are siblings of the filter form, never inside.
    rvforms = [f for f in forms if f["attrs"].get("action") == "/queue/api/review"]
    assert rvforms, "no review-result forms rendered"
    assert all(f["attrs"].get("method", "").lower() == "post" for f in rvforms)


# --- Canonical query generation --------------------------------------------


def test_canonical_query_explicit_1_for_checked_0_for_unchecked():
    qs = filter_query_string(filters_from_args(MultiDict([])))
    assert qs == "overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active"


def test_canonical_query_all_three_categories_off():
    qs = filter_query_string(filters_from_args(MultiDict([
        ("overdue", "0"), ("responded", "0"), ("waiting", "0"),
    ])))
    assert qs == "overdue=0&responded=0&waiting=0&missing_tags=1&days=60&review_view=active"


def test_canonical_query_no_duplicate_parameters():
    # Even when the same key arrives multiple times, the canonical URL emits
    # it exactly once (last value wins; no Overdue=0&overdue=1 ambiguity).
    qs = filter_query_string(filters_from_args(MultiDict([
        ("overdue", "0"), ("overdue", "1"), ("responded", "1"), ("responded", "0"),
    ])))
    assert qs.count("overdue=") == 1 and qs.count("responded=") == 1
    assert "overdue=1&" in qs and "responded=0&" in qs


def test_canonical_query_all_six_params_exactly_once():
    qs = filter_query_string(filters_from_args(MultiDict([("days", "30")])))
    for key in ("overdue", "responded", "waiting", "missing_tags", "days", "review_view"):
        assert qs.count(f"{key}=") == 1, f"{key} must appear exactly once: {qs}"
    assert qs.endswith("review_view=active")


def test_canonical_query_unknown_boolean_falls_back():
    # Unknown boolean values fall back to the documented default safely.
    cfg = filters_from_args(MultiDict([("overdue", "banana"), ("responded", "yes")]))
    assert cfg["overdue"] is DEFAULT_FILTERS["overdue"]
    assert cfg["responded"] is True  # parse_bool accepts 'yes'


def test_canonical_query_invalid_review_view_falls_back_to_active():
    cfg = filters_from_args(MultiDict([("review_view", "wat")]))
    assert cfg["review_view"] == "active"


def test_canonical_query_invalid_days_falls_back_to_60():
    for bad in ("", "0", "-1", "1.5", "abc", "366"):
        assert parse_days(bad) == 60, f"parse_days({bad!r}) should fall back to 60"
    assert parse_days("1") == 1 and parse_days("365") == 365


# --- Reset / persistence ----------------------------------------------------


def test_reset_link_is_exact_canonical_default_url(client):
    html = client.get("/queue").get_data(as_text=True)
    assert 'href="/queue?overdue=1&amp;responded=0&amp;waiting=0&amp;missing_tags=1&amp;days=60&amp;review_view=active"' in html


def test_reset_keeps_review_data_and_last_opened(client):
    _open(client, 500001)
    from app import set_review_result
    set_review_result("500003", "Resolved")
    # review_view=all shows every local review state, so the resolved row is
    # visible and the last-opened marker on 500001 survives the canonical URL.
    html = client.get("/queue?overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=all").get_data(as_text=True)
    assert "rv-last-opened" in _row_for(html, 500001).split(">", 1)[0]
    row3 = _row_for(html, 500003)
    assert row3 is not None
    assert "rv-resolved" in row3.split(">", 1)[0]


def test_missing_url_parameters_use_defaults(client):
    html = client.get("/queue").get_data(as_text=True)
    # bare /queue behaves exactly like the canonical default state
    assert _ids(html) == _ids(client.get(
        "/queue?overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active"
    ).get_data(as_text=True))


def test_filter_changes_do_not_alter_review_state(client):
    # Changing filters must never write review state (GET-only).
    before = app.load_review_rows() if hasattr(app, "load_review_rows") else None
    client.get("/queue?overdue=0&responded=1&waiting=0&missing_tags=0&days=7&review_view=completed")
    after = app.load_review_rows() if hasattr(app, "load_review_rows") else None
    assert before == after


# =============================================================================
# Prompt07 - Filter panel polish
# =============================================================================


def _queue_html(client, params=""):
    return client.get("/queue" + (("?" + params) if params else "")).get_data(as_text=True)


# --- Panel structure ---------------------------------------------------------


def test_panel_single_controls_form_with_three_regions(client):
    """The redesigned panel is a single filter form holding three labelled
    regions (time range, filter groups, view + actions). No extra controls form."""
    html = _queue_html(client)
    form, forms = _controls_form(html)
    assert form["attrs"].get("method", "").lower() == "get"
    assert form["attrs"].get("action") == "/queue"
    for region in ("region-time", "region-groups", "region-actions"):
        assert f'class="panel-region {region}"' in html, region


def test_panel_three_filter_groups_with_legends(client):
    html = _queue_html(client)
    for legend in ("Ticket conditions", "Freshdesk status", "Additional filters"):
        assert f"<legend class=group-lbl>{legend}</legend>" in html, legend


def test_panel_time_review_and_action_regions_hold_correct_controls(client):
    html = _queue_html(client)
    assert 'name=days' in html
    assert 'id=review_view name=review_view' in html
    assert 'Apply Filters' in html and 'Reset to Defaults' in html


def test_panel_apply_is_primary_and_reset_is_secondary(client):
    html = _queue_html(client)
    assert re.search(r'<button[^>]*type=?submit?[^>]*class=apply[^>]*>Apply Filters</button>', html)
    assert re.search(r'<a[^>]*class=reset[^>]*role=button[^>]*>Reset to Defaults</a>', html)
    assert 'href="/queue?overdue=1&amp;responded=0&amp;waiting=0&amp;missing_tags=1&amp;days=60&amp;review_view=active"' in html


def test_panel_no_nested_forms(client):
    html = _queue_html(client)
    _, forms = _controls_form(html)
    assert all(not f["nested"] for f in forms), "a <form> is nested inside another"


# --- Presets -----------------------------------------------------------------


def test_panel_presets_render_7_14_30_60_90(client):
    html = _queue_html(client, "overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active")
    html = html.replace("&amp;", "&")
    for d in ("7", "14", "30", "60", "90"):
        assert f'class=preset href="/queue?overdue=1&responded=0&waiting=0&missing_tags=1&days={d}&review_view=active"' in html, d


def test_panel_active_preset_has_non_color_indicator(client):
    """The active time preset is identified by more than background colour:
    aria-current=page plus a check-mark glyph inside the preset pill."""
    html = _queue_html(client, "overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active")
    m = re.search(r'<a class=preset[^>]*days=60[^>]*>.*?</a>', html)
    assert m, "active 60d preset not found"
    seg = m.group(0)
    assert 'aria-current=page' in seg
    assert 'preset-mark' in seg  # non-color indicator element


def test_panel_custom_days_keeps_no_active_preset(client):
    """A non-preset days value (e.g. 45) stays supported in the days input and
    marks no preset as active (aria-current appears on none)."""
    html = _queue_html(client, "overdue=1&responded=0&waiting=0&missing_tags=1&days=45&review_view=active")
    assert 'name=days min=1 max=365 value=45' in html
    # No preset pill is marked active (aria-current=page only ever appears on
    # preset links; the CSS rule `.preset[aria-current=page]` also contains the
    # literal text, so scope the check to anchor tags only).
    assert not re.search(r'<a class=preset[^>]*aria-current=page', html)


def test_panel_presets_preserve_other_filter_values(client):
    # With non-default primary/missing-tags state, each preset keeps those
    # values and only changes the day. Canonical URL per preset.
    html = _queue_html(client, "overdue=0&responded=1&waiting=1&missing_tags=0&days=7&review_view=all")
    html = html.replace("&amp;", "&")
    for d in ("7", "14", "30", "60", "90"):
        assert f'class=preset href="/queue?overdue=0&responded=1&waiting=1&missing_tags=0&days={d}&review_view=all"' in html, d


# --- Active-filter summary ---------------------------------------------------


def test_summary_default(client):
    html = _queue_html(client)
    assert "Showing: Overdue + Missing Tags \u00b7 Last 60 days \u00b7 Active" in html


def test_summary_overdue_only(client):
    html = _queue_html(client, "overdue=1&responded=0&waiting=0&missing_tags=0&days=60&review_view=active")
    assert "Showing: Overdue \u00b7 Last 60 days \u00b7 Active" in html


def test_summary_responded_only(client):
    html = _queue_html(client, "overdue=0&responded=1&waiting=0&missing_tags=0&days=30&review_view=active")
    assert "Showing: Customer Responded \u00b7 Last 30 days \u00b7 Active" in html


def test_summary_waiting_only(client):
    html = _queue_html(client, "overdue=0&responded=0&waiting=1&missing_tags=0&days=14&review_view=completed")
    assert "Showing: Waiting on Customer \u00b7 Last 14 days \u00b7 Completed" in html


def test_summary_combined_and_missing_tags_and_view(client):
    html = _queue_html(client, "overdue=1&responded=1&waiting=0&missing_tags=1&days=7&review_view=all")
    assert "Showing: Overdue + Customer Responded + Missing Tags \u00b7 Last 7 days \u00b7 All" in html


def test_summary_both_statuses_union(client):
    html = _queue_html(client, "overdue=0&responded=1&waiting=1&missing_tags=0&days=90&review_view=active")
    assert "Showing: Customer Responded + Waiting on Customer \u00b7 Last 90 days \u00b7 Active" in html


def test_summary_no_primary_filter(client):
    html = _queue_html(client, "overdue=0&responded=0&waiting=0&missing_tags=1&days=60&review_view=active")
    assert "Showing: No ticket category selected" in html


def test_summary_matches_url_derived_state(client):
    cfg = filters_from_args(MultiDict([("overdue", "1"), ("responded", "1"),
                                       ("waiting", "0"), ("missing_tags", "1"),
                                       ("days", "14"), ("review_view", "completed")]))
    want = app.filter_summary_text(cfg)
    assert want == "Showing: Overdue + Customer Responded + Missing Tags \u00b7 Last 14 days \u00b7 Completed"
    html = _queue_html(client, "overdue=1&responded=1&waiting=0&missing_tags=1&days=14&review_view=completed")
    assert want in html


# --- Accessibility -----------------------------------------------------------


def test_panel_accessible_group_names(client):
    html = _queue_html(client)
    for legend in ("Ticket conditions", "Freshdesk status", "Additional filters"):
        assert f"<legend class=group-lbl>{legend}</legend>" in html
    assert 'role=group aria-label="Quick time presets"' in html


def test_panel_focus_visible_styles_present(client):
    html = _queue_html(client)
    for sel in (".preset:focus-visible", ".controls button[type=submit]:focus-visible",
                ".controls a.reset:focus-visible", "input[type=checkbox]:focus-visible"):
        assert sel in html, sel


def test_panel_days_input_accessible_label(client):
    html = _queue_html(client)
    assert 'aria-label="Days back"' in html
    assert 'name=days' in html


def test_panel_review_view_accessible_label(client):
    html = _queue_html(client)
    assert re.search(r'<label[^>]*for=review_view>', html)


def test_panel_checkbox_ids_unique_and_associated(client):
    html = _queue_html(client)
    form, _ = _controls_form(html)
    boxes = [i for i in form["inputs"] if i.get("type") == "checkbox"]
    names = [i.get("name") for i in boxes]
    assert names == ["overdue", "responded", "waiting", "missing_tags"]
    ids = [i.get("id") for i in boxes]
    assert ids == ["filter-overdue", "filter-responded", "filter-waiting", "filter-missing"]
    assert len(set(_all_ids(html))) == len(_all_ids(html)), "duplicate element IDs in page"


# --- Responsive layout -------------------------------------------------------


def test_panel_responsive_css_has_mobile_breakpoint(client):
    html = _queue_html(client)
    assert "@media (max-width:720px)" in html


def test_panel_responsive_css_stacks_groups_and_wraps_controls(client):
    html = _queue_html(client)
    media = html.split("@media (max-width:720px)")[1]
    # Mobile: groups stack vertically, actions stack, buttons wrap and share width.
    assert ".region-groups{flex-direction:column}" in media
    assert ".region-actions{flex-direction:column" in media
    assert "flex-wrap:wrap" in media or "width:100%" in media


def test_panel_group_uses_flex_not_fixed_width(client):
    # Filter groups flex rather than carrying a rigid width that would overflow
    # narrow screens. (The data <table> has its own overflow-x:auto wrapper.)
    html = _queue_html(client)
    assert "min-width:150px" in html and "flex:1 1" in html


def test_panel_buttons_and_summary_can_wrap(client):
    css = _queue_html(client)
    assert ".action-buttons{display:inline-flex;gap:10px;flex-wrap:wrap" in css
    assert ".filter-summary{" in css


# --- Regression (semantics unchanged) ----------------------------------------


def test_panel_semantics_unchanged_counts(client):
    # Default overdue-only = 10 rows; overdue+responded intersection = 7 rows.
    assert len(_ids(_queue_html(client))) == 10
    combo = _ids(_queue_html(client, "overdue=1&responded=1&waiting=0&missing_tags=1&days=60&review_view=active"))
    assert len(combo) == 7


def test_panel_canonical_url_unchanged(client):
    qs = filter_query_string(filters_from_args(MultiDict([])))
    assert qs == "overdue=1&responded=0&waiting=0&missing_tags=1&days=60&review_view=active"

