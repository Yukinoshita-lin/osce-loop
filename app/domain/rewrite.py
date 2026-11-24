"""S2 行为锚改写辅助：把不可判定的表述转成可观察建议。"""
from __future__ import annotations

from typing import Any

from .rubric import ANCHOR_FIELDS, is_observable

PRINCIPLE = (
    "行为锚必须包含可被判定的动词与可听/可见的对象；"
    "禁止“理解/具备/表现出良好…”这类无法形成一致判断的表述。"
)


def _suggest(text: str, reason: str) -> tuple[str, str]:
    value = str(text or "")
    if not value.strip():
        return (
            "补充一条可观察行为，至少包含动作与对象，例如：询问胸痛的部位与性质",
            "空锚会让评分员各自理解，必须在冻结前补齐。",
        )
    if any(key in value for key in ("同理", "共情", "人文", "关怀", "尊重")):
        return (
            "回应患者的情绪（如“我理解您很担心”），并说明下一步安排",
            "把态度表述落到可听见的回应与信息告知上。",
        )
    if any(key in value for key in ("掌握", "熟悉", "了解", "知识", "理论")):
        return (
            "说出（具体要点），并列出（具体条目）",
            "知识类锚必须落到可复述的具体内容。",
        )
    if any(key in value for key in ("沟通", "表达", "交流", "解释")):
        return (
            "向患者说明检查目的，并确认患者理解",
            "沟通类锚需要包含说明对象与确认动作。",
        )
    if any(key in value for key in ("具备", "意识", "态度", "责任", "素养")):
        return (
            "在（具体场景）完成（具体动作），并核对（对象）",
            "素养类表述需转换为场景中的具体行为。",
        )
    if "缺少可被判定的动词" in reason:
        return (
            "改用可判定动词重写，例如：询问…… / 说明…… / 确认…… / 完成……",
            "当前句子没有评分员能共同观察的动作。",
        )
    return (
        "删除不可判定表述，改为“可判定动词 + 可听/可见对象”",
        "保留原意，但把判断依据落到具体行为。",
    )


def suggest_anchor_rewrites(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """对每个不可观测的行为锚给出改写建议；不改动原 payload。"""
    rewrites: list[dict[str, Any]] = []
    for item in payload.get("rubric") or []:
        anchor = item.get("anchor") or {}
        for field in ANCHOR_FIELDS:
            value = anchor.get(field)
            ok, reason = is_observable(value)
            if ok:
                continue
            suggestion, rationale = _suggest(value, reason)
            rewrites.append(
                {
                    "item_id": item.get("item_id"),
                    "item_title": item.get("title"),
                    "field": field,
                    "current": value,
                    "reason": reason,
                    "suggestion": suggestion,
                    "rationale": rationale,
                }
            )
    return rewrites
