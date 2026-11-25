"""内容审计抽样：SP 越界话术与高风险表述扫描。"""
from __future__ import annotations

import json

import pytest

from app import db
from app.domain.content_audit import (
    audit_utterances,
    build_leak_terms,
    build_regression_cases,
    content_audit_summary,
    regression_signature,
    review_finding,
    run_content_audit,
    scan_attempt,
)


def _attempt(conn, attempt_id, utterances):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, created_at) VALUES(?,?,?,?,?,?,?)",
        (attempt_id, "bnd", "C001", "考生", "SES", "L2", "2026-01-01T09:00:00+08:00"),
    )
    conn.execute(
        "INSERT INTO transcripts(attempt_id, payload, sha256, created_at) VALUES(?,?,?,?)",
        (attempt_id, json.dumps(utterances, ensure_ascii=False), "sha256:x", "2026-01-01T09:00:00+08:00"),
    )


def _utt(utt_id, speaker, text, start=0):
    return {
        "utt_id": utt_id,
        "speaker": speaker,
        "start_ms": start,
        "end_ms": start + 2000,
        "text": text,
        "asr_confidence": 0.95,
        "phase": "information_gathering",
    }


def test_audit_utterances_flags_sp_diagnosis_and_prescription():
    findings = audit_utterances(
        [
            _utt("u1", "patient", "你得了冠心病，吃点阿司匹林吧。"),
            _utt("u2", "candidate", "肯定没事，不用检查。", 2000),
            _utt("u3", "patient", "我从昨天开始胸口疼。", 4000),
        ]
    )
    rules = {finding["rule"] for finding in findings}
    assert "sp_diagnosis" in rules
    assert "sp_prescription" in rules
    assert "candidate_false_reassurance" in rules
    assert all(finding["quote"] for finding in findings)
    assert all(finding["severity"] in ("high", "medium") for finding in findings)


def test_run_content_audit_scans_and_audits(tmp_path):
    conn = db.connect(tmp_path / "audit.db")
    db.init_db(conn)
    _attempt(conn, "att_bad", [_utt("u1", "patient", "你得了冠心病。")])
    _attempt(conn, "att_clean", [_utt("u1", "patient", "我胸口疼了两小时。")])
    conn.commit()

    result = run_content_audit(conn, sample_rate=1.0, limit=10, actor="审计员")
    assert result["attempts_considered"] == 2
    assert result["attempts_scanned"] == 2
    assert result["finding_count"] == 1
    assert result["attempts_with_findings"] == 1
    assert result["findings"][0]["attempt_id"] == "att_bad"
    assert result["by_severity"]["high"] == 1
    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'content.audit_run'"
    ).fetchone()
    assert event is not None
    conn.close()


def test_content_findings_can_be_reviewed_and_summary_updates(tmp_path):
    conn = db.connect(tmp_path / "audit_review.db")
    db.init_db(conn)
    _attempt(conn, "att_bad", [_utt("u1", "patient", "你得了冠心病，吃点阿司匹林。")])
    conn.commit()
    scan_attempt(conn, "att_bad", actor="test")

    summary = content_audit_summary(conn)
    assert summary["open_count"] == 2
    assert summary["high_count"] == 2
    finding = summary["recent"][0]
    reviewed = review_finding(
        conn,
        finding["finding_id"],
        decision="confirmed",
        note="确认越界，回写红队用例",
        actor="auditor",
    )
    assert reviewed["status"] == "confirmed"
    assert reviewed["reviewed_by"] == "auditor"

    updated = content_audit_summary(conn)
    assert updated["open_count"] == 1
    assert updated["high_count"] == 1
    assert updated["confirmed_count"] == 1
    assert updated["confirmed_high_count"] == 1
    with pytest.raises(ValueError, match="已处置"):
        review_finding(
            conn,
            finding["finding_id"],
            decision="dismissed",
            note="重复处置",
            actor="auditor",
        )
    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'content.finding_reviewed'"
    ).fetchone()
    assert event is not None
    cases = build_regression_cases(conn)
    assert cases
    assert cases[0]["source_finding_id"] == finding["finding_id"]
    assert cases[0]["turns"]
    terms = build_leak_terms(conn)
    assert terms
    assert terms[0] in finding["match"]
    assert regression_signature(conn)
    conn.close()


def test_run_content_audit_supports_single_attempt_and_zero_rate(tmp_path):
    conn = db.connect(tmp_path / "audit2.db")
    db.init_db(conn)
    _attempt(conn, "att_one", [_utt("u1", "patient", "吃点阿司匹林。")])
    conn.commit()

    single = run_content_audit(conn, sample_rate=0.0, attempt_id="att_one")
    assert single["attempts_scanned"] == 1
    assert single["finding_count"] == 1

    none = run_content_audit(conn, sample_rate=0.0, limit=10)
    assert none["attempts_scanned"] == 0
    assert none["finding_count"] == 0
    conn.close()
