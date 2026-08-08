# Closed Ticket Housekeeping — Freshdesk API v2 Contract

**Accessed:** 2026-08-05
**Authoritative source reviewed:** [Freshdesk Developer Documentation — API v2, Filter Tickets](https://developers.freshdesk.com/api/#filter_tickets), plus the API documentation's pagination, rate-limit, and error sections. This document records public documentation only. No account endpoint or Freshdesk API endpoint was contacted.

## Supported facts used by this milestone

| Contract item | Officially documented fact | Foundation use |
|---|---|---|
| Endpoint | `GET /api/v2/search/tickets?query=[query]` | Future read-only retrieval endpoint; this offline milestone does not instantiate a real transport. |
| Closed status | `status` is a supported integer field. Freshdesk's documented ticket status enumeration uses **5 = Closed**. | Every closed-housekeeping query contains `status:5`; local results are independently checked for status `5`. |
| Tags | `tag` is a supported string field. | Missing-tags option emits `tag:null`. |
| Null predicate | To filter a field with no assigned value, use keyword `null`. | `tag:null` is the documented missing-tag expression. |
| Date field | `closed_at` is a supported date field (`YYYY-MM-DD`). | Inclusive calendar-day window predicates use `closed_at:>'YYYY-MM-DD'` and `closed_at:<'YYYY-MM-DD'`. |
| Date comparison | `:>` is greater-than-or-equal-to; `:<` is less-than-or-equal-to for date and numeric fields. | Both bounds are inclusive, so adjacent split windows intentionally overlap at their midpoint and ticket IDs are deduplicated. |
| Date input | Date-field input must be UTC format. | The UI calculates a local calendar range for display, then sends explicit `YYYY-MM-DD` calendar dates. A future live pilot must confirm the account's intended UTC boundary semantics. |
| Operators | `AND`, `OR`, and parentheses may group conditions. | The foundation only constructs fixed `AND` clauses; it does not accept raw query syntax. |
| Quoting | The entire query string must be enclosed in a pair of double quotes and may be up to 512 characters. | The builder returns a quoted query and separately percent-encodes it for URL parameters. Date literals are single-quoted. |
| URL encoding | The query must be URL encoded. | The builder uses `urllib.parse.urlencode`; no string concatenation with user query text is allowed. |
| Pagination | The search endpoint returns a fixed 30 objects per page. `page` starts at 1 and must not exceed 10. | The guarded probe sends only `page=1`; it deliberately does **not** send `per_page`, which is documented for the separate list-tickets endpoint, not this search endpoint. Constants are `30`, `10`, and a 300-result per-query ceiling. The orchestrator never asks for page 11. |
| Count | Search response returns total result count with results. | Page 1 total drives page planning and split decisions. |
| Indexing | Ticket updates can take a few minutes to be indexed and then become available through the API. | A future live result represents indexed search state, not a guaranteed real-time ticket state. |
| Archived tickets | Archived tickets are not included in search results. | The feature cannot claim archived-ticket completeness. |
| Rate-limit headers | Rate-limit documentation identifies `X-RateLimit-Total`, `X-RateLimit-Remaining`, and `X-RateLimit-Used-CurrentRequest` response headers. | Fake transport can report the rate-limit condition; no retry loop is added in this foundation. |
| 429 | A rate-limited request receives HTTP `429`; documentation directs callers to honor `Retry-After`. | A 429 becomes visible incomplete/error state and preserves a numeric `Retry-After` value when supplied. |

## Search shape

A missing-tags query for 2026-06-07 through 2026-08-05 is constructed as:

```text
"status:5 AND tag:null AND closed_at:>'2026-06-07' AND closed_at:<'2026-08-05'"
```

The URL parameter form is generated with `urlencode`, for example `query=%22status%3A5...%22&page=1`; code never interpolates a raw user query.

## Retrieval limit and splitting

The official 30-object page size and page-10 maximum mean one query can expose at most **300** returned results. A reported total above 300 is split by deterministic calendar midpoint. The planner uses non-overlapping calendar-date partitions so it can make deterministic progress. Deduplication by Freshdesk ticket ID still prevents duplicate display if a future transport returns a boundary duplicate. A single calendar day that still reports more than 300 results is marked incomplete:

> More than 300 matching tickets were closed on one date. This range cannot be fully retrieved through the Search Tickets page limit.

## Confirmed discrepancy / limitation

The official Filter Tickets table describes `closed_at` as `YYYY-MM-DD`, while the general note says date-field input must be UTC format. This foundation uses the documented calendar `YYYY-MM-DD` representation and records the user's locally displayed dates separately. It makes no unverified claim about time-of-day boundary behavior. A future read-only live pilot must validate this with non-sensitive test data before enabling any live transport.

## Deliberate safety decision

No dormant real HTTP adapter is included. The only transport is an injectable synthetic fixture transport. Therefore the `/closed` implementation has no Freshdesk URL, authentication path, key read, or write-capable HTTP method. Offline mode has no possible live fallback.

## List All Tickets endpoint — batch stats contract

**Accessed:** 2026-08-08
**Authoritative source reviewed:** [Freshdesk Developer Documentation — API v2, List All Tickets](https://developers.freshdesk.com/api/#list_all_tickets) and the API documentation's pagination and rate-limit sections. This document records public documentation only. No Freshdesk API endpoint was contacted for this section.

| Contract item | Officially documented fact | Batch-probe use |
|---|---|---|
| Endpoint | `GET /api/v2/tickets` | Returns a JSON array of ticket objects. Used for one-page batch retrieval with stats. |
| `include=stats` | Embedding stats via `?include=stats` "will return the ticket's `closed_at`, `resolved_at` and `first_responded_at` time." | Each ticket in the list response carries a nested `stats` dictionary when `include=stats` is sent. |
| `per_page` | "The maximum number of objects that can be retrieved per page is 100. Invalid values and values greater than 100 will result in an error." | The batch probe forces `per_page=100`. |
| `page` | "The page number starts with 1." | The batch probe forces `page=1` and does not request page 2. |
| `updated_since` | `?updated_since=2015-01-19T02:00:00Z` | The batch probe sends `updated_since=2026-08-01T00:00:00Z` to scope results. |
| `order_by` | Supported fields: `created_at`, `due_by`, `updated_at`, `status`. Default is `created_at`. | The batch probe forces `order_by=status`. |
| `order_type` | Supported values: `asc`, `desc`. Default is `desc`. | The batch probe forces `order_type=desc`. |
| Rate-limit cost | "Each include will consume an additional 2 credits. For example if you embed the stats information you will be charged a total of 3 API credits for the call." | The batch probe records `X-RateLimit-Used-CurrentRequest` from the response header; no additional capacity is purchased or enabled. |
| Link header | "The 'link' header in the response will hold the next page url if exists." | The batch probe records whether a Link header is present and whether it indicates a next page, but never follows it. |
| Default window | "By default, only tickets that have not been deleted or marked as spam will be returned." The 30-day default applies unless `updated_since` is used. | The batch probe sends `updated_since` to override the 30-day default. |

### Key difference from Search Tickets

List All Tickets does **not** support the same arbitrary query expression as `GET /api/v2/search/tickets?query=`. It supports predefined filters (`filter=new_and_my_open`, `watching`, `spam`, `deleted`), requester-based filters, and company-based filters, but not the `status:5 AND tag:null AND closed_at:>'...'` expression used by the search endpoint. Therefore, closed-status and missing-tags filtering must be applied **locally** to the list response, not via query parameters.

### Local filtering criteria applied by the batch probe

A ticket is kept when all of the following hold:

1. `status == 5` (Closed)
2. `tags` is an empty list `[]` (missing tags)
3. `stats.closed_at` is present, non-null, a string, and parseable by `parse_dt()`

For the local date-window aggregate, the probe additionally checks:

```
2026-08-01T00:00:00Z <= stats.closed_at < 2026-08-04T00:00:00Z
```

This represents calendar dates August 1 through August 3 inclusive. The request itself is never modified based on this date range; it is a post-response comparison only.

## Non-goals

This milestone does not write tags, invoke `POST`, `PUT`, `PATCH`, or `DELETE`, access a Freshdesk tenant, test authentication, or follow ticket links.

## Normal-order rate-limited batch retriever (Prompt 22)

**Accessed:** 2026-08-08
**Authoritative source:** [Freshdesk API v2 — List All Tickets](https://developers.freshdesk.com/api/#list_all_tickets), with the documentation's [pagination](https://developers.freshdesk.com/api/#pagination), [rate-limit](https://developers.freshdesk.com/api/#ratelimit), and error sections.

The retriever uses only `GET /api/v2/tickets` with `include=stats`, `per_page=100`, `page=1..N`, and an `updated_since` lower bound. It intentionally omits both `order_by` and `order_type`, therefore relying on Freshdesk's documented default ordering (`created_at` with descending default order). The page number starts at 1; the response `Link` header supplies a next-page URL when another page exists. `stats.closed_at` is available when `include=stats` is requested. Rate-limit response headers include `X-RateLimit-Total`, `X-RateLimit-Remaining`, and `X-RateLimit-Used-CurrentRequest`; the retriever also records `Retry-After` when supplied. A 429 is handled as a bounded same-page retry, never an immediate unbounded retry. Freshdesk documents HTTP error responses and pagination, but does not promise a `Retry-After` value for every 429, so the implementation uses a conservative 60-second fallback when it is absent.

The live validation uses `updated_since=2026-07-31T23:59:55Z` and locally retains only status 5, exactly-empty tags, valid `stats.closed_at`, and the half-open Aug 1–4 UTC window. Dashboard routes remain offline and the retriever is a standalone service.

## Multi-page pagination and status-order stop rule

**Accessed:** 2026-08-08 (Prompt 20)

### Verified official documentation facts

| Contract item | Officially documented fact | Pagination-probe use |
|---|---|---|
| `order_by=status` | Supported field, default `created_at`. | The probe forces `order_by=status` to group statuses contiguously. |
| `order_type=desc` | Supported values `asc`, `desc`; default `desc`. | The probe forces `order_type=desc` so higher statuses sort first, placing status 5 (Closed) in a contiguous block. |
| `page` | "The page number starts with 1." | The probe requests sequential pages 1..N (N ≤ 15). It does NOT follow the Link header URL directly; it constructs `page=<next>` from an incremented integer and validates that number. |
| Link header | "The 'link' header in the response will hold the next page url if exists." | The presence/absence of a next-page Link is recorded per page. If state still needs more pages but no Link exists, the dataset is treated as exhausted and the probe stops successfully. |
| Rate-limit headers | `X-RateLimit-Total`, `X-RateLimit-Remaining`, `X-RateLimit-Used-CurrentRequest` | The probe inspects `X-RateLimit-Remaining` before every next request. If remaining ≤ 40 (conservative floor), it stops without making the request. |

### Status-desc ordering and the Closed block

Because `order_by=status&order_type=desc` sorts by numeric status descending:

- Statuses > 5 appear first
- Status 5 (Closed) forms a contiguous block
- Statuses < 5 appear after the Closed block

**Key assumption to verify on live data:** If the sort is behaving consistently, once a response record has status < 5 after status 5 has been seen, no later page should contain status 5. The probe must validate this assumption by checking sort monotonicity (`previous_status >= current_status`) across ALL records and page boundaries — not blindly trust it.

### Stop conditions (first match wins)

1. Status block exited (first status < 5 seen after status 5 block) → stop after current page
2. No next-page Link present → stop (dataset exhausted)
3. Page 15 reached → stop (safety cap)
4. `X-RateLimit-Remaining` ≤ 40 → stop (rate-limit safety floor)
5. Sort violation detected → stop (early-stop not safe, review required)
6. HTTP/JSON/transport error → stop immediately

### Credential and safety constraints

- GET only; no POST/PUT/PATCH/DELETE
- No retries; one request per page; sequential pages only (no skips, no duplicates)
- No View Ticket (`GET /api/v2/tickets/<id>`) calls
- No Search Tickets (`/api/v2/search/tickets`) calls
- No conversations, requester lookups, attachment requests, notes, or replies
- TLS verification enforced; foreign redirects rejected
- API key never output to logs, stdout, or stored in source/fixtures
