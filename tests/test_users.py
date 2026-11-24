"""用户管理：创建、角色调整、启停、密码重置与最后管理员保护。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.auth import (
    authenticate,
    create_session,
    ensure_users,
    rbac_allowed,
    resolve_token,
)
from app.domain.users import create_user, list_users, update_user


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "users.db")
    db.init_db(connection)
    ensure_users(connection)
    yield connection
    connection.close()


def _admin(conn):
    return conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()


def test_create_user_is_listed_and_audited(conn):
    user = create_user(
        conn,
        username="examiner2",
        display_name="考官 王敏",
        password="examiner123",
        role="examiner",
        actor="系统管理员",
    )
    assert user["role_label"] == "考官/评分员"
    assert user["active"] is True
    assert user["candidate_code"] is None
    assert any(u["username"] == "examiner2" for u in list_users(conn))
    assert authenticate(conn, "examiner2", "examiner123")["role"] == "examiner"

    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'user.create' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event["object_id"] == user["user_id"]
    assert "examiner123" not in (event["after"] or "")
    assert "examiner123" not in (event["before"] or "")


def test_create_user_rejects_invalid_input(conn):
    with pytest.raises(ValueError, match="用户名已存在"):
        create_user(
            conn, username="admin", display_name="重复", password="admin1234", role="admin"
        )
    with pytest.raises(ValueError, match="用户名至少"):
        create_user(conn, username="ab", display_name="短", password="abcdefgh", role="auditor")
    with pytest.raises(ValueError, match="密码至少"):
        create_user(conn, username="shortpwd", display_name="短密码", password="1234567", role="auditor")
    with pytest.raises(ValueError, match="非法角色"):
        create_user(conn, username="ghostrole", display_name="幽灵", password="abcdefgh", role="ghost")
    with pytest.raises(ValueError, match="考生编号"):
        create_user(conn, username="stu2", display_name="学生", password="student123", role="student")


def test_student_requires_candidate_code_on_create_and_update(conn):
    student = create_user(
        conn,
        username="stu2",
        display_name="学生 李四",
        password="student123",
        role="student",
        candidate_code="C2026-099",
    )
    assert student["candidate_code"] == "C2026-099"

    examiner = create_user(
        conn, username="exm3", display_name="考官 赵", password="examiner123", role="examiner"
    )
    with pytest.raises(ValueError, match="考生编号"):
        update_user(conn, examiner["user_id"], role="student")


def test_update_role_and_candidate_code(conn):
    user = create_user(
        conn, username="auditor2", display_name="审计 何", password="auditor123", role="auditor"
    )
    updated = update_user(
        conn, user["user_id"], role="student", candidate_code=" C2026-100 ", actor="系统管理员"
    )
    assert updated["role"] == "student"
    assert updated["candidate_code"] == "C2026-100"
    assert updated["display_name"] == "审计 何"


def test_deactivate_invalidates_sessions_and_blocks_login(conn):
    user = create_user(
        conn, username="exm4", display_name="考官 钱", password="examiner123", role="examiner"
    )
    session = create_session(conn, user["user_id"])
    assert resolve_token(conn, session["token"])["user_id"] == user["user_id"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM auth_sessions WHERE user_id = ?", (user["user_id"],)
    ).fetchone()["n"] == 1

    updated = update_user(conn, user["user_id"], active=False)
    assert updated["active"] is False
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM auth_sessions WHERE user_id = ?", (user["user_id"],)
    ).fetchone()["n"] == 0
    assert resolve_token(conn, session["token"]) is None
    assert authenticate(conn, "exm4", "examiner123") is None

    updated = update_user(conn, user["user_id"], active=True)
    assert updated["active"] is True
    assert authenticate(conn, "exm4", "examiner123")["role"] == "examiner"


def test_password_reset_invalidates_sessions(conn):
    user = create_user(
        conn, username="exm5", display_name="考官 孙", password="examiner123", role="examiner"
    )
    session = create_session(conn, user["user_id"])
    update_user(conn, user["user_id"], password="newpass123")
    assert authenticate(conn, "exm5", "examiner123") is None
    assert authenticate(conn, "exm5", "newpass123")["role"] == "examiner"
    assert resolve_token(conn, session["token"]) is None

    with pytest.raises(ValueError, match="密码至少"):
        update_user(conn, user["user_id"], password="1234567")


def test_last_active_admin_cannot_be_demoted_or_disabled(conn):
    admin = _admin(conn)
    with pytest.raises(ValueError, match="至少保留一名启用的管理员"):
        update_user(conn, admin["user_id"], active=False)
    with pytest.raises(ValueError, match="至少保留一名启用的管理员"):
        update_user(conn, admin["user_id"], role="auditor")

    second = create_user(
        conn, username="admin2", display_name="第二管理员", password="admin1234", role="admin"
    )
    demoted = update_user(conn, admin["user_id"], role="auditor")
    assert demoted["role"] == "auditor"
    restored = update_user(conn, admin["user_id"], role="admin")
    assert restored["role"] == "admin"
    assert second["role"] == "admin"


def test_unknown_user_raises(conn):
    with pytest.raises(ValueError, match="用户不存在"):
        update_user(conn, "usr_missing", active=False)


def test_users_api_is_admin_only():
    assert rbac_allowed("GET", "/api/users", "admin") is True
    for role in ("exam_manager", "examiner", "auditor", "student"):
        assert rbac_allowed("GET", "/api/users", role) is False
        assert rbac_allowed("POST", "/api/users", role) is False
        assert rbac_allowed("PATCH", "/api/users/usr_1", role) is False
    assert rbac_allowed("POST", "/api/users", "admin") is True
    assert rbac_allowed("PATCH", "/api/users/usr_1", "admin") is True
