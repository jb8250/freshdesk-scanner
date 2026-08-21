"""Finite, queue-specific background refresh job support."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

IDLE = "idle"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"


class RefreshJobManager:
    """One finite queue refresh at a time, with lock-protected progress."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._state = self._idle()

    @staticmethod
    def _idle():
        return {"state": IDLE, "progress": None, "message": "", "error": None,
                "started_at": None, "finished_at": None, "days": None, "written": False}

    def status(self):
        with self._lock:
            out = dict(self._state)
            out["progress"] = dict(out["progress"]) if out["progress"] else None
            out["running"] = out["state"] == RUNNING
            out["cancel_requested"] = self._cancel.is_set()
            return out

    def start(self, *, days: int, api_key: str, retrieve: Callable, save: Callable,
               finalize: Callable | None = None, plan: Callable | None = None,
               attempt_started_at: datetime | None = None):
        if not api_key:
            return False, "No Freshdesk API key is available."
        if not self._lock.acquire(blocking=False):
            return False, "A Freshdesk queue refresh is already running."
        try:
            if self._state["state"] == RUNNING:
                return False, "A Freshdesk queue refresh is already running."
            self._cancel.clear()
            self._state = self._idle()
            self._state.update({
                "state": RUNNING, "days": days,
                "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "message": "Starting Freshdesk refresh…",
                "progress": {"state": "starting", "page": 0, "pages_completed": 0,
                              "tickets_received": 0, "request_count": 0,
                              "rate_limit_remaining": None, "elapsed_seconds": 0,
                              "wait_seconds": 0},
                "merge_metrics": None, "retention_metrics": None, "refresh_mode": None,
                "effective_updated_since": None, "cursor_source": None,
            })
            self._thread = threading.Thread(
                target=self._run, args=(days, api_key, retrieve, save, finalize, plan, attempt_started_at),
                name="queue-refresh", daemon=True,
            )
            self._thread.start()
            return True, "Refresh started."
        finally:
            self._lock.release()

    def cancel(self):
        with self._lock:
            running = self._state["state"] == RUNNING
            if running:
                self._state["message"] = "Cancel requested — finishing the current request."
        if running:
            self._cancel.set()
        return running

    def _progress(self, event):
        with self._lock:
            if self._state["state"] != RUNNING:
                return
            self._state["progress"] = dict(event)
            if event.get("state") == "waiting":
                self._state["message"] = "Waiting before the next Freshdesk request…"
            elif event.get("current_stage") == "Checking reviewed updates":
                completed = event.get("conversation_checks_completed", 0)
                total = event.get("conversation_candidates", 0)
                self._state["message"] = f"Checking reviewed updates {completed} / {total}…"
            elif event.get("current_stage") == "Saving cache":
                self._state["message"] = "Saving refreshed ticket cache…"
            elif event.get("state") == "running":
                self._state["message"] = "Refreshing Freshdesk tickets…"

    def _finish(self, state, message, error=None, written=False):
        with self._lock:
            self._state.update({"state": state, "message": message, "error": error,
                                "written": written,
                                "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
        self._cancel.clear()

    def wait(self, timeout=30):
        """Block until the running job reaches a terminal state (tests only)."""
        deadline = time.time() + timeout
        while True:
            if self._state["state"] != RUNNING:
                return
            time.sleep(0.01)
            if time.time() > deadline:
                return

    def _run(self, days, api_key, retrieve, save, finalize=None, plan=None, attempt_started_at=None):
        # Capture one UTC whole-second attempt start before the first request.
        refresh_started_at = (attempt_started_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        try:
            refresh_plan = plan(days, refresh_started_at) if plan else {
                "refresh_mode": "baseline", "cursor_source": "days_baseline",
                "effective_updated_since": None, "durable_refresh_started_at": refresh_started_at,
            }
            with self._lock:
                self._state.update({key: refresh_plan[key] for key in (
                    "refresh_mode", "cursor_source", "effective_updated_since")})
            tickets = retrieve(days=days, effective_since=refresh_plan["effective_updated_since"], api_key=api_key,
                                progress_callback=self._progress,
                                cancel_callback=self._cancel.is_set)
            if self._cancel.is_set():
                self._finish(CANCELLED, "Refresh cancelled. The cached results were left unchanged.")
                return
            if finalize:
                finalized = finalize(
                    tickets, progress_callback=self._progress,
                    cancel_callback=self._cancel.is_set,
                    attempt_started_at=refresh_started_at,
                )
                if len(finalized) == 4:
                    tickets, finalize_after_save, merge_metrics, retention_metrics = finalized
                    with self._lock:
                        self._state["merge_metrics"] = {
                            "received_count": merge_metrics.incoming_count,
                            **vars(merge_metrics),
                        }
                        self._state["retention_metrics"] = vars(retention_metrics)
                elif len(finalized) == 3:
                    tickets, finalize_after_save, merge_metrics = finalized
                    with self._lock:
                        self._state["merge_metrics"] = {
                            "received_count": merge_metrics.incoming_count,
                            **vars(merge_metrics),
                        }
                else:
                    tickets, finalize_after_save = finalized
                if self._cancel.is_set():
                    self._finish(CANCELLED, "Refresh cancelled. The cached results were left unchanged.")
                    return
            else:
                finalize_after_save = None
            self._progress({"current_stage": "Saving cache", "state": "running"})
            # Completion is captured after successful retrieval/finalization and
            # immediately before the atomic cache commit.
            save(tickets, days=days,
                 refresh_started_at=refresh_plan["durable_refresh_started_at"],
                 refresh_finished_at=datetime.now(timezone.utc),
                 refresh_mode=refresh_plan["refresh_mode"],
                 effective_updated_since=refresh_plan["effective_updated_since"])
            if finalize_after_save:
                finalize_after_save()
            self._finish(SUCCEEDED, f"Refresh complete — {len(tickets)} tickets cached.", written=True)
        except Exception as exc:
            if self._cancel.is_set():
                self._finish(CANCELLED, "Refresh cancelled. The cached results were left unchanged.")
            else:
                self._finish(FAILED, "Refresh failed; the cache was left unchanged.", error=f"{type(exc).__name__}: {exc}")


JOB = RefreshJobManager()
