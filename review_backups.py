"""Verified, multi-generation backups for the local review-state database."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)
DEFAULT_REVIEW_BACKUP_KEEP = 200
AUTO_PREFIX = "Review_state-auto-"
_AUTO_RE = re.compile(r"^Review_state-auto-[0-9]{8}T[0-9]{6}_[0-9]{6}Z-[a-z0-9-]+\.sqlite3$")
_LOCK = threading.Lock()


def backup_dir() -> Path:
    return Path(os.environ.get("REVIEW_BACKUP_DIR") or "~/FreshdeskScannerBackups/review_state").expanduser()


def keep_count() -> int:
    try:
        value = int(os.environ.get("REVIEW_BACKUP_KEEP", str(DEFAULT_REVIEW_BACKUP_KEEP)))
        return value if value >= 1 else DEFAULT_REVIEW_BACKUP_KEEP
    except (TypeError, ValueError):
        return DEFAULT_REVIEW_BACKUP_KEEP


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_reason(reason: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", str(reason)).strip("-").lower()
    return value or "backup"


def _counts(conn, table):
    total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    counts = {row[0]: row[1] for row in conn.execute(
        f'SELECT review_result, COUNT(*) FROM "{table}" GROUP BY review_result')}
    return total, counts


def _metadata_for(path: Path, reason: str, source: Path, source_sha: str, backup_sha: str, integrity: str):
    conn = sqlite3.connect(path)
    try:
        queue_total, queue_counts = _counts(conn, "review_state")
        closed_total, closed_counts = _counts(conn, "closed_review_state")
    finally:
        conn.close()
    return {
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reason": _safe_reason(reason), "source_db": str(source),
        "source_sha256": source_sha, "backup_sha256": backup_sha,
        "integrity_check": integrity, "review_state_total": queue_total,
        "review_state_counts": queue_counts, "closed_review_state_total": closed_total,
        "closed_review_state_counts": closed_counts,
    }


def _valid_auto(path: Path) -> bool:
    return bool(_AUTO_RE.match(path.name))


def prune() -> None:
    files = sorted((p for p in backup_dir().glob(AUTO_PREFIX + "*.sqlite3") if _valid_auto(p)), key=lambda p: p.name)
    excess = files[:-keep_count()]
    for db in excess:
        db.unlink(missing_ok=True)
        db.with_suffix(".json").unlink(missing_ok=True)


def create_backup(db_path: str | os.PathLike | None = None, reason="startup") -> Path:
    source = Path(db_path or os.environ.get("REVIEW_DB_PATH") or "data/review_state.sqlite3").expanduser().resolve()
    destination_dir = backup_dir()
    with _LOCK:
        destination_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        final = destination_dir / f"{AUTO_PREFIX}{stamp}-{_safe_reason(reason)}.sqlite3"
        fd, temp_name = tempfile.mkstemp(prefix=".review-state-", suffix=".tmp", dir=destination_dir)
        os.close(fd)
        temp = Path(temp_name)
        try:
            source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            destination = sqlite3.connect(temp)
            try:
                source_conn.backup(destination)
                destination.commit()
                integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"backup integrity_check failed: {integrity}")
                tables = {row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"review_state", "closed_review_state"} <= tables:
                    raise RuntimeError("backup is missing required review-state tables")
            finally:
                destination.close(); source_conn.close()
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            backup_sha = _sha(temp)
            source_sha = _sha(source)
            os.replace(temp, final)
            metadata = _metadata_for(final, reason, source, source_sha, backup_sha, integrity)
            meta = final.with_suffix(".json")
            with meta.open("w", encoding="utf-8") as stream:
                json.dump(metadata, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush(); os.fsync(stream.fileno())
            try:
                prune()
            except Exception:
                LOGGER.warning("Automatic review-state backup retention cleanup failed", exc_info=True)
            return final
        except Exception:
            temp.unlink(missing_ok=True)
            raise


def ensure_startup_backup(db_path=None) -> Path | None:
    source = Path(db_path or os.environ.get("REVIEW_DB_PATH") or "data/review_state.sqlite3").expanduser().resolve()
    if not source.exists():
        return None
    source_sha = _sha(source)
    candidates = sorted((p for p in backup_dir().glob(AUTO_PREFIX + "*.json") if _valid_auto(p.with_suffix(".sqlite3"))), key=lambda p: p.name)
    if candidates:
        try:
            data = json.loads(candidates[-1].read_text(encoding="utf-8"))
            if data.get("source_sha256") == source_sha and candidates[-1].with_suffix(".sqlite3").exists():
                return candidates[-1].with_suffix(".sqlite3")
        except (OSError, ValueError):
            pass
    return create_backup(source, "startup")
