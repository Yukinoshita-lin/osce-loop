"""审计事件哈希链：追加、校验与篡改检测。"""
from __future__ import annotations

import pytest

from app import db


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "audit.db")
    db.init_db(connection)
    yield connection
    connection.close()


def test_audit_chain_is_valid_after_appends(conn):
    first = db.append_audit(
        conn,
        actor="tester",
        action="test.first",
        object_type="Test",
        object_id="1",
        after={"value": 1},
    )
    second = db.append_audit(
        conn,
        actor="tester",
        action="test.second",
        object_type="Test",
        object_id="2",
        before={"value": 1},
        after={"value": 2},
    )
    result = db.verify_audit_chain(conn)
    assert result["ok"] is True
    assert result["total"] == 2
    assert first != second
    row = conn.execute("SELECT prev_hash, event_hash FROM audit_events WHERE event_id = ?", (second,)).fetchone()
    assert row["prev_hash"].startswith("sha256:")
    assert row["event_hash"].startswith("sha256:")


def test_audit_chain_detects_tampering(conn):
    event_id = db.append_audit(
        conn,
        actor="tester",
        action="test.tamper",
        object_type="Test",
        object_id="1",
        after={"value": 1},
    )
    db.append_audit(
        conn,
        actor="tester",
        action="test.after",
        object_type="Test",
        object_id="2",
        after={"value": 2},
    )
    conn.execute("UPDATE audit_events SET actor = 'intruder' WHERE event_id = ?", (event_id,))
    conn.commit()
    result = db.verify_audit_chain(conn)
    assert result["ok"] is False
    assert result["broken_at"] == event_id
    assert "哈希" in result["message"]


def test_empty_audit_chain_is_valid(conn):
    result = db.verify_audit_chain(conn)
    assert result["ok"] is True
    assert result["total"] == 0
