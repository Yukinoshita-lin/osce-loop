"""数据库备份：SQLite 在线快照、SHA-256 与备份清单。"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import db
from ..config import DB_PATH

BACKUP_DIR = DB_PATH.parent / "backups"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def create_backup(
    conn, *, actor: str = "系统", target_dir: Path | str | None = None
) -> dict[str, Any]:
    directory = Path(target_dir or BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"osceloop-{stamp}.db"
    if path.exists():
        path = directory / f"osceloop-{stamp}-{db.new_id('bk').split('_')[-1][:6]}.db"
    destination = sqlite3.connect(path)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    digest = _sha256_file(path)
    size_bytes = path.stat().st_size
    db.append_audit(
        conn,
        actor=actor,
        action="ops.backup",
        object_type="Database",
        object_id=path.name,
        before=None,
        after={"sha256": digest, "size_bytes": size_bytes},
    )
    return {
        "filename": path.name,
        "path": str(path),
        "size_bytes": size_bytes,
        "sha256": digest,
        "created_at": db.now_iso(),
    }


def list_backups(
    *, limit: int = 10, target_dir: Path | str | None = None
) -> list[dict[str, Any]]:
    directory = Path(target_dir or BACKUP_DIR)
    if not directory.exists():
        return []
    files = sorted(
        directory.glob("osceloop-*.db"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[:limit]
    backups = []
    for path in files:
        stat = path.stat()
        backups.append(
            {
                "filename": path.name,
                "size_bytes": stat.st_size,
                "sha256": _sha256_file(path),
                "created_at": datetime.fromtimestamp(stat.st_mtime)
                .astimezone()
                .isoformat(timespec="seconds"),
            }
        )
    return backups
