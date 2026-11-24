"""评分表维护看板：拒绝理由码聚合到评分项，指向该改哪条锚。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.rubric_maintenance import (
    DISAGREEMENT_FLOOR,
    MIN_ACTIONS,
    REASON_ACTIONS,
    rubric_maintenance,
)
from app.seed import build_bundle_payload


def _setup(tmp_path):
    conn = db.connect(tmp_path / "maint.db")
    db.init_db(conn)
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="测试", version="1.0")
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("att_1", bundle["bundle_id"], "C001", "考生", "SES", "L2", "scored", "2026-12-10T09:00:00+08:00"),
    )
    return conn, bundle


def _record(conn, item_id: str) -> str:
    record_id = f"r_att_1_{item_id}"
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, routing, status, "
        "final_score, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (record_id, "att_1", item_id, 1, "{}", "reviewed", 1, "2026-12-10T09:10:00+08:00"),
    )
    return record_id


def _action(conn, record_id: str, action: str, reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO review_actions(action_id, record_id, reviewer, action, from_score, to_score, "
        "reason_code, seconds_spent, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (db.new_id("act"), record_id, "考官", action, 1, None, reason, 12.0, "2026-12-10T09:12:00+08:00"),
    )


def test_reason_codes_aggregate_to_item_and_priority(tmp_path):
    conn, bundle = _setup(tmp_path)
    anchor_record = _record(conn, "HX.03")
    _action(conn, anchor_record, "reject", "行为锚歧义")
    _action(conn, anchor_record, "reject", "行为锚歧义")
    _action(conn, anchor_record, "reject", "判定过严")
    mixed_record = _record(conn, "HX.01")
    _action(conn, mixed_record, "reject", "证据不足")
    _action(conn, mixed_record, "accept")
    _action(conn, mixed_record, "accept")
    clean_record = _record(conn, "HX.02")
    _action(conn, clean_record, "accept")
    conn.commit()

    result = rubric_maintenance(conn)
    assert result["bundle_id"] == bundle["bundle_id"]
    assert result["totals"] == {"actions": 7, "reviewed_items": 3, "needs_review": 1}
    by_item = {item["item_id"]: item for item in result["items"]}
    anchor = by_item["HX.03"]
    assert anchor["total"] == 3 and anchor["rejected"] == 3
    assert anchor["dominant_reason"] == "行为锚歧义"
    assert anchor["suggested_action"] == REASON_ACTIONS["行为锚歧义"]
    assert anchor["needs_review"] is True
    assert anchor["reasons"][0] == {"code": "行为锚歧义", "count": 2}

    mixed = by_item["HX.01"]
    assert mixed["rejected"] == 1
    assert round(mixed["disagreement"], 3) == round(1 / 3, 3)
    assert mixed["needs_review"] is False
    assert mixed["dominant_reason"] == "证据不足"

    clean = by_item["HX.02"]
    assert clean["needs_review"] is False
    assert clean["dominant_reason"] is None

    assert result["items"][0]["item_id"] == "HX.03"
    reasons = {r["code"]: r["count"] for r in result["reasons"]}
    assert reasons == {"行为锚歧义": 2, "判定过严": 1, "证据不足": 1}
    assert result["thresholds"] == {
        "min_actions": MIN_ACTIONS,
        "disagreement_floor": DISAGREEMENT_FLOOR,
        "reject_floor": 2,
    }


def test_single_reject_below_floor_is_not_priority(tmp_path):
    conn, _ = _setup(tmp_path)
    for item_id in ("HX.01", "HX.02", "HX.03"):
        record_id = _record(conn, item_id)
        _action(conn, record_id, "accept")
        _action(conn, record_id, "accept")
        _action(conn, record_id, "reject", "其他")
    conn.commit()

    result = rubric_maintenance(conn)
    assert all(item["needs_review"] is False for item in result["items"])
    assert result["totals"]["needs_review"] == 0


def test_unknown_reason_falls_back_to_other(tmp_path):
    conn, _ = _setup(tmp_path)
    record_id = _record(conn, "HX.01")
    _action(conn, record_id, "reject", "随手填的理由")
    _action(conn, record_id, "reject", None)
    conn.commit()

    result = rubric_maintenance(conn)
    item = result["items"][0]
    assert item["dominant_reason"] == "其他"
    assert item["rejected"] == 2
    assert item["needs_review"] is True


def test_high_disagreement_without_reject_triggers_review(tmp_path):
    conn, _ = _setup(tmp_path)
    record_id = _record(conn, "HX.04")
    _action(conn, record_id, "correct")
    _action(conn, record_id, "correct")
    _action(conn, record_id, "accept")
    conn.commit()

    item = rubric_maintenance(conn)["items"][0]
    assert item["rejected"] == 0
    assert item["needs_review"] is True
    assert item["suggested_action"] == "复核行为锚与证据类型是否匹配"


def test_maintenance_is_scoped_to_bundle_version(tmp_path):
    conn, old_bundle = _setup(tmp_path)
    _action(conn, _record(conn, "HX.01"), "reject", "证据不足")
    revised = build_bundle_payload()
    revised["title"] = str(revised["title"]) + "（改版）"
    new_bundle = freeze_bundle(
        conn,
        revised,
        actor="测试",
        version="1.1",
        supersedes=old_bundle["bundle_id"],
        reason="改版",
    )
    conn.commit()

    latest = rubric_maintenance(conn)
    assert latest["bundle_id"] == new_bundle["bundle_id"]
    assert latest["items"] == []
    assert latest["totals"]["actions"] == 0

    historical = rubric_maintenance(conn, bundle_id=old_bundle["bundle_id"])
    assert historical["items"][0]["item_id"] == "HX.01"
