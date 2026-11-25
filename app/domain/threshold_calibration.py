"""S9 阈值本地校准档案：固化本次部署采用的阈值快照与本地金标准证据。

设计约束：文中阈值是公开文献起始建议，必须在本地数据上重新校准后使用。
本模块不自动修改代码常量，而是把“本次部署实际采用的阈值 + 校准证据 + 说明”
写入可审计档案；G2/G3 阶段缺少有效校准记录时持续告警。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from ..config import COST_TARGET_CNY_EXAM, COST_TARGET_CNY_PRACTICE

VALID_DAYS = 365
DUE_SOON_DAYS = 30


def current_threshold_snapshot() -> list[dict[str, Any]]:
    """返回当前代码实际使用的阈值快照，供校准记录与界面核对。"""
    from .gates import GATES

    items: list[dict[str, Any]] = []
    for gate in GATES:
        for key, label, target_text, target, direction in gate["criteria"]:
            items.append(
                {
                    "gate": gate["id"],
                    "key": key,
                    "label": label,
                    "target": target,
                    "target_text": target_text,
                    "direction": direction,
                }
            )
    items.extend(
        [
            {
                "gate": "S9",
                "key": "asr_error_rate",
                "label": "医学专名 ASR 词错率",
                "target": 0.08,
                "target_text": "≤ 8%",
                "direction": "le",
            },
            {
                "gate": "S9",
                "key": "sus",
                "label": "SUS 可用性",
                "target": 75,
                "target_text": "≥ 75",
                "direction": "ge",
            },
            {
                "gate": "S9",
                "key": "cost_practice",
                "label": "练习单站成本",
                "target": COST_TARGET_CNY_PRACTICE,
                "target_text": f"≤ ¥{COST_TARGET_CNY_PRACTICE:.0f}",
                "direction": "le",
            },
            {
                "gate": "S9",
                "key": "cost_exam",
                "label": "考试单站成本",
                "target": COST_TARGET_CNY_EXAM,
                "target_text": f"≤ ¥{COST_TARGET_CNY_EXAM:.0f}",
                "direction": "le",
            },
            {
                "gate": "S9",
                "key": "feedback_practice_seconds",
                "label": "练习反馈生成",
                "target": 60,
                "target_text": "≤ 60 秒",
                "direction": "le",
            },
            {
                "gate": "S9",
                "key": "feedback_exam_hours",
                "label": "考试反馈生成",
                "target": 72,
                "target_text": "≤ 72 小时",
                "direction": "le",
            },
        ]
    )
    return items


def calibration_evidence(conn) -> dict[str, Any]:
    from .calibration import calibration_report

    report = calibration_report(conn)
    return {
        "gold_count": report.get("gold_count", 0),
        "qwk_ai": report.get("qwk_ai"),
        "qwk_human": report.get("qwk_human"),
        "coverage": report.get("coverage"),
        "recommended_confidence_floor": report.get("recommended_confidence_floor"),
    }


def record_threshold_calibration(
    conn,
    *,
    actor: str,
    note: str,
    valid_days: int = VALID_DAYS,
) -> dict[str, Any]:
    rationale = str(note or "").strip()
    if not rationale:
        raise ValueError("必须填写本地校准说明（数据来源、调整依据与适用范围）")
    thresholds = current_threshold_snapshot()
    evidence = calibration_evidence(conn)
    now = datetime.now(timezone.utc).astimezone()
    days = max(30, min(int(valid_days), 730))
    valid_until = (now + timedelta(days=days)).isoformat(timespec="seconds")
    calibration_id = db.new_id("tcal")
    timestamp = now.isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO threshold_calibrations(calibration_id, actor, thresholds, evidence, note, "
        "valid_until, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            calibration_id,
            actor,
            json.dumps(thresholds, ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False),
            rationale,
            valid_until,
            timestamp,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="thresholds.calibrated",
        object_type="ThresholdCalibration",
        object_id=calibration_id,
        before=None,
        after={
            "threshold_count": len(thresholds),
            "gold_count": evidence["gold_count"],
            "qwk_ai": evidence["qwk_ai"],
            "recommended_confidence_floor": evidence["recommended_confidence_floor"],
            "valid_until": valid_until,
            "note": rationale,
        },
    )
    return {
        "calibration_id": calibration_id,
        "actor": actor,
        "thresholds": thresholds,
        "evidence": evidence,
        "note": rationale,
        "valid_until": valid_until,
        "created_at": timestamp,
    }


def latest_threshold_calibration(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM threshold_calibrations ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        thresholds = json.loads(row["thresholds"] or "[]")
    except (TypeError, ValueError):
        thresholds = []
    try:
        evidence = json.loads(row["evidence"] or "{}")
    except (TypeError, ValueError):
        evidence = {}
    expires = None
    if row["valid_until"]:
        try:
            expires = datetime.fromisoformat(row["valid_until"])
        except ValueError:
            expires = None
    now = datetime.now(timezone.utc).astimezone()
    if expires is None:
        expired = True
        days_remaining = 0
    else:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        remaining = expires - now
        expired = remaining.total_seconds() <= 0
        days_remaining = max(0, int(remaining.total_seconds() // 86400))
    return {
        "calibration_id": row["calibration_id"],
        "actor": row["actor"],
        "thresholds": thresholds,
        "evidence": evidence,
        "note": row["note"],
        "valid_until": row["valid_until"],
        "created_at": row["created_at"],
        "expired": expired,
        "days_remaining": days_remaining,
        "due_soon": (not expired) and days_remaining <= DUE_SOON_DAYS,
        "valid": not expired,
    }
