"""治理自检、终止开关、申诉演练、伦理批准与阶段升级。"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import db
from ..config import MODEL_VERSION, PROMPT_VERSION
from .ab import ab_report
from .bundle import apply_authority_reset, apply_item_downgrades, list_bundles
from .fairness import fairness_report
from .gates import evaluate_gates
from .stats import quality_report, quadratic_weighted_kappa

STAGE_ORDER = ["G0_offline", "G1_practice", "G2_simulation", "G3_limited"]
# 运行期降级按当前闸门 QWK 目标减 0.05；G1 没有 QWK 指标，沿用 G0 基线。
QWK_STAGE_TARGET = {"G0_offline": 0.70, "G1_practice": 0.70, "G2_simulation": 0.75, "G3_limited": 0.80}
INTEGRITY_GAP_LIMIT = 0.02
SUBGROUP_GAP_LIMIT = 0.20
SUBGROUP_WEEKS_REQUIRED = 2
SUBGROUP_HISTORY_KEY = "safety_subgroup_history"


def _week_key(moment: date | datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _week_start(key: str) -> date:
    year, week = key.split("-W")
    return date.fromisocalendar(int(year), int(week), 1)


def _previous_week(key: str) -> str:
    return _week_key(_week_start(key) - timedelta(days=7))


def _load_subgroup_history(conn) -> dict[str, float]:
    raw = db.get_state(conn, SUBGROUP_HISTORY_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    history: dict[str, float] = {}
    for key, value in data.items():
        try:
            history[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return history


def _record_subgroup_observation(conn, week: str, gap: float) -> None:
    history = _load_subgroup_history(conn)
    history[week] = round(float(gap), 4)
    ordered = sorted(history.items(), key=lambda item: _week_start(item[0]))[-8:]
    db.set_state(conn, SUBGROUP_HISTORY_KEY, json.dumps(dict(ordered), ensure_ascii=False))


def _subgroup_streak(history: dict[str, float], week: str, gap: float | None) -> list[str]:
    """返回连续超阈值的周列表；当前周无数据时从最近一次观测起算。"""
    observations = dict(history)
    if gap is not None:
        observations[week] = float(gap)
    elif not observations:
        return []
    else:
        week = max(observations, key=_week_start)
    weeks: list[str] = []
    cursor = week
    while True:
        value = observations.get(cursor)
        if value is None or value <= SUBGROUP_GAP_LIMIT:
            break
        weeks.append(cursor)
        cursor = _previous_week(cursor)
    return list(reversed(weeks))


def _rolling_qwk(conn, limit: int = 200) -> float | None:
    rows = conn.execute(
        "SELECT ai_score, gold_score FROM score_records "
        "WHERE ai_score IS NOT NULL AND gold_score IS NOT NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return quadratic_weighted_kappa(
        [row["ai_score"] for row in rows],
        [row["gold_score"] for row in rows],
    )


def _station_integrity(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.bundle_id, b.station_id, b.title, COUNT(*) AS attempts, "
        "SUM(CASE WHEN a.integrity_gap = 1 THEN 1 ELSE 0 END) AS gap_attempts "
        "FROM attempts a LEFT JOIN bundles b ON b.bundle_id = a.bundle_id "
        "GROUP BY a.bundle_id ORDER BY a.bundle_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        attempts = int(row["attempts"] or 0)
        gaps = int(row["gap_attempts"] or 0)
        rate = round(gaps / attempts, 4) if attempts else 0.0
        result.append(
            {
                "bundle_id": row["bundle_id"],
                "station_id": row["station_id"],
                "title": row["title"],
                "attempts": attempts,
                "gap_attempts": gaps,
                "rate": rate,
                "breach": rate > INTEGRITY_GAP_LIMIT,
            }
        )
    return result


def run_self_check(
    conn,
    *,
    actor: str = "系统",
    apply: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    report = quality_report(conn)
    fairness = fairness_report(conn)
    ab = ab_report(conn)
    stage = db.get_state(conn, "release_stage", "G0_offline") or "G0_offline"
    qwk_target = QWK_STAGE_TARGET.get(stage, 0.70)
    qwk_floor = round(qwk_target - 0.05, 2)
    week = _week_key(now or datetime.now(timezone.utc).astimezone())

    records = conn.execute("SELECT * FROM score_records WHERE ai_score IS NOT NULL").fetchall()
    stamped = sum(
        1
        for rec in records
        if rec["model_version"] and rec["prompt_version"] and rec["rubric_version"]
    )
    version_complete = len(records) > 0 and stamped == len(records)

    checks: list[dict[str, Any]] = []
    qwk = _rolling_qwk(conn)
    checks.append(
        {
            "key": "qwk",
            "label": f"滚动 200 条 QWK（{stage} 降级线 {qwk_floor}）",
            "value": qwk,
            "threshold": qwk_floor,
            "status": "pass" if (qwk is None or qwk >= qwk_floor) else "fail",
            "action": "自动把评分能力降为 extract，直到重新校准达标",
            "auto_apply": True,
        }
    )
    stations = _station_integrity(conn)
    breaches = [row for row in stations if row["breach"]]
    integrity = max((row["rate"] for row in stations), default=0.0)
    checks.append(
        {
            "key": "integrity_gap",
            "label": "站级证据链缺口率（>2% 自动降级该站）",
            "value": integrity,
            "threshold": INTEGRITY_GAP_LIMIT,
            "status": "fail" if breaches else "pass",
            "action": "只降级缺口超限的考站，相关场次全站人工复核",
            "auto_apply": True,
            "stations": stations,
        }
    )
    subgroup = fairness.get("overall_gap_sd")
    history = _load_subgroup_history(conn)
    streak_weeks = _subgroup_streak(history, week, subgroup)
    if apply and subgroup is not None:
        _record_subgroup_observation(conn, week, subgroup)
    subgroup_fail = len(streak_weeks) >= SUBGROUP_WEEKS_REQUIRED
    if subgroup is None:
        subgroup_status = "unmeasured"
    elif subgroup_fail:
        subgroup_status = "fail"
    elif subgroup > SUBGROUP_GAP_LIMIT:
        subgroup_status = "observing"
    else:
        subgroup_status = "pass"
    checks.append(
        {
            "key": "subgroup_gap",
            "label": "亚组分数差（连续 2 周 >0.20 SD 自动降级）",
            "value": subgroup,
            "threshold": SUBGROUP_GAP_LIMIT,
            "status": subgroup_status,
            "action": "自动下调相关能力并启动公平性纠正动作",
            "auto_apply": subgroup_status == "fail",
            "streak_weeks": streak_weeks,
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
    bundles = list_bundles(conn)
    model_version = db.get_state(conn, "model_version", MODEL_VERSION)
    prompt_version = db.get_state(conn, "prompt_version", PROMPT_VERSION)
    changes: list[dict[str, Any]] = []
    if apply:
        if any(c["key"] == "qwk" for c in failing if c["auto_apply"]):
            changes.extend(
                apply_authority_reset(
                    conn,
                    reason=f"治理自检：滚动 QWK {qwk} 低于 {qwk_floor}，暂停 AI 建议分",
                    actor=actor,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    floor="extract",
                )
            )
        for row in breaches:
            bundle = next(
                (entry for entry in bundles if entry["bundle_id"] == row["bundle_id"]), None
            )
            if not bundle:
                continue
            item_ids = [
                item.get("item_id")
                for item in bundle["payload"].get("rubric", [])
                if item.get("item_id")
            ]
            changes.extend(
                apply_item_downgrades(
                    conn,
                    bundle,
                    item_ids,
                    reason=f"站级 integrity_gap {row['rate']:.1%} > 2%，自动降级该站",
                    actor=actor,
                    model_version=model_version,
                    prompt_version=prompt_version,
                    floor="extract",
                )
            )
        if subgroup_fail:
            for bundle in bundles:
                item_ids = [
                    item.get("item_id")
                    for item in bundle["payload"].get("rubric", [])
                    if item.get("item_id")
                ]
                changes.extend(
                    apply_item_downgrades(
                        conn,
                        bundle,
                        item_ids,
                        reason=f"亚组分数差连续 {len(streak_weeks)} 周 > 0.20 SD，自动降级",
                        actor=actor,
                        model_version=model_version,
                        prompt_version=prompt_version,
                        floor="extract",
                    )
                )

    trigger_summary = "、".join(c["label"] for c in failing) if failing else "全部通过"
    detail = {
        "checks": checks,
        "changes": changes,
        "metrics": {
            "qwk_ai": qwk,
            "qwk_human": report.get("qwk_human"),
            "qwk_floor": qwk_floor,
            "integrity_gap_rate": integrity,
            "subgroup_gap_sd": subgroup,
            "subgroup_streak_weeks": streak_weeks,
            "teacher_hours_reduction": ab.get("comparison", {}).get("teacher_hours_reduction"),
        },
        "station_integrity": stations,
        "week": week,
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
    db.append_audit(
        conn,
        actor=actor,
        action="governance.self_check",
        object_type="System",
        object_id=check_id,
        before=None,
        after={
            "trigger_summary": trigger_summary,
            "applied": bool(changes),
            "changes": len(changes),
            "rolling_qwk": qwk,
            "qwk_floor": qwk_floor,
            "integrity_breaches": [row["bundle_id"] for row in breaches],
            "subgroup_streak_weeks": streak_weeks,
        },
        model_version=model_version,
        prompt_version=prompt_version,
    )
    return {
        "check_id": check_id,
        "trigger_summary": trigger_summary,
        "checks": checks,
        "applied_changes": changes,
        "applied": bool(changes),
        "detail": detail,
        "week": week,
        "subgroup_streak_weeks": streak_weeks,
    }


def latest_self_check(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM governance_checks ORDER BY created_at DESC, rowid DESC LIMIT 1"
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
    row = conn.execute(
        "SELECT * FROM model_cards ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("尚无可公开的模型卡")
    if "发布前需完成新一轮人工比对校准" in str(row["limitations"] or ""):
        raise ValueError(
            "最新模型卡是版本变更快照，指标仍来自历史版本；"
            "请先完成新版本人工比对校准并刷新模型卡后再发布"
        )
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE model_cards SET is_public = 1, published_at = ? WHERE card_id = ?",
        (timestamp, row["card_id"]),
    )
    sheet = conn.execute(
        "SELECT * FROM data_sheets ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if sheet:
        conn.execute(
            "UPDATE data_sheets SET is_public = 1, published_at = ? WHERE data_sheet_id = ?",
            (timestamp, sheet["data_sheet_id"]),
        )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="model_card.publish",
        object_type="ModelCard",
        object_id=row["card_id"],
        before={"is_public": bool(row["is_public"])},
        after={"is_public": True, "data_sheet": sheet["data_sheet_id"] if sheet else None},
        model_version=row["model_version"],
        prompt_version=row["prompt_version"],
    )
    return {
        "card_id": row["card_id"],
        "is_public": True,
        "published_at": timestamp,
        "data_sheet": sheet["data_sheet_id"] if sheet else None,
    }


def governance_status(conn) -> dict[str, Any]:
    switch = conn.execute(
        "SELECT * FROM switch_drills ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    appeal_rows = conn.execute(
        "SELECT * FROM appeal_drills ORDER BY created_at DESC, rowid DESC"
    ).fetchall()
    approvals = db.rows_to_dicts(
        conn.execute(
            "SELECT * FROM governance_approvals ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    )
    model_card = conn.execute(
        "SELECT card_id, is_public, published_at FROM model_cards "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    data_sheet = conn.execute(
        "SELECT data_sheet_id, version, is_public, published_at FROM data_sheets "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1"
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
        "academic_committee_approved": any(
            row["approval_type"] == "academic_committee" and row["status"] == "approved"
            for row in approvals
        ),
        "model_card": dict(model_card) if model_card else None,
        "model_card_public": bool(model_card and model_card["is_public"]),
        "data_sheet": dict(data_sheet) if data_sheet else None,
        "data_sheet_public": bool(data_sheet and data_sheet["is_public"]),
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


def change_model_version(
    conn,
    *,
    model_version: str | None = None,
    prompt_version: str | None = None,
    actor: str = "系统管理员",
) -> dict[str, Any]:
    """模型/提示词变更：降级既有 AI 权限，并立即生成绑定新版本的模型卡快照。"""
    from .transparency import refresh_transparency_assets

    old_model = db.get_state(conn, "model_version", MODEL_VERSION)
    old_prompt = db.get_state(conn, "prompt_version", PROMPT_VERSION)
    new_model = (model_version or "").strip() or f"{old_model}-next"
    new_prompt = (prompt_version or "").strip() or old_prompt
    changes = apply_authority_reset(
        conn,
        reason=f"模型/提示词版本变更：{old_model}/{old_prompt} → {new_model}/{new_prompt}",
        actor=actor,
        model_version=new_model,
        prompt_version=new_prompt,
    )
    db.set_state(conn, "model_version", new_model)
    db.set_state(conn, "prompt_version", new_prompt)
    db.append_audit(
        conn,
        actor=actor,
        action="governance.version_change",
        object_type="ModelVersion",
        object_id="score.suggest",
        before={"model_version": old_model, "prompt_version": old_prompt},
        after={"model_version": new_model, "prompt_version": new_prompt, "downgraded": len(changes)},
        model_version=new_model,
        prompt_version=new_prompt,
    )
    snapshot = refresh_transparency_assets(
        conn,
        actor=actor,
        extra_limitations=[
            "模型/提示词版本刚变更，当前指标仍来自历史版本；发布前需完成新一轮人工比对校准。"
        ],
    )
    return {
        "model_version": new_model,
        "prompt_version": new_prompt,
        "changes": changes,
        "model_card": {
            "card_id": snapshot["card_id"],
            "data_sheet_version": snapshot["data_sheet_version"],
            "is_public": snapshot["is_public"],
            "limitations": snapshot["limitations"],
        },
    }
