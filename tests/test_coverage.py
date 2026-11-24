"""蓝图覆盖度：能力域占比、空白域与过度重复。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.coverage import blueprint_coverage
from app.seed import build_bundle_payload


def test_coverage_reports_shares_and_gaps(tmp_path):
    conn = db.connect(tmp_path / "coverage.db")
    db.init_db(conn)
    freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")

    report = blueprint_coverage(conn)
    assert report["stations"] == 1
    assert report["domains"]
    assert abs(sum(row["share"] for row in report["domains"]) - 1.0) < 0.01
    domains = {row["domain"] for row in report["domains"]}
    assert {"communication", "history_taking"} <= domains
    uncovered = {row["domain"] for row in report["uncovered"]}
    assert {"procedures", "patient_safety"} <= uncovered
    assert report["overrepresented"]
    conn.close()


def test_coverage_without_bundles_lists_all_gaps(tmp_path):
    conn = db.connect(tmp_path / "coverage_empty.db")
    db.init_db(conn)
    report = blueprint_coverage(conn)
    assert report["domains"] == []
    assert len(report["uncovered"]) == 6
    assert report["total_weight"] == 0
    conn.close()
