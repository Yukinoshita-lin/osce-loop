"""场次重跑评分：在模型或提示词版本变更后重新生成 AI 建议，同时保住人工复核与金标准。"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from ..ai.base import CapabilityGateway
from ..ai.pipeline import run_scoring
from .bundle import get_bundle

LOCKED_STATUSES = ("reviewed", "signed_off", "published")


def rescore_attempt(
    conn,
    gateway: CapabilityGateway,
    *,
    attempt_id: str,
    actor: str = "考务",
) -> dict[str, Any]:
    """重跑尚未进入人工复核的场次评分。

    - 已复核、已签字或已发布的场次拒绝重跑，避免覆盖人工结论。
    - 重跑只覆盖 AI 字段，已有 gold_score 保留，保证校准数据不丢失。
    - 历史 AI 建议以审计事件 ``attempt.rescored`` 留存变更前后摘要。
    """
    row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    if not row:
        raise ValueError("场次不存在")
    attempt = dict(row)
    if attempt["status"] in LOCKED_STATUSES:
        raise ValueError("该场次已完成人工复核、签字或发布，不能重跑评分；如需重评请走申诉流程")
    reviewed = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ? "
        "AND (reviewer IS NOT NULL OR reviewed_at IS NOT NULL)",
        (attempt_id,),
    ).fetchone()["n"]
    if reviewed:
        raise ValueError(f"该场次已有 {reviewed} 条记录完成人工复核，不能重跑评分")
    actions = conn.execute(
        "SELECT COUNT(*) AS n FROM review_actions a "
        "JOIN score_records r ON r.record_id = a.record_id WHERE r.attempt_id = ?",
        (attempt_id,),
    ).fetchone()["n"]
    if actions:
        raise ValueError(f"该场次已有 {actions} 条复核动作，不能重跑评分")

    transcript_row = conn.execute(
        "SELECT payload FROM transcripts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if not transcript_row:
        raise ValueError("场次缺少转写，无法重跑评分")
    try:
        transcript = json.loads(transcript_row["payload"] or "[]")
    except (TypeError, ValueError) as exc:
        raise ValueError("转写内容损坏，无法重跑评分") from exc
    if not transcript:
        raise ValueError("转写为空，无法重跑评分")

    bundle = get_bundle(conn, attempt["bundle_id"])
    if not bundle:
        raise ValueError("场次绑定的站包不存在，无法重跑评分")

    previous_records = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(gold_score) AS gold FROM score_records WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    stage = db.get_state(conn, "release_stage", "G0_offline") or "G0_offline"
    scoring_attempt = {**attempt, "release_stage": stage, "appeal": bool(attempt.get("appeal"))}
    result = run_scoring(
        conn,
        gateway,
        bundle=bundle,
        attempt=scoring_attempt,
        transcript=transcript,
        actor=actor,
        force=True,
    )
    updated = conn.execute(
        "SELECT status, ai_total FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return {
        "attempt_id": attempt_id,
        "previous_records": int(previous_records["n"] or 0),
        "preserved_gold": int(previous_records["gold"] or 0),
        "records": len(result["records"]),
        "ai_total": updated["ai_total"],
        "status": updated["status"],
        "release_stage": stage,
        "model_version": gateway.model_version,
        "prompt_version": gateway.prompt_version,
    }


def rescore_eligible_attempts(
    conn,
    gateway: CapabilityGateway,
    *,
    bundle_id: str | None = None,
    actor: str = "考务",
    limit: int = 200,
) -> dict[str, Any]:
    """批量重跑未复核场次；已复核、已签字或已发布场次自动跳过并说明原因。"""
    sql = "SELECT attempt_id FROM attempts"
    params: list[Any] = []
    if bundle_id:
        sql += " WHERE bundle_id = ?"
        params.append(bundle_id)
    sql += " ORDER BY created_at, rowid LIMIT ?"
    params.append(max(1, min(limit, 500)))
    rows = conn.execute(sql, params).fetchall()

    rescored: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        try:
            rescored.append(
                rescore_attempt(conn, gateway, attempt_id=row["attempt_id"], actor=actor)
            )
        except ValueError as exc:
            skipped.append({"attempt_id": row["attempt_id"], "reason": str(exc)})
    batch_id = db.new_id("rescbatch")
    db.append_audit(
        conn,
        actor=actor,
        action="attempt.rescore_batch",
        object_type="BatchJob",
        object_id=batch_id,
        before=None,
        after={
            "bundle_id": bundle_id,
            "considered": len(rows),
            "rescored": len(rescored),
            "skipped": len(skipped),
            "model_version": gateway.model_version,
            "prompt_version": gateway.prompt_version,
        },
        model_version=gateway.model_version,
        prompt_version=gateway.prompt_version,
    )
    return {
        "batch_id": batch_id,
        "bundle_id": bundle_id,
        "considered": len(rows),
        "rescored": rescored,
        "skipped": skipped,
        "model_version": gateway.model_version,
        "prompt_version": gateway.prompt_version,
    }
