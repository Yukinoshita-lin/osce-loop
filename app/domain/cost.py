"""单站成本归集：AI/ASR 调用按场次还原，媒体按留存期摊销，给出练习/考试目标核对。"""
from __future__ import annotations

from typing import Any

from .. import db
from ..config import (
    CNY_PER_USD,
    COST_TARGET_CNY_EXAM,
    COST_TARGET_CNY_PRACTICE,
    STORAGE_PRICE_CNY_PER_GB_MONTH,
)
from .retention import retention_plan


def storage_cost_cny(
    media_size_bytes: int | None,
    risk_level: str | None,
    created_at: str | None,
) -> float:
    """按留存期把媒体存储折算为一次性摊销成本（人民币）。

    设计口径：单站成本含 ASR、模型调用与存储摊销。仅在媒体尚未清理时计入，
    避免已释放的存储继续抬高历史成本。
    """
    size = int(media_size_bytes or 0)
    if size <= 0 or STORAGE_PRICE_CNY_PER_GB_MONTH <= 0:
        return 0.0
    plan = retention_plan(risk_level or "L2", created_at or db.now_iso())
    months = float(plan["retention_days"]) / 30.0
    return round(size / (1024**3) * STORAGE_PRICE_CNY_PER_GB_MONTH * months, 6)


def ai_cost_summary(conn) -> dict[str, Any]:
    attempt_rows = conn.execute(
        "SELECT attempt_id, media_id, risk_level, created_at, media_size_bytes, media_purged_at "
        "FROM attempts"
    ).fetchall()
    attempt_ids = {row["attempt_id"] for row in attempt_rows}
    media_to_attempt = {
        row["media_id"]: row["attempt_id"] for row in attempt_rows if row["media_id"]
    }
    risk_by_attempt = {row["attempt_id"]: row["risk_level"] for row in attempt_rows}
    storage_by_attempt = {
        row["attempt_id"]: storage_cost_cny(
            row["media_size_bytes"], row["risk_level"], row["created_at"]
        )
        for row in attempt_rows
        if not row["media_purged_at"]
    }

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
            "ai_attempts": 0,
            "ai_cny": 0.0,
            "storage_cny": 0.0,
        },
        "exam": {
            "key": "exam",
            "label": "考试",
            "target_cny": COST_TARGET_CNY_EXAM,
            "attempts": 0,
            "ai_attempts": 0,
            "ai_cny": 0.0,
            "storage_cny": 0.0,
        },
    }
    cost_attempts = set(per_attempt) | {
        attempt_id for attempt_id, cost in storage_by_attempt.items() if cost > 0
    }
    for attempt_id in cost_attempts:
        bucket = (
            buckets["practice"]
            if risk_by_attempt.get(attempt_id) in ("L0", "L1")
            else buckets["exam"]
        )
        bucket["attempts"] += 1
        if attempt_id in per_attempt:
            bucket["ai_attempts"] += 1
            bucket["ai_cny"] += per_attempt[attempt_id] * CNY_PER_USD
        bucket["storage_cny"] += storage_by_attempt.get(attempt_id, 0.0)

    targets = []
    for bucket in buckets.values():
        attempts = bucket["attempts"]
        ai_cny = round(bucket["ai_cny"], 6)
        storage_cny = round(bucket["storage_cny"], 6)
        total_cny = round(ai_cny + storage_cny, 6)
        avg_usd = round(ai_cny / CNY_PER_USD / bucket["ai_attempts"], 4) if bucket["ai_attempts"] else None
        avg_cny = round(total_cny / attempts, 2) if attempts else None
        if not attempts:
            status = "unmeasured"
        elif not bucket["ai_attempts"]:
            status = "warn"  # 仅有存储摊销，AI/ASR 尚未计价，不能视为达标
        elif avg_cny is not None and avg_cny <= bucket["target_cny"]:
            status = "pass"
        else:
            status = "fail"
        targets.append(
            {
                "key": bucket["key"],
                "label": bucket["label"],
                "attempts": attempts,
                "ai_attempts": bucket["ai_attempts"],
                "total_cny": round(total_cny, 2),
                "ai_cny": round(ai_cny, 2),
                "storage_cny": round(storage_cny, 2),
                "avg_usd": avg_usd,
                "avg_cny": avg_cny,
                "avg_storage_cny": round(storage_cny / attempts, 2) if attempts else None,
                "target_cny": bucket["target_cny"],
                "status": status,
            }
        )

    total_storage_cny = round(sum(storage_by_attempt.values()), 6)
    total_ai_cny = round(total * CNY_PER_USD, 6)
    combined_cny = round(total_ai_cny + total_storage_cny, 2)
    per_attempt_usd = round(total / len(per_attempt), 4) if per_attempt else None
    return {
        "total_usd": round(total, 6),
        "total_ai_cny": round(total_ai_cny, 2),
        "total_storage_cny": round(total_storage_cny, 2),
        "total_cny": combined_cny,
        "priced_calls": priced_calls,
        "attempts": len(attempt_ids),
        "attempts_with_cost": len(cost_attempts),
        "per_attempt_usd": per_attempt_usd,
        "cny_per_usd": CNY_PER_USD,
        "storage_price_cny_per_gb_month": STORAGE_PRICE_CNY_PER_GB_MONTH,
        "per_attempt_cny": (
            round(combined_cny / len(cost_attempts), 2) if cost_attempts else None
        ),
        "per_attempt_storage_cny": (
            round(total_storage_cny / len(cost_attempts), 2) if cost_attempts else None
        ),
        "targets": targets,
    }
