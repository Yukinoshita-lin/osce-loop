"""离线录像录入：把解析后的转写写入场次并跑通评分流水线。"""
from __future__ import annotations

import json
from statistics import mean
from typing import Any

from .. import db
from ..ai.base import CapabilityGateway
from ..ai.pipeline import run_scoring
from .anomaly import detect_anomalies
from .execution import link_attempt_to_slot, slot_for_ingest
from .integrity import transcript_digest
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
    run_id: str | None = None,
    slot_key: str | None = None,
) -> str:
    if not transcript:
        raise ValueError("转写为空，无法创建场次")
    if bool(run_id) != bool(slot_key):
        raise ValueError("run_id 与 slot_key 必须同时提供")
    if run_id and slot_key:
        slot_for_ingest(conn, run_id, slot_key)
    attempt_id = db.new_id("att")
    created_at = db.now_iso()
    plan = retention_plan(risk_level, created_at)
    duration_ms = max(int(u["end_ms"]) for u in transcript)
    asr_values = [
        float(u["asr_confidence"])
        for u in transcript
        if u.get("asr_confidence") is not None
    ]
    asr_confidence = round(mean(asr_values), 3) if asr_values else None
    integrity_gap = 1 if not media_url else 0
    integrity_note = None
    if integrity_gap:
        integrity_note = "未上传媒体文件：证据只能显示转写片段，无法回放，全部记录转人工复核。"
    anomalies = detect_anomalies(
        transcript,
        duration_ms=duration_ms,
        station_duration_min=bundle["payload"].get("duration_min"),
        integrity_gap=bool(integrity_gap),
        media_present=bool(media_url),
    )

    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, risk_level, "
        "media_id, media_url, media_filename, media_sha256, media_size_bytes, retention_class, retain_until, "
        "duration_ms, integrity_gap, integrity_note, appeal, asr_confidence, status, ab_group, run_id, slot_key, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            run_id,
            slot_key,
            created_at,
        ),
    )
    conn.execute(
        "INSERT INTO transcripts(attempt_id, payload, sha256, created_at) VALUES(?,?,?,?)",
        (
            attempt_id,
            json.dumps(transcript, ensure_ascii=False),
            transcript_digest(transcript),
            db.now_iso(),
        ),
    )
    if anomalies:
        conn.execute(
            "UPDATE attempts SET anomalies = ? WHERE attempt_id = ?",
            (json.dumps(anomalies, ensure_ascii=False), attempt_id),
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
            "notes": sum(1 for u in transcript if u.get("source") == "note"),
            "anomalies": anomalies,
            "media_filename": media_filename,
            "media_sha256": media_sha256,
            "media_size_bytes": media_size_bytes,
            "retention_class": plan["retention_class"],
            "retain_until": plan["retain_until"],
            "transcript_source": transcript_source,
            "asr_model": asr_model,
            "integrity_gap": bool(integrity_gap),
            "run_id": run_id,
            "slot_key": slot_key,
        },
        model_version=gateway.model_version,
        prompt_version=gateway.prompt_version,
    )
    if run_id and slot_key:
        link_attempt_to_slot(
            conn, run_id=run_id, slot_key=slot_key, attempt_id=attempt_id, actor=actor
        )
    return attempt_id
