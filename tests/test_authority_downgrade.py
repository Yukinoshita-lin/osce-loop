"""校准降级：指标跌破阈值的评分项自动下调 AI 权限。"""
from __future__ import annotations

from app import db
from app.domain.bundle import apply_item_downgrades, freeze_bundle, get_bundle
from app.seed import build_bundle_payload


def _conn(tmp_path):
    conn = db.connect(tmp_path / "downgrade.db")
    db.init_db(conn)
    payload = build_bundle_payload()
    payload["station_id"] = "ST-DOWN-001"
    bundle = freeze_bundle(conn, payload, actor="测试", version="1.0")
    return conn, bundle


def test_apply_item_downgrades_steps_down_and_audits(tmp_path):
    conn, bundle = _conn(tmp_path)
    item = next(i for i in bundle["payload"]["rubric"] if i["item_id"] == "OP.01")
    assert item["ai_authority"] == "suggest"

    first = apply_item_downgrades(
        conn,
        bundle,
        ["OP.01", "NOT_EXIST"],
        reason="item_quality_below_threshold",
        actor="维护",
    )
    assert first == [{"item_id": "OP.01", "from": "suggest", "to": "extract"}]
    override = conn.execute(
        "SELECT authority FROM authority_overrides WHERE bundle_id = ? AND item_id = 'OP.01'",
        (bundle["bundle_id"],),
    ).fetchone()
    assert override["authority"] == "extract"

    second = apply_item_downgrades(
        conn,
        bundle,
        ["OP.01"],
        reason="item_quality_below_threshold",
        actor="维护",
    )
    assert second == [{"item_id": "OP.01", "from": "extract", "to": "none"}]
    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'authority.calibration_downgrade' "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    conn.close()


def test_apply_item_downgrades_ignores_empty_targets(tmp_path):
    conn, bundle = _conn(tmp_path)
    assert apply_item_downgrades(conn, bundle, [], reason="x") == []
    assert get_bundle(conn, bundle["bundle_id"]) is not None
    conn.close()
