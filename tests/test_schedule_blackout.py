"""S4 考站不可用时段：校验、排考避开、修复改派避开与审计留痕。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.scheduling import (
    add_blackout,
    blackouts_by_round,
    delete_blackout,
    ensure_schedule_inputs,
    generate_schedule,
    list_blackouts,
    repair_schedule,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "blackout.db")
    db.init_db(connection)
    ensure_schedule_inputs(connection)
    yield connection
    connection.close()


def test_blackout_validation_and_audit(conn):
    with pytest.raises(ValueError, match="考站不存在"):
        add_blackout(
            conn, station_id="NOPE", from_round=1, to_round=2, reason="考官临时请假"
        )
    with pytest.raises(ValueError, match="不合法"):
        add_blackout(
            conn, station_id="ST03", from_round=3, to_round=2, reason="考官临时请假"
        )
    with pytest.raises(ValueError, match="不少于 4 字"):
        add_blackout(conn, station_id="ST03", from_round=1, to_round=2, reason="请假")

    item = add_blackout(
        conn,
        station_id="ST03",
        from_round=2,
        to_round=3,
        reason="E03 考官请假",
        actor="考务 李萍",
    )
    assert item["station_id"] == "ST03"
    assert item["from_round"] == 2 and item["to_round"] == 3
    assert list_blackouts(conn)[0]["blackout_id"] == item["blackout_id"]
    with pytest.raises(ValueError, match="已有不可用记录"):
        add_blackout(
            conn, station_id="ST03", from_round=3, to_round=4, reason="重复登记冲突"
        )
    assert blackouts_by_round(conn, 6) == {2: {"ST03"}, 3: {"ST03"}}

    delete_blackout(conn, item["blackout_id"], actor="考务 李萍")
    assert list_blackouts(conn) == []
    actions = {
        row["action"] for row in conn.execute("SELECT action FROM audit_events").fetchall()
    }
    assert {"exam.schedule_blackout_add", "exam.schedule_blackout_delete"} <= actions


def test_generation_avoids_blacked_out_station(conn):
    add_blackout(
        conn, station_id="ST03", from_round=1, to_round=6, reason="E03 考官请假"
    )
    schedule = generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=2026)
    for candidate in schedule["candidates"]:
        assert all(entry["station_id"] != "ST03" for entry in candidate["stations"])
    for round_row in schedule["rounds_detail"]:
        assert "ST03" not in {slot["station_id"] for slot in round_row["slots"]}
    assert schedule["blackouts"][0]["station_id"] == "ST03"
    assert any(
        "不可用时段" in warning and "ST03" in warning
        for warning in schedule["quality"]["warnings"]
    )


def test_generation_fails_when_blackout_exceeds_capacity(conn):
    for station_id in ("ST01", "ST02", "ST03"):
        add_blackout(
            conn,
            station_id=station_id,
            from_round=1,
            to_round=6,
            reason="场地临时占用",
        )
    with pytest.raises(ValueError, match="未找到可行"):
        generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=2026)


def test_repair_avoids_blackout_targets(conn):
    generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=2026)
    add_blackout(
        conn, station_id="ST05", from_round=1, to_round=6, reason="场地临时占用"
    )
    repaired = repair_schedule(
        conn,
        conflicts=[{"type": "candidate_round", "candidate_id": "C2026-001", "round": 2}],
    )
    changed = next(
        candidate for candidate in repaired["candidates"]
        if candidate["candidate_id"] == "C2026-001"
    )
    entry = next(item for item in changed["stations"] if item["round"] == 2)
    assert entry["station_id"] != "ST05"
