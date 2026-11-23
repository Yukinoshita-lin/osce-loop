"""运行健康检查：数据库、审计链、锚点与媒体目录。"""
from __future__ import annotations

import shutil

import pytest

from app import db
from app.domain.health import system_health
from app.seed import seed_if_empty


@pytest.fixture(scope="module")
def seeded_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("health") / "seed.db"
    conn = db.connect(path)
    db.init_db(conn)
    seed_if_empty(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(seeded_path, tmp_path):
    path = tmp_path / "health.db"
    shutil.copy(seeded_path, path)
    connection = db.connect(path)
    yield connection
    connection.close()


def _media_dir(tmp_path):
    path = tmp_path / "media"
    path.mkdir()
    return path


def test_health_reports_ok_for_seeded_database(conn, tmp_path):
    result = system_health(
        conn,
        media_dir=_media_dir(tmp_path),
        anchor_path=tmp_path / "anchors.jsonl",
        env={"OSCELOOP_AI_PROVIDER": "mock"},
    )
    assert result["status"] == "ok"
    assert [c["key"] for c in result["checks"]] == [
        "database",
        "audit_chain",
        "audit_anchors",
        "media_dir",
    ]
    assert all(c["ok"] for c in result["checks"])
    assert result["ai"]["provider"] == "mock-score"
    assert result["counts"]["attempts"] == 13
    assert result["counts"]["audit_events"] > 0
    assert result["release_stage"]


def test_health_degrades_when_audit_chain_is_tampered(conn, tmp_path):
    conn.execute(
        "UPDATE audit_events SET after = '{\"tampered\": true}' WHERE rowid = (SELECT MIN(rowid) FROM audit_events)"
    )
    conn.commit()
    result = system_health(
        conn,
        media_dir=_media_dir(tmp_path),
        anchor_path=tmp_path / "anchors.jsonl",
        env={"OSCELOOP_AI_PROVIDER": "mock"},
    )
    assert result["status"] == "degraded"
    chain = next(c for c in result["checks"] if c["key"] == "audit_chain")
    assert chain["ok"] is False
    assert "不一致" in chain["detail"] or "失败" in chain["detail"]


def test_health_degrades_when_media_dir_missing(conn, tmp_path):
    result = system_health(
        conn,
        media_dir=tmp_path / "missing",
        anchor_path=tmp_path / "anchors.jsonl",
        env={"OSCELOOP_AI_PROVIDER": "mock"},
    )
    assert result["status"] == "degraded"
    media = next(c for c in result["checks"] if c["key"] == "media_dir")
    assert media["ok"] is False
    assert "不存在" in media["detail"]
