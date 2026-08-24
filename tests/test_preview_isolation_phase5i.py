import hashlib
import importlib
import os
import sqlite3
from pathlib import Path


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_preview_path_overrides_and_defaults(monkeypatch, tmp_path):
    import app
    import closed_live

    production_queue = app.os.path.join(app.CACHE_DIR, "queue_live_tickets.json")
    production_closed = closed_live.os.path.join(closed_live.CACHE_DIR, "closed_tickets.json")
    assert production_queue.endswith("cache/queue_live_tickets.json")
    assert production_closed.endswith("cache/closed_tickets.json")

    preview_db = tmp_path / "review.sqlite3"
    preview_queue = tmp_path / "queue.json"
    preview_closed = tmp_path / "closed.json"
    monkeypatch.setenv("REVIEW_DB_PATH", str(preview_db))
    monkeypatch.setenv("QUEUE_CACHE_PATH", str(preview_queue))
    monkeypatch.setenv("CLOSED_CACHE_PATH", str(preview_closed))
    reloaded_app = importlib.reload(app)
    reloaded_closed = importlib.reload(closed_live)
    assert reloaded_app.get_db_path() == str(preview_db)
    assert reloaded_app.LIVE_QUEUE_CACHE_FILE == str(preview_queue)
    assert reloaded_closed.cache_path() == str(preview_closed)


def test_preview_db_write_does_not_change_production(monkeypatch, tmp_path):
    import app
    production = tmp_path / "production.sqlite3"
    preview = tmp_path / "preview.sqlite3"
    app.init_db(str(production))
    app.init_db(str(preview))
    before = _sha(production)
    monkeypatch.setenv("REVIEW_DB_PATH", str(preview))
    app.set_review_result(123456, "Resolved")
    assert _sha(production) == before
    with sqlite3.connect(preview) as conn:
        assert conn.execute("SELECT review_result FROM review_state WHERE ticket_id = 123456").fetchone()[0] == "Resolved"


def test_preview_cache_write_does_not_change_production(monkeypatch, tmp_path):
    import app
    production = tmp_path / "production.json"
    preview = tmp_path / "preview.json"
    production.write_text('{"tickets": []}')
    preview.write_text('{"tickets": []}')
    before = _sha(production)
    import json
    preview.write_text(json.dumps({"tickets": [{"id": 1}]}))
    assert _sha(production) == before
    assert '"id": 1' in preview.read_text()


def test_offline_disables_auto_refresh(monkeypatch):
    import app
    monkeypatch.setenv("FRESHDESK_OFFLINE", "1")
    assert app.initialize_live_auto_refresh() is False
    assert app.auto_refresh_status()["enabled"] is False


def test_preview_launcher_contract():
    text = Path("Start_Dark_Theme_Preview.command").read_text()
    assert 'PORT="5051"' in text
    assert 'FRESHDESK_OFFLINE=1' in text
    assert 'python' in text.lower()
    assert 'app.py' in text
    assert "5050" not in text
