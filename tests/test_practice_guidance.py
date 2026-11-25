"""练习报告中的 SP 阶段末引导汇总。"""
from __future__ import annotations

import json

from app import db
from app.domain.practice import practice_guidance_summary


def _report(conn, session_id, guidance=None):
    conn.execute(
        "INSERT INTO sp_reports(session_id, bundle_id, report, created_at) VALUES(?,?,?,?)",
        (session_id, "bnd", json.dumps({"guidance": guidance} if guidance else {}), db.now_iso()),
    )


def test_guidance_summary_counts_reports_and_phases(tmp_path):
    conn = db.connect(tmp_path / "practice_guidance.db")
    db.init_db(conn)
    _report(conn, "sp_1", {"count": 1, "phases": ["information_gathering"]})
    _report(conn, "sp_2", {"count": 2, "phases": ["information_gathering", "assessment"]})
    _report(conn, "sp_3", {"count": 0, "phases": []})
    _report(conn, "sp_4")
    conn.commit()

    summary = practice_guidance_summary(conn)
    assert summary["reports"] == 4
    assert summary["guided_reports"] == 2
    assert summary["guide_rate"] == 0.5
    assert summary["total_guidance"] == 3
    by_phase = {item["phase"]: item for item in summary["by_phase"]}
    assert by_phase["information_gathering"]["count"] == 2
    assert by_phase["information_gathering"]["label"]
    assert by_phase["assessment"]["count"] == 1
    conn.close()


def test_guidance_summary_is_unmeasured_without_reports(tmp_path):
    conn = db.connect(tmp_path / "practice_guidance_empty.db")
    db.init_db(conn)
    summary = practice_guidance_summary(conn)
    assert summary["reports"] == 0
    assert summary["guided_reports"] == 0
    assert summary["guide_rate"] is None
    assert summary["total_guidance"] == 0
    assert summary["by_phase"] == []
    conn.close()
