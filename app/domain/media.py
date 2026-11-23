"""媒体完整性：内容 SHA-256 计算与历史场次回填。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .. import db
from ..config import MEDIA_DIR


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def media_path_from_url(media_url: str | None, media_dir: Path | str | None = None) -> Path | None:
    if not media_url or not media_url.startswith("/media/"):
        return None
    base = Path(media_dir or MEDIA_DIR).resolve()
    target = (base / media_url[len("/media/"):]).resolve()
    if target != base and base not in target.parents:
        return None
    return target


def backfill_media_hashes(conn, media_dir: Path | str | None = None) -> int:
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT attempt_id, media_url FROM attempts "
            "WHERE media_url IS NOT NULL AND media_url != '' "
            "AND (media_sha256 IS NULL OR media_sha256 = '')"
        ).fetchall()
    )
    cache: dict[str, Any] = {}
    updated = 0
    for row in rows:
        media_url = row["media_url"]
        if media_url not in cache:
            path = media_path_from_url(media_url, media_dir)
            cache[media_url] = file_sha256(path) if path and path.is_file() else None
        digest = cache[media_url]
        if not digest:
            continue
        conn.execute(
            "UPDATE attempts SET media_sha256 = ? WHERE attempt_id = ?",
            (digest, row["attempt_id"]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated
