"""闸门告警：人工编辑率、复核占比、申诉/改判率与加固校验。"""
from __future__ import annotations

from app import db
from app.domain.gates import evaluate_gates


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
            "targets": [
                {
                    "key": "exam",
                    "label": "考试",
                    "attempts": 3,
                    "avg_cny": 72.0,
                    "target_cny": 60.0,
                    "status": "fail",
                }
            ]
        },
    )
    gates = evaluate_gates(conn, _report(0.2, 40), "G0_offline")
    titles = [alert["title"] for alert in gates["alerts"]]
    assert any("考试单站成本 ¥72.00 超过目标 ¥60" in title for title in titles)
    conn.close()
