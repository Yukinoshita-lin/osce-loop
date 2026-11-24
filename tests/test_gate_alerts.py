"""闸门告警：人工编辑率过低时的橡皮图章预警。"""
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
