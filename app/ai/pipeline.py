"""P0 离线评分流水线：证据提取 → 项级建议 → 风险路由 → 入库。"""
from __future__ import annotations

import json
from statistics import mean
from typing import Any

from .. import db
from ..config import ASR_COHORT_MIN_ATTEMPTS
from ..domain.bundle import effective_authority
from ..domain.routing import compute_ai_total, is_near_cutoff, route_item
from .base import CapabilityGateway, evidence_to_json


def _item_disagreement(conn, bundle_hash: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT r.item_id, a.action FROM review_actions a "
        "JOIN score_records r ON r.record_id = a.record_id "
        "WHERE r.rubric_version = ?",
        (bundle_hash,),
    ).fetchall()
    buckets: dict[str, list[str]] = {}
    for row in rows:
        buckets.setdefault(row["item_id"], []).append(row["action"])
    return {
        item_id: round(sum(1 for a in acts if a in ("correct", "reject")) / len(acts), 3)
        for item_id, acts in buckets.items()
        if acts
    }


def _mark_degraded(routing: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """主模型失败并启用兜底时，任何分数都必须回到人工确认。"""
    if not response.get("degraded"):
        return routing
    reasons = list(dict.fromkeys([*(routing.get("reasons") or []), "ai_degraded"]))
    routing.update(
        {
            "requires_human": True,
            "route": "manual",
            "reason": "ai_degraded",
            "reasons": reasons,
            "reason_text": "主评分模型不可用，已使用兜底评分，必须人工确认",
        }
    )
    return routing


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
    return round(earned / total_weight * 100, 2)


def _item_asr_confidence(
    transcript: list[dict[str, Any]], item: dict[str, Any], default: float | None
) -> float | None:
    """项级 ASR 置信度：优先取该项对应阶段内发言的平均置信度。"""
    phase = item.get("phase")
    if phase:
        values = [
            float(u["asr_confidence"])
            for u in transcript
            if u.get("phase") == phase and u.get("asr_confidence") is not None
        ]
        if values:
            return round(mean(values), 3)
    return default


def _asr_cohort_gap(conn, bundle_id: str, attempt: dict[str, Any]) -> float | None:
    """当前场次转写置信度与同站历史均值的差；历史样本不足时返回 None。"""
    current = attempt.get("asr_confidence")
    if current is None:
        return None
    row = conn.execute(
        "SELECT AVG(asr_confidence) AS avg, COUNT(*) AS n FROM attempts "
        "WHERE bundle_id = ? AND attempt_id != ? AND asr_confidence IS NOT NULL",
        (bundle_id, attempt["attempt_id"]),
    ).fetchone()
    if not row or int(row["n"] or 0) < ASR_COHORT_MIN_ATTEMPTS or row["avg"] is None:
        return None
    return round(float(current) - float(row["avg"]), 3)


def run_scoring(
    conn,
    gateway: CapabilityGateway,
    *,
    bundle: dict[str, Any],
    attempt: dict[str, Any],
    transcript: list[dict[str, Any]],
    actor: str = "AI 评分副驾",
) -> dict[str, Any]:
    attempt_id = attempt["attempt_id"]
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()["n"]
    if existing:
        return get_attempt_scores(conn, attempt_id)

    payload = bundle["payload"]
    rubric = payload.get("rubric", [])
    pass_line = float(payload.get("pass_line", 60))
    risk_level = attempt["risk_level"]
    stage = attempt.get("release_stage") or "G0_offline"
    disagreement = _item_disagreement(conn, bundle["bundle_hash"])
    attempt_meta = {
        "media_id": attempt.get("media_id"),
        "duration_ms": attempt.get("duration_ms"),
        "asr_confidence": attempt.get("asr_confidence"),
    }

    # 第一遍：只做证据提取与建议分，不在没有总分时草率路由
    prepared: dict[str, dict[str, Any]] = {}
    for item in rubric:
        authority = effective_authority(conn, bundle, item, risk_level, stage)
        if authority == "none":
            prepared[item["item_id"]] = {"authority": "none", "suggestion": None, "gateway": None}
            continue
        response = gateway.score_item(
            item=item,
            transcript=transcript,
            attempt_meta=attempt_meta,
            subject_ref=f"{attempt_id}:{item['item_id']}",
        )
        prepared[item["item_id"]] = {
            "authority": authority,
            "suggestion": response["output"],
            "gateway": response,
        }

    ai_scores = {
        item_id: (
            entry["suggestion"].score
            if entry["suggestion"] and entry["authority"] in ("suggest", "decide")
            else None
        )
        for item_id, entry in prepared.items()
    }
    ai_total = _weighted_total(rubric, ai_scores)
    near = is_near_cutoff(ai_total, pass_line)
    low_tail = risk_level == "L2" and ai_total is not None and ai_total < pass_line
    integrity_gap = bool(attempt.get("integrity_gap"))
    asr_gap = _asr_cohort_gap(conn, bundle["bundle_id"], attempt)

    for item in rubric:
        item_id = item["item_id"]
        entry = prepared[item_id]
        authority = entry["authority"]
        suggestion = entry["suggestion"]
        record_id = f"rec_{attempt_id}_{item_id}"
        item_asr = _item_asr_confidence(transcript, item, attempt.get("asr_confidence"))

        if authority == "none":
            routing = route_item(
                record_id=record_id,
                item=item,
                authority="none",
                ai_score=None,
                confidence=None,
                risk_level=risk_level,
                release_stage=stage,
            )
            final_score = None
            status = "manual_pending"
            conn.execute(
                _INSERT_RECORD,
                (
                    record_id, attempt_id, item_id, None, None, "[]", "该项为全局/人工保留项，AI 不参与。",
                    json.dumps(routing, ensure_ascii=False), None, status, final_score, None, None,
                    None, None, bundle["bundle_hash"], item_asr, db.now_iso(),
                ),
            )
            continue

        response = entry["gateway"]
        if authority == "extract":
            evidence_json = evidence_to_json(suggestion.evidence) if suggestion else "[]"
            rationale = (
                f"AI 仅定位证据，不给分：{suggestion.rationale}"
                if suggestion and suggestion.rationale
                else "AI 仅定位证据，分数由人给。"
            )
            routing = {
                "requires_human": True,
                "route": "manual",
                "reason": "extract_only",
                "reasons": ["extract_only"],
                "reason_text": "AI 仅提取证据，人工评分",
                "detail": {},
            }
            _mark_degraded(routing, response)
            if suggestion and suggestion.score is not None and not json.loads(evidence_json or "[]"):
                raise ValueError(f"{item_id}: 仅提取证据模式缺少 evidence")
            conn.execute(
                _INSERT_RECORD,
                (
                    record_id, attempt_id, item_id, None, suggestion.confidence if suggestion else None,
                    evidence_json, rationale, json.dumps(routing, ensure_ascii=False), None,
                    "manual_pending", None, None, None,
                    response["model_version"], response["prompt_version"], bundle["bundle_hash"],
                    item_asr, db.now_iso(),
                ),
            )
            continue

        if suggestion is None or suggestion.score is None:
            reason = "ai_refusal" if suggestion and suggestion.refusal else "extract_only"
            routing = {
                "requires_human": True,
                "route": "manual",
                "reason": reason,
                "reasons": [reason],
                "reason_text": (suggestion.refusal_reason if suggestion else None) or "AI 仅提取证据，人工评分",
                "detail": {},
            }
            _mark_degraded(routing, response)
            status = "manual_pending" if authority == "extract" else "pending_review"
            evidence_json = evidence_to_json(suggestion.evidence) if suggestion else "[]"
            rationale = (suggestion.rationale if suggestion else "") or "AI 未给出分数，转人工。"
            if suggestion and suggestion.refusal:
                rationale = suggestion.refusal_reason or rationale
            _assert_schema(evidence_json, response, item_id, allow_null_score=True)
            conn.execute(
                _INSERT_RECORD,
                (
                    record_id, attempt_id, item_id, None, suggestion.confidence if suggestion else None,
                    evidence_json, rationale, json.dumps(routing, ensure_ascii=False), None, status, None, None, None,
                    response["model_version"], response["prompt_version"], bundle["bundle_hash"],
                    item_asr, db.now_iso(),
                ),
            )
            continue

        routing = route_item(
            record_id=record_id,
            item=item,
            authority=authority,
            ai_score=suggestion.score,
            confidence=suggestion.confidence,
            risk_level=risk_level,
            release_stage=stage,
            near_cutoff=near,
            integrity_gap=integrity_gap,
            appeal=bool(attempt.get("appeal")),
            item_disagreement_rate=disagreement.get(item_id, 0.0),
            asr_confidence=item_asr,
            asr_cohort_gap=asr_gap,
            low_score_tail=low_tail,
            low_score_detail={"ai_total": ai_total, "pass_line": pass_line},
        )
        _mark_degraded(routing, response)
        evidence_json = evidence_to_json(suggestion.evidence)
        _assert_schema(evidence_json, response, item_id, allow_null_score=False)
        auto = not routing["requires_human"]
        status = "auto" if auto else "pending_review"
        final_score = suggestion.score if auto else None
        conn.execute(
            _INSERT_RECORD,
            (
                record_id, attempt_id, item_id, suggestion.score, suggestion.confidence,
                evidence_json, suggestion.rationale, json.dumps(routing, ensure_ascii=False), None,
                status, final_score, "AI" if auto else None, db.now_iso() if auto else None,
                response["model_version"], response["prompt_version"], bundle["bundle_hash"],
                item_asr, db.now_iso(),
            ),
        )

    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ? AND status IN ('pending_review','manual_pending')",
        (attempt_id,),
    ).fetchone()["n"]
    status = "integrity_hold" if integrity_gap else ("pending_review" if pending else "scored")
    conn.execute(
        "UPDATE attempts SET status = ?, ai_total = ?, near_cutoff = ? WHERE attempt_id = ?",
        (status, ai_total, 1 if near else 0, attempt_id),
    )
    conn.commit()

    summary = {
        "attempt_id": attempt_id,
        "ai_total": ai_total,
        "near_cutoff": near,
        "low_score_tail": low_tail,
        "integrity_gap": integrity_gap,
        "pending_review": pending,
        "records": len(rubric),
        "status": status,
    }
    db.append_audit(
        conn,
        actor=actor,
        action="attempt.scored",
        object_type="StationAttempt",
        object_id=attempt_id,
        before=None,
        after=summary,
        model_version=gateway.model_version,
        prompt_version=gateway.prompt_version,
    )
    return get_attempt_scores(conn, attempt_id)


def _assert_schema(evidence_json: str, response: dict[str, Any], item_id: str, *, allow_null_score: bool) -> None:
    """无证据的 AI 分在 schema 层就无法写入。"""
    evidence = json.loads(evidence_json or "[]")
    has_score = response.get("output") and response["output"].score is not None
    if has_score and not evidence:
        raise ValueError(f"{item_id}: AI 分数缺少 evidence，schema 拒绝写入")
    if has_score and not allow_null_score:
        if not response.get("model_version") or not response.get("prompt_version"):
            raise ValueError(f"{item_id}: 缺少 model_version/prompt_version，记录无效")


_INSERT_RECORD = """
INSERT INTO score_records(
    record_id, attempt_id, item_id, ai_score, ai_confidence, ai_evidence, ai_rationale,
    routing, gold_score, status, final_score, reviewer, reviewed_at,
    model_version, prompt_version, rubric_version, asr_confidence, created_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def get_attempt_scores(conn, attempt_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = ? ORDER BY item_id", (attempt_id,)
    ).fetchall()
    return {
        "attempt_id": attempt_id,
        "records": db.rows_to_dicts(rows),
    }
