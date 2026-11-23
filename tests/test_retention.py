"""媒体分级留存：保留期、到期预览、清理与审计留痕。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.domain.auth import media_access, rbac_allowed
from app.domain.retention import (
    purge_expired,
    refresh_retention,
    retention_overview,
    retention_plan,
)


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(tmp_path / "retention.db")
    db.init_db(conn)
    media = tmp_path / "media"
    (media / "uploads").mkdir(parents=True)
    yield conn, media
    conn.close()


def _created(days_old: int) -> str:
    return (datetime.now(timezone.utc).astimezone() - timedelta(days=days_old)).isoformat(timespec="seconds")


def _insert(
    conn,
    attempt_id: str,
    *,
    code: str = "C2026-001",
    risk: str = "L1",
    days_old: int = 0,
    media_url: str | None = None,
    appeal: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, risk_level, "
        "media_id, media_url, duration_ms, integrity_gap, appeal, status, ab_group, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, "bnd_test", code, "考生", "SES", risk, "media_1", media_url, 1000, 0, appeal, "scored", "ai_assisted", _created(days_old)),
    )
    conn.commit()


def _write_media(media, name: str, size: int = 2048) -> str:
    path = media / "uploads" / name
    path.write_bytes(b"x" * size)
    return f"/media/uploads/{name}"


def test_policy_is_graduated_by_risk():
    now = _created(0)
    assert retention_plan("L0", now)["retention_days"] == 30
    assert retention_plan("L1", now)["retention_days"] == 90
    assert retention_plan("L2", now)["retention_days"] == 365
    assert retention_plan("L3", now)["retention_days"] == 1095
    assert retention_plan("L3", now)["retention_class"] == "L3"


def test_overview_dedupes_shared_media_bytes(env):
    conn, media = env
    url = _write_media(media, "shared-fresh.wav", 8192)
    _insert(conn, "att_a", code="C2026-001", days_old=1, media_url=url)
    _insert(conn, "att_b", code="C2026-002", days_old=2, media_url=url)
    overview = retention_overview(conn, media_dir=media)
    assert overview["bytes"]["media_total"] == 8192


def test_refresh_backfills_class_expiry_and_size(env):
    conn, media = env
    url = _write_media(media, "old.wav", 4096)
    _insert(conn, "att_old", days_old=100, media_url=url)
    assert refresh_retention(conn, media_dir=media) == 1
    row = conn.execute("SELECT * FROM attempts WHERE attempt_id = 'att_old'").fetchone()
    assert row["retention_class"] == "L1"
    assert row["media_size_bytes"] == 4096
    retain_until = datetime.fromisoformat(row["retain_until"])
    assert retain_until <= datetime.now(timezone.utc)


def test_overview_lists_expired_and_holds_appeals(env):
    conn, media = env
    expired_url = _write_media(media, "expired.wav", 1024)
    held_url = _write_media(media, "held.wav", 2048)
    fresh_url = _write_media(media, "fresh.wav", 512)
    _insert(conn, "att_expired", days_old=120, media_url=expired_url)
    _insert(conn, "att_held", risk="L2", days_old=400, media_url=held_url, appeal=1)
    _insert(conn, "att_fresh", risk="L3", days_old=10, media_url=fresh_url)

    overview = retention_overview(conn, media_dir=media)
    assert overview["counts"]["eligible"] == 1
    assert overview["counts"]["expired"] == 1
    assert overview["counts"]["held"] == 1
    assert overview["counts"]["due_soon"] == 0
    assert [item["attempt_id"] for item in overview["items"]] == ["att_expired"]
    assert overview["items"][0]["file_action"] == "delete"
    assert overview["bytes"]["expired"] == 1024


def test_dry_run_does_not_touch_files_or_records(env):
    conn, media = env
    path = media / "uploads" / "dry.wav"
    url = _write_media(media, "dry.wav", 3000)
    _insert(conn, "att_dry", days_old=200, media_url=url)

    result = purge_expired(conn, actor="考务", dry_run=True, media_dir=media)
    assert result["dry_run"] is True
    assert result["summary"]["total"] == 1
    assert result["deleted_files"] == 0
    assert path.exists()
    row = conn.execute("SELECT media_purged_at FROM attempts WHERE attempt_id = 'att_dry'").fetchone()
    assert row["media_purged_at"] is None


def test_apply_purges_media_and_keeps_audit_chain(env):
    conn, media = env
    path = media / "uploads" / "gone.wav"
    url = _write_media(media, "gone.wav", 5000)
    _insert(conn, "att_gone", code="C2026-001", days_old=200, media_url=url)

    result = purge_expired(conn, actor="考务 李萍", dry_run=False, media_dir=media)
    assert result["deleted_files"] == 1
    assert result["freed_bytes"] == 5000
    assert not path.exists()
    row = conn.execute("SELECT * FROM attempts WHERE attempt_id = 'att_gone'").fetchone()
    assert row["media_purged_at"]
    assert row["media_purged_by"] == "考务 李萍"
    assert row["status"] == "scored"

    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_events ORDER BY rowid").fetchall()]
    assert "media.purge" in actions
    assert "retention.purge" in actions
    assert db.verify_audit_chain(conn)["ok"] is True

    own = {"role": "student", "candidate_code": "C2026-001"}
    other = {"role": "student", "candidate_code": "C2026-002"}
    assert media_access(conn, own, url) == "purged"
    assert media_access(conn, other, url) == "forbidden"
    assert media_access(conn, {"role": "admin"}, url) == "purged"


def test_expired_media_access_blocked_before_cleanup(env):
    conn, media = env
    url = _write_media(media, "expired-access.wav", 1024)
    held_url = _write_media(media, "held-access.wav", 1024)
    _insert(conn, "att_expired_access", days_old=120, media_url=url)
    _insert(conn, "att_held_access", risk="L2", days_old=500, media_url=held_url, appeal=1)
    refresh_retention(conn, media_dir=media)
    assert media_access(conn, {"role": "student", "candidate_code": "C2026-001"}, url) == "expired"
    assert media_access(conn, {"role": "admin"}, url) == "expired"
    assert media_access(conn, {"role": "admin"}, held_url) == "ok"


def test_shared_media_file_is_kept_for_active_attempt(env):
    conn, media = env
    path = media / "uploads" / "shared.wav"
    url = _write_media(media, "shared.wav", 4096)
    _insert(conn, "att_expired_shared", code="C2026-001", days_old=200, media_url=url)
    _insert(conn, "att_active_shared", code="C2026-002", days_old=1, media_url=url)

    result = purge_expired(conn, actor="考务", dry_run=False, media_dir=media)
    assert result["summary"]["total"] == 1
    assert result["items"][0]["file_action"] == "shared_retain"
    assert result["deleted_files"] == 0
    assert path.exists()
    assert media_access(conn, {"role": "student", "candidate_code": "C2026-001"}, url) == "purged"
    assert media_access(conn, {"role": "student", "candidate_code": "C2026-002"}, url) == "ok"
    overview = retention_overview(conn, media_dir=media)
    assert overview["bytes"]["media_total"] == 4096
    assert overview["bytes"]["freed"] == 0
    assert overview["counts"]["purged"] == 1


def test_retention_rbac():
    assert rbac_allowed("GET", "/api/retention", "auditor") is True
    assert rbac_allowed("GET", "/api/retention", "student") is False
    assert rbac_allowed("POST", "/api/retention/purge", "exam_manager") is True
    assert rbac_allowed("POST", "/api/retention/purge", "auditor") is False
    assert rbac_allowed("POST", "/api/retention/purge", "student") is False
