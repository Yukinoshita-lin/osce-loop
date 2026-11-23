"""SQLite 数据层：只做存取，业务规则放在 domain 中。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS bundles (
    bundle_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    title TEXT NOT NULL,
    version TEXT NOT NULL,
    bundle_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    derived TEXT NOT NULL,
    validation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    supersedes TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL,
    candidate_code TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    session_id TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    media_id TEXT,
    media_url TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    integrity_gap INTEGER NOT NULL DEFAULT 0,
    integrity_note TEXT,
    appeal INTEGER NOT NULL DEFAULT 0,
    asr_confidence REAL,
    status TEXT NOT NULL DEFAULT 'scored',
    ai_total REAL,
    near_cutoff INTEGER NOT NULL DEFAULT 0,
    ab_group TEXT NOT NULL DEFAULT 'ai_assisted',
    risk_escalated_at TEXT,
    risk_escalation_reason TEXT,
    media_filename TEXT,
    media_size_bytes INTEGER NOT NULL DEFAULT 0,
    retention_class TEXT,
    retain_until TEXT,
    media_purged_at TEXT,
    media_purged_by TEXT,
    signed_off_by TEXT,
    signed_off_at TEXT,
    published_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcripts (
    attempt_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score_records (
    record_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    ai_score INTEGER,
    ai_confidence REAL,
    ai_evidence TEXT,
    ai_rationale TEXT,
    routing TEXT NOT NULL,
    gold_score INTEGER,
    status TEXT NOT NULL,
    final_score INTEGER,
    reviewer TEXT,
    reviewed_at TEXT,
    model_version TEXT,
    prompt_version TEXT,
    rubric_version TEXT,
    asr_confidence REAL,
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, item_id)
);

CREATE TABLE IF NOT EXISTS review_actions (
    action_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    action TEXT NOT NULL,
    from_score INTEGER,
    to_score INTEGER,
    reason_code TEXT,
    seconds_spent REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    before TEXT,
    after TEXT,
    model_version TEXT,
    prompt_version TEXT,
    prev_hash TEXT,
    event_hash TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS authority_overrides (
    bundle_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    authority TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bundle_id, item_id)
);

CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_cards (
    card_id TEXT PRIMARY KEY,
    capability TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    metrics TEXT NOT NULL,
    limitations TEXT,
    is_public INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_calls (
    call_id TEXT PRIMARY KEY,
    capability TEXT NOT NULL,
    model_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    subject_ref TEXT,
    latency_ms REAL NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sp_sessions (
    session_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL,
    candidate_label TEXT,
    state TEXT NOT NULL,
    transcript TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sp_certifications (
    cert_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL,
    persona_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sp_reports (
    session_id TEXT PRIMARY KEY,
    bundle_id TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_profiles (
    attempt_id TEXT PRIMARY KEY,
    gender TEXT,
    origin TEXT,
    language TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ab_reviews (
    review_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    score INTEGER NOT NULL,
    reviewer TEXT,
    seconds_spent REAL,
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, item_id)
);

CREATE TABLE IF NOT EXISTS governance_checks (
    check_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    trigger_summary TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS switch_drills (
    drill_id TEXT PRIMARY KEY,
    duration_ms REAL NOT NULL,
    passed INTEGER NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeal_drills (
    drill_id TEXT PRIMARY KEY,
    attempt_id TEXT,
    steps TEXT NOT NULL,
    passed INTEGER NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS governance_approvals (
    approval_id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    approver TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS data_sheets (
    data_sheet_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    content TEXT NOT NULL,
    is_public INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule_stations (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    difficulty REAL NOT NULL,
    room_type TEXT,
    equipment TEXT,
    sp_type TEXT,
    examiner_type TEXT
);

CREATE TABLE IF NOT EXISTS schedule_candidates (
    candidate_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    extra_time_min INTEGER NOT NULL DEFAULT 0,
    quiet_room INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS exam_schedules (
    schedule_id TEXT PRIMARY KEY,
    exam_date TEXT NOT NULL,
    params TEXT NOT NULL,
    schedule TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exam_runs (
    run_id TEXT PRIMARY KEY,
    schedule_id TEXT,
    exam_date TEXT,
    status TEXT NOT NULL,
    current_round INTEGER NOT NULL DEFAULT 1,
    hints_enabled INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exam_run_slots (
    run_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    candidate_id TEXT,
    candidate_name TEXT,
    round INTEGER NOT NULL,
    station_id TEXT,
    station_name TEXT,
    room TEXT,
    sp TEXT,
    examiner TEXT,
    start_min INTEGER,
    end_min INTEGER,
    status TEXT NOT NULL DEFAULT 'waiting',
    note TEXT,
    hints TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, slot_key)
);

CREATE TABLE IF NOT EXISTS exam_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    slot_key TEXT,
    payload TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    candidate_code TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """兼容已存在的演示数据库：SQLite 无法通过 CREATE TABLE IF NOT EXISTS 加列。"""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    additions = {
        "ab_group": "TEXT NOT NULL DEFAULT 'ai_assisted'",
        "risk_escalated_at": "TEXT",
        "risk_escalation_reason": "TEXT",
        "media_filename": "TEXT",
        "media_size_bytes": "INTEGER NOT NULL DEFAULT 0",
        "retention_class": "TEXT",
        "retain_until": "TEXT",
        "media_purged_at": "TEXT",
        "media_purged_by": "TEXT",
    }
    for column, ddl in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE attempts ADD COLUMN {column} {ddl}")

    card_columns = {row["name"] for row in conn.execute("PRAGMA table_info(model_cards)").fetchall()}
    if "is_public" not in card_columns:
        conn.execute("ALTER TABLE model_cards ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
    if "published_at" not in card_columns:
        conn.execute("ALTER TABLE model_cards ADD COLUMN published_at TEXT")

    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "candidate_code" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN candidate_code TEXT")

    audit_columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_events)").fetchall()}
    if "prev_hash" not in audit_columns:
        conn.execute("ALTER TABLE audit_events ADD COLUMN prev_hash TEXT")
    if "event_hash" not in audit_columns:
        conn.execute("ALTER TABLE audit_events ADD COLUMN event_hash TEXT")
    missing = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event_hash IS NULL"
    ).fetchone()["n"]
    if missing:
        _backfill_audit_chain(conn)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM system_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO system_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def _audit_event_hash(fields: dict[str, Any], prev_hash: str) -> str:
    payload = {**fields, "prev_hash": prev_hash}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _backfill_audit_chain(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM audit_events ORDER BY rowid").fetchall()
    prev_hash = ""
    for row in rows:
        fields = {
            "event_id": row["event_id"],
            "actor": row["actor"],
            "action": row["action"],
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "before": row["before"],
            "after": row["after"],
            "model_version": row["model_version"],
            "prompt_version": row["prompt_version"],
            "created_at": row["created_at"],
        }
        event_hash = _audit_event_hash(fields, prev_hash)
        conn.execute(
            "UPDATE audit_events SET prev_hash = ?, event_hash = ? WHERE event_id = ?",
            (prev_hash, event_hash, row["event_id"]),
        )
        prev_hash = event_hash
    conn.commit()


def verify_audit_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM audit_events ORDER BY rowid").fetchall()
    prev_hash = ""
    for index, row in enumerate(rows, start=1):
        if (row["prev_hash"] or "") != prev_hash:
            return {
                "ok": False,
                "total": len(rows),
                "broken_at": row["event_id"],
                "index": index,
                "message": "prev_hash 与上一条事件不一致",
            }
        fields = {
            "event_id": row["event_id"],
            "actor": row["actor"],
            "action": row["action"],
            "object_type": row["object_type"],
            "object_id": row["object_id"],
            "before": row["before"],
            "after": row["after"],
            "model_version": row["model_version"],
            "prompt_version": row["prompt_version"],
            "created_at": row["created_at"],
        }
        expected = _audit_event_hash(fields, prev_hash)
        if (row["event_hash"] or "") != expected:
            return {
                "ok": False,
                "total": len(rows),
                "broken_at": row["event_id"],
                "index": index,
                "message": "事件内容与哈希不一致，疑似被篡改",
            }
        prev_hash = row["event_hash"]
    return {
        "ok": True,
        "total": len(rows),
        "broken_at": None,
        "index": None,
        "message": "审计链完整",
    }


def append_audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    object_type: str,
    object_id: str,
    before: Any = None,
    after: Any = None,
    model_version: str | None = None,
    prompt_version: str | None = None,
) -> str:
    event_id = new_id("ev")
    timestamp = now_iso()
    before_json = json.dumps(before, ensure_ascii=False) if before is not None else None
    after_json = json.dumps(after, ensure_ascii=False) if after is not None else None
    last = conn.execute(
        "SELECT event_hash FROM audit_events ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    prev_hash = (last["event_hash"] or "") if last else ""
    fields = {
        "event_id": event_id,
        "actor": actor,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "before": before_json,
        "after": after_json,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "created_at": timestamp,
    }
    event_hash = _audit_event_hash(fields, prev_hash)
    conn.execute(
        "INSERT INTO audit_events(event_id, actor, action, object_type, object_id, before, after, "
        "model_version, prompt_version, prev_hash, event_hash, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            actor,
            action,
            object_type,
            object_id,
            before_json,
            after_json,
            model_version,
            prompt_version,
            prev_hash,
            event_hash,
            timestamp,
        ),
    )
    conn.commit()
    return event_id


def log_ai_call(
    conn: sqlite3.Connection,
    *,
    capability: str,
    model_version: str,
    prompt_version: str,
    subject_ref: str | None,
    latency_ms: float,
    status: str,
    detail: Any = None,
) -> None:
    conn.execute(
        "INSERT INTO ai_calls(call_id, capability, model_version, prompt_version, subject_ref, "
        "latency_ms, status, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            new_id("call"),
            capability,
            model_version,
            prompt_version,
            subject_ref,
            round(latency_ms, 2),
            status,
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
            now_iso(),
        ),
    )
    conn.commit()
