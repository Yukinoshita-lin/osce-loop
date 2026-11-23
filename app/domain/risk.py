"""风险等级治理：同一场考试内只允许向上调整，禁止组织性降级掩盖。"""
from __future__ import annotations

import json
from typing import Any

from .. import db

RISK_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


def escalate_risk(
    conn,
    *,
    attempt_id: str,
    new_level: str,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    if new_level not in RISK_ORDER:
        raise ValueError("非法风险等级")
    row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    if not row:
        raise ValueError("场次不存在")
    attempt = dict(row)
    if attempt["status"] == "published":
        raise ValueError("已发布场次需通过申诉流程重新开启")
    if RISK_ORDER[new_level] <= RISK_ORDER.get(attempt["risk_level"], 0):
        raise ValueError("风险等级只能向上调整，不能向下或持平")

    timestamp = db.now_iso()
    conn.execute(
        "UPDATE attempts SET risk_level = ?, risk_escalated_at = ?, risk_escalation_reason = ?, "
        "status = 'pending_review', signed_off_by = NULL, signed_off_at = NULL, published_at = NULL "
        "WHERE attempt_id = ?",
        (new_level, timestamp, reason, attempt_id),
    )
    for record in conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ?", (attempt_id,)
    ).fetchall():
        routing = json.loads(record["routing"] or "{}")
        routing.update(
            {
                "requires_human": True,
                "route": "full_station_human",
                "reason": "risk_escalation",
                "reasons": ["risk_escalation"],
                "reason_text": f"风险等级由 {attempt['risk_level']} 提升为 {new_level}，全站进入人工复核",
            }
        )
        conn.execute(
            "UPDATE score_records SET status = ?, final_score = NULL, reviewer = NULL, reviewed_at = NULL, "
            "routing = ? WHERE record_id = ?",
            (
                "manual_pending" if record["ai_score"] is None else "pending_review",
                json.dumps(routing, ensure_ascii=False),
                record["record_id"],
            ),
        )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="attempt.risk_escalation",
        object_type="StationAttempt",
        object_id=attempt_id,
        before={"risk_level": attempt["risk_level"], "status": attempt["status"]},
        after={"risk_level": new_level, "status": "pending_review", "reason": reason},
    )
    return {
        "attempt_id": attempt_id,
        "old_level": attempt["risk_level"],
        "new_level": new_level,
        "reason": reason,
        "escalated_at": timestamp,
    }
