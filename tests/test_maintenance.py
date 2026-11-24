"""运维维护：过期会话清理、留存刷新与审计锚点。"""
from __future__ import annotations

from app import db
from app.domain.auth import authenticate, create_session, ensure_users, resolve_token
from app.domain.maintenance import run_maintenance
from app.seed import seed_if_empty


def test_maintenance_cleans_sessions_and_writes_anchor(tmp_path):
    conn = db.connect(tmp_path / "maintenance.db")
    db.init_db(conn)
    seed_if_empty(conn)
    ensure_users(conn)
    admin = authenticate(conn, "admin", "admin123")
    expired = create_session(conn, admin["user_id"], ttl_hours=-1)
    valid = create_session(conn, admin["user_id"])
    anchor_path = tmp_path / "anchors.jsonl"

    result = run_maintenance(
        conn, actor="考务 李萍", anchor_threshold=1, anchor_path=anchor_path
    )
    assert result["sessions_cleaned"] == 1
    assert resolve_token(conn, expired["token"]) is None
    assert resolve_token(conn, valid["token"]) is not None
    assert result["retention_updated"] >= 0
    assert result["anchor_id"] is not None
    assert anchor_path.exists()
    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'ops.maintenance' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    assert event["actor"] == "考务 李萍"

    second = run_maintenance(
        conn, actor="考务 李萍", anchor_threshold=1000, anchor_path=anchor_path
    )
    assert second["anchor_id"] is None
    assert second["sessions_cleaned"] == 0
    conn.close()
