"""反馈质量：替代话术兜底与证据时间戳覆盖统计。"""
from __future__ import annotations

from typing import Any

FEEDBACK_RULE = "每条扣分反馈必须包含可回放证据时间戳与可直接背诵的替代话术。"


def fallback_alternative(item: dict[str, Any]) -> str:
    anchor = item.get("anchor") or {}
    script = str(anchor.get("positive") or anchor.get("partial") or "").strip()
    if script:
        return f"可补一句：「{script}」"
    return "请按评分表行为锚完整复述一次，并确认患者理解。"


def ensure_feedback_units(
    units: list[dict[str, Any]], items: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """对缺失替代话术的反馈单元做确定性兜底，并标记已修复。"""
    for unit in units:
        if str(unit.get("alternative") or "").strip():
            continue
        unit["alternative"] = fallback_alternative(items.get(unit.get("item_id")) or {})
        unit["alternative_repaired"] = True
    return units


def summarize_feedback(units: list[dict[str, Any]]) -> dict[str, Any]:
    checked = len(units)
    with_timestamp = sum(
        1 for unit in units if (unit.get("evidence") or {}).get("start_ms") is not None
    )
    with_alternative = sum(1 for unit in units if str(unit.get("alternative") or "").strip())
    repaired = sum(1 for unit in units if unit.get("alternative_repaired"))
    return {
        "checked": checked,
        "with_timestamp": with_timestamp,
        "with_alternative": with_alternative,
        "repaired": repaired,
        "all_have_alternative": checked == with_alternative,
        "rule": FEEDBACK_RULE,
    }
