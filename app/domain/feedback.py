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


def ensure_feedback_evidence(
    units: list[dict[str, Any]],
    *,
    media_id: str | None,
    duration_ms: int,
) -> list[dict[str, Any]]:
    """扣分项缺少时间戳时补全为“全段缺席”证据，保证反馈可回放且显式标注需人工确认。"""
    for unit in units:
        if unit.get("score") == 2:
            continue
        evidence = unit.get("evidence") or {}
        if evidence.get("start_ms") is not None and evidence.get("end_ms") is not None:
            continue
        unit["evidence"] = {
            "media_id": media_id,
            "start_ms": 0,
            "end_ms": max(0, int(duration_ms or 0)),
            "speaker": "system",
            "quote": "本场次未捕获与该项相关的可回放片段",
            "evidence_kind": "absence",
            "note": "系统补全的缺席证据：需人工确认整段录像后再判定。",
        }
        unit["evidence_repaired"] = True
    return units


def summarize_feedback(units: list[dict[str, Any]]) -> dict[str, Any]:
    checked = len(units)
    with_timestamp = sum(
        1 for unit in units if (unit.get("evidence") or {}).get("start_ms") is not None
    )
    with_alternative = sum(1 for unit in units if str(unit.get("alternative") or "").strip())
    repaired = sum(1 for unit in units if unit.get("alternative_repaired"))
    repaired_evidence = sum(1 for unit in units if unit.get("evidence_repaired"))
    return {
        "checked": checked,
        "with_timestamp": with_timestamp,
        "with_alternative": with_alternative,
        "repaired": repaired,
        "repaired_evidence": repaired_evidence,
        "all_have_alternative": checked == with_alternative,
        "rule": FEEDBACK_RULE,
    }
