"""执行归档包：运行摘要、槽位覆盖与哈希。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.execution import append_live_segment, start_run
from app.domain.export import package_hash
from app.domain.run_export import build_run_package
from app.domain.scheduling import ensure_schedule_inputs, generate_schedule
from app.seed import seed_if_empty


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "run_export.db")
    db.init_db(connection)
    seed_if_empty(connection)
    ensure_schedule_inputs(connection)
    generate_schedule(connection, exam_date="2026-12-21", rounds=2, seed=11)
    yield connection
    connection.close()


def test_run_package_contains_slots_and_integrity(conn):
    run = start_run(conn, actor="考务测试")
    result = build_run_package(conn, run["run_id"], exported_by="审计员 周宁")
    package = result["package"]

    assert result["package_hash"].startswith("sha256:")
    assert package_hash(package) == result["package_hash"]
    assert package["schema_version"] == "osce-loop-run-v1"
    assert package["run"]["run_id"] == run["run_id"]
    assert package["run"]["integrity"]["missing"] == package["run"]["counts"]["total"]
    assert package["schedule"]["candidates"]
    assert len(package["slots"]) == package["run"]["counts"]["total"]
    assert package["attempts"] == []
    assert package["audit_events"]

    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'exam.run_export' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    assert event["object_id"] == run["run_id"]


def test_unknown_run_is_rejected(conn):
    with pytest.raises(ValueError, match="不存在"):
        build_run_package(conn, "run_missing", exported_by="审计员")


def test_run_package_includes_live_segments(conn):
    run = start_run(conn, actor="考务测试")
    slot = next(s for s in run["slots"] if s["station_name"] == "急性胸痛")
    append_live_segment(
        conn,
        run["run_id"],
        slot["slot_key"],
        speaker="candidate",
        text="胸痛多久了？",
    )
    result = build_run_package(conn, run["run_id"], exported_by="审计员 周宁")
    package = result["package"]
    assert package["run"]["live_segments"] == 1
    assert len(package["live_segments"]) == 1
    assert package["live_segments"][0]["slot_key"] == slot["slot_key"]
    assert package["live_segments"][0]["text"] == "胸痛多久了？"
    assert package_hash(package) == result["package_hash"]
