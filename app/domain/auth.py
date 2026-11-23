"""身份认证与会话：PBKDF2 密码哈希、不透明令牌、角色权限矩阵。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db

PBKDF2_ITERATIONS = 180_000
SESSION_TTL_HOURS = 8
ROLE_LABELS = {
    "admin": "系统管理员",
    "exam_manager": "考务管理者",
    "examiner": "考官/评分员",
    "auditor": "审计员",
    "student": "学生",
}
DEMO_USERS = [
    ("admin", "系统管理员", "admin", "admin123"),
    ("exam", "考务 李萍", "exam_manager", "exam123"),
    ("reviewer", "主治 张蕾", "examiner", "review123"),
    ("auditor", "审计员 周宁", "auditor", "audit123"),
    ("student", "练习学生", "student", "student123"),
]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_users(conn) -> None:
    for username, display_name, role, password in DEMO_USERS:
        existing = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO users(user_id, username, display_name, role, password_hash, active, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                db.new_id("usr"),
                username,
                display_name,
                role,
                hash_password(password),
                1,
                db.now_iso(),
            ),
        )
    conn.commit()


def public_user(row) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "role_label": ROLE_LABELS.get(row["role"], row["role"]),
    }


def authenticate(conn, username: str, password: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND active = 1", (username.strip(),)
    ).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return public_user(row)


def create_session(conn, user_id: str, ttl_hours: int = SESSION_TTL_HOURS) -> dict[str, Any]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)
    conn.execute(
        "INSERT INTO auth_sessions(session_id, user_id, token_hash, expires_at, created_at) VALUES(?,?,?,?,?)",
        (
            db.new_id("sess"),
            user_id,
            _token_hash(token),
            expires.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return {"token": token, "expires_at": expires.isoformat(timespec="seconds")}


def resolve_token(conn, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT s.session_id, s.expires_at, u.* FROM auth_sessions s "
        "JOIN users u ON u.user_id = s.user_id WHERE s.token_hash = ? AND u.active = 1",
        (_token_hash(token),),
    ).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        conn.execute("DELETE FROM auth_sessions WHERE session_id = ?", (row["session_id"],))
        conn.commit()
        return None
    user = public_user(row)
    user["session_id"] = row["session_id"]
    return user


def revoke_token(conn, token: str) -> None:
    if not token:
        return
    conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()


def rbac_allowed(method: str, path: str, role: str) -> bool:
    if role == "admin":
        return True
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if path == "/api/auth/logout":
        return True
    if method != "POST":
        return False
    if path.startswith("/api/sp/"):
        return role in ("student", "examiner", "exam_manager")
    if path.startswith("/api/review/"):
        return role in ("examiner", "exam_manager")
    if path.startswith("/api/attempts/"):
        if path.endswith("/ingest"):
            return role == "exam_manager"
        if path.endswith(("/signoff", "/publish", "/appeal", "/risk-level")):
            return role == "exam_manager"
        return False
    if path.startswith("/api/governance/"):
        if path.startswith("/api/governance/termination-switch") or path.startswith("/api/governance/drills/"):
            return role == "exam_manager"
        return False
    if path == "/api/schedule/generate":
        return role == "exam_manager"
    if path.startswith("/api/execution/"):
        return role in ("examiner", "exam_manager")
    return False
