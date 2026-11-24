"""排考特殊需求审批：延长时长与安静候考区由考务逐人确认并留痕。"""
from __future__ import annotations

import json
from typing import Any

from .. import db

DECISIONS = ("approved", "rejected")
DECISION_LABELS = {"approved": "已批准", "rejected": "已驳回"}


def _load_schedule(conn, schedule_id: str | None) -> dict[str, Any]:
    if schedule_id:
        row = conn.execute(
            "SELECT schedule FROM exam_schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        if not row:
            raise ValueError("排考表不存在")
    else:
        row = conn.execute(
            "SELECT schedule FROM exam_schedules ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise ValueError("尚未生成排考表")
    return json.loads(row["schedule"])


def _needs(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("extra_time_min") or candidate.get("quiet_room"))


def _latest_approvals(conn, schedule_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM schedule_special_needs_approvals WHERE schedule_id = ? "
        "ORDER BY created_at DESC, rowid DESC",
        (schedule_id,),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(row["candidate_id"], dict(row))
    return latest


def special_needs_status(conn, schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回特殊需求清单及每人最新审批状态；未审批即 pending。"""
    schedule = schedule or _load_schedule(conn, None)
    approvals = _latest_approvals(conn, schedule["schedule_id"])
    items: list[dict[str, Any]] = []
    for candidate in schedule.get("candidates") or []:
        if not _needs(candidate):
            continue
        approval = approvals.get(candidate["candidate_id"])
        items.append(
            {
                "candidate_id": candidate["candidate_id"],
                "name": candidate.get("name"),
                "extra_time_min": int(candidate.get("extra_time_min") or 0),
                "quiet_room": bool(candidate.get("quiet_room")),
                "note": candidate.get("note") or "",
                "status": approval["decision"] if approval else "pending",
                "status_label": DECISION_LABELS.get(approval["decision"], "待审批")
                if approval
                else "待审批",
                "approval": approval,
            }
        )
    pending = [item["candidate_id"] for item in items if item["status"] == "pending"]
    return {
        "schedule_id": schedule["schedule_id"],
        "items": items,
        "pending": pending,
        "approved": sum(1 for item in items if item["status"] == "approved"),
        "rejected": sum(1 for item in items if item["status"] == "rejected"),
        "complete": not pending,
    }


def record_special_needs_approval(
    conn,
    *,
    candidate_id: str,
    decision: str,
    note: str = "",
    actor: str = "考务",
    schedule_id: str | None = None,
) -> dict[str, Any]:
    """批准或驳回某考生的特殊需求安排；驳回必须填写理由。"""
    if decision not in DECISIONS:
        raise ValueError("decision 必须是 approved 或 rejected")
    note = str(note or "").strip()
    if decision == "rejected" and not note:
        raise ValueError("驳回特殊需求必须填写理由")
    schedule = _load_schedule(conn, schedule_id)
    candidate = next(
        (c for c in schedule.get("candidates") or [] if c["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise ValueError(f"考生 {candidate_id} 不在排考表中")
    if not _needs(candidate):
        raise ValueError(f"考生 {candidate_id} 没有需要审批的特殊需求")
    before = next(
        (
            item
            for item in special_needs_status(conn, schedule)["items"]
            if item["candidate_id"] == candidate_id
        ),
        None,
    )
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO schedule_special_needs_approvals(approval_id, schedule_id, candidate_id, "
        "decision, note, actor, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            db.new_id("snappr"),
            schedule["schedule_id"],
            candidate_id,
            decision,
            note,
            actor,
            timestamp,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.special_needs_approval",
        object_type="ExamSchedule",
        object_id=schedule["schedule_id"],
        before={"candidate_id": candidate_id, "status": before["status"] if before else None},
        after={
            "candidate_id": candidate_id,
            "decision": decision,
            "note": note,
            "extra_time_min": int(candidate.get("extra_time_min") or 0),
            "quiet_room": bool(candidate.get("quiet_room")),
        },
    )
    return special_needs_status(conn, schedule)
