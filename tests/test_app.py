"""Offline test suite for the Freshdesk Review Queue Scanner.

All tests run entirely offline:
  - the autouse conftest fixture blocks every requests call,
  - offline mode reads fixture pages only (no network, no API key),
  - live-mode code paths are exercised with monkeypatched fake responses.
"""
import json
import os
import time

import pytest
import requests

import app
from app import (
    KEYWORD_RE,
    OfflineDataError,
    offline_paginate_tickets,
    passes_filters,
    paginate_tickets,
    resolve_bind_host,
    is_offline,
    keyword_filter_hits,
    fmt_due,
    ticket_url,
)

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
# Keyword matching / word boundaries
# ---------------------------------------------------------------------------


def test_keyword_matches_positive():
    for subject in [
        "Customer sent photo of damage",
        "photos attached",
        "picture of the shelf",
        "pictures from customer",
        "see pic",
        "pics enclosed",
        "video of the issue",
        "videos in reply",
        "vid from phone",
    ]:
        assert keyword_filter_hits(subject), f"expected match: {subject}"


def test_keyword_case_insensitive():
    assert keyword_filter_hits("PHOTO OF DAMAGE")
    assert keyword_filter_hits("Customer sent Photo of the drawer")


def test_keyword_word_boundaries_no_substring_matches():
    # "vendor" must NOT match "vid", "topic" must NOT match "pic".
    for subject in [
        "Vendor painted the topic area before delivery",
        "vendor delivery delay",
        "the topic of the meeting",
        "photography service",
        "photographer visited",
        "Provide identification for pickup",  # contains "vid" inside "Provide"
        "picnic table order",                  # contains "pic" inside "picnic"
        "vicinity of the store",
        "update on table delivery",
    ]:
        assert not keyword_filter_hits(subject), f"expected NO match: {subject}"


def test_keyword_regex_is_word_boundary():
    # The regex itself must anchor with \\b on both sides.
    assert KEYWORD_RE.search("send a vid now") is not None
    assert KEYWORD_RE.search("provide identification") is None
    assert KEYWORD_RE.search("topic") is None


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


def test_filter_includes_missing_due_by():
    t = _ticket(due_by=None)
    assert passes_filters(t) is True


def test_filter_includes_malformed_due_by():
    t = _ticket(due_by="not-a-date")
    assert passes_filters(t) is True


def test_filter_includes_waiting_on_customer():
    t = _ticket(status=6, subject="Waiting on customer for photos of shelf damage")
    assert passes_filters(t) is True


def test_filter_excludes_tagged_ticket():
    t = _ticket(tags=["warranty"])
    assert passes_filters(t) is False


def test_filter_excludes_closed_ticket():
    t = _ticket(status=5)
    assert passes_filters(t) is False


def test_filter_excludes_no_keyword_subject():
    t = _ticket(subject="Update on table delivery")
    assert passes_filters(t) is False


def test_filter_excludes_vendor_and_topic_subjects():
    t = _ticket(subject="Vendor painted the topic area before delivery")
    assert passes_filters(t) is False


def test_filter_unknown_status_excluded():
    assert passes_filters(_ticket(status=4)) is False


def test_filter_open_status_excluded():
    assert passes_filters(_ticket(status=1)) is False


# ---------------------------------------------------------------------------
# Fixtures (realistic fake data, all fictional)
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
        assert needle not in raw, f"fixture contains suspicious content: {needle}"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_offline_pagination_reads_all_pages(monkeypatch):
    monkeypatch.setattr(app, "FIXTURES_FILE", os.path.join(os.path.dirname(__file__), "..", "fixtures", "fixtures.json"))
    tickets = list(offline_paginate_tickets())
    assert len(tickets) == 12
    ids = [t["id"] for t in tickets]
    assert ids == sorted(ids)


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
    monkeypatch.setattr(app, "FIXTURES_FILE", os.path.join(os.path.dirname(__file__), "..", "fixtures", "fixtures.json"))
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


def test_offline_get_ticket_pool_uses_fixtures(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.setattr(app, "FIXTURES_FILE", os.path.join(os.path.dirname(__file__), "..", "fixtures", "fixtures.json"))
    tickets, _ = app.get_ticket_pool()
    ids = [t["id"] for t in tickets]
    # Only tickets passing the filters survive: overdue + missing-due + malformed-due status 2,
    # plus waiting-on-customer. Tagged/closed/no-keyword/not-yet-due are excluded.
    assert 500001 in ids  # overdue photo
    assert 500007 in ids  # overdue pic
    assert 500009 in ids  # missing due_by
    assert 500010 in ids  # malformed due_by
    assert 500003 in ids  # waiting on customer
    assert 500011 in ids  # waiting on customer
    assert 500002 not in ids  # not yet overdue
    assert 500004 not in ids  # tagged
    assert 500005 not in ids  # closed
    assert 500006 not in ids  # vendor/topic word-boundary
    assert 500008 not in ids  # no keyword
    assert 500012 not in ids  # no keyword


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


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
    assert all(t["id"] >= 500001 for t in tickets)


def test_cache_not_yet_expired_served(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    fresh = {
        "fetched_at": time.time() - 10,
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
    tickets, cache_age = app.get_ticket_pool()
    assert calls["n"] == 0
    assert cache_age >= 10
    assert tickets[0]["id"] == 1


def test_cache_corrupt_file_refetches(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    with open(app.CACHE_FILE, "w") as fh:
        fh.write("{ corrupted !!!")
    calls = {"n": 0}
    orig = app.offline_paginate_tickets

    def spy():
        calls["n"] += 1
        yield from orig()

    monkeypatch.setattr(app, "offline_paginate_tickets", spy)
    tickets, _ = app.get_ticket_pool()
    assert calls["n"] == 1
    assert tickets  # refetched successfully
    with open(app.CACHE_FILE) as fh:
        json.load(fh)  # rewritten as valid json


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


def test_queue_offline_default_hides_waiting(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    resp = app.app.test_client().get("/queue")
    text = resp.get_data(as_text=True)
    assert "#500003" not in text  # waiting hidden by default
    assert "#500011" not in text


def test_queue_offline_no_network_and_no_key(monkeypatch):
    """Full offline /queue render under the global network block: must succeed
    with no API key present and no requests call."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    resp = app.app.test_client().get("/queue")
    assert resp.status_code == 200
    assert "OFFLINE MODE" in resp.get_data(as_text=True)


def test_refresh_link_does_not_bypass_cache(monkeypatch):
    """Existing behavior (preserved, documented): Refresh is a plain /queue link.
    It reloads the page; the 30-min cache still applies."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    resp = app.app.test_client().get("/queue")
    text = resp.get_data(as_text=True)
    assert 'href=/queue' in text
    assert 'refresh' not in text.lower() or True  # no cache-busting param added
    assert "?refresh=" not in text and "cache=0" not in text


# ---------------------------------------------------------------------------
# Routes / app shape
# ---------------------------------------------------------------------------


def test_only_queue_route_registered():
    rules = {r.rule for r in app.app.url_map.iter_rules()}
    assert rules == {"/queue"}, f"unexpected routes: {rules}"


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
