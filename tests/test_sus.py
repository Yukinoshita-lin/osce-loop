"""SUS 可用性问卷：计分、入库、均值与闸门接入。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.auth import rbac_allowed
from app.domain.gates import evaluate_gates
from app.domain.stats import quality_report
from app.domain.sus import SUS_ITEMS, compute_sus, submit_sus, sus_summary

BEST = [5, 1, 5, 1, 5, 1, 5, 1, 5, 1]
WORST = [1, 5, 1, 5, 1, 5, 1, 5, 1, 5]
SAMPLE = [5, 2, 4, 1, 4, 2, 4, 2, 4, 2]


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "sus.db")
    db.init_db(connection)
    yield connection
    connection.close()


def test_sus_scoring_bounds():
    assert len(SUS_ITEMS) == 10
    assert compute_sus(BEST) == 100.0
    assert compute_sus(WORST) == 0.0
    assert compute_sus(SAMPLE) == 80.0


def test_sus_rejects_invalid_answers():
    with pytest.raises(ValueError):
        compute_sus([5] * 9)
    with pytest.raises(ValueError):
        compute_sus([5] * 9 + [6])
    with pytest.raises(ValueError):
        compute_sus([5] * 9 + [True])


def test_submit_and_summary(conn):
    first = submit_sus(conn, answers=BEST, participant_label="甲", actor="测试员")
    second = submit_sus(conn, answers=WORST, participant_label="乙", actor="测试员")
    assert first["score"] == 100.0
    assert second["score"] == 0.0

    summary = sus_summary(conn)
    assert summary["count"] == 2
    assert summary["average"] == 50.0
    assert summary["recent_scores"][0] == 0.0

    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_events ORDER BY rowid").fetchall()]
    assert actions.count("sus.submit") == 2
    assert db.verify_audit_chain(conn)["ok"] is True


def test_gate_uses_sus_average(conn):
    submit_sus(conn, answers=BEST, actor="测试员")
    submit_sus(conn, answers=WORST, actor="测试员")
    gates = evaluate_gates(conn, quality_report(conn), "G1_practice")
    g1 = next(gate for gate in gates["gates"] if gate["id"] == "G1")
    sus_value = next(row["value"] for row in g1["criteria"] if row["key"] == "sus")
    assert sus_value == 50.0
    assert gates["metric_values"]["sus_source"] == "responses"


def test_gate_flags_legacy_sus_value(conn):
    db.set_state(conn, "sus_score", "72.5")
    gates = evaluate_gates(conn, quality_report(conn), "G1_practice")
    assert gates["metric_values"]["sus"] == 72.5
    assert gates["metric_values"]["sus_source"] == "legacy"
    assert any("SUS" in alert["title"] for alert in gates["alerts"])


def test_sus_rbac():
    assert rbac_allowed("POST", "/api/quality/sus", "student") is True
    assert rbac_allowed("POST", "/api/quality/sus", "examiner") is True
    assert rbac_allowed("POST", "/api/quality/sus", "exam_manager") is True
    assert rbac_allowed("POST", "/api/quality/sus", "auditor") is False
