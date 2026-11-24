"""提示影响分析：比较开启/关闭事实层提示的场次分数分布。"""
from __future__ import annotations

import json
import statistics
from typing import Any

from .. import db


def _weighted_total(items: list[dict[str, Any]], scores: dict[str, int]) -> float | None:
    earned = 0.0
    weight = 0.0
    for item in items:
        score = scores.get(item.get("item_id"))
        if score is None:
            continue
        item_weight = float(item.get("weight", 0))
        weight += item_weight
        earned += item_weight * float(score) / 2.0
    return round(earned / weight * 100, 1) if weight else None


def hint_impact(conn) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT a.attempt_id, a.bundle_id, a.ai_total, r.hints_enabled FROM attempts a "
        "JOIN exam_runs r ON r.run_id = a.run_id WHERE a.run_id IS NOT NULL"
    ).fetchall()
    grouped: dict[bool, list[float]] = {True: [], False: []}
    bundle_cache: dict[str, dict[str, Any]] = {}
    pass_lines: dict[bool, list[float]] = {True: [], False: []}
    for row in rows:
        bundle = bundle_cache.get(row["bundle_id"])
        if bundle is None:
            bundle_row = conn.execute(
                "SELECT payload FROM bundles WHERE bundle_id = ?", (row["bundle_id"],)
            ).fetchone()
            bundle = json.loads(bundle_row["payload"]) if bundle_row else {}
            bundle_cache[row["bundle_id"]] = bundle
        items = bundle.get("rubric") or []
        scores: dict[str, int] = {}
        for record in conn.execute(
            "SELECT item_id, final_score, ai_score FROM score_records WHERE attempt_id = ?",
            (row["attempt_id"],),
        ).fetchall():
            score = record["final_score"] if record["final_score"] is not None else record["ai_score"]
            if score is not None:
                scores[record["item_id"]] = int(score)
        total = _weighted_total(items, scores)
        if total is None:
            continue
        enabled = bool(row["hints_enabled"])
        grouped[enabled].append(total)
        pass_lines[enabled].append(float(bundle.get("pass_line", 60)))

    groups = []
    for enabled in (True, False):
        values = grouped[enabled]
        lines = pass_lines[enabled]
        groups.append(
            {
                "hints_enabled": enabled,
                "label": "提示开启" if enabled else "提示关闭",
                "attempts": len(values),
                "mean_total": round(sum(values) / len(values), 2) if values else None,
                "sd_total": round(statistics.pvariance(values) ** 0.5, 2) if len(values) >= 2 else None,
                "pass_rate": (
                    round(
                        sum(1 for value, line in zip(values, lines) if value >= line) / len(values),
                        3,
                    )
                    if values
                    else None
                ),
            }
        )
    delta = (
        round(groups[0]["mean_total"] - groups[1]["mean_total"], 2)
        if groups[0]["mean_total"] is not None and groups[1]["mean_total"] is not None
        else None
    )
    sufficient = groups[0]["attempts"] > 0 and groups[1]["attempts"] > 0
    return {
        "groups": groups,
        "delta_mean": delta,
        "sufficient": sufficient,
        "status": "ok" if sufficient else "insufficient",
        "note": (
            "仅比较已关联考试执行 run 的场次；提示开启/关闭不是随机分组，"
            "结果只能作为事后线索，不能直接解释为因果效应。"
        ),
    }
