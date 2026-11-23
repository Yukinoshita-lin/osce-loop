"""审计链外部锚点：写入、截断检测、篡改检测与权限。"""
from __future__ import annotations

import json

import pytest

from app import db
from app.domain.auth import rbac_allowed


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "anchor.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _event(conn, action: str = "test.event") -> None:
    db.append_audit(conn, actor="tester", action=action, object_type="Test", object_id=action)


def test_create_and_verify_anchor(conn, tmp_path):
    path = tmp_path / "anchors.jsonl"
    _event(conn, "one")
    anchor = db.create_audit_anchor(conn, actor="审计员", anchor_path=path)
    assert anchor["event_count"] == 2
    assert anchor["head_hash"].startswith("sha256:")
    assert path.exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    _event(conn, "two")
    result = db.verify_audit_anchors(conn, anchor_path=path)
    assert result["ok"] is True
    assert result["checked"] == 1
    assert "全部通过" in result["message"]
    assert db.verify_audit_chain(conn)["ok"] is True


def test_anchor_detects_truncation(conn, tmp_path):
    path = tmp_path / "anchors.jsonl"
    _event(conn, "one")
    anchor = db.create_audit_anchor(conn, actor="审计员", anchor_path=path)
    last = conn.execute("SELECT event_id FROM audit_events ORDER BY rowid DESC LIMIT 1").fetchone()
    conn.execute("DELETE FROM audit_events WHERE event_id = ?", (last["event_id"],))
    conn.commit()

    assert db.verify_audit_chain(conn)["ok"] is True
    result = db.verify_audit_anchors(conn, anchor_path=path)
    assert result["ok"] is False
    assert str(anchor["event_count"]) in result["message"]
    assert "被删除" in result["message"]


def test_anchor_detects_tampered_event(conn, tmp_path):
    path = tmp_path / "anchors.jsonl"
    _event(conn, "one")
    db.create_audit_anchor(conn, actor="审计员", anchor_path=path)
    first = conn.execute("SELECT event_id FROM audit_events ORDER BY rowid LIMIT 1").fetchone()
    conn.execute("UPDATE audit_events SET actor = 'tampered' WHERE event_id = ?", (first["event_id"],))
    conn.commit()

    result = db.verify_audit_anchors(conn, anchor_path=path)
    assert result["ok"] is False
    assert "审计链校验失败" in result["message"]


def test_external_anchor_file_tamper_is_detected(conn, tmp_path):
    path = tmp_path / "anchors.jsonl"
    _event(conn, "one")
    db.create_audit_anchor(conn, actor="审计员", anchor_path=path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["head_hash"] = "sha256:forged"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    result = db.verify_audit_anchors(conn, anchor_path=path)
    assert result["ok"] is False
    assert "不一致" in result["message"]


def test_missing_external_anchor_file_is_detected(conn, tmp_path):
    path = tmp_path / "anchors.jsonl"
    _event(conn, "one")
    db.create_audit_anchor(conn, actor="审计员", anchor_path=path)
    path.unlink()
    result = db.verify_audit_anchors(conn, anchor_path=path)
    assert result["ok"] is False
    assert "缺失" in result["message"]


def test_database_anchor_deleted_is_detected(conn, tmp_path):
    path = tmp_path / "anchors.jsonl"
    _event(conn, "one")
    db.create_audit_anchor(conn, actor="审计员", anchor_path=path)
    conn.execute("DELETE FROM audit_anchors")
    conn.commit()
    result = db.verify_audit_anchors(conn, anchor_path=path)
    assert result["ok"] is False
    assert "不一致" in result["message"]


def test_create_anchor_refuses_broken_chain(conn, tmp_path):
    _event(conn, "one")
    first = conn.execute("SELECT event_id FROM audit_events ORDER BY rowid LIMIT 1").fetchone()
    conn.execute("UPDATE audit_events SET actor = 'tampered' WHERE event_id = ?", (first["event_id"],))
    conn.commit()
    with pytest.raises(ValueError):
        db.create_audit_anchor(conn, actor="审计员", anchor_path=tmp_path / "anchors.jsonl")


def test_anchor_rbac():
    assert rbac_allowed("GET", "/api/audit/anchors", "auditor") is True
    assert rbac_allowed("GET", "/api/audit/anchors", "exam_manager") is True
    assert rbac_allowed("GET", "/api/audit/anchors", "student") is False
    assert rbac_allowed("POST", "/api/audit/anchors", "auditor") is True
    assert rbac_allowed("POST", "/api/audit/anchors", "exam_manager") is True
    assert rbac_allowed("POST", "/api/audit/anchors", "examiner") is False
