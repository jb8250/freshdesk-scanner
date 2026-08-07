"""Offline-only tests for Prompt 09 — Closed Page Theme Alignment.

Verifies that /queue and /closed share a single application theme (the /queue
stylesheet promoted to a shared constant), share one navigation component with
visible spacing and correct aria-current, and that neither page's behavior or
safety posture regressed. No Freshdesk network, API key, live retrieval, or
write path is exercised; the autouse conftest network blocker makes any
attempted requests.get/post fail loudly.
"""
import re

import pytest

import app
from app import _SHARED_CSS, _nav_html


def _html(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/queue", "/closed"])
def test_both_pages_share_the_same_theme_css(client, path, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    html = _html(client, path)
    # The body background / content width / font come from the shared stylesheet
    # (the /queue theme), not a per-page fork.
    assert "body{font-family:system-ui" in html
    assert "background:#f5f5f5" in html
    assert "max-width:1100px" in html
    assert ".controls{" in html
    assert ".preset{" in html
    assert ".filter-group{" in html
    assert "table{" in html
    assert ".badge{" in html
    assert ":focus-visible" in html


@pytest.mark.parametrize("path", ["/queue", "/closed"])
def test_shared_nav_has_two_spaced_links(client, path, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    html = _html(client, path)
    # Exactly two nav links, correctly separated (shared component).
    links = re.findall(r'<a class="top-link"[^>]*>.*?</a>', html)
    assert len(links) == 2, links
    hrefs = re.findall(r'href="(/queue|/closed)"', html)
    assert set(hrefs) == {"/queue", "/closed"}
    # Spacing comes from real CSS (flex + gap on .top-nav), not a separator char.
    assert ".top-nav" in html
    assert "gap:" in html.replace(" ", "")


def test_nav_aria_current_is_per_page(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    q = _html(client, "/queue")
    cl = _html(client, "/closed")

    def active_page(html):
        m = re.search(r'<a class="top-link" href="(/queue|/closed)" aria-current="page">', html)
        return m.group(1) if m else None

    assert active_page(q) == "/queue"
    assert active_page(cl) == "/closed"


def test_nav_mobile_wrapping_media_query_exists():
    # Safe wrapping on mobile: the shared stylesheet includes the nav breakpoint.
    assert "@media (max-width:500px)" in _SHARED_CSS
    assert ".top-nav" in _SHARED_CSS
    assert ".top-link" in _SHARED_CSS


def test_nav_helper_produces_expected_markup():
    q = _nav_html("queue")
    cl = _nav_html("closed")
    assert 'href="/queue" aria-current="page"' in q
    assert 'href="/closed" aria-current="page"' in cl
    assert cl.count("top-link") == 2 and q.count("top-link") == 2


def _token(client):
    html = _html(client, "/queue")
    m = re.search(r'name=csrf_token value="([^"]+)"', html)
    assert m, "csrf token not in page"
    return m.group(1)


@pytest.mark.parametrize(
    "path,classes",
    [
        ("/queue", ["controls", "panel-region", "preset-group", "preset",
                    "filter-group", "action-buttons", "tablewrap", "badge"]),
        ("/closed", ["controls", "panel-region", "preset-group", "preset",
                     "filter-group", "action-buttons", "tablewrap", "badge"]),
    ],
)
def test_shared_panel_button_table_badge_classes(client, path, classes, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    html = _html(client, path)
    for cls in classes:
        assert re.search(rf"\b{cls}\b", html), f"missing class {cls} on {path}"


def test_closed_uses_queue_compatible_theme_not_legacy(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    cl = _html(client, "/closed")
    # Legacy /closed accent + background must be gone in favor of the queue theme.
    assert "#1f5faa" not in cl
    assert "#f6f8fa" not in cl
    assert "#1a73e8" in cl          # queue accent used for presets/buttons/nav
    assert "background:#f5f5f5" in cl
    # Offline banner now uses the shared .banner style; summary uses .filter-summary.
    assert 'class="banner"' in cl
    assert "filter-summary" in cl


def test_queue_js_selectors_and_dom_ids_unchanged(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    q = _html(client, "/queue")
    # The IDs/selectors the queue JS hooks into must still exist in markup or JS.
    for token in ("queue-table", "review_view", "filter-overdue",
                  "filter-responded", "filter-waiting", "filter-missing",
                  "last-opened-jump", "last-opened-hidden",
                  "data-ticket-id", "class=apply", "class=reset",
                  "action=/queue"):
        assert token in q, f"missing queue token {token}"


def test_queue_filters_and_review_still_work_after_theme(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    # Apply Filters / Reset present and form submits to /queue.
    q = _html(client, "/queue")
    assert "Apply Filters" in q and "Reset to Defaults" in q
    assert 'action=/queue' in q
    # Local review write still works (its token scraped from the page).
    resp = client.post("/queue/api/review", data={
        "csrf_token": _token(client), "ticket_id": "500007", "review_result": "Resolved",
    })
    assert resp.status_code in (200, 302, 303)


def test_closed_route_columns_preserved(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    cl = _html(client, "/closed")
    for col in ("Ticket ID", "Subject", "Status", "Closed date",
                "Current tags", "Housekeeping", "Freshdesk ticket"):
        assert col in cl, f"missing closed column {col}"


def test_external_links_safe_target_and_rel(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    for path in ("/queue", "/closed"):
        html = _html(client, path)
        # Every target=_blank link must carry the noopener noreferrer rel.
        targets = re.findall(r'<a [^>]*target=_blank[^>]*>', html)
        assert targets, f"expected some external links on {path}"
        for a in targets:
            assert 'rel="noopener noreferrer"' in a, f"unsafe link on {path}: {a}"


def test_offline_render_no_key_no_network(client, monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    for path in ("/queue", "/closed"):
        resp = client.get(path)
        assert resp.status_code == 200, path
    # No Freshdesk write route exists for either page; /closed and its local
    # review endpoints are offline-only POST routes, never touching Freshdesk.
    methods = {r.rule: sorted((r.methods or set()) - {"HEAD", "OPTIONS"})
               for r in app.app.url_map.iter_rules()}
    assert methods["/closed"] == ["GET"]
    assert methods["/closed/api/review"] == ["POST"]
    assert methods["/closed/api/opened"] == ["POST"]
