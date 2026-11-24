"""评分表即协议：校验、权限封顶与四类派生资产。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

PHASES = ("opening", "information_gathering", "assessment", "plan", "closing")
PHASE_RANK = {p: i for i, p in enumerate(PHASES)}
PHASE_LABELS = {
    "opening": "开场准备",
    "information_gathering": "信息采集",
    "assessment": "评估",
    "plan": "计划与结论",
    "closing": "结束",
}
EVIDENCE_TYPES = ("utterance", "action", "document", "global")
AUTHORITIES = ("none", "extract", "suggest", "decide")
AUTHORITY_LABELS = {
    "none": "AI 不参与",
    "extract": "仅提取证据",
    "suggest": "建议分 + 人确认",
    "decide": "AI 可入分",
}
AUTHORITY_ORDER = {"none": 0, "extract": 1, "suggest": 2, "decide": 3}

VAGUE_PATTERNS = (
    "具备",
    "表现出",
    "有较强的",
    "良好的",
    "较高的",
    "掌握",
    "熟悉",
    "态度端正",
    "意识强",
    "沟通能力强",
    "人文关怀",
)
OBSERVABLE_VERBS = (
    "询问",
    "追问",
    "说明",
    "告知",
    "确认",
    "回应",
    "解释",
    "总结",
    "提出",
    "完成",
    "识别",
    "测量",
    "记录",
    "征得",
    "复述",
    "检查",
    "建议",
    "示范",
    "核对",
    "提及",
    "表达",
    "说出",
    "给出",
    "指出",
    "列出",
)
ANCHOR_FIELDS = ("observable", "positive", "partial", "negative")


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_observable(text: str) -> tuple[bool, str]:
    """粗粒度可观测性检查：禁止空泛表述，要求出现可判定动词。"""
    value = _text(text)
    if not value:
        return False, "行为锚为空"
    for vague in VAGUE_PATTERNS:
        if vague in value:
            return False, f"含不可判定表述「{vague}」"
    if not any(verb in value for verb in OBSERVABLE_VERBS):
        return False, "缺少可被判定的动词（询问/告知/确认/回应等）"
    return True, "ok"


GUIDELINE_RELEVANCE_FLOOR = 0.03


def _relevance_terms(text: str) -> set[str]:
    """中英混排的粗粒度词项：中文按二字片段，英文/数字按整词。"""
    terms: set[str] = set()
    for part in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", (text or "").lower()):
        if re.fullmatch(r"[a-z0-9]+", part):
            terms.add(part)
        elif len(part) == 1:
            terms.add(part)
        else:
            terms.update(part[index : index + 2] for index in range(len(part) - 1))
    return terms


def guideline_relevance(item: dict[str, Any], guideline_text: str) -> float:
    """返回评分项与指南条款的词汇重合度，供人工核验语义适用性。"""
    anchor = item.get("anchor") or {}
    item_text = " ".join(
        [
            str(item.get("title") or ""),
            *(str(anchor.get(field) or "") for field in ANCHOR_FIELDS),
        ]
    )
    item_terms = _relevance_terms(item_text)
    guideline_terms = _relevance_terms(guideline_text)
    if not item_terms or not guideline_terms:
        return 0.0
    return round(len(item_terms & guideline_terms) / len(item_terms), 4)


def _diff_id(kind: str, key: str) -> str:
    """差异的稳定标识：同一 payload 重复校验得到同一 diff_id，供逐条裁决引用。"""
    return "cd_" + hashlib.sha1(f"{kind}:{key}".encode("utf-8")).hexdigest()[:12]


def _instruction_sentences(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sentences: list[dict[str, Any]] = []
    for block_index, block in enumerate(payload.get("candidate_instructions") or []):
        for piece in re.split(r"(?<=[。！？；!?;])\s*", str(block or "")):
            piece = piece.strip()
            if piece:
                sentences.append(
                    {"sentence_index": len(sentences), "instruction_index": block_index, "sentence": piece}
                )
    return sentences


def _closest_instruction(text: str, sentences: list[dict[str, Any]]) -> dict[str, Any] | None:
    """为未承诺的评分项定位最接近的题干句子，便于命题专家判断是否漏写承诺。"""
    if not sentences:
        return None
    terms = _relevance_terms(text)
    best = None
    for entry in sentences:
        overlap = len(terms & _relevance_terms(entry["sentence"]))
        if best is None or overlap > best["overlap"]:
            best = {**entry, "overlap": overlap}
    return best


def validate_rubric_item(item: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    item_id = _text(item.get("item_id")) or "(缺少 item_id)"
    anchor = item.get("anchor") or {}
    for field in ANCHOR_FIELDS:
        ok, why = is_observable(anchor.get(field, ""))
        if not ok:
            issues.append(f"{item_id}.anchor.{field}: {why}")

    evidence_type = item.get("evidence_type")
    if evidence_type not in EVIDENCE_TYPES:
        issues.append(f"{item_id}: evidence_type 非法 ({evidence_type})")
    phase = item.get("phase")
    if phase not in PHASES:
        issues.append(f"{item_id}: phase 非法 ({phase})")
    authority = item.get("ai_authority")
    if authority not in AUTHORITIES:
        issues.append(f"{item_id}: ai_authority 非法 ({authority})")
    try:
        weight = float(item.get("weight", 0))
        if weight <= 0:
            issues.append(f"{item_id}: weight 必须为正")
    except (TypeError, ValueError):
        issues.append(f"{item_id}: weight 不是数字")

    if evidence_type == "global" and authority != "none":
        issues.append(f"{item_id}: global 证据项必须 ai_authority=none")
    if evidence_type != "global":
        slot = item.get("slot") or {}
        if not _text(slot.get("content")):
            issues.append(f"{item_id}: 缺少可派生的 SP 信息槽 content")
        if not item.get("probe", {}).get("positive"):
            issues.append(f"{item_id}: 缺少证据探针 positive 模式")
        if _text(slot.get("phase") or phase) != phase:
            issues.append(f"{item_id}: 信息槽阶段与评分项阶段不一致")
    if item.get("critical") and authority == "decide":
        issues.append(f"{item_id}: 关键项不允许 AI 直接入分")

    ref = _text(item.get("guideline_ref"))
    if not ref:
        issues.append(f"{item_id}: 缺少 guideline_ref")
    if evidence_type != "global" and not _text(item.get("feedback_alt")):
        issues.append(f"{item_id}: 缺少可直接背诵的替代话术 feedback_alt")
    return issues


def validate_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    """返回四项强制检查结果，任意 fail 阻断冻结。"""
    rubric = payload.get("rubric") or []
    kb = {entry.get("ref"): entry for entry in payload.get("guideline_kb", []) if entry.get("ref")}
    issues: list[str] = []
    seen: set[str] = set()

    required_issues: list[str] = []
    if not _text(payload.get("station_id")):
        required_issues.append("payload 缺少 station_id")
    if not _text(payload.get("title")):
        required_issues.append("payload 缺少 title")
    if not rubric:
        required_issues.append("rubric 不得为空，至少包含一个评分项")
    issues.extend(required_issues)

    for item in rubric:
        item_id = item.get("item_id")
        if item_id in seen:
            issues.append(f"item_id 重复: {item_id}")
        seen.add(item_id)
        issues.extend(validate_rubric_item(item))

    # 1. 可观测性
    observability_issues = [i for i in issues if ".anchor." in i]
    # 2. 可达性
    reachability_issues = [
        i
        for i in issues
        if "缺少可派生的 SP 信息槽" in i or "信息槽阶段" in i or "global 证据项" in i
    ]
    # 3. 指南一致性：先查原文是否在库，再做粗粒度语义相关性筛查
    guideline_issues: list[str] = []
    guideline_pending: list[str] = []
    guideline_similarity: dict[str, float] = {}
    for item in rubric:
        ref = _text(item.get("guideline_ref"))
        if ref and ref not in kb:
            guideline_pending.append(f"{item.get('item_id')}: 指南条款未在知识库中命中，需人工核验「{ref}」")
        elif ref:
            score = guideline_relevance(item, str(kb[ref].get("text") or ""))
            guideline_similarity[item.get("item_id")] = score
            if score < GUIDELINE_RELEVANCE_FLOOR:
                guideline_pending.append(
                    f"{item.get('item_id')}: 指南条款语义相关性低（{score:.2f}），需人工确认适用性「{ref}」"
                )
    guideline_status = "fail" if guideline_issues else ("warn" if guideline_pending else "pass")

    # 4. 三方一致：考生指令承诺的考点 ⊆ rubric；脚本信息槽 ⊆ rubric
    rubric_ids = {i.get("item_id") for i in rubric}
    refs = payload.get("instruction_item_refs") or []
    missing_refs = [r for r in refs if r not in rubric_ids]
    if missing_refs:
        issues.append(f"考生指令引用了不存在的评分项: {', '.join(missing_refs)}")
    unpromised_items = sorted(rubric_ids - set(refs))
    extra_slots = [slot for slot in payload.get("extra_slots") or [] if isinstance(slot, dict)]
    orphan_extra_slots = [
        index
        for index, slot in enumerate(extra_slots, start=1)
        if not _text(slot.get("purpose"))
    ]
    broken_slot_links = [
        f"附加信息槽 {index} 关联不存在的评分项 {linked}"
        for index, slot in enumerate(extra_slots, start=1)
        for linked in slot.get("linked_items") or []
        if linked not in rubric_ids
    ]
    issues.extend(broken_slot_links)
    consistency_pending: list[str] = []
    if unpromised_items:
        consistency_pending.append(
            "以下评分项未被考生指令显式承诺：" + "、".join(unpromised_items)
        )
    if orphan_extra_slots:
        consistency_pending.append(
            "以下附加信息槽未标注 purpose（共病/无关诉求/模糊/红旗等）："
            + "、".join(f"#{index}" for index in orphan_extra_slots)
        )
    slot_issues = [i for i in issues if "缺少证据探针" in i or "weight" in i or "evidence_type" in i or "phase 非法" in i]
    feedback_issues = [i for i in issues if "替代话术" in i]
    consistency_status = (
        "fail"
        if (missing_refs or required_issues or feedback_issues or broken_slot_links)
        else ("warn" if (slot_issues or consistency_pending) else "pass")
    )

    # 差异清单精确到句子：每条差异带稳定 diff_id、严重度、定位与修改建议，供逐条裁决。
    rubric_by_id = {item.get("item_id"): item for item in rubric}
    item_index_by_id: dict[Any, int] = {}
    for item_index, item in enumerate(rubric):
        item_index_by_id.setdefault(item.get("item_id"), item_index)
    instruction_sentences = _instruction_sentences(payload)
    diffs: list[dict[str, Any]] = []

    def add_diff(
        kind: str,
        key: str,
        *,
        severity: str,
        message: str,
        location: dict[str, Any],
        hint: str,
    ) -> None:
        diffs.append(
            {
                "diff_id": _diff_id(kind, key),
                "type": kind,
                "severity": severity,
                "message": message,
                "location": location,
                "hint": hint,
            }
        )

    for ref_index, ref in enumerate(refs):
        if ref in rubric_ids:
            continue
        add_diff(
            "missing_ref",
            f"{ref_index}:{ref}",
            severity="fail",
            message=f"考生指令引用了不存在的评分项 {ref}",
            location={"section": "instruction_item_refs", "ref_index": ref_index, "item_id": ref},
            hint="删除该引用，或在 rubric 中补充对应评分项",
        )
    for item_id in unpromised_items:
        item = rubric_by_id.get(item_id) or {}
        observable = _text((item.get("anchor") or {}).get("observable"))
        add_diff(
            "unpromised_item",
            str(item_id),
            severity="warn",
            message=f"评分项 {item_id} 未被考生指令显式承诺",
            location={
                "section": "rubric",
                "item_index": item_index_by_id.get(item_id, 0),
                "item_id": item_id,
                "sentence": observable,
                "closest_instruction": _closest_instruction(
                    f"{item.get('title') or ''} {observable}", instruction_sentences
                ),
            },
            hint="在 candidate_instructions 中补充该考点，或从 rubric 中删除该项",
        )
    for index, slot in enumerate(extra_slots, start=1):
        content = str(slot.get("content") or "").strip()
        if index in orphan_extra_slots:
            add_diff(
                "slot_without_purpose",
                str(index),
                severity="warn",
                message=f"附加信息槽 {index} 未标注 purpose",
                location={"section": "extra_slots", "slot_index": index, "sentence": content},
                hint="标注 purpose（chief_complaint/red_flag/comorbidity/unrelated_concern/ambiguity 等），供派生与质控识别",
            )
        for linked in slot.get("linked_items") or []:
            if linked in rubric_ids:
                continue
            add_diff(
                "broken_slot_link",
                f"{index}:{linked}",
                severity="fail",
                message=f"附加信息槽 {index} 关联不存在的评分项 {linked}",
                location={
                    "section": "extra_slots",
                    "slot_index": index,
                    "sentence": content,
                    "linked_item": linked,
                },
                hint="修正 linked_items，或补充对应评分项",
            )

    consistency_details = {
        "promised_items": sorted(set(refs)),
        "unpromised_items": unpromised_items,
        "extra_slots": len(extra_slots),
        "extra_slots_without_purpose": orphan_extra_slots,
        "broken_slot_links": broken_slot_links,
        "diffs": diffs,
        "diff_counts": {
            "fail": sum(1 for diff in diffs if diff["severity"] == "fail"),
            "warn": sum(1 for diff in diffs if diff["severity"] == "warn"),
        },
    }

    def status_of(entries: list[str]) -> str:
        return "fail" if entries else "pass"

    checks = [
        {
            "key": "observability",
            "label": "可观测性",
            "rule": "行为锚必须含可被判定的动词与可听/可见的对象",
            "status": status_of(observability_issues),
            "issues": observability_issues,
        },
        {
            "key": "reachability",
            "label": "可达性",
            "rule": "每个评分项的信息源必须存在于 SP 信息槽中",
            "status": status_of(reachability_issues),
            "issues": reachability_issues,
        },
        {
            "key": "guideline",
            "label": "指南一致性",
            "rule": "guideline_ref 必须能在知识库中检索到原文，且与行为锚语义相关",
            "status": guideline_status,
            "issues": guideline_issues + guideline_pending,
        },
        {
            "key": "consistency",
            "label": "三方一致",
            "rule": "题干承诺的考点 ⊆ 脚本可提供的信息 ⊆ rubric 评的内容",
            "status": consistency_status,
            "details": consistency_details,
            "issues": [
                i
                for i in issues
                if i not in observability_issues and i not in reachability_issues
            ]
            + consistency_pending,
        },
    ]
    blocking = [
        i
        for check in checks
        if check["status"] == "fail"
        for i in check["issues"]
    ]
    return {
        "checks": checks,
        "issues": issues,
        "blocking": blocking,
        "can_freeze": not blocking,
        "guideline_similarity": guideline_similarity,
        "consistency_details": consistency_details,
        "counts": {
            "items": len(rubric),
            "critical": sum(1 for i in rubric if i.get("critical")),
            "guideline_pending": len(guideline_pending),
            "unpromised_items": len(unpromised_items),
            "extra_slots_without_purpose": len(orphan_extra_slots),
            "consistency_diffs": len(diffs),
        },
    }


def cap_authority(
    item: dict[str, Any],
    risk_level: str,
    release_stage: str = "G0_offline",
) -> str:
    """按证据形态、风险等级与闸门阶段对 AI 权限自动封顶。"""
    authority = item.get("ai_authority", "none")
    if authority not in AUTHORITY_ORDER:
        return "none"
    if item.get("evidence_type") == "global":
        return "none"
    ceiling = "suggest"
    if risk_level in ("L0", "L1") and release_stage in ("G0_offline", "G1_practice"):
        ceiling = "decide"
    if risk_level == "L3":
        # P3 有限高利害：只有完成 G3 放行后，才允许非关键 utterance/action 项给建议分
        if release_stage != "G3_limited":
            ceiling = "extract"
        elif item.get("critical"):
            ceiling = "extract"
        elif item.get("evidence_type") in ("utterance", "action"):
            ceiling = "suggest"
        else:
            ceiling = "extract"
    if item.get("critical") and AUTHORITY_ORDER[authority] > AUTHORITY_ORDER["suggest"]:
        if AUTHORITY_ORDER[ceiling] > AUTHORITY_ORDER["suggest"]:
            ceiling = "suggest"
    if AUTHORITY_ORDER[authority] <= AUTHORITY_ORDER[ceiling]:
        return authority
    return ceiling


def derive_assets(payload: dict[str, Any]) -> dict[str, Any]:
    """在冻结时一次性派生出 SP 信息槽、证据探针、对话检查点、反馈模板。"""
    rubric = payload.get("rubric") or []
    phase_rank = {p: i for i, p in enumerate(PHASES)}
    ordered = sorted(
        rubric,
        key=lambda i: (phase_rank.get(i.get("phase"), 99), int(i.get("seq", 99)), str(i.get("item_id"))),
    )

    slots: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    feedback: list[dict[str, Any]] = []
    checkpoints: dict[str, dict[str, Any]] = {p: {"phase": p, "must_probe": [], "guidance": []} for p in PHASES}
    slot_seq = 0

    # 主诉等不属于单一评分项的公共信息槽
    for extra in payload.get("extra_slots", []):
        slot_seq += 1
        slots.append(
            {
                "slot_id": f"S.{slot_seq:02d}",
                "content": extra.get("content", ""),
                "linked_items": extra.get("linked_items", []),
                "phase": extra.get("phase", "opening"),
                "disclosure": extra.get("disclosure", "active"),
                "trigger": {"type": "any_of", "conditions": []},
                "triggers": extra.get("triggers", []),
                "entities": extra.get("entities", []),
                "leak_guard": True,
                "is_public": True,
            }
        )

    for item in ordered:
        item_id = item.get("item_id")
        phase = item.get("phase")
        evidence_type = item.get("evidence_type")
        probe_cfg = item.get("probe") or {}
        checkpoint = checkpoints.setdefault(phase, {"phase": phase, "must_probe": [], "guidance": []})
        if item.get("critical") or float(item.get("weight", 0)) >= 2:
            checkpoint["must_probe"].append(item_id)

        probes.append(
            {
                "probe_id": f"P.{item_id}",
                "item_id": item_id,
                "evidence_type": evidence_type,
                "phase": phase,
                "positive": probe_cfg.get("positive", []),
                "partial": probe_cfg.get("partial", []),
                "absence_note": "该阶段未观察到与评分项相关的询问",
                "confidence_floor": float(item.get("confidence_floor", 0.55)),
            }
        )

        feedback.append(
            {
                "template_id": f"F.{item_id}",
                "item_id": item_id,
                "title": item.get("title") or item_id,
                "anchor": item.get("anchor") or {},
                "alternative": item.get("feedback_alt")
                or "下次可追加一句具体提问，覆盖该评分项后再进入下一环节。",
                "structure": ["evidence", "behavior", "alternative"],
            }
        )

        slot_cfg = item.get("slot") or {}
        if evidence_type != "global" and slot_cfg.get("content"):
            slot_seq += 1
            triggers = slot_cfg.get("triggers") or []
            slots.append(
                {
                    "slot_id": f"S.{slot_seq:02d}",
                    "content": slot_cfg.get("content"),
                    "linked_items": [item_id],
                    "phase": phase,
                    "disclosure": slot_cfg.get("disclosure", "on_direct_ask"),
                    "trigger": {
                        "type": "any_of",
                        "conditions": [{"intent": f"kw:{t}"} for t in triggers],
                    },
                    "triggers": triggers,
                    "entities": slot_cfg.get("entities") or [],
                    "leak_guard": True,
                }
            )
            checkpoint["guidance"].append(
                f"阶段末若 {item_id} 对应信息未被问及，可用自然引导语提示而非静默漏考。"
            )

    safe_templates = {
        p: "患者停顿了一下，看着你，等待继续交流。"
        for p in PHASES
    }
    return {
        "phase_order": list(PHASES),
        "phase_labels": PHASE_LABELS,
        "slots": slots,
        "probes": probes,
        "feedback_templates": feedback,
        "checkpoints": list(checkpoints.values()),
        "safe_templates": safe_templates,
        "counts": {
            "slots": len(slots),
            "probes": len(probes),
            "feedback_templates": len(feedback),
            "checkpoints": len(checkpoints),
        },
    }


def rubric_patterns(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """供 mock 证据提取器使用：item_id -> 探针配置。"""
    result: dict[str, dict[str, Any]] = {}
    for item in payload.get("rubric", []):
        probe = item.get("probe") or {}
        result[item.get("item_id")] = {
            "positive": [re.compile(p) for p in probe.get("positive", [])],
            "partial": [re.compile(p) for p in probe.get("partial", [])],
        }
    return result
