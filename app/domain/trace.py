"""成绩—证据—操作三者互查：单条成绩的完整追溯视图。"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from .integrity import quote_digest, transcript_digest, verify_evidence


def _loads(value: Any, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _transcript_utterances(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    data = _loads(payload, [])
    if isinstance(data, dict):
        utterances = data.get("utterances")
        return (utterances if isinstance(utterances, list) else []), data
    if isinstance(data, list):
        return data, None
    return [], None


def _quote_verified(evidence: dict[str, Any], utterances: list[dict[str, Any]]) -> dict[str, Any]:
    quote = str(evidence.get("quote") or "").strip()
    if evidence.get("evidence_kind") == "absence" or not quote:
        return {"verified": None, "utterance_id": None, "transcript_text": None}
    match = next(
        (
            utt
            for utt in utterances
            if utt.get("speaker") == evidence.get("speaker")
            and int(utt.get("start_ms") or -1) == int(evidence.get("start_ms") or -2)
            and int(utt.get("end_ms") or -1) == int(evidence.get("end_ms") or -2)
        ),
        None,
    )
    if match is None:
        match = next((utt for utt in utterances if quote in str(utt.get("text") or "")), None)
    matched_text = str(match.get("text") or "") if match else ""
    return {
        "verified": bool(match) and quote in matched_text,
        "utterance_id": match.get("utt_id") if match else None,
        "transcript_text": matched_text or None,
        "utterance_hash": quote_digest(matched_text) if matched_text else None,
    }


def score_trace(conn, attempt_id: str, item_id: str | None = None) -> dict[str, Any] | None:
    """还原“谁在什么时候依据哪段视频改了什么”，供抽查与申诉调档。"""
    attempt_row = conn.execute(
        "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if not attempt_row:
        return None
    attempt = dict(attempt_row)

    bundle_row = conn.execute(
        "SELECT station_id, title, version, bundle_hash, status, frozen_at, supersedes, reason "
        "FROM bundles WHERE bundle_id = ?",
        (attempt["bundle_id"],),
    ).fetchone()
    bundle = dict(bundle_row) if bundle_row else None

    transcript_row = conn.execute(
        "SELECT payload, sha256, created_at FROM transcripts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    utterances: list[dict[str, Any]] = []
    transcript_meta = None
    transcript_state = "missing"
    if transcript_row:
        utterances, transcript_meta = _transcript_utterances(transcript_row["payload"])
        stored = transcript_row["sha256"]
        if not stored:
            transcript_state = "unrecorded"
        elif stored in (
            transcript_digest(utterances),
            transcript_digest(_loads(transcript_row["payload"], [])),
        ):
            transcript_state = "ok"
        else:
            transcript_state = "mismatch"

    query = "SELECT * FROM score_records WHERE attempt_id = ?"
    params: list[Any] = [attempt_id]
    if item_id:
        query += " AND item_id = ?"
        params.append(item_id)
    record_rows = conn.execute(query + " ORDER BY item_id", params).fetchall()
    records: list[dict[str, Any]] = []
    all_evidence: list[dict[str, Any]] = []
    record_ids: list[str] = []
    for row in record_rows:
        record = dict(row)
        record_ids.append(record["record_id"])
        evidence = _loads(record.get("ai_evidence"), [])
        evidence_entries = []
        for span in evidence if isinstance(evidence, list) else []:
            check = _quote_verified(span, utterances)
            enriched = {
                **span,
                **check,
                "media_url": attempt.get("media_url"),
                "media_id": attempt.get("media_id"),
            }
            evidence_entries.append(enriched)
            all_evidence.append(span)
        actions = [
            dict(action)
            for action in conn.execute(
                "SELECT * FROM review_actions WHERE record_id = ? ORDER BY created_at, rowid",
                (record["record_id"],),
            ).fetchall()
        ]
        records.append(
            {
                "record": record,
                "routing": _loads(record.get("routing"), {}),
                "evidence": evidence_entries,
                "review_actions": actions,
            }
        )

    audit_ids = [attempt_id, *record_ids, attempt["bundle_id"]]
    placeholders = ",".join("?" * len(audit_ids))
    audit_rows = conn.execute(
        "SELECT event_id, actor, action, object_type, object_id, before, after, model_version, "
        f"prompt_version, created_at FROM audit_events WHERE object_id IN ({placeholders}) "
        "ORDER BY created_at, rowid LIMIT 200",
        audit_ids,
    ).fetchall()
    audit_events = []
    for row in audit_rows:
        event = dict(row)
        event["before"] = _loads(event.get("before"), None)
        event["after"] = _loads(event.get("after"), None)
        audit_events.append(event)

    evidence_check = verify_evidence(all_evidence)
    quoted = [entry for entry in all_evidence if entry.get("evidence_kind") != "absence" and entry.get("quote")]
    quote_matched = sum(
        1
        for entry in quoted
        if _quote_verified(entry, utterances)["verified"]
    )
    version_stamped = bool(records) and all(
        entry["record"].get("model_version")
        and entry["record"].get("prompt_version")
        and entry["record"].get("rubric_version")
        for entry in records
    )
    signed_at = attempt.get("signed_off_at")
    published_at = attempt.get("published_at")
    signoff_order = None
    if signed_at and published_at:
        signoff_order = signed_at <= published_at
    issues: list[str] = []
    if not version_stamped:
        issues.append("存在缺少 model/prompt/rubric 版本戳的评分记录，按规则不得进入成绩计算。")
    if transcript_state == "mismatch":
        issues.append("转写哈希与入库值不一致，需排查证据链是否被改写。")
    if evidence_check["mismatched"]:
        issues.append(f"{evidence_check['mismatched']} 条证据引文哈希与入库值不一致。")
    if quoted and quote_matched < len(quoted):
        issues.append(f"{len(quoted) - quote_matched} 条证据引文无法在转写中逐字定位。")
    if signoff_order is False:
        issues.append("发布时间早于人工签字时间，签字链顺序异常。")
    if attempt.get("media_purged_at") and not attempt.get("media_sha256"):
        issues.append("媒体已清理但未留下哈希，无法核验原始文件。")

    return {
        "attempt": attempt,
        "bundle": bundle,
        "media": {
            "media_id": attempt.get("media_id"),
            "media_url": attempt.get("media_url"),
            "media_sha256": attempt.get("media_sha256"),
            "duration_ms": attempt.get("duration_ms"),
            "integrity_gap": attempt.get("integrity_gap"),
            "integrity_note": attempt.get("integrity_note"),
            "purged_at": attempt.get("media_purged_at"),
            "purged_by": attempt.get("media_purged_by"),
        },
        "transcript": {
            "state": transcript_state,
            "sha256": transcript_row["sha256"] if transcript_row else None,
            "created_at": transcript_row["created_at"] if transcript_row else None,
            "utterances": utterances,
            "warnings": (transcript_meta or {}).get("warnings"),
        },
        "records": records,
        "audit_events": audit_events,
        "integrity": {
            "version_stamped": version_stamped,
            "evidence_hash": evidence_check,
            "quotes_checked": len(quoted),
            "quotes_matched": quote_matched,
            "signoff_before_publish": signoff_order,
            "issues": issues,
        },
        "note": "追溯范围：评分记录 → AI 证据片段 → 转写原文 → 复核动作 → 审计事件；用于人工抽查与申诉调档。",
        "generated_at": db.now_iso(),
    }
