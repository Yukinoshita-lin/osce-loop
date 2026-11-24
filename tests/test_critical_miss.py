"""关键项漏判：识别、逐条归因与审计留痕。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.critical_miss import critical_misses, record_critical_miss_review
from app.seed import build_bundle_payload


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "critical.db")
    db.init_db(connection)
    bundle = freeze_bundle(connection, build_bundle_payload(), actor="测试", version="1.0")
    connection.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("att_1", bundle["bundle_id"], "C001", "考生", "SES", "L3", "scored", "2026-12-10T09:00:00+08:00"),
    )
    connection.commit()
    return connection, bundle


def _record(conn, record_id, item_id, ai_score, gold, final, status="reviewed", attempt_id="att_1"):
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, gold_score, final_score, "
        "routing, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (record_id, attempt_id, item_id, ai_score, gold, final, "{}", status, "2026-12-10T09:10:00+08:00"),
    )


def test_critical_miss_requires_ai_negative_and_positive_reference(conn):
    connection, bundle = conn
    items = bundle["payload"]["rubric"]
    critical_ids = [item["item_id"] for item in items if item.get("critical")]
    normal_id = next(item["item_id"] for item in items if not item.get("critical"))
    assert len(critical_ids) >= 2
    miss_id, agreed_id = critical_ids[0], critical_ids[1]
    _record(connection, "rec_miss", miss_id, 0, 2, 2)
    _record(connection, "rec_agree", agreed_id, 0, 0, 0)
    _record(connection, "rec_normal", normal_id, 0, 2, 2)
    connection.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("att_2", bundle["bundle_id"], "C002", "考生二", "SES", "L3", "scored", "2026-12-10T10:00:00+08:00"),
    )
    _record(connection, "rec_not_negative", miss_id, 1, 2, 2, attempt_id="att_2")
    connection.commit()

    result = critical_misses(connection)
    assert result["total"] == 1
    assert result["unresolved"] == 1
    assert result["resolved"] == 0
    item = result["items"][0]
    assert item["record_id"] == "rec_miss"
    assert item["item_id"] == miss_id
    assert item["reference_score"] == 2
    assert item["reference_source"] == "gold"
    assert item["resolved"] is False
    assert {entry["key"] for entry in result["root_causes"]} >= {"evidence_gap", "anchor_ambiguity"}


def test_human_reference_counts_when_gold_missing(conn):
    connection, bundle = conn
    miss_id = next(item["item_id"] for item in bundle["payload"]["rubric"] if item.get("critical"))
    _record(connection, "rec_human", miss_id, 0, None, 1, status="corrected")
    connection.commit()

    result = critical_misses(connection)
    assert result["total"] == 1
    assert result["items"][0]["reference_source"] == "human"
    assert result["items"][0]["reference_score"] == 1


def test_review_requires_valid_reason_and_action(conn):
    connection, bundle = conn
    miss_id = next(item["item_id"] for item in bundle["payload"]["rubric"] if item.get("critical"))
    _record(connection, "rec_miss", miss_id, 0, 2, 2)
    connection.commit()

    with pytest.raises(ValueError, match="根因分类"):
        record_critical_miss_review(
            connection, "rec_miss", root_cause="bad", action="rubric_revision", note="说明足够长", actor="考务"
        )
    with pytest.raises(ValueError, match="纠正动作"):
        record_critical_miss_review(
            connection, "rec_miss", root_cause="evidence_gap", action="bad", note="说明足够长", actor="考务"
        )
    with pytest.raises(ValueError, match="不少于"):
        record_critical_miss_review(
            connection, "rec_miss", root_cause="evidence_gap", action="rubric_revision", note="太短", actor="考务"
        )
    with pytest.raises(ValueError, match="不是关键项漏判"):
        record_critical_miss_review(
            connection, "rec_missing", root_cause="evidence_gap", action="rubric_revision", note="说明足够长了", actor="考务"
        )

    review = record_critical_miss_review(
        connection,
        "rec_miss",
        root_cause="evidence_gap",
        action="evidence_probe",
        note="探针未覆盖否认表述",
        actor="考务 李萍",
    )
    assert review["root_cause"] == "evidence_gap"
    assert review["action"] == "evidence_probe"
    state = critical_misses(connection)
    assert state["unresolved"] == 0
    assert state["resolved"] == 1
    assert state["items"][0]["review"]["actor"] == "考务 李萍"

    updated = record_critical_miss_review(
        connection,
        "rec_miss",
        root_cause="anchor_ambiguity",
        action="rubric_revision",
        note="行为锚歧义，已改为可观察表述",
        actor="考务 李萍",
    )
    assert updated["root_cause"] == "anchor_ambiguity"
    events = connection.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'quality.critical_miss_review'"
    ).fetchone()["n"]
    assert events == 2
