"""Live Closed-dashboard refresh support (Prompt 24).

This module owns everything the /closed page needs to run an *explicit*,
operator-triggered live refresh:

  * the separate closed cache file (``cache/closed_tickets.json``) with a
    strict field allowlist — no ticket body, requester, email, or custom
    fields are ever persisted,
  * atomic cache writes (temp file + ``os.replace``) that only happen for a
    retrieval that is BOTH ``success`` and ``complete`` — never for a
    cancelled, failed, or page-capped run,
  * a single-slot background job manager (one daemon thread at a time,
    guarded by a lock plus a cancel event) that drives
    ``closed_retriever.retrieve`` through its existing progress and cancel
    callbacks,
  * the UTC window math shared by the page and the refresh job.

``closed_retriever`` imports ``app``, and ``app`` imports this module, so the
retriever is imported lazily inside the job body to keep imports acyclic.

Nothing here reads the API key. The key is supplied by the caller (the route),
which is the only place allowed to touch ``load_api_key`` — and only in live
mode.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
#: Separate from the queue cache on purpose: /closed must never read or write
#: cache/tickets.json, and the queue must never see closed rows.
CLOSED_CACHE_FILE = os.path.join(CACHE_DIR, "closed_tickets.json")

#: The ONLY ticket fields persisted to the closed cache.
ALLOWED_TICKET_FIELDS = ("id", "subject", "status", "tags", "created_at", "updated_at")
#: The only nested stats key persisted.
ALLOWED_STATS_FIELDS = ("closed_at",)

#: Safe (non-ticket) metadata persisted alongside the rows.
CACHE_SCHEMA_VERSION = 1

#: Grace applied to updated_since so a ticket updated exactly on the boundary
#: is not missed because of clock skew or second-level truncation.
UPDATED_SINCE_GRACE_SECONDS = 5


# ---------------------------------------------------------------------------
# Window math
# ---------------------------------------------------------------------------

def utc_window(days: int, now: datetime) -> tuple[datetime, datetime]:
    """Map the existing "last N days" control onto a half-open UTC window.

    Returns ``(start, end)`` where ``start`` is UTC midnight of the day that is
    ``days - 1`` days before *now*'s UTC date, and ``end`` is UTC midnight of
    the day AFTER *now*'s UTC date. Membership is ``start <= closed_at < end``
    so the final day is fully included without double-counting midnight.
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError("days must be a positive integer")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(timezone.utc).date()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=days - 1)
    end = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


def updated_since_for(start: datetime) -> str:
    """``updated_since`` = window start minus a small fixed grace, ISO-8601 UTC."""
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    value = start.astimezone(timezone.utc) - timedelta(seconds=UPDATED_SINCE_GRACE_SECONDS)
    return value.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Cache shaping and IO
# ---------------------------------------------------------------------------

def sanitize_ticket(ticket: Any) -> dict | None:
    """Reduce a raw API ticket to the allowlisted fields, or None if unusable."""
    if not isinstance(ticket, dict) or not isinstance(ticket.get("id"), int) or isinstance(ticket.get("id"), bool):
        return None
    row: dict[str, Any] = {}
    for field in ALLOWED_TICKET_FIELDS:
        if field not in ticket:
            continue
        value = ticket[field]
        if field == "tags":
            if isinstance(value, list):
                row["tags"] = [t for t in value if isinstance(t, str)]
            continue
        if field == "id" or field == "status":
            if isinstance(value, int) and not isinstance(value, bool):
                row[field] = value
            continue
        if isinstance(value, str):
            row[field] = value
    stats = ticket.get("stats")
    if isinstance(stats, dict):
        safe_stats = {k: stats[k] for k in ALLOWED_STATS_FIELDS
                      if isinstance(stats.get(k), str)}
        if safe_stats:
            row["stats"] = safe_stats
    return row if "id" in row else None


def sanitize_tickets(tickets) -> list[dict]:
    rows = []
    for t in tickets or []:
        row = sanitize_ticket(t)
        if row is not None:
            rows.append(row)
    return rows


def build_cache_payload(tickets, *, days, start, end, summary, fetched_at=None) -> dict:
    """Build the on-disk cache blob. Only safe metadata is included."""
    summary = summary or {}
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "fetched_at": (fetched_at or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "days": days,
        "window_start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "complete": True,
        "ticket_count": len(tickets),
        "coverage": {
            "pages_completed": summary.get("pages_completed"),
            "rows_received": summary.get("rows_received"),
            "unique_ticket_count": summary.get("unique_ticket_count"),
            "duplicate_count": summary.get("duplicate_count"),
            "next_page_existed_at_cap": bool(summary.get("next_page_existed_at_cap")),
            "invalid_or_missing_closed_at_count": summary.get("invalid_or_missing_closed_at_count"),
            "closed_no_tags_in_date_window_count": summary.get("closed_no_tags_in_date_window_count"),
        },
        "tickets": tickets,
    }


def cache_path() -> str:
    return CLOSED_CACHE_FILE


def write_cache_atomic(payload: dict, path: str | None = None) -> str:
    """Serialize first, then temp-file + ``os.replace`` in the same directory.

    A partially written or malformed payload can never replace a good cache:
    JSON serialization happens before the temp file is created, and the rename
    is atomic on POSIX.
    """
    target = path or cache_path()
    blob = json.dumps(payload, indent=2, sort_keys=True)
    directory = os.path.dirname(os.path.abspath(target)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".closed_tickets.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def load_cache(path: str | None = None) -> dict | None:
    """Read the closed cache. Missing/malformed/wrong-shape reads return None."""
    target = path or cache_path()
    try:
        with open(target, "r") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or not isinstance(blob.get("tickets"), list):
        return None
    blob["tickets"] = [t for t in blob["tickets"] if isinstance(t, dict) and isinstance(t.get("id"), int)]
    return blob


def cache_closed_at(ticket) -> str | None:
    stats = ticket.get("stats") if isinstance(ticket, dict) else None
    value = stats.get("closed_at") if isinstance(stats, dict) else None
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Single-slot background refresh job
# ---------------------------------------------------------------------------

IDLE = "idle"
RUNNING = "running"
SUCCESS = "success"
CANCELLED = "cancelled"
FAILED = "failed"

_TERMINAL = (SUCCESS, CANCELLED, FAILED)


class RefreshJobManager:
    """At most ONE refresh thread at a time, ever.

    ``start`` is refused while a job is running. The worker is a daemon thread
    so it can never block interpreter shutdown; cancellation is cooperative via
    an ``threading.Event`` handed to the retriever's ``cancel_callback``.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "state": IDLE,
            "progress": None,
            "message": "",
            "error": None,
            "started_at": None,
            "finished_at": None,
            "days": None,
            "written": False,
            "summary": None,
        }

    # -- introspection -----------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._state)
            snapshot["progress"] = dict(snapshot["progress"]) if snapshot["progress"] else None
            snapshot["running"] = snapshot["state"] == RUNNING
            snapshot["cancel_requested"] = self._cancel.is_set()
            return snapshot

    def is_running(self) -> bool:
        with self._lock:
            return self._state["state"] == RUNNING

    def reset(self) -> None:
        """Test/teardown hook: forget any terminal state and clear the event."""
        with self._lock:
            self._thread = None
            self._state = self._idle_state()
        self._cancel.clear()

    # -- control -----------------------------------------------------------
    def cancel(self) -> bool:
        """Request cancellation. Returns True when a running job was signalled."""
        with self._lock:
            running = self._state["state"] == RUNNING
            if running:
                self._state["message"] = "Cancel requested — finishing current page."
        if running:
            self._cancel.set()
        return running

    def start(self, *, days: int, api_key: str, now: datetime,
              cache_file: str | None = None,
              retrieve: Callable | None = None,
              config_factory: Callable | None = None,
              max_pages: int | None = None,
              join: bool = False,
              **retriever_kwargs) -> tuple[bool, str]:
        """Start a refresh. Returns ``(started, message)``.

        ``api_key`` is passed in by the caller; this module never loads it.
        ``retrieve``/``config_factory`` exist for tests — production leaves them
        None so the real ``closed_retriever`` (and its untouched production
        ``MAX_PAGES``) is used.
        """
        if not api_key:
            return False, "No Freshdesk API key is available."
        start_dt, end_dt = utc_window(days, now)
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False, "A refresh is already running."
            self._thread = None
            self._state = self._idle_state()
            self._state.update({
                "state": RUNNING,
                "days": days,
                "message": "Starting refresh…",
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "progress": {"status": "starting", "page": 0, "pages_completed": 0,
                             "unique_tickets": 0, "rows_received": 0,
                             "waiting_seconds": 0, "rate_limit_remaining": None},
            })
        self._cancel.clear()
        target = cache_file or cache_path()

        thread = threading.Thread(
            target=self._run,
            kwargs={"days": days, "api_key": api_key, "start_dt": start_dt, "end_dt": end_dt,
                    "cache_file": target, "retrieve": retrieve,
                    "config_factory": config_factory, "max_pages": max_pages,
                    "retriever_kwargs": retriever_kwargs},
            name="closed-refresh", daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()
        if join:
            thread.join(timeout=30)
        return True, "Refresh started."

    # -- worker ------------------------------------------------------------
    def _progress(self, event: dict) -> None:
        with self._lock:
            if self._state["state"] != RUNNING:
                return
            self._state["progress"] = dict(event)
            page = event.get("page")
            status = event.get("status")
            if status == "waiting":
                self._state["message"] = f"Rate-limit pause ({event.get('waiting_seconds', 0)}s) before page {page}…"
            else:
                self._state["message"] = (
                    f"Page {event.get('pages_completed', 0)} complete · "
                    f"{event.get('unique_tickets', 0)} unique tickets"
                )

    def _finish(self, state: str, message: str, *, error=None, summary=None, written=False) -> None:
        with self._lock:
            self._state.update({
                "state": state,
                "message": message,
                "error": error,
                "summary": summary,
                "written": written,
                "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
        self._cancel.clear()

    def _run(self, *, days, api_key, start_dt, end_dt, cache_file, retrieve,
             config_factory, max_pages, retriever_kwargs) -> None:
        try:
            import closed_retriever  # lazy: closed_retriever imports app

            retrieve_fn = retrieve or closed_retriever.retrieve
            make_config = config_factory or closed_retriever.RetrieverConfig
            kwargs: dict[str, Any] = dict(
                updated_since=updated_since_for(start_dt),
                window_start=start_dt,
                window_end=end_dt,
                api_key=api_key,
                progress_callback=self._progress,
                cancel_callback=self._cancel.is_set,
            )
            # Production leaves max_pages alone so the retriever's own MAX_PAGES
            # ceiling stays authoritative and untouched.
            if max_pages is not None:
                kwargs["max_pages"] = max_pages
            kwargs.update(retriever_kwargs)
            result = retrieve_fn(make_config(**kwargs))

            summary = closed_retriever.safe_summary(result) if hasattr(closed_retriever, "safe_summary") else {}
            if self._cancel.is_set() or getattr(result, "stop_reason", "") == "cancelled":
                self._finish(CANCELLED, "Refresh cancelled. The cached results were left unchanged.",
                             summary=summary)
                return
            if not (getattr(result, "success", False) and getattr(result, "complete", False)):
                reason = getattr(result, "stop_reason", "") or "incomplete retrieval"
                self._finish(FAILED, "Refresh did not complete; the cache was left unchanged.",
                             error=str(reason), summary=summary)
                return

            rows = sanitize_tickets(getattr(result, "matches", []) or [])
            payload = build_cache_payload(rows, days=days, start=start_dt, end=end_dt, summary=summary)
            write_cache_atomic(payload, cache_file)
            self._finish(SUCCESS, f"Refresh complete — {len(rows)} closed tickets cached.",
                         summary=summary, written=True)
        except Exception as exc:  # never let a worker thread die silently
            self._finish(FAILED, "Refresh failed; the cache was left unchanged.",
                         error=f"{type(exc).__name__}: {exc}")


#: Process-wide single job slot.
JOB = RefreshJobManager()

__all__ = [
    "CLOSED_CACHE_FILE", "ALLOWED_TICKET_FIELDS", "ALLOWED_STATS_FIELDS",
    "utc_window", "updated_since_for", "sanitize_ticket", "sanitize_tickets",
    "build_cache_payload", "write_cache_atomic", "load_cache", "cache_path",
    "cache_closed_at", "RefreshJobManager", "JOB",
    "IDLE", "RUNNING", "SUCCESS", "CANCELLED", "FAILED",
]
