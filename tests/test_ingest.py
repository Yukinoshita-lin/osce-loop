"""离线录入：转写解析、说话人/阶段推断与评分入库。"""
from __future__ import annotations

import json

import pytest

from app import db
from app.ai.base import CapabilityGateway
from app.ai.mock import MockScoringProvider
from app.config import MODEL_VERSION, PROMPT_VERSION
from app.domain.bundle import freeze_bundle
from app.domain.ingest import create_ingested_attempt
from app.domain.transcript import parse_notes, parse_transcript
from app.seed import build_bundle_payload

SAMPLE_JSON = [
    {"speaker": "patient", "start_ms": 0, "end_ms": 5000, "text": "大夫，我胸口疼。"},
    {"speaker": "candidate", "start_ms": 6000, "end_ms": 11000, "text": "您好，我是急诊科张医生，请问怎么称呼？"},
    {"speaker": "patient", "start_ms": 12000, "end_ms": 15000, "text": "我叫王建国。"},
    {"speaker": "candidate", "start_ms": 16000, "end_ms": 22000, "text": "疼多久了？有没有向肩膀或胳膊放射？"},
    {"speaker": "patient", "start_ms": 23000, "end_ms": 27000, "text": "两个多小时了，会窜到左边胳膊。"},
    {"speaker": "candidate", "start_ms": 28000, "end_ms": 34000, "text": "有没有出汗、恶心？"},
    {"speaker": "patient", "start_ms": 35000, "end_ms": 39000, "text": "出了不少汗，有点恶心。"},
    {"speaker": "candidate", "start_ms": 40000, "end_ms": 48000, "text": "我先给您听诊一下，然后做心电图和心电监护。"},
    {"speaker": "candidate", "start_ms": 49000, "end_ms": 56000, "text": "持续胸痛伴出汗是危险信号，需要马上处理。我总结一下。"},
]


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "ingest.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def bundle(conn):
    return freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")


def test_parse_json_transcript():
    parsed = parse_transcript("sample.json", json.dumps(SAMPLE_JSON, ensure_ascii=False).encode("utf-8"))
    assert parsed["format"] == "json"
    assert len(parsed["utterances"]) == 9
    assert parsed["candidate_utterances"] == 5
    assert parsed["patient_utterances"] == 4
    assert parsed["duration_ms"] == 56000
    assert parsed["utterances"][0]["start_ms"] == 0
    assert parsed["utterances"][1]["start_ms"] == 6000
    phases = [u["phase"] for u in parsed["utterances"]]
    assert "information_gathering" in phases
    assert "assessment" in phases
    assert "plan" in phases
    assert parsed["warnings"]


def test_parse_srt_and_speaker_prefix():
    srt = """1
00:00:00,000 --> 00:00:03,000
患者：大夫，我胸口疼。

2
00:00:04,000 --> 00:00:08,000
医生：您好，我是急诊科张医生。

3
00:00:09,000 --> 00:00:14,000
医生：疼多久了？有没有出汗？

4
00:00:15,000 --> 00:00:19,000
医生：我先给您听诊一下，然后做心电图。

5
00:00:20,000 --> 00:00:25,000
医生：我们马上做心电图和心电监护。
"""
    parsed = parse_transcript("sample.srt", srt.encode("utf-8"))
    assert parsed["format"] == "srt"
    assert parsed["utterances"][0]["speaker"] == "patient"
    assert parsed["utterances"][1]["speaker"] == "candidate"
    assert parsed["utterances"][1]["text"].startswith("您好")
    assert any(u["phase"] == "assessment" for u in parsed["utterances"])
    assert parsed["utterances"][-1]["phase"] == "plan"


def test_parse_srt_without_prefix_alternates_and_warns():
    srt = """1
00:00:00,000 --> 00:00:03,000
大夫，我胸口疼。

2
00:00:04,000 --> 00:00:08,000
您好，我是急诊科张医生。
"""
    parsed = parse_transcript("sample.srt", srt.encode("utf-8"))
    assert {u["speaker"] for u in parsed["utterances"]} == {"patient", "candidate"}
    assert any("交替推断" in warning for warning in parsed["warnings"])


def test_parse_invalid_transcript():
    with pytest.raises(ValueError):
        parse_transcript("bad.json", b"{not-json}")
    with pytest.raises(ValueError):
        parse_transcript("empty.srt", b"")


def test_parse_notes_supports_timestamped_lines_and_plain_text():
    notes = parse_notes(
        "[00:08] 我先给您听诊一下，然后做心电图。\n已完成知情沟通。",
        duration_ms=7000,
    )
    assert len(notes) == 2
    first, second = notes
    assert first["source"] == "note"
    assert first["speaker"] == "candidate"
    assert first["start_ms"] == 8000
    assert first["phase"] == "assessment"
    assert first["note_timestamp_supplied"] is True
    assert first["asr_confidence"] is None
    assert second["note_timestamp_supplied"] is False
    assert second["start_ms"] >= first["end_ms"]


def test_ingest_with_notes_produces_note_evidence(conn, bundle):
    transcript = [
        {
            "utt_id": "u001",
            "speaker": "patient",
            "start_ms": 0,
            "end_ms": 3000,
            "text": "大夫，我胸口疼。",
            "asr_confidence": 0.95,
            "phase": "opening",
        },
        {
            "utt_id": "u002",
            "speaker": "candidate",
            "start_ms": 4000,
            "end_ms": 6000,
            "text": "您好，我是急诊科张医生。",
            "asr_confidence": 0.95,
            "phase": "opening",
        },
    ] + parse_notes(
        "[00:08] 我先给您听诊一下，然后做心电图和心电监护。",
        duration_ms=7000,
    )
    gateway = CapabilityGateway(conn, MockScoringProvider(), MODEL_VERSION, PROMPT_VERSION)
    attempt_id = create_ingested_attempt(
        conn,
        gateway,
        bundle=bundle,
        transcript=transcript,
        candidate_code="C-NOTES-001",
        candidate_name="笔记考生",
        risk_level="L2",
        release_stage="G0_offline",
        media_id="media_notes",
        media_url="/media/uploads/notes.wav",
        media_filename="notes.wav",
        actor="test",
    )
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    assert attempt["asr_confidence"] == 0.95
    stored = json.loads(
        conn.execute(
            "SELECT payload FROM transcripts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()["payload"]
    )
    assert any(utterance.get("source") == "note" for utterance in stored)
    records = conn.execute(
        "SELECT ai_evidence FROM score_records WHERE attempt_id = ? AND ai_evidence != '[]'",
        (attempt_id,),
    ).fetchall()
    evidence = [
        entry
        for row in records
        for entry in json.loads(row["ai_evidence"])
        if entry.get("evidence_kind") == "note"
    ]
    assert evidence
    assert evidence[0]["speaker"] == "candidate"
    assert "笔记" in (evidence[0].get("note") or "")


def test_ingest_records_automatic_anomalies(conn, bundle):
    transcript = [
        {"speaker": "patient", "start_ms": 0, "end_ms": 3000, "text": "大夫，我胸痛。"},
        {"speaker": "candidate", "start_ms": 6000, "end_ms": 9000, "text": "疼多久了？"},
        {"speaker": "patient", "start_ms": 75000, "end_ms": 78000, "text": "疼死了，受不了。"},
    ]
    gateway = CapabilityGateway(conn, MockScoringProvider(), MODEL_VERSION, PROMPT_VERSION)
    attempt_id = create_ingested_attempt(
        conn,
        gateway,
        bundle=bundle,
        transcript=transcript,
        candidate_code="C-ANOMALY-001",
        candidate_name="异常考生",
        risk_level="L2",
        release_stage="G0_offline",
        media_id="media_anomaly",
        media_url="/media/uploads/anomaly.wav",
        media_filename="anomaly.wav",
        actor="test",
    )
    attempt = conn.execute(
        "SELECT anomalies FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    anomalies = json.loads(attempt["anomalies"])
    types = {entry["type"] for entry in anomalies}
    assert {"silence", "emotion"} <= types
    assert any("沉默" in entry["label"] for entry in anomalies)
    assert any("情绪" in entry["label"] for entry in anomalies)


def test_ingest_creates_scored_attempt(conn, bundle):
    parsed = parse_transcript("sample.json", json.dumps(SAMPLE_JSON, ensure_ascii=False).encode("utf-8"))
    gateway = CapabilityGateway(conn, MockScoringProvider(), MODEL_VERSION, PROMPT_VERSION)
    attempt_id = create_ingested_attempt(
        conn,
        gateway,
        bundle=bundle,
        transcript=parsed["utterances"],
        candidate_code="C-UPLOAD-001",
        candidate_name="上传考生",
        risk_level="L2",
        release_stage="G0_offline",
        media_id="media_1",
        media_url="/media/uploads/test.wav",
        media_filename="test.wav",
        media_sha256="sha256:test",
        actor="test",
    )
    attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    assert attempt["candidate_code"] == "C-UPLOAD-001"
    assert attempt["media_sha256"] == "sha256:test"
    assert attempt["integrity_gap"] == 0
    assert attempt["status"] == "pending_review"
    assert attempt["retention_class"] == "L2"
    assert attempt["retain_until"] is not None
    records = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ?", (attempt_id,)
    ).fetchall()
    assert len(records) == 15
    assert all(rec["rubric_version"] == bundle["bundle_hash"] for rec in records)
    assert any(rec["ai_evidence"] and rec["ai_evidence"] != "[]" for rec in records)
    transcript = conn.execute(
        "SELECT payload FROM transcripts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    assert len(json.loads(transcript["payload"])) == 9


def test_ingest_without_media_marks_integrity_gap(conn, bundle):
    parsed = parse_transcript("sample.json", json.dumps(SAMPLE_JSON, ensure_ascii=False).encode("utf-8"))
    gateway = CapabilityGateway(conn, MockScoringProvider(), MODEL_VERSION, PROMPT_VERSION)
    attempt_id = create_ingested_attempt(
        conn,
        gateway,
        bundle=bundle,
        transcript=parsed["utterances"],
        candidate_code="C-UPLOAD-002",
        candidate_name="无媒体考生",
        risk_level="L2",
        release_stage="G0_offline",
        media_id="upload_no_media",
        media_url=None,
        media_filename=None,
        actor="test",
    )
    attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    assert attempt["integrity_gap"] == 1
    assert attempt["status"] == "integrity_hold"
    record = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ? AND item_id = 'HX.01'", (attempt_id,)
    ).fetchone()
    routing = json.loads(record["routing"])
    assert routing["reason"] == "integrity_gap"
    assert routing["requires_human"] is True
