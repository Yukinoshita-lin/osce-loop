"""透明度资产：按当前数据快照生成新的模型卡与数据表。"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from .ab import ab_report
from .bundle import list_bundles
from .fairness import fairness_report
from .spqa import certification_for_bundle
from .stats import quality_report


def _next_version(version: str | None) -> str:
    try:
        major, minor = str(version or "1.0").split(".")[:2]
        return f"{int(major)}.{int(minor) + 1}"
    except (TypeError, ValueError):
        return "1.1"


def attempt_ai_disclosure(
    attempt, records: list[dict[str, Any]], *, has_transcript: bool
) -> dict[str, Any]:
    """考生可读的 AI 参与说明：哪些环节用了 AI、最终由谁确认。"""
    stages: list[str] = []
    if has_transcript:
        stages.append("语音转写（ASR）")
    if any(rec.get("ai_score") is not None for rec in records):
        stages.append("证据定位与评分建议")
    reviewers = sorted(
        {
            rec.get("reviewer")
            for rec in records
            if rec.get("reviewer") and rec.get("reviewer") != "AI"
        }
        | ({attempt.get("signed_off_by")} if attempt.get("signed_off_by") else set())
    )
    models = sorted({rec.get("model_version") for rec in records if rec.get("model_version")})
    prompts = sorted({rec.get("prompt_version") for rec in records if rec.get("prompt_version")})
    human_confirmed = bool(reviewers) or attempt.get("status") in ("signed_off", "published")
    if not stages:
        statement = "本场次未使用 AI 自动评分，成绩由人工评定。"
    elif human_confirmed:
        statement = "AI 参与了上述环节并给出建议；最终成绩由人工复核确认。"
    else:
        statement = "AI 参与了上述环节并给出建议；该场次尚未完成人工签字确认。"
    return {
        "stages": stages,
        "model_versions": models,
        "prompt_versions": prompts,
        "human_confirmed": human_confirmed,
        "human_reviewers": reviewers,
        "statement": statement,
    }


def disclosure_acknowledged(conn, attempt_id: str, user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM ai_disclosure_acks WHERE attempt_id = ? AND user_id = ?",
        (attempt_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def acknowledge_disclosure(
    conn,
    *,
    attempt_id: str,
    user_id: str,
    actor: str,
) -> dict[str, Any]:
    existing = disclosure_acknowledged(conn, attempt_id, user_id)
    if existing:
        return existing
    ack_id = db.new_id("ack")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO ai_disclosure_acks(ack_id, attempt_id, user_id, actor, created_at) VALUES(?,?,?,?,?)",
        (ack_id, attempt_id, user_id, actor, timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="attempt.ai_disclosure_ack",
        object_type="StationAttempt",
        object_id=attempt_id,
        before=None,
        after={"ack_id": ack_id, "user_id": user_id},
    )
    return {
        "ack_id": ack_id,
        "attempt_id": attempt_id,
        "user_id": user_id,
        "actor": actor,
        "created_at": timestamp,
    }


def public_transparency(conn) -> dict[str, Any]:
    """考生/校内可查的公开摘要：只返回已公开的模型卡与最新公开数据表。"""
    cards = []
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT * FROM model_cards WHERE is_public = 1 ORDER BY created_at DESC"
    ).fetchall():
        if row["capability"] in seen:
            continue
        seen.add(row["capability"])
        cards.append(
            {
                "capability": row["capability"],
                "model_version": row["model_version"],
                "prompt_version": row["prompt_version"],
                "metrics": json.loads(row["metrics"] or "{}"),
                "limitations": row["limitations"],
                "published_at": row["published_at"],
            }
        )
    sheet_row = conn.execute(
        "SELECT * FROM data_sheets WHERE is_public = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    sheet = None
    if sheet_row:
        sheet = {
            "version": sheet_row["version"],
            "published_at": sheet_row["published_at"],
            "content": json.loads(sheet_row["content"] or "{}"),
        }
    return {
        "model_cards": cards,
        "data_sheet": sheet,
        "statement": "本页只展示已公开发布的模型卡与数据表摘要；未公开版本不对外可见。",
        "as_of": db.now_iso(),
    }


def refresh_transparency_assets(conn, *, actor: str = "系统") -> dict[str, Any]:
    report = quality_report(conn)
    fairness = fairness_report(conn)
    ab = ab_report(conn)
    bundles = list_bundles(conn)
    cert = certification_for_bundle(conn, bundles[0]) if bundles else None
    model_version = db.get_state(conn, "model_version", "") or ""
    prompt_version = db.get_state(conn, "prompt_version", "") or ""

    limitations: list[str] = []
    if report["counts"]["gold_records"] < report["counts"]["records"]:
        limitations.append("金标准尚未覆盖全部评分记录，QWK 仍需随标注扩充复验。")
    if not cert or not cert.get("valid"):
        limitations.append("当前站包的 SP 认证缺失、过期或未覆盖最新 persona_version。")
    if fairness.get("overall_gap_sd") is None:
        limitations.append("亚组样本不足，公平性差异暂无统计结论。")
    limitations.append("模型卡与数据表基于当前数据库快照；模型或提示词版本变更后需重新生成。")

    metrics = {
        "qwk_ai": report["qwk_ai"],
        "qwk_human": report["qwk_human"],
        "qwk_near_cutoff": report["qwk_near_cutoff"],
        "evidence_rate": report["evidence"]["rate"],
        "edit_rate": report["review"]["edit_rate"],
        "sample_size": report["counts"]["records"],
        "gold_records": report["counts"]["gold_records"],
        "integrity_gap_rate": report["integrity_gap_rate"],
        "subgroup_gap_sd": fairness.get("overall_gap_sd"),
        "near_improvement": (ab.get("comparison") or {}).get("near_improvement"),
        "teacher_hours_reduction": (ab.get("comparison") or {}).get("teacher_hours_reduction"),
        "sp_fact_error_rate": (cert or {}).get("red_team", {}).get("fact_error_rate"),
    }
    timestamp = db.now_iso()
    card_id = db.new_id("mc")
    conn.execute(
        "INSERT INTO model_cards(card_id, capability, model_version, prompt_version, metrics, limitations, "
        "is_public, published_at, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            card_id,
            "score.suggest",
            model_version,
            prompt_version,
            json.dumps(metrics, ensure_ascii=False),
            " ".join(limitations),
            0,
            None,
            timestamp,
        ),
    )

    latest_sheet = conn.execute(
        "SELECT version FROM data_sheets ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    version = _next_version(latest_sheet["version"] if latest_sheet else None)
    rubric_items = sum(len(bundle["payload"].get("rubric", [])) for bundle in bundles)
    sheet_content = {
        "name": "OSCE·Loop 数据表",
        "version": version,
        "source": "当前数据库快照；数据来源与授权状态以库内记录为准。",
        "attempts": report["counts"]["attempts"],
        "records": report["counts"]["records"],
        "gold_records": report["counts"]["gold_records"],
        "rubric_items": rubric_items,
        "bundles": len(bundles),
        "model_version": model_version,
        "prompt_version": prompt_version,
        "generated_at": timestamp,
        "limitations": limitations,
        "retention": "成绩、评审与审计永久保留；音视频按 L0–L3 分级留存。",
    }
    sheet_id = db.new_id("ds")
    conn.execute(
        "INSERT INTO data_sheets(data_sheet_id, version, content, is_public, published_at, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (sheet_id, version, json.dumps(sheet_content, ensure_ascii=False), 0, None, timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="model_card.refresh",
        object_type="ModelCard",
        object_id=card_id,
        before=None,
        after={
            "card_id": card_id,
            "data_sheet_id": sheet_id,
            "data_sheet_version": version,
            "metrics": metrics,
            "limitations": limitations,
        },
        model_version=model_version,
        prompt_version=prompt_version,
    )
    return {
        "card_id": card_id,
        "data_sheet_id": sheet_id,
        "data_sheet_version": version,
        "metrics": metrics,
        "limitations": limitations,
        "is_public": False,
    }
