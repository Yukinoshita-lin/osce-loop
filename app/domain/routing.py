"""证据锚定的风险路由：七条规则按顺序求值，命中即停。"""
from __future__ import annotations

import hashlib
from typing import Any

from ..config import PASS_LINE_MARGIN, SAMPLE_RATES
from .rubric import AUTHORITY_ORDER


def sampling_hit(record_id: str, rate: float) -> bool:
    if rate <= 0:
        return False
    bucket = int(hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:8], 16) % 10000
    return bucket < int(rate * 10000)


def is_near_cutoff(total_score: float | None, pass_line: float, margin: float = PASS_LINE_MARGIN) -> bool:
    if total_score is None:
        return False
    return abs(float(total_score) - float(pass_line)) <= margin


def route_item(
    *,
    record_id: str,
    item: dict[str, Any],
    authority: str,
    ai_score: int | None,
    confidence: float | None,
    risk_level: str,
    release_stage: str,
    near_cutoff: bool = False,
    integrity_gap: bool = False,
    appeal: bool = False,
    item_disagreement_rate: float = 0.0,
) -> dict[str, Any]:
    floor = float(item.get("confidence_floor", 0.55))
    rate = SAMPLE_RATES.get(risk_level, 0.2)

    # 0. 无评分权限 / 仅提取证据：不产生 AI 分，直接进入人工评分
    if authority == "none" or ai_score is None:
        return _route("manual", "manual_only", requires_human=True, reason_text="该项 AI 无评分权限")
    if authority == "extract":
        return _route("manual", "extract_only", requires_human=True, reason_text="AI 仅提取证据，分数由人给")

    # 1. 证据链不完整：全站人工
    if integrity_gap:
        return _route("full_station_human", "integrity_gap", requires_human=True)

    # 2. 关键项 AI 判阴性：双人复核
    if item.get("critical") and ai_score == 0:
        return _route("dual_review", "critical_negative", requires_human=True)

    # 3. 边缘案例：高年资裁决
    if near_cutoff:
        return _route("senior_arbitration", "near_cutoff", requires_human=True)

    # 4. 置信度低于项阈值
    if confidence is not None and confidence < floor:
        return _route("send_review", "confidence_below_floor", requires_human=True)

    # 5. 该项历史人机分歧率过高
    if item_disagreement_rate > 0.15:
        return _route("send_review", "item_disagreement", requires_human=True)

    # 6. 考生申诉：全量重评
    if appeal:
        return _route("full_reevaluation", "appeal", requires_human=True)

    # 7. 抽检；L3 覆盖全部关键项
    must_cover_critical = risk_level == "L3" and bool(item.get("critical"))
    if sampling_hit(record_id, rate) or must_cover_critical:
        return _route(
            "sampling",
            "sampling",
            requires_human=True,
            detail={"sample_rate": rate, "critical_coverage": must_cover_critical},
        )

    return _route("auto", None, requires_human=False, detail={"sample_rate": rate})


def _route(
    route: str,
    reason: str | None,
    *,
    requires_human: bool,
    reason_text: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "requires_human": requires_human,
        "route": route,
        "reason": reason,
        "reasons": [reason] if reason else [],
        "reason_text": reason_text,
        "detail": detail or {},
    }


def apply_near_cutoff(records: list[dict[str, Any]], pass_line: float) -> bool:
    """对已路由的记录做尝试级边缘案例覆盖。"""
    near = is_near_cutoff(
        _weighted_total(records),
        pass_line,
    )
    if near:
        for rec in records:
            if rec.get("routing", {}).get("reason") in (None, "sampling"):
                rec["routing"] = _route(
                    "senior_arbitration",
                    "near_cutoff",
                    requires_human=True,
                    reason_text="总分距及格线 ±5% 以内",
                )
    return near


def _weighted_total(records: list[dict[str, Any]]) -> float | None:
    total_weight = 0.0
    earned = 0.0
    for rec in records:
        score = rec.get("ai_score")
        if score is None:
            continue
        weight = float(rec.get("weight", 0))
        total_weight += weight
        earned += weight * (float(score) / 2.0)
    if total_weight <= 0:
        return None
    return round(earned / total_weight * 100, 2)


def compute_ai_total(rubric: list[dict[str, Any]], ai_scores: dict[str, int | None]) -> tuple[float | None, int, int]:
    """返回 AI 暂定总分、已评分权重、总权重。未评项不计入，避免用 0 冒充结论。"""
    earned = 0.0
    scored_weight = 0.0
    total_weight = 0.0
    for item in rubric:
        weight = float(item.get("weight", 0))
        total_weight += weight
        score = ai_scores.get(item.get("item_id"))
        if score is None:
            continue
        scored_weight += weight
        earned += weight * (float(score) / 2.0)
    if scored_weight <= 0:
        return None, 0, int(total_weight)
    return round(earned / scored_weight * 100, 2), int(scored_weight), int(total_weight)
