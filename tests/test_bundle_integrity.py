"""冻结核验：哈希、派生资产、评分版本戳与冻结审计锚点。"""
from __future__ import annotations

import json

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.bundle_integrity import verify_bundle_integrity
from app.seed import build_bundle_payload


def _setup(tmp_path):
    conn = db.connect(tmp_path / "integrity.db")
    db.init_db(conn)
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="测试", version="1.0")
    return conn, bundle


def test_frozen_bundle_passes_integrity_check(tmp_path):
    conn, bundle = _setup(tmp_path)
    result = verify_bundle_integrity(conn, bundle["bundle_id"], actor="审计员")
    assert result["status"] == "pass"
    assert {check["key"] for check in result["checks"]} == {"hash", "derived", "audit", "records"}
    assert all(check["passed"] for check in result["checks"])

    event = conn.execute(
        "SELECT action, actor FROM audit_events WHERE action = 'bundle.integrity_verify'"
    ).fetchone()
    assert event["actor"] == "审计员"


def test_tampered_rubric_fails_hash_and_derived_checks(tmp_path):
    conn, bundle = _setup(tmp_path)
    payload = json.loads(
        conn.execute("SELECT payload FROM bundles WHERE bundle_id = ?", (bundle["bundle_id"],)).fetchone()["payload"]
    )
    item = next(entry for entry in payload["rubric"] if (entry.get("slot") or {}).get("content"))
    item["slot"]["content"] = str(item["slot"]["content"]) + "（篡改）"
    conn.execute(
        "UPDATE bundles SET payload = ? WHERE bundle_id = ?",
        (json.dumps(payload, ensure_ascii=False), bundle["bundle_id"]),
    )
    conn.commit()

    result = verify_bundle_integrity(conn, bundle["bundle_id"])
    assert result["status"] == "fail"
    checks = {check["key"]: check for check in result["checks"]}
    assert checks["hash"]["passed"] is False
    assert checks["derived"]["passed"] is False
    assert checks["hash"]["actual"] != checks["hash"]["expected"]


def test_score_record_with_foreign_rubric_version_fails(tmp_path):
    conn, bundle = _setup(tmp_path)
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("att_1", bundle["bundle_id"], "C001", "考生", "SES", "L2", "scored", "2026-12-10T09:00:00+08:00"),
    )
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, routing, status, rubric_version, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("rec_1", "att_1", "HX.01", "{}", "reviewed", "sha256:foreign", "2026-12-10T09:10:00+08:00"),
    )
    conn.commit()

    result = verify_bundle_integrity(conn, bundle["bundle_id"])
    assert result["status"] == "fail"
    records = next(check for check in result["checks"] if check["key"] == "records")
    assert records["passed"] is False
    assert records["detail"]["mismatched"] == [{"rubric_version": "sha256:foreign", "records": 1}]


def test_missing_freeze_audit_is_reported_as_warning(tmp_path):
    conn, bundle = _setup(tmp_path)
    conn.execute(
        "DELETE FROM audit_events WHERE action = 'bundle.freeze' AND object_id = ?",
        (bundle["bundle_id"],),
    )
    conn.commit()

    result = verify_bundle_integrity(conn, bundle["bundle_id"])
    assert result["status"] == "warn"
    audit = next(check for check in result["checks"] if check["key"] == "audit")
    assert audit["missing"] is True
    assert audit["passed"] is False


def test_unknown_bundle_raises(tmp_path):
    conn, _ = _setup(tmp_path)
    try:
        verify_bundle_integrity(conn, "bnd_missing")
    except ValueError as exc:
        assert "不存在" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应抛出 ValueError")
