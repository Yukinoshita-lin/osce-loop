"""数据库备份：快照可读、哈希与审计。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app import db
from app.domain.backup import create_backup, list_backups
from app.seed import seed_if_empty


def test_backup_snapshot_is_readable_and_audited(tmp_path):
    source = db.connect(tmp_path / "source.db")
    db.init_db(source)
    seed_if_empty(source)
    backup_dir = tmp_path / "backups"

    result = create_backup(source, actor="考务 李萍", target_dir=backup_dir)
    path = Path(result["path"])
    assert path.exists()
    assert result["filename"].startswith("osceloop-")
    assert result["sha256"].startswith("sha256:")
    assert result["size_bytes"] > 0

    snapshot = sqlite3.connect(path)
    snapshot.row_factory = sqlite3.Row
    try:
        assert snapshot.execute("SELECT COUNT(*) AS n FROM bundles").fetchone()["n"] == 1
        assert snapshot.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 13
    finally:
        snapshot.close()

    event = source.execute(
        "SELECT * FROM audit_events WHERE action = 'ops.backup' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    assert event["actor"] == "考务 李萍"
    source.close()

    listing = list_backups(target_dir=backup_dir)
    assert len(listing) == 1
    assert listing[0]["filename"] == result["filename"]
    assert listing[0]["sha256"] == result["sha256"]


def test_list_backups_handles_missing_directory(tmp_path):
    assert list_backups(target_dir=tmp_path / "missing") == []
