"""Phase 5B unified Closed Ticket Housekeeping queue tests (offline only)."""
import html
import json
from urllib.parse import parse_qs, urlparse

import app as scanner_app
from werkzeug.datastructures import MultiDict
from app import filters_from_args, ticket_matches_photo_video


_NORMAL_RETURN = {
    "normal_photo_video_only": "0",
    "normal_hide_reviewed_tags": "0",
    "normal_missing_tags": "1",
    "normal_days": "17",
    "normal_review_view": "completed",
    "normal_workflow_tab": "resolved",
}


def _closed_url(**changes):
    values = {"mode": "closed", **_NORMAL_RETURN, **changes}
    return "/queue?" + "&".join(f"{key}={value}" for key, value in values.items())


def _queue_params(url):
    return parse_qs(urlparse(html.unescape(url)).query)


def _workflow_url(page, tab):
    needle = f'href="/queue?'
    for fragment in page.split(needle)[1:]:
        url = fragment.split('"', 1)[0]
        params = _queue_params("/queue?" + url)
        if params.get("workflow_tab") == [tab]:
            return "/queue?" + url
    raise AssertionError(f"workflow tab URL not found: {tab}")


def _return_to_normal(closed_url):
    params = _queue_params(closed_url)
    result = {name.removeprefix("normal_"): values[-1] for name, values in params.items() if name.startswith("normal_")}
    result["mode"] = "normal"
    return "/queue?" + "&".join(f"{key}={value}" for key, value in result.items())


def _assert_normal_return(url):
    params = _queue_params(url)
    assert all(len(params[name]) == 1 and params[name] == [value] for name, value in _NORMAL_RETURN.items())
    assert not any(name.startswith("normal_normal_") for name in params)


def _ticket(ticket_id, *, status=5, subject="Photo request", tags=None):
    return {
        "id": ticket_id, "status": status, "subject": subject, "tags": tags,
        "priority": 2, "updated_at": "2026-08-20T12:00:00Z",
    }


def _write_master_cache(path, tickets):
    path.write_text(json.dumps({"tickets": tickets, "days": 60, "updated_at": "2026-08-22T00:00:00Z"}))


def test_mode_defaults_normal_and_legacy_params_are_inert():
    assert filters_from_args(MultiDict())["mode"] == "normal"
    config = filters_from_args(MultiDict({"overdue": "1", "responded": "1", "waiting": "1"}))
    assert (config["overdue"], config["responded"], config["waiting"]) == (True, True, True)
    # Values remain serializable for bookmarked URLs; route-level regression
    # coverage below proves they do not restrict the queue.


def test_closed_mode_uses_only_master_cache_and_local_toggles(client, monkeypatch, tmp_path):
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    master = tmp_path / "master.json"
    _write_master_cache(master, [
        _ticket(701, subject="Photo needed", tags=[]),
        _ticket(702, subject="ordinary", tags=[]),
        _ticket(703, subject="ordinary", tags=["video request"]),
        _ticket(704, subject="Photo needed", tags=["assigned"]),
        _ticket(706, subject="ordinary", tags=["PHOTOS"]),
        _ticket(705, status=2, subject="Photo needed", tags=[]),
    ])
    monkeypatch.setattr(scanner_app, "LIVE_QUEUE_CACHE_FILE", str(master))
    monkeypatch.setattr(scanner_app, "CACHE_FILE", str(master))
    monkeypatch.setattr(scanner_app.closed_live, "load_cache", lambda: (_ for _ in ()).throw(AssertionError("closed cache used")))

    default = client.get("/queue?mode=closed").get_data(as_text=True)
    assert 'data-ticket-id="701"' in default
    assert 'data-ticket-id="702"' not in default
    assert 'data-ticket-id="704"' not in default
    assert 'data-ticket-id="706"' not in default
    assert "Missing Tags Only" in default and "Photo/Video Review Scope" in default
    assert 'id=filter-photo-video name=photo_video_only value=1 checked' in default
    assert 'id=filter-missing name=missing_tags value=1 checked' in default

    no_missing = client.get("/queue?mode=closed&missing_tags=0").get_data(as_text=True)
    assert 'data-ticket-id="704"' in no_missing
    assert 'data-ticket-id="706"' in no_missing
    no_scope = client.get("/queue?mode=closed&photo_video_only=0").get_data(as_text=True)
    assert 'data-ticket-id="702"' in no_scope
    all_closed = client.get("/queue?mode=closed&photo_video_only=0&missing_tags=0").get_data(as_text=True)
    for ticket_id in (701, 702, 703, 704, 706):
        assert f'data-ticket-id="{ticket_id}"' in all_closed
    assert 'data-ticket-id="705"' not in all_closed


def test_mode_switch_script_preserves_normal_state_and_enters_closed_defaults(client):
    page = client.get("/queue?workflow_tab=resolved&photo_video_only=0&hide_reviewed_tags=0&missing_tags=1&review_view=completed").get_data(as_text=True)
    assert 'data-rendered-mode="normal"' in page
    assert "normalKeys = ['photo_video_only', 'hide_reviewed_tags', 'missing_tags', 'days', 'review_view', 'workflow_tab']" in page
    assert "q.set('normal_' + name" in page
    assert "q.delete(name);" in page
    assert "normalReturnValue(q, name)" in page
    closed = client.get("/queue?mode=closed&normal_workflow_tab=resolved&normal_photo_video_only=0&normal_hide_reviewed_tags=0&normal_missing_tags=1&normal_review_view=completed").get_data(as_text=True)
    assert 'name="normal_workflow_tab" value="resolved"' in closed
    assert 'name="normal_photo_video_only" value="0"' in closed
    assert 'name="normal_review_view" value="completed"' in closed


def test_closed_navigation_preserves_normal_return_state(client):
    closed = _closed_url()
    first = client.get(closed).get_data(as_text=True)

    followup = _workflow_url(first, "followup")
    _assert_normal_return(followup)
    second = client.get(followup).get_data(as_text=True)

    # Closed filter submissions carry the existing private return workspace.
    changed_filter = _closed_url(photo_video_only="0", missing_tags="0", workflow_tab="followup")
    _assert_normal_return(changed_filter)
    third = client.get(changed_filter).get_data(as_text=True)

    no_action = _workflow_url(third, "no_action")
    _assert_normal_return(no_action)
    returned = _return_to_normal(no_action)
    params = _queue_params(returned)
    assert params == {
        "photo_video_only": ["0"], "hide_reviewed_tags": ["0"],
        "missing_tags": ["1"], "days": ["17"],
        "review_view": ["completed"], "workflow_tab": ["resolved"],
        "mode": ["normal"],
    }
    normal = client.get(returned).get_data(as_text=True)
    assert 'data-rendered-mode="normal"' in normal
    assert 'id=filter-photo-video name=photo_video_only value=1 checked' not in normal
    assert 'id=filter-hide-reviewed name=hide_reviewed_tags value=1 checked' not in normal
    assert 'id=filter-missing name=missing_tags value=1 checked' in normal


def test_closed_presets_and_review_redirect_preserve_normal_return_state(client):
    page = client.get(_closed_url(photo_video_only="0", missing_tags="0")).get_data(as_text=True)
    preset_url = html.unescape(page.split('href="/queue?', 1)[1].split('"', 1)[0])
    preset_url = "/queue?" + preset_url
    _assert_normal_return(preset_url)

    closed = client.get(_closed_url(photo_video_only="0", missing_tags="0"))
    page = closed.get_data(as_text=True)
    token = page.split('name=csrf_token value="', 1)[1].split('"', 1)[0]
    response = client.post("/queue/api/review", data={
        "csrf_token": token, "ticket_id": "500005", "review_result": "Resolved",
        "mode": "closed", "photo_video_only": "0", "missing_tags": "0",
        **_NORMAL_RETURN,
    })
    assert response.status_code == 303
    redirect_url = response.headers["Location"]
    _assert_normal_return(redirect_url)
    assert _queue_params(redirect_url)["mode"] == ["closed"]
    returned = _return_to_normal(redirect_url)
    assert "normal_" not in returned
    assert _queue_params(returned)["workflow_tab"] == ["resolved"]


def test_closed_normal_return_values_canonicalize_without_recursion():
    args = MultiDict([
        ("mode", "closed"),
        ("normal_photo_video_only", "wat"), ("normal_photo_video_only", "0"),
        ("normal_days", "9999"), ("normal_review_view", "bogus"),
        ("normal_workflow_tab", "bogus"), ("normal_normal_days", "1"),
    ])
    config = filters_from_args(args)
    query = _queue_params("/queue?" + scanner_app.filter_query_string(config))
    assert query["normal_photo_video_only"] == ["0"]
    assert query["normal_days"] == ["60"]
    assert query["normal_review_view"] == ["all"]
    assert query["normal_workflow_tab"] == ["main"]
    assert "normal_normal_days" not in query
    assert all(len(values) == 1 for name, values in query.items() if name.startswith("normal_"))
    assert not any(name.startswith("normal_") for name in _queue_params(
        "/queue?" + scanner_app.filter_query_string({"mode": "normal", "normal_return": config["normal_return"]})
    ))


def test_each_legacy_param_is_inert_in_queue_route(client):
    baseline = client.get("/queue?photo_video_only=0&hide_reviewed_tags=0").get_data(as_text=True)
    baseline_ids = {line.split('data-ticket-id="', 1)[1].split('"', 1)[0] for line in baseline.splitlines() if 'data-ticket-id="' in line}
    for query in ("overdue=1", "responded=1", "waiting=1", "overdue=1&responded=1&waiting=1"):
        html = client.get(f"/queue?photo_video_only=0&hide_reviewed_tags=0&{query}").get_data(as_text=True)
        ids = {line.split('data-ticket-id="', 1)[1].split('"', 1)[0] for line in html.splitlines() if 'data-ticket-id="' in line}
        assert ids == baseline_ids


def test_shared_matcher_subject_tags_and_malformed_tags():
    assert ticket_matches_photo_video(_ticket(1, subject="Pictures requested", tags=[]))
    assert ticket_matches_photo_video(_ticket(2, subject="ordinary", tags=["VIDS"]))
    assert not ticket_matches_photo_video(_ticket(3, subject="ordinary", tags="video"))


def test_removed_selectors_are_not_visible_and_badges_remain(client):
    html = client.get("/queue").get_data(as_text=True)
    assert 'id=filter-overdue' not in html
    assert 'id=filter-responded' not in html
    assert 'id=filter-waiting' not in html
    assert "OVERDUE" in html
    assert "CUSTOMER RESPONDED" in html
    assert "WAITING ON CUSTOMER" in html


def test_closed_mode_review_posts_to_unified_review_state(client):
    page = client.get("/queue?mode=closed&photo_video_only=0&missing_tags=0")
    html = page.get_data(as_text=True)
    token = html.split('name=csrf_token value="', 1)[1].split('"', 1)[0]
    response = client.post("/queue/api/review", data={
        "csrf_token": token, "ticket_id": "500005", "review_result": "Resolved",
        "mode": "closed", "photo_video_only": "0", "missing_tags": "0",
    })
    assert response.status_code == 303
    assert scanner_app.load_review_rows()[500005]["review_result"] == "Resolved"
    assert scanner_app.load_closed_review_rows().get(500005) is None
