"""Offline tests for the Prompt 22 normal-order retriever."""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests

import closed_retriever as cr

START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 4, tzinfo=timezone.utc)

class Resp:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = dict(headers or {})
    def json(self): return self._payload

def ticket(tid, status=5, tags=None, closed="2026-08-02T12:00:00Z", updated="2026-08-02T13:00:00Z", stats=True):
    return {"id": tid, "status": status, "tags": [] if tags is None else tags,
            "updated_at": updated, "stats": {"closed_at": closed} if stats else None,
            "subject": "SECRET", "description": "SECRET BODY"}

def config(**kw):
    values = {"updated_since": "2026-07-31T23:59:55Z", "window_start": START, "window_end": END,
              "api_key": "test-key", "sleeper": lambda _: None}
    values.update(kw)
    return cr.RetrieverConfig(**values)

def test_request_shape_and_normal_order(monkeypatch):
    calls=[]
    def get(url, **kwargs): calls.append((url, kwargs)); return Resp([])
    monkeypatch.setattr(requests, "get", get)
    result=cr.retrieve(config())
    query=parse_qs(urlparse(calls[0][0]).query)
    assert urlparse(calls[0][0]).hostname == cr.ALLOWED_HOST
    assert query == {"include":["stats"], "per_page":["100"], "page":["1"], "updated_since":["2026-07-31T23:59:55Z"]}
    assert "order_by" not in query and "order_type" not in query
    assert calls[0][1]["verify"] is True and calls[0][1]["allow_redirects"] is False
    assert result.http_requests_made == result.pages_completed == 1

def test_sequential_link_pagination_and_partial_stop(monkeypatch):
    calls=[]
    pages=[[ticket(1)], [ticket(2)]]
    def get(url, **kw):
        calls.append(url); page=int(parse_qs(urlparse(url).query)["page"][0])
        headers={"Link": '<https://broadriverretail-help.freshdesk.com/api/v2/tickets?page=2>; rel="next"'} if page == 1 else {}
        return Resp(pages[page-1], headers=headers)
    monkeypatch.setattr(requests,"get",get)
    result=cr.retrieve(config())
    assert [parse_qs(urlparse(u).query)["page"][0] for u in calls] == ["1","2"]
    assert result.complete and result.rows_received == result.unique_ticket_count == 2

def test_max_page_cap_and_no_parallel(monkeypatch):
    active=0; max_active=0; calls=[]
    def get(url, **kw):
        nonlocal active,max_active
        active+=1; max_active=max(max_active,active); calls.append(url); active-=1
        return Resp([ticket(len(calls))], headers={"Link":"<next>; rel=\"next\""})
    monkeypatch.setattr(requests,"get",get)
    result=cr.retrieve(config(max_pages=3))
    assert result.pages_completed == result.http_requests_made == 3
    assert result.next_page_existed_at_cap and max_active == 1

def test_dedup_keeps_newer_and_counts_updates(monkeypatch):
    pages=[[ticket(1,updated="2026-08-02T12:00:00Z"),ticket(2)], [ticket(1,updated="2026-08-02T14:00:00Z"),ticket(2)]]
    n=[0]
    def get(url,**kw):
        n[0]+=1; return Resp(pages[n[0]-1], headers={"Link":"<next>; rel=\"next\""} if n[0]==1 else {})
    monkeypatch.setattr(requests,"get",get)
    r=cr.retrieve(config())
    assert r.rows_received == 4 and r.unique_ticket_count == 2 and r.duplicate_count == 2 and r.duplicate_payload_updates == 1
    assert next(t for t in r.tickets_unique if t["id"]==1)["updated_at"].endswith("14:00:00Z")
    assert len({t["id"] for t in r.tickets_unique}) == len(r.tickets_unique)

def test_filter_strict_local_criteria():
    items=[ticket(1),ticket(2,status=4),ticket(3,tags=["x"]),ticket(4,stats=False),ticket(5,closed="bad"),ticket(6,closed="2026-08-04T00:00:00Z"),ticket(7,closed="2026-08-01T00:00:00Z")]
    assert [x["id"] for x in cr.filter_closed_tickets(items,START,END)] == [1,7]

def test_progress_is_safe_and_cancellation_stops_before_next(monkeypatch):
    events=[]; calls=[]; cancelled=[False]
    def get(url,**kw): calls.append(url); cancelled[0]=True; return Resp([ticket(1)],headers={"Link":"<next>; rel=\"next\""})
    monkeypatch.setattr(requests,"get",get)
    r=cr.retrieve(config(progress_callback=events.append,cancel_callback=lambda:cancelled[0]))
    assert len(calls)==1 and r.stop_reason=="cancelled"
    assert events and set(events[-1]) == {"page","pages_completed","http_requests_made","rows_received","unique_tickets","duplicates_removed","matching_closed_no_tag_tickets","rate_limit_remaining","waiting_seconds","status"}
    assert "SECRET" not in str(events)

def test_two_second_spacing_with_injected_clock(monkeypatch):
    now=[0.0]; starts=[]
    def sleep(seconds): now[0]+=seconds
    def clock(): return now[0]
    def get(url,**kw): starts.append(now[0]); page=int(parse_qs(urlparse(url).query)["page"][0]); return Resp([ticket(page)],headers={"Link":"<next>; rel=\"next\""} if page<2 else {})
    monkeypatch.setattr(requests,"get",get)
    r=cr.retrieve(config(clock=clock,sleeper=sleep))
    assert starts[1]-starts[0] >= 2.0 and r.minimum_request_spacing >= 2.0

def test_caution_and_low_remaining_delays(monkeypatch):
    now=[0.0]; waits=[]
    def sleep(s): waits.append(s); now[0]+=s
    def get(url,**kw):
        page=int(parse_qs(urlparse(url).query)["page"][0]); rem="40" if page==1 else "10"
        return Resp([ticket(page)],headers={"Link":"<next>; rel=\"next\"","X-RateLimit-Remaining":rem})
    monkeypatch.setattr(requests,"get",get)
    r=cr.retrieve(config(clock=lambda:now[0],sleeper=sleep,max_pages=3))
    assert sum(waits) >= 65.0 and r.rate_limit_slowdowns_triggered >= 2

def test_429_retry_after_once(monkeypatch):
    waits=[]; responses=[Resp([],429,{"Retry-After":"7"}),Resp([])]
    monkeypatch.setattr(requests,"get",lambda *a,**k: responses.pop(0))
    r=cr.retrieve(config(sleeper=waits.append))
    assert r.http_requests_made == 2 and r.retry_429_count == 1 and sum(waits) >= 7.0 and r.complete

def test_second_429_stops_and_counts(monkeypatch):
    monkeypatch.setattr(requests,"get",lambda *a,**k: Resp([],429))
    r=cr.retrieve(config(sleeper=lambda _:None))
    assert r.http_requests_made == 2 and r.retry_429_count == 1 and "repeated" in r.stop_reason

@pytest.mark.parametrize("status",[400,401,403,404])
def test_4xx_stops_without_retry(monkeypatch,status):
    monkeypatch.setattr(requests,"get",lambda *a,**k: Resp([],status))
    r=cr.retrieve(config())
    assert r.http_requests_made == 1 and f"HTTP {status}" in r.stop_reason

def test_5xx_bounded_retry(monkeypatch):
    n=[0]
    def get(*a,**k): n[0]+=1; return Resp([],500) if n[0]==1 else Resp([])
    monkeypatch.setattr(requests,"get",get)
    r=cr.retrieve(config())
    assert r.http_requests_made == 2 and r.retry_5xx_timeout_count == 1 and r.complete

def test_timeout_bounded_retry(monkeypatch):
    n=[0]
    def get(*a,**k):
        n[0]+=1
        if n[0]==1: raise requests.Timeout()
        return Resp([])
    monkeypatch.setattr(requests,"get",get)
    r=cr.retrieve(config())
    assert r.http_requests_made == 2 and r.complete

def test_malformed_and_non_list_stop(monkeypatch):
    class Bad:
        status_code=200; headers={}
        def json(self): raise ValueError
    monkeypatch.setattr(requests,"get",lambda *a,**k: Bad())
    assert "malformed JSON" in cr.retrieve(config()).stop_reason
    monkeypatch.setattr(requests,"get",lambda *a,**k: Resp({}))
    assert "non-list JSON" in cr.retrieve(config()).stop_reason

def test_over_100_rows_safe_stop(monkeypatch):
    monkeypatch.setattr(requests,"get",lambda *a,**k: Resp([ticket(i) for i in range(101)]))
    r=cr.retrieve(config()); assert r.http_requests_made == 1 and "more than 100" in r.stop_reason

def test_config_guards_and_result_serializable():
    with pytest.raises(ValueError): cr.retrieve(config(max_pages=301))
    assert isinstance(cr.serialize(cr.RetrievalResult()),dict)
