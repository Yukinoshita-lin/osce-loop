"""站级心理测量：难度、区分度、项间相关、Cronbach's α 与地板/天花板异常。"""
from __future__ import annotations

import statistics
from typing import Any

from .bundle import get_bundle, list_station_bundles


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return round(cov / (var_x * var_y) ** 0.5, 3)


def _cronbach_alpha(matrix: list[list[float]]) -> float | None:
    """matrix 为“场次 × 评分项”的完整评分矩阵。"""
    if len(matrix) < 3:
        return None
    k = len(matrix[0])
    if k < 2:
        return None
    item_variances = []
    for index in range(k):
        values = [row[index] for row in matrix]
        item_variances.append(statistics.pvariance(values))
    totals = [sum(row) for row in matrix]
    total_variance = statistics.pvariance(totals)
    if total_variance <= 0:
        return None
    alpha = k / (k - 1) * (1 - sum(item_variances) / total_variance)
    return round(alpha, 3)


def _pairwise_correlations(
    scores: dict[str, dict[str, int]], item_ids: list[str]
) -> list[dict[str, Any]]:
    """两两计算项间 Pearson 相关；样本不足或方差为零的配对不返回。"""
    pairs: list[dict[str, Any]] = []
    for index, item_a in enumerate(item_ids):
        for item_b in item_ids[index + 1 :]:
            xs: list[float] = []
            ys: list[float] = []
            for per_attempt in scores.values():
                if item_a in per_attempt and item_b in per_attempt:
                    xs.append(float(per_attempt[item_a]))
                    ys.append(float(per_attempt[item_b]))
            correlation = _pearson(xs, ys)
            if correlation is None:
                continue
            pairs.append(
                {"item_a": item_a, "item_b": item_b, "r": correlation, "n": len(xs)}
            )
    pairs.sort(key=lambda entry: abs(entry["r"]), reverse=True)
    return pairs


def _weighted_total(items: list[dict[str, Any]], scores: dict[str, int]) -> float | None:
    earned = 0.0
    weight_sum = 0.0
    for item in items:
        score = scores.get(item["item_id"])
        if score is None:
            continue
        weight = float(item.get("weight", 0))
        weight_sum += weight
        earned += weight * float(score) / 2.0
    if weight_sum <= 0:
        return None
    return round(earned / weight_sum * 100, 2)


def station_psychometrics(conn) -> list[dict[str, Any]]:
    results = []
    for summary in list_station_bundles(conn):
        bundle = get_bundle(conn, summary["bundle_id"])
        if not bundle:
            continue
        payload = bundle["payload"]
        items = payload.get("rubric", [])
        item_ids = [item["item_id"] for item in items]
        item_by_id = {item["item_id"]: item for item in items}
        pass_line = float(payload.get("pass_line", 60))

        attempts = conn.execute(
            "SELECT attempt_id FROM attempts WHERE bundle_id = ?", (bundle["bundle_id"],)
        ).fetchall()
        attempt_ids = [row["attempt_id"] for row in attempts]
        scores: dict[str, dict[str, int]] = {}
        if attempt_ids:
            placeholders = ",".join("?" * len(attempt_ids))
            for row in conn.execute(
                f"SELECT attempt_id, item_id, final_score, ai_score FROM score_records "
                f"WHERE attempt_id IN ({placeholders})",
                attempt_ids,
            ).fetchall():
                score = row["final_score"] if row["final_score"] is not None else row["ai_score"]
                if score is None:
                    continue
                scores.setdefault(row["attempt_id"], {})[row["item_id"]] = int(score)

        totals = []
        for per_attempt in scores.values():
            total = _weighted_total(items, per_attempt)
            if total is not None:
                totals.append(total)

        item_stats = []
        complete_rows: list[list[float]] = []
        for attempt_id, per_attempt in scores.items():
            if all(item_id in per_attempt for item_id in item_ids):
                complete_rows.append([float(per_attempt[item_id]) for item_id in item_ids])
        for item in items:
            item_id = item["item_id"]
            values = [per_attempt[item_id] for per_attempt in scores.values() if item_id in per_attempt]
            difficulty = round(sum(values) / len(values) / 2.0, 3) if values else None
            paired_item = []
            paired_rest = []
            for per_attempt in scores.values():
                if item_id not in per_attempt:
                    continue
                rest = [score for other, score in per_attempt.items() if other != item_id]
                if len(rest) < 2:
                    continue
                paired_item.append(float(per_attempt[item_id]))
                paired_rest.append(float(sum(rest)))
            discrimination = _pearson(paired_item, paired_rest)
            item_stats.append(
                {
                    "item_id": item_id,
                    "title": item.get("title"),
                    "critical": bool(item.get("critical")),
                    "n": len(values),
                    "difficulty": difficulty,
                    "discrimination": discrimination,
                    "all_max": len(values) >= 3 and all(value == 2 for value in values),
                    "all_min": len(values) >= 3 and all(value == 0 for value in values),
                }
            )

        ceiling_items = [entry["item_id"] for entry in item_stats if entry["all_max"]]
        floor_items = [entry["item_id"] for entry in item_stats if entry["all_min"]]
        low_discrimination = [
            entry["item_id"]
            for entry in item_stats
            if entry["discrimination"] is not None and entry["discrimination"] < 0.2
        ]
        alpha = _cronbach_alpha(complete_rows)
        inter_item = _pairwise_correlations(scores, item_ids)
        redundant_pairs = [
            entry for entry in inter_item if entry["r"] >= 0.8 and entry["n"] >= 5
        ]
        flags = []
        if len(totals) < 5:
            flags.append("sample_insufficient")
        if ceiling_items:
            flags.append("ceiling")
        if floor_items:
            flags.append("floor")
        if low_discrimination:
            flags.append("low_discrimination")
        if redundant_pairs:
            flags.append("redundant_items")
        results.append(
            {
                "bundle_id": bundle["bundle_id"],
                "station_id": bundle["station_id"],
                "title": bundle["title"],
                "version": bundle["version"],
                "scored_attempts": len(totals),
                "mean_total": round(sum(totals) / len(totals), 2) if totals else None,
                "sd_total": round(statistics.pvariance(totals) ** 0.5, 2) if len(totals) >= 2 else None,
                "pass_rate": round(sum(1 for total in totals if total >= pass_line) / len(totals), 3)
                if totals
                else None,
                "alpha": alpha,
                "flags": flags,
                "ceiling_items": ceiling_items,
                "floor_items": floor_items,
                "low_discrimination_items": low_discrimination,
                "inter_item": inter_item[:10],
                "redundant_pairs": redundant_pairs[:10],
                "items": item_stats,
            }
        )
    results.sort(key=lambda entry: entry["station_id"])
    return results
