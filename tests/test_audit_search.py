"""审计检索：过滤、排序与分页。"""
from __future__ import annotations

from app import db
from app.domain.audit_search import search_audit_events


def _conn(tmp_path):
    conn = db.connect(tmp_path / "audit_search.db")
    db.init_db(conn)
    db.append_audit(
        conn, actor="alice", action="feedback.survey", object_type="Attempt", object_id="att_1"
    )
    db.append_audit(
        conn, actor="bob", action="record.accept", object_type="ScoreRecord", object_id="att_2"
    )
    db.append_audit(
        conn, actor="alice", action="feedback.view", object_type="Attempt", object_id="att_3"
    )
    return conn


def test_search_filters_by_action_actor_and_object(tmp_path):
    conn = _conn(tmp_path)
    try:
        feedback = search_audit_events(conn, action="feedback.")
        assert feedback["total"] == 2
        assert [event["action"] for event in feedback["events"]] == ["feedback.view", "feedback.survey"]

        alice = search_audit_events(conn, actor="alice")
        assert alice["total"] == 2
        assert all(event["actor"] == "alice" for event in alice["events"])

        by_object = search_audit_events(conn, object_id="att_2")
        assert by_object["total"] == 1
        assert by_object["events"][0]["action"] == "record.accept"
    finally:
        conn.close()


def test_search_paginates_and_clamps_limit(tmp_path):
    conn = _conn(tmp_path)
    try:
        first = search_audit_events(conn, limit=2)
        second = search_audit_events(conn, limit=2, offset=2)
        assert first["total"] == 3
        assert len(first["events"]) == 2
        assert len(second["events"]) == 1
        assert first["events"][0]["object_id"] == "att_3"

        clamped = search_audit_events(conn, limit=0)
        assert clamped["limit"] == 1
        assert search_audit_events(conn, limit=999)["limit"] == 200
    finally:
        conn.close()
