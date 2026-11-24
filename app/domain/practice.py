"""SP 练习结束后的项级评分与三段式形成性反馈。"""
from __future__ import annotations

from typing import Any

from ..ai.mock import MockScoringProvider
from .feedback import ensure_feedback_evidence, ensure_feedback_units, summarize_feedback
from .stats import domain_scores

SCORE_LABELS = {0: "negative", 1: "partial", 2: "positive"}


def build_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = []
    cursor = 0
    for message in messages:
        role = message.get("role")
        text = str(message.get("text") or "").strip()
        if role not in ("candidate", "patient") or not text:
            continue
        start = int(message.get("at_ms") or cursor)
        duration = max(2200, min(8000, len(text) * 90))
        end = start + duration
        cursor = max(cursor, end) + 700
        transcript.append(
            {
                "utt_id": f"p{len(transcript) + 1:03d}",
                "speaker": role,
                "start_ms": start,
                "end_ms": end,
                "text": text,
                "asr_confidence": 0.97,
                "phase": message.get("phase") or "information_gathering",
            }
        )
    return transcript


def _weighted_total(items: list[dict[str, Any]], scores: dict[str, int | None]) -> float | None:
    earned = 0.0
    total_weight = 0.0
    for item in items:
        score = scores.get(item["item_id"])
        if score is None:
            continue
        weight = float(item.get("weight", 0))
        total_weight += weight
        earned += weight * score / 2.0
    if total_weight <= 0:
        return None
    return round(earned / total_weight * 100, 1)


def build_practice_report(
    bundle: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    gateway=None,
) -> dict[str, Any]:
    payload = bundle["payload"]
    items = payload.get("rubric", [])
    templates = {t["item_id"]: t for t in bundle["derived"].get("feedback_templates", [])}
    transcript = build_transcript(messages)
    candidates = [utt for utt in transcript if utt["speaker"] == "candidate"]
    provider = gateway or MockScoringProvider()
    scores: dict[str, int | None] = {}
    records: list[dict[str, Any]] = []
    duration_ms = max((u["end_ms"] for u in transcript), default=0)

    for item in items:
        if item.get("evidence_type") == "global":
            continue
        candidate_lines = [{**utt, "phase": item["phase"]} for utt in candidates]
        score_kwargs = {
            "item": item,
            "transcript": candidate_lines,
            "attempt_meta": {
                "media_id": "practice",
                "duration_ms": duration_ms,
                "asr_confidence": 0.97,
            },
        }
        if gateway is None:
            suggestion = provider.score_item(**score_kwargs)
        else:
            suggestion = gateway.score_item(
                **score_kwargs,
                subject_ref=f"practice:{bundle['bundle_id']}:{item['item_id']}",
            )["output"]
        scores[item["item_id"]] = suggestion.score
        evidence = suggestion.evidence[0] if suggestion.evidence else None
        covered = bool(evidence and evidence.evidence_kind != "absence")
        level = "positive" if suggestion.score == 2 else "partial" if suggestion.score == 1 else "negative"
        anchor = item.get("anchor") or {}
        alternative = (templates.get(item["item_id"]) or {}).get("alternative") or item.get("feedback_alt")
        records.append(
            {
                "item_id": item["item_id"],
                "item_title": item.get("title"),
                "domain": item.get("domain"),
                "weight": item.get("weight"),
                "critical": bool(item.get("critical")),
                "score": suggestion.score,
                "score_label": SCORE_LABELS.get(suggestion.score, "—"),
                "confidence": suggestion.confidence,
                "covered": covered,
                "evidence": {
                    "quote": evidence.quote if evidence else None,
                    "start_ms": evidence.start_ms if evidence else None,
                    "end_ms": evidence.end_ms if evidence else None,
                    "speaker": evidence.speaker if evidence else None,
                    "evidence_kind": evidence.evidence_kind if evidence else "absence",
                    "note": evidence.note if evidence else None,
                },
                "behavior": f"{item['item_id']}（{item.get('title')}）：{level}"
                + "。对应行为锚："
                + (anchor.get("positive") if suggestion.score == 2 else anchor.get("partial") if suggestion.score == 1 else anchor.get("negative") or ""),
                "alternative": alternative,
                "rationale": suggestion.rationale,
            }
        )

    deductions = [
        r for r in records if r["score"] < 2 and (r["covered"] or r["critical"])
    ]
    uncovered = [r for r in records if r["score"] == 0 and not r["covered"]]
    strengths = [r for r in records if r["score"] == 2]
    retry_items = [r for r in records if r["score"] is not None and r["score"] < 2]
    retry_items.sort(
        key=lambda r: (not r["critical"], r["score"], -float(r.get("weight") or 0))
    )
    retry_plan = {
        "count": len(retry_items),
        "critical_count": sum(1 for r in retry_items if r["critical"]),
        "items": [
            {
                "item_id": r["item_id"],
                "item_title": r["item_title"],
                "domain": r["domain"],
                "score": r["score"],
                "score_label": r["score_label"],
                "critical": r["critical"],
                "focus": r.get("alternative"),
                "evidence_at_ms": (r.get("evidence") or {}).get("start_ms"),
                "priority": "关键项" if r["critical"] else "提分项",
            }
            for r in retry_items[:6]
        ],
        "opening_tip": retry_items[0].get("alternative") if retry_items else None,
        "note": "重练清单按关键项优先、低分优先排序；先用替代话术完整演练，再开始新会话。",
    }
    ensure_feedback_units(records, {item["item_id"]: item for item in items})
    ensure_feedback_evidence(deductions, media_id="practice", duration_ms=duration_ms)
    total = _weighted_total(items, scores)
    covered_count = sum(1 for r in records if r["covered"])
    return {
        "bundle_id": bundle["bundle_id"],
        "station_title": payload.get("title"),
        "persona_version": bundle["bundle_hash"],
        "ai_versions": {
            "model_version": getattr(provider, "model_version", None),
            "prompt_version": getattr(provider, "prompt_version", None),
            "rubric_version": bundle["bundle_hash"],
        },
        "total": total,
        "pass_line": float(payload.get("pass_line", 60)),
        "items_scored": len(records),
        "covered": covered_count,
        "coverage": round(covered_count / len(records), 3) if records else 0,
        "domains": domain_scores(payload, scores),
        "deductions": deductions,
        "strengths": strengths,
        "retry_plan": retry_plan,
        "uncovered": uncovered,
        "uncovered_count": len(uncovered),
        "feedback_quality": summarize_feedback(deductions),
        "disclaimer": "形成性练习反馈，不计入成绩；共情类全局项需教师观察，未纳入自动评分。",
        "feedback_rule": "每条扣分项包含：证据片段 + 对应评分项档位 + 可直接背诵的替代话术。",
        "created_at": None,
    }
