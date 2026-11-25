"""S9 阈值本地校准档案：阈值快照、证据、有效期与审计。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.domain import threshold_calibration


def _conn(tmp_path, name="threshold.db"):
    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    return conn


def test_snapshot_covers_all_gate_criteria_and_s9(tmp_path):
    snapshot = threshold_calibration.current_threshold_snapshot()
    keys = {(item["gate"], item["key"]) for item in snapshot}
    assert len(keys) == len(snapshot), "阈值快照不应包含重复指标"
    for gate in ("G0", "G1", "G2", "G3"):
        assert any(item["gate"] == gate for item in snapshot)
    for key in (
        "asr_error_rate",
        "sus",
        "cost_practice",
        "cost_exam",
        "feedback_practice_seconds",
        "feedback_exam_hours",
    ):
        assert ("S9", key) in keys
    item = next(i for i in snapshot if i["gate"] == "G3" and i["key"] == "qwk_ai")
    assert item["target"] == 0.80
    assert item["direction"] == "ge"
    assert item["target_text"] == "≥ 0.80"


def test_record_and_read_latest_calibration(tmp_path):
    conn = _conn(tmp_path)
    calibration = threshold_calibration.record_threshold_calibration(
        conn,
        actor="教研主任",
        note="基于 2026 春 120 条本地金标准校准，QWK 0.78，保留文献起始值",
    )
    assert calibration["calibration_id"].startswith("tcal_")
    assert calibration["evidence"]["gold_count"] == 0
    assert calibration["thresholds"]

    latest = threshold_calibration.latest_threshold_calibration(conn)
    assert latest is not None
    assert latest["valid"] is True
    assert latest["expired"] is False
    assert latest["days_remaining"] >= 360
    assert latest["note"] == "基于 2026 春 120 条本地金标准校准，QWK 0.78，保留文献起始值"
    assert conn.execute("SELECT COUNT(*) AS n FROM threshold_calibrations").fetchone()["n"] == 1
    audit = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'thresholds.calibrated'"
    ).fetchone()
    assert audit is not None
    assert audit["actor"] == "教研主任"
    conn.close()


def test_blank_note_is_rejected(tmp_path):
    conn = _conn(tmp_path)
    try:
        threshold_calibration.record_threshold_calibration(conn, actor="test", note="   ")
    except ValueError as exc:
        assert "校准说明" in str(exc)
    else:  # pragma: no cover - 明确失败信息
        raise AssertionError("空说明应被拒绝")
    assert conn.execute("SELECT COUNT(*) AS n FROM threshold_calibrations").fetchone()["n"] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'thresholds.calibrated'"
        ).fetchone()["n"]
        == 0
    )
    conn.close()


def test_expired_calibration_is_flagged(tmp_path):
    conn = _conn(tmp_path)
    past = (datetime.now(timezone.utc).astimezone() - timedelta(days=5)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO threshold_calibrations(calibration_id, actor, thresholds, evidence, note, "
        "valid_until, created_at) VALUES(?,?,?,?,?,?,?)",
        ("tcal_old", "test", "[]", "{}", "旧校准", past, past),
    )
    conn.commit()
    latest = threshold_calibration.latest_threshold_calibration(conn)
    assert latest["expired"] is True
    assert latest["valid"] is False
    assert latest["days_remaining"] == 0
    conn.close()


def test_no_calibration_returns_none(tmp_path):
    conn = _conn(tmp_path)
    assert threshold_calibration.latest_threshold_calibration(conn) is None
    conn.close()
