"""S1 命题辅助：L0 草案的校验、强制配额、难度预判与去重。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.drafting import case_topics, generate_bundle_draft
from app.domain.rubric import validate_bundle


def _conn(tmp_path):
    conn = db.connect(tmp_path / "draft.db")
    db.init_db(conn)
    return conn


def test_topics_cover_multiple_presentations():
    topics = {entry["key"] for entry in case_topics()}
    assert {"abdominal_pain", "fever", "dyspnea"} <= topics


def test_draft_passes_freeze_checks_and_has_quotas(tmp_path):
    conn = _conn(tmp_path)
    for topic in ("abdominal_pain", "fever", "dyspnea"):
        result = generate_bundle_draft(conn, topic=topic, difficulty="medium")
        draft = result["draft"]
        assert result["validation"]["can_freeze"] is True
        assert result["validation"]["blocking"] == []
        assert result["quota"]["passed"] is True
        assert draft["risk_level_default"] == "L0"
        assert draft["version"] == "0.1"
        assert len(draft["rubric"]) == 10
        assert draft["instruction_item_refs"] == [item["item_id"] for item in draft["rubric"]]
        assert result["disclosure"]["level"] == "L0_draft"
        assert result["versions"]["model_version"].startswith("mock-draft")
        quota_keys = {check["key"] for check in result["quota"]["checks"]}
        assert quota_keys == {"comorbidity", "unrelated_concern", "ambiguity"}
    conn.close()


def test_draft_embeds_required_quota_content(tmp_path):
    conn = _conn(tmp_path)
    result = generate_bundle_draft(conn, topic="abdominal_pain", difficulty="medium")
    draft = result["draft"]
    extra_texts = [slot["content"] for slot in draft["extra_slots"]]
    assert any("说不上来怎么个疼" in text for text in extra_texts)
    assert any("检查大概要多久" in text for text in extra_texts)
    comorbidity_slot = next(
        item["slot"]["content"] for item in draft["rubric"] if item["item_id"] == "HX.04"
    )
    assert "糖尿病" in comorbidity_slot
    conn.close()


def test_difficulty_controls_critical_items_and_estimate(tmp_path):
    conn = _conn(tmp_path)
    reports = {}
    for difficulty in ("easy", "medium", "hard"):
        result = generate_bundle_draft(conn, topic="fever", difficulty=difficulty)
        reports[difficulty] = result
        critical = sum(1 for item in result["draft"]["rubric"] if item.get("critical"))
        assert result["validation"]["can_freeze"] is True
        assert result["difficulty"]["estimated"] == difficulty
        assert result["difficulty"]["aligned"] is True
        reports[difficulty]["critical"] = critical
    assert reports["easy"]["critical"] == 0
    assert reports["medium"]["critical"] == 1
    assert reports["hard"]["critical"] == 2
    assert (
        reports["easy"]["difficulty"]["expected_pass_rate"]
        > reports["medium"]["difficulty"]["expected_pass_rate"]
        > reports["hard"]["difficulty"]["expected_pass_rate"]
    )
    assert reports["hard"]["draft"]["duration_min"] < reports["easy"]["draft"]["duration_min"]
    conn.close()


def test_draft_duration_override_and_audit(tmp_path):
    conn = _conn(tmp_path)
    result = generate_bundle_draft(conn, topic="dyspnea", difficulty="medium", duration_min=12)
    assert result["draft"]["duration_min"] == 12
    audit = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'bundle.draft_generated'"
    ).fetchall()
    assert len(audit) == 1
    conn.close()


def test_draft_rejects_unknown_topic_and_difficulty(tmp_path):
    conn = _conn(tmp_path)
    for kwargs in ({"topic": "unknown"}, {"topic": "fever", "difficulty": "extreme"}):
        try:
            generate_bundle_draft(conn, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝非法参数：{kwargs}")
    conn.close()


def test_draft_flags_similar_bundle_from_other_station(tmp_path):
    conn = _conn(tmp_path)
    draft = generate_bundle_draft(conn, topic="abdominal_pain", difficulty="medium")["draft"]
    clone = {**draft, "station_id": "ST-OTHER-999"}
    assert validate_bundle(clone)["can_freeze"] is True
    freeze_bundle(conn, clone, actor="测试", version="1.0")

    result = generate_bundle_draft(conn, topic="abdominal_pain", difficulty="medium")
    matches = {entry["station_id"]: entry for entry in result["similarity"]}
    assert "ST-OTHER-999" in matches
    assert matches["ST-OTHER-999"]["similarity"] >= 0.99
    conn.close()
