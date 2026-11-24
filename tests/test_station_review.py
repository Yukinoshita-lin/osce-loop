"""异常站处置：质量异常到修订动作的记录与追踪。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.auth import rbac_allowed
from app.domain.bundle import freeze_bundle
from app.domain.station_review import list_station_reviews, record_station_review
from app.seed import build_bundle_payload


def _conn(tmp_path):
    conn = db.connect(tmp_path / "station_review.db")
    db.init_db(conn)
    return conn


def _bundle(conn, station_id="ST-REVIEW-001"):
    payload = build_bundle_payload()
    payload["station_id"] = station_id
    return freeze_bundle(conn, payload, actor="测试", version="1.0")


def test_record_station_review_and_list(tmp_path):
    conn = _conn(tmp_path)
    bundle = _bundle(conn)
    review = record_station_review(
        conn,
        station_id="ST-REVIEW-001",
        flag="low_discrimination",
        decision="rubric_revision",
        note="HX.03 锚过于笼统，下一年度改写",
        actor="命题专家",
    )
    assert review["review_id"].startswith("srv_")
    assert review["bundle_id"] == bundle["bundle_id"]
    reviews = list_station_reviews(conn, "ST-REVIEW-001")
    assert len(reviews) == 1
    assert reviews[0]["decision"] == "rubric_revision"
    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'quality.station_review_recorded'"
    ).fetchone()
    assert event is not None
    conn.close()


def test_station_review_rejects_invalid_inputs(tmp_path):
    conn = _conn(tmp_path)
    _bundle(conn)
    for kwargs in (
        {"flag": "unknown", "decision": "teaching", "note": "x"},
        {"flag": "ceiling", "decision": "unknown", "note": "x"},
        {"flag": "ceiling", "decision": "teaching", "note": "  "},
    ):
        with pytest.raises(ValueError):
            record_station_review(conn, station_id="ST-REVIEW-001", **kwargs)
    with pytest.raises(ValueError):
        record_station_review(
            conn,
            station_id="ST-MISSING",
            flag="ceiling",
            decision="teaching",
            note="x",
        )
    conn.close()


def test_station_review_bundle_must_match_station(tmp_path):
    conn = _conn(tmp_path)
    first = _bundle(conn)
    second = _bundle(conn, station_id="ST-REVIEW-002")
    with pytest.raises(ValueError):
        record_station_review(
            conn,
            station_id="ST-REVIEW-002",
            bundle_id=first["bundle_id"],
            flag="ceiling",
            decision="monitor",
            note="错配",
        )
    assert second["station_id"] == "ST-REVIEW-002"
    conn.close()


def test_station_review_rbac():
    assert rbac_allowed("POST", "/api/quality/station-review", "exam_manager") is True
    assert rbac_allowed("POST", "/api/quality/station-review", "examiner") is False
    assert rbac_allowed("POST", "/api/quality/station-review", "auditor") is False
    assert rbac_allowed("GET", "/api/quality/station-reviews", "auditor") is True
