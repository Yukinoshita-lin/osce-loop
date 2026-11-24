"""AI 成本归集：按场次还原评分与转写调用，给出单站成本基线与练习/考试目标核对。"""
from __future__ import annotations

from typing import Any

from .. import db
from ..config import CNY_PER_USD, COST_TARGET_CNY_EXAM, COST_TARGET_CNY_PRACTICE


def ai_cost_summary(conn) -> dict[str, Any]:
    attempt_rows = conn.execute(
        "SELECT attempt_id, media_id, risk_level FROM attempts"
    ).fetchall()
    attempt_ids = {row["attempt_id"] for row in attempt_rows}
    media_to_attempt = {
        row["media_id"]: row["attempt_id"] for row in attempt_rows if row["media_id"]
    }
    risk_by_attempt = {row["attempt_id"]: row["risk_level"] for row in attempt_rows}

    total = 0.0
    priced_calls = 0
    per_attempt: dict[str, float] = {}
    for row in conn.execute("SELECT subject_ref, cost_usd FROM ai_calls").fetchall():
        cost = row["cost_usd"]
        if cost is None:
            continue
        ref = row["subject_ref"] or ""
        attempt_id = None
        if ref.startswith("media:"):
            attempt_id = media_to_attempt.get(ref[6:])
        elif ":" in ref:
            prefix = ref.split(":", 1)[0]
            if prefix in attempt_ids:
                attempt_id = prefix
        if not attempt_id:
            continue
        total += float(cost)
        priced_calls += 1
        per_attempt[attempt_id] = per_attempt.get(attempt_id, 0.0) + float(cost)

    buckets = {
        "practice": {
            "key": "practice",
            "label": "练习",
            "target_cny": COST_TARGET_CNY_PRACTICE,
            "attempts": 0,
            "cost_usd": 0.0,
        },
        "exam": {
            "key": "exam",
            "label": "考试",
            "target_cny": COST_TARGET_CNY_EXAM,
            "attempts": 0,
            "cost_usd": 0.0,
        },
    }
    for attempt_id, cost in per_attempt.items():
        bucket = (
            buckets["practice"]
            if risk_by_attempt.get(attempt_id) in ("L0", "L1")
            else buckets["exam"]
        )
        bucket["attempts"] += 1
        bucket["cost_usd"] += cost
    targets = []
    for bucket in buckets.values():
        attempts = bucket["attempts"]
        avg_usd = round(bucket["cost_usd"] / attempts, 4) if attempts else None
        avg_cny = round(avg_usd * CNY_PER_USD, 2) if avg_usd is not None else None
        status = (
            "pass"
            if avg_cny is not None and avg_cny <= bucket["target_cny"]
            else "fail"
            if avg_cny is not None
            else "unmeasured"
        )
        targets.append(
            {
                "key": bucket["key"],
                "label": bucket["label"],
                "attempts": attempts,
                "total_usd": round(bucket["cost_usd"], 6),
                "avg_usd": avg_usd,
                "avg_cny": avg_cny,
                "target_cny": bucket["target_cny"],
                "status": status,
            }
        )

    per_attempt_usd = round(total / len(per_attempt), 4) if per_attempt else None
    return {
        "total_usd": round(total, 6),
        "priced_calls": priced_calls,
        "attempts": len(attempt_ids),
        "attempts_with_cost": len(per_attempt),
        "per_attempt_usd": per_attempt_usd,
        "cny_per_usd": CNY_PER_USD,
        "per_attempt_cny": (
            round(per_attempt_usd * CNY_PER_USD, 2) if per_attempt_usd is not None else None
        ),
        "targets": targets,
    }
