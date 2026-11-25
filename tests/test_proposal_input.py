"""下一年度命题输入：聚合站级质量、项维护与已处置记录。"""
from __future__ import annotations

from app import db
from app.domain.proposal_input import proposal_input


def _conn(tmp_path):
    conn = db.connect(tmp_path / "proposal.db")
    db.init_db(conn)
    return conn


def test_empty_database_returns_empty_input(tmp_path):
    conn = _conn(tmp_path)
    result = proposal_input(conn)
    assert result["counts"]["total"] == 0
    assert result["items"] == []
    assert "冻结" in result["note"]
    conn.close()


def test_proposal_input_merges_sources_and_skips_monitor(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.proposal_input.station_psychometrics",
        lambda conn: [
            {
                "station_id": "ST-1",
                "title": "急性胸痛",
                "flags": ["low_discrimination", "sample_insufficient"],
                "mean_total": 72.5,
                "alpha": 0.42,
                "low_discrimination_items": ["HX.03"],
            }
        ],
    )
    monkeypatch.setattr(
        "app.domain.proposal_input.rubric_maintenance",
        lambda conn: {
            "items": [
                {
                    "item_id": "HX.03",
                    "title": "放射部位",
                    "critical": True,
                    "needs_review": True,
                    "rejected": 3,
                    "disagreement": 0.75,
                    "dominant_reason": "行为锚歧义",
                    "suggested_action": "改写为可观察动词",
                }
            ]
        },
    )
    monkeypatch.setattr(
        "app.domain.proposal_input.list_station_reviews",
        lambda conn, limit=50: [
            {
                "station_id": "ST-1",
                "decision": "rubric_revision",
                "note": "修订两处锚",
                "actor": "考官",
                "created_at": "2026-01-01T00:00:00",
            },
            {
                "station_id": "ST-1",
                "decision": "monitor",
                "note": "继续观察",
                "actor": "考官",
                "created_at": "2026-01-02T00:00:00",
            },
        ],
    )
    result = proposal_input(conn)
    assert result["counts"]["total"] == 3
    assert result["counts"]["high"] == 3
    assert result["counts"]["by_source"] == {
        "station_quality": 1,
        "rubric_item": 1,
        "station_review": 1,
    }
    findings = [item["finding"] for item in result["items"]]
    assert any("低区分度" in finding for finding in findings)
    assert any("评分项待维护" in finding for finding in findings)
    assert any("修订评分表" in finding for finding in findings)
    assert not any("继续观察" in finding for finding in findings)
    station_item = next(item for item in result["items"] if item["source"] == "station_quality")
    assert "低区分度" in station_item["recommended"]
    conn.close()


def test_sample_insufficient_and_teaching_are_medium(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    monkeypatch.setattr(
        "app.domain.proposal_input.station_psychometrics",
        lambda conn: [
            {"station_id": "ST-2", "title": "样本不足站", "flags": ["sample_insufficient"]}
        ],
    )
    monkeypatch.setattr(
        "app.domain.proposal_input.rubric_maintenance", lambda conn: {"items": []}
    )
    monkeypatch.setattr(
        "app.domain.proposal_input.list_station_reviews",
        lambda conn, limit=50: [
            {
                "station_id": "ST-2",
                "decision": "teaching",
                "note": "补教学暴露",
                "actor": "考官",
                "created_at": "2026-01-01T00:00:00",
            }
        ],
    )
    result = proposal_input(conn)
    assert result["counts"]["medium"] == 2
    assert result["counts"]["high"] == 0
    conn.close()
