"""Pytest configuration for the Freshdesk Scanner.

The autouse `block_network` fixture is the network-blocking test guard: any
test that triggers an unexpected external HTTP request through requests fails
loudly. The scanner's only network path is `requests.get` inside
`paginate_tickets()` (live mode), so patching the requests surface is a simple
and reliable way to prove the app stays offline during tests.

The autouse `clean_state` fixture isolates every test from the real repo
state: offline flag, API key, cache file, and the review-state SQLite database
(REVIEW_DB_PATH) all point at per-test temporary locations, so tests can never
read or write the operator's real cache or real review database.
"""
import os

import pytest
import requests

import app as scanner_app


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Fail any test that attempts an unexpected external HTTP request."""

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "NETWORK BLOCKED: an unexpected external HTTP request was attempted."
        )

    monkeypatch.setattr(requests, "get", _blocked)
    monkeypatch.setattr(requests, "post", _blocked)
    monkeypatch.setattr(requests, "put", _blocked)
    monkeypatch.setattr(requests, "patch", _blocked)
    monkeypatch.setattr(requests, "delete", _blocked)
    monkeypatch.setattr(requests, "request", _blocked)
    monkeypatch.setattr("requests.sessions.Session.request", _blocked)
    monkeypatch.setattr("requests.sessions.Session.send", _blocked)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Reset per-test module state: no offline flag, no cached API key, an
    isolated cache file, and an isolated review-state SQLite database so tests
    never read or write the real repo cache or the real data/ database."""
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    monkeypatch.delenv("REVIEW_DB_PATH", raising=False)
    monkeypatch.setattr(scanner_app, "FRESHDESK_API_KEY", "")
    # Tests must never consult an operator-provisioned external key file. This
    # keeps missing-key behavior deterministic and prevents credential reads.
    monkeypatch.setattr(scanner_app, "FRESHDESK_KEY_FILE", str(tmp_path / "absent_freshdesk_api_key"))
    monkeypatch.setattr(scanner_app, "CACHE_FILE", str(tmp_path / "fd_test_cache_isolated.json"))
    monkeypatch.setattr(scanner_app.closed_live, "CLOSED_CACHE_FILE", str(tmp_path / "closed_test_cache_isolated.json"))
    monkeypatch.setattr(scanner_app.closed_live, "JOB", scanner_app.closed_live.RefreshJobManager())
    db_path = str(tmp_path / "review_state_test.sqlite3")
    monkeypatch.setenv("REVIEW_DB_PATH", db_path)
    scanner_app.init_db(db_path)
    yield db_path


@pytest.fixture
def fixed_clock(monkeypatch):
    """Pin the app's clock to the fixture reference time (2026-08-05T12:00:00Z)
    so days-back windows and overdue math are deterministic. Fixture
    updated_at values are anchored to this exact instant."""
    from datetime import datetime, timezone

    ref = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(scanner_app, "now_utc", lambda: ref)
    return ref


@pytest.fixture
def client(fixed_clock, monkeypatch):
    """Offline Flask test client with a pinned clock and isolated DB."""
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    return scanner_app.app.test_client()


def offline_env(monkeypatch):
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
