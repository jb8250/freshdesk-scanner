"""Pytest configuration for the Freshdesk Scanner.

The autouse `block_network` fixture is the network-blocking test guard: any
test that triggers an unexpected external HTTP request through requests fails
loudly. The scanner's only network path is `requests.get` inside
`paginate_tickets()` (live mode), so patching the requests surface is a simple
and reliable way to prove the app stays offline during tests.
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
def clean_state(monkeypatch):
    """Reset per-test module state: no offline flag, no cached API key, and an
    isolated cache file so tests never read or write the real repo cache."""
    monkeypatch.delenv("FRESHDESK_OFFLINE", raising=False)
    monkeypatch.delenv("FRESHDESK_API_KEY", raising=False)
    monkeypatch.setattr(scanner_app, "FRESHDESK_API_KEY", "")
    monkeypatch.setattr(scanner_app, "CACHE_FILE", "/tmp/fd_test_cache_isolated.json")
    if os.path.exists(scanner_app.CACHE_FILE):
        os.remove(scanner_app.CACHE_FILE)


@pytest.fixture()
def client():
    return scanner_app.app.test_client()
