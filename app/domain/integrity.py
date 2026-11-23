"""转写完整性：规范化 SHA-256、回填与校验状态。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def transcript_digest(utterances: list[dict[str, Any]]) -> str:
    canonical = json.dumps(utterances, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def transcript_integrity(stored: str | None, utterances: list[dict[str, Any]] | None) -> str:
    if utterances is None:
        return "missing"
    if not stored:
        return "unrecorded"
    return "ok" if stored == transcript_digest(utterances) else "mismatch"


def backfill_transcript_hashes(conn) -> int:
    """为历史转写补齐校验值；不改动 payload 本身。"""
    rows = conn.execute(
        "SELECT attempt_id, payload FROM transcripts WHERE sha256 IS NULL OR sha256 = ''"
    ).fetchall()
    updated = 0
    for row in rows:
        try:
            utterances = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(utterances, list):
            continue
        conn.execute(
            "UPDATE transcripts SET sha256 = ? WHERE attempt_id = ?",
            (transcript_digest(utterances), row["attempt_id"]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated
