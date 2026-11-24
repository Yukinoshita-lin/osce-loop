"""S4 软约束权重确认：权重语义、签字留痕与排考绑定。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.schedule_policy import (
    confirm_policy,
    get_policy,
    latest_policy,
    presets_payload,
)
from app.domain.scheduling import (
    ensure_schedule_inputs,
    generate_schedule,
    latest_schedule,
    policy_score,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "policy.db")
    db.init_db(connection)
    ensure_schedule_inputs(connection)
    yield connection
    connection.close()


def test_policy_score_switches_priority():
    fairness_first = policy_score(
        difficulty_spread=0.2,
        late_spread=0.2,
        usage_spread=3,
        max_usage=7,
        fairness_weight=0.9,
    )
    usage_heavy = policy_score(
        difficulty_spread=0.8,
        late_spread=0.4,
        usage_spread=1,
        max_usage=5,
        fairness_weight=0.9,
    )
    assert fairness_first < usage_heavy

    usage_first = policy_score(
        difficulty_spread=0.8,
        late_spread=0.4,
        usage_spread=1,
        max_usage=5,
        fairness_weight=0.1,
    )
    difficulty_heavy = policy_score(
        difficulty_spread=0.2,
        late_spread=0.2,
        usage_spread=3,
        max_usage=7,
        fairness_weight=0.1,
    )
    assert usage_first < difficulty_heavy

    over_limit = policy_score(
        difficulty_spread=1.5,
        late_spread=0.2,
        usage_spread=0,
        max_usage=6,
        fairness_weight=0.1,
    )
    assert difficulty_heavy < over_limit


def test_confirm_policy_requires_note_and_valid_weight(conn):
    with pytest.raises(ValueError, match="理由"):
        confirm_policy(conn, preset="fairness", note="", actor="考务")
    with pytest.raises(ValueError, match="0 到 1"):
        confirm_policy(
            conn, preset="balanced", fairness_weight=1.2, note="权重过高", actor="考务"
        )
    with pytest.raises(ValueError, match="preset"):
        confirm_policy(conn, preset="unknown", note="非法预设", actor="考务")

    policy = confirm_policy(
        conn,
        preset="fairness",
        note="本次模拟考优先保证考生间难度公平",
        actor="考务 李萍",
    )
    assert policy["fairness_weight"] == 0.75
    assert policy["label"] == "公平优先"
    assert policy["actor"] == "考务 李萍"
    assert latest_policy(conn)["policy_id"] == policy["policy_id"]
    assert get_policy(conn, policy["policy_id"])["note"] == "本次模拟考优先保证考生间难度公平"

    custom = confirm_policy(
        conn,
        preset="balanced",
        fairness_weight=0.33,
        note="本期更关注资源占用",
        actor="考务 李萍",
    )
    assert custom["fairness_weight"] == 0.33
    assert latest_policy(conn)["policy_id"] == custom["policy_id"]
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'exam.schedule_policy_confirmed'"
    ).fetchone()["n"]
    assert audit == 2
    assert {item["preset"] for item in presets_payload()} == {"fairness", "balanced", "efficiency"}


def test_generate_schedule_binds_confirmed_policy(conn):
    policy = confirm_policy(
        conn,
        preset="efficiency",
        note="本次排考优先压缩考站使用次数",
        actor="考务 李萍",
    )
    schedule = generate_schedule(
        conn,
        exam_date="2026-12-10",
        rounds=6,
        seed=2026,
        fairness_weight=policy["fairness_weight"],
        policy=policy,
    )
    assert schedule["policy"]["policy_id"] == policy["policy_id"]
    assert schedule["policy"]["label"] == "效率优先"
    assert schedule["policy"]["fairness_weight"] == 0.25
    assert schedule["policy"]["confirmed_by"] == "考务 李萍"
    assert schedule["quality"]["difficulty_spread"] <= 1.0
    assert latest_schedule(conn)["policy"]["policy_id"] == policy["policy_id"]

    with pytest.raises(ValueError, match="0 到 1"):
        generate_schedule(conn, exam_date="2026-12-10", fairness_weight=1.5)
    with pytest.raises(ValueError, match="数字"):
        generate_schedule(conn, exam_date="2026-12-10", fairness_weight="high")
