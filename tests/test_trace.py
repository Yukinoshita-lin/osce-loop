"""成绩—证据—操作互查：单条成绩的全链路追溯。"""
from __future__ import annotations

import json

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.integrity import quote_digest, transcript_digest
from app.domain.trace import score_trace
from app.seed import build_bundle_payload

CANDIDATE_TEXT = "您好，我是急诊科张医生，请问您怎么称呼？"
PATIENT_TEXT = "我叫王建国。"


def _setup(tmp_path):
    conn = db.connect(tmp_path / "trace.db")
    db.init_db(conn)
    payload = build_bundle_payload()
    payload["station_id"] = "ST-TRACE-001"
    bundle = freeze_bundle(conn, payload, actor="测试", version="1.0")
    utterances = [
        {
            "utt_id": "u001",
            "speaker": "candidate",
            "start_ms": 0,
            "end_ms": 3000,
            "text": CANDIDATE_TEXT,
            "asr_confidence": 0.98,
            "phase": "opening",
        },
        {
            "utt_id": "u002",
            "speaker": "patient",
            "start_ms": 3200,
            "end_ms": 5000,
            "text": PATIENT_TEXT,
            "asr_confidence": 0.97,
            "phase": "opening",
        },
    ]
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, media_id, media_url, media_sha256, duration_ms, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "att_trace",
            bundle["bundle_id"],
            "C001",
            "考生一",
            "SES",
            "L2",
            "demo.wav",
            "/media/demo.wav",
            "sha256:media",
            5000,
            "published",
            "2026-01-01T09:00:00+08:00",
        ),
    )
    conn.execute(
        "INSERT INTO transcripts(attempt_id, payload, sha256, created_at) VALUES(?,?,?,?)",
        (
            "att_trace",
            json.dumps(utterances, ensure_ascii=False),
            transcript_digest(utterances),
            "2026-01-01T09:10:00+08:00",
        ),
    )
    evidence = [
        {
            "media_id": "demo.wav",
            "start_ms": 0,
            "end_ms": 3000,
            "speaker": "candidate",
            "quote": CANDIDATE_TEXT,
            "transcript_hash": quote_digest(CANDIDATE_TEXT),
            "evidence_kind": "utterance",
            "note": None,
        }
    ]
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, ai_confidence, ai_evidence, "
        "ai_rationale, routing, status, final_score, reviewer, reviewed_at, model_version, prompt_version, "
        "rubric_version, asr_confidence, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rec_trace",
            "att_trace",
            "OP.01",
            2,
            0.92,
            json.dumps(evidence, ensure_ascii=False),
            "考生主动说明身份并核对姓名",
            json.dumps({"route": "auto", "reasons": []}),
            "reviewed",
            2,
            "张老师",
            "2026-01-01T10:05:00+08:00",
            "mock-score-1.0",
            "score-prompt-1.0",
            bundle["bundle_hash"],
            0.97,
            "2026-01-01T09:12:00+08:00",
        ),
    )
    conn.execute(
        "INSERT INTO review_actions(action_id, record_id, reviewer, action, from_score, to_score, "
        "reason_code, seconds_spent, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("rv_trace", "rec_trace", "张老师", "accept", 2, 2, None, 42.0, "2026-01-01T10:05:00+08:00"),
    )
    db.append_audit(
        conn,
        actor="张老师",
        action="record.accept",
        object_type="ScoreRecord",
        object_id="rec_trace",
        before={"ai_score": 2, "status": "pending"},
        after={"final_score": 2},
    )
    conn.commit()
    return conn, bundle


def test_trace_links_score_evidence_review_and_audit(tmp_path):
    conn, bundle = _setup(tmp_path)
    result = score_trace(conn, "att_trace", item_id="OP.01")
    assert result is not None
    assert result["bundle"]["station_id"] == "ST-TRACE-001"
    assert result["bundle"]["bundle_hash"] == bundle["bundle_hash"]
    assert result["transcript"]["state"] == "ok"
    entry = result["records"][0]
    assert entry["record"]["final_score"] == 2
    assert entry["evidence"][0]["verified"] is True
    assert entry["evidence"][0]["transcript_text"] == CANDIDATE_TEXT
    assert entry["evidence"][0]["media_url"] == "/media/demo.wav"
    assert len(entry["review_actions"]) == 1
    assert entry["review_actions"][0]["reviewer"] == "张老师"
    actions = {event["action"] for event in result["audit_events"]}
    assert "record.accept" in actions
    integrity = result["integrity"]
    assert integrity["version_stamped"] is True
    assert integrity["quotes_checked"] == 1
    assert integrity["quotes_matched"] == 1
    assert integrity["issues"] == []
    conn.close()


def test_trace_detects_transcript_and_quote_mismatch(tmp_path):
    conn, _ = _setup(tmp_path)
    row = conn.execute("SELECT payload FROM transcripts WHERE attempt_id = ?", ("att_trace",)).fetchone()
    utterances = json.loads(row["payload"])
    utterances[0]["text"] = "被改写的一句话"
    conn.execute(
        "UPDATE transcripts SET payload = ? WHERE attempt_id = ?",
        (json.dumps(utterances, ensure_ascii=False), "att_trace"),
    )
    conn.commit()
    result = score_trace(conn, "att_trace")
    assert result["transcript"]["state"] == "mismatch"
    assert any("转写哈希" in issue for issue in result["integrity"]["issues"])
    assert result["records"][0]["evidence"][0]["verified"] is False
    conn.close()


def test_trace_returns_none_for_unknown_attempt(tmp_path):
    conn, _ = _setup(tmp_path)
    assert score_trace(conn, "att_missing") is None
    conn.close()
