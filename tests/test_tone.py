"""反馈语气检查：打击性表述与不及格反馈的鼓励缺失。"""
from __future__ import annotations

from app.domain.tone import analyze_feedback_tone


def test_harsh_language_requires_review():
    result = analyze_feedback_tone(
        texts=["该生沟通能力不足，表现太差。"],
        final_total=55,
        pass_line=60,
    )
    assert result["level"] == "review"
    assert "能力不足" in result["harsh_terms"]
    assert "太差" in result["harsh_terms"]
    assert any("改写" in suggestion for suggestion in result["suggestions"])


def test_failing_feedback_without_encouragement_requires_review():
    result = analyze_feedback_tone(
        texts=["HX.01（腹痛部位）：未达成，对应行为锚：未询问部位。"],
        final_total=40,
        pass_line=60,
    )
    assert result["failing"] is True
    assert result["level"] == "review"
    assert any("鼓励" in suggestion for suggestion in result["suggestions"])


def test_failing_feedback_with_encouragement_passes():
    result = analyze_feedback_tone(
        texts=["HX.01（腹痛部位）：未达成。下次可以追加：“是哪个位置疼？”"],
        final_total=40,
        pass_line=60,
    )
    assert result["failing"] is True
    assert result["encouragement_terms"]
    assert result["level"] == "ok"


def test_passing_feedback_passes():
    result = analyze_feedback_tone(
        texts=["OP.01（自我介绍）：达成，对应行为锚：主动说明身份并核对姓名。"],
        final_total=82,
        pass_line=60,
    )
    assert result["level"] == "ok"
    assert result["failing"] is False
