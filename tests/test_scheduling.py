"""考务编排：约束、难度均衡、特殊需求与持久化。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.scheduling import (
    ensure_schedule_inputs,
    generate_schedule,
    latest_schedule,
    schedule_inputs,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "schedule.db")
    db.init_db(connection)
    ensure_schedule_inputs(connection)
    yield connection
    connection.close()


def test_schedule_inputs_are_seeded(conn):
    inputs = schedule_inputs(conn)
    assert len(inputs["candidates"]) == 8
    assert len(inputs["stations"]) == 10
    assert inputs["resources"]["rooms"] >= 10
    assert any(c["extra_time_min"] for c in inputs["candidates"])
    assert any(c["quiet_room"] for c in inputs["candidates"])


def test_generate_schedule_satisfies_hard_constraints(conn):
    schedule = generate_schedule(conn, exam_date="2026-12-10", rounds=6, buffer_min=2, seed=2026)
    candidates = schedule["candidates"]
    rounds = schedule["rounds_detail"]
    assert len(candidates) == 8
    assert len(rounds) == 6
    for candidate in candidates:
        stations = [s["station_id"] for s in candidate["stations"]]
        assert len(stations) == 6
        assert len(set(stations)) == 6
        assert [s["round"] for s in candidate["stations"]] == list(range(1, 7))
    for round_row in rounds:
        stations = [slot["station_id"] for slot in round_row["slots"]]
        candidates_in_round = [slot["candidate_id"] for slot in round_row["slots"]]
        assert len(stations) == len(set(stations)) == 8
        assert len(candidates_in_round) == len(set(candidates_in_round)) == 8
    assert schedule["quality"]["difficulty_spread"] <= 1.0
    assert schedule["quality"]["late_difficulty_spread"] <= 1.0
    assert schedule["quality"]["total_minutes"] > 0
    assert any("延长" in w for w in schedule["quality"]["warnings"])
    assert any("安静候考" in w for w in schedule["quality"]["warnings"])


def test_schedule_is_persisted_and_deterministic(conn):
    first = generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=42)
    second = generate_schedule(conn, exam_date="2026-12-10", rounds=6, seed=42)
    first_signature = {
        c["candidate_id"]: tuple(s["station_id"] for s in c["stations"])
        for c in first["candidates"]
    }
    second_signature = {
        c["candidate_id"]: tuple(s["station_id"] for s in c["stations"])
        for c in second["candidates"]
    }
    assert first_signature == second_signature
    latest = latest_schedule(conn)
    assert latest["schedule_id"] == second["schedule_id"]
    rows = conn.execute("SELECT COUNT(*) AS n FROM exam_schedules").fetchone()["n"]
    assert rows == 2


def test_invalid_rounds_rejected(conn):
    with pytest.raises(ValueError):
        generate_schedule(conn, exam_date="2026-12-10", rounds=11)
    with pytest.raises(ValueError):
        generate_schedule(conn, exam_date="2026-12-10", rounds=6, buffer_min=-1)
