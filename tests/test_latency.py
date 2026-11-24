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


def _sp_report(conn, session_id, updated_at, report_at):
    conn.execute(
        "INSERT INTO sp_sessions(session_id, bundle_id, candidate_label, state, transcript, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (session_id, "bnd", "练习学生", "finished", "[]", updated_at, updated_at),
    )
    conn.execute(
        "INSERT INTO sp_reports(session_id, bundle_id, report, created_at) VALUES(?,?,?,?)",
        (session_id, "bnd", "{}", report_at),
    )


def _exam_attempt(conn, attempt_id, created_at, published_at):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, published_at, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (attempt_id, "bnd", "C001", "考生", "SES", "L2", "published", published_at, created_at),
    )


def test_practice_ready_latency_targets_sixty_seconds(tmp_path):
    conn = db.connect(tmp_path / "ready_practice.db")
    db.init_db(conn)
    _sp_report(conn, "sp_fast", "2026-01-01T10:00:00+08:00", "2026-01-01T10:00:20+08:00")
    _sp_report(conn, "sp_slow", "2026-01-01T10:00:00+08:00", "2026-01-01T10:02:00+08:00")
    conn.commit()

    ready = feedback_ack_latency(conn)["ready"]["practice"]
    assert ready["count"] == 2
    assert ready["target"] == 60
    assert ready["within_target"] == 1
    assert ready["within_rate"] == 0.5
    assert ready["avg"] == 70.0
    assert ready["status"] == "fail"
    assert sorted(ready["samples"]) == [20.0, 120.0]
    conn.close()


def test_practice_ready_latency_passes_when_mostly_within_target(tmp_path):
    conn = db.connect(tmp_path / "ready_practice_pass.db")
    db.init_db(conn)
    _sp_report(conn, "sp_a", "2026-01-01T10:00:00+08:00", "2026-01-01T10:00:30+08:00")
    _sp_report(conn, "sp_b", "2026-01-01T10:00:00+08:00", "2026-01-01T10:00:40+08:00")
    conn.commit()

    ready = feedback_ack_latency(conn)["ready"]["practice"]
    assert ready["status"] == "pass"
    assert ready["within_rate"] == 1.0
    conn.close()


def test_exam_ready_latency_targets_seventy_two_hours(tmp_path):
    conn = db.connect(tmp_path / "ready_exam.db")
    db.init_db(conn)
    _exam_attempt(conn, "att_24h", "2026-01-01T00:00:00+08:00", "2026-01-02T00:00:00+08:00")
    _exam_attempt(conn, "att_100h", "2026-01-01T00:00:00+08:00", "2026-01-05T04:00:00+08:00")
    conn.commit()

    ready = feedback_ack_latency(conn)["ready"]["exam"]
    assert ready["count"] == 2
    assert ready["target"] == 72
    assert ready["within_target"] == 1
    assert ready["avg"] == 62.0
    assert ready["status"] == "fail"
    conn.close()


def test_ready_latency_without_reports_or_published_is_unmeasured(tmp_path):
    conn = db.connect(tmp_path / "ready_empty.db")
    db.init_db(conn)
    _exam_attempt(conn, "att_unpublished", "2026-01-01T00:00:00+08:00", None)
    conn.commit()

    ready = feedback_ack_latency(conn)["ready"]
    assert ready["practice"]["status"] == "unmeasured"
    assert ready["practice"]["count"] == 0
    assert ready["exam"]["status"] == "unmeasured"
    assert ready["exam"]["count"] == 0
    conn.close()
