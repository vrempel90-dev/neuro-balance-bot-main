import sqlite3
from pathlib import Path
from production_backup import checkpoint


def test_checkpoint_restores_original_database(tmp_path):
    original = tmp_path / "state.sqlite3"
    with sqlite3.connect(original) as conn:
        conn.execute("CREATE TABLE example (value TEXT)")
        conn.execute("INSERT INTO example VALUES ('synthetic')")
    result = checkpoint(str(original), "synthetic-release")
    assert result["created"] and result["verified"]
    source = tmp_path / "backups" / result["checkpoint"]
    restored = tmp_path / "restore.sqlite3"
    with sqlite3.connect(source) as backup, sqlite3.connect(restored) as target:
        backup.backup(target)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT value FROM example").fetchone()[0] == "synthetic"
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert source.stat().st_mode & 0o777 == 0o600
    assert checkpoint(str(original), "synthetic-release")["created"] is False


def test_corrupt_backup_prevents_migration(tmp_path):
    original = tmp_path / "state.sqlite3"
    with sqlite3.connect(original) as conn:
        conn.execute("CREATE TABLE example (value TEXT)")
    result = checkpoint(str(original), "synthetic-release")
    (tmp_path / "backups" / result["checkpoint"]).write_bytes(b"corrupt")
    try:
        checkpoint(str(original), "synthetic-release")
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("corrupt checkpoint accepted")
