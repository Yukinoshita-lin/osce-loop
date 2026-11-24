"""反馈确认时延：从成绩发布到学生确认阅读 AI 参与说明的时间。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

TARGET_HOURS = 72


def _hours_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, (end_dt - start_dt).total_seconds() / 3600), 2)


def feedback_ack_latency(conn) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT a.attempt_id, a.candidate_code, a.published_at, a.signed_off_at, a.created_at, "
        "MIN(k.created_at) AS ack_at "
        "FROM attempts a JOIN ai_disclosure_acks k ON k.attempt_id = a.attempt_id "
        "GROUP BY a.attempt_id ORDER BY ack_at DESC"
    ).fetchall()
    samples = []
    values = []
    for row in rows:
        base = row["published_at"] or row["signed_off_at"] or row["created_at"]
        hours = _hours_between(base, row["ack_at"])
        if hours is None:
            continue
        values.append(hours)
        samples.append(
            {
                "attempt_id": row["attempt_id"],
                "candidate_code": row["candidate_code"],
                "hours": hours,
                "ack_at": row["ack_at"],
            }
        )
    count = len(values)
    within = sum(1 for value in values if value <= TARGET_HOURS)
    if not count:
        status = "unmeasured"
        note = "尚无学生确认阅读记录。"
    elif within / count >= 0.8:
        status = "pass"
        note = f"{count} 份确认中 {within} 份在 {TARGET_HOURS} 小时内完成。"
    else:
        status = "fail"
        note = f"{count} 份确认中仅 {within} 份在 {TARGET_HOURS} 小时内完成。"
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))] if ordered else None
    return {
        "status": status,
        "count": count,
        "target_hours": TARGET_HOURS,
        "avg_hours": round(sum(values) / count, 2) if count else None,
        "p90_hours": p90,
        "within_target": within,
        "within_rate": round(within / count, 3) if count else None,
        "note": note,
        "samples": samples[:5],
    }
