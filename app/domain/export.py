"""申诉举证包：把场次、证据、评分、复核与审计导出为可校验 JSON。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import db
from .integrity import transcript_integrity


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def package_hash(package: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(package).encode("utf-8")).hexdigest()


def build_attempt_package(conn, attempt_id: str, *, exported_by: str) -> dict[str, Any]:
    attempt_row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    if not attempt_row:
        raise ValueError("场次不存在")
    attempt = dict(attempt_row)
    bundle_row = conn.execute("SELECT * FROM bundles WHERE bundle_id = ?", (attempt["bundle_id"],)).fetchone()
    if not bundle_row:
        raise ValueError("场次绑定的站包不存在")
    bundle = dict(bundle_row)
    for key in ("payload", "derived", "validation"):
        try:
            bundle[key] = json.loads(bundle[key])
        except (TypeError, ValueError):
            bundle[key] = {} if key != "payload" else []

    transcript_row = conn.execute(
        "SELECT payload, sha256, created_at FROM transcripts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    try:
        transcript = json.loads(transcript_row["payload"]) if transcript_row else []
    except (TypeError, ValueError):
        transcript = []
    transcript_sha = transcript_row["sha256"] if transcript_row else None
    transcript_state = transcript_integrity(transcript_sha, transcript if transcript_row else None)

    records = db.rows_to_dicts(
        conn.execute(
            "SELECT * FROM score_records WHERE attempt_id = ? ORDER BY item_id", (attempt_id,)
        ).fetchall()
    )
    record_ids = [row["record_id"] for row in records]
    actions = []
    if record_ids:
        placeholders = ",".join("?" * len(record_ids))
        actions = db.rows_to_dicts(
            conn.execute(
                f"SELECT * FROM review_actions WHERE record_id IN ({placeholders}) ORDER BY created_at, rowid",
                record_ids,
            ).fetchall()
        )
    profile_row = conn.execute(
        "SELECT gender, origin, language, created_at FROM candidate_profiles WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()

    object_ids = [attempt_id, bundle["bundle_id"], *record_ids]
    for action in actions:
        object_ids.append(action["action_id"])
    placeholders = ",".join("?" * len(object_ids))
    audit_events = db.rows_to_dicts(
        conn.execute(
            f"SELECT * FROM audit_events WHERE object_id IN ({placeholders}) ORDER BY rowid",
            object_ids,
        ).fetchall()
    )

    package: dict[str, Any] = {
        "schema_version": "osce-loop-appeal-v1",
        "generated_at": db.now_iso(),
        "exported_by": exported_by,
        "attempt": attempt,
        "bundle": {
            "bundle_id": bundle["bundle_id"],
            "station_id": bundle["station_id"],
            "title": bundle["title"],
            "version": bundle["version"],
            "bundle_hash": bundle["bundle_hash"],
            "status": bundle["status"],
            "frozen_at": bundle["frozen_at"],
            "supersedes": bundle["supersedes"],
            "reason": bundle["reason"],
            "payload": bundle["payload"],
            "derived": bundle["derived"],
            "validation": bundle["validation"],
        },
        "transcript": {
            "created_at": transcript_row["created_at"] if transcript_row else None,
            "sha256": transcript_sha,
            "integrity": transcript_state,
            "utterances": transcript,
        },
        "score_records": records,
        "review_actions": actions,
        "audit_events": audit_events,
        "candidate_profile": dict(profile_row) if profile_row else None,
        "integrity": {
            "media_url": attempt.get("media_url"),
            "media_purged_at": attempt.get("media_purged_at"),
            "retention_class": attempt.get("retention_class"),
            "retain_until": attempt.get("retain_until"),
            "integrity_gap": bool(attempt.get("integrity_gap")),
            "integrity_note": attempt.get("integrity_note"),
        },
    }
    digest = package_hash(package)
    event_id = db.append_audit(
        conn,
        actor=exported_by,
        action="attempt.export",
        object_type="StationAttempt",
        object_id=attempt_id,
        before=None,
        after={
            "package_hash": digest,
            "records": len(records),
            "review_actions": len(actions),
            "audit_events": len(audit_events),
        },
    )
    return {"package_hash": digest, "package": package, "audit_event_id": event_id}
