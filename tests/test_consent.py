"""SP 知情同意：记录、有效期与闸门联动。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.consent import consent_status, list_consents, record_consent
from app.domain.gates import evaluate_gates
from app.domain.stats import quality_report
from app.seed import build_bundle_payload


def _conn(tmp_path):
    conn = db.connect(tmp_path / "consent.db")
    db.init_db(conn)
    return conn


def _bundle(conn):
    payload = build_bundle_payload()
    payload["station_id"] = "ST-CONSENT-001"
    return freeze_bundle(conn, payload, actor="测试", version="1.0")


def test_missing_consent_is_invalid(tmp_path):
    conn = _conn(tmp_path)
    bundle = _bundle(conn)
    status = consent_status(conn, bundle["bundle_id"])
    assert status["valid"] is False
    assert status["missing"] == ["informed_use"]
    assert status["consents"] == []
    conn.close()


def test_record_adult_consent_makes_status_valid(tmp_path):
    conn = _conn(tmp_path)
    bundle = _bundle(conn)
    consent = record_consent(
        conn,
        sp_name="王建国",
        sp_role="adult_sp",
        consent_type="informed_use",
        version="v2026.1",
        bundle_id=bundle["bundle_id"],
        actor="考务",
    )
    assert consent["consent_id"].startswith("csp_")
    status = consent_status(conn, bundle["bundle_id"])
    assert status["valid"] is True
    assert status["missing"] == []
    assert status["expired"] == []
    assert list_consents(conn, bundle["bundle_id"])[0]["sp_name"] == "王建国"
    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'sp.consent_recorded'"
    ).fetchone()
    assert event is not None
    conn.close()


def test_minor_sp_requires_guardian_consent(tmp_path):
    conn = _conn(tmp_path)
    bundle = _bundle(conn)
    record_consent(
        conn,
        sp_name="小李",
        sp_role="minor_sp",
        consent_type="informed_use",
        version="v2026.1",
        bundle_id=bundle["bundle_id"],
    )
    status = consent_status(conn, bundle["bundle_id"])
    assert status["valid"] is False
    assert status["missing"] == ["minor_guardian"]

    record_consent(
        conn,
        sp_name="李女士",
        sp_role="guardian",
        consent_type="minor_guardian",
        version="v2026.1",
        bundle_id=bundle["bundle_id"],
    )
    assert consent_status(conn, bundle["bundle_id"])["valid"] is True
    conn.close()


def test_expired_consent_is_flagged(tmp_path):
    conn = _conn(tmp_path)
    bundle = _bundle(conn)
    conn.execute(
        "INSERT INTO sp_consents(consent_id, sp_name, sp_role, bundle_id, consent_type, version, "
        "signed_at, notes, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "csp_old",
            "旧 SP",
            "adult_sp",
            bundle["bundle_id"],
            "informed_use",
            "v2024.1",
            "2024-01-01T00:00:00+08:00",
            None,
            "测试",
            "2024-01-01T00:00:00+08:00",
        ),
    )
    conn.commit()
    status = consent_status(conn, bundle["bundle_id"])
    assert status["valid"] is False
    assert status["expired"] == ["informed_use"]
    assert status["consents"][0]["valid"] is False
    conn.close()


def test_invalid_consent_inputs_are_rejected(tmp_path):
    conn = _conn(tmp_path)
    bundle = _bundle(conn)
    with pytest.raises(ValueError):
        record_consent(conn, sp_name="", sp_role="adult_sp", consent_type="informed_use", version="v1")
    with pytest.raises(ValueError):
        record_consent(conn, sp_name="SP", sp_role="robot", consent_type="informed_use", version="v1")
    with pytest.raises(ValueError):
        record_consent(conn, sp_name="SP", sp_role="adult_sp", consent_type="unknown", version="v1")
    with pytest.raises(ValueError):
        record_consent(
            conn, sp_name="SP", sp_role="adult_sp", consent_type="informed_use", version="v1",
            bundle_id="bnd_missing",
        )
    assert bundle["bundle_id"]
    conn.close()


def test_gate_requires_valid_consent(tmp_path):
    conn = _conn(tmp_path)
    bundle = _bundle(conn)
    gates = evaluate_gates(conn, quality_report(conn), "G1_practice")
    g1 = next(gate for gate in gates["gates"] if gate["id"] == "G1")
    row = next(item for item in g1["criteria"] if item["key"] == "sp_consent")
    assert row["value"] == 0.0
    assert row["status"] == "fail"
    assert any("知情同意" in alert["title"] for alert in gates["alerts"])

    record_consent(
        conn,
        sp_name="王建国",
        sp_role="adult_sp",
        consent_type="informed_use",
        version="v2026.1",
        bundle_id=bundle["bundle_id"],
    )
    gates = evaluate_gates(conn, quality_report(conn), "G1_practice")
    g1 = next(gate for gate in gates["gates"] if gate["id"] == "G1")
    row = next(item for item in g1["criteria"] if item["key"] == "sp_consent")
    assert row["value"] == 1.0
    assert row["status"] == "pass"
    assert not any("知情同意" in alert["title"] for alert in gates["alerts"])
    conn.close()
