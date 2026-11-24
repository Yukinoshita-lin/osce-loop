"""AI 成本归集：按场次还原评分与转写调用，给出单站成本基线。"""
from __future__ import annotations

from typing import Any

from .. import db


def ai_cost_summary(conn) -> dict[str, Any]:
    attempt_rows = conn.execute("SELECT attempt_id, media_id FROM attempts").fetchall()
    attempt_ids = {row["attempt_id"] for row in attempt_rows}
    media_to_attempt = {row["media_id"]: row["attempt_id"] for row in attempt_rows if row["media_id"]}

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

    return {
        "total_usd": round(total, 6),
        "priced_calls": priced_calls,
        "attempts": len(attempt_ids),
        "attempts_with_cost": len(per_attempt),
        "per_attempt_usd": round(total / len(per_attempt), 4) if per_attempt else None,
    }
