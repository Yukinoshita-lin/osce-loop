"""转写完整性：规范化 SHA-256、回填与校验状态。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import db

MIN_SUPPLEMENT_CHARS = 10
MAX_SUPPLEMENT_CHARS = 500


def transcript_digest(utterances: list[dict[str, Any]]) -> str:
    canonical = json.dumps(utterances, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def quote_digest(text: str) -> str:
    """与评分提供方一致的证据引文指纹：sha256 前 24 位。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def verify_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """核验引文与入库时的 transcript_hash；缺席证据使用阶段窗口，不参与比对。"""
    checked = 0
    matched = 0
    skipped = 0
    samples: list[dict[str, Any]] = []
    for entry in evidence:
        quote = str(entry.get("quote") or "")
        stored = str(entry.get("transcript_hash") or "")
        if entry.get("evidence_kind") == "absence" or not quote or not stored:
            skipped += 1
            continue
        checked += 1
        expected = quote_digest(quote)
        if stored == expected:
            matched += 1
        elif len(samples) < 5:
            samples.append(
                {"quote": quote[:80], "stored": stored, "expected": expected}
            )
    mismatched = checked - matched
    return {
        "checked": checked,
        "matched": matched,
        "mismatched": mismatched,
        "skipped": skipped,
        "passed": mismatched == 0,
        "samples": samples,
    }


def transcript_integrity(stored: str | None, utterances: list[dict[str, Any]] | None) -> str:
    if utterances is None:
        return "missing"
    if not stored:
        return "unrecorded"
    return "ok" if stored == transcript_digest(utterances) else "mismatch"


def integrity_supplement_missing(attempt: dict[str, Any]) -> bool:
    """证据链缺口的场次必须补现场记录后才能签字。"""
    return bool(attempt.get("integrity_gap")) and not str(
        attempt.get("integrity_supplement") or ""
    ).strip()


def record_integrity_supplement(
    conn,
    attempt_id: str,
    *,
    note: str,
    actor: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT attempt_id, integrity_gap, integrity_supplement FROM attempts "
        "WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if not row:
        raise ValueError("场次不存在")
    if not row["integrity_gap"]:
        raise ValueError("该场次未标记证据链缺口，无需补充现场记录")
    note = (note or "").strip()
    if len(note) < MIN_SUPPLEMENT_CHARS:
        raise ValueError(f"现场补充记录不少于 {MIN_SUPPLEMENT_CHARS} 字")
    if len(note) > MAX_SUPPLEMENT_CHARS:
        raise ValueError(f"现场补充记录不能超过 {MAX_SUPPLEMENT_CHARS} 字")
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE attempts SET integrity_supplement = ?, integrity_supplement_by = ?, "
        "integrity_supplement_at = ? WHERE attempt_id = ?",
        (note, actor, timestamp, attempt_id),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="attempt.integrity_supplement",
        object_type="StationAttempt",
        object_id=attempt_id,
        before={"integrity_supplement": row["integrity_supplement"]},
        after={"integrity_supplement": note, "chars": len(note)},
    )
    return {
        "attempt_id": attempt_id,
        "note": note,
        "actor": actor,
        "created_at": timestamp,
    }


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
