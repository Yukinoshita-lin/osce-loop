"""离线录像录入：把解析后的转写写入场次并跑通评分流水线。"""
from __future__ import annotations

import json
from statistics import mean
from typing import Any

from .. import db
from ..ai.base import CapabilityGateway
from ..ai.pipeline import run_scoring
from .retention import retention_plan


def create_ingested_attempt(
    conn,
    gateway: CapabilityGateway,
    *,
    bundle: dict[str, Any],
    transcript: list[dict[str, Any]],
    candidate_code: str,
    candidate_name: str,
    risk_level: str,
    release_stage: str,
    media_id: str,
    media_url: str | None,
    media_filename: str | None,
    media_sha256: str | None = None,
    media_size_bytes: int = 0,
    transcript_source: str = "upload",
    asr_model: str | None = None,
    actor: str = "考务录入",
) -> str:
    if not transcript:
        raise ValueError("转写为空，无法创建场次")
    attempt_id = db.new_id("att")
    created_at = db.now_iso()
    plan = retention_plan(risk_level, created_at)
    duration_ms = max(int(u["end_ms"]) for u in transcript)
    asr_confidence = round(mean(float(u.get("asr_confidence", 0.95)) for u in transcript), 3)
    integrity_gap = 1 if not media_url else 0
    integrity_note = None
    if integrity_gap:
        integrity_note = "未上传媒体文件：证据只能显示转写片段，无法回放，全部记录转人工复核。"

    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, risk_level, "
        "media_id, media_url, media_filename, media_sha256, media_size_bytes, retention_class, retain_until, "
        "duration_ms, integrity_gap, integrity_note, appeal, asr_confidence, status, ab_group, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            bundle["bundle_id"],
            candidate_code,
            candidate_name,
            "SES-UPLOAD",
            risk_level,
            media_id,
            media_url,
            media_filename,
            media_sha256,
            media_size_bytes,
            plan["retention_class"],
            plan["retain_until"],
            duration_ms,
            integrity_gap,
            integrity_note,
            0,
            asr_confidence,
            "scored",
            "ai_assisted",
            created_at,
        ),
    )
    conn.execute(
        "INSERT INTO transcripts(attempt_id, payload, created_at) VALUES(?,?,?)",
        (attempt_id, json.dumps(transcript, ensure_ascii=False), db.now_iso()),
    )
    conn.commit()

    attempt = {
        "attempt_id": attempt_id,
        "risk_level": risk_level,
        "release_stage": release_stage,
        "media_id": media_id,
        "duration_ms": duration_ms,
        "asr_confidence": asr_confidence,
        "integrity_gap": bool(integrity_gap),
        "appeal": False,
    }
    run_scoring(conn, gateway, bundle=bundle, attempt=attempt, transcript=transcript)
    db.append_audit(
        conn,
        actor=actor,
        action="attempt.ingest",
        object_type="StationAttempt",
        object_id=attempt_id,
        before=None,
        after={
            "bundle_id": bundle["bundle_id"],
            "candidate_code": candidate_code,
            "risk_level": risk_level,
            "utterances": len(transcript),
            "media_filename": media_filename,
            "media_sha256": media_sha256,
            "media_size_bytes": media_size_bytes,
            "retention_class": plan["retention_class"],
            "retain_until": plan["retain_until"],
            "transcript_source": transcript_source,
            "asr_model": asr_model,
            "integrity_gap": bool(integrity_gap),
        },
        model_version=gateway.model_version,
        prompt_version=gateway.prompt_version,
    )
    return attempt_id
