"""S4 排考冲突最小改动修复：只改受影响的人，硬约束不破坏。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.scheduling import (
    ensure_schedule_inputs,
    generate_schedule,
    latest_schedule,
    repair_schedule,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "repair.db")
    db.init_db(connection)
    ensure_schedule_inputs(connection)
    yield connection
    connection.close()


def _entries(schedule):
    return {
        candidate["candidate_id"]: {
            entry["round"]: entry["station_id"] for entry in candidate["stations"]
        }
        for candidate in schedule["candidates"]
    }


def _assert_hard_constraints(schedule):
    for candidate in schedule["candidates"]:
        stations = [entry["station_id"] for entry in candidate["stations"]]
        assert len(stations) == len(set(stations)) == schedule["rounds"]
        assert [entry["round"] for entry in candidate["stations"]] == list(
            range(1, schedule["rounds"] + 1)
        )
    for round_row in schedule["rounds_detail"]:
        stations = [slot["station_id"] for slot in round_row["slots"]]
        candidates = [slot["candidate_id"] for slot in round_row["slots"]]
        assert len(stations) == len(set(stations))
        assert len(candidates) == len(set(candidates))


def test_repair_candidate_round_changes_only_affected(conn):
    schedule = generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=2026)
    target_id = schedule["candidates"][0]["candidate_id"]
    before = _entries(schedule)
    repaired = repair_schedule(
        conn,
        conflicts=[{"type": "candidate_round", "candidate_id": target_id, "round": 2}],
    )
    after = _entries(repaired)
    changes = repaired["repair_report"]["changes"]
    assert len(changes) >= 1
    assert changes[0]["candidate_id"] == target_id
    assert changes[0]["method"] in ("free_station", "chain")
    assert after[target_id][2] != before[target_id][2]

    touched = {candidate_id for candidate_id in before if before[candidate_id] != after[candidate_id]}
    assert touched == {change["candidate_id"] for change in changes}
    assert repaired["schedule_id"] != schedule["schedule_id"]
    assert repaired["repaired_from"] == schedule["schedule_id"]
    _assert_hard_constraints(repaired)
    assert latest_schedule(conn)["schedule_id"] == repaired["schedule_id"]
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'exam.schedule_repair'"
    ).fetchone()["n"]
    assert audit == 1


def test_repair_station_outage_removes_station_from_round(conn):
    schedule = generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=7)
    round_row = schedule["rounds_detail"][2]
    station_id = round_row["slots"][0]["station_id"]
    affected_id = round_row["slots"][0]["candidate_id"]
    repaired = repair_schedule(
        conn,
        conflicts=[{"type": "station_round", "station_id": station_id, "round": 3}],
    )
    remaining = [slot["station_id"] for slot in repaired["rounds_detail"][2]["slots"]]
    assert station_id not in remaining
    assert len(remaining) == len(set(remaining)) == 8
    affected = next(
        candidate for candidate in repaired["candidates"] if candidate["candidate_id"] == affected_id
    )
    entry = next(item for item in affected["stations"] if item["round"] == 3)
    assert entry["station_id"] != station_id
    _assert_hard_constraints(repaired)
    assert repaired["repair_report"]["unresolved"] == []


def test_repair_multiple_conflicts_in_same_round(conn):
    schedule = generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=11)
    first, second = schedule["candidates"][0]["candidate_id"], schedule["candidates"][1]["candidate_id"]
    repaired = repair_schedule(
        conn,
        conflicts=[
            {"type": "candidate_round", "candidate_id": first, "round": 4},
            {"type": "candidate_round", "candidate_id": second, "round": 4},
        ],
    )
    changes = repaired["repair_report"]["changes"]
    assert len(changes) >= 2
    assert {first, second} <= {change["candidate_id"] for change in changes}
    assert repaired["repair_report"]["unresolved"] == []
    _assert_hard_constraints(repaired)
    entries = _entries(repaired)
    assert entries[first][4] != entries[second][4]


def test_repair_rejects_unknown_conflicts(conn):
    with pytest.raises(ValueError, match="尚未生成排考表"):
        repair_schedule(
            conn,
            conflicts=[{"type": "candidate_round", "candidate_id": "C2026-001", "round": 1}],
        )
    generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=3)
    with pytest.raises(ValueError, match="不在排考表"):
        repair_schedule(
            conn,
            conflicts=[{"type": "candidate_round", "candidate_id": "NOPE", "round": 1}],
        )
    with pytest.raises(ValueError, match="不存在"):
        repair_schedule(
            conn,
            conflicts=[{"type": "candidate_round", "candidate_id": "C2026-001", "round": 99}],
        )
    with pytest.raises(ValueError, match="没有安排"):
        repair_schedule(
            conn,
            conflicts=[{"type": "station_round", "station_id": "ST-NONE", "round": 1}],
        )
