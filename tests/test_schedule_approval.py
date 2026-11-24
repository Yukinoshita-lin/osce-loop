"""S4 特殊需求审批：逐人确认、驳回留理由、随排考版本隔离。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.schedule_approval import (
    record_special_needs_approval,
    special_needs_status,
)
from app.domain.scheduling import ensure_schedule_inputs, generate_schedule, repair_schedule


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "special_needs.db")
    db.init_db(connection)
    ensure_schedule_inputs(connection)
    yield connection
    connection.close()


@pytest.fixture()
def schedule(conn):
    return generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=2026)


def test_special_needs_are_listed_as_pending(conn, schedule):
    status = special_needs_status(conn, schedule)
    ids = {item["candidate_id"] for item in status["items"]}
    assert ids == {"C2026-002", "C2026-003", "C2026-005", "C2026-007"}
    assert status["pending"] == sorted(ids)
    assert status["approved"] == 0
    assert status["rejected"] == 0
    assert status["complete"] is False
    assert all(item["status"] == "pending" for item in status["items"])


def test_approve_and_reject_special_needs(conn, schedule):
    status = record_special_needs_approval(
        conn,
        candidate_id="C2026-002",
        decision="approved",
        note="延长 5 分钟已确认，单独通道同步安排",
        actor="考务 李萍",
    )
    assert status["approved"] == 1
    assert "C2026-002" not in status["pending"]
    approved = next(item for item in status["items"] if item["candidate_id"] == "C2026-002")
    assert approved["status"] == "approved"
    assert approved["approval"]["actor"] == "考务 李萍"

    with pytest.raises(ValueError, match="理由"):
        record_special_needs_approval(
            conn, candidate_id="C2026-003", decision="rejected", note="", actor="考务 李萍"
        )
    status = record_special_needs_approval(
        conn,
        candidate_id="C2026-003",
        decision="rejected",
        note="候考区容量不足，改为普通候考并加强巡场",
        actor="考务 李萍",
    )
    assert status["rejected"] == 1
    rejected = next(item for item in status["items"] if item["candidate_id"] == "C2026-003")
    assert rejected["status"] == "rejected"
    assert rejected["approval"]["note"]


def test_approval_rejects_candidate_without_needs(conn, schedule):
    with pytest.raises(ValueError, match="没有需要审批"):
        record_special_needs_approval(
            conn, candidate_id="C2026-001", decision="approved", actor="考务"
        )
    with pytest.raises(ValueError, match="不在排考表"):
        record_special_needs_approval(
            conn, candidate_id="NOPE", decision="approved", actor="考务"
        )


def test_repair_creates_new_schedule_with_pending_approvals(conn, schedule):
    record_special_needs_approval(
        conn, candidate_id="C2026-002", decision="approved", note="已确认", actor="考务"
    )
    repaired = repair_schedule(
        conn,
        conflicts=[
            {"type": "candidate_round", "candidate_id": "C2026-004", "round": 2}
        ],
    )
    old_status = special_needs_status(conn, schedule)
    new_status = special_needs_status(conn, repaired)
    assert old_status["approved"] == 1
    assert new_status["schedule_id"] == repaired["schedule_id"]
    assert new_status["approved"] == 0
    assert len(new_status["pending"]) == 4
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'exam.special_needs_approval'"
    ).fetchone()["n"]
    assert audit == 1
