"""S2 行为锚改写建议：把不可观测表述转成可执行改写。"""
from __future__ import annotations

import copy

from app.domain.rewrite import PRINCIPLE, suggest_anchor_rewrites
from app.seed import build_bundle_payload


def test_valid_bundle_has_no_rewrites():
    assert suggest_anchor_rewrites(build_bundle_payload()) == []


def test_vague_anchors_get_targeted_suggestions():
    payload = copy.deepcopy(build_bundle_payload())
    item = payload["rubric"][0]
    item["anchor"]["observable"] = "具备良好的沟通能力与人文关怀"
    item["anchor"]["negative"] = ""
    item2 = payload["rubric"][1]
    item2["anchor"]["partial"] = "掌握沟通要点"

    rewrites = suggest_anchor_rewrites(payload)
    by_key = {(entry["item_id"], entry["field"]): entry for entry in rewrites}
    observable = by_key[(item["item_id"], "observable")]
    assert "不可判定" in observable["reason"]
    assert "回应患者的情绪" in observable["suggestion"]
    empty = by_key[(item["item_id"], "negative")]
    assert "为空" in empty["reason"]
    assert "补充一条可观察行为" in empty["suggestion"]
    knowledge = by_key[(item2["item_id"], "partial")]
    assert "说出" in knowledge["suggestion"]
    for entry in rewrites:
        assert entry["suggestion"]
        assert entry["rationale"]
    assert "可被判定的动词" in PRINCIPLE


def test_rewrites_do_not_mutate_payload():
    payload = copy.deepcopy(build_bundle_payload())
    item = payload["rubric"][0]
    item["anchor"]["observable"] = "表现出良好的职业态度"
    before = copy.deepcopy(payload)
    suggest_anchor_rewrites(payload)
    assert payload == before
