"""S6 复核队列 SLA：按风险级计算等待时长，超时项目优先升级处理。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SLA_HOURS = {"L0": 72, "L1": 72, "L2": 48, "L3": 24}
DEFAULT_SLA_HOURS = 48


def review_sla(
    created_at: str | None,
    risk_level: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回等待时长与超时标记；时间无法解析时视为未测量，不做超时升级。"""
    sla = SLA_HOURS.get(risk_level or "", DEFAULT_SLA_HOURS)
    if not created_at:
        return {
            "age_hours": None,
            "sla_hours": sla,
            "remaining_hours": None,
            "overdue": False,
        }
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return {
            "age_hours": None,
            "sla_hours": sla,
            "remaining_hours": None,
            "overdue": False,
        }
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age_hours = round((current - created).total_seconds() / 3600, 1)
    age_hours = max(0.0, age_hours)
    return {
        "age_hours": age_hours,
        "sla_hours": sla,
        "remaining_hours": round(sla - age_hours, 1),
        "overdue": age_hours > sla,
    }
