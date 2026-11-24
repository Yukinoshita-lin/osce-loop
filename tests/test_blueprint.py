"""年度能力蓝图：版本、目标校验、人工批准与覆盖度绑定。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import db
from app.domain.blueprint import (
    DEFAULT_BLUEPRINT,
    approve_blueprint,
    blueprint_state,
    draft_blueprint,
)
from app.domain.bundle import freeze_bundle
from app.domain.coverage import blueprint_coverage
from app.seed import build_bundle_payload


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "blueprint.db")
    db.init_db(connection)
    yield connection
    connection.close()


def test_default_state_has_no_approved_blueprint(conn):
    state = blueprint_state(conn)
    assert state["source"] == "default"
    assert state["current"] is None
    assert state["history"] == []
    assert state["defaults"] == DEFAULT_BLUEPRINT


def test_draft_validates_share_total_and_domains(conn):
    year = datetime.now().year
    with pytest.raises(ValueError, match="合计"):
        draft_blueprint(
            conn,
            year=year,
            domains=[{"domain": "communication", "label": "沟通", "target_share": 0.5}],
            note=None,
            actor="考务",
        )
    with pytest.raises(ValueError, match="重复"):
        draft_blueprint(
            conn,
            year=year,
            domains=[
                {"domain": "communication", "label": "沟通", "target_share": 0.5},
                {"domain": "communication", "label": "沟通二", "target_share": 0.5},
            ],
            note=None,
            actor="考务",
        )
    with pytest.raises(ValueError, match="不合法"):
        draft_blueprint(
            conn,
            year=year,
            domains=[{"domain": "Bad Domain", "label": "沟通", "target_share": 1.0}],
            note=None,
            actor="考务",
        )


def test_approval_requires_reason_and_activates_latest_version(conn):
    year = datetime.now().year
    first = draft_blueprint(
        conn,
        year=year,
        domains=DEFAULT_BLUEPRINT,
        note="年度首版",
        actor="考务 李萍",
    )
    assert first["status"] == "draft"
    assert first["version"] == 1

    with pytest.raises(ValueError, match="理由"):
        approve_blueprint(conn, first["blueprint_id"], note="行", actor="考务 李萍")

    approved = approve_blueprint(
        conn, first["blueprint_id"], note="课程委员会审议通过", actor="考务 李萍"
    )
    assert approved["status"] == "approved"
    state = blueprint_state(conn)
    assert state["source"] == "approved"
    assert state["current"]["blueprint_id"] == first["blueprint_id"]
    assert state["stale"] is False

    second = draft_blueprint(
        conn,
        year=year,
        domains=DEFAULT_BLUEPRINT,
        note="修订版",
        actor="考务 李萍",
    )
    assert second["version"] == 2
    approve_blueprint(conn, second["blueprint_id"], note="修订后批准生效", actor="考务 李萍")
    state = blueprint_state(conn)
    assert state["current"]["version"] == 2
    statuses = {row["version"]: row["status"] for row in state["history"]}
    assert statuses == {1: "superseded", 2: "approved"}

    events = [
        row["action"]
        for row in conn.execute(
            "SELECT action FROM audit_events WHERE action LIKE 'blueprint.%' ORDER BY rowid"
        ).fetchall()
    ]
    assert events == ["blueprint.draft", "blueprint.approve", "blueprint.draft", "blueprint.approve"]


def test_approved_blueprint_drives_coverage_targets(conn):
    freeze_bundle(conn, build_bundle_payload(), actor="测试", version="1.0")
    second = build_bundle_payload()
    second["station_id"] = "ST-CARDIO-002"
    second["title"] = str(second["title"]) + "（并行站）"
    freeze_bundle(conn, second, actor="测试", version="1.0")
    year = datetime.now().year
    draft = draft_blueprint(
        conn,
        year=year,
        domains=[
            {"domain": "communication", "label": "沟通与人文", "target_share": 0.01},
            {"domain": "history_taking", "label": "病史采集", "target_share": 0.99},
        ],
        note="压低沟通域",
        actor="考务 李萍",
    )
    approve_blueprint(conn, draft["blueprint_id"], note="课程委员会批准新占比", actor="考务 李萍")

    coverage = blueprint_coverage(conn)
    assert coverage["blueprint"]["source"] == "approved"
    assert coverage["blueprint"]["year"] == year
    assert coverage["blueprint"]["version"] == 1
    rows = {row["domain"]: row for row in coverage["domains"]}
    assert rows["communication"]["target"] == 0.01
    assert rows["communication"]["status"] == "overrepresented"
    assert rows["history_taking"]["target"] == 0.99
    assert "communication" not in coverage["extra_domains"]
    assert "assessment" in coverage["extra_domains"]
    assert coverage["uncovered"] == []
