"""S7→S1 闭环：把站级质量、评分项维护与已处置记录汇总为下一年度命题输入。"""
from __future__ import annotations

from typing import Any

from .. import db
from .psychometrics import station_psychometrics
from .rubric_maintenance import rubric_maintenance
from .station_review import DECISIONS, list_station_reviews

FLAG_LABELS = {
    "ceiling": "天花板效应",
    "floor": "地板效应",
    "low_discrimination": "低区分度",
    "sample_insufficient": "样本不足",
    "redundant_items": "冗余项",
}

STATION_ACTIONS = {
    "ceiling": "检查 positive 档是否过宽或信息槽是否提前泄露，下一版收紧判据。",
    "floor": "检查题干可读性、信息槽可达性与评分员理解偏差，必要时调整难度或增加教学暴露。",
    "low_discrimination": "优先改写低区分度行为锚，并用边缘案例验证改写后的区分能力。",
    "sample_insufficient": "先扩大样本，不据此改写评分表。",
    "redundant_items": "合并或删除高相关冗余项，减少重复计分。",
}

DECISION_ACTIONS = {
    "rubric_revision": "在下一版站包落实评分表修订，并记录 supersedes 与修订原因。",
    "sp_retraining": "按分歧与拒绝原因安排 SP 复训，更新后重新完成红队认证。",
    "teaching": "把该站暴露的能力缺口纳入下一轮教学与练习计划。",
}

SEVERITY_ORDER = {"high": 0, "medium": 1}


def proposal_input(conn) -> dict[str, Any]:
    """聚合可执行的命题改进项；只提供输入，不改写任何已冻结版本。"""
    items: list[dict[str, Any]] = []

    for station in station_psychometrics(conn):
        flags = [flag for flag in station.get("flags") or [] if flag in FLAG_LABELS]
        if not flags:
            continue
        serious = any(flag != "sample_insufficient" for flag in flags)
        actions = []
        for flag in flags:
            action = STATION_ACTIONS.get(flag)
            if action and action not in actions:
                actions.append(action)
        detail_parts = []
        if station.get("mean_total") is not None:
            detail_parts.append(f"均分 {station['mean_total']}")
        if station.get("alpha") is not None:
            detail_parts.append(f"α {station['alpha']}")
        if station.get("low_discrimination_items"):
            detail_parts.append("低区分度项 " + "、".join(station["low_discrimination_items"][:4]))
        items.append(
            {
                "source": "station_quality",
                "severity": "high" if serious else "medium",
                "ref": station.get("station_id"),
                "title": station.get("title"),
                "finding": "站级质量异常：" + "、".join(FLAG_LABELS[flag] for flag in flags),
                "detail": "；".join(detail_parts) or None,
                "recommended": " ".join(actions) or None,
            }
        )

    maintenance = rubric_maintenance(conn)
    for item in maintenance.get("items") or []:
        if not item.get("needs_review"):
            continue
        items.append(
            {
                "source": "rubric_item",
                "severity": "high" if item.get("critical") else "medium",
                "ref": item.get("item_id"),
                "title": item.get("title"),
                "finding": (
                    f"评分项待维护：拒绝 {item.get('rejected', 0)} 次 / "
                    f"分歧率 {float(item.get('disagreement') or 0):.0%}"
                ),
                "detail": (
                    f"主要理由码：{item['dominant_reason']}"
                    if item.get("dominant_reason")
                    else None
                ),
                "recommended": item.get("suggested_action")
                or "复核行为锚与证据类型是否匹配，必要时补充证据探针。",
            }
        )

    for review in list_station_reviews(conn, limit=50):
        recommendation = DECISION_ACTIONS.get(review.get("decision"))
        if not recommendation:
            continue
        items.append(
            {
                "source": "station_review",
                "severity": "high" if review.get("decision") in ("rubric_revision", "sp_retraining") else "medium",
                "ref": review.get("station_id"),
                "title": review.get("note"),
                "finding": f"已记录的处置：{DECISIONS.get(review.get('decision'), review.get('decision'))}",
                "detail": f"{review.get('actor')} · {review.get('created_at')}",
                "recommended": recommendation,
            }
        )

    items.sort(
        key=lambda entry: (
            SEVERITY_ORDER.get(entry["severity"], 9),
            entry["source"],
            str(entry.get("ref") or ""),
        )
    )
    counts = {
        "total": len(items),
        "high": sum(1 for item in items if item["severity"] == "high"),
        "medium": sum(1 for item in items if item["severity"] == "medium"),
        "by_source": {
            source: sum(1 for item in items if item["source"] == source)
            for source in ("station_quality", "rubric_item", "station_review")
        },
    }
    return {
        "generated_at": db.now_iso(),
        "counts": counts,
        "items": items,
        "note": (
            "该清单只作为下一年度命题输入：是否修订、如何修订由临床教育专家裁定；"
            "已冻结站包不改写，修订需发布新版本并记录 supersedes 与原因。"
        ),
    }
