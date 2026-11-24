"""S6 评分表维护看板：把人工拒绝理由码聚合到评分项，指向该改哪条锚。"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .bundle import list_bundles

REASON_CODES = ("证据不足", "判定过严", "判定过宽", "行为锚歧义", "其他")

REASON_ACTIONS = {
    "证据不足": "补充证据探针或信息槽，确保该行为可被采集",
    "判定过严": "下调行为锚档位，或在 partial 档补充示例",
    "判定过宽": "上调行为锚档位，或收紧 positive 档判据",
    "行为锚歧义": "把行为锚改写为可观察动词 + 可听/可见对象",
    "其他": "人工复核该项表述与本站案例的适配性",
}

MIN_ACTIONS = 3
DISAGREEMENT_FLOOR = 0.5
REJECT_FLOOR = 2


def rubric_maintenance(
    conn, bundle_id: str | None = None, limit: int = 20
) -> dict[str, Any]:
    """按评分项聚合复核动作与拒绝理由码；理由码只指向维护动作，不替代专家裁定。"""
    bundles = list_bundles(conn)
    bundle = None
    if bundle_id:
        bundle = next((b for b in bundles if b["bundle_id"] == bundle_id), None)
    elif bundles:
        bundle = bundles[0]
    if bundle is None:
        return {
            "bundle_id": bundle_id,
            "items": [],
            "reasons": [],
            "totals": {"actions": 0, "reviewed_items": 0, "needs_review": 0},
            "thresholds": {
                "min_actions": MIN_ACTIONS,
                "disagreement_floor": DISAGREEMENT_FLOOR,
                "reject_floor": REJECT_FLOOR,
            },
            "note": "暂无站包，无法聚合复核理由码。",
        }

    item_map = {item["item_id"]: item for item in bundle["payload"].get("rubric", [])}
    rows = conn.execute(
        "SELECT ra.action, ra.reason_code, sr.item_id FROM review_actions ra "
        "JOIN score_records sr ON sr.record_id = ra.record_id "
        "JOIN attempts a ON a.attempt_id = sr.attempt_id "
        "WHERE a.bundle_id = ?",
        (bundle["bundle_id"],),
    ).fetchall()

    stats: dict[str, dict[str, Any]] = {}
    reason_total: Counter[str] = Counter()
    for row in rows:
        item_id = row["item_id"]
        entry = stats.setdefault(
            item_id,
            {"total": 0, "accepted": 0, "corrected": 0, "rejected": 0, "reasons": Counter()},
        )
        entry["total"] += 1
        action = row["action"]
        if action == "accept":
            entry["accepted"] += 1
        elif action == "correct":
            entry["corrected"] += 1
        elif action == "reject":
            entry["rejected"] += 1
            code = row["reason_code"] if row["reason_code"] in REASON_ACTIONS else "其他"
            entry["reasons"][code] += 1
            reason_total[code] += 1

    items: list[dict[str, Any]] = []
    for item_id, entry in stats.items():
        disagreement = (
            round((entry["corrected"] + entry["rejected"]) / entry["total"], 3)
            if entry["total"]
            else 0.0
        )
        dominant = entry["reasons"].most_common(1)[0][0] if entry["reasons"] else None
        high_disagreement = entry["total"] >= MIN_ACTIONS and disagreement >= DISAGREEMENT_FLOOR
        needs_review = entry["rejected"] >= REJECT_FLOOR or high_disagreement
        meta = item_map.get(item_id) or {}
        items.append(
            {
                "item_id": item_id,
                "title": meta.get("title") or item_id,
                "critical": bool(meta.get("critical")),
                "total": entry["total"],
                "accepted": entry["accepted"],
                "corrected": entry["corrected"],
                "rejected": entry["rejected"],
                "disagreement": disagreement,
                "dominant_reason": dominant,
                "reasons": [
                    {"code": code, "count": count}
                    for code, count in entry["reasons"].most_common()
                ],
                "suggested_action": REASON_ACTIONS.get(dominant)
                or ("复核行为锚与证据类型是否匹配" if high_disagreement else None),
                "needs_review": needs_review,
            }
        )
    needs_review = sum(1 for item in items if item["needs_review"])
    items.sort(
        key=lambda item: (
            not item["needs_review"],
            -item["rejected"],
            -item["disagreement"],
            item["item_id"],
        )
    )
    return {
        "bundle_id": bundle["bundle_id"],
        "station_id": bundle["station_id"],
        "title": bundle["payload"].get("title"),
        "version": bundle["version"],
        "items": items[:limit],
        "reasons": [
            {"code": code, "count": count, "suggested_action": REASON_ACTIONS.get(code)}
            for code, count in reason_total.most_common()
        ],
        "totals": {
            "actions": len(rows),
            "reviewed_items": len(stats),
            "needs_review": needs_review,
        },
        "thresholds": {
            "min_actions": MIN_ACTIONS,
            "disagreement_floor": DISAGREEMENT_FLOOR,
            "reject_floor": REJECT_FLOOR,
        },
        "note": "理由码只回答“该改哪条锚”；是否改写仍由临床教育专家裁定，并随新版本站包冻结。",
    }
