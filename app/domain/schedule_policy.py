"""排考软约束权重确认：公平与效率的取舍是价值判断，必须由人签字。"""
from __future__ import annotations

from typing import Any

from .. import db

PRESETS: dict[str, dict[str, Any]] = {
    "fairness": {
        "label": "公平优先",
        "fairness_weight": 0.75,
        "description": "优先压低考生间加权难度差与末尾顺序效应",
    },
    "balanced": {
        "label": "均衡",
        "fairness_weight": 0.5,
        "description": "难度均衡与考站使用均衡同时考虑",
    },
    "efficiency": {
        "label": "效率优先",
        "fairness_weight": 0.25,
        "description": "优先压低考站使用次数差，接受一定难度波动",
    },
}
MIN_NOTE_CHARS = 4


def presets_payload() -> list[dict[str, Any]]:
    return [{"preset": key, **value} for key, value in PRESETS.items()]


def _row_to_policy(row) -> dict[str, Any]:
    return {
        "policy_id": row["policy_id"],
        "preset": row["preset"],
        "fairness_weight": float(row["fairness_weight"]),
        "label": row["label"],
        "note": row["note"],
        "actor": row["actor"],
        "created_at": row["created_at"],
    }


def get_policy(conn, policy_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM schedule_policy_confirmations WHERE policy_id = ?", (policy_id,)
    ).fetchone()
    return _row_to_policy(row) if row else None


def latest_policy(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM schedule_policy_confirmations ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return _row_to_policy(row) if row else None


def confirm_policy(
    conn,
    *,
    preset: str = "balanced",
    fairness_weight: float | None = None,
    note: str = "",
    actor: str = "考务",
) -> dict[str, Any]:
    """确认一次软约束权重；preset 决定默认权重，也可显式传自定义权重。"""
    if preset not in PRESETS and fairness_weight is None:
        raise ValueError("preset 必须是 fairness、balanced、efficiency 之一")
    if fairness_weight is not None:
        try:
            weight = float(fairness_weight)
        except (TypeError, ValueError):
            raise ValueError("fairness_weight 必须是 0 到 1 之间的数字") from None
    else:
        weight = float(PRESETS[preset]["fairness_weight"])
    if weight < 0 or weight > 1:
        raise ValueError("fairness_weight 必须在 0 到 1 之间")
    note = str(note or "").strip()
    if len(note) < MIN_NOTE_CHARS:
        raise ValueError(f"确认软约束权重必须填写不少于 {MIN_NOTE_CHARS} 字的理由")
    label = PRESETS[preset]["label"] if preset in PRESETS else "自定义"
    timestamp = db.now_iso()
    policy_id = db.new_id("pol")
    conn.execute(
        "INSERT INTO schedule_policy_confirmations(policy_id, preset, fairness_weight, label, "
        "note, actor, created_at) VALUES(?,?,?,?,?,?,?)",
        (policy_id, preset, weight, label, note, actor, timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.schedule_policy_confirmed",
        object_type="ExamSchedule",
        object_id=policy_id,
        after={
            "preset": preset,
            "fairness_weight": round(weight, 3),
            "label": label,
            "note": note,
        },
    )
    return get_policy(conn, policy_id)
