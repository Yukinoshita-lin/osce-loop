"""分站质量：按冻结版本过滤与最新版本标记。"""
from __future__ import annotations

import copy

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.stats import quality_report
from app.domain.station_quality import station_quality_report
from app.seed import build_bundle_payload, seed_if_empty


def _conn(tmp_path):
    conn = db.connect(tmp_path / "station_quality.db")
    db.init_db(conn)
    seed_if_empty(conn)
    return conn


def test_quality_report_filters_to_bundle(tmp_path):
    conn = _conn(tmp_path)
    try:
        original = build_bundle_payload()
        original_bundle = freeze_bundle(conn, original, actor="test", version="1.0")
        other = copy.deepcopy(original)
        other["station_id"] = "ST-OTHER-001"
        other["title"] = "其他考站"
        other_bundle = freeze_bundle(conn, other, actor="test", version="1.0")

        full = quality_report(conn)
        scoped = quality_report(conn, original_bundle["bundle_id"])
        empty = quality_report(conn, other_bundle["bundle_id"])
        assert full["counts"]["attempts"] == 13
        assert scoped["counts"]["attempts"] == 13
        assert scoped["qwk_ai"] == full["qwk_ai"]
        assert empty["counts"]["attempts"] == 0
        assert empty["qwk_ai"] is None
        assert empty["evidence"]["rate"] is None
    finally:
        conn.close()


def test_station_quality_marks_latest_and_keeps_historical(tmp_path):
    conn = _conn(tmp_path)
    try:
        original = build_bundle_payload()
        first = freeze_bundle(conn, original, actor="test", version="1.0")
        revised = copy.deepcopy(original)
        revised["title"] = revised["title"] + "（第二版）"
        second = freeze_bundle(
            conn, revised, actor="test", version="1.1", supersedes=first["bundle_id"], reason="修订"
        )
        other = copy.deepcopy(original)
        other["station_id"] = "ST-OTHER-001"
        other["title"] = "其他考站"
        other_bundle = freeze_bundle(conn, other, actor="test", version="1.0")

        rows = station_quality_report(conn)
        by_id = {row["bundle_id"]: row for row in rows}
        assert by_id[first["bundle_id"]]["attempts"] == 13
        assert by_id[first["bundle_id"]]["is_latest"] is False
        assert by_id[second["bundle_id"]]["attempts"] == 0
        assert by_id[second["bundle_id"]]["is_latest"] is True
        assert by_id[second["bundle_id"]]["qwk_ai"] is None
        assert by_id[other_bundle["bundle_id"]]["is_latest"] is True
        assert by_id[other_bundle["bundle_id"]]["attempts"] == 0
    finally:
        conn.close()
