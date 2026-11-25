"""反馈可读性初筛：句长、长句占比与术语密度。"""
from __future__ import annotations

from app.domain.readability import analyze_feedback_readability, simplify_feedback_text


def test_readable_feedback_is_easy():
    result = analyze_feedback_readability(
        ["你问得很好。下次再问一句疼不疼。这样患者更容易回答。"]
    )
    assert result["level"] == "easy"
    assert result["avg_sentence_chars"] <= 25
    assert result["suggestions"] == []


def test_long_technical_feedback_is_hard():
    sentence = "鉴别诊断生命体征危险信号压榨样放射" * 6
    result = analyze_feedback_readability([sentence])
    assert result["level"] == "hard"
    assert result["avg_sentence_chars"] > 40
    assert result["term_density"] > 3
    assert result["suggestions"]


def test_medium_feedback_is_flagged():
    text = "你在信息采集阶段询问了疼痛持续时间但没有继续追问放射部位和伴随症状请下次注意补充完整。"
    result = analyze_feedback_readability([text])
    assert result["level"] in ("medium", "hard")


def test_empty_feedback_is_unmeasured():
    result = analyze_feedback_readability([])
    assert result["level"] == "unmeasured"
    assert result["avg_sentence_chars"] is None


def test_plain_language_replaces_terms_and_lowers_density():
    source = (
        "鉴别诊断需要结合生命体征与危险信号，同时关注压榨样胸痛是否放射到左臂，"
        "必要时完善心电图并评估并发症与预后，再决定后续干预与给药剂量。"
    )
    simplified = simplify_feedback_text(source)
    assert "鉴别诊断" not in simplified["text"]
    assert "血压、脉搏、呼吸、体温" in simplified["text"]
    assert any(entry["term"] == "鉴别诊断" for entry in simplified["glossary"])
    assert simplified["text"].count("。") >= 2
    before = analyze_feedback_readability([source])
    after = analyze_feedback_readability([simplified["text"]])
    assert after["term_density"] < before["term_density"]


def test_plain_language_empty_text_is_safe():
    result = simplify_feedback_text("")
    assert result == {"text": "", "glossary": []}
