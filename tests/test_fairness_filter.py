"""分站公平性：按 bundle 过滤与分站质量表接入。"""
from __future__ import annotations

import copy

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.fairness import fairness_report
from app.domain.station_quality import station_quality_report
from app.seed import build_bundle_payload, seed_if_empty


def _conn(tmp_path):
    conn = db.connect(tmp_path / "fairness_filter.db")
    db.init_db(conn)
    seed_if_empty(conn)
    return conn


def test_fairness_report_filters_by_bundle(tmp_path):
    conn = _conn(tmp_path)
    try:
        original = build_bundle_payload()
        original_bundle = freeze_bundle(conn, original, actor="test", version="1.0")
        other = copy.deepcopy(original)
        other["station_id"] = "ST-OTHER-001"
        other["title"] = "其他考站"
        other_bundle = freeze_bundle(conn, other, actor="test", version="1.0")

        global_report = fairness_report(conn)
        scoped = fairness_report(conn, original_bundle["bundle_id"])
        empty = fairness_report(conn, other_bundle["bundle_id"])

        assert global_report["sample"]["attempts"] == 13
        assert scoped["sample"]["attempts"] == 13
        assert empty["sample"]["attempts"] == 0
        assert empty["overall_gap_sd"] is None
        assert all(dimension["gap_sd"] is None for dimension in empty["dimensions"])
        assert empty["feedback_accessibility"]["count"] == 0
    finally:
        conn.close()


def test_station_quality_includes_fairness_gap(tmp_path):
    conn = _conn(tmp_path)
    try:
        original = build_bundle_payload()
        first = freeze_bundle(conn, original, actor="test", version="1.0")
        other = copy.deepcopy(original)
        other["station_id"] = "ST-OTHER-001"
        other["title"] = "其他考站"
        other_bundle = freeze_bundle(conn, other, actor="test", version="1.0")

        rows = {row["bundle_id"]: row for row in station_quality_report(conn)}
        scoped = fairness_report(conn, first["bundle_id"])
        assert rows[first["bundle_id"]]["fairness_gap_sd"] == scoped["overall_gap_sd"]
        assert rows[other_bundle["bundle_id"]]["fairness_gap_sd"] is None
        assert "fairness_edit_gap_pp" in rows[first["bundle_id"]]
    finally:
        conn.close()
