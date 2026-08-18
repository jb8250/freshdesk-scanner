"""Read-only, normal-order, rate-limited Freshdesk ticket retriever."""
from __future__ import annotations
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
import requests
from app import CLOSED_STATUS, parse_dt

ALLOWED_HOST = "broadriverretail-help.freshdesk.com"
ENDPOINT = "/api/v2/tickets"
MAX_PAGES = 300
PER_PAGE = 100
INCLUDE = "stats"
MIN_DELAY_SECONDS = 2.0
CAUTION_DELAY_SECONDS = 5.0
LOW_DELAY_SECONDS = 60.0
RETRY_FALLBACK_SECONDS = 60.0

@dataclass
class RetrieverConfig:
    updated_since: str
    window_start: datetime
    window_end: datetime
    api_key: str
    max_pages: int = MAX_PAGES
    base_url: str = f"https://{ALLOWED_HOST}"
    min_delay: float = MIN_DELAY_SECONDS
    timeout: float = 30.0
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    progress_callback: Callable[[dict[str, Any]], None] | None = None
    cancel_callback: Callable[[], bool] | None = None

@dataclass
class RetrievalResult:
    success: bool = False
    complete: bool = False
    stop_reason: str = ""
    tickets_unique: list[dict[str, Any]] = field(default_factory=list)
    matches: list[dict[str, Any]] = field(default_factory=list)
    rows_received: int = 0
    unique_ticket_count: int = 0
    duplicate_count: int = 0
    duplicate_payload_updates: int = 0
    pages_completed: int = 0
    http_requests_made: int = 0
    rate_limit_total_last: str | None = None
    rate_limit_remaining_last: str | None = None
    rate_limit_units_reported_sum: int = 0
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float = 0.0
    minimum_request_spacing: float | None = None
    maximum_request_spacing: float | None = None
    rate_limit_slowdowns_triggered: int = 0
    retry_429_count: int = 0
    retry_5xx_timeout_count: int = 0
    next_page_existed_at_cap: bool = False
    status_5_count: int = 0
    empty_tags_count: int = 0
    closed_no_tags_count: int = 0
    valid_closed_at_count: int = 0
    invalid_or_missing_closed_at_count: int = 0
    closed_no_tags_in_date_window_count: int = 0
    def to_dict(self): return asdict(self)

def _num(value):
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None

def _headers(response):
    h = {str(k).lower(): str(v) for k,v in response.headers.items()}
    return {"total": h.get("x-ratelimit-total"), "remaining": h.get("x-ratelimit-remaining"), "used": h.get("x-ratelimit-used-currentrequest"), "retry_after": h.get("retry-after")}

def _has_next(response):
    link = next((str(v) for k,v in response.headers.items() if str(k).lower() == "link"), "")
    return any('rel="next"' in x.lower() or "rel=next" in x.lower() for x in link.split(","))

def _retry_after(value):
    try: return max(0.0, float(value)) if value is not None else RETRY_FALLBACK_SECONDS
    except (TypeError, ValueError): return RETRY_FALLBACK_SECONDS

def _newer(candidate, current):
    a, b = parse_dt(candidate.get("updated_at")), parse_dt(current.get("updated_at"))
    return a is not None and b is not None and a > b

def aggregate_counters(tickets, start, end):
    counters = {
        "status_5_count": 0,
        "empty_tags_count": 0,
        "closed_no_tags_count": 0,
        "valid_closed_at_count": 0,
        "invalid_or_missing_closed_at_count": 0,
        "closed_no_tags_in_date_window_count": 0,
    }
    matches = []
    for ticket in tickets:
        status_5 = ticket.get("status") == CLOSED_STATUS
        empty_tags = isinstance(ticket.get("tags"), list) and len(ticket["tags"]) == 0
        if status_5:
            counters["status_5_count"] += 1
        if empty_tags:
            counters["empty_tags_count"] += 1
        if status_5 and empty_tags:
            counters["closed_no_tags_count"] += 1

        stats = ticket.get("stats")
        closed = parse_dt(stats.get("closed_at")) if isinstance(stats, dict) and isinstance(stats.get("closed_at"), str) else None
        if closed is None:
            counters["invalid_or_missing_closed_at_count"] += 1
        else:
            counters["valid_closed_at_count"] += 1
            if status_5 and empty_tags and start <= closed < end:
                counters["closed_no_tags_in_date_window_count"] += 1
                matches.append(ticket)
    return counters, matches


def filter_closed_tickets(tickets, start, end):
    return aggregate_counters(tickets, start, end)[1]


def safe_summary(result):
    summary = result.to_dict()
    summary.pop("tickets_unique", None)
    summary.pop("matches", None)
    return summary

def validate_config(c):
    if not c.api_key: raise ValueError("api_key is required")
    if not 1 <= c.max_pages <= MAX_PAGES: raise ValueError(f"max_pages must be between 1 and {MAX_PAGES}")
    if parse_dt(c.updated_since) is None or c.window_start.tzinfo is None or c.window_end.tzinfo is None: raise ValueError("timestamps must be timezone-aware ISO-8601")
    if urlparse(c.base_url).hostname != ALLOWED_HOST: raise ValueError("base_url hostname is not allowed")

class TicketRetriever:
    def __init__(self, config):
        validate_config(config); self.c = config; self.r = RetrievalResult(); self.tickets = {}; self.starts=[]; self.last={}
    def cancelled(self): return bool(self.c.cancel_callback and self.c.cancel_callback())
    def emit(self, status, page, waiting=0):
        if self.c.progress_callback:
            self.c.progress_callback({"page":page,"pages_completed":self.r.pages_completed,"http_requests_made":self.r.http_requests_made,"rows_received":self.r.rows_received,"unique_tickets":len(self.tickets),"duplicates_removed":self.r.duplicate_count,"rate_limit_remaining":self.r.rate_limit_remaining_last,"waiting_seconds":waiting,"status":status})
    def wait(self, seconds, page):
        if seconds <= 0: return not self.cancelled()
        self.emit("waiting", page, seconds); remaining=seconds
        while remaining > 0:
            if self.cancelled(): return False
            step=min(remaining, .25); self.c.sleeper(step); remaining-=step
        return True
    def before(self, page):
        if not self.starts: return not self.cancelled()
        delay=max(0.0, self.c.min_delay-(self.c.clock()-self.starts[-1])); rem=_num(self.last.get("remaining"))
        target=60.0 if rem is not None and rem <= 20 else 5.0 if rem is not None and rem <= 50 else self.c.min_delay
        if target > self.c.min_delay: self.r.rate_limit_slowdowns_triggered += 1
        return self.wait(max(delay,target if target>self.c.min_delay else 0),page)
    def url(self,page): return f"{self.c.base_url.rstrip('/')}{ENDPOINT}?"+urlencode({"include":INCLUDE,"per_page":PER_PAGE,"page":page,"updated_since":self.c.updated_since})
    def request(self,page):
        retry429=retry5=False
        while True:
            if not self.before(page): self.r.stop_reason="cancelled"; return None
            self.starts.append(self.c.clock()); self.r.http_requests_made+=1
            try: response=requests.get(self.url(page),auth=(self.c.api_key,"X"),timeout=self.c.timeout,allow_redirects=False,verify=True)
            except (requests.Timeout,requests.ConnectionError) as e:
                if retry5: self.r.stop_reason=f"transport error on page {page}: {type(e).__name__}"; return None
                retry5=True; self.r.retry_5xx_timeout_count+=1; self.wait(1,page); continue
            self.last=_headers(response); self.r.rate_limit_total_last=self.last["total"]; self.r.rate_limit_remaining_last=self.last["remaining"]
            used=_num(self.last["used"])
            if used is not None: self.r.rate_limit_units_reported_sum += int(used)
            if response.status_code == 429:
                if retry429: self.r.stop_reason=f"HTTP 429 repeated on page {page}"; return None
                retry429=True; self.r.retry_429_count+=1; self.wait(_retry_after(self.last["retry_after"]),page); continue
            if 500 <= response.status_code <= 599:
                if retry5: self.r.stop_reason=f"HTTP {response.status_code} repeated on page {page}"; return None
                retry5=True; self.r.retry_5xx_timeout_count+=1; self.wait(1,page); continue
            if response.status_code != 200: self.r.stop_reason=f"HTTP {response.status_code} on page {page}"; return None
            if len(self.starts)>1:
                gaps=[self.starts[i]-self.starts[i-1] for i in range(1,len(self.starts))]; self.r.minimum_request_spacing=min(gaps); self.r.maximum_request_spacing=max(gaps)
            return response
    def run(self):
        started=datetime.now(timezone.utc); self.r.started_at=started.isoformat()
        try:
            for page in range(1,self.c.max_pages+1):
                if self.cancelled(): self.r.stop_reason="cancelled"; break
                response=self.request(page)
                if response is None: break
                try: payload=response.json()
                except (ValueError,TypeError): self.r.stop_reason=f"malformed JSON on page {page}"; break
                if not isinstance(payload,list): self.r.stop_reason=f"non-list JSON on page {page}"; break
                if len(payload)>PER_PAGE: self.r.stop_reason=f"page {page} returned more than {PER_PAGE} rows"; break
                self.r.pages_completed+=1; self.r.rows_received+=len(payload)
                for t in payload:
                    if not isinstance(t,dict) or not isinstance(t.get("id"),int): continue
                    tid=t["id"]
                    if tid in self.tickets:
                        self.r.duplicate_count+=1
                        if _newer(t,self.tickets[tid]): self.tickets[tid]=t; self.r.duplicate_payload_updates+=1
                    else: self.tickets[tid]=t
                self.r.unique_ticket_count=len(self.tickets); self.r.matches=filter_closed_tickets(list(self.tickets.values()),self.c.window_start,self.c.window_end); self.emit("page_complete",page)
                if self.cancelled(): self.r.stop_reason="cancelled"; break
                if not _has_next(response): self.r.complete=True; self.r.stop_reason="no next-page Link — dataset exhausted"; break
            else: self.r.next_page_existed_at_cap=True; self.r.stop_reason=f"hard page ceiling reached ({self.c.max_pages})"
        finally:
            self.r.tickets_unique=list(self.tickets.values())
            self.r.unique_ticket_count=len(self.tickets)
            counters, self.r.matches = aggregate_counters(self.r.tickets_unique, self.c.window_start, self.c.window_end)
            for name, value in counters.items():
                setattr(self.r, name, value)
            self.r.finished_at=datetime.now(timezone.utc).isoformat()
            self.r.elapsed_seconds=(datetime.now(timezone.utc)-started).total_seconds()
            self.r.success=self.r.stop_reason in {"no next-page Link — dataset exhausted",f"hard page ceiling reached ({self.c.max_pages})"}
        return self.r

def retrieve(config): return TicketRetriever(config).run()
def serialize(result): return result.to_dict()
__all__=["TicketRetriever","RetrieverConfig","RetrievalResult","aggregate_counters","filter_closed_tickets","safe_summary","retrieve","serialize","MAX_PAGES","PER_PAGE"]
