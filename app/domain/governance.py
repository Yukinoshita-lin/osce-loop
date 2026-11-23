"""治理自检：按闸门阈值自动降级，且把检查结果写入审计。"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from ..config import MODEL_VERSION, PROMPT_VERSION
from .ab import ab_report
from .bundle import apply_authority_reset
from .fairness import fairness_report
from .stats import quality_report


def run_self_check(conn, *, actor: str = "系统", apply: bool = True) -> dict[str, Any]:
    report = quality_report(conn)
    fairness = fairness_report(conn)
    ab = ab_report(conn)

    records = conn.execute("SELECT * FROM score_records WHERE ai_score IS NOT NULL").fetchall()
    stamped = sum(
        1
        for rec in records
        if rec["model_version"] and rec["prompt_version"] and rec["rubric_version"]
    )
    version_complete = len(records) > 0 and stamped == len(records)

    checks: list[dict[str, Any]] = []
    qwk = report.get("qwk_ai")
    checks.append(
        {
            "key": "qwk",
            "label": "滚动 QWK",
            "value": qwk,
            "threshold": 0.65,
            "status": "pass" if (qwk is None or qwk >= 0.65) else "fail",
            "action": "自动把评分能力降为 extract，直到重新校准达标",
            "auto_apply": True,
        }
    )
    integrity = report.get("integrity_gap_rate", 0)
    checks.append(
        {
            "key": "integrity_gap",
            "label": "证据链缺口率",
            "value": integrity,
            "threshold": 0.02,
            "status": "pass" if integrity <= 0.02 else "fail",
            "action": "相关场次全站人工复核；不因单场缺口全局降级",
            "auto_apply": False,
        }
    )
    subgroup = fairness.get("overall_gap_sd")
    checks.append(
        {
            "key": "subgroup_gap",
            "label": "亚组分数差",
            "value": subgroup,
            "threshold": 0.20,
            "status": "unmeasured" if subgroup is None else ("pass" if subgroup <= 0.20 else "fail"),
            "action": "自动降级相关能力并启动公平性纠正动作",
            "auto_apply": True,
        }
    )
    checks.append(
        {
            "key": "version_stamp",
            "label": "三元版本戳完整率",
            "value": 1.0 if version_complete else 0.0,
            "threshold": 1.0,
            "status": "pass" if version_complete else "fail",
            "action": "缺失版本戳的记录不得进入成绩计算",
            "auto_apply": False,
        }
    )
    checks.append(
        {
            "key": "teacher_hours",
            "label": "教师评分耗时下降",
            "value": ab.get("comparison", {}).get("teacher_hours_reduction"),
            "threshold": 0.40,
            "status": "unmeasured"
            if ab.get("comparison", {}).get("teacher_hours_reduction") is None
            else (
                "pass"
                if ab["comparison"]["teacher_hours_reduction"] >= 0.40
                else "fail"
            ),
            "action": "未达 40% 时回到人工流程并定位瓶颈",
            "auto_apply": False,
        }
    )

    failing = [c for c in checks if c["status"] == "fail"]
    auto_failing = [c for c in failing if c["auto_apply"]]
    changes: list[dict[str, Any]] = []
    if apply and auto_failing:
        changes = apply_authority_reset(
            conn,
            reason="治理自检触发自动降级：" + "、".join(c["label"] for c in auto_failing),
            actor=actor,
            model_version=db.get_state(conn, "model_version", MODEL_VERSION),
            prompt_version=db.get_state(conn, "prompt_version", PROMPT_VERSION),
        )

    trigger_summary = "、".join(c["label"] for c in failing) if failing else "全部通过"
    detail = {
        "checks": checks,
        "changes": changes,
        "metrics": {
            "qwk_ai": qwk,
            "qwk_human": report.get("qwk_human"),
            "integrity_gap_rate": integrity,
            "subgroup_gap_sd": subgroup,
            "teacher_hours_reduction": ab.get("comparison", {}).get("teacher_hours_reduction"),
        },
    }
    check_id = db.new_id("gcheck")
    conn.execute(
        "INSERT INTO governance_checks(check_id, actor, trigger_summary, applied, detail, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            check_id,
            actor,
            trigger_summary,
            1 if changes else 0,
            json.dumps(detail, ensure_ascii=False),
            db.now_iso(),
        ),
    )
    conn.commit()
    return {
        "check_id": check_id,
        "trigger_summary": trigger_summary,
        "checks": checks,
        "applied_changes": changes,
        "applied": bool(changes),
        "detail": detail,
    }


def latest_self_check(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM governance_checks ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    data = json.loads(row["detail"])
    data.update(
        {
            "check_id": row["check_id"],
            "actor": row["actor"],
            "trigger_summary": row["trigger_summary"],
            "applied": bool(row["applied"]),
            "created_at": row["created_at"],
        }
    )
    return data
