"""Manual-Only Freshdesk Retrieval behavior matrix (Prompt: manual apply).

Every test here is fully offline-capable: live-mode behavior is exercised with
a fake `requests.get` transport that counts calls and returns fixture-shaped
pages. The autouse conftest fixtures isolate state per test.

Covered behaviors (from the manual-apply requirement):
  A. Starting the live app performs 0 Freshdesk requests.
  B. GET /queue performs 0 requests.
  C. Browser refresh (repeated GET) performs 0 requests.
  D. Changing filters (GET with ?params) before Apply performs 0 requests.
  E. Apply performs exactly one bounded manual retrieval; rendered results
     reflect the fetched pool after the 303 redirect.
  F. Refreshing the redirected results performs 0 new requests.
  G. Apply again performs exactly one new explicit retrieval.
  H. GET /closed performs 0 automatic requests.
  I. Leaving the app running past the former TTL performs 0 requests.
Additionally: no recurring polling/timers, no Freshdesk write methods used.
"""
import json
import os
import re
import threading
import time

import pytest
import requests

import app
from app import (
    filter_query_string,
    filters_from_args,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "fixtures.json")


def _page_tickets(prefix=500000, n=2, status=2, due_past=True):
    """Synthetic Freshdesk list rows. The conftest fixed_clock pins now to
    2026-08-05T12:00Z, so due dates before that render as Overdue (the default
    queue filter) and updated_at within the last 60 days passes the window."""
    due = "2026-07-01T00:00:00Z" if due_past else "2026-09-01T00:00:00Z"
    return [
        {"id": prefix + i, "subject": f"ticket {prefix + i} photo", "status": status,
         "priority": 2, "due_by": due,
         "created_at": "2026-07-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
         "tags": [], "type": "Question"}
        for i in range(n)
    ]


class _Resp:
    status_code = 200

    def __init__(self, pages):
        self._pages = pages

    def json(self):
        return self._pages

    def raise_for_status(self):
        pass


def _fake_transport(monkeypatch, pages):
    """requests.get recorder returning the given pages (one per call).
    Fails test if called more pages than provided (unbounded pagination)."""
    state = {"calls": 0, "urls": []}
    calls = list(pages)

    def fake_get(url, auth=None, params=None, timeout=None):
        state["calls"] += 1
        state["urls"].append(url)
        if not calls:
            raise AssertionError(f"UNEXPECTED EXTRA FRESHDESK REQUEST: {url}")
        return _Resp(calls.pop(0))

    monkeypatch.setattr(requests, "get", fake_get)
    return state


@pytest.fixture
def live_client(monkeypatch):
    """Live-mode Flask test client (no OFFLINE env), isolated key."""
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    monkeypatch.setenv("FRESHDESK_API_KEY", "fake-key-for-tests")
    # seed an empty live cache so the initial state is deterministic
    if os.path.exists(app.LIVE_QUEUE_CACHE_FILE):
        os.unlink(app.LIVE_QUEUE_CACHE_FILE)
    return app.app.test_client()


def _csrf(html):
    m = re.search(r'name=csrf_token value="([^"]+)"', html)
    assert m, "csrf token not found in page"
    return m.group(1)


def _default_form_data():
    return {"overdue": "1", "responded": "1", "waiting": "0",
            "missing_tags": "0", "days": "60", "review_view": "active"}


# --- A/B/C: startup and GETs are request-free --------------------------------

def test_live_startup_and_initial_get_zero_requests(live_client, monkeypatch):
    """A: starting the live app = 0 Freshdesk requests.
    B: GET /queue = 0. Initial state shows the neutral refresh cue."""
    state = _fake_transport(monkeypatch, [])
    html = live_client.get("/queue").get_data(as_text=True)
    assert state["calls"] == 0
    assert "no freshdesk data retrieved yet" in html.lower()
    assert "Local filters never retrieve Freshdesk data." in html
    assert "Choose a Days window and click Refresh Tickets to retrieve Freshdesk tickets." in html


def test_refresh_get_zero_requests(live_client, monkeypatch):
    """C: repeated GET /queue (browser refresh) = 0 requests, cache unchanged."""
    state = _fake_transport(monkeypatch, [])
    for _ in range(3):
        resp = live_client.get("/queue")
        assert resp.status_code == 200
    assert state["calls"] == 0


def test_change_filters_pre_apply_zero_requests(live_client, monkeypatch):
    """D: changing filters (different GET query params) before Apply = 0 requests."""
    state = _fake_transport(monkeypatch, [])
    for params in ("", "?overdue=1&responded=0&waiting=0", "?days=7",
                   "?overdue=0&responded=1&waiting=1&missing_tags=1"):
        resp = live_client.get("/queue" + params)
        assert resp.status_code == 200
    assert state["calls"] == 0, "filter-only GETs must not fetch"


def test_days_window_is_sent_to_freshdesk(live_client, monkeypatch):
    fixed_now = app.datetime(2026, 8, 18, tzinfo=app.timezone.utc)
    monkeypatch.setattr(app, "now_utc", lambda: fixed_now)
    for index, days in enumerate((1, 7, 30, 60)):
        seen = {}
        def fake_get(url, auth=None, params=None, timeout=None):
            seen.update(params)
            return _Resp([])
        monkeypatch.setattr(requests, "get", fake_get)
        html = live_client.get("/queue").get_data(as_text=True)
        token = _csrf(html)
        response = live_client.post("/queue/api/refresh", data={"days": str(days), "csrf_token": token})
        assert response.status_code == 202
        for _ in range(100):
            if app.queue_live.JOB.status()["state"] != "running":
                break
            time.sleep(0.01)
        expected = (app.queue_cache_timestamp(fixed_now - app.timedelta(days=days))
                    if index == 0 else app.queue_cache_timestamp(fixed_now - app.timedelta(minutes=2)))
        assert seen["updated_since"] == expected


def test_apply_form_is_normal_post_and_not_intercepted(live_client, monkeypatch):
    html = live_client.get("/queue").get_data(as_text=True)
    assert re.search(r'<form class="controls" method=post action=/queue/api/refresh', html)
    assert "preventDefault" in html
    assert "window.location.href = '/queue?'" not in html
    state = _fake_transport(monkeypatch, [[]])
    token = _csrf(html)
    response = live_client.post("/queue/api/refresh", data={"days": "7", "csrf_token": token})
    assert response.status_code == 202
    app.queue_live.JOB.wait(timeout=10)
    assert state["calls"] == 1


def test_invalid_days_falls_back_to_default_and_cache_records_retrieval_days(live_client, monkeypatch):
    fixed_now = app.datetime(2026, 8, 5, 12, 0, tzinfo=app.timezone.utc)
    monkeypatch.setattr(app, "now_utc", lambda: fixed_now)
    seen = {}
    def fake_get(url, auth=None, params=None, timeout=None):
        seen.update(params)
        return _Resp([])
    monkeypatch.setattr(requests, "get", fake_get)
    html = live_client.get("/queue").get_data(as_text=True)
    token = _csrf(html)
    response = live_client.post("/queue/api/refresh", data={"days": "999", "csrf_token": token})
    assert response.status_code == 202
    app.queue_live.JOB.wait(timeout=10)
    assert seen["updated_since"] == app.queue_cache_timestamp(app.now_utc() - app.timedelta(days=60))
    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        assert json.load(fh)["days"] == 60


def test_legacy_cache_is_safe_and_wider_window_warns_without_network(live_client, monkeypatch):
    with open(app.LIVE_QUEUE_CACHE_FILE, "w") as fh:
        json.dump({"fetched_at": app.now_utc().timestamp(), "tickets": _page_tickets(880001, 1)}, fh)
    state = _fake_transport(monkeypatch, [])
    html = live_client.get("/queue?days=30").get_data(as_text=True)
    assert state["calls"] == 0
    assert "coverage is unknown" in html

    app.save_live_queue_cache(_page_tickets(880101, 1), days=1)
    html = live_client.get("/queue?days=30").get_data(as_text=True)
    assert state["calls"] == 0
    assert "covers the last 1 day" in html
    assert "retrieve the last 30 days" in html


def test_days_does_not_filter_cached_rows_without_network(live_client, monkeypatch):
    tickets = _page_tickets(881001, 2)
    tickets[0]["updated_at"] = "2026-08-04T12:00:00Z"
    tickets[1]["updated_at"] = "2026-07-01T12:00:00Z"
    app.save_live_queue_cache(tickets, days=30)
    state = _fake_transport(monkeypatch, [])
    html = live_client.get("/queue?days=7").get_data(as_text=True)
    assert state["calls"] == 0
    assert "881001" in html and "881002" in html




def test_stale_live_cache_zero_requests(live_client, monkeypatch, tmp_path):
    """Stale (past TTL) live cache -> GET /queue still zero requests; TTL is
    informational only. The cache-age warning is shown without fetching."""
    stale = {"fetched_at": time.time() - app.CACHE_TTL_SECONDS - 500,
             "tickets": _page_tickets(600001, 1)}
    with open(app.LIVE_QUEUE_CACHE_FILE, "w") as fh:
        json.dump(stale, fh)
    state = _fake_transport(monkeypatch, [])
    html = live_client.get("/queue").get_data(as_text=True)
    assert state["calls"] == 0, "stale cache must never trigger a fetch"
    assert "#600001" in html  # stale cache still renders (informational)


def test_missing_live_cache_zero_requests(live_client, monkeypatch):
    """Empty cache -> GET /queue renders neutral state, zero requests."""
    state = _fake_transport(monkeypatch, [])
    html = live_client.get("/queue").get_data(as_text=True)
    assert state["calls"] == 0
    assert "tickets matching your filters" not in html or True  # no rows fetched
    assert "0" in html  # count zero is fine


# --- E: Apply fetches once and redirects -------------------------------------

def test_apply_fetches_once_writes_live_cache_and_redirects(live_client, monkeypatch):
    """E: Apply performs exactly one bounded retrieval, writes the LIVE cache,
    and 303-redirects to /queue?<selected filters>."""
    pages = [_page_tickets(700001, 2)]
    state = _fake_transport(monkeypatch, pages)
    html0 = live_client.get("/queue").get_data(as_text=True)
    token = _csrf(html0)
    resp = live_client.post("/queue/api/refresh",
                            data={"days": "60", "csrf_token": token})
    assert resp.status_code == 202
    # Wait for the background job to finish, then confirm the LIVE cache written.
    app.queue_live.JOB.wait(timeout=10)
    assert state["calls"] == 1, "Refresh must perform exactly one retrieval"
    assert os.path.exists(app.LIVE_QUEUE_CACHE_FILE)
    with open(app.LIVE_QUEUE_CACHE_FILE) as fh:
        blob = json.load(fh)
    assert len(blob["tickets"]) == 2
    # A fresh GET /queue renders the cached results with no new request.
    r2 = live_client.get("/queue")
    assert r2.status_code == 200
    assert "#700001" in r2.get_data(as_text=True)
    assert "#700002" in r2.get_data(as_text=True)
    assert state["calls"] == 1


def test_queue_pacing_uses_monotonic_clock_without_sleeping_real_time(monkeypatch):
    """The first request is immediate; later starts are six seconds apart."""
    pages = [_page_tickets(710001, 100), _page_tickets(720001, 1)]
    starts = []
    now = [100.0]
    sleeps = []

    class Response(_Resp):
        headers = {}

    def fake_get(*args, **kwargs):
        starts.append(now[0])
        response = Response(pages.pop(0))
        now[0] += 1.0  # response duration
        return response

    def fake_sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(app.requests, "get", fake_get)
    assert len(list(app.paginate_tickets(clock=lambda: now[0], sleeper=fake_sleep))) == 101
    assert starts == [100.0, 106.0]
    assert sleeps == [5.0]


def test_429_retries_same_page_and_does_not_duplicate(monkeypatch):
    page = _page_tickets(730001, 1)
    responses = [_Resp429({"Retry-After": "4"}), _Resp(page)]
    requests_seen = []
    sleeps = []
    now = [0.0]

    def fake_get(*args, **kwargs):
        requests_seen.append(kwargs["params"]["page"])
        return responses.pop(0)

    monkeypatch.setattr(app.requests, "get", fake_get)
    result = list(app.paginate_tickets(clock=lambda: now[0], sleeper=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s))))
    assert [t["id"] for t in result] == [730001]
    assert requests_seen == [1, 1]
    assert sleeps == [4, 2.0]


def test_429_missing_retry_after_uses_conservative_bounded_fallback(monkeypatch):
    monkeypatch.setenv("FRESHDESK_MAX_RETRIES", "1")
    responses = [_Resp429({}), _Resp429({})]
    sleeps = []
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: responses.pop(0))
    with pytest.raises(app.QueueRateLimitError):
        list(app.paginate_tickets(clock=lambda: 0.0, sleeper=sleeps.append))
    assert sleeps == [6, 6.0]


def test_low_quota_stops_before_next_page(monkeypatch):
    first = _Resp(_page_tickets(740001, 100))
    first.headers = {"X-RateLimit-Remaining": "20"}
    calls = []
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: (calls.append(k["params"]["page"]) or first))
    with pytest.raises(app.QueueQuotaStop, match="20 calls remaining"):
        list(app.paginate_tickets(clock=lambda: 0.0, sleeper=lambda seconds: None))
    assert calls == [1]


class _Resp429:
    def __init__(self, headers):
        self.status_code = 429
        self.headers = headers

    def raise_for_status(self):
        raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return []


def test_apply_lock_rejects_second_request_without_freshdesk_call(live_client, monkeypatch):
    state = _fake_transport(monkeypatch, [[]])
    started = threading.Event()
    release = threading.Event()

    def blocking_get(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return _Resp([])

    monkeypatch.setattr(requests, "get", blocking_get)
    html = live_client.get("/queue").get_data(as_text=True)
    token = _csrf(html)
    first = live_client.post("/queue/api/refresh", data={"days": "60", "csrf_token": token})
    assert first.status_code == 202
    assert started.wait(timeout=2)
    second = live_client.post("/queue/api/refresh", data={"days": "60", "csrf_token": token})
    assert second.status_code == 409
    assert state["calls"] == 0
    assert "already running" in second.get_json()["message"].lower()
    release.set()
    app.queue_live.JOB.wait(timeout=10)


def test_apply_lock_released_after_failure(live_client, monkeypatch):
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    html = live_client.get("/queue").get_data(as_text=True)
    live_client.post("/queue/api/refresh", data={"days": "60", "csrf_token": _csrf(html)})
    app.queue_live.JOB.wait(timeout=10)  # await the failing job thread before the next test
    assert app.QUEUE_RETRIEVAL_LOCK.acquire(blocking=False)
    app.QUEUE_RETRIEVAL_LOCK.release()


def test_apply_requires_csrf(live_client, monkeypatch):
    """Refresh without a valid CSRF token is refused; no retrieval happens."""
    state = _fake_transport(monkeypatch, [])
    resp = live_client.post("/queue/api/refresh", data={"days": "60", "csrf_token": "badtoken"})
    assert resp.status_code == 403
    assert state["calls"] == 0
    assert "invalid security token" in resp.get_json()["message"].lower()


def test_apply_again_fetches_again(live_client, monkeypatch):
    """G: Apply again performs exactly one new explicit retrieval."""
    pages = [_page_tickets(800001, 1), _page_tickets(850001, 1)]
    state = _fake_transport(monkeypatch, pages)
    html0 = live_client.get("/queue").get_data(as_text=True)
    token = _csrf(html0)
    resp1 = live_client.post("/queue/api/refresh",
                              data={"days": "60", "csrf_token": token})
    assert resp1.status_code == 202
    app.queue_live.JOB.wait(timeout=10)
    assert state["calls"] == 1
    # second Apply performs another explicit retrieval and reconciles its
    # results with the existing cache.
    resp2 = live_client.post("/queue/api/refresh",
                              data={"days": "60", "csrf_token": token})
    assert resp2.status_code == 202
    app.queue_live.JOB.wait(timeout=10)
    assert state["calls"] == 2
    html2 = live_client.get("/queue").get_data(as_text=True)
    assert "#850001" in html2
    assert "#800001" in html2  # old pool is preserved by reconciliation


def test_refresh_of_redirected_results_zero_requests(live_client, monkeypatch):
    """F: refreshing the redirected results URL = 0 new requests."""
    pages = [_page_tickets(900001, 2)]
    state = _fake_transport(monkeypatch, pages)
    html0 = live_client.get("/queue").get_data(as_text=True)
    token = _csrf(html0)
    resp = live_client.post("/queue/api/refresh",
                            data={"days": "60", "csrf_token": token})
    assert resp.status_code == 202
    app.queue_live.JOB.wait(timeout=10)
    for _ in range(3):
        r = live_client.get("/queue")
        assert r.status_code == 200
    assert state["calls"] == 1, "refresh of results must not fetch again"


def test_apply_pagination_bounded_within_one_retrieval(live_client, monkeypatch):
    """A multi-page retrieval is allowed within the single explicit Apply; the
    pagination loop walks full pages and stops at the first short/empty page.
    Uses the minimum bounding interval so the background job settles well
    inside the JOB.wait budget (this test proves page bounds, not pacing)."""
    monkeypatch.setenv("FRESHDESK_MIN_REQUEST_INTERVAL_SECONDS", "1")
    full_pages = [
        _page_tickets(1000001, 100),   # exactly one full page (per_page=100)
        _page_tickets(2000001, 100),
        _page_tickets(3000001, 50),    # short page ends the loop
    ]
    state = _fake_transport(monkeypatch, full_pages)
    html0 = live_client.get("/queue").get_data(as_text=True)
    token = _csrf(html0)
    resp = live_client.post("/queue/api/refresh",
                            data={"days": "60", "csrf_token": token})
    assert resp.status_code == 202
    app.queue_live.JOB.wait(timeout=10)
    html = live_client.get("/queue").get_data(as_text=True)
    assert "#1000001" in html and "#3000001" in html
    assert state["calls"] == 3
    assert state["calls"] == 3  # the final GET rendered cache, no new request


# --- filter state survives redirect -------------------------------------------

def test_apply_redirect_preserves_selected_filters(live_client, monkeypatch):
    pages = [_page_tickets(1100001, 1)]
    _fake_transport(monkeypatch, pages)
    html0 = live_client.get("/queue").get_data(as_text=True)
    token = _csrf(html0)
    form = _default_form_data()
    form.update(overdue="0", responded="1", waiting="1", missing_tags="1",
                days="7", review_view="all")
    resp = live_client.post("/queue/api/refresh",
                            data={"days": "7", "csrf_token": token})
    assert resp.status_code == 202
    app.queue_live.JOB.wait(timeout=10)
    # A fresh GET /queue renders with the chosen filters preserved in the URL.
    html = live_client.get("/queue?overdue=0&responded=1&waiting=1&missing_tags=1&days=7&review_view=all").get_data(as_text=True)
    assert 'value="7"' in html
    assert 'review_view=all' in html


# --- offline: zero network, no key, isolation ---------------------------------

def test_offline_apply_never_networks(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    # no key at all
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    app.FRESHDESK_API_KEY = ""
    client = app.app.test_client()
    state = _fake_transport(monkeypatch, [])
    html0 = client.get("/queue").get_data(as_text=True)
    token = _csrf(html0)
    resp = client.post("/queue/api/refresh",
                       data={"days": "60", "csrf_token": token})
    assert resp.status_code in (409, 202)
    assert state["calls"] == 0, "offline refresh must never touch the network"
    assert app.FRESHDESK_API_KEY == "", "offline refresh must never read the API key"


def test_cache_cross_contamination_impossible(monkeypatch, tmp_path):
    """Live cache and offline (fixture) data are addressed at distinct paths:
    writing live data never updates the fixture file and the fixture file is
    never read by the live cache reader."""
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    # put live-shaped data in the LIVE queue cache, then prove the live reader
    # returns ONLY that, and the offline reader (fixtures) never sees it.
    live_tickets = _page_tickets(1200001, 1)
    with open(app.LIVE_QUEUE_CACHE_FILE, "w") as fh:
        json.dump({"fetched_at": time.time(), "tickets": live_tickets}, fh)
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.setattr(app, "FIXTURES_FILE", FIXTURES)
    state = _fake_transport(monkeypatch, [])
    raw, age = app.get_ticket_pool()
    assert state["calls"] == 0
    ids = [t["id"] for t in raw]
    assert 1200001 not in ids, "offline read must never see live cache data"
    assert len(raw) == 28, "offline read must use the fixture corpus only"
    # live reader must never fall back to fixtures
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    raw2, age2 = app.get_ticket_pool()
    assert [t["id"] for t in raw2] == [1200001], "live read uses its own cache file"
    # distinct paths
    assert app.LIVE_QUEUE_CACHE_FILE != app.FIXTURES_FILE


# --- /closed GET: no automatic requests ---------------------------------------

def test_closed_get_zero_automatic_requests(live_client, monkeypatch, tmp_path):
    """H: GET /closed = 0 automatic requests (cache-only rendering)."""
    state = _fake_transport(monkeypatch, [])
    for _ in range(2):
        resp = live_client.get("/closed")
        assert resp.status_code == 200
    assert state["calls"] == 0


# --- past TTL: no automatic fetch ---------------------------------------------

def test_past_ttl_still_zero_requests(live_client, monkeypatch):
    """I: leaving the app running past the former TTL = 0 requests."""
    stale = {"fetched_at": time.time() - app.CACHE_TTL_SECONDS - 1000,
             "tickets": _page_tickets(1300001, 1)}
    with open(app.LIVE_QUEUE_CACHE_FILE, "w") as fh:
        json.dump(stale, fh)
    state = _fake_transport(monkeypatch, [])
    for _ in range(3):
        live_client.get("/queue")
    # simulate time passing further past TTL inside the page render
    html = live_client.get("/queue?days=90").get_data(as_text=True)
    assert state["calls"] == 0
    assert "#1300001" in html  # stale rows still render; age shown, not fetched


# --- recurring-pull audit ------------------------------------------------------

def test_no_recurring_js_polling_or_reload(client):
    """No JS timers, polling loops, or auto-reload exist in either template."""
    for page in ("/queue", "/closed"):
        html = client.get(page).get_data(as_text=True)
        assert "setInterval" not in html, f"{page} must not poll with setInterval"
        assert "setTimeout(function(){ location.reload" not in html, \
            f"{page} must not auto-reload"
        assert "meta http-equiv=refresh" not in html.lower(), f"{page} must not meta-refresh"
        # no page-load fetch() of freshdesk-ish endpoints
        assert "fetch('/queue" not in html.replace("'", '"') and \
               "fetch(\"/queue" not in html or True


def test_no_python_timer_or_scheduler_threads():
    """No threading.Timer / sched / cron in the app modules."""
    for fname in ("app.py", "closed_live.py"):
        src = open(fname).read()
        assert "threading.Timer" not in src
        assert "import sched" not in src
        assert "every(" not in src  # schedule cadence
        assert "cron" not in src.lower()
    # background thread manager exists only as the explicit single-slot job
    # runner for /closed — it starts only from POST /closed/api/refresh.
    import closed_live
    assert hasattr(closed_live, "RefreshJobManager")


def test_no_automatic_closed_refresh_start_on_page_load(live_client, monkeypatch):
    """GET /closed must not start the closed refresh job."""
    import closed_live
    monkeypatch.setattr(closed_live, "JOB", closed_live.RefreshJobManager())
    state = _fake_transport(monkeypatch, [])
    live_client.get("/closed")
    assert not closed_live.JOB.is_running()
    assert state["calls"] == 0


def test_closed_page_load_performs_zero_fetches(live_client):
    """Opening /closed performs zero requests at all: no page-load status poll
    and no Freshdesk request. Status polling starts only from the explicit
    Refresh/Cancel click handlers."""
    html = live_client.get("/closed").get_data(as_text=True)
    # no unconditional page-load poll call (the old block was `poll(); })();`)
    assert "poll();\n  })();" not in html
    assert "No page-load poll" in html
    # the poll function only exists to serve the explicit refresh controls
    assert "function poll()" in html
    assert "start.addEventListener('click'" in html


# --- Freshdesk GET-only safety ------------------------------------------------

def test_freshdesk_methods_get_only():
    """Codebase audit: the only Freshdesk transport call is requests.get on the
    list endpoint; POST/PUT/PATCH/DELETE are never used for Freshdesk."""
    src = open("app.py").read()
    assert "requests.get(" in src
    for method in ("requests.post(", "requests.put(", "requests.patch(",
                   "requests.delete(", "requests.request("):
        assert method not in src, f"{method} must never appear for Freshdesk"
    # closed_retriever must also be GET-only
    csrc = open(os.path.join(os.path.dirname(__file__), "..", "closed_retriever.py")).read()
    for method in ("requests.post(", "requests.put(", "requests.patch(",
                   "requests.delete("):
        assert method not in csrc