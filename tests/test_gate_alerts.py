"""闸门告警：人工编辑率、复核占比、申诉/改判率与加固校验。"""
from __future__ import annotations

from app import db
from app.domain.dataflow import record_data_flow_review
from app.domain.gates import _g2_metric_issues, _technical_metric_issues, evaluate_gates


def _conn(tmp_path):
    conn = db.connect(tmp_path / "gate_alert.db")
    db.init_db(conn)
    return conn


def _report(edit_rate, reviewed):
    return {
        "qwk_ai": None,
        "evidence": {"rate": None},
        "review": {"edit_rate": edit_rate, "reviewed": reviewed, "avg_seconds": None},
        "counts": {"records": 0},
        "integrity_gap_rate": 0,
    }


def test_low_edit_rate_with_enough_reviews_alerts(tmp_path):
    conn = _conn(tmp_path)
    gates = evaluate_gates(conn, _report(0.02, 40), "G0_offline")
    assert any("橡皮图章" in alert["title"] for alert in gates["alerts"])
    conn.close()


def test_healthy_edit_rate_does_not_alert(tmp_path):
    conn = _conn(tmp_path)
    gates = evaluate_gates(conn, _report(0.22, 40), "G0_offline")
    assert not any("橡皮图章" in alert["title"] for alert in gates["alerts"])
    assert not any("超过 30%" in alert["title"] for alert in gates["alerts"])
    conn.close()


def test_high_edit_rate_alerts(tmp_path):
    conn = _conn(tmp_path)
    gates = evaluate_gates(conn, _report(0.35, 40), "G0_offline")
    assert any("人工编辑率 35.0% 超过 30%" in alert["title"] for alert in gates["alerts"])
    conn.close()


def test_low_edit_rate_with_small_sample_does_not_alert(tmp_path):
    conn = _conn(tmp_path)
    gates = evaluate_gates(conn, _report(0.0, 5), "G0_offline")
    assert not any("橡皮图章" in alert["title"] for alert in gates["alerts"])
    conn.close()


def test_tampered_audit_chain_fails_replay_and_alerts(tmp_path):
    conn = _conn(tmp_path)
    db.append_audit(
        conn,
        actor="测试",
        action="test.event",
        object_type="TestObject",
        object_id="t1",
        after={"value": 1},
    )
    conn.execute("UPDATE audit_events SET actor = 'tampered'")
    conn.commit()
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    assert gates["metric_values"]["audit_replay"] == 0.0
    assert any("审计链" in alert["title"] for alert in gates["alerts"])
    conn.close()


def test_external_model_endpoint_alerts_at_g3(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.domain.gates.data_flow_status",
        lambda: {
            "flows": [
                {
                    "kind": "scoring",
                    "label": "评分模型",
                    "host": "api.example.com",
                    "scope": "public",
                    "external": True,
                    "allowed": False,
                }
            ],
            "external": [{"label": "评分模型", "host": "api.example.com"}],
            "compliant": False,
        },
    )
    conn = _conn(tmp_path)
    gates = evaluate_gates(conn, _report(0.2, 40), "G3_limited")
    alert = next(item for item in gates["alerts"] if "端点不在内网" in item["title"])
    assert alert["level"] == "danger"
    assert "api.example.com" in alert["detail"]
    conn.close()


def test_confirmed_content_finding_counts_as_p0(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO content_audit_findings(finding_id, attempt_id, rule, severity, role, label, "
        "quote, start_ms, status, reviewed_by, reviewed_at, review_note, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "caf_p0",
            "att_p0",
            "sp_diagnosis",
            "high",
            "patient",
            "SP 给出诊断或病情结论",
            "你得了心梗。",
            0,
            "confirmed",
            "auditor",
            db.now_iso(),
            "确认越界，回写红队用例",
            db.now_iso(),
        ),
    )
    conn.commit()
    gates = evaluate_gates(conn, _report(0.2, 40), "G1_practice")
    g1 = next(gate for gate in gates["gates"] if gate["id"] == "G1")
    p0 = next(row for row in g1["criteria"] if row["key"] == "p0_incidents")
    assert p0["value"] == 1
    assert p0["status"] == "fail"
    assert any("P0 安全事件" in alert["title"] for alert in gates["alerts"])
    conn.close()


def test_missing_data_flow_review_alerts_until_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.domain.gates.data_flow_status",
        lambda: {"flows": [], "external": [], "compliant": True},
    )
    conn = _conn(tmp_path)
    first = evaluate_gates(conn, _report(0.2, 40), "G3_limited")
    assert any("数据流向年度复核" in alert["title"] for alert in first["alerts"])

    record_data_flow_review(conn, actor="test", note="年度复核")
    second = evaluate_gates(conn, _report(0.2, 40), "G3_limited")
    assert not any("数据流向年度复核" in alert["title"] for alert in second["alerts"])
    conn.close()


def test_rolling_qwk_and_subgroup_gap_alerts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr("app.domain.gates.quadratic_weighted_kappa", lambda a, b: 0.5)
    monkeypatch.setattr(
        "app.domain.gates.fairness_report",
        lambda conn, bundle_id=None: {"overall_gap_sd": 0.25},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("滚动 200 条" in title for title in titles)
    assert any("亚组分数差" in title for title in titles)
    conn.close()


def test_sp_runtime_degraded_alerts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.sp_runtime_summary",
        lambda conn: {"turns": 12, "degraded": 2, "degradation_rate": 0.1667},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("SP 运行时发生 2 次安全回退" in title for title in titles)
    conn.close()


def test_sp_runtime_without_degraded_does_not_alert(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.sp_runtime_summary",
        lambda conn: {"turns": 12, "degraded": 0, "degradation_rate": 0.0},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert not any("SP 运行时发生" in title for title in titles)
    conn.close()


def test_sus_between_gate_and_experience_target_warns(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.sus_summary", lambda conn: {"count": 6, "average": 72.0}
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("SUS 72.0 低于体验目标 75" in title for title in titles)
    assert not any("< 70" in title for title in titles)
    conn.close()


def test_subgroup_gap_between_target_and_degrade_warns(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.fairness_report",
        lambda conn, bundle_id=None: {"overall_gap_sd": 0.18},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("0.18 SD 超过 0.15 SD" in title for title in titles)
    assert not any("超过 0.20 SD" in title for title in titles)
    conn.close()


def test_subgroup_gap_above_degrade_line_only_alerts_danger(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.fairness_report",
        lambda conn, bundle_id=None: {"overall_gap_sd": 0.26},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("超过 0.20 SD" in title for title in titles)
    assert not any("0.15 SD" in title for title in titles)
    conn.close()


def test_review_share_and_appeal_alerts(tmp_path):
    conn = _conn(tmp_path)
    report = _report(0.2, 40)
    report["review_share"] = {
        "by_risk": [
            {"risk_level": "L2", "attempts": 10, "reviewed": 4, "share": 0.4, "target": 0.35},
            {"risk_level": "L3", "attempts": 10, "reviewed": 9, "share": 0.9, "target": 0.6},
        ]
    }
    report["appeals"] = {
        "appeal_rate": 0.05,
        "change_rate": 0.25,
        "target_appeal_rate": 0.03,
        "target_change_rate": 0.20,
    }
    gates = evaluate_gates(conn, report, "G3_limited")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("L2 人工复核占比" in title and "超过目标" in title for title in titles)
    assert any("L3 人工复核占比" in title and "风险路由可能失效" in title for title in titles)
    assert any("申诉率" in title for title in titles)
    assert any("改判率" in title for title in titles)
    conn.close()


def test_ops_metrics_within_targets_do_not_alert(tmp_path):
    conn = _conn(tmp_path)
    report = _report(0.2, 40)
    report["review_share"] = {
        "by_risk": [
            {"risk_level": "L2", "attempts": 10, "reviewed": 2, "share": 0.2, "target": 0.35},
        ]
    }
    report["appeals"] = {
        "appeal_rate": 0.01,
        "change_rate": 0.1,
        "target_appeal_rate": 0.03,
        "target_change_rate": 0.20,
    }
    gates = evaluate_gates(conn, report, "G3_limited")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert not any("人工复核占比" in title for title in titles)
    assert not any("申诉率" in title for title in titles)
    assert not any("改判率" in title for title in titles)
    conn.close()


def test_cost_over_target_alerts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.ai_cost_summary",
        lambda conn: {
            "priced_calls": 3,
            "targets": [
                {
                    "key": "exam",
                    "label": "考试",
                    "attempts": 3,
                    "avg_cny": 72.0,
                    "target_cny": 60.0,
                    "status": "fail",
                }
            ],
        },
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("考试单站成本 ¥72.00 超过目标 ¥60" in title for title in titles)
    conn.close()


def test_unpriced_cost_alerts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.ai_cost_summary",
        lambda conn: {"priced_calls": 0, "targets": []},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    assert any("单站成本尚未计量" in alert["title"] for alert in gates["alerts"])
    conn.close()


def test_priced_cost_within_target_does_not_alert(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.ai_cost_summary",
        lambda conn: {
            "priced_calls": 12,
            "targets": [
                {
                    "key": "practice",
                    "label": "练习",
                    "attempts": 4,
                    "avg_cny": 8.0,
                    "target_cny": 15.0,
                    "status": "pass",
                }
            ],
        },
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert not any("单站成本尚未计量" in title for title in titles)
    assert not any("超过目标" in title for title in titles)
    conn.close()


def test_feedback_ready_latency_over_target_alerts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.feedback_ack_latency",
        lambda conn: {
            "ready": {
                "practice": {"count": 5, "within_target": 2, "within_rate": 0.4, "status": "fail"},
                "exam": {"count": 3, "within_target": 1, "within_rate": 0.333, "status": "fail"},
            }
        },
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("练习反馈生成 60 秒内仅 40%" in title for title in titles)
    assert any("考试反馈生成 72 小时内仅 33%" in title for title in titles)
    conn.close()


def test_feedback_ready_latency_within_target_does_not_alert(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.feedback_ack_latency",
        lambda conn: {
            "ready": {
                "practice": {"count": 5, "within_target": 5, "within_rate": 1.0, "status": "pass"},
                "exam": {"count": 3, "within_target": 3, "within_rate": 1.0, "status": "pass"},
            }
        },
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert not any("练习反馈生成" in title for title in titles)
    assert not any("考试反馈生成" in title for title in titles)
    conn.close()


def test_fairness_edit_and_accessibility_gap_alerts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.fairness_report",
        lambda conn, bundle_id=None: {
            "overall_gap_sd": 0.1,
            "edit_rate": {"gap_pp": 6.2},
            "feedback_accessibility_gap": {"overall_gap": 0.4},
        },
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("评分员编辑率组间差 6.2pp 超过 5pp" in title for title in titles)
    assert any("反馈可及性组间差 0.40 分超过 0.3 分" in title for title in titles)
    conn.close()


def test_fairness_gaps_within_thresholds_do_not_alert(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.fairness_report",
        lambda conn, bundle_id=None: {
            "overall_gap_sd": 0.1,
            "edit_rate": {"gap_pp": 3.5},
            "feedback_accessibility_gap": {"overall_gap": 0.2},
        },
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert not any("编辑率组间差" in title for title in titles)
    assert not any("反馈可及性组间差" in title for title in titles)
    conn.close()


def _experience(usefulness, realism):
    return {
        "experience": {
            "usefulness": usefulness,
            "realism": realism,
        }
    }


def test_low_usefulness_and_realism_alert(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.feedback_accessibility",
        lambda conn, bundle_id=None: _experience(
            {"average": 6.4, "count": 8, "status": "fail"},
            {"average": 5.2, "count": 8, "status": "fail"},
        ),
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    alerts = {alert["title"]: alert for alert in gates["alerts"]}
    assert alerts["学生实用性评分 6.4 低于 7.0"]["level"] == "warn"
    assert alerts["学生真实感评分 5.2 低于 7.0"]["level"] == "danger"
    assert gates["metric_values"]["usefulness"] == 6.4
    assert gates["metric_values"]["realism_status"] == "fail"
    conn.close()


def test_experience_within_target_or_insufficient_does_not_alert(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.feedback_accessibility",
        lambda conn, bundle_id=None: _experience(
            {"average": 8.1, "count": 8, "status": "pass"},
            {"average": 6.8, "count": 3, "status": "insufficient"},
        ),
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert not any("学生实用性评分" in title for title in titles)
    assert not any("学生真实感评分" in title for title in titles)
    conn.close()


def test_unresolved_critical_miss_alerts(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.critical_misses",
        lambda conn, limit=50: {"total": 3, "unresolved": 1, "resolved": 2},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("关键项漏判 1 条待根因分析" in title for title in titles)
    conn.close()


def test_resolved_critical_misses_do_not_alert(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.gates.critical_misses",
        lambda conn, limit=50: {"total": 3, "unresolved": 0, "resolved": 3},
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert not any("待根因分析" in title for title in titles)
    conn.close()


def test_technical_metric_issues_flag_each_threshold():
    issues = _technical_metric_issues(
        {
            "sp_fact_error": 0.03,
            "persona_pass": 0.90,
            "p95_latency": 2.0,
            "sus": 65,
            "evidence_rate": 0.90,
            "asr_error_rate": 0.12,
        }
    )
    assert len(issues) == 6
    assert any("SP 事实错误率" in issue for issue in issues)
    assert any("人格首轮通过率" in issue for issue in issues)
    assert any("语音延迟 P95" in issue for issue in issues)
    assert any("SUS 65.0" in issue for issue in issues)
    assert any("证据定位准确率" in issue for issue in issues)
    assert any("ASR 词错率" in issue for issue in issues)


def test_technical_metric_issues_pass_within_targets():
    issues = _technical_metric_issues(
        {
            "sp_fact_error": 0.01,
            "persona_pass": 0.98,
            "p95_latency": 1.2,
            "sus": 80,
            "evidence_rate": 1.0,
        }
    )
    assert issues == []


def test_g2_metric_issues_flag_thresholds():
    issues = _g2_metric_issues({"near_improvement": 0.05, "teacher_hours": 0.2})
    assert len(issues) == 2
    assert any("边缘案例一致率提升" in issue for issue in issues)
    assert any("教师评分工时下降" in issue for issue in issues)


def test_g2_metric_issues_pass_within_targets():
    issues = _g2_metric_issues({"near_improvement": 0.12, "teacher_hours": 0.45})
    assert issues == []
