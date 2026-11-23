"""A/B 对照报告：AI 辅助组 vs 纯人工组的耗时、QWK、通过率与边缘案例表现。"""
from __future__ import annotations

import json
import statistics
from typing import Any

from .stats import quadratic_weighted_kappa

GROUPS = [
    ("ai_assisted", "AI 辅助组"),
    ("human_only", "纯人工组"),
]


def _attempt_totals(conn) -> dict[str, float]:
    bundles = {
        row["bundle_id"]: json.loads(row["payload"])
        for row in conn.execute("SELECT bundle_id, payload FROM bundles").fetchall()
    }
    attempt_bundle = {
        row["attempt_id"]: row["bundle_id"]
        for row in conn.execute("SELECT attempt_id, bundle_id FROM attempts").fetchall()
    }
    reviews = conn.execute("SELECT attempt_id, item_id, score FROM ab_reviews").fetchall()
    by_attempt: dict[str, list[Any]] = {}
    for row in reviews:
        by_attempt.setdefault(row["attempt_id"], []).append(row)

    totals: dict[str, float] = {}
    for attempt_id, rows in by_attempt.items():
        payload = bundles.get(attempt_bundle.get(attempt_id))
        if not payload:
            continue
        weights = {item["item_id"]: float(item.get("weight", 0)) for item in payload.get("rubric", [])}
        earned = 0.0
        total = 0.0
        for row in rows:
            weight = weights.get(row["item_id"], 0.0)
            total += weight
            earned += weight * float(row["score"]) / 2.0
        if total > 0:
            totals[attempt_id] = round(earned / total * 100, 2)
    return totals


def ab_report(conn) -> dict[str, Any]:
    attempts = {
        row["attempt_id"]: dict(row)
        for row in conn.execute("SELECT * FROM attempts").fetchall()
    }
    gold = {
        (row["attempt_id"], row["item_id"]): row["gold_score"]
        for row in conn.execute("SELECT attempt_id, item_id, gold_score FROM score_records").fetchall()
        if row["gold_score"] is not None
    }
    totals = _attempt_totals(conn)
    pass_lines = {
        row["bundle_id"]: float(json.loads(row["payload"]).get("pass_line", 60))
        for row in conn.execute("SELECT bundle_id, payload FROM bundles").fetchall()
    }

    groups: list[dict[str, Any]] = []
    values: dict[str, dict[str, Any]] = {}
    for key, name in GROUPS:
        reviews = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM ab_reviews WHERE group_name = ?", (key,)
            ).fetchall()
        ]
        attempt_ids = {r["attempt_id"] for r in reviews}
        scores = [totals[a] for a in attempt_ids if a in totals]
        qwk_pairs = [
            (r["score"], gold[(r["attempt_id"], r["item_id"])])
            for r in reviews
            if (r["attempt_id"], r["item_id"]) in gold
        ]
        near_pairs = [
            (r["score"], gold[(r["attempt_id"], r["item_id"])])
            for r in reviews
            if (r["attempt_id"], r["item_id"]) in gold
            and attempts.get(r["attempt_id"], {}).get("near_cutoff")
        ]
        near_agreement = (
            round(sum(1 for a, b in near_pairs if a == b) / len(near_pairs), 4)
            if near_pairs
            else None
        )
        pass_line = 60.0
        for attempt_id in attempt_ids:
            bundle_id = attempts.get(attempt_id, {}).get("bundle_id")
            if bundle_id:
                pass_line = pass_lines.get(bundle_id, pass_line)
        total_seconds = sum(float(r["seconds_spent"] or 0) for r in reviews)
        group = {
            "key": key,
            "name": name,
            "attempts": len(attempt_ids),
            "items": len(reviews),
            "mean_score": round(statistics.mean(scores), 2) if scores else None,
            "pass_rate": round(sum(1 for s in scores if s >= pass_line) / len(scores), 3) if scores else None,
            "qwk": quadratic_weighted_kappa([p[0] for p in qwk_pairs], [p[1] for p in qwk_pairs]),
            "near_cutoff_agreement": near_agreement,
            "avg_seconds_per_item": round(total_seconds / len(reviews), 1) if reviews else None,
            "teacher_hours": round(total_seconds / 3600, 2),
            "pass_line": pass_line,
        }
        groups.append(group)
        values[key] = group

    treatment = values.get("ai_assisted", {})
    control = values.get("human_only", {})
    reduction = None
    if treatment.get("teacher_hours") and control.get("teacher_hours"):
        reduction = round(
            (control["teacher_hours"] - treatment["teacher_hours"]) / control["teacher_hours"], 4
        )
    near_improvement = None
    if treatment.get("near_cutoff_agreement") is not None and control.get("near_cutoff_agreement") is not None:
        near_improvement = round(
            treatment["near_cutoff_agreement"] - control["near_cutoff_agreement"], 4
        )
    qwk_delta = None
    if treatment.get("qwk") is not None and control.get("qwk") is not None:
        qwk_delta = round(treatment["qwk"] - control["qwk"], 4)

    return {
        "groups": groups,
        "comparison": {
            "teacher_hours_reduction": reduction,
            "teacher_target": 0.40,
            "near_improvement": near_improvement,
            "near_target": 0.10,
            "qwk_delta": qwk_delta,
            "score_delta": round((treatment.get("mean_score") or 0) - (control.get("mean_score") or 0), 2)
            if treatment.get("mean_score") is not None and control.get("mean_score") is not None
            else None,
        },
        "note": "合成 A/B 数据用于验证报告结构与 G2 计算；真实研究需随机分组、基线协变量校正与学术委员会审议。",
    }
