"""治理自检、终止开关、申诉演练、伦理批准与阶段升级。"""
from __future__ import annotations

import json
import time
from typing import Any

from .. import db
from ..config import MODEL_VERSION, PROMPT_VERSION
from .ab import ab_report
from .bundle import apply_authority_reset
from .fairness import fairness_report
from .gates import evaluate_gates
from .stats import quality_report

STAGE_ORDER = ["G0_offline", "G1_practice", "G2_simulation", "G3_limited"]


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


def emergency_stop_active(conn) -> bool:
    return (db.get_state(conn, "emergency_stop", "0") or "0") == "1"


def set_emergency_stop(conn, *, active: bool, reason: str, actor: str) -> dict[str, Any]:
    previous = db.get_state(conn, "emergency_stop", "0")
    db.set_state(conn, "emergency_stop", "1" if active else "0")
    db.append_audit(
        conn,
        actor=actor,
        action="governance.termination_switch",
        object_type="System",
        object_id="emergency_stop",
        before={"active": previous == "1"},
        after={"active": active, "reason": reason},
    )
    return {"active": active, "reason": reason, "actor": actor}


def run_termination_drill(conn, *, actor: str = "考务") -> dict[str, Any]:
    started = time.perf_counter()
    set_emergency_stop(conn, active=True, reason="终止开关演练", actor=actor)
    blocked = emergency_stop_active(conn)
    set_emergency_stop(conn, active=False, reason="终止开关演练结束", actor=actor)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    passed = blocked and not emergency_stop_active(conn) and duration_ms < 2000
    drill_id = db.new_id("swdrill")
    conn.execute(
        "INSERT INTO switch_drills(drill_id, duration_ms, passed, actor, detail, created_at) VALUES(?,?,?,?,?,?)",
        (
            drill_id,
            duration_ms,
            1 if passed else 0,
            actor,
            json.dumps({"blocked": blocked, "restored": not emergency_stop_active(conn)}, ensure_ascii=False),
            db.now_iso(),
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="governance.termination_drill",
        object_type="System",
        object_id=drill_id,
        after={"duration_ms": duration_ms, "passed": passed},
    )
    return {
        "drill_id": drill_id,
        "duration_ms": duration_ms,
        "passed": passed,
        "actor": actor,
        "created_at": db.now_iso(),
    }


def run_appeal_drill(conn, *, actor: str = "考务", attempt_id: str | None = None) -> dict[str, Any]:
    if attempt_id:
        attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    else:
        attempt = conn.execute(
            "SELECT * FROM attempts ORDER BY CASE WHEN status = 'published' THEN 0 ELSE 1 END, attempt_id LIMIT 1"
        ).fetchone()
    if not attempt:
        raise ValueError("无可用场次进行申诉演练")
    record_count = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ?", (attempt["attempt_id"],)
    ).fetchone()["n"]
    bundle = conn.execute("SELECT * FROM bundles WHERE bundle_id = ?", (attempt["bundle_id"],)).fetchone()
    rubric_count = 0
    if bundle:
        rubric_count = len(json.loads(bundle["payload"]).get("rubric", []))
    steps = [
        {"step": "申诉提交", "passed": True, "detail": "已创建申诉演练记录"},
        {"step": "记录锁定", "passed": record_count > 0, "detail": f"锁定 {record_count} 条评分记录与 bundle 哈希"},
        {"step": "全量重评", "passed": record_count == rubric_count and rubric_count > 0, "detail": "全部评分项均进入重评清单"},
        {"step": "裁决记录", "passed": True, "detail": "演练裁决：维持原判（不写入真实成绩）"},
    ]
    passed = all(step["passed"] for step in steps)
    drill_id = db.new_id("apdrill")
    conn.execute(
        "INSERT INTO appeal_drills(drill_id, attempt_id, steps, passed, actor, created_at) VALUES(?,?,?,?,?,?)",
        (
            drill_id,
            attempt["attempt_id"],
            json.dumps(steps, ensure_ascii=False),
            1 if passed else 0,
            actor,
            db.now_iso(),
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="governance.appeal_drill",
        object_type="Appeal",
        object_id=drill_id,
        after={"attempt_id": attempt["attempt_id"], "passed": passed},
    )
    return {
        "drill_id": drill_id,
        "attempt_id": attempt["attempt_id"],
        "steps": steps,
        "passed": passed,
        "actor": actor,
        "created_at": db.now_iso(),
    }


def register_approval(conn, *, approval_type: str, approver: str, note: str = "") -> dict[str, Any]:
    approval_id = db.new_id("approval")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO governance_approvals(approval_id, approval_type, status, approver, note, created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(approval_type) DO UPDATE SET status = excluded.status, "
        "approver = excluded.approver, note = excluded.note, created_at = excluded.created_at",
        (approval_id, approval_type, "approved", approver, note, timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=approver,
        action="governance.approval",
        object_type="Approval",
        object_id=approval_type,
        after={"status": "approved", "note": note},
    )
    return {
        "approval_id": approval_id,
        "approval_type": approval_type,
        "status": "approved",
        "approver": approver,
        "note": note,
        "created_at": timestamp,
    }


def publish_model_card(conn, *, actor: str = "系统") -> dict[str, Any]:
    row = conn.execute("SELECT * FROM model_cards ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row:
        raise ValueError("尚无可公开的模型卡")
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE model_cards SET is_public = 1, published_at = ? WHERE card_id = ?",
        (timestamp, row["card_id"]),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="model_card.publish",
        object_type="ModelCard",
        object_id=row["card_id"],
        before={"is_public": bool(row["is_public"])},
        after={"is_public": True},
        model_version=row["model_version"],
        prompt_version=row["prompt_version"],
    )
    return {"card_id": row["card_id"], "is_public": True, "published_at": timestamp}


def governance_status(conn) -> dict[str, Any]:
    switch = conn.execute(
        "SELECT * FROM switch_drills ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    appeal_rows = conn.execute(
        "SELECT * FROM appeal_drills ORDER BY created_at DESC"
    ).fetchall()
    approvals = db.rows_to_dicts(
        conn.execute("SELECT * FROM governance_approvals ORDER BY created_at DESC").fetchall()
    )
    model_card = conn.execute(
        "SELECT card_id, is_public, published_at FROM model_cards ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return {
        "release_stage": db.get_state(conn, "release_stage", "G0_offline"),
        "emergency_stop": emergency_stop_active(conn),
        "switch_drill": dict(switch) if switch else None,
        "appeal_drills": {
            "count": len(appeal_rows),
            "passed": sum(1 for row in appeal_rows if row["passed"]),
            "latest": dict(appeal_rows[0]) if appeal_rows else None,
        },
        "approvals": approvals,
        "ethics_approved": any(
            row["approval_type"] == "ethics_and_data" and row["status"] == "approved"
            for row in approvals
        ),
        "model_card": dict(model_card) if model_card else None,
        "model_card_public": bool(model_card and model_card["is_public"]),
    }


def set_release_stage(conn, *, stage: str, actor: str, reason: str) -> dict[str, Any]:
    if stage not in STAGE_ORDER:
        raise ValueError("非法闸门阶段")
    current = db.get_state(conn, "release_stage", "G0_offline") or "G0_offline"
    if STAGE_ORDER.index(stage) <= STAGE_ORDER.index(current):
        raise ValueError("闸门阶段只能向上调整")
    report = quality_report(conn)
    gates = evaluate_gates(conn, report, current)["gates"]
    target = next((g for g in gates if g["stage"] == stage), None)
    if not target or target["status"] != "pass":
        raise ValueError(f"{stage} 闸门未通过，不能升级")
    for gate in gates:
        if STAGE_ORDER.index(gate["stage"]) < STAGE_ORDER.index(stage) and gate["status"] != "pass":
            raise ValueError(f"{gate['id']} 尚未通过，不能跨越式升级")
    db.set_state(conn, "release_stage", stage)
    db.append_audit(
        conn,
        actor=actor,
        action="governance.release_stage",
        object_type="ReleaseStage",
        object_id=stage,
        before={"release_stage": current},
        after={"release_stage": stage, "reason": reason},
    )
    return {"old_stage": current, "new_stage": stage, "actor": actor, "reason": reason}
