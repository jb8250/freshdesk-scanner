#!/usr/bin/env python3
"""Guarded Prompt 22 live validation; dry-run unless --execute is explicit."""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import closed_retriever as retriever

LIVE_TEST_MAX_PAGES = 100
UPDATED_SINCE = "2026-07-31T23:59:55Z"
WINDOW_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 4, tzinfo=timezone.utc)

def credential():
    value = os.environ.get("FRESHDESK_API_KEY", "")
    if value: return value
    candidates = [Path.home() / ".config" / "furtouch" / "freshdesk_api_key", Path(__file__).resolve().parents[1] / "freshdesk_api_key.txt", Path.home() / ".freshdesk_api_key"]
    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if value: return value
    return None

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    base = {"probe":"closed_retriever_validation", "executed":args.execute, "max_pages":LIVE_TEST_MAX_PAGES, "updated_since":UPDATED_SINCE, "window_start":WINDOW_START.isoformat(), "window_end":WINDOW_END.isoformat(), "order_by":None, "order_type":None}
    if not args.execute:
        base.update({"stop_reason":"dry-run — no HTTP request made", "success":None})
        print(json.dumps(base, indent=2 if args.pretty else None)); return 0
    key = credential()
    if not key:
        base.update({"stop_reason":"credential unavailable", "success":False})
        print(json.dumps(base, indent=2 if args.pretty else None)); return 2
    events=[]
    def progress(event):
        events.append(event)
        if event["status"] in {"page_complete", "waiting"}:
            print(
                "progress"
                f" status={event['status']}"
                f" page={event['page']}"
                f" rows_received={event['rows_received']}"
                f" unique_tickets={event['unique_tickets']}"
                f" duplicates_removed={event['duplicates_removed']}"
                f" rate_limit_remaining={event['rate_limit_remaining']}"
                f" http_requests_made={event['http_requests_made']}"
                f" waiting_seconds={event['waiting_seconds']}",
                file=sys.stderr,
                flush=True,
            )
    result = retriever.retrieve(retriever.RetrieverConfig(updated_since=UPDATED_SINCE, window_start=WINDOW_START, window_end=WINDOW_END, api_key=key, max_pages=LIVE_TEST_MAX_PAGES, progress_callback=progress))
    base.update(retriever.safe_summary(result)); base["progress_events"] = events
    print(json.dumps(base, indent=2 if args.pretty else None, default=str))
    return 0 if result.success else 3

if __name__ == "__main__": raise SystemExit(main())
