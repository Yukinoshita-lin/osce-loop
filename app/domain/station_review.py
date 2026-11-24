"""异常站处置：把质量报告中的异常标记转成可追踪的修订动作。"""
from __future__ import annotations

from typing import Any

from .. import db

DECISIONS = {
    "rubric_revision": "修订评分表",
    "sp_retraining": "SP 复训",
    "teaching": "教学改进",
    "monitor": "继续观察",
    "no_action": "暂不处理",
}
FLAGS = {
    "ceiling": "天花板效应",
    "floor": "地板效应",
    "low_discrimination": "低区分度",
    "sample_insufficient": "样本不足",
    "other": "其他异常",
}


def record_station_review(
    conn,
    *,
    station_id: str,
    decision: str,
    note: str,
    flag: str = "other",
    bundle_id: str | None = None,
    actor: str = "考务",
) -> dict[str, Any]:
    if flag not in FLAGS:
        raise ValueError(f"flag 必须是 {', '.join(FLAGS)}")
    if decision not in DECISIONS:
        raise ValueError(f"decision 必须是 {', '.join(DECISIONS)}")
    note = str(note or "").strip()
    if not note:
        raise ValueError("请填写处置说明，便于后续追踪")
    bundle = None
    if bundle_id:
        bundle = conn.execute(
            "SELECT station_id FROM bundles WHERE bundle_id = ?", (bundle_id,)
        ).fetchone()
        if not bundle:
            raise ValueError("bundle_id 指向的站包不存在")
        if bundle["station_id"] != station_id:
            raise ValueError("station_id 与 bundle_id 不一致")
    else:
        bundle = conn.execute(
            "SELECT bundle_id, station_id FROM bundles WHERE station_id = ? "
            "ORDER BY frozen_at DESC, rowid DESC LIMIT 1",
            (station_id,),
        ).fetchone()
        if not bundle:
            raise ValueError("考站不存在")
        bundle_id = bundle["bundle_id"]
    review_id = db.new_id("srv")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO station_reviews(review_id, station_id, bundle_id, flag, decision, note, actor, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (review_id, station_id, bundle_id, flag, decision, note, actor, timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="quality.station_review_recorded",
        object_type="StationReview",
        object_id=review_id,
        after={
            "station_id": station_id,
            "bundle_id": bundle_id,
            "flag": flag,
            "decision": decision,
            "note": note,
        },
    )
    row = conn.execute(
        "SELECT * FROM station_reviews WHERE review_id = ?", (review_id,)
    ).fetchone()
    return dict(row)


def list_station_reviews(
    conn, station_id: str | None = None, *, limit: int = 50
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    if station_id:
        rows = conn.execute(
            "SELECT * FROM station_reviews WHERE station_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (station_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM station_reviews ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
