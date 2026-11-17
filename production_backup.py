"""Verified SQLite checkpoint before additive production schema migration."""
from contextlib import closing
from pathlib import Path
import hashlib
import os
import sqlite3


def checkpoint(database_path: str, release: str) -> dict:
    source = Path(database_path).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        return {"created": False, "reason": "no_existing_database"}
    folder = source.parent / "backups"
    folder.mkdir(mode=0o700, exist_ok=True)
    identifier = hashlib.sha256((str(source) + release).encode()).hexdigest()[:24]
    destination = folder / f"pre-release-{identifier}.sqlite3"
    if destination.exists():
        with closing(sqlite3.connect(destination)) as existing:
            if existing.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Existing release checkpoint is corrupt")
        return {"created": False, "verified": True, "checkpoint": destination.name}
    temporary = destination.with_suffix(".tmp")
    try:
        # sqlite3's own context manager ends transactions but leaves handles
        # open. Close both files before publication or exception cleanup.
        with closing(sqlite3.connect(source, timeout=10)) as original, closing(sqlite3.connect(temporary)) as backup:
            original.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Database checkpoint verification failed")
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"created": True, "verified": True, "checkpoint": destination.name}
