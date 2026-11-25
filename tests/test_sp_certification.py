"""SP 认证与 persona_version 绑定：改版后不得复用旧认证。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.domain.gates as gates_module
import app.domain.spqa as spqa_module
from app import db
from app.domain.bundle import freeze_bundle
from app.domain.gates import evaluate_gates
from app.domain.sp_approval import persona_approval_status, record_persona_approval
from app.domain.spqa import (
    RED_TEAM_VERSION,
    certification_for_bundle,
    certify_bundle,
    ensure_certification,
    sp_session_ready,
)
from app.domain.stats import quality_report
from app.seed import build_bundle_payload


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "sp-cert.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _freeze(conn, version: str = "1.0", title_suffix: str = "", supersedes: str | None = None):
    payload = build_bundle_payload()
    payload["title"] = str(payload["title"]) + title_suffix
    return freeze_bundle(
        conn,
        payload,
        actor="test",
        version=version,
        supersedes=supersedes,
        reason="测试冻结",
    )


def test_certification_is_bound_to_current_persona(conn):
    bundle = _freeze(conn)
    report = certify_bundle(conn, bundle, actor="test")
    assert report["valid"] is True
    assert report["stale"] is False
    cert = certification_for_bundle(conn, bundle)
    assert cert["valid"] is True
    assert cert["stale"] is False
    assert cert["persona_version"] == bundle["bundle_hash"]


def test_certification_is_bound_to_red_team_case_version(conn, monkeypatch):
    bundle = _freeze(conn)
    report = certify_bundle(conn, bundle, actor="test")
    assert report["red_team_version"] == RED_TEAM_VERSION
    cert = certification_for_bundle(conn, bundle)
    assert cert["case_stale"] is False
    assert cert["valid"] is True

    record_persona_approval(
        conn,
        bundle,
        decision="approved",
        scope="L1",
        disclosure_approved=True,
        forbidden_approved=True,
        trial_approved=True,
        actor="考官",
    )
    assert sp_session_ready(conn, bundle)["ready"] is True

    monkeypatch.setattr(spqa_module, "RED_TEAM_VERSION", "2099Q1")
    monkeypatch.setattr(gates_module, "RED_TEAM_VERSION", "2099Q1")
    stale = certification_for_bundle(conn, bundle)
    assert stale["case_stale"] is True
    assert stale["valid"] is False
    assert sp_session_ready(conn, bundle)["ready"] is False

    result = evaluate_gates(conn, quality_report(conn), "G1_practice")
    assert any("红队用例版本" in alert["title"] for alert in result["alerts"])


def test_certification_has_quarterly_expiry(conn):
    bundle = _freeze(conn)
    report = certify_bundle(conn, bundle, actor="test")
    assert report["valid_until"]
    cert = certification_for_bundle(conn, bundle)
    assert cert["expired"] is False
    assert cert["days_remaining"] >= 89


def _expire(conn, cert_id: str) -> None:
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE sp_certifications SET valid_until = ? WHERE cert_id = ?", (past, cert_id))
    conn.commit()


def test_sp_session_requires_valid_certification_and_persona_approval(conn):
    bundle = _freeze(conn)
    assert sp_session_ready(conn, bundle)["ready"] is False
    report = certify_bundle(conn, bundle, actor="test")
    assert sp_session_ready(conn, bundle)["ready"] is False
    with pytest.raises(ValueError, match="披露阈值"):
        record_persona_approval(
            conn,
            bundle,
            decision="approved",
            scope="L1",
            disclosure_approved=True,
            forbidden_approved=False,
            trial_approved=True,
        )
    record_persona_approval(
        conn,
        bundle,
        decision="approved",
        scope="L1",
        disclosure_approved=True,
        forbidden_approved=True,
        trial_approved=True,
        actor="考官",
    )
    assert sp_session_ready(conn, bundle)["ready"] is True
    _expire(conn, report["cert_id"])
    assert sp_session_ready(conn, bundle)["ready"] is False


def test_persona_approval_is_bound_to_persona_version(conn):
    bundle = _freeze(conn)
    approval = record_persona_approval(
        conn,
        bundle,
        decision="approved",
        scope="L2",
        disclosure_approved=True,
        forbidden_approved=True,
        trial_approved=True,
    )
    status = persona_approval_status(conn, bundle)
    assert status["approved"] is True
    assert status["scope"] == "L2"
    assert approval["persona_version"] == bundle["bundle_hash"]

    new_bundle = _freeze(
        conn, version="1.1", title_suffix="（改版）", supersedes=bundle["bundle_id"]
    )
    stale = persona_approval_status(conn, new_bundle)
    assert stale["approved"] is False
    assert stale["stale"] is True


def test_persona_approval_requires_complete_persona_card(conn):
    payload = build_bundle_payload()
    payload["sp_profile"]["persona"].pop("disclosure")
    payload["sp_profile"]["forbidden"] = []
    bundle = freeze_bundle(conn, payload, actor="test", version="1.0", reason="测试不完整人格卡")
    with pytest.raises(ValueError, match="人格卡缺少必要维度"):
        record_persona_approval(
            conn,
            bundle,
            decision="approved",
            scope="L1",
            disclosure_approved=True,
            forbidden_approved=True,
            trial_approved=True,
        )
    rejected = record_persona_approval(
        conn,
        bundle,
        decision="rejected",
        scope="L1",
        disclosure_approved=False,
        forbidden_approved=False,
        trial_approved=False,
        note="人格卡缺披露阈值，退回补充",
    )
    assert rejected["decision"] == "rejected"


def test_expired_certification_does_not_feed_g1_metrics(conn):
    bundle = _freeze(conn)
    report = certify_bundle(conn, bundle, actor="test")
    _expire(conn, report["cert_id"])

    cert = certification_for_bundle(conn, bundle)
    assert cert["expired"] is True
    assert cert["valid"] is False

    result = evaluate_gates(conn, quality_report(conn), "G1_practice")
    g1 = next(gate for gate in result["gates"] if gate["id"] == "G1")
    sp_keys = {"sp_fact_error", "persona_pass", "p95_latency", "p0_incidents"}
    assert all(row["value"] is None for row in g1["criteria"] if row["key"] in sp_keys)
    assert any("过期" in alert["title"] for alert in result["alerts"])


def test_ensure_certification_does_not_recertify_expired(conn):
    bundle = _freeze(conn)
    report = certify_bundle(conn, bundle, actor="test")
    _expire(conn, report["cert_id"])
    before = conn.execute("SELECT COUNT(*) AS n FROM sp_certifications").fetchone()["n"]

    cert = ensure_certification(conn)
    after = conn.execute("SELECT COUNT(*) AS n FROM sp_certifications").fetchone()["n"]
    assert after == before
    assert cert["expired"] is True
    assert cert["valid"] is False


def test_bundle_change_requires_new_certification(conn):
    old_bundle = _freeze(conn)
    certify_bundle(conn, old_bundle, actor="test")
    new_bundle = _freeze(conn, version="1.1", title_suffix="（改版）", supersedes=old_bundle["bundle_id"])
    assert new_bundle["bundle_hash"] != old_bundle["bundle_hash"]

    assert certification_for_bundle(conn, new_bundle) is None
    old_cert = certification_for_bundle(conn, old_bundle)
    assert old_cert["valid"] is True
    assert old_cert["persona_version"] == old_bundle["bundle_hash"]


def test_uncertified_new_bundle_does_not_feed_g1_metrics(conn):
    old_bundle = _freeze(conn)
    certify_bundle(conn, old_bundle, actor="test")
    _freeze(conn, version="1.1", title_suffix="（改版）", supersedes=old_bundle["bundle_id"])

    result = evaluate_gates(conn, quality_report(conn), "G1_practice")
    g1 = next(gate for gate in result["gates"] if gate["id"] == "G1")
    sp_keys = {"sp_fact_error", "persona_pass", "p95_latency", "p0_incidents"}
    sp_rows = [row for row in g1["criteria"] if row["key"] in sp_keys]
    assert sp_rows
    assert all(row["value"] is None for row in sp_rows)
    assert g1["status"] != "pass"
    assert any("SP 认证" in alert["title"] for alert in result["alerts"])


def test_ensure_certification_does_not_silently_recertify(conn):
    old_bundle = _freeze(conn)
    certify_bundle(conn, old_bundle, actor="test")
    _freeze(conn, version="1.1", title_suffix="（改版）", supersedes=old_bundle["bundle_id"])
    before = conn.execute("SELECT COUNT(*) AS n FROM sp_certifications").fetchone()["n"]

    cert = ensure_certification(conn)
    after = conn.execute("SELECT COUNT(*) AS n FROM sp_certifications").fetchone()["n"]
    assert after == before
    assert cert is None
