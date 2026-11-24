"""反馈语言可读性初筛：句长、术语密度与长句占比。"""
from __future__ import annotations

import re
from typing import Any

TERMS = (
    "鉴别诊断",
    "生命体征",
    "危险信号",
    "压榨样",
    "放射",
    "适应证",
    "禁忌证",
    "并发症",
    "预后",
    "查体",
    "听诊",
    "触诊",
    "叩诊",
    "病理",
    "循证",
    "给药",
    "剂量",
    "干预",
)
LEVEL_LABELS = {"easy": "易懂", "medium": "一般", "hard": "偏难", "unmeasured": "未采集"}


def analyze_feedback_readability(texts: list[str]) -> dict[str, Any]:
    content = " ".join(str(text or "").strip() for text in texts if str(text or "").strip())
    if not content:
        return {
            "level": "unmeasured",
            "level_label": LEVEL_LABELS["unmeasured"],
            "chars": 0,
            "sentences": 0,
            "avg_sentence_chars": None,
            "long_sentence_ratio": None,
            "term_density": None,
            "term_hits": 0,
            "suggestions": [],
            "note": "暂无可分析的反馈文本。",
        }
    sentences = [part.strip() for part in re.split(r"[。！？!?；;\n]+", content) if part.strip()]
    compact = re.sub(r"\s+", "", content)
    total_chars = len(compact)
    sentence_count = max(1, len(sentences))
    avg_sentence = round(total_chars / sentence_count, 1)
    long_sentences = sum(1 for sentence in sentences if len(re.sub(r"\s+", "", sentence)) >= 40)
    long_ratio = round(long_sentences / sentence_count, 3)
    term_hits = sum(content.count(term) for term in TERMS)
    term_density = round(term_hits / total_chars * 100, 2) if total_chars else 0.0

    if avg_sentence > 40 or long_ratio > 0.35 or term_density > 3:
        level = "hard"
    elif avg_sentence <= 25 and long_ratio <= 0.2 and term_density <= 1.5:
        level = "easy"
    else:
        level = "medium"

    suggestions = []
    if avg_sentence > 30:
        suggestions.append("把长句拆成 1–2 个短句，先说结论再解释。")
    if long_ratio > 0.25:
        suggestions.append("减少单句信息量，避免一句话包含多个要求。")
    if term_density > 2:
        suggestions.append("首次出现术语时补一句白话解释。")
    note = {
        "easy": "反馈语言简短、术语密度低，适合学生自行阅读。",
        "medium": "反馈语言基本可读，仍有少量长句或术语。",
        "hard": "反馈句子偏长或术语偏多，建议改写后再面向学生发布。",
    }[level]
    if level == "hard":
        reasons = []
        if avg_sentence > 40 or long_ratio > 0.35:
            reasons.append("句子偏长")
        if term_density > 3:
            reasons.append("术语偏多")
        if reasons:
            note = f"反馈{'、'.join(reasons)}，建议改写后再面向学生发布。"
    return {
        "level": level,
        "level_label": LEVEL_LABELS[level],
        "chars": total_chars,
        "sentences": sentence_count,
        "avg_sentence_chars": avg_sentence,
        "long_sentence_ratio": long_ratio,
        "term_density": term_density,
        "term_hits": term_hits,
        "suggestions": suggestions,
        "note": note,
    }
