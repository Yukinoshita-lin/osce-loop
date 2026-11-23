"""考试执行台：轮次推进、事实层提示、异常与终止。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.execution import (
    advance_run,
    latest_run,
    slot_action,
    start_run,
    stop_run,
    toggle_hints,
)
from app.domain.scheduling import ensure_schedule_inputs, generate_schedule


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "execution.db")
    db.init_db(connection)
    ensure_schedule_inputs(connection)
    generate_schedule(connection, exam_date="2026-12-10", rounds=6, seed=2026)
    yield connection
    connection.close()


def test_start_and_advance_run(conn):
    run = start_run(conn, actor="test")
    assert run["status"] == "running"
    assert run["current_round"] == 1
    assert run["counts"]["total"] == 48
    assert run["counts"]["in_progress"] == 8
    assert run["counts"]["waiting"] == 40
    assert run["hints_enabled"] is True
    assert run["events"][0]["event_type"] == "run_started"
    advanced = advance_run(conn, run["run_id"], actor="test")
    assert advanced["current_round"] == 2
    assert advanced["counts"]["completed"] == 8
    assert advanced["counts"]["in_progress"] == 8
    assert any(ev["event_type"] == "round_advanced" for ev in advanced["events"])


def test_slot_anomaly_and_termination(conn):
    run = start_run(conn, actor="test")
    slot_key = run["slots"][0]["slot_key"]
    updated = slot_action(conn, run["run_id"], slot_key, action="anomaly", note="设备音频中断", actor="test")
    slot = next(s for s in updated["slots"] if s["slot_key"] == slot_key)
    assert slot["status"] == "anomaly"
    assert slot["note"] == "设备音频中断"
    assert any(ev["event_type"] == "slot_anomaly" for ev in updated["events"])
    terminated = slot_action(
        conn,
        run["run_id"],
        slot_key,
        action="terminate",
        note="设备无法恢复",
        actor="test",
    )
    slot = next(s for s in terminated["slots"] if s["slot_key"] == slot_key)
    assert slot["status"] == "terminated"
    assert any(ev["event_type"] == "slot_terminated" and ev["severity"] == "critical" for ev in terminated["events"])


def test_hints_toggle_and_stop(conn):
    run = start_run(conn, actor="test")
    updated = toggle_hints(conn, run["run_id"], enabled=False, actor="test")
    assert updated["hints_enabled"] is False
    assert any(ev["event_type"] == "hints_toggled" and "关闭" in ev["message"] for ev in updated["events"])
    stopped = stop_run(conn, run["run_id"], reason="现场异常", actor="test")
    assert stopped["status"] == "stopped"
    assert stopped["counts"]["stopped"] > 0
    assert any(ev["event_type"] == "run_stopped" for ev in stopped["events"])
    latest = latest_run(conn)
    assert latest["run_id"] == run["run_id"]
    assert latest["status"] == "stopped"


def test_cannot_start_two_active_runs(conn):
    start_run(conn, actor="test")
    with pytest.raises(ValueError):
        start_run(conn, actor="test")
