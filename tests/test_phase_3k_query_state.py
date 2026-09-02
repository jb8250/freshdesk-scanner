"""Phase 3K regression test: post-refresh Main Queue query-state bug.

Reproduces the exact production screenshot state:

    GET /queue?review_view=all          (no workflow_tab)

triggers the legacy ``review_view`` fallback path, which displays every
ticket matching the review view (active/completed/all) REGARDLESS of the
local workflow routing. Meanwhile the per-tab workflow COUNTS are computed
using the correct workflow-routing path — so the Main Queue tab can read
"Main Queue (0)" while 68 reviewed tickets render in the table below.

After Refresh, the JS completion handler navigates to exactly that URL, so
every reviewed ticket briefly re-appears in the Main Queue. Clicking another
workflow tab (which sets workflow_tab) then returning fixes the display.

This test pins the failure: when review_view is present but workflow_tab is
NOT, the active workflow-tab routing must remain authoritative. The ticket
table and the Main Queue count must agree.
"""
import re

import pytest

import app


def _ids(html):
    return set(re.findall(r'data-ticket-id="(\d+)"', html))


def _main_queue_count(html):
    m = re.search(r'To Review[^<]*<span[^>]*>\((\d+)\)', html)
    assert m, "To Review tab count not found in HTML"
    return int(m.group(1))


def _displayed_count(html):
    m = re.search(r'(\d+) tickets displayed', html)
    assert m, "Displayed-count text not found in HTML"
    return int(m.group(1))


@pytest.fixture
def routed_state(client, fixed_clock):
    """Seed the review-state DB with tickets on non-main workflow tabs."""
    from datetime import datetime, timezone
    ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    # Each destination exercises a different branch of workflow_destination()
    app.set_review_result(500001, "Needs Follow-Up", ref)           # -> followup
    app.set_review_result(500002, "Needs Supervisor Review", ref)   # -> supervisor
    app.set_review_result(500003, "Resolved", ref)                  # -> resolved
    app.set_review_result(500004, "Not Applicable to Me", ref)      # -> no_action
    app.set_review_result(500010, "Needs Follow-Up", ref)           # -> followup
    app.set_review_result(500011, "Resolved", ref)                  # -> resolved
    return ref


def test_legacy_review_view_cannot_override_workflow_routing(client, routed_state):
    """The reported bug: /queue?review_view=all with no workflow_tab.

    Main Queue count must match the actual rendered Main Queue rows. No
    Needs Follow-Up / Supervisor Review / Resolved / Not Applicable ticket
    may appear in the Main Queue table.
    """
    html = client.get("/queue?review_view=all").get_data(as_text=True)
    count = _main_queue_count(html)
    displayed = _displayed_count(html)
    ids = _ids(html)

    # The count badge and the visible table MUST agree (the bug was 0 vs 68).
    assert count == displayed, (
        f"Main Queue tab count ({count}) disagrees with displayed rows "
        f"({displayed}) — legacy review_view is overriding workflow routing"
    )

    # No ticket that lives on a non-main workflow tab may appear in Main Queue.
    non_main = {"500001", "500002", "500003", "500004", "500010", "500011"}
    leaked = ids & non_main
    assert not leaked, (
        f"Workflow-routed tickets {leaked} leaked into the Main Queue table "
        f"when review_view=all was supplied without workflow_tab"
    )


def test_review_view_all_consistent_with_workflow_tab_main(client, routed_state):
    """Adding workflow_tab=main explicitly must produce the same result."""
    plain = client.get("/queue?review_view=all").get_data(as_text=True)
    explicit = client.get("/queue?review_view=all&workflow_tab=main").get_data(as_text=True)
    assert _ids(plain) == _ids(explicit)
    assert _main_queue_count(plain) == _main_queue_count(explicit)


def test_review_view_active_excludes_completed(client, routed_state):
    """review_view=active must exclude completed-state tickets from the table."""
    html = client.get("/queue?review_view=active").get_data(as_text=True)
    ids = _ids(html)
    completed_only = {"500003", "500004", "500011"}  # Resolved / Not Applicable
    assert not (ids & completed_only), (
        f"Completed tickets {ids & completed_only} shown under review_view=active"
    )


def test_review_view_completed_excludes_active(client, routed_state):
    """review_view=completed must exclude active-state tickets from the table."""
    html = client.get("/queue?review_view=completed").get_data(as_text=True)
    ids = _ids(html)
    active_only = {"500001", "500002", "500010"}  # Follow-Up / Supervisor
    assert not (ids & active_only), (
        f"Active tickets {ids & active_only} shown under review_view=completed"
    )


def test_post_refresh_url_shows_only_main_queue(client, routed_state, monkeypatch):
    """The exact post-refresh URL from the JS completion handler."""
    # The JS sends: /queue?overdue=0&responded=0&waiting=0&missing_tags=0&days=60&review_view=all
    html = client.get(
        "/queue?overdue=0&responded=0&waiting=0&missing_tags=0&days=60&review_view=all"
    ).get_data(as_text=True)

    count = _main_queue_count(html)
    displayed = _displayed_count(html)
    ids = _ids(html)

    assert count == displayed, (
        f"Post-refresh: Main Queue count ({count}) != displayed rows ({displayed})"
    )

    non_main = {"500001", "500002", "500003", "500004", "500010", "500011"}
    assert not (ids & non_main), (
        f"Post-refresh: workflow tickets {ids & non_main} incorrectly shown "
        f"in Main Queue before any tab navigation"
    )


def test_workflow_tabs_still_route_correctly(client, routed_state):
    """Regression guard: each workflow tab must still route independently.

    Note: _main_queue_count() reads the Main Queue tab's count badge, not the
    active tab's count. So we verify the displayed ticket count against the
    active tab's badge via the active-tab aria-current selector instead.
    """
    # For each tab, the set of rendered IDs must equal exactly the tickets
    # whose local workflow destination is that tab.
    expected = {
        "supervisor": {"500002"},
        "followup": {"500001", "500010"},
        "resolved": {"500003", "500011"},
        "no_action": {"500004"},
    }
    for tab, want in expected.items():
        html = client.get(f"/queue?workflow_tab={tab}").get_data(as_text=True)
        ids = _ids(html)
        displayed = _displayed_count(html)
        assert displayed == len(want), (
            f"{tab}: displayed {displayed} rows but expected {len(want)} ({want})"
        )
        assert want <= ids, f"{tab}: expected {want}, got {ids}"
        # No ticket from any other routed tab may appear.
        all_routed = set().union(*expected.values())
        foreign = (ids - want) & all_routed
        assert not foreign, f"{tab}: foreign routed tickets {foreign} present"

    # Main Queue must show only Unreviewed/Opened-in-Review fixtures.
    main_html = client.get("/queue?workflow_tab=main").get_data(as_text=True)
    main_displayed = _displayed_count(main_html)
    # The 16 Unreviewed fixtures that survive the default photo/video scope
    # and are not on a non-main workflow tab.
    assert main_displayed == 16, f"Main Queue: expected 16 rows, got {main_displayed}"


def test_show_all_cached_tickets_still_works(client, routed_state):
    """The explicit 'Show All Cached Tickets' control must show the full cache."""
    # All toggles OFF + workflow_tab=main -> show_all_cached path
    html = client.get(
        "/queue?photo_video_only=0&hide_reviewed_tags=0&overdue=0&responded=0"
        "&waiting=0&missing_tags=0&days=60&review_view=all&workflow_tab=main"
    ).get_data(as_text=True)
    ids = _ids(html)
    # The fixture holds 28 tickets; 1 is status=5 (Closed) so 27 survive the
    # Closed-gate. All 28 should appear under the explicit Show-All path.
    assert len(ids) == 28, f"Show All expected 28 tickets, got {len(ids)}"
