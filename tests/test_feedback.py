"""反馈质量：替代话术兜底与时间戳统计。"""
from __future__ import annotations

from app.domain.feedback import (
    ensure_feedback_units,
    fallback_alternative,
    summarize_feedback,
)


def test_fallback_alternative_prefers_positive_anchor():
    text = fallback_alternative({"anchor": {"positive": "询问胸痛的部位与性质"}})
    assert "询问胸痛的部位与性质" in text
    assert text.startswith("可补一句")


def test_fallback_alternative_without_anchor_is_still_actionable():
    text = fallback_alternative({})
    assert "完整复述" in text


def test_ensure_feedback_units_repairs_only_missing_alternatives():
    units = [
        {"item_id": "A", "alternative": ""},
        {"item_id": "B", "alternative": "可直接背诵的话术"},
    ]
    ensure_feedback_units(units, {"A": {"anchor": {"positive": "询问胸痛部位"}}})
    assert units[0]["alternative_repaired"] is True
    assert "询问胸痛部位" in units[0]["alternative"]
    assert "alternative_repaired" not in units[1]


def test_summarize_feedback_counts_timestamps_and_alternatives():
    summary = summarize_feedback(
        [
            {"alternative": "x", "evidence": {"start_ms": 1200}},
            {"alternative": "", "evidence": {}},
            {"alternative": "y", "evidence": {"start_ms": 0}},
        ]
    )
    assert summary["checked"] == 3
    assert summary["with_timestamp"] == 2
    assert summary["with_alternative"] == 2
    assert summary["all_have_alternative"] is False
    assert summary["repaired"] == 0
