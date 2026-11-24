"""签字链抽查：人工签字、发布时间与关键项确认。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.signoff_audit import audit_signoff_chain
from app.seed import build_bundle_payload


def _setup(tmp_path):
    conn = db.connect(tmp_path / "signoff.db")
    db.init_db(conn)
    payload = build_bundle_payload()
    payload["station_id"] = "ST-SIGN-001"
    bundle = freeze_bundle(conn, payload, actor="测试", version="1.0")
    critical = [item["item_id"] for item in payload["rubric"] if item.get("critical")]
    return conn, bundle, critical


def _attempt(conn, attempt_id, bundle_id, *, signed_by, signed_at, published_at, status="published", gap=0):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, integrity_gap, signed_off_by, signed_off_at, published_at, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id, bundle_id, "C001", "考生", "SES", "L2", status, gap,
            signed_by, signed_at, published_at, "2026-01-01T08:00:00+08:00",
        ),
    )


def _record(conn, record_id, attempt_id, item_id, status="reviewed", score=2):
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, routing, status, "
        "final_score, model_version, prompt_version, rubric_version, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            record_id, attempt_id, item_id, score, "{}", status, score,
            "mock-score-1.0", "score-prompt-1.0", "sha256:x", "2026-01-01T09:00:00+08:00",
        ),
    )


def test_signoff_audit_flags_missing_signoff_and_pending_critical(tmp_path):
    conn, bundle, critical = _setup(tmp_path)
    _attempt(
        conn, "att_good", bundle["bundle_id"],
        signed_by="张老师", signed_at="2026-01-01T10:00:00+08:00", published_at="2026-01-01T11:00:00+08:00",
    )
    for index, item_id in enumerate(critical):
        _record(conn, f"good_{index}", "att_good", item_id)
    _attempt(
        conn, "att_bad", bundle["bundle_id"],
        signed_by=None, signed_at=None, published_at="2026-01-01T11:00:00+08:00",
    )
    _record(conn, "bad_1", "att_bad", critical[0], status="pending_review", score=None)
    conn.commit()

    result = audit_signoff_chain(conn, sample_size=20)
    assert result["checked"] == 2
    assert result["passed"] == 1
    assert result["failed"] == 1
    bad = next(item for item in result["issues"] if item["attempt_id"] == "att_bad")
    joined = "；".join(bad["issues"])
    assert "缺少人工签字" in joined
    assert "尚未完成人工确认" in joined
    assert result["pass_rate"] == 0.5
    conn.close()


def test_signoff_audit_flags_publish_before_signoff(tmp_path):
    conn, bundle, critical = _setup(tmp_path)
    _attempt(
        conn, "att_order", bundle["bundle_id"],
        signed_by="张老师", signed_at="2026-01-01T12:00:00+08:00", published_at="2026-01-01T11:00:00+08:00",
    )
    for index, item_id in enumerate(critical):
        _record(conn, f"order_{index}", "att_order", item_id)
    conn.commit()

    result = audit_signoff_chain(conn)
    assert result["failed"] == 1
    assert "发布时间早于人工签字时间" in result["issues"][0]["issues"]
    conn.close()


def test_signoff_audit_empty_database(tmp_path):
    conn = db.connect(tmp_path / "empty.db")
    db.init_db(conn)
    result = audit_signoff_chain(conn)
    assert result["checked"] == 0
    assert result["passed"] == 0
    assert result["pass_rate"] is None
    conn.close()
