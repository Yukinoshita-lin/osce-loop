"""复核动作：关键项双人独立复核与分歧裁决。"""
from __future__ import annotations

import json

import pytest

from app import db
from app.domain.review import apply_review, blind_dual_review


def _conn(tmp_path):
    conn = db.connect(tmp_path / "review.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("att_r", "bnd_1", "C001", "考生", "SES", "L2", "pending_review", db.now_iso()),
    )
    conn.commit()
    return conn


def _record(conn, record_id: str, routing: dict | None = None, ai_score: int | None = 2):
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, routing, status, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            record_id,
            "att_r",
            "HX.01",
            ai_score,
            json.dumps(routing or {"route": "send_review", "reason": "sampling", "requires_human": True}),
            "pending_review",
            db.now_iso(),
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM score_records WHERE record_id = ?", (record_id,)).fetchone())


DUAL_ROUTING = {
    "route": "dual_review",
    "reason": "critical_negative",
    "requires_human": True,
    "dual_review": {"required": 2, "stage": 0, "status": "pending", "reviews": []},
}


def test_dual_review_requires_two_distinct_reviewers(tmp_path):
    conn = _conn(tmp_path)
    record = _record(conn, "rec_dual", DUAL_ROUTING)
    outcome = apply_review(
        conn, record, action="accept", status="accepted", final_score=2, reviewer="主治 张蕾"
    )
    assert outcome["finalized"] is False
    assert outcome["needs_second_review"] is True
    row = conn.execute("SELECT * FROM score_records WHERE record_id = 'rec_dual'").fetchone()
    assert row["status"] == "pending_review"
    assert row["final_score"] is None
    assert json.loads(row["routing"])["dual_review"]["status"] == "awaiting_second"
    assert conn.execute(
        "SELECT action FROM audit_events WHERE action = 'record.dual_review_first'"
    ).fetchone() is not None

    with pytest.raises(ValueError, match="不同评分员"):
        apply_review(
            conn, dict(row), action="accept", status="accepted", final_score=2, reviewer="主治 张蕾"
        )

    outcome = apply_review(
        conn, dict(row), action="accept", status="accepted", final_score=2, reviewer="主治 王强"
    )
    assert outcome["finalized"] is True
    assert outcome["dual_review"]["status"] == "resolved"
    row = conn.execute("SELECT * FROM score_records WHERE record_id = 'rec_dual'").fetchone()
    assert row["status"] == "accepted"
    assert row["final_score"] == 2
    assert row["reviewer"] == "主治 王强"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_actions WHERE record_id = 'rec_dual'"
    ).fetchone()["n"] == 2
    conn.close()


def test_dual_review_disagreement_escalates_to_senior(tmp_path):
    conn = _conn(tmp_path)
    record = _record(conn, "rec_dual2", DUAL_ROUTING)
    apply_review(conn, record, action="accept", status="accepted", final_score=2, reviewer="主治 张蕾")
    row = dict(conn.execute("SELECT * FROM score_records WHERE record_id = 'rec_dual2'").fetchone())
    outcome = apply_review(
        conn, row, action="correct", status="corrected", final_score=1,
        reviewer="主治 王强", reason_code="判定过严",
    )
    assert outcome["finalized"] is False
    assert outcome["needs_senior_review"] is True
    row = dict(conn.execute("SELECT * FROM score_records WHERE record_id = 'rec_dual2'").fetchone())
    assert row["status"] == "pending_review"
    assert row["final_score"] is None
    routing = json.loads(row["routing"])
    assert routing["route"] == "senior_arbitration"
    assert routing["reason"] == "review_disagreement"
    assert routing["dual_review"]["status"] == "disagreement"

    outcome = apply_review(
        conn, row, action="correct", status="corrected", final_score=1,
        reviewer="高年资 赵敏", reason_code="判定过严",
    )
    assert outcome["finalized"] is True
    row = conn.execute("SELECT * FROM score_records WHERE record_id = 'rec_dual2'").fetchone()
    assert row["status"] == "corrected"
    assert row["final_score"] == 1
    assert row["reviewer"] == "高年资 赵敏"
    assert json.loads(row["routing"])["dual_review"]["stage"] == 3
    conn.close()


def test_blind_dual_review_masks_other_reviewer():
    dual = {
        "status": "awaiting_second",
        "required": 2,
        "reviews": [
            {"reviewer": "主治 张蕾", "action": "accept", "score": 2, "at": "2026-12-10T09:10:00+08:00"}
        ],
    }
    masked = blind_dual_review(dual, viewer="主治 王强", role="examiner")
    assert masked["blind"] is True
    assert masked["viewer_submitted"] is False
    assert masked["reviews"][0]["action"] == "hidden"
    assert masked["reviews"][0]["score"] is None
    assert "主治 张蕾" not in masked["reviews"][0]["reviewer"]

    own = blind_dual_review(dual, viewer="主治 张蕾", role="examiner")
    assert own["viewer_submitted"] is True
    assert own["reviews"][0]["score"] == 2

    auditor = blind_dual_review(dual, viewer="审计员 周宁", role="auditor")
    assert auditor["reviews"][0]["score"] == 2
    assert "blind" not in auditor

    resolved = {**dual, "status": "resolved"}
    assert blind_dual_review(resolved, viewer="主治 王强", role="examiner")["reviews"][0]["score"] == 2

    empty = {"status": "awaiting_second", "required": 2, "reviews": []}
    assert blind_dual_review(empty, viewer="主治 王强", role="examiner") is empty


def test_normal_review_finalizes_in_one_action(tmp_path):
    conn = _conn(tmp_path)
    record = _record(conn, "rec_normal", ai_score=1)
    outcome = apply_review(
        conn, record, action="correct", status="corrected", final_score=2,
        reviewer="主治 张蕾", reason_code="判定过严",
    )
    assert outcome["finalized"] is True
    row = conn.execute("SELECT * FROM score_records WHERE record_id = 'rec_normal'").fetchone()
    assert row["status"] == "corrected"
    assert row["final_score"] == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM review_actions WHERE record_id = 'rec_normal'"
    ).fetchone()["n"] == 1
    conn.close()
