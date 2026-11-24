"""提示影响分析：开启/关闭事实层提示的分数分布比较。"""
from __future__ import annotations

import json

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.hint_impact import hint_impact
from app.seed import build_bundle_payload


def _setup(tmp_path):
    conn = db.connect(tmp_path / "hint.db")
    db.init_db(conn)
    payload = build_bundle_payload()
    payload["station_id"] = "ST-HINT-001"
    bundle = freeze_bundle(conn, payload, actor="测试", version="1.0")
    for run_id, enabled in (("run_on", 1), ("run_off", 0)):
        conn.execute(
            "INSERT INTO exam_runs(run_id, schedule_id, exam_date, status, current_round, "
            "hints_enabled, started_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, "sch", "2026-12-10", "running", 1, enabled, "2026-12-10T08:00:00+08:00", "2026-12-10T08:00:00+08:00"),
        )
    return conn, bundle


def _attempt(conn, attempt_id, bundle_id, run_id, code):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, run_id, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (attempt_id, bundle_id, code, "考生", "SES", "L2", "scored", run_id, "2026-12-10T09:00:00+08:00"),
    )


def _score(conn, attempt_id, item_id, score):
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, routing, status, "
        "final_score, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (f"r_{attempt_id}_{item_id}", attempt_id, item_id, score, "{}", "reviewed", score, "2026-12-10T09:10:00+08:00"),
    )


def test_hint_impact_compares_groups(tmp_path):
    conn, bundle = _setup(tmp_path)
    _attempt(conn, "att_on", bundle["bundle_id"], "run_on", "C001")
    _attempt(conn, "att_off", bundle["bundle_id"], "run_off", "C002")
    _score(conn, "att_on", "OP.01", 2)
    _score(conn, "att_on", "HX.01", 2)
    _score(conn, "att_off", "OP.01", 0)
    _score(conn, "att_off", "HX.01", 0)
    conn.commit()

    result = hint_impact(conn)
    assert result["status"] == "ok"
    assert result["sufficient"] is True
    on_group = next(g for g in result["groups"] if g["hints_enabled"])
    off_group = next(g for g in result["groups"] if not g["hints_enabled"])
    assert on_group["attempts"] == 1
    assert off_group["attempts"] == 1
    assert on_group["mean_total"] > off_group["mean_total"]
    assert result["delta_mean"] == 100.0
    assert "因果" in result["note"]
    conn.close()


def test_hint_impact_insufficient_with_one_group(tmp_path):
    conn, bundle = _setup(tmp_path)
    _attempt(conn, "att_on", bundle["bundle_id"], "run_on", "C001")
    _score(conn, "att_on", "OP.01", 2)
    conn.commit()
    result = hint_impact(conn)
    assert result["status"] == "insufficient"
    assert result["sufficient"] is False
    assert result["delta_mean"] is None
    conn.close()


def test_hint_impact_ignores_attempts_without_run(tmp_path):
    conn, bundle = _setup(tmp_path)
    _attempt(conn, "att_free", bundle["bundle_id"], None, "C003")
    _score(conn, "att_free", "OP.01", 2)
    conn.commit()
    result = hint_impact(conn)
    assert all(group["attempts"] == 0 for group in result["groups"])
    assert result["status"] == "insufficient"
    conn.close()
