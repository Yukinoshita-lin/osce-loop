"""执行槽位与场次录入联动。"""
from __future__ import annotations

import json

import pytest

from app import db
from app.ai.base import CapabilityGateway
from app.ai.mock import MockScoringProvider
from app.config import MODEL_VERSION, PROMPT_VERSION
from app.domain.bundle import list_bundles
from app.domain.execution import (
    acknowledge_live_anomalies,
    append_live_segment,
    advance_run,
    get_run,
    start_run,
)
from app.domain.ingest import create_ingested_attempt
from app.domain.scheduling import ensure_schedule_inputs, generate_schedule
from app.seed import seed_if_empty


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "execution_ingest.db")
    db.init_db(connection)
    seed_if_empty(connection)
    ensure_schedule_inputs(connection)
    generate_schedule(connection, exam_date="2026-12-20", rounds=2, seed=7)
    yield connection
    connection.close()


def _transcript() -> list[dict]:
    return [
        {
            "utt_id": "u1",
            "speaker": "candidate",
            "start_ms": 1000,
            "end_ms": 4000,
            "text": "您好，我是急诊科医生，请问您怎么称呼？",
            "asr_confidence": 0.97,
            "phase": "opening",
        },
        {
            "utt_id": "u2",
            "speaker": "patient",
            "start_ms": 4500,
            "end_ms": 7000,
            "text": "我叫王建国，今年六十五。",
            "asr_confidence": 0.96,
            "phase": "opening",
        },
        {
            "utt_id": "u3",
            "speaker": "candidate",
            "start_ms": 7500,
            "end_ms": 12000,
            "text": "胸痛多久了？有没有向肩膀或下巴放射？",
            "asr_confidence": 0.95,
            "phase": "information_gathering",
        },
    ]


def _ingest(conn, run, slot, *, slot_key=None):
    gateway = CapabilityGateway(conn, MockScoringProvider(), MODEL_VERSION, PROMPT_VERSION)
    bundle = list_bundles(conn)[0]
    return create_ingested_attempt(
        conn,
        gateway,
        bundle=bundle,
        transcript=_transcript(),
        candidate_code=slot["candidate_id"],
        candidate_name=slot["candidate_name"],
        risk_level="L1",
        release_stage="G1_practice",
        media_id="test_media",
        media_url=None,
        media_filename=None,
        actor="考务测试",
        run_id=run["run_id"],
        slot_key=slot_key or slot["slot_key"],
    )


def test_ingest_links_attempt_to_run_slot(conn):
    run = start_run(conn, actor="考务测试")
    slot = run["slots"][0]
    attempt_id = _ingest(conn, run, slot)

    attempt = conn.execute(
        "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    assert attempt["run_id"] == run["run_id"]
    assert attempt["slot_key"] == slot["slot_key"]

    slot_row = conn.execute(
        "SELECT * FROM exam_run_slots WHERE run_id = ? AND slot_key = ?",
        (run["run_id"], slot["slot_key"]),
    ).fetchone()
    assert slot_row["status"] == "completed"
    assert slot_row["attempt_id"] == attempt_id

    refreshed = get_run(conn, run["run_id"])
    linked = next(s for s in refreshed["slots"] if s["slot_key"] == slot["slot_key"])
    assert linked["attempt"]["attempt_id"] == attempt_id
    facts = linked["evidence_facts"]
    assert facts["utterances"] == 2
    assert facts["measured"] > 0
    assert facts["covered"] > 0
    assert facts["missing"] > 0
    assert any("尚无证据的评分项" in fact for fact in facts["facts"])
    assert refreshed["counts"]["ingested"] == 1
    assert refreshed["integrity"]["ingested"] == 1
    assert refreshed["integrity"]["missing"] == refreshed["counts"]["total"] - 1
    assert 0 < refreshed["integrity"]["coverage"] <= 1

    event = conn.execute(
        "SELECT * FROM exam_events WHERE run_id = ? AND event_type = 'slot_ingested'",
        (run["run_id"],),
    ).fetchone()
    assert event is not None
    audit = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'exam.slot_ingest' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert audit is not None


def test_slot_cannot_be_ingested_twice(conn):
    run = start_run(conn, actor="考务测试")
    slot = run["slots"][0]
    _ingest(conn, run, slot)
    with pytest.raises(ValueError, match="已录入"):
        _ingest(conn, run, slot)


def test_unknown_slot_is_rejected(conn):
    run = start_run(conn, actor="考务测试")
    slot = run["slots"][0]
    with pytest.raises(ValueError, match="不存在"):
        _ingest(conn, run, slot, slot_key="missing__9")


def test_completion_records_integrity_gap(conn):
    run = start_run(conn, actor="考务测试")
    slot = run["slots"][0]
    _ingest(conn, run, slot)
    advance_run(conn, run["run_id"], actor="考务测试")
    completed = advance_run(conn, run["run_id"], actor="考务测试")
    assert completed["status"] == "completed"
    assert completed["integrity"]["missing"] >= 1
    event = conn.execute(
        "SELECT * FROM exam_events WHERE run_id = ? AND event_type = 'run_integrity_gap'",
        (run["run_id"],),
    ).fetchone()
    assert event is not None
    assert str(completed["integrity"]["missing"]) in event["message"]


def test_live_segments_update_evidence_facts(conn):
    run = start_run(conn, actor="考务测试")
    slot = next(s for s in run["slots"] if s["station_name"] == "急性胸痛")
    first = append_live_segment(
        conn,
        run["run_id"],
        slot["slot_key"],
        speaker="candidate",
        text="我是急诊科医生，请问您怎么称呼？",
    )
    assert first["facts"]["segment_count"] == 1
    first_facts = first["facts"]["facts"]
    assert first_facts["utterances"] == 1
    assert any("尚无证据" in fact for fact in first_facts["facts"])

    second = append_live_segment(
        conn,
        run["run_id"],
        slot["slot_key"],
        speaker="candidate",
        text="胸痛多久了？有没有向肩膀或下巴放射？",
    )
    second_facts = second["facts"]["facts"]
    assert second["facts"]["segment_count"] == 2
    assert second_facts["missing"] < first_facts["missing"]
    assert second_facts["covered"] > first_facts["covered"]

    third = append_live_segment(
        conn,
        run["run_id"],
        slot["slot_key"],
        speaker="patient",
        text="疼死我了，受不了",
        start_ms=80000,
        end_ms=82000,
    )
    anomaly_types = {item["type"] for item in third["facts"]["anomalies"]}
    assert "emotion" in anomaly_types
    assert "silence" in anomaly_types
    assert {item["type"] for item in third["new_anomalies"]} == {"emotion", "silence"}

    duplicate = append_live_segment(
        conn,
        run["run_id"],
        slot["slot_key"],
        speaker="patient",
        text="疼死我了，受不了",
        start_ms=80000,
        end_ms=82000,
    )
    assert duplicate["new_anomalies"] == []

    refreshed = get_run(conn, run["run_id"])
    linked = next(s for s in refreshed["slots"] if s["slot_key"] == slot["slot_key"])
    assert linked["live_segment_count"] == 4
    assert linked["evidence_facts"]["utterances"] == 2
    assert any(item["type"] == "emotion" for item in linked["live_anomalies"])
    event = conn.execute(
        "SELECT COUNT(*) AS n FROM exam_events WHERE run_id = ? AND event_type = 'live_segment'",
        (run["run_id"],),
    ).fetchone()
    assert event["n"] == 4
    anomaly_events = conn.execute(
        "SELECT payload FROM exam_events WHERE run_id = ? AND event_type = 'live_anomaly'",
        (run["run_id"],),
    ).fetchall()
    assert len(anomaly_events) == 2
    assert {json.loads(row["payload"])["type"] for row in anomaly_events} == {"silence", "emotion"}

    ack = acknowledge_live_anomalies(
        conn,
        run["run_id"],
        slot["slot_key"],
        note="已提示考官安抚患者并检查设备",
        actor="考务测试",
    )
    assert {item["type"] for item in ack["acknowledged"]} == {"silence", "emotion"}
    assert ack["facts"]["anomalies"] == []
    after_ack = get_run(conn, run["run_id"])
    acked_slot = next(
        s for s in after_ack["slots"] if s["slot_key"] == slot["slot_key"]
    )
    assert acked_slot["live_anomalies"] == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM exam_events WHERE run_id = ? AND event_type = 'live_anomaly_ack'",
        (run["run_id"],),
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'exam.live_anomaly_ack' "
        "AND object_id = ?",
        (f"{run['run_id']}:{slot['slot_key']}",),
    ).fetchone()["n"] == 1

    after_ack_segment = append_live_segment(
        conn,
        run["run_id"],
        slot["slot_key"],
        speaker="patient",
        text="我还是很疼，受不了",
        start_ms=90000,
        end_ms=92000,
    )
    assert after_ack_segment["new_anomalies"] == []
