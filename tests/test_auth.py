"""认证、会话与 RBAC 写权限矩阵。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.auth import (
    authenticate,
    create_session,
    ensure_users,
    hash_password,
    resolve_token,
    revoke_token,
    rbac_allowed,
    verify_password,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "auth.db")
    db.init_db(connection)
    ensure_users(connection)
    yield connection
    connection.close()


def test_password_hashing_and_authentication(conn):
    encoded = hash_password("secret123")
    assert encoded.startswith("pbkdf2_sha256$")
    assert "secret123" not in encoded
    assert verify_password("secret123", encoded) is True
    assert verify_password("wrong", encoded) is False
    admin = authenticate(conn, "admin", "admin123")
    assert admin["role"] == "admin"
    assert authenticate(conn, "admin", "wrong") is None
    assert authenticate(conn, "missing", "x") is None


def test_session_create_resolve_and_revoke(conn):
    admin = authenticate(conn, "admin", "admin123")
    session = create_session(conn, admin["user_id"])
    resolved = resolve_token(conn, session["token"])
    assert resolved["username"] == "admin"
    assert resolved["role"] == "admin"
    revoke_token(conn, session["token"])
    assert resolve_token(conn, session["token"]) is None


def test_expired_session_is_rejected(conn):
    admin = authenticate(conn, "admin", "admin123")
    session = create_session(conn, admin["user_id"], ttl_hours=-1)
    assert resolve_token(conn, session["token"]) is None


def test_rbac_write_matrix():
    assert rbac_allowed("POST", "/api/governance/release-stage", "admin") is True
    assert rbac_allowed("POST", "/api/governance/release-stage", "exam_manager") is False
    assert rbac_allowed("POST", "/api/governance/termination-switch", "exam_manager") is True
    assert rbac_allowed("POST", "/api/review/rec_1/action", "examiner") is True
    assert rbac_allowed("POST", "/api/review/rec_1/action", "auditor") is False
    assert rbac_allowed("POST", "/api/attempts/att_1/ingest", "exam_manager") is True
    assert rbac_allowed("POST", "/api/attempts/att_1/ingest", "examiner") is False
    assert rbac_allowed("POST", "/api/attempts/att_1/signoff", "exam_manager") is True
    assert rbac_allowed("POST", "/api/attempts/att_1/publish", "examiner") is False
    assert rbac_allowed("POST", "/api/schedule/generate", "exam_manager") is True
    assert rbac_allowed("POST", "/api/schedule/generate", "examiner") is False
    assert rbac_allowed("POST", "/api/execution/run_1/advance", "examiner") is True
    assert rbac_allowed("POST", "/api/execution/run_1/advance", "student") is False
    assert rbac_allowed("POST", "/api/sp/sessions", "student") is True
    assert rbac_allowed("POST", "/api/sp/sessions", "auditor") is False
    assert rbac_allowed("GET", "/api/quality", "auditor") is True
    assert rbac_allowed("GET", "/api/audit", "student") is False
    assert rbac_allowed("GET", "/api/attempts", "student") is True
    assert rbac_allowed("GET", "/api/attempts/att_1", "student") is False
    assert rbac_allowed("GET", "/api/feedback/att_1", "student") is True
    assert rbac_allowed("GET", "/api/review/queue", "student") is False
    assert rbac_allowed("GET", "/api/overview", "student") is False
    assert rbac_allowed("GET", "/api/bundles", "student") is False
    assert rbac_allowed("POST", "/api/auth/logout", "auditor") is True


def test_student_user_is_bound_to_candidate(conn):
    student = authenticate(conn, "student", "student123")
    assert student["role"] == "student"
    assert student["candidate_code"] == "C2026-001"
