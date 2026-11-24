"""申诉快照与改判率：在重评前固化原始结果，事后才能区分“维持原判”与“改判”。"""
from __future__ import annotations

import json
from typing import Any

from .. import db


def capture_snapshot(conn, attempt) -> str:
    """申诉发起时保存每个评分项的最终分与场次状态，作为改判比较基线。"""
    rows = conn.execute(
        "SELECT record_id, final_score FROM score_records WHERE attempt_id = ?",
        (attempt["attempt_id"],),
    ).fetchall()
    snapshot = {
        "at": db.now_iso(),
        "status": attempt["status"],
        "records": {row["record_id"]: row["final_score"] for row in rows},
    }
    return json.dumps(snapshot, ensure_ascii=False)


def appeal_metrics(
    attempts: list[dict[str, Any]], records: list[dict[str, Any]]
) -> dict[str, Any]:
    appealed = [a for a in attempts if a.get("appeal")]
    by_attempt: dict[str, dict[str, Any]] = {}
    pending_attempts: set[str] = set()
    for record in records:
        by_attempt.setdefault(record["attempt_id"], {})[record["record_id"]] = record["final_score"]
        if record["status"] in ("pending_review", "manual_pending"):
            pending_attempts.add(record["attempt_id"])

    comparable = 0
    changed = 0
    missing_snapshot = 0
    in_progress = 0
    for attempt in appealed:
        raw = attempt.get("appeal_snapshot")
        if not raw:
            missing_snapshot += 1
            continue
        try:
            snapshot = json.loads(raw)
        except (TypeError, ValueError):
            missing_snapshot += 1
            continue
        # 只有“申诉前已有签字/发布结论”且重评已经结束的场次，才能比较是否改判。
        if snapshot.get("status") not in ("signed_off", "published"):
            in_progress += 1
            continue
        if attempt.get("status") in ("pending_review", "integrity_hold") or attempt["attempt_id"] in pending_attempts:
            in_progress += 1
            continue
        before = snapshot.get("records") or {}
        current = by_attempt.get(attempt["attempt_id"], {})
        comparable += 1
        if any(current.get(record_id) != score for record_id, score in before.items()) or any(
            record_id not in current for record_id in before
        ):
            changed += 1

    total = len(attempts)
    return {
        "appealed": len(appealed),
        "appeal_rate": round(len(appealed) / total, 4) if total else 0.0,
        "compared": comparable,
        "changed": changed,
        "change_rate": round(changed / comparable, 4) if comparable else None,
        "in_progress": in_progress,
        "missing_snapshot": missing_snapshot,
        "target_appeal_rate": 0.03,
        "target_change_rate": 0.20,
    }
