"""身份认证与会话：PBKDF2 密码哈希、不透明令牌、角色权限矩阵。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import db
from ..config import LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS

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
    ("admin", "系统管理员", "admin", "admin123", None),
    ("exam", "考务 李萍", "exam_manager", "exam123", None),
    ("reviewer", "主治 张蕾", "examiner", "review123", None),
    ("auditor", "审计员 周宁", "auditor", "audit123", None),
    ("student", "练习学生", "student", "student123", "C2026-001"),
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
    for username, display_name, role, password, candidate_code in DEMO_USERS:
        existing = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            if candidate_code:
                conn.execute(
                    "UPDATE users SET candidate_code = ? WHERE username = ? AND (candidate_code IS NULL OR candidate_code = '')",
                    (candidate_code, username),
                )
            continue
        conn.execute(
            "INSERT INTO users(user_id, username, display_name, role, password_hash, active, candidate_code, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                db.new_id("usr"),
                username,
                display_name,
                role,
                hash_password(password),
                1,
                candidate_code,
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
        "candidate_code": row["candidate_code"] if "candidate_code" in row.keys() else None,
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


def record_login_attempt(conn, *, username: str, ip: str | None, success: bool) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO login_attempts(attempt_id, username, ip, success, created_ts, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (db.new_id("login"), username, ip or "", 1 if success else 0, now, db.now_iso()),
    )
    conn.execute("DELETE FROM login_attempts WHERE created_ts < ?", (now - 7 * 86400,))
    conn.commit()


def login_rate_limited(
    conn,
    *,
    username: str,
    ip: str | None,
    max_failures: int | None = None,
    window_seconds: int | None = None,
) -> bool:
    limit = max_failures if max_failures is not None else LOGIN_MAX_FAILURES
    window = window_seconds if window_seconds is not None else LOGIN_WINDOW_SECONDS
    cutoff = time.time() - max(1, window)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM login_attempts "
        "WHERE success = 0 AND created_ts >= ? AND (username = ? OR ip = ?)",
        (cutoff, username, ip or ""),
    ).fetchone()
    return row["n"] >= limit


def cleanup_expired_sessions(conn) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
    conn.commit()
    return cursor.rowcount


def _media_ref_active(row, now: datetime) -> bool:
    if row["media_purged_at"]:
        return False
    if row["appeal"]:
        return True
    try:
        until = datetime.fromisoformat(row["retain_until"])
    except (TypeError, ValueError):
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > now


def media_access(conn, user: dict[str, Any], path: str) -> str:
    """返回 ok / purged / expired / forbidden，供媒体中间件区分 200、410 与 403。"""
    rows = conn.execute(
        "SELECT candidate_code, media_purged_at, retain_until, appeal FROM attempts WHERE media_url = ?",
        (path,),
    ).fetchall()
    if user.get("role") == "student":
        candidate_code = user.get("candidate_code")
        if not candidate_code:
            return "forbidden"
        rows = [row for row in rows if row["candidate_code"] == candidate_code]
        if not rows:
            return "forbidden"
    if not rows:
        return "ok"
    now = datetime.now(timezone.utc)
    if any(_media_ref_active(row, now) for row in rows):
        return "ok"
    if all(row["media_purged_at"] for row in rows):
        return "purged"
    return "expired"


def media_allowed(conn, user: dict[str, Any], path: str) -> bool:
    return media_access(conn, user, path) == "ok"


def rbac_allowed(method: str, path: str, role: str) -> bool:
    if role == "admin":
        return True
    if method in ("GET", "HEAD", "OPTIONS"):
        if path.startswith("/api/review/"):
            return role in ("exam_manager", "examiner")
        if path.startswith("/api/asr/"):
            return role in ("exam_manager", "auditor")
        if path.startswith(("/api/quality", "/api/fairness", "/api/ab", "/api/audit", "/api/retention", "/api/calibration")):
            return role in ("exam_manager", "auditor")
        if path.startswith("/api/rubric"):
            return role in ("exam_manager", "examiner", "auditor")
        if path.startswith("/api/blueprint"):
            return role in ("exam_manager", "examiner", "auditor")
        if path.startswith("/api/governance/"):
            return role in ("exam_manager", "auditor")
        if path.startswith("/api/overview"):
            return role in ("exam_manager", "examiner", "auditor")
        if path == "/api/bundles/stations":
            return role in ("student", "examiner", "exam_manager", "auditor")
        if path.startswith("/api/bundles"):
            return role in ("exam_manager", "examiner", "auditor")
        if path.startswith("/api/ops/"):
            return role in ("exam_manager", "auditor")
        if path.startswith("/api/users"):
            return False
        if path.startswith(("/api/schedule", "/api/execution")):
            return role in ("exam_manager", "examiner", "auditor")
        if path.startswith("/api/attempts/"):
            return role in ("exam_manager", "examiner", "auditor")
        if path.startswith("/api/sp/consents"):
            return role in ("exam_manager", "examiner", "auditor")
        if path.startswith("/api/sp/persona-approvals"):
            return role in ("exam_manager", "examiner", "auditor")
        return True
    if path == "/api/auth/logout":
        return True
    if method != "POST":
        return False
    if path == "/api/sp/consent":
        return role in ("exam_manager", "examiner")
    if path == "/api/sp/persona-approval":
        return role in ("exam_manager", "examiner")
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
        if path == "/api/governance/model-card/refresh":
            return role == "exam_manager"
        return False
    if path in (
        "/api/schedule/generate",
        "/api/schedule/repair",
        "/api/schedule/special-needs",
        "/api/schedule/policy",
    ):
        return role == "exam_manager"
    if path.startswith("/api/blueprint"):
        return role == "exam_manager"
    if path.startswith("/api/bundles/") and path.endswith("/verify-integrity"):
        return role in ("exam_manager", "auditor")
    if path.startswith("/api/bundles/"):
        return role == "exam_manager"
    if path == "/api/retention/purge":
        return role == "exam_manager"
    if path == "/api/ops/backup":
        return role == "exam_manager"
    if path == "/api/ops/maintenance":
        return role == "exam_manager"
    if path == "/api/ops/content-audit":
        return role in ("exam_manager", "auditor")
    if path == "/api/ops/ai-check":
        return role in ("exam_manager", "auditor")
    if path.startswith("/api/feedback/"):
        return role in ("student", "exam_manager", "examiner", "auditor")
    if path == "/api/quality/sus":
        return role in ("student", "examiner", "exam_manager")
    if path == "/api/quality/station-review":
        return role == "exam_manager"
    if path == "/api/calibration/import":
        return role == "exam_manager"
    if path.startswith("/api/asr/"):
        return role == "exam_manager"
    if path == "/api/audit/anchors":
        return role in ("exam_manager", "auditor")
    if path.startswith("/api/execution/"):
        return role in ("examiner", "exam_manager")
    return False
