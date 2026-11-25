"""重跑评分：允许模型迭代后重算未复核场次，同时保住金标准与人工结论。"""
from __future__ import annotations

import json

import pytest

from app import db
from app.ai.base import CapabilityGateway
from app.ai.mock import MockScoringProvider
from app.config import MODEL_VERSION, PROMPT_VERSION
from app.domain.bundle import freeze_bundle
from app.domain.ingest import create_ingested_attempt
from app.domain.rescore import rescore_attempt
from app.domain.transcript import parse_transcript
from app.seed import build_bundle_payload

SAMPLE = [
    {"speaker": "patient", "start_ms": 0, "end_ms": 5000, "text": "大夫，我胸口疼。"},
    {"speaker": "candidate", "start_ms": 6000, "end_ms": 11000, "text": "您好，我是急诊科张医生，请问怎么称呼？"},
    {"speaker": "patient", "start_ms": 12000, "end_ms": 15000, "text": "我叫王建国。"},
    {"speaker": "candidate", "start_ms": 16000, "end_ms": 22000, "text": "疼多久了？有没有向肩膀或胳膊放射？"},
    {"speaker": "patient", "start_ms": 23000, "end_ms": 27000, "text": "两个多小时了，会窜到左边胳膊。"},
    {"speaker": "candidate", "start_ms": 28000, "end_ms": 34000, "text": "有没有出汗、恶心？"},
    {"speaker": "patient", "start_ms": 35000, "end_ms": 39000, "text": "出了不少汗，有点恶心。"},
]


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "rescore.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def bundle(conn):
    return freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")


def _ingest(conn, bundle):
    parsed = parse_transcript("sample.json", json.dumps(SAMPLE, ensure_ascii=False).encode("utf-8"))
    gateway = CapabilityGateway(conn, MockScoringProvider(), MODEL_VERSION, PROMPT_VERSION)
    return create_ingested_attempt(
        conn,
        gateway,
        bundle=bundle,
        transcript=parsed["utterances"],
        candidate_code="C-RESCORE-001",
        candidate_name="重跑考生",
        risk_level="L2",
        release_stage="G0_offline",
        media_id="media_rescore",
        media_url="/media/uploads/rescore.wav",
        media_filename="rescore.wav",
        actor="test",
    )


def _new_gateway(conn):
    return CapabilityGateway(conn, MockScoringProvider(), "mock-score-2.0", "score-prompt-2.0")


def test_rescore_updates_versions_and_preserves_gold(conn, bundle):
    attempt_id = _ingest(conn, bundle)
    first_record = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ? ORDER BY item_id LIMIT 1", (attempt_id,)
    ).fetchone()
    conn.execute(
        "UPDATE score_records SET gold_score = 2 WHERE record_id = ?", (first_record["record_id"],)
    )
    conn.commit()
    result = rescore_attempt(conn, _new_gateway(conn), attempt_id=attempt_id, actor="测试")
    assert result["records"] == 15
    assert result["previous_records"] == 15
    assert result["preserved_gold"] == 1
    records = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ?", (attempt_id,)
    ).fetchall()
    assert len(records) == 15
    ai_records = [record for record in records if record["model_version"] is not None]
    assert ai_records
    assert all(record["model_version"] == "mock-score-2.0" for record in ai_records)
    assert all(record["prompt_version"] == "score-prompt-2.0" for record in ai_records)
    kept = [record for record in records if record["record_id"] == first_record["record_id"]]
    assert kept and kept[0]["gold_score"] == 2
    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'attempt.rescored' AND object_id = ?",
        (attempt_id,),
    ).fetchone()
    assert event is not None
    assert json.loads(event["after"])["rerun"] is True


def test_rescore_blocked_after_review_action(conn, bundle):
    attempt_id = _ingest(conn, bundle)
    record = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ? LIMIT 1", (attempt_id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO review_actions(action_id, record_id, reviewer, action, from_score, to_score, "
        "reason_code, seconds_spent, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("act_test", record["record_id"], "评分员", "accept", 2, 2, None, 10.0, db.now_iso()),
    )
    conn.commit()
    with pytest.raises(ValueError, match="复核动作"):
        rescore_attempt(conn, _new_gateway(conn), attempt_id=attempt_id)


def test_rescore_blocked_after_reviewer_assignment(conn, bundle):
    attempt_id = _ingest(conn, bundle)
    record = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ? LIMIT 1", (attempt_id,)
    ).fetchone()
    conn.execute(
        "UPDATE score_records SET reviewer = ?, reviewed_at = ? WHERE record_id = ?",
        ("评分员", db.now_iso(), record["record_id"]),
    )
    conn.commit()
    with pytest.raises(ValueError, match="人工复核"):
        rescore_attempt(conn, _new_gateway(conn), attempt_id=attempt_id)


@pytest.mark.parametrize("status", ["reviewed", "signed_off", "published"])
def test_rescore_blocked_for_locked_attempt(conn, bundle, status):
    attempt_id = _ingest(conn, bundle)
    conn.execute("UPDATE attempts SET status = ? WHERE attempt_id = ?", (status, attempt_id))
    conn.commit()
    with pytest.raises(ValueError, match="不能重跑评分"):
        rescore_attempt(conn, _new_gateway(conn), attempt_id=attempt_id)


def test_rescore_requires_transcript(conn, bundle):
    attempt_id = _ingest(conn, bundle)
    conn.execute("DELETE FROM transcripts WHERE attempt_id = ?", (attempt_id,))
    conn.commit()
    with pytest.raises(ValueError, match="缺少转写"):
        rescore_attempt(conn, _new_gateway(conn), attempt_id=attempt_id)
