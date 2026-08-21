import json
import sqlite3
from pathlib import Path

import review_backups


def test_verified_backup_preserves_both_tables_and_metadata(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE review_state (ticket_id INTEGER PRIMARY KEY, review_result TEXT);
    CREATE TABLE closed_review_state (ticket_id INTEGER PRIMARY KEY, review_result TEXT);
    INSERT INTO review_state VALUES (1, 'Resolved'), (2, 'Unreviewed');
    INSERT INTO closed_review_state VALUES (3, 'Needs Follow-Up');
    """)
    conn.commit(); conn.close()
    backup_dir = tmp_path / "backups"
    monkeypatch.setenv("REVIEW_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("REVIEW_DB_PATH", str(db))
    result = review_backups.create_backup(db, "Review change / unsafe!@#")
    assert result.exists() and result.parent == backup_dir
    assert "review-change-unsafe" in result.name
    assert result.with_suffix(".json").exists()
    check = sqlite3.connect(result)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert check.execute("SELECT COUNT(*) FROM review_state").fetchone()[0] == 2
    assert check.execute("SELECT COUNT(*) FROM closed_review_state").fetchone()[0] == 1
    check.close()
    metadata = json.loads(result.with_suffix(".json").read_text())
    assert metadata["backup_sha256"] == review_backups._sha(result)
    assert metadata["review_state_counts"] == {"Resolved": 1, "Unreviewed": 1}
    assert metadata["closed_review_state_total"] == 1


def test_retention_only_removes_automatic_pairs(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE review_state (ticket_id INTEGER PRIMARY KEY, review_result TEXT); CREATE TABLE closed_review_state (ticket_id INTEGER PRIMARY KEY, review_result TEXT);")
    c.commit(); c.close()
    monkeypatch.setenv("REVIEW_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("REVIEW_BACKUP_KEEP", "3")
    for n in range(5):
        review_backups.create_backup(db, f"reason-{n}")
    automatic = list((tmp_path / "backups").glob("Review_state-auto-*.sqlite3"))
    assert len(automatic) == 3
    assert len(list((tmp_path / "backups").glob("Review_state-auto-*.json"))) == 3
    manual = tmp_path / "backups" / "Review_state-restored-important.sqlite3"
    manual.write_bytes(b"manual")
    review_backups.prune()
    assert manual.exists()


def test_bad_keep_is_safe_default(monkeypatch):
    monkeypatch.setenv("REVIEW_BACKUP_KEEP", "not-a-number")
    assert review_backups.keep_count() == review_backups.DEFAULT_REVIEW_BACKUP_KEEP
    monkeypatch.setenv("REVIEW_BACKUP_KEEP", "0")
    assert review_backups.keep_count() == review_backups.DEFAULT_REVIEW_BACKUP_KEEP


def test_two_backups_do_not_overwrite(tmp_path, monkeypatch):
    db = tmp_path / "state.sqlite3"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE review_state (ticket_id INTEGER PRIMARY KEY, review_result TEXT); CREATE TABLE closed_review_state (ticket_id INTEGER PRIMARY KEY, review_result TEXT);")
    c.commit(); c.close()
    monkeypatch.setenv("REVIEW_BACKUP_DIR", str(tmp_path / "backups"))
    first = review_backups.create_backup(db, "startup")
    second = review_backups.create_backup(db, "startup")
    assert first != second and first.exists() and second.exists()
