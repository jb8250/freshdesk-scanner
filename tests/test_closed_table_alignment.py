"""Prompt 13 tests — the Closed results table aligned to the /queue row layout.

The /closed table was refactored as the same component as /queue: a compact
header (Ticket | Subject | Review), shared .tablewrap/table/.badge/.badges/
a.tid/a.sbj/rvform-select styling, and a Subject cell that consolidates the
old wide columns (Status, Closed date, Current tags, Housekeeping, duplicate
Freshdesk link) into badge/metadata lines under the row. Every important
fact (subject link, status label, closed date, tags, review state, Last
Opened, review control) is still present in the rendered row.

No Freshdesk network, API key, live retrieval, or write path is exercised;
the conftest autouse block_network fixture makes any attempted
requests.get/post fail loudly.
"""
import re

import pytest

from app import closed_display


def _html(client, query=""):
    resp = client.get("/closed" + query)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_data(as_text=True)


def _row(html, ticket_id):
    m = re.search(
        r'<tr class="[^"]+" data-ticket-id="%s">.*?</tr>' % ticket_id, html, re.S
    )
    assert m, f"row #{ticket_id} not found"
    return m.group(0)


def _queue_html(client):
    q = client.get("/queue?overdue=1&responded=1&waiting=1")
    assert q.status_code == 200
    return q.get_data(as_text=True)


# ---------------------------------------------------------------------------
# table structure — the /queue layout
# ---------------------------------------------------------------------------


def test_header_table_uses_queue_style_columns(client):
    """Old eight-column layout is gone; the queue's compact header is used
    (Ticket | Subject | Review) with the queue's bare <tr> header and a
    visually-hidden caption."""
    html = _html(client)
    assert re.search(
        r"<table id=closed-table><caption class=visually-hidden>"
        r"Closed ticket search results</caption><tr>"
        r"<th scope=col>Ticket</th><th scope=col>Subject</th>"
        r"<th scope=col>Review</th></tr>", html
    ), "closed table header is not the queue-style three-column header"
    for legacy in ("<th scope=col>Ticket ID</th>", "<th scope=col>Closed date</th>",
                   "<th scope=col>Current tags</th>", "<th scope=col>Housekeeping</th>",
                   "<th scope=col>Review Result</th>",
                   "<th scope=col>Freshdesk ticket</th>"):
        assert legacy not in html, f"legacy closed column header still present: {legacy}"
    assert "<thead>" not in html, "closed table should use the queue's bare <tr> header"


def test_queue_table_layout_unchanged(client):
    """Regression guard: /queue (the source of truth) keeps its own header."""
    q = _queue_html(client)
    assert re.search(
        r"<th scope=col>Ticket</th><th scope=col>Subject</th>"
        r"<th scope=col>Status</th>\s*<th scope=col>Badges</th>\s*"
        r"<th scope=col>Review</th>", q
    ), "/queue header structure changed unexpectedly"


# ---------------------------------------------------------------------------
# preserved row facts, in the queue-style arrangement
# ---------------------------------------------------------------------------


def test_ticket_and_subject_links_preserved(client):
    html = _html(client)
    row = _row(html, 810001)
    # Ticket number stays the primary link of the first cell.
    assert re.search(
        r'<a class="tid fd-link" href="https://[^"]+" target=_blank '
        r'rel="noopener noreferrer" data-ticket-id="810001" '
        r'aria-label="Open ticket #810001 in Freshdesk \(new tab\)">#810001</a>', row
    )
    # Subject is the second cell's link, its text preserved verbatim.
    assert 'class="sbj fd-link"' in row
    assert "Synthetic closed untagged" in row
    assert 'aria-label="Open subject of ticket #810001 in Freshdesk (new tab)"' in row


def test_status_label_is_closed_badge_and_raw_5_never_rendered(client):
    html = _html(client)
    row = _row(html, 810001)
    assert '<span class="badge b-closed">Closed</span>' in row
    assert "MISSING TAGS" in row
    assert re.search(r">5<", html) is None, "raw status 5 must never be visible"


def test_closed_date_badge_uses_compact_display(client):
    html = _html(client, "?missing_tags=0&review_view=all")
    row = _row(html, 810001)
    assert '<span class="badge b-date">2026-08-04 09:00</span>' in row
    row2 = _row(html, 810002)
    assert '<span class="badge b-date">2026-08-03 10:00</span>' in row2
    # The badge CSS rule ships with the shared stylesheet.
    assert ".b-date{background:#00838f;color:#fff}" in html


def test_tags_metadata_line_preserves_tag_list(client):
    html = _html(client, "?review_view=all&missing_tags=0")
    untagged = _row(html, 810001)
    tagged = _row(html, 810002)
    assert '<div class="closed-tags" style="margin-top:4px">No tags</div>' in untagged
    assert '<div class="closed-tags" style="margin-top:4px">Tags: parts</div>' in tagged
    assert '<span class="badge b-missing">MISSING TAGS</span>' in untagged
    assert "MISSING TAGS" not in tagged, "810002 has tags, no missing badge"


def test_review_state_badge_and_select_in_row(client, fixed_clock):
    """Review state badge (queue-style b-review) and the Review select both
    render inside the row; the select keeps its per-row aria-label."""
    from app import set_closed_review_result
    set_closed_review_result(810001, "Needs Follow-Up")
    html = _html(client, "?review_view=all&missing_tags=0")
    row = _row(html, 810001)
    assert '<span class="badge b-review rv-followup">Needs Follow-Up</span>' in row
    assert re.search(
        r'<select name=review_result aria-label="Review result for closed '
        r'ticket 810001" onchange="this.form.submit\(\)">', row)
    assert re.search(r'<option value="Needs Follow-Up" selected>', row)
    assert re.search(
        r'<form class="?rvform"? method="?post"? action=/closed/api/review>', row)


def test_last_opened_badge_in_subject_metadata(client, fixed_clock):
    from app import mark_closed_opened
    mark_closed_opened(810002)
    html = _html(client, "?review_view=all&missing_tags=0")
    row = _row(html, 810002)
    assert '<span class="badge b-last-opened">LAST OPENED</span>' in row
    # Badge flow: closed -> date -> review state -> LAST OPENED -> tags line.
    assert row.index("LAST OPENED") > row.index("b-date")
    assert "rv-last-opened" in row


def test_no_extra_open_ticket_column(client):
    """Old 8th 'Open ticket' column is removed; the only row links are the
    ticket number and the subject (both already open Freshdesk)."""
    html = _html(client)
    assert ">Open ticket<" not in html
    rows = re.findall(r'<tr class="[^"]*" data-ticket-id="\d+">.*?</tr>', html, re.S)
    assert rows
    for row in rows:
        assert row.count('class="tid fd-link"') == 1, row[:200]
        assert row.count('class="sbj fd-link"') == 1, row[:200]
        assert row.count("target=_blank") == 2, row[:200]


def test_review_form_hidden_fields_preserved(client):
    html = _html(client, "?review_view=all&missing_tags=0")
    row = _row(html, 810001)
    for field in ("name=csrf_token", "name=ticket_id", "name=days",
                  "name=missing_tags", "name=review_view", "name=review_result"):
        assert field in row, f"missing {field} in closed row"


def test_responsive_uses_shared_table_wrap_and_no_closed_specific_widths(client):
    html = _html(client)
    assert ".tablewrap{overflow-x:auto" in html
    assert re.search(r"table\{[^}]*min-width:960px", html)
    assert re.search(r"#closed-table\{", html) is None, (
        "no closed-specific CSS rule; both tables share the queue stylesheet")


# ---------------------------------------------------------------------------
# closed_display() presentation helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("", ""),
    ("2026-08-04T09:00:00Z", "2026-08-04 09:00"),
    ("2026-08-04T12:00:00Z", "2026-08-04 12:00"),
    ("not-a-date", "not-a-date"),
])
def test_closed_display_compacts_iso_timestamps(raw, expected):
    assert closed_display(raw) == expected