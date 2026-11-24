"""反馈确认时延：发布到学生确认阅读的小时数。"""
from __future__ import annotations

from app import db
from app.domain.latency import feedback_ack_latency


def _attempt(conn, attempt_id, published_at):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, published_at, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (attempt_id, "bnd", "C001", "考生", "SES", "L2", "published", published_at, published_at),
    )


def _ack(conn, attempt_id, ack_id, created_at):
    conn.execute(
        "INSERT INTO ai_disclosure_acks(ack_id, attempt_id, user_id, actor, created_at) VALUES(?,?,?,?,?)",
        (ack_id, attempt_id, "usr_student", "练习学生", created_at),
    )


def test_feedback_latency_computes_hours_and_target(tmp_path):
    conn = db.connect(tmp_path / "latency.db")
    db.init_db(conn)
    _attempt(conn, "att_fast", "2026-01-01T00:00:00+08:00")
    _attempt(conn, "att_slow", "2026-01-01T00:00:00+08:00")
    _ack(conn, "att_fast", "ack1", "2026-01-02T00:00:00+08:00")
    _ack(conn, "att_slow", "ack2", "2026-01-04T08:00:00+08:00")
    conn.commit()

    result = feedback_ack_latency(conn)
    assert result["count"] == 2
    assert result["within_target"] == 1
    assert result["within_rate"] == 0.5
    assert result["status"] == "fail"
    assert result["avg_hours"] == 52.0
    assert result["samples"][0]["hours"] == 80.0
    conn.close()


def test_feedback_latency_without_ack_is_unmeasured(tmp_path):
    conn = db.connect(tmp_path / "latency_empty.db")
    db.init_db(conn)
    result = feedback_ack_latency(conn)
    assert result["status"] == "unmeasured"
    assert result["count"] == 0
    assert result["avg_hours"] is None
    conn.close()
