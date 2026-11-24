"""用户管理：创建、角色调整、启停与密码重置。"""
from __future__ import annotations

from typing import Any

from .. import db
from .auth import hash_password

ROLES = ("admin", "exam_manager", "examiner", "auditor", "student")
ROLE_LABELS = {
    "admin": "系统管理员",
    "exam_manager": "考务管理者",
    "examiner": "考官/评分员",
    "auditor": "审计员",
    "student": "学生",
}


def _public(row) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "role_label": ROLE_LABELS.get(row["role"], row["role"]),
        "active": bool(row["active"]),
        "candidate_code": row["candidate_code"],
        "created_at": row["created_at"],
    }


def list_users(conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    return [_public(row) for row in rows]


def _user_or_error(conn, user_id: str):
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        raise ValueError("用户不存在")
    return row


def _validate_role(role: str, candidate_code: str | None) -> None:
    if role not in ROLES:
        raise ValueError("非法角色")
    if role == "student" and not (candidate_code or "").strip():
        raise ValueError("学生账号必须绑定考生编号")


def create_user(
    conn,
    *,
    username: str,
    display_name: str,
    password: str,
    role: str,
    candidate_code: str | None = None,
    actor: str = "系统管理员",
) -> dict[str, Any]:
    username = username.strip()
    if len(username) < 3:
        raise ValueError("用户名至少 3 个字符")
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    candidate_code = (candidate_code or "").strip() or None
    _validate_role(role, candidate_code)
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise ValueError("用户名已存在")
    user_id = db.new_id("usr")
    conn.execute(
        "INSERT INTO users(user_id, username, display_name, role, password_hash, active, candidate_code, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            user_id,
            username,
            display_name.strip() or username,
            role,
            hash_password(password),
            1,
            candidate_code,
            db.now_iso(),
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="user.create",
        object_type="User",
        object_id=user_id,
        before=None,
        after={"username": username, "role": role, "candidate_code": candidate_code},
    )
    return _public(_user_or_error(conn, user_id))


def update_user(
    conn,
    user_id: str,
    *,
    display_name: str | None = None,
    role: str | None = None,
    active: bool | None = None,
    candidate_code: str | None = None,
    password: str | None = None,
    actor: str = "系统管理员",
) -> dict[str, Any]:
    row = _user_or_error(conn, user_id)
    changes: dict[str, Any] = {}
    if display_name is not None:
        changes["display_name"] = display_name.strip() or row["display_name"]
    if role is not None and role != row["role"]:
        target_code = candidate_code if candidate_code is not None else row["candidate_code"]
        _validate_role(role, target_code)
        changes["role"] = role
    if candidate_code is not None:
        code = candidate_code.strip() or None
        _validate_role(role or row["role"], code)
        changes["candidate_code"] = code
    if active is not None and bool(active) != bool(row["active"]):
        changes["active"] = 1 if active else 0
    if password is not None:
        if len(password) < 8:
            raise ValueError("密码至少 8 位")
        changes["password_hash"] = hash_password(password)
    if not changes:
        return _public(row)

    becomes_inactive = changes.get("active") == 0
    loses_admin = row["role"] == "admin" and (
        changes.get("role", row["role"]) != "admin" or becomes_inactive
    )
    if loses_admin:
        active_admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1"
        ).fetchone()["n"]
        if active_admins <= 1:
            raise ValueError("至少保留一名启用的管理员")

    assignments = ", ".join(f"{column} = ?" for column in changes)
    conn.execute(
        f"UPDATE users SET {assignments} WHERE user_id = ?",
        (*changes.values(), user_id),
    )
    if becomes_inactive or "password_hash" in changes:
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    after = {
        key: ("已更新" if key == "password_hash" else value)
        for key, value in changes.items()
    }
    db.append_audit(
        conn,
        actor=actor,
        action="user.update",
        object_type="User",
        object_id=user_id,
        before={"role": row["role"], "active": bool(row["active"])},
        after=after,
    )
    return _public(_user_or_error(conn, user_id))
