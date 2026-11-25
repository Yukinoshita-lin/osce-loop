"""SP 对话引擎：确定性状态机负责“说什么”，生成器只负责“怎么说”。"""
from __future__ import annotations

import re
from typing import Any

from .rubric import PHASES, PHASE_RANK

EMPATHY_MARKERS = ("理解", "担心", "别急", "放心", "陪", "难受", "辛苦", "害怕")
PRESSURE_MARKERS = (
    "快点",
    "赶紧",
    "别废话",
    "你到底",
    "必须",
    "马上",
    "说了多少遍",
    "少啰嗦",
)
DISTRESS_LINES = (
    "医生，您这么急，我心里更慌了……能不能慢一点问？",
    "您别这么大声，我有点害怕，您慢慢说。",
    "我……我一下记不清了，您让我缓一缓。",
)
CONFUSED_LINES = (
    "医生，您刚才问什么？我这一阵一阵的，脑子有点乱……",
    "啊？您说的是……我记混了，早上还是昨天晚上来着？",
    "您别急，我这脑子转不过来，您再说一遍行吗？",
)
MEMORY_PROMPT_LINES = (
    "这个……我记不太清了，您提醒提醒我？",
    "您这么一问，我一时想不起来，能不能说得具体点？",
)
CLARIFY_LINES = (
    "医生，您说的那个词我不太明白，您能说得简单点吗？",
    "啥意思？我听不太懂，您换个说法吧。",
)
MEDICAL_JARGON = (
    "放射",
    "既往史",
    "过敏史",
    "伴随症状",
    "负荷试验",
    "抗凝",
    "生命体征",
    "梗死",
)
PARTIAL_MEMORY = ("部分模糊", "部分记忆模糊")
NEEDS_PROMPT_MEMORY = ("需提示才能回忆", "需要提示才能回忆")
GUIDANCE_LINES = {
    "opening": "医生，我还有点担心……您还有什么要问我的吗？",
    "information_gathering": "医生，我还有点担心，有些难受的地方好像还没说清楚……",
    "assessment": "医生，检查的时候您轻一点，我有点紧张……",
    "plan": "医生，那我回去以后要注意什么？",
}

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
    forbidden_patterns = (
        r"你得了",
        r"确诊为",
        r"应该是.{0,6}病",
        r"就是.{0,4}病",
        r"你吃点",
        r"应该吃",
        r"可以吃.{0,2}药",
        r"吃点.{0,3}药",
        r"吃阿司匹林",
        r"含服硝酸甘油",
        r"打一针",
        r"剂量",
        r"不用去医院",
        r"没事",
        r"你不行",
        r"不合格",
        r"太差",
        r"水平不行",
        r"不会看病",
        r"问得不好",
    )
    for pattern in forbidden_patterns:
        match = re.search(pattern, draft)
        if match:
            violations.append({"kind": "clinical_safety", "entities": [match.group(0)]})
            break
    return violations


class DialogueStateMachine:
    """只做确定性状态维护，不具备任何生成能力，因此不可能幻觉。"""

    def __init__(self, bundle_payload: dict[str, Any], bundle_derived: dict[str, Any]):
        self.payload = bundle_payload
        self.slots = bundle_derived.get("slots", [])
        self.phase_order = bundle_derived.get("phase_order", list(PHASES))
        self.persona = bundle_payload.get("sp_profile", {})
        self.checkpoints = {
            checkpoint.get("phase"): checkpoint
            for checkpoint in bundle_derived.get("checkpoints", [])
            if checkpoint.get("phase")
        }
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
            "emotion_load": 0.0,
            "ask_counts": {},
            "intent_history": [],
            "transitioned": False,
            "degraded_count": 0,
            "guided_phases": [],
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
            "ask_counts": dict(state.get("ask_counts") or {}),
            "intent_history": list(state.get("intent_history", [])),
            "guided_phases": list(state.get("guided_phases") or []),
            "transitioned": False,
        }
        new_state["turn"] = int(state.get("turn", 0)) + 1
        intent = self.classify(user_text)
        for slot_id in intent["matched_slots"]:
            new_state["ask_counts"][slot_id] = int(new_state["ask_counts"].get(slot_id, 0)) + 1
        new_state["intent_history"].append(
            {"turn": new_state["turn"], "phase_target": intent["phase_target"], "empathy": intent["empathy"]}
        )
        if intent["empathy"]:
            new_state["trust"] = int(state.get("trust", 0)) + 1

        # 情绪唤起：压力性提问累积，共情回落；超过人格卡阈值时暂停释放新信息
        persona = self.persona.get("persona") if isinstance(self.persona.get("persona"), dict) else {}
        try:
            threshold = float(persona.get("emotion_threshold", 1.0))
        except (TypeError, ValueError):
            threshold = 1.0
        threshold = min(1.0, max(0.0, threshold))
        try:
            cognition = float(persona.get("cognition", 0) or 0)
        except (TypeError, ValueError):
            cognition = 0.0
        cognition = min(1.0, max(0.0, cognition))
        memory = str(persona.get("memory") or "清晰")
        language = str(persona.get("language") or "普通话清晰")
        needs_prompt = memory in NEEDS_PROMPT_MEMORY
        confused = cognition >= 0.5 and new_state["turn"] % 3 == 0
        clarification_needed = language == "医学词汇不理解" and any(
            term in user_text for term in MEDICAL_JARGON
        )
        load = float(state.get("emotion_load") or 0.0)
        exclamations = user_text.count("！") + user_text.count("!")
        pressured = bool(_contains_any(user_text, PRESSURE_MARKERS)) or exclamations >= 2
        if intent["empathy"]:
            load = max(0.0, load - 0.2)
        elif pressured:
            load = min(1.0, load + 0.3)
        else:
            load = min(1.0, load + (0.08 if intent["is_question"] else 0.04))
        distressed = threshold < 1.0 and load >= threshold
        if distressed:
            load = threshold * 0.5
        new_state["emotion_load"] = round(load, 3)
        intent["distressed"] = distressed
        intent["confused"] = confused
        intent["clarification_needed"] = clarification_needed
        intent["emotion_threshold"] = threshold
        intent["emotion_load"] = new_state["emotion_load"]

        # 开场后第一轮对话自动进入信息采集；其余阶段必须由考生行为触发
        if not (distressed or confused or clarification_needed):
            if intent["phase_target"]:
                target_rank = PHASE_RANK[intent["phase_target"]]
                current_phase = new_state["phase"]
                if target_rank > PHASE_RANK.get(current_phase, 0):
                    missing = self._missing_must_probe(new_state, current_phase)
                    if missing and current_phase not in new_state["guided_phases"]:
                        new_state["guided_phases"].append(current_phase)
                        intent["guidance"] = True
                        intent["guidance_text"] = GUIDANCE_LINES.get(
                            current_phase, "医生，我还有点担心……"
                        )
                        intent["missing_items"] = missing
                        intent["transition_blocked"] = True
                    else:
                        new_state["phase"] = intent["phase_target"]
                        new_state["transitioned"] = True
            elif new_state["phase"] == "opening" and new_state["turn"] >= 1:
                new_state["phase"] = "information_gathering"

        released_now: list[str] = []
        withheld: list[str] = []
        current_rank = PHASE_RANK.get(new_state["phase"], 0)
        blocked = distressed or confused or clarification_needed
        for slot in self.slots:
            if blocked:
                break
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
                if needs_prompt and int(new_state["ask_counts"].get(slot_id, 0)) < 2:
                    withheld.append(slot_id)
                    continue
                new_state["released"].append(slot_id)
                released_now.append(slot_id)
            elif disclosure == "on_trust" and int(new_state.get("trust", 0)) >= 2:
                new_state["released"].append(slot_id)
                released_now.append(slot_id)

        intent["withheld"] = withheld
        return new_state, released_now, intent, bool(new_state.get("transitioned"))

    def _missing_must_probe(self, state: dict[str, Any], phase: str) -> list[str]:
        """阶段末检查：该阶段的关键评分项是否已被问及，未问及则返回项 ID。"""
        checkpoint = self.checkpoints.get(phase) or {}
        must_probe = checkpoint.get("must_probe") or []
        if not must_probe:
            return []
        released = set(state.get("released") or [])
        asked = state.get("ask_counts") or {}
        missing: list[str] = []
        for item_id in must_probe:
            linked = [
                slot for slot in self.slots if item_id in (slot.get("linked_items") or [])
            ]
            if not linked:
                continue
            covered = any(
                slot["slot_id"] in released or int(asked.get(slot["slot_id"], 0)) > 0
                for slot in linked
            )
            if not covered:
                missing.append(item_id)
        return missing

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

    def generate(
        self,
        released_slots: list[dict[str, Any]],
        persona: dict[str, Any],
        turn: int,
        *,
        focus_ids: list[str] | None = None,
        empathy: bool = False,
    ) -> str:
        if focus_ids is not None:
            focused = [s for s in released_slots if s["slot_id"] in set(focus_ids)]
            if focused:
                released_slots = focused
            else:
                if empathy:
                    options = (
                        "唉，谢谢您……听您这么说，我心里踏实一点。",
                        "您这么说，我就不那么慌了。",
                        "大夫，您态度真好，我配合您。",
                    )
                    return options[turn % len(options)]
                options = (
                    "医生，您问得具体点，我有点糊涂……",
                    "唉，就是不舒服，您看我这是怎么了？",
                    "我这心里没底，您多问问吧。",
                )
                return options[turn % len(options)]
        if not released_slots:
            if empathy:
                options = (
                    "唉，谢谢您……听您这么说，我心里踏实一点。",
                    "您这么说，我就不那么慌了。",
                    "大夫，您态度真好，我配合您。",
                )
                return options[turn % len(options)]
            options = (
                "医生，这个我刚才说过了……您还想问啥？",
                "唉，就是不舒服，您看我这是怎么了？",
                "我这心里没底，您多问问吧。",
            )
            return options[turn % len(options)]
        contents = [str(s.get("content", "")).rstrip("。！？") for s in released_slots if s.get("content")]
        if not contents:
            return "医生，您问的这个……我记不太清了。"
        lead = "唉，医生，" if persona.get("persona", {}).get("tone") == "焦虑但克制" else "医生，"
        memory = ""
        if isinstance(persona.get("persona"), dict):
            memory = str(persona["persona"].get("memory") or "")
        hedged = memory in PARTIAL_MEMORY or ("部分" in memory and "模糊" in memory)
        if len(contents) == 1:
            core = f"{contents[0]}。"
        else:
            core = f"{contents[0]}。还有，{contents[1]}。"
        if hedged:
            core = "好像是……" + core.rstrip("。") + "，具体的我记不太准。"
        return lead + core

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
        distressed = bool(intent.get("distressed"))
        confused = bool(intent.get("confused"))
        clarification = bool(intent.get("clarification_needed"))
        withheld = intent.get("withheld") or []
        memory_prompt = bool(withheld) and not released_now
        scripted = distressed or confused or clarification or memory_prompt
        if distressed:
            draft = DISTRESS_LINES[new_state["turn"] % len(DISTRESS_LINES)]
        elif confused:
            draft = CONFUSED_LINES[new_state["turn"] % len(CONFUSED_LINES)]
        elif clarification:
            draft = CLARIFY_LINES[new_state["turn"] % len(CLARIFY_LINES)]
        elif memory_prompt:
            draft = MEMORY_PROMPT_LINES[new_state["turn"] % len(MEMORY_PROMPT_LINES)]
        else:
            draft = self.generate(
                allowed,
                self.machine.persona,
                new_state["turn"],
                focus_ids=released_now,
                empathy=bool(intent.get("empathy")),
            )
        guidance_text = intent.get("guidance_text") if intent.get("guidance") else None
        if guidance_text:
            if released_now:
                draft = draft.rstrip("。！？") + "。" + guidance_text
            else:
                draft = guidance_text

        verification = self.verify(draft, self.machine.persona, unreleased)
        attempts = 0
        degraded = False
        while not scripted and not verification["passed"] and attempts < 2:
            attempts += 1
            if not verification["repairable"]:
                break
            draft = self.generate(
                allowed,
                self.machine.persona,
                new_state["turn"] + attempts,
                focus_ids=released_now,
                empathy=bool(intent.get("empathy")),
            )
            verification = self.verify(draft, self.machine.persona, unreleased)

        if not verification["passed"] and not scripted:
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
