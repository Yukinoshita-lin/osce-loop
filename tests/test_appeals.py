"""申诉快照与申诉率/改判率。"""
from __future__ import annotations

import json

from app import db
from app.domain.appeals import appeal_metrics, capture_snapshot


def test_capture_snapshot_records_final_scores(tmp_path):
    conn = db.connect(tmp_path / "appeal.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, appeal, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("att_a", "bnd_1", "C001", "考生", "SES", "L2", "signed_off", 0, db.now_iso()),
    )
    for record_id, final_score in (("rec_a_1", 2), ("rec_a_2", 1)):
        conn.execute(
            "INSERT INTO score_records(record_id, attempt_id, item_id, routing, status, final_score, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (record_id, "att_a", record_id, "{}", "accepted", final_score, db.now_iso()),
        )
    conn.commit()
    attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id = 'att_a'").fetchone()
    snapshot = json.loads(capture_snapshot(conn, attempt))
    assert snapshot["status"] == "signed_off"
    assert snapshot["records"] == {"rec_a_1": 2, "rec_a_2": 1}
    conn.close()


def test_appeal_metrics_distinguishes_change_and_missing_snapshot():
    attempts = [
        {
            "attempt_id": "a1",
            "status": "signed_off",
            "appeal": 1,
            "appeal_snapshot": json.dumps({"status": "signed_off", "records": {"r1": 1, "r2": 2}}),
        },
        {
            "attempt_id": "a2",
            "status": "signed_off",
            "appeal": 1,
            "appeal_snapshot": json.dumps({"status": "signed_off", "records": {"r3": 2}}),
        },
        {"attempt_id": "a3", "status": "pending_review", "appeal": 1, "appeal_snapshot": None},
        {"attempt_id": "a4", "status": "signed_off", "appeal": 0, "appeal_snapshot": None},
        {"attempt_id": "a5", "status": "signed_off", "appeal": 0, "appeal_snapshot": None},
        {
            "attempt_id": "a6",
            "status": "pending_review",
            "appeal": 1,
            "appeal_snapshot": json.dumps({"status": "signed_off", "records": {"r6": 2}}),
        },
    ]
    records = [
        {"attempt_id": "a1", "record_id": "r1", "final_score": 2, "status": "accepted"},
        {"attempt_id": "a1", "record_id": "r2", "final_score": 2, "status": "accepted"},
        {"attempt_id": "a2", "record_id": "r3", "final_score": 2, "status": "accepted"},
        {"attempt_id": "a4", "record_id": "r4", "final_score": 1, "status": "accepted"},
        {"attempt_id": "a6", "record_id": "r6", "final_score": None, "status": "pending_review"},
    ]
    metrics = appeal_metrics(attempts, records)
    assert metrics["appealed"] == 4
    assert metrics["appeal_rate"] == 0.6667
    assert metrics["compared"] == 2
    assert metrics["changed"] == 1
    assert metrics["change_rate"] == 0.5
    assert metrics["in_progress"] == 1
    assert metrics["missing_snapshot"] == 1


def test_appeal_metrics_without_appeals():
    metrics = appeal_metrics([{"attempt_id": "a1", "appeal": 0}], [])
    assert metrics["appealed"] == 0
    assert metrics["appeal_rate"] == 0.0
    assert metrics["change_rate"] is None
