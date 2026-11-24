"""G3 连续零关键项漏判：按时间倒序计算连续干净场次。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.gates import evaluate_gates
from app.domain.stats import quality_report
from app.seed import build_bundle_payload


def _setup(tmp_path):
    conn = db.connect(tmp_path / "streak.db")
    db.init_db(conn)
    payload = build_bundle_payload()
    payload["station_id"] = "ST-STREAK-001"
    bundle = freeze_bundle(conn, payload, actor="测试", version="1.0")
    critical = [item["item_id"] for item in payload["rubric"] if item.get("critical")]
    return conn, bundle, critical


def _attempt(conn, attempt_id, bundle_id, created_at):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (attempt_id, bundle_id, "C001", "考生", "SES", "L2", "scored", created_at),
    )


def _clean_records(conn, attempt_id, critical):
    for index, item_id in enumerate(critical):
        conn.execute(
            "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, gold_score, routing, "
            "status, final_score, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"{attempt_id}_{index}", attempt_id, item_id, 2, 2, "{}", "reviewed", 2, "2026-01-01T00:00:00+08:00"),
        )


def _miss_record(conn, attempt_id, item_id):
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, gold_score, routing, "
        "status, final_score, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (f"{attempt_id}_miss", attempt_id, item_id, 0, 2, "{}", "reviewed", 0, "2026-01-01T00:00:00+08:00"),
    )


def _streak(conn):
    gates = evaluate_gates(conn, quality_report(conn), "G3_limited")
    return gates["metric_values"]["critical_streak"]


def test_streak_resets_after_critical_miss(tmp_path):
    conn, bundle, critical = _setup(tmp_path)
    _attempt(conn, "att_miss", bundle["bundle_id"], "2026-01-01T09:00:00+08:00")
    _miss_record(conn, "att_miss", critical[0])
    _attempt(conn, "att_clean1", bundle["bundle_id"], "2026-01-02T09:00:00+08:00")
    _clean_records(conn, "att_clean1", critical)
    _attempt(conn, "att_clean2", bundle["bundle_id"], "2026-01-03T09:00:00+08:00")
    _clean_records(conn, "att_clean2", critical)
    conn.commit()
    assert _streak(conn) == 2

    _attempt(conn, "att_clean3", bundle["bundle_id"], "2026-01-04T09:00:00+08:00")
    _clean_records(conn, "att_clean3", critical)
    conn.commit()
    assert _streak(conn) == 3
    conn.close()


def test_streak_ignores_attempts_without_gold(tmp_path):
    conn, bundle, critical = _setup(tmp_path)
    _attempt(conn, "att_nogold", bundle["bundle_id"], "2026-01-01T09:00:00+08:00")
    for index, item_id in enumerate(critical):
        conn.execute(
            "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, routing, status, "
            "final_score, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"nogold_{index}", "att_nogold", item_id, 2, "{}", "reviewed", 2, "2026-01-01T00:00:00+08:00"),
        )
    conn.commit()
    assert _streak(conn) == 0
    conn.close()
