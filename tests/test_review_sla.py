"""复核队列 SLA：按风险级计算等待时长与超时升级。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.review_sla import SLA_HOURS, review_sla

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def test_l3_overdue_after_24_hours():
    result = review_sla(_ago(30), "L3", now=NOW)
    assert result["age_hours"] == 30.0
    assert result["sla_hours"] == SLA_HOURS["L3"] == 24
    assert result["remaining_hours"] == -6.0
    assert result["overdue"] is True


def test_l2_within_sla_is_not_overdue():
    result = review_sla(_ago(47), "L2", now=NOW)
    assert result["sla_hours"] == 48
    assert result["remaining_hours"] == 1.0
    assert result["overdue"] is False


def test_boundary_is_not_overdue_until_exceeded():
    result = review_sla(_ago(48), "L2", now=NOW)
    assert result["age_hours"] == 48.0
    assert result["overdue"] is False


def test_missing_or_invalid_timestamp_is_unmeasured():
    missing = review_sla(None, "L3")
    assert missing["age_hours"] is None
    assert missing["overdue"] is False
    invalid = review_sla("not-a-time", "L3")
    assert invalid["age_hours"] is None
    assert invalid["sla_hours"] == 24
    assert invalid["overdue"] is False


def test_unknown_risk_uses_default_sla_and_naive_time_is_utc():
    unknown = review_sla(_ago(49), "L9", now=NOW)
    assert unknown["sla_hours"] == 48
    assert unknown["overdue"] is True
    naive = review_sla("2026-11-23T10:00:00", "L3", now=NOW)
    assert naive["age_hours"] == 26.0
    assert naive["overdue"] is True
