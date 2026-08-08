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

## Non-goals

This milestone does not write tags, invoke `POST`, `PUT`, `PATCH`, or `DELETE`, access a Freshdesk tenant, test authentication, or follow ticket links.
