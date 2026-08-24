"""In-memory monotonic scheduler for the live queue's automatic refresh."""
from __future__ import annotations

import threading
import time
from typing import Callable

AUTO_REFRESH_INTERVAL_SECONDS = 30 * 60


class AutoRefreshScheduler:
    """Run one callback per interval without queuing or retrying work.

    The callback is responsible for deciding whether the queue job is busy and
    for returning a short result string suitable for read-only status display.
    """

    def __init__(self, callback: Callable[[], str | None], *, interval_seconds: float = AUTO_REFRESH_INTERVAL_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic):
        self._callback = callback
        self.interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._wake_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next_deadline: float | None = None
        self._last_attempt_at: float | None = None
        self._last_result: str | None = None

    def start(self) -> bool:
        """Start once, arming the first attempt for one full interval away."""
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._next_deadline = self._monotonic() + self.interval_seconds
            self._thread = threading.Thread(target=self._run, name="queue-auto-refresh", daemon=True)
            self._thread.start()
            return True

    def reset(self) -> None:
        """Postpone the next automatic attempt by one full interval."""
        with self._state_lock:
            self._next_deadline = self._monotonic() + self.interval_seconds
        self._wake_event.set()

    def status(self, *, enabled: bool = True) -> dict:
        with self._state_lock:
            deadline = self._next_deadline
            seconds = None if deadline is None else max(0, int(deadline - self._monotonic()))
            return {
                "enabled": enabled,
                "interval_seconds": self.interval_seconds,
                "seconds_until_next": seconds,
                "last_attempt_at": self._last_attempt_at,
                "last_result": self._last_result,
            }

    def _run(self) -> None:
        while True:
            with self._state_lock:
                deadline = self._next_deadline
            if deadline is None:
                return
            remaining = max(0, deadline - self._monotonic())
            if self._wake_event.wait(remaining):
                self._wake_event.clear()
                continue
            # Rearm before invoking the callback: failures, cancellation, and
            # busy skips always retain the normal future interval.
            with self._state_lock:
                now = self._monotonic()
                if self._next_deadline != deadline:
                    continue
                self._last_attempt_at = now
                self._next_deadline = now + self.interval_seconds
            try:
                result = self._callback()
            except Exception:
                result = "failed"
            with self._state_lock:
                self._last_result = result or "started"
