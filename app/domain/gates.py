"""闸门式放行与自动降级规则。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .. import db
from .ab import ab_report
from .bundle import list_bundles
from .consent import consent_status
from .fairness import fairness_report
from .sp_approval import persona_approval_status
from .spqa import RED_TEAM_VERSION
from .stats import quadratic_weighted_kappa
from .sus import sus_summary

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
            ("sp_consent", "SP 知情同意", "= 已签署", 1.0, "ge"),
            ("sp_persona_approval", "SP 人格卡人工批准", "= 已批准", 1.0, "ge"),
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
            ("academic_committee", "A/B 报告学术委员会审议", "通过", 1.0, "ge"),
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
            ("critical_streak", "连续零关键项漏判", "≥ 3 场", 3, "ge"),
            ("switch_drill", "终止开关演练", "通过", 1.0, "ge"),
            ("appeal_drill", "申诉演练", "≥ 2 次", 2, "ge"),
            ("ethics", "伦理与合规批准", "通过", 1.0, "ge"),
            ("model_card", "模型卡与数据表公开", "通过", 1.0, "ge"),
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
    records_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        records_by_attempt.setdefault(rec["attempt_id"], []).append(rec)
        if rec["ai_score"] is None:
            continue
        ai_records += 1
        if rec["model_version"] and rec["prompt_version"] and rec["rubric_version"]:
            version_stamped += 1
        item = item_map.get((attempt_bundle.get(rec["attempt_id"]), rec["item_id"]))
        if item and item.get("critical") and rec["ai_score"] == 0 and (rec["gold_score"] or 0) > 0:
            critical_miss += 1

    critical_streak = 0
    for attempt in sorted(attempts, key=lambda entry: entry["created_at"], reverse=True):
        payload = bundle_payloads.get(attempt["bundle_id"]) or {}
        critical_ids = {
            item["item_id"] for item in payload.get("rubric", []) if item.get("critical")
        }
        reviewed = [
            record
            for record in records_by_attempt.get(attempt["attempt_id"], [])
            if record["item_id"] in critical_ids and record["gold_score"] is not None
        ]
        if not reviewed:
            continue
        missed = any(
            record["ai_score"] == 0 and (record["gold_score"] or 0) > 0 for record in reviewed
        )
        if missed:
            break
        critical_streak += 1

    public_card_count = conn.execute(
        "SELECT COUNT(*) AS n FROM model_cards WHERE is_public = 1"
    ).fetchone()["n"]
    public_sheet_count = conn.execute(
        "SELECT COUNT(*) AS n FROM data_sheets WHERE is_public = 1"
    ).fetchone()["n"]
    version_complete = version_stamped == ai_records and ai_records > 0
    sus = sus_summary(conn)
    if sus["count"]:
        sus_score = sus["average"]
        sus_source = "responses"
    else:
        sus_row = conn.execute("SELECT value FROM system_state WHERE key = 'sus_score'").fetchone()
        sus_score = float(sus_row["value"]) if sus_row else None
        sus_source = "legacy" if sus_row else None
    switch_row = conn.execute(
        "SELECT passed FROM switch_drills ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    switch_drill = 1.0 if switch_row and switch_row["passed"] else 0.0
    appeal_drill = conn.execute(
        "SELECT COUNT(*) AS n FROM appeal_drills WHERE passed = 1"
    ).fetchone()["n"]
    ethics_row = conn.execute(
        "SELECT status FROM governance_approvals WHERE approval_type = 'ethics_and_data'"
    ).fetchone()
    ethics = 1.0 if ethics_row and ethics_row["status"] == "approved" else 0.0
    committee_row = conn.execute(
        "SELECT status FROM governance_approvals WHERE approval_type = 'academic_committee'"
    ).fetchone()
    academic_committee = 1.0 if committee_row and committee_row["status"] == "approved" else 0.0
    model_card_public = 1.0 if public_card_count > 0 and public_sheet_count > 0 else 0.0
    bundles = list_bundles(conn)
    current_bundle = bundles[0] if bundles else None
    sp_consent = consent_status(conn, current_bundle["bundle_id"]) if current_bundle else None
    sp_consent_value = None if sp_consent is None else (1.0 if sp_consent["valid"] else 0.0)
    persona_approval = persona_approval_status(conn, current_bundle) if current_bundle else None
    persona_approval_value = (
        None if persona_approval is None else (1.0 if persona_approval["approved"] else 0.0)
    )
    sp_cert_issue = None
    cert_row = None
    if current_bundle:
        cert_row = conn.execute(
            "SELECT report, persona_version, valid_until FROM sp_certifications WHERE bundle_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (current_bundle["bundle_id"],),
        ).fetchone()
        if not cert_row:
            sp_cert_issue = "missing"
        elif cert_row["persona_version"] != current_bundle["bundle_hash"]:
            sp_cert_issue = "stale"
            cert_row = None
        else:
            expires = None
            if cert_row["valid_until"]:
                try:
                    expires = datetime.fromisoformat(cert_row["valid_until"])
                except ValueError:
                    expires = None
            if expires is None:
                sp_cert_issue = "expired"
                cert_row = None
            else:
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= datetime.now(timezone.utc):
                    sp_cert_issue = "expired"
                    cert_row = None
    sp_cert = json.loads(cert_row["report"]) if cert_row else None
    if sp_cert and sp_cert.get("red_team_version") != RED_TEAM_VERSION:
        sp_cert_issue = "case_stale"
        sp_cert = None
    sp_fact_error = None
    sp_persona_pass = None
    sp_p95_latency = None
    sp_p0 = None
    if sp_cert:
        red_team = sp_cert.get("red_team", {})
        sp_fact_error = red_team.get("fact_error_rate")
        sp_persona_pass = red_team.get("persona_first_pass_rate")
        p95_ms = red_team.get("p95_latency_ms")
        sp_p95_latency = round(p95_ms / 1000, 3) if p95_ms is not None else None
        sp_p0 = red_team.get("p0_incidents")
    ab = ab_report(conn)
    fairness = fairness_report(conn)
    chain = db.verify_audit_chain(conn)
    rolling_rows = conn.execute(
        "SELECT ai_score, gold_score FROM score_records "
        "WHERE ai_score IS NOT NULL AND gold_score IS NOT NULL "
        "ORDER BY created_at DESC, rowid DESC LIMIT 200"
    ).fetchall()
    rolling_qwk = quadratic_weighted_kappa(
        [row["ai_score"] for row in rolling_rows],
        [row["gold_score"] for row in rolling_rows],
    )
    return {
        "qwk_ai": report.get("qwk_ai"),
        "rolling_qwk": rolling_qwk,
        "evidence_rate": (report.get("evidence") or {}).get("rate"),
        "avg_review_seconds": (report.get("review") or {}).get("avg_seconds"),
        "edit_rate": (report.get("review") or {}).get("edit_rate"),
        "critical_miss": critical_miss,
        "critical_streak": critical_streak,
        "audit_replay": (
            0.0
            if not chain.get("ok")
            else (
                1.0
                if version_complete and report.get("counts", {}).get("records", 0) > 0
                else None
            )
        ),
        "sp_fact_error": sp_fact_error,
        "persona_pass": sp_persona_pass,
        "p95_latency": sp_p95_latency,
        "sus": sus_score,
        "sus_source": sus_source,
        "sp_consent": sp_consent_value,
        "sp_persona_approval": persona_approval_value,
        "p0_incidents": sp_p0,
        "sp_cert_issue": sp_cert_issue,
        "near_improvement": ab.get("comparison", {}).get("near_improvement"),
        "subgroup_gap": fairness.get("overall_gap_sd"),
        "teacher_hours": ab.get("comparison", {}).get("teacher_hours_reduction"),
        "academic_committee": academic_committee,
        "switch_drill": switch_drill,
        "appeal_drill": appeal_drill,
        "ethics": ethics,
        "model_card": model_card_public,
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
    chain = db.verify_audit_chain(conn)
    if not chain.get("ok"):
        alerts.append(
            {
                "level": "danger",
                "title": "审计链校验失败",
                "detail": f"{chain.get('message')}（断点 {chain.get('broken_at')}），G2 审计回放不得放行。",
            }
        )
    qwk = values.get("qwk_ai")
    if qwk is not None and qwk < 0.65:
        alerts.append(
            {
                "level": "danger",
                "title": "QWK 跌破自动降级线",
                "detail": f"当前 QWK {qwk}，低于 0.70 − 0.05，应自动降级并暂停 AI 建议分。",
            }
        )
    rolling_qwk = values.get("rolling_qwk")
    if rolling_qwk is not None and rolling_qwk < 0.65:
        alerts.append(
            {
                "level": "danger",
                "title": "滚动 200 条 QWK 跌破降级线",
                "detail": f"最近 200 条有金标准的记录 QWK {rolling_qwk}，低于 0.70 − 0.05，需立即降级。",
            }
        )
    subgroup_gap = values.get("subgroup_gap")
    if subgroup_gap is not None and subgroup_gap > 0.20:
        alerts.append(
            {
                "level": "danger",
                "title": "亚组分数差超过 0.20 SD",
                "detail": f"当前最大亚组差 {subgroup_gap} SD；连续超标应自动降级并复核该组边缘案例。",
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
    edit_rate = values.get("edit_rate")
    reviewed = (report.get("review") or {}).get("reviewed") or 0
    if edit_rate is not None and reviewed >= 20 and edit_rate < 0.05:
        alerts.append(
            {
                "level": "warn",
                "title": "人工编辑率过低，警惕“橡皮图章”",
                "detail": (
                    f"当前编辑率 {edit_rate:.1%}（复核 {reviewed} 条），低于 5%；"
                    "人工复核可能已形式化，建议检查默认选中、一键通过或批量确认等界面诱导。"
                ),
            }
        )
    review_share_rows = (report.get("review_share") or {}).get("by_risk") or []
    for row in review_share_rows:
        target = row.get("target")
        if target is None:
            continue
        share = float(row.get("share") or 0)
        level = row.get("risk_level")
        if share >= 0.9:
            alerts.append(
                {
                    "level": "danger",
                    "title": f"{level} 人工复核占比 {share:.0%}，风险路由可能失效",
                    "detail": (
                        f"{row.get('reviewed', 0)}/{row.get('attempts', 0)} 场次进入人工复核，"
                        "接近全人工；请检查路由阈值与自动通过条件，避免退化为全量人工。"
                    ),
                }
            )
        elif share > float(target):
            alerts.append(
                {
                    "level": "warn",
                    "title": f"{level} 人工复核占比 {share:.0%}，超过目标 {float(target):.0%}",
                    "detail": "复核占比偏高会侵蚀 AI 提效收益；建议定位低置信项与路由命中原因。",
                }
            )
    appeals = report.get("appeals") or {}
    appeal_rate = appeals.get("appeal_rate")
    if appeal_rate is not None and appeal_rate > float(appeals.get("target_appeal_rate") or 0.03):
        alerts.append(
            {
                "level": "warn",
                "title": f"申诉率 {appeal_rate:.1%} 超过目标 {float(appeals.get('target_appeal_rate') or 0.03):.0%}",
                "detail": "申诉率偏高可能指向评分一致性或反馈质量问题；建议复盘申诉理由码与改判项。",
            }
        )
    change_rate = appeals.get("change_rate")
    if change_rate is not None and change_rate > float(appeals.get("target_change_rate") or 0.20):
        alerts.append(
            {
                "level": "warn",
                "title": f"改判率 {change_rate:.1%} 超过目标 {float(appeals.get('target_change_rate') or 0.20):.0%}",
                "detail": "改判率偏高说明初评存在系统性偏差；建议检查行为锚、评分员校准与 AI 建议校准。",
            }
        )
    if values.get("sp_cert_issue") == "missing":
        alerts.append(
            {
                "level": "danger",
                "title": "当前站包尚未完成 SP 认证",
                "detail": "站包改版或首次部署后必须重新运行红队与一致性抽测；G1 的 SP 指标按未采集处理。",
            }
        )
    elif values.get("sp_cert_issue") == "stale":
        alerts.append(
            {
                "level": "danger",
                "title": "SP 认证未覆盖当前 persona_version",
                "detail": "认证仍绑定旧 persona_version，G1 的 SP 指标按未采集处理；需重新运行红队与一致性抽测。",
            }
        )
    elif values.get("sp_cert_issue") == "expired":
        alerts.append(
            {
                "level": "danger",
                "title": "SP 认证已过期",
                "detail": "认证有效期已结束，G1 的 SP 指标按未采集处理；需按季度更新红队用例并重新认证。",
            }
        )
    elif values.get("sp_cert_issue") == "case_stale":
        alerts.append(
            {
                "level": "danger",
                "title": "SP 认证未覆盖当前红队用例版本",
                "detail": (
                    f"红队攻击用例已更新到 {RED_TEAM_VERSION}，旧认证不再有效；"
                    "G1 的 SP 指标按未采集处理，需重新运行红队与一致性抽测。"
                ),
            }
        )
    if values.get("sp_consent") == 0.0:
        bundle_row = conn.execute(
            "SELECT bundle_id FROM bundles ORDER BY frozen_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        consent = consent_status(conn, bundle_row["bundle_id"]) if bundle_row else None
        missing_labels = (
            [consent["type_labels"].get(key, key) for key in consent["missing"] + consent["expired"]]
            if consent
            else ["SP 知情同意"]
        )
        alerts.append(
            {
                "level": "danger",
                "title": "当前站包缺少有效 SP 知情同意",
                "detail": "缺少或过期：" + "、".join(missing_labels) + "；签署后才能开放 G1 练习场景。",
            }
        )
    if values.get("sp_persona_approval") == 0.0:
        bundles_now = list_bundles(conn)
        approval_status = persona_approval_status(conn, bundles_now[0]) if bundles_now else None
        if approval_status and approval_status["stale"]:
            title = "SP 人格卡批准未覆盖当前 persona_version"
            detail = "最新批准绑定旧人格卡版本；改版后必须重新确认披露阈值、禁用话术与试演结论。"
        elif approval_status and (approval_status.get("approval") or {}).get("decision") == "rejected":
            title = "SP 人格卡被考官拒绝"
            detail = "当前人格卡未获批准，不得开放 SP 练习或模拟考；请按拒绝意见修改后重新试演。"
        else:
            title = "SP 人格卡尚未获得人工批准"
            detail = "需要考官确认披露阈值、禁用话术并完成真人试演后才能开放 SP 练习。"
        alerts.append({"level": "danger", "title": title, "detail": detail})
    if values.get("sus_source") == "legacy":
        alerts.append(
            {
                "level": "warn",
                "title": "SUS 仍为旧版配置值",
                "detail": "当前未采集真实 SUS 问卷，G1 使用历史配置值；请在质量页录入至少一份 10 项问卷。",
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
