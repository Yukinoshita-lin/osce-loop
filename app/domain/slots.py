"""SP 对话引擎：确定性状态机负责“说什么”，生成器只负责“怎么说”。"""
from __future__ import annotations

import re
from typing import Any

from .rubric import PHASES, PHASE_RANK

EMPATHY_MARKERS = ("理解", "担心", "别急", "放心", "陪", "难受", "辛苦", "害怕")

PHASE_TRIGGERS = {
    "assessment": ("听诊", "查体", "检查一下", "量血压", "触诊", "叩诊", "体格检查"),
    "plan": ("心电图", "诊断", "怎么办", "怎么治", "处理", "计划", "住院", "转", "开药", "治疗"),
    "closing": ("总结", "清楚", "明白", "还有问题", "回去", "注意"),
}


def _contains_any(text: str, needles: tuple[str, ...] | list[str]) -> str | None:
    for needle in needles:
        if needle and needle in text:
            return needle
    return None


def scan_leaks(draft: str, unreleased_slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对未释放槽的关键实体做字符串扫描；这是信息边界的最后一道闸。"""
    violations: list[dict[str, Any]] = []
    for slot in unreleased_slots:
        if slot.get("disclosure") == "never":
            continue
        entities = [e for e in slot.get("entities", []) if e]
        hit = [e for e in entities if e in draft]
        if hit:
            violations.append(
                {
                    "slot_id": slot.get("slot_id"),
                    "entities": hit,
                    "kind": "information_boundary",
                }
            )
    forbidden_advice = ("你得了", "确诊为", "吃阿司匹林", "含服硝酸甘油", "不用去医院", "没事")
    for phrase in forbidden_advice:
        if phrase in draft:
            violations.append({"kind": "clinical_safety", "entities": [phrase]})
            break
    return violations


class DialogueStateMachine:
    """只做确定性状态维护，不具备任何生成能力，因此不可能幻觉。"""

    def __init__(self, bundle_payload: dict[str, Any], bundle_derived: dict[str, Any]):
        self.payload = bundle_payload
        self.slots = bundle_derived.get("slots", [])
        self.phase_order = bundle_derived.get("phase_order", list(PHASES))
        self.persona = bundle_payload.get("sp_profile", {})
        self.guidance = self._build_guidance(bundle_derived)

    def _build_guidance(self, derived: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for cp in derived.get("checkpoints", []):
            result[cp["phase"]] = "患者停顿片刻，似乎还有话想说。"
        return result

    def start(self) -> dict[str, Any]:
        released = [
            s["slot_id"] for s in self.slots if s.get("disclosure") == "active"
        ]
        return {
            "phase": "opening",
            "turn": 0,
            "released": released,
            "trust": 0,
            "intent_history": [],
            "transitioned": False,
            "degraded_count": 0,
        }

    def classify(self, text: str) -> dict[str, Any]:
        empathy = _contains_any(text, EMPATHY_MARKERS)
        phase_target = None
        for phase in ("closing", "plan", "assessment"):
            if _contains_any(text, PHASE_TRIGGERS[phase]):
                phase_target = phase
                break
        matched_slots = []
        for slot in self.slots:
            triggers = slot.get("triggers") or []
            if _contains_any(text, triggers):
                matched_slots.append(slot["slot_id"])
        return {
            "empathy": bool(empathy),
            "phase_target": phase_target,
            "matched_slots": matched_slots,
            "is_question": bool(re.search(r"[?？]|吗|呢|有没有|多久|哪里|怎么", text)),
        }

    def step_state(self, state: dict[str, Any], user_text: str) -> tuple[dict[str, Any], list[str], dict[str, Any], bool]:
        """返回新状态、本轮允许释放的槽、意图、是否发生阶段推进。"""
        new_state = {
            **state,
            "released": list(state.get("released", [])),
            "intent_history": list(state.get("intent_history", [])),
        }
        new_state["turn"] = int(state.get("turn", 0)) + 1
        intent = self.classify(user_text)
        new_state["intent_history"].append(
            {"turn": new_state["turn"], "phase_target": intent["phase_target"], "empathy": intent["empathy"]}
        )
        if intent["empathy"]:
            new_state["trust"] = int(state.get("trust", 0)) + 1

        # 开场后第一轮对话自动进入信息采集；其余阶段必须由考生行为触发
        if intent["phase_target"]:
            target_rank = PHASE_RANK[intent["phase_target"]]
            if target_rank > PHASE_RANK.get(new_state["phase"], 0):
                new_state["phase"] = intent["phase_target"]
                new_state["transitioned"] = True
        elif new_state["phase"] == "opening" and new_state["turn"] >= 1:
            new_state["phase"] = "information_gathering"

        released_now: list[str] = []
        current_rank = PHASE_RANK.get(new_state["phase"], 0)
        for slot in self.slots:
            slot_id = slot["slot_id"]
            if slot_id in new_state["released"]:
                continue
            if PHASE_RANK.get(slot.get("phase"), 99) > current_rank:
                continue
            disclosure = slot.get("disclosure", "on_direct_ask")
            if disclosure == "never":
                continue
            if disclosure == "active":
                new_state["released"].append(slot_id)
                released_now.append(slot_id)
            elif disclosure == "on_direct_ask" and slot_id in intent["matched_slots"]:
                new_state["released"].append(slot_id)
                released_now.append(slot_id)
            elif disclosure == "on_trust" and int(new_state.get("trust", 0)) >= 2:
                new_state["released"].append(slot_id)
                released_now.append(slot_id)

        return new_state, released_now, intent, bool(intent["phase_target"])

    def slots_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        idset = set(ids)
        return [s for s in self.slots if s["slot_id"] in idset]

    def unreleased(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        released = set(state.get("released", []))
        return [s for s in self.slots if s["slot_id"] not in released]

    def safe_template(self, phase: str) -> str:
        return self.guidance.get(phase, "患者看着你，等待你的下一句话。")


class SPController:
    """生成 → 独立校验 → 修正/回退，三步闭环。"""

    def __init__(self, machine: DialogueStateMachine):
        self.machine = machine

    def generate(self, released_slots: list[dict[str, Any]], persona: dict[str, Any], turn: int) -> str:
        if not released_slots:
            options = (
                "医生，我也说不太好……您再具体问问我？",
                "唉，就是不舒服，您看我这是怎么了？",
                "我这心里没底，您多问问吧。",
            )
            return options[turn % len(options)]
        contents = [str(s.get("content", "")).rstrip("。！？") for s in released_slots if s.get("content")]
        if not contents:
            return "医生，您问的这个……我记不太清了。"
        lead = "唉，医生，" if persona.get("persona", {}).get("tone") == "焦虑但克制" else "医生，"
        if len(contents) == 1:
            return f"{lead}{contents[0]}。"
        return f"{lead}{contents[0]}。还有，{contents[1]}。"

    def verify(self, draft: str, persona: dict[str, Any], unreleased: list[dict[str, Any]]) -> dict[str, Any]:
        boundary_violations = scan_leaks(draft, unreleased)
        boundary = 1.0
        if boundary_violations:
            boundary = 0.0
        persona_score = 0.96
        if persona.get("persona", {}).get("tone") == "焦虑但克制" and not any(
            marker in draft for marker in ("唉", "医生", "心里", "没底", "担心")
        ):
            persona_score = 0.72
        clinical = 1.0
        if any(v.get("kind") == "clinical_safety" for v in boundary_violations):
            clinical = 0.0
        total = round(persona_score * 0.35 + boundary * 0.50 + clinical * 0.15, 3)
        return {
            "persona": persona_score,
            "boundary": boundary,
            "clinical": clinical,
            "total": total,
            "violations": boundary_violations,
            "passed": total >= 0.85,
            "repairable": boundary > 0 and clinical > 0,
        }

    def respond(self, state: dict[str, Any], user_text: str) -> dict[str, Any]:
        new_state, released_now, intent, transitioned = self.machine.step_state(state, user_text)
        allowed = self.machine.slots_by_ids(new_state["released"])
        unreleased = self.machine.unreleased(new_state)
        draft = self.generate(allowed, self.machine.persona, new_state["turn"])

        verification = self.verify(draft, self.machine.persona, unreleased)
        attempts = 0
        degraded = False
        while not verification["passed"] and attempts < 2:
            attempts += 1
            if not verification["repairable"]:
                break
            draft = self.generate(allowed, self.machine.persona, new_state["turn"] + attempts)
            verification = self.verify(draft, self.machine.persona, unreleased)

        if not verification["passed"]:
            draft = self.machine.safe_template(new_state["phase"])
            degraded = True
            new_state["degraded_count"] = int(new_state.get("degraded_count", 0)) + 1
            verification = {
                **verification,
                "fallback": True,
            }

        return {
            "reply": draft,
            "state": new_state,
            "intent": intent,
            "released_now": released_now,
            "allowed_slots": allowed,
            "unreleased_count": len(unreleased),
            "verification": verification,
            "transitioned": transitioned,
            "degraded": degraded,
        }
