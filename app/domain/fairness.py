"""公平性监测回路：亚组分数差、复核编辑率差与预绑定纠正动作。"""
from __future__ import annotations

import json
import statistics
from typing import Any

from .accessibility import feedback_accessibility

DIMENSIONS = [
    ("gender", "性别"),
    ("origin", "生源"),
    ("language", "语言背景"),
]
SCORE_GAP_THRESHOLD_SD = 0.15
EDIT_GAP_THRESHOLD_PP = 5.0


def _attempt_scores(conn) -> list[dict[str, Any]]:
    bundles = {
        row["bundle_id"]: json.loads(row["payload"])
        for row in conn.execute("SELECT bundle_id, payload FROM bundles").fetchall()
    }
    attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts").fetchall()]
    records = [dict(r) for r in conn.execute("SELECT * FROM score_records").fetchall()]
    by_attempt: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_attempt.setdefault(rec["attempt_id"], []).append(rec)

    results: list[dict[str, Any]] = []
    for attempt in attempts:
        payload = bundles.get(attempt["bundle_id"])
        if not payload:
            continue
        item_weights = {item["item_id"]: float(item.get("weight", 0)) for item in payload.get("rubric", [])}
        earned = 0.0
        scored_weight = 0.0
        total_weight = sum(item_weights.values())
        for rec in by_attempt.get(attempt["attempt_id"], []):
            score = rec["final_score"] if rec["final_score"] is not None else rec["ai_score"]
            if score is None:
                continue
            weight = item_weights.get(rec["item_id"], 0.0)
            earned += weight * float(score) / 2.0
            scored_weight += weight
        if scored_weight <= 0 or total_weight <= 0:
            continue
        results.append(
            {
                "attempt_id": attempt["attempt_id"],
                "score": round(earned / scored_weight * 100, 2),
                "coverage": round(scored_weight / total_weight, 3),
            }
        )
    return results


def _edit_rates(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT a.action, p.gender, p.origin, p.language, r.attempt_id "
        "FROM review_actions a "
        "JOIN score_records r ON r.record_id = a.record_id "
        "JOIN candidate_profiles p ON p.attempt_id = r.attempt_id"
    ).fetchall()
    buckets: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        edited = 1 if row["action"] in ("correct", "reject") else 0
        for key, _label in DIMENSIONS:
            group = row[key]
            if group is None:
                continue
            buckets.setdefault((key, group), []).append(edited)
    result = []
    for (key, group), values in buckets.items():
        if len(values) < 2:
            continue
        result.append(
            {
                "dimension": key,
                "group": group,
                "n": len(values),
                "edit_rate": round(sum(values) / len(values), 3),
            }
        )
    return result


def fairness_report(conn) -> dict[str, Any]:
    attempt_scores = _attempt_scores(conn)
    profiles = {
        row["attempt_id"]: dict(row)
        for row in conn.execute("SELECT * FROM candidate_profiles").fetchall()
    }
    score_values = [row["score"] for row in attempt_scores]
    overall_sd = round(statistics.pstdev(score_values), 3) if len(score_values) > 1 else 0.0

    dimensions: list[dict[str, Any]] = []
    measured_gaps: list[float] = []
    for key, label in DIMENSIONS:
        groups: dict[str, list[float]] = {}
        for row in attempt_scores:
            profile = profiles.get(row["attempt_id"])
            if not profile:
                continue
            group = profile.get(key)
            if not group:
                continue
            groups.setdefault(group, []).append(row["score"])
        group_rows = [
            {
                "name": group,
                "n": len(values),
                "mean": round(statistics.mean(values), 2) if values else None,
                "sd": round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0,
            }
            for group, values in sorted(groups.items())
        ]
        valid_groups = [g for g in group_rows if g["n"] >= 2 and g["mean"] is not None]
        if len(valid_groups) < 2 or overall_sd == 0:
            gap = None
            status = "insufficient"
        else:
            gap = round((max(g["mean"] for g in valid_groups) - min(g["mean"] for g in valid_groups)) / overall_sd, 3)
            status = "pass" if gap <= SCORE_GAP_THRESHOLD_SD else "fail"
            measured_gaps.append(gap)
        dimensions.append(
            {
                "key": key,
                "label": label,
                "groups": group_rows,
                "gap_sd": gap,
                "threshold": SCORE_GAP_THRESHOLD_SD,
                "status": status,
                "corrective_action": (
                    "审查人格提示词与病例设定 → 增补该情境的教学暴露 → 重跑对照"
                    if status == "fail"
                    else "维持监测，下一轮继续观察"
                ),
            }
        )

    edit_rows = _edit_rates(conn)
    edit_by_dimension: dict[str, list[dict[str, Any]]] = {}
    for row in edit_rows:
        edit_by_dimension.setdefault(row["dimension"], []).append(row)
    edit_summary: list[dict[str, Any]] = []
    edit_gap_pp = None
    for key, label in DIMENSIONS:
        rows = edit_by_dimension.get(key, [])
        if len(rows) < 2:
            edit_summary.append(
                {
                    "key": key,
                    "label": label,
                    "groups": rows,
                    "gap_pp": None,
                    "threshold_pp": EDIT_GAP_THRESHOLD_PP,
                    "status": "insufficient",
                    "corrective_action": "ASR 词表与声学适配 → 评分员校准会 → 复核该组全部边缘案例",
                }
            )
            continue
        gap_pp = round((max(r["edit_rate"] for r in rows) - min(r["edit_rate"] for r in rows)) * 100, 2)
        edit_gap_pp = max(edit_gap_pp or 0.0, gap_pp)
        edit_summary.append(
            {
                "key": key,
                "label": label,
                "groups": rows,
                "gap_pp": gap_pp,
                "threshold_pp": EDIT_GAP_THRESHOLD_PP,
                "status": "pass" if gap_pp <= EDIT_GAP_THRESHOLD_PP else "fail",
                "corrective_action": (
                    "ASR 词表与声学适配 → 评分员校准会 → 复核该组全部边缘案例"
                    if gap_pp > EDIT_GAP_THRESHOLD_PP
                    else "维持监测，下一轮继续观察"
                ),
            }
        )

    return {
        "dimensions": dimensions,
        "edit_rate": {
            "dimensions": edit_summary,
            "gap_pp": edit_gap_pp,
            "threshold_pp": EDIT_GAP_THRESHOLD_PP,
        },
        "feedback_accessibility": feedback_accessibility(conn),
        "overall_gap_sd": max(measured_gaps) if measured_gaps else None,
        "sample": {
            "attempts": len(attempt_scores),
            "score_sd": overall_sd,
        },
        "note": "合成亚组数据仅用于验证公平性监测回路；真实分组字段不得向评分员暴露。",
    }
