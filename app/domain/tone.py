"""反馈语气检查：识别打击性表述与不及格反馈缺少鼓励。"""
from __future__ import annotations

from typing import Any

HARSH_TERMS = (
    "不合格",
    "太差",
    "差劲",
    "不适合",
    "能力不足",
    "态度不端",
    "无可救药",
    "糟糕",
    "失败",
)
ENCOURAGEMENT_TERMS = (
    "继续",
    "下次",
    "可以",
    "建议",
    "进步",
    "已经",
    "再试",
)


def analyze_feedback_tone(
    *,
    texts: list[str],
    final_total: float | None,
    pass_line: float | None,
) -> dict[str, Any]:
    joined = " ".join(str(text or "") for text in texts)
    harsh = [term for term in HARSH_TERMS if term in joined]
    encouragement = [term for term in ENCOURAGEMENT_TERMS if term in joined]
    failing = (
        final_total is not None
        and pass_line is not None
        and float(final_total) < float(pass_line)
    )
    suggestions: list[str] = []
    if harsh:
        suggestions.append(
            "把打击性结论改写为具体行为描述与可执行替代话术，例如“本次未询问放射部位，下次可追加一句…”。"
        )
    if failing and not encouragement:
        suggestions.append(
            "不及格反馈需要人工补一段鼓励，指出已经做到的具体行为，并给出下一步练习目标。"
        )
    if not suggestions:
        suggestions.append("语气检查通过；仍需人工确认是否存在隐性贬损或过度空泛的表述。")
    level = "review" if harsh or (failing and not encouragement) else "ok"
    return {
        "level": level,
        "harsh_terms": harsh,
        "encouragement_terms": encouragement,
        "failing": failing,
        "suggestions": suggestions,
        "note": "系统只做粗筛，最终语气与鼓励段落由审核人负责。",
    }
