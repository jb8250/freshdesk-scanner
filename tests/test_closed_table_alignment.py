"""Offline-only tests for Prompt 14 — Table Structure Parity.

Prompt 13 compressed /closed to Ticket | Subject | Review and pushed every
secondary fact into the Subject cell. Prompt 14 reverses that: /closed
returns to dedicated queue-style columns —

    Ticket | Subject | Status | Badges | Review | Closed | Updated | Created | Tags

and /queue loses its visible Priority (plus the extra Type column that the
actual markup had but the Prompt-14 expected-state summary did not list, so
both tables line up 1:1). The ONLY intentional structural difference is the
sixth column's heading: "Due / SLA" on /queue versus "Closed" on /closed.

Every test in this file runs fully offline against synthetic fixtures; the
autouse conftest network blocker turns any accidental requests.get/post into
a loud failure.

Test-changes relative to the Prompt-13 version of this file (each is called
out in the affected docstring below):

- test_header_table_uses_queue_style_columns        : 3-col header -> exact 9-col closed header.
- test_queue_table_layout_unchanged_priority_gone   : renamed/scope: now the /queue counter-test asserting 9 columns + no Priority/Type.
- test_ticket_and_subject_links_preserved           : unchanged intent; scoped to the new row shape.
- test_status_label_is_closed_and_raw_5_never...    : badge-in-Subject -> Status column cell; renamed.
- test_closed_date_columns_use_compact_display      : b-date badge -> dedicated Closed column (meta cell).
- test_tags_column_uses_queue_presentation          : tags line under Subject -> Tags column; old "No tags" under-Subject line is gone by spec.
- test_review_state_badge_and_select_in_row         : badge moved from Subject cluster to Badges column.
- test_last_opened_badge_in_badges_column           : moved from Subject metadata to Badges column.
- test_no_extra_open_ticket_column / no thead       : unchanged.
- test_review_form_hidden_fields_preserved          : unchanged.
- test_responsive_uses_shared_table_wrap...         : unchanged + no .b-date CSS leftover.
- test_closed_display_compacts_iso_timestamps       : parametrized cases updated: missing/malformed now render an em dash (safe), not a blank or the raw string.
"""
import re
import urllib.parse

import pytest

import app

# The nine shared columns; the only deliberate difference is index 5.
QUEUE_COLUMNS = ["Ticket", "Subject", "Status", "Badges", "Review",
                 "Due / SLA", "Updated", "Created", "Tags"]
CLOSED_COLUMNS = ["Ticket", "Subject", "Status", "Badges", "Review",
                  "Closed", "Updated", "Created", "Tags"]


def _html(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, path
    return resp.get_data(as_text=True)


def _table_body(html, table_id):
    m = re.search(rf"<table id={table_id}>(.*?)</table>", html, re.S)
    assert m, f"table {table_id} not found"
    return m.group(1)


def _headers(tab):
    return re.findall(r"<th scope=col>([^<]+)</th>", tab)


def _rows(tab):
    return re.findall(r'<tr class="[^"]*" data-ticket-id="\d+">(.*?)</tr>', tab, re.S)


def _row_cells(row):
    return re.findall(r"<td(?:\s[^>]*)?>", row)


def _cell_text(cell):
    return re.sub(r"<[^>]+>", "", cell).strip()


# --- header / column-count parity ----------------------------------------

def test_header_table_uses_queue_style_columns(client):
    """The closed table's bare header row is exactly nine queue-parity
    columns (was: Ticket | Subject | Review after Prompt 13)."""
    tab = _table_body(_html(client, "/closed"), "closed-table")
    assert _headers(tab) == CLOSED_COLUMNS, _headers(tab)


def test_queue_table_layout_unchanged_priority_gone(client):
    """/queue keeps its untouched column set minus the visible Priority
    (and the extra Type column the actual markup had, which the Prompt-14
    summary did not list), so the two tables line up 1:1. Priority data
    stays in the row dict for any internal use — it is only unrendered."""
    tab = _table_body(_html(client, "/queue"), "queue-table")
    assert _headers(tab) == QUEUE_COLUMNS, _headers(tab)
    assert "<th scope=col>Priority</th>" not in tab
    assert "<th scope=col>Type</th>" not in tab


def test_only_intentional_difference_is_closed_vs_due_sla(client):
    """Headers match pairwise except column 6 (Closed <> Due / SLA)."""
    q = _headers(_table_body(_html(client, "/queue"), "queue-table"))
    c = _headers(_table_body(_html(client, "/closed"), "closed-table"))
    assert len(q) == len(c) == 9
    for left, right in zip(CLOSED_COLUMNS, q):
        assert left == right or (CLOSED_COLUMNS.index(left) == 5 and right == "Due / SLA")


def test_rows_match_header_cell_count(client):
    """Every data row has exactly as many cells as its header (9 on both)."""
    for path, tid, ncols in (("/queue", "queue-table", len(QUEUE_COLUMNS)),
                             ("/closed", "closed-table", len(CLOSED_COLUMNS))):
        tab = _table_body(_html(client, path), tid)
        rows = _rows(tab)
        assert rows, f"no rows on {path}"
        counts = {len(re.findall(r"<td(?:\s[^>]*)?>", row)) for row in rows}
        assert counts == {ncols}, (path, counts)


def test_no_thead_and_no_extra_open_ticket_column(client):
    """Both tables keep the shared bare-<tr> header pattern; the old
    independent "Ticket ID"-style columns stay gone from /closed."""
    q = _table_body(_html(client, "/queue"), "queue-table")
    c = _table_body(_html(client, "/closed"), "closed-table")
    assert "<thead>" not in q and "<thead>" not in c
    for legacy in ("Ticket ID", "Closed date", "Current tags", "Housekeeping",
                   "Review Result", "Freshdesk ticket", "Priority"):
        assert f"<th scope=col>{legacy}</th>" not in c, legacy


def test_priority_absent_from_both_visible_tables(client):
    """No Priority header or priority-value cell anywhere on either page."""
    for path, tid in (("/queue", "queue-table"), ("/closed", "closed-table")):
        html = _html(client, path)
        tab = _table_body(html, tid)
        assert "Priority" not in tab
        assert "priority" not in tab.lower()


# --- closed cell-by-cell placement -------------------------------------

def test_ticket_and_subject_links_preserved(client):
    """Ticket and Subject anchors keep their handling and data attributes
    (new-tab, safe rel, click-tracking hooks) in the new layout."""
    tab = _table_body(_html(client, "/closed"), "closed-table")
    rows = _rows(tab)
    assert rows
    first = rows[0]
    assert re.search(r'<td><a class="tid fd-link" href="[^"]+" target=_blank '
                     r'rel="noopener noreferrer" data-ticket-id="\d+" '
                     r'aria-label="Open ticket #\d+ in Freshdesk \(new tab\)">#\d+</a></td>', first)
    assert re.search(r'<td><a class="sbj fd-link" href="[^"]+" target=_blank '
                     r'rel="noopener noreferrer" data-ticket-id="\d+" '
                     r'aria-label="Open subject of ticket #\d+ in Freshdesk \(new tab\)">', first)


def test_status_label_is_closed_and_raw_5_never_rendered(client):
    """The Status column spells "Closed" (never the raw integer 5, never a
    badge — plain queue-style label as /queue renders)."""
    tab = _table_body(_html(client, "/closed"), "closed-table")
    statuses = re.findall(r"<td>Closed</td>", tab)
    assert statuses, "no Closed status cells"
    assert re.search(r">5<", tab) is None, "raw status 5 must never be visible"


def test_closed_date_columns_use_compact_display(client):
    """closed_at renders in the Closed column as compact 'YYYY-MM-DD HH:MM'
    (queue date-column style), never raw ISO; Updated renders in Eastern local
    time. Missing/malformed values show an em dash."""
    # 810001 has no photo/video subject; turn scope OFF to include it.
    html = _html(client, "/closed?photo_video_only=0")
    tab = _table_body(html, "closed-table")
    # Untagged fixture 810001: closed 2026-08-04T09:00 -> "2026-08-04 09:00";
    # updated 2026-08-04T10:00Z -> Eastern "8/4/26 6:00 AM EDT" (summer).
    m = re.search(r'<tr class="[^"]*" data-ticket-id="810001">(.*?)</tr>', tab, re.S)
    assert m, "fixture 810001 missing"
    metas = re.findall(r'<td class=meta>([^<]*)</td>', m.group(1))
    assert metas[:3] == ["2026-08-04 09:00", "8/4/26 6:00 AM EDT", "2026-07-20"], metas
    assert "not-a-date" not in html, "raw malformed date leaked"


def test_missing_and_malformed_dates_never_render_junk(client):
    """Malformed (810007) and missing (810006) closed_at values are never
    rendered as junk or raw strings: the date-window retrieval excludes
    them safely (pre-existing Prompt-12 behaviour), and the closed_display
    unit contract (parametrized below) covers the dash fallback for any
    value that does reach the template."""
    tab = _table_body(_html(client, "/closed"), "closed-table")
    assert "not-a-date" not in tab
    assert "810007" not in tab and "810006" not in tab
    # A well-formed fixture date renders compact, proving the column works.
    assert '<td class=meta>2026-08-04 08:00</td>' in tab


def test_subject_cell_contains_subject_only(client):
    """The Subject cell holds just the subject link: no status badge, no
    date badge, no tags line beneath it (Prompt 14 requirement)."""
    tab = _table_body(_html(client, "/closed"), "closed-table")
    for row in _rows(tab):
        m = re.search(r'<td>(<a class="sbj fd-link"[^>]*>.*?</a>)</td>', row, re.S)
        assert m, f"subject-only cell not found in row: {row[:180]}"
        assert "badge" not in m.group(1)


def test_review_state_badge_and_select_in_row(client, fixed_clock):
    """The review-state badge sits in the Badges column and the matching
    queue-style <select> in the Review column."""
    tab = _table_body(_html(client, "/closed"), "closed-table")
    rows = _rows(tab)
    assert rows
    row = rows[0]
    badges_cell = re.search(r'<div class=badges>.*?</div>', row)
    assert badges_cell, "badges div missing"
    assert "b-review" in badges_cell.group(0)
    assert re.search(r'<select name=review_result[^>]*aria-label="Review result for closed ticket \d+"',
                     row), "review select missing"


def test_last_opened_badge_in_badges_column(client, fixed_clock):
    """LAST OPENED is a Badges-column badge (moved from the Prompt-13
    Subject metadata), so the queue badge pattern is shared. Uses the real
    /closed/api/opened endpoint (JSON, X-CSRF-Token) to set the local
    last-opened state, then re-renders. 810002 has tags and no photo/video
    subject, so the page is loaded with missing_tags=0 AND photo scope OFF."""
    import re
    html = _html(client, "/closed?missing_tags=0&photo_video_only=0")
    m = re.search(r'name=csrf_token value="([^"]+)"', html)
    assert m, "no csrf token rendered"
    token = m.group(1)
    resp = client.post("/closed/api/opened",
                       json={"csrf_token": token, "ticket_id": 810002})
    assert resp.status_code == 200
    tab2 = _table_body(_html(client, "/closed?missing_tags=0&photo_video_only=0"), "closed-table")
    row = re.search(r'<tr class="[^"]*rv-last-opened[^"]*" data-ticket-id="810002">(.*?)</tr>', tab2, re.S)
    assert row, "last-opened row class missing"
    badges = re.search(r'<div class=badges>.*?</div>', row.group(1), re.S)
    assert badges, "badges div missing in last-opened row"
    assert 'class="badge b-last-opened"' in badges.group(0)


def test_review_form_hidden_fields_preserved(client):
    """Review forms keep csrf/ticket_id/days/missing_tags/review_view."""
    tab = _table_body(_html(client, "/closed"), "closed-table")
    form = re.search(r'<form class=rvform[^>]*action=/closed/api/review>.*?</form>', tab, re.S)
    assert form, "rv form missing"
    for name in ("csrf_token", "ticket_id", "days", "missing_tags", "review_view"):
        assert f"name={name}" in form.group(0), name


def test_tags_column_uses_queue_presentation(client):
    """Tags live in the Tags column exactly like /queue: joined text for
    tagged rows, the shared <em>none</em> token for untagged rows. No
    second 'No tags' line under Subject (Prompt 14 requirement — the
    Prompt-13 under-Subject tags line is gone)."""
    cl = _html(client, "/closed")
    assert '<em style=color:#bbb>none</em>' in cl
    assert "No tags" not in cl
    assert "closed-tags" not in cl
    # The untagged row's Tags cell is the queue-style token.
    tab = _table_body(cl, "closed-table")
    assert re.search(r"<td><em style=color:#bbb>none</em></td>", tab)


def test_tags_column_renders_actual_tag(client):
    """The synthetic tagged ticket (810002, tags: parts) shows its tag in
    the Tags column when the Missing-Tags-Only filter is off (the default
    view excludes tagged tickets by design). 810002 has no photo/video
    subject, so the Photo/Video scope must be turned OFF to include it."""
    tab = _table_body(_html(client, "/closed?missing_tags=0&photo_video_only=0"), "closed-table")
    row = re.search(r'<tr class="[^"]*" data-ticket-id="810002">(.*?)</tr>', tab, re.S)
    assert row, "fixture 810002 missing"
    assert re.search(r"<td>parts</td>", row.group(1)), "tag not rendered in Tags column"


def test_responsive_uses_shared_table_wrap_and_no_closed_specific_widths(client):
    """The closed table uses the same .tablewrap scroller as the queue and
    adds no closed-only width or date-badge CSS."""
    html = _html(client, "/closed")
    assert "tablewrap" in html
    assert "b-date" not in html
    assert re.search(r'<td[^>]*style="[^"]*min-width', html) is None, "inline width hack on closed cells"


# --- display helper ----------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-04T09:00:00Z", "2026-08-04 09:00"),
        ("2026-08-04T12:00:00Z", "2026-08-04 12:00"),
        ("2026-08-04 09:00:00+00:00", "2026-08-04 09:00"),
        ("", "—"),
        (None, "—"),
        ("not-a-date", "—"),
        ("garbage", "—"),
        ("2021-01-01", "2021-01-01"),
    ],
)
def test_closed_display_compacts_iso_timestamps(raw, expected):
    """closed_display compactness + safe-dash contract (updated from the
    Prompt-13 blank-string behaviour)."""
    from app import closed_display
    assert closed_display(raw) == expected