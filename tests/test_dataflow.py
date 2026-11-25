"""数据流向：模型与 ASR 端点的内网/公网分类、合规判定与年度复核。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.domain import dataflow


def test_host_kind_classifies_loopback_private_and_public():
    assert dataflow.host_kind("127.0.0.1") == "loopback"
    assert dataflow.host_kind("::1") == "loopback"
    assert dataflow.host_kind("10.2.3.4") == "private"
    assert dataflow.host_kind("192.168.10.8") == "private"
    assert dataflow.host_kind("ai.hospital.local") == "private"
    assert dataflow.host_kind("api.openai.com") == "public"
    assert dataflow.host_kind(None) == "unconfigured"


def test_external_scoring_endpoint_is_flagged(monkeypatch):
    monkeypatch.setattr(dataflow, "AI_PROVIDER", "http")
    monkeypatch.setattr(dataflow, "AI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(dataflow, "AI_ALLOW_EXTERNAL", False)
    monkeypatch.setattr(dataflow, "ASR_PROVIDER", "off")
    monkeypatch.setattr(dataflow, "ASR_BASE_URL", "")
    status = dataflow.data_flow_status()
    assert status["compliant"] is False
    assert status["external"][0]["label"] == "评分模型"
    assert status["external"][0]["host"] == "api.example.com"


def test_external_endpoint_requires_explicit_allow(monkeypatch):
    monkeypatch.setattr(dataflow, "AI_PROVIDER", "http")
    monkeypatch.setattr(dataflow, "AI_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setattr(dataflow, "AI_ALLOW_EXTERNAL", True)
    monkeypatch.setattr(dataflow, "ASR_PROVIDER", "off")
    monkeypatch.setattr(dataflow, "ASR_BASE_URL", "")
    status = dataflow.data_flow_status()
    assert status["compliant"] is True
    assert status["external"][0]["allowed"] is True


def test_private_and_trusted_hosts_are_compliant(monkeypatch):
    monkeypatch.setattr(dataflow, "AI_PROVIDER", "http")
    monkeypatch.setattr(dataflow, "AI_BASE_URL", "http://192.168.10.5:8000/v1")
    monkeypatch.setattr(dataflow, "AI_ALLOW_EXTERNAL", False)
    monkeypatch.setattr(dataflow, "ASR_PROVIDER", "http")
    monkeypatch.setattr(dataflow, "ASR_BASE_URL", "https://asr.hospital/v1")
    monkeypatch.setattr(dataflow, "ASR_ALLOW_EXTERNAL", False)
    monkeypatch.setattr(dataflow, "TRUSTED_AI_HOSTS", {"asr.hospital"})
    status = dataflow.data_flow_status()
    assert status["compliant"] is True
    scopes = {flow["kind"]: flow["scope"] for flow in status["flows"]}
    assert scopes == {"scoring": "private", "asr": "trusted"}


def test_mock_providers_have_no_flows(monkeypatch):
    monkeypatch.setattr(dataflow, "AI_PROVIDER", "mock")
    monkeypatch.setattr(dataflow, "AI_BASE_URL", "")
    monkeypatch.setattr(dataflow, "ASR_PROVIDER", "off")
    monkeypatch.setattr(dataflow, "ASR_BASE_URL", "")
    status = dataflow.data_flow_status()
    assert status["flows"] == []
    assert status["compliant"] is True


def test_data_flow_review_records_snapshot_and_validity(tmp_path):
    conn = db.connect(tmp_path / "dataflow_review.db")
    db.init_db(conn)
    review = dataflow.record_data_flow_review(conn, actor="test", note="年度复核")
    assert review["endpoints"] == []
    latest = dataflow.latest_data_flow_review(conn)
    assert latest["valid"] is True
    assert latest["expired"] is False
    assert latest["days_remaining"] >= 360
    assert conn.execute("SELECT COUNT(*) AS n FROM data_flow_reviews").fetchone()["n"] == 1
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM audit_events WHERE action = 'dataflow.review'").fetchone()["n"]
        == 1
    )
    conn.close()


def test_expired_data_flow_review_is_flagged(tmp_path):
    conn = db.connect(tmp_path / "dataflow_expired.db")
    db.init_db(conn)
    past = (datetime.now(timezone.utc).astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO data_flow_reviews(review_id, actor, endpoints, note, valid_until, created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("dfr_old", "test", "[]", "旧复核", past, past),
    )
    conn.commit()
    latest = dataflow.latest_data_flow_review(conn)
    assert latest["expired"] is True
    assert latest["valid"] is False
    assert latest["days_remaining"] == 0
    conn.close()
