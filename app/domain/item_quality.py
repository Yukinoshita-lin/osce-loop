"""项级可判性：人工编辑率、金标准一致率与置信水平。"""
from __future__ import annotations

from typing import Any

from .bundle import get_bundle
from .psychometrics import _pearson


def item_quality_report(conn, bundle_id: str) -> list[dict[str, Any]]:
    bundle = get_bundle(conn, bundle_id)
    if not bundle:
        return []
    attempt_ids = {
        row["attempt_id"]
        for row in conn.execute(
            "SELECT attempt_id FROM attempts WHERE bundle_id = ?", (bundle_id,)
        ).fetchall()
    }
    if not attempt_ids:
        return []
    placeholders = ",".join("?" * len(attempt_ids))
    records = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM score_records WHERE attempt_id IN ({placeholders})",
            tuple(attempt_ids),
        ).fetchall()
    ]
    record_ids = [row["record_id"] for row in records]
    actions: list[dict[str, Any]] = []
    if record_ids:
        marks = ",".join("?" * len(record_ids))
        actions = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM review_actions WHERE record_id IN ({marks})",
                tuple(record_ids),
            ).fetchall()
        ]
    actions_by_record: dict[str, list[str]] = {}
    for action in actions:
        actions_by_record.setdefault(action["record_id"], []).append(action["action"])
    records_by_item: dict[str, list[dict[str, Any]]] = {}
    scores_by_attempt: dict[str, dict[str, int]] = {}
    for record in records:
        records_by_item.setdefault(record["item_id"], []).append(record)
        score = record["final_score"] if record["final_score"] is not None else record["ai_score"]
        if score is not None:
            scores_by_attempt.setdefault(record["attempt_id"], {})[record["item_id"]] = int(score)

    rows: list[dict[str, Any]] = []
    for item in bundle["payload"].get("rubric", []):
        item_records = records_by_item.get(item["item_id"], [])
        reviewed = 0
        edited = 0
        gold_pairs: list[tuple[int, int]] = []
        confidence_values: list[float] = []
        for record in item_records:
            if record["ai_confidence"] is not None:
                confidence_values.append(float(record["ai_confidence"]))
            if record["ai_score"] is not None and record["gold_score"] is not None:
                gold_pairs.append((int(record["ai_score"]), int(record["gold_score"])))
            for action in actions_by_record.get(record["record_id"], []):
                if action in ("accept", "correct", "reject", "manual_score"):
                    reviewed += 1
                if action in ("correct", "reject"):
                    edited += 1
        exact_matches = sum(1 for ai, gold in gold_pairs if ai == gold)
        edit_rate = round(edited / reviewed, 3) if reviewed else None
        exact_rate = round(exact_matches / len(gold_pairs), 3) if gold_pairs else None
        avg_confidence = (
            round(sum(confidence_values) / len(confidence_values), 3)
            if confidence_values
            else None
        )
        paired_item: list[float] = []
        paired_rest: list[float] = []
        for per_attempt in scores_by_attempt.values():
            if item["item_id"] not in per_attempt:
                continue
            rest = [score for other, score in per_attempt.items() if other != item["item_id"]]
            if len(rest) < 2:
                continue
            paired_item.append(float(per_attempt[item["item_id"]]))
            paired_rest.append(float(sum(rest)))
        discrimination = _pearson(paired_item, paired_rest)
        needs_review = (reviewed >= 3 and (edit_rate or 0) >= 0.3) or (
            len(gold_pairs) >= 3 and (exact_rate or 1) <= 0.6
        ) or (
            discrimination is not None and discrimination < 0.2 and len(item_records) >= 5
        )
        rows.append(
            {
                "item_id": item["item_id"],
                "title": item.get("title"),
                "domain": item.get("domain"),
                "critical": bool(item.get("critical")),
                "records": len(item_records),
                "reviewed": reviewed,
                "edited": edited,
                "edit_rate": edit_rate,
                "gold_records": len(gold_pairs),
                "exact_match_rate": exact_rate,
                "avg_confidence": avg_confidence,
                "discrimination": discrimination,
                "needs_review": needs_review,
            }
        )
    rows.sort(
        key=lambda row: (
            not row["needs_review"],
            -(row["edit_rate"] or 0),
            row["exact_match_rate"] if row["exact_match_rate"] is not None else 2,
            row["item_id"],
        )
    )
    return rows
