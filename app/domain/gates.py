"""闸门式放行与自动降级规则。"""
from __future__ import annotations

import json
from typing import Any

GATES = [
    {
        "id": "G0",
        "name": "P0 离线副驾",
        "stage": "G0_offline",
        "scope": "历史录像，不接触在考考生",
        "criteria": [
            ("qwk_ai", "AI-人 QWK", "≥ 0.70", 0.70, "ge"),
            ("evidence_rate", "证据定位准确率", "≥ 90%", 0.90, "ge"),
            ("avg_review_seconds", "单条复核耗时", "≤ 90 秒", 90, "le"),
        ],
    },
    {
        "id": "G1",
        "name": "练习沙箱",
        "stage": "G1_practice",
        "scope": "自愿学生，不计入成绩",
        "criteria": [
            ("sp_fact_error", "SP 事实错误率", "≤ 2%", 0.02, "le"),
            ("persona_pass", "人格首轮通过率", "≥ 95%", 0.95, "ge"),
            ("p95_latency", "语音延迟 P95", "≤ 1.5 秒", 1.5, "le"),
            ("sus", "SUS 可用性", "≥ 70", 70, "ge"),
            ("p0_incidents", "P0 安全事故", "= 0", 0, "eq"),
        ],
    },
    {
        "id": "G2",
        "name": "模拟考",
        "stage": "G2_simulation",
        "scope": "整班模拟考 + A/B 对照",
        "criteria": [
            ("qwk_ai", "AI-人 QWK", "≥ 0.75", 0.75, "ge"),
            ("near_improvement", "边缘案例一致率提升", "≥ 10pp", 0.10, "ge"),
            ("edit_rate", "人工编辑率", "≤ 30%", 0.30, "le"),
            ("subgroup_gap", "亚组分数差", "≤ 0.15 SD", 0.15, "le"),
            ("teacher_hours", "教师评分工时下降", "≥ 40%", 0.40, "ge"),
            ("audit_replay", "审计链可回放", "100%", 1.0, "ge"),
        ],
    },
    {
        "id": "G3",
        "name": "有限高利害",
        "stage": "G3_limited",
        "scope": "行为锚清晰的非关键项",
        "criteria": [
            ("qwk_ai", "AI-人 QWK", "≥ 0.80", 0.80, "ge"),
            ("critical_miss", "关键项漏判", "= 0", 0, "eq"),
            ("switch_drill", "终止开关演练", "通过", 1.0, "ge"),
            ("appeal_drill", "申诉演练", "≥ 2 次", 2, "ge"),
            ("ethics", "伦理与合规批准", "通过", 1.0, "ge"),
            ("model_card", "模型卡公开", "通过", 1.0, "ge"),
        ],
    },
]


def _metric_values(conn, report: dict[str, Any]) -> dict[str, Any]:
    attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts").fetchall()]
    records = [dict(r) for r in conn.execute("SELECT * FROM score_records").fetchall()]
    bundle_payloads = {}
    for row in conn.execute("SELECT bundle_id, payload FROM bundles").fetchall():
        bundle_payloads[row["bundle_id"]] = json.loads(row["payload"])
    item_map: dict[tuple[str, str], dict[str, Any]] = {}
    for bundle_id, payload in bundle_payloads.items():
        for item in payload.get("rubric", []):
            item_map[(bundle_id, item["item_id"])] = item
    attempt_bundle = {a["attempt_id"]: a["bundle_id"] for a in attempts}

    critical_miss = 0
    version_stamped = 0
    ai_records = 0
    for rec in records:
        if rec["ai_score"] is None:
            continue
        ai_records += 1
        if rec["model_version"] and rec["prompt_version"] and rec["rubric_version"]:
            version_stamped += 1
        item = item_map.get((attempt_bundle.get(rec["attempt_id"]), rec["item_id"]))
        if item and item.get("critical") and rec["ai_score"] == 0 and (rec["gold_score"] or 0) > 0:
            critical_miss += 1

    model_card_count = conn.execute("SELECT COUNT(*) AS n FROM model_cards").fetchone()["n"]
    version_complete = version_stamped == ai_records and ai_records > 0
    return {
        "qwk_ai": report.get("qwk_ai"),
        "evidence_rate": (report.get("evidence") or {}).get("rate"),
        "avg_review_seconds": (report.get("review") or {}).get("avg_seconds"),
        "edit_rate": (report.get("review") or {}).get("edit_rate"),
        "critical_miss": critical_miss,
        "audit_replay": 1.0 if version_complete and report.get("counts", {}).get("records", 0) > 0 else None,
        "model_card": 1.0 if model_card_count > 0 else None,
        "sp_fact_error": None,
        "persona_pass": None,
        "p95_latency": None,
        "sus": None,
        "p0_incidents": 0,
        "near_improvement": None,
        "subgroup_gap": None,
        "teacher_hours": None,
        "switch_drill": None,
        "appeal_drill": None,
        "ethics": None,
    }


def evaluate_gates(conn, report: dict[str, Any], current_stage: str) -> dict[str, Any]:
    values = _metric_values(conn, report)
    gates: list[dict[str, Any]] = []
    stage_order = [g["stage"] for g in GATES]
    current_index = stage_order.index(current_stage) if current_stage in stage_order else 0

    for idx, gate in enumerate(GATES):
        rows = []
        for key, label, target_text, target, direction in gate["criteria"]:
            value = values.get(key)
            if value is None:
                status = "unmeasured"
            elif direction == "ge":
                status = "pass" if float(value) >= target else "fail"
            elif direction == "le":
                status = "pass" if float(value) <= target else "fail"
            else:
                status = "pass" if float(value) == float(target) else "fail"
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "target": target_text,
                    "value": value,
                    "status": status,
                }
            )
        if any(r["status"] == "fail" for r in rows):
            overall = "fail"
        elif all(r["status"] == "pass" for r in rows):
            overall = "pass"
        else:
            overall = "incomplete"
        gates.append(
            {
                "id": gate["id"],
                "name": gate["name"],
                "stage": gate["stage"],
                "scope": gate["scope"],
                "status": overall,
                "is_current": gate["stage"] == current_stage,
                "is_locked": idx > current_index,
                "criteria": rows,
            }
        )

    alerts: list[dict[str, Any]] = []
    qwk = values.get("qwk_ai")
    if qwk is not None and qwk < 0.65:
        alerts.append(
            {
                "level": "danger",
                "title": "QWK 跌破自动降级线",
                "detail": f"当前 QWK {qwk}，低于 0.70 − 0.05，应自动降级并暂停 AI 建议分。",
            }
        )
    if report.get("integrity_gap_rate", 0) > 0.02:
        alerts.append(
            {
                "level": "danger",
                "title": "证据链缺口比例超限",
                "detail": f"integrity_gap 比例 {report['integrity_gap_rate']:.1%} > 2%，相关场次应全站人工复核。",
            }
        )
    overrides = conn.execute("SELECT COUNT(*) AS n FROM authority_overrides").fetchone()["n"]
    if overrides:
        alerts.append(
            {
                "level": "warn",
                "title": "存在权限自动降级",
                "detail": f"{overrides} 个评分项的 AI 权限已被重置，需重新完成人工比对校准后回升。",
            }
        )
    return {
        "current_stage": current_stage,
        "gates": gates,
        "alerts": alerts,
        "metric_values": values,
    }
