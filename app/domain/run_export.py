"""考试执行归档包：运行状态、槽位、场次、事件与审计的哈希封装。"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from .execution import get_run
from .export import package_hash


def build_run_package(conn, run_id: str, *, exported_by: str) -> dict[str, Any]:
    run = get_run(conn, run_id)
    schedule_row = conn.execute(
        "SELECT schedule, created_at FROM exam_schedules WHERE schedule_id = ?",
        (run["schedule_id"],),
    ).fetchone()
    schedule = None
    if schedule_row:
        try:
            schedule = json.loads(schedule_row["schedule"])
        except (TypeError, ValueError):
            schedule = None
    attempts = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY created_at, attempt_id",
            (run_id,),
        ).fetchall()
    ]
    transcript_hashes = {
        row["attempt_id"]: row["sha256"]
        for row in conn.execute("SELECT attempt_id, sha256 FROM transcripts").fetchall()
    }
    for attempt in attempts:
        attempt["transcript_sha256"] = transcript_hashes.get(attempt["attempt_id"])
    live_segments = db.rows_to_dicts(
        conn.execute(
            "SELECT * FROM exam_run_live_segments WHERE run_id = ? "
            "ORDER BY slot_key, start_ms, rowid",
            (run_id,),
        ).fetchall()
    )

    object_ids = [run_id, *(f"{run_id}:{slot['slot_key']}" for slot in run["slots"])]
    placeholders = ",".join("?" * len(object_ids))
    audit_events = db.rows_to_dicts(
        conn.execute(
            f"SELECT * FROM audit_events WHERE object_id IN ({placeholders}) ORDER BY rowid",
            tuple(object_ids),
        ).fetchall()
    )
    package: dict[str, Any] = {
        "schema_version": "osce-loop-run-v1",
        "generated_at": db.now_iso(),
        "exported_by": exported_by,
        "run": {
            "run_id": run["run_id"],
            "schedule_id": run["schedule_id"],
            "exam_date": run["exam_date"],
            "status": run["status"],
            "current_round": run["current_round"],
            "max_round": run["max_round"],
            "started_at": run["started_at"],
            "ended_at": run["ended_at"],
            "counts": run["counts"],
            "integrity": run["integrity"],
            "live_segments": len(live_segments),
        },
        "schedule": schedule,
        "slots": run["slots"],
        "attempts": attempts,
        "live_segments": live_segments,
        "events": run["events"],
        "audit_events": audit_events,
    }
    digest = package_hash(package)
    db.append_audit(
        conn,
        actor=exported_by,
        action="exam.run_export",
        object_type="ExamRun",
        object_id=run_id,
        before=None,
        after={
            "package_hash": digest,
            "slots": run["counts"]["total"],
            "attempts": len(attempts),
            "live_segments": len(live_segments),
            "missing_attempts": run["integrity"]["missing"],
        },
    )
    return {"package_hash": digest, "package": package}
