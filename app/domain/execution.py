"""考试执行台：运行状态机、事实层提示、异常处置与终止。"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import db
from .anomaly import detect_anomalies
from .scheduling import latest_schedule

STATION_HINTS: dict[str, list[str]] = {
    "急性胸痛": [
        "尚未观察到询问放射部位",
        "距轮次结束还有 2 分钟，以下 2 项尚无证据",
        "设备音频信号正常",
    ],
    "呼吸困难": [
        "尚未观察到询问起病方式与诱因",
        "转写置信度正常",
        "设备音频信号正常",
    ],
    "意识障碍": [
        "尚未观察到意识状态评估",
        "同传延迟 0.4 秒",
        "设备音频信号正常",
    ],
    "急性腹痛": [
        "尚未观察到询问疼痛部位与性质",
        "转写置信度正常",
        "设备音频信号正常",
    ],
    "发热待查": [
        "尚未观察到询问流行病学史",
        "设备音频信号正常",
    ],
    "用药咨询": [
        "尚未观察到核对用药清单",
        "处方系统连接正常",
    ],
    "知情同意": [
        "尚未观察到说明替代方案",
        "知情同意书已就位",
    ],
    "坏消息告知": [
        "尚未观察到确认患者理解",
        "设备音频信号正常",
    ],
    "交接班": [
        "尚未观察到使用结构化交接框架",
        "病历系统连接正常",
    ],
    "出院指导": [
        "尚未观察到复述用药计划",
        "出院小结已就位",
    ],
}
DEFAULT_HINTS = ["尚未观察到关键证据", "转写置信度正常", "设备状态正常"]
ACTIVE_RUN_STATUSES = ("running", "paused")


def _slot_key(candidate_id: str, round_index: int) -> str:
    return f"{candidate_id}__{round_index}"


def _event(conn, run_id: str, event_type: str, message: str, *, severity: str = "info", slot_key: str | None = None, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO exam_events(event_id, run_id, event_type, severity, message, slot_key, payload, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            db.new_id("eev"),
            run_id,
            event_type,
            severity,
            message,
            slot_key,
            json.dumps(payload, ensure_ascii=False) if payload else None,
            db.now_iso(),
        ),
    )


def start_run(conn, *, schedule_id: str | None = None, actor: str = "考务") -> dict[str, Any]:
    active = conn.execute(
        "SELECT * FROM exam_runs WHERE status IN ('running','paused') ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if active:
        raise ValueError("已存在进行中的考试执行")
    schedule = None
    if schedule_id:
        row = conn.execute(
            "SELECT schedule FROM exam_schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        if row:
            schedule = json.loads(row["schedule"])
    if not schedule:
        schedule = latest_schedule(conn)
    if not schedule:
        raise ValueError("尚无排考表，请先生成考务编排")

    run_id = db.new_id("run")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO exam_runs(run_id, schedule_id, exam_date, status, current_round, hints_enabled, "
        "started_at, ended_at, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            schedule["schedule_id"],
            schedule.get("exam_date"),
            "running",
            1,
            1,
            timestamp,
            None,
            timestamp,
        ),
    )
    slot_count = 0
    for round_row in schedule.get("rounds_detail", []):
        round_index = int(round_row["round"])
        for slot in round_row.get("slots", []):
            hints = STATION_HINTS.get(slot.get("station_name"), DEFAULT_HINTS)
            conn.execute(
                "INSERT INTO exam_run_slots(run_id, slot_key, candidate_id, candidate_name, round, station_id, "
                "station_name, room, sp, examiner, start_min, end_min, status, note, hints, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    _slot_key(slot["candidate_id"], round_index),
                    slot["candidate_id"],
                    slot["candidate_name"],
                    round_index,
                    slot["station_id"],
                    slot["station_name"],
                    slot["room"],
                    slot["sp"],
                    slot["examiner"],
                    slot["start_min"],
                    slot["end_min"],
                    "in_progress" if round_index == 1 else "waiting",
                    None,
                    json.dumps(hints, ensure_ascii=False),
                    timestamp,
                ),
            )
            slot_count += 1
    _event(conn, run_id, "run_started", f"考试执行开始，共 {slot_count} 个考站槽位", payload={"schedule_id": schedule["schedule_id"]})
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.run_start",
        object_type="ExamRun",
        object_id=run_id,
        before=None,
        after={"schedule_id": schedule["schedule_id"], "slots": slot_count},
    )
    return get_run(conn, run_id)


def _evidence_facts(payload: dict[str, Any], transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """按转写中的考生发言统计已有/缺失证据，只输出事实计数，不输出判断。"""
    candidate_lines = [
        str(utterance.get("text") or "")
        for utterance in transcript
        if utterance.get("speaker") == "candidate"
    ]
    candidate_text = "\n".join(candidate_lines)
    measured = 0
    covered = 0
    missing_critical = 0
    for item in payload.get("rubric") or []:
        if item.get("evidence_type") == "global":
            continue
        probe = item.get("probe") or {}
        patterns = [
            pattern
            for pattern in [*(probe.get("positive") or []), *(probe.get("partial") or [])]
            if pattern
        ]
        if not patterns:
            continue
        measured += 1
        matched = False
        for pattern in patterns:
            try:
                if re.search(pattern, candidate_text):
                    matched = True
                    break
            except re.error:
                continue
        if matched:
            covered += 1
        elif item.get("critical"):
            missing_critical += 1
    missing = measured - covered
    facts = [
        f"已采集候选发言 {len(candidate_lines)} 段",
        f"尚无证据的评分项 {missing} 项",
    ]
    if missing_critical:
        facts.append(f"其中关键项 {missing_critical} 项尚无证据")
    return {
        "utterances": len(candidate_lines),
        "measured": measured,
        "covered": covered,
        "missing": missing,
        "missing_critical": missing_critical,
        "facts": facts,
    }


def _live_segments(conn, run_id: str, slot_key: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT speaker, start_ms, end_ms, text, asr_confidence "
            "FROM exam_run_live_segments WHERE run_id = ? AND slot_key = ? "
            "ORDER BY start_ms, rowid",
            (run_id, slot_key),
        ).fetchall()
    ]


def live_segments_for_slot(conn, run_id: str, slot_key: str) -> list[dict[str, Any]]:
    return _live_segments(conn, run_id, slot_key)


def _bundle_payload_for_station(
    conn, station_id: str, station_name: str | None = None
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM bundles WHERE station_id = ? "
        "ORDER BY frozen_at DESC, rowid DESC LIMIT 1",
        (station_id,),
    ).fetchone()
    if not row and station_name:
        row = conn.execute(
            "SELECT payload FROM bundles WHERE title LIKE ? "
            "ORDER BY frozen_at DESC, rowid DESC LIMIT 1",
            (f"%{station_name}%",),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _acknowledged_live_types(conn, run_id: str, slot_key: str) -> set[str]:
    types: set[str] = set()
    for row in conn.execute(
        "SELECT payload FROM exam_events WHERE run_id = ? AND event_type = 'live_anomaly_ack' "
        "AND slot_key = ?",
        (run_id, slot_key),
    ).fetchall():
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        types.update(payload.get("types") or [])
    return types


def _record_live_anomalies(
    conn, run_id: str, slot: dict[str, Any], anomalies: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """同一槽位同类异常只记录一次，避免实时回传重复告警。"""
    seen: set[str] = set()
    for row in conn.execute(
        "SELECT payload FROM exam_events WHERE run_id = ? AND event_type = 'live_anomaly' "
        "AND slot_key = ?",
        (run_id, slot["slot_key"]),
    ).fetchall():
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        kind = payload.get("type")
        if kind:
            seen.add(kind)
    recorded: list[dict[str, Any]] = []
    severity_map = {"high": "critical", "medium": "warn"}
    for anomaly in anomalies:
        kind = anomaly.get("type")
        if not kind or kind in seen:
            continue
        seen.add(kind)
        _event(
            conn,
            run_id,
            "live_anomaly",
            f"{slot['candidate_name']} · {slot['station_name']} 实时异常：{anomaly.get('label') or kind}",
            severity=severity_map.get(str(anomaly.get("severity")), "info"),
            slot_key=slot["slot_key"],
            payload={
                "slot_key": slot["slot_key"],
                "type": kind,
                "at_ms": anomaly.get("at_ms"),
                "detail": anomaly.get("detail"),
            },
        )
        recorded.append(anomaly)
    return recorded


def live_facts(conn, run_id: str, slot_key: str) -> dict[str, Any] | None:
    run = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("考试执行不存在")
    slot = conn.execute(
        "SELECT * FROM exam_run_slots WHERE run_id = ? AND slot_key = ?", (run_id, slot_key)
    ).fetchone()
    if not slot:
        raise ValueError("考站槽位不存在")
    payload = _bundle_payload_for_station(conn, slot["station_id"], slot["station_name"])
    segments = _live_segments(conn, run_id, slot_key)
    facts = _evidence_facts(payload, segments) if payload else None
    duration_ms = max((int(segment.get("end_ms") or 0) for segment in segments), default=0)
    anomalies = detect_anomalies(
        segments,
        duration_ms=duration_ms,
        station_duration_min=(payload or {}).get("duration_min"),
    )
    acknowledged = _acknowledged_live_types(conn, run_id, slot_key)
    anomalies = [item for item in anomalies if item.get("type") not in acknowledged]
    return {
        "slot_key": slot_key,
        "segment_count": len(segments),
        "facts": facts,
        "anomalies": anomalies,
    }


def append_live_segment(
    conn,
    run_id: str,
    slot_key: str,
    *,
    speaker: str,
    text: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    asr_confidence: float | None = None,
    actor: str = "考务",
) -> dict[str, Any]:
    """接收外部 ASR 回传的实时转写片段，并立即返回事实层证据覆盖。"""
    run = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("考试执行不存在")
    if run["status"] not in ACTIVE_RUN_STATUSES:
        raise ValueError("考试执行未在进行中")
    slot = conn.execute(
        "SELECT * FROM exam_run_slots WHERE run_id = ? AND slot_key = ?", (run_id, slot_key)
    ).fetchone()
    if not slot:
        raise ValueError("考站槽位不存在")
    speaker = (speaker or "").strip()
    if speaker not in ("candidate", "patient"):
        raise ValueError("说话人必须是 candidate 或 patient")
    text = (text or "").strip()
    if not text:
        raise ValueError("转写文本不能为空")
    if len(text) > 500:
        raise ValueError("单条转写文本不能超过 500 字")
    if asr_confidence is not None:
        confidence = float(asr_confidence)
        if confidence < 0 or confidence > 1:
            raise ValueError("ASR 置信度必须在 0–1 之间")
    else:
        confidence = None
    last = conn.execute(
        "SELECT MAX(end_ms) AS last_end FROM exam_run_live_segments WHERE run_id = ? AND slot_key = ?",
        (run_id, slot_key),
    ).fetchone()
    start = int(start_ms) if start_ms is not None else int((last["last_end"] or 0) + 500)
    end = (
        int(end_ms)
        if end_ms is not None
        else start + max(1000, min(15000, len(text) * 180))
    )
    if start < 0 or end <= start:
        raise ValueError("时间戳必须满足 0 ≤ start_ms < end_ms")
    segment_id = db.new_id("lseg")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO exam_run_live_segments(segment_id, run_id, slot_key, speaker, start_ms, "
        "end_ms, text, asr_confidence, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (segment_id, run_id, slot_key, speaker, start, end, text, confidence, timestamp),
    )
    role_label = "考生" if speaker == "candidate" else "患者"
    _event(
        conn,
        run_id,
        "live_segment",
        f"{slot['candidate_name']} · {slot['station_name']} 收到{role_label}转写片段",
        payload={"slot_key": slot_key, "speaker": speaker, "segment_id": segment_id},
    )
    facts = live_facts(conn, run_id, slot_key)
    new_anomalies = _record_live_anomalies(
        conn, run_id, dict(slot), (facts or {}).get("anomalies") or []
    )
    conn.commit()
    return {
        "ok": True,
        "segment": {
            "segment_id": segment_id,
            "slot_key": slot_key,
            "speaker": speaker,
            "start_ms": start,
            "end_ms": end,
            "text": text,
            "asr_confidence": confidence,
            "actor": actor,
            "created_at": timestamp,
        },
        "facts": facts,
        "new_anomalies": new_anomalies,
    }


def acknowledge_live_anomalies(
    conn, run_id: str, slot_key: str, *, note: str, actor: str = "考务"
) -> dict[str, Any]:
    note = (note or "").strip()
    if len(note) < 4:
        raise ValueError("实时异常处置说明不少于 4 字")
    if len(note) > 200:
        raise ValueError("实时异常处置说明不能超过 200 字")
    run = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("考试执行不存在")
    slot = conn.execute(
        "SELECT * FROM exam_run_slots WHERE run_id = ? AND slot_key = ?", (run_id, slot_key)
    ).fetchone()
    if not slot:
        raise ValueError("考站槽位不存在")
    facts = live_facts(conn, run_id, slot_key) or {}
    anomalies = facts.get("anomalies") or []
    if not anomalies:
        raise ValueError("当前没有待处置的实时异常")
    types = sorted({item.get("type") for item in anomalies if item.get("type")})
    _event(
        conn,
        run_id,
        "live_anomaly_ack",
        f"{slot['candidate_name']} · {slot['station_name']} 实时异常已处置：{note}",
        severity="info",
        slot_key=slot_key,
        payload={"slot_key": slot_key, "types": types, "note": note, "actor": actor},
    )
    db.append_audit(
        conn,
        actor=actor,
        action="exam.live_anomaly_ack",
        object_type="ExamRunSlot",
        object_id=f"{run_id}:{slot_key}",
        before={"anomalies": anomalies},
        after={"types": types, "note": note},
    )
    conn.commit()
    return {
        "ok": True,
        "acknowledged": anomalies,
        "facts": live_facts(conn, run_id, slot_key),
    }


def get_run(conn, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row:
        raise ValueError("考试执行不存在")
    run = dict(row)
    slots = []
    overrides = {
        row["station_id"]: bool(row["enabled"])
        for row in conn.execute(
            "SELECT * FROM exam_run_hint_overrides WHERE run_id = ?", (run_id,)
        ).fetchall()
    }
    for slot in conn.execute(
        "SELECT * FROM exam_run_slots WHERE run_id = ? ORDER BY round, candidate_id", (run_id,)
    ).fetchall():
        data = dict(slot)
        try:
            data["hints"] = json.loads(data.get("hints") or "[]")
        except (TypeError, ValueError):
            data["hints"] = []
        data["hints_override"] = overrides.get(data.get("station_id"))
        data["hints_enabled"] = bool(run["hints_enabled"]) and overrides.get(
            data.get("station_id"), True
        )
        slots.append(data)

    attempt_rows = {
        attempt["attempt_id"]: dict(attempt)
        for attempt in conn.execute(
            "SELECT * FROM attempts WHERE run_id = ?", (run_id,)
        ).fetchall()
    }
    attempt_ids = [slot["attempt_id"] for slot in slots if slot.get("attempt_id")]
    bundle_payloads: dict[str, dict[str, Any]] = {}
    bundle_ids = {row["bundle_id"] for row in attempt_rows.values()}
    if bundle_ids:
        placeholders = ",".join("?" * len(bundle_ids))
        for row in conn.execute(
            f"SELECT bundle_id, payload FROM bundles WHERE bundle_id IN ({placeholders})",
            tuple(bundle_ids),
        ).fetchall():
            try:
                bundle_payloads[row["bundle_id"]] = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
    transcripts: dict[str, list[dict[str, Any]]] = {}
    if attempt_ids:
        placeholders = ",".join("?" * len(attempt_ids))
        for row in conn.execute(
            f"SELECT attempt_id, payload FROM transcripts WHERE attempt_id IN ({placeholders})",
            tuple(attempt_ids),
        ).fetchall():
            try:
                parsed = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, list):
                transcripts[row["attempt_id"]] = parsed
    pending_by_attempt: dict[str, int] = {}
    if attempt_ids:
        placeholders = ",".join("?" * len(attempt_ids))
        pending_by_attempt = {
            pending["attempt_id"]: pending["n"]
            for pending in conn.execute(
                "SELECT attempt_id, COUNT(*) AS n FROM score_records "
                f"WHERE attempt_id IN ({placeholders}) "
                "AND status IN ('pending_review','manual_pending') GROUP BY attempt_id",
                tuple(attempt_ids),
            ).fetchall()
        }
    station_payloads: dict[str, dict[str, Any]] = {}
    title_payloads: list[tuple[str, dict[str, Any]]] = []
    for row in conn.execute(
        "SELECT station_id, title, payload FROM bundles ORDER BY frozen_at DESC, rowid DESC"
    ).fetchall():
        try:
            parsed = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        station_payloads.setdefault(row["station_id"], parsed)
        title_payloads.append((row["title"] or "", parsed))
    live_by_slot: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT slot_key, speaker, start_ms, end_ms, text, asr_confidence "
        "FROM exam_run_live_segments WHERE run_id = ? ORDER BY start_ms, rowid",
        (run_id,),
    ).fetchall():
        live_by_slot.setdefault(row["slot_key"], []).append(dict(row))
    ack_types_by_slot: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT slot_key, payload FROM exam_events WHERE run_id = ? "
        "AND event_type = 'live_anomaly_ack'",
        (run_id,),
    ).fetchall():
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        ack_types_by_slot.setdefault(row["slot_key"], set()).update(payload.get("types") or [])
    for slot in slots:
        attempt = attempt_rows.get(slot.get("attempt_id") or "")
        slot["attempt"] = (
            {
                "attempt_id": attempt["attempt_id"],
                "candidate_code": attempt["candidate_code"],
                "candidate_name": attempt["candidate_name"],
                "status": attempt["status"],
                "pending": pending_by_attempt.get(attempt["attempt_id"], 0),
                "media_url": attempt["media_url"],
                "integrity_gap": bool(attempt["integrity_gap"]),
            }
            if attempt
            else None
        )
        payload = bundle_payloads.get(attempt["bundle_id"]) if attempt else None
        slot["evidence_facts"] = (
            _evidence_facts(payload, transcripts.get(attempt["attempt_id"], []))
            if payload and attempt
            else None
        )
        live_segments = live_by_slot.get(slot["slot_key"], [])
        slot["live_segment_count"] = len(live_segments)
        slot["live_anomalies"] = []
        station_payload = station_payloads.get(slot["station_id"])
        if station_payload is None:
            station_name = slot.get("station_name") or ""
            if station_name:
                station_payload = next(
                    (payload for title, payload in title_payloads if station_name in title),
                    None,
                )
        if live_segments:
            if slot["evidence_facts"] is None and station_payload:
                slot["evidence_facts"] = _evidence_facts(station_payload, live_segments)
            live_duration_ms = max(
                (int(segment.get("end_ms") or 0) for segment in live_segments), default=0
            )
            slot["live_anomalies"] = detect_anomalies(
                live_segments,
                duration_ms=live_duration_ms,
                station_duration_min=(station_payload or {}).get("duration_min"),
            )
            acknowledged = ack_types_by_slot.get(slot["slot_key"], set())
            slot["live_anomalies"] = [
                item for item in slot["live_anomalies"] if item.get("type") not in acknowledged
            ]
    events = []
    for event in conn.execute(
        "SELECT * FROM exam_events WHERE run_id = ? ORDER BY rowid DESC LIMIT 120", (run_id,)
    ).fetchall():
        data = dict(event)
        try:
            data["payload"] = json.loads(data["payload"]) if data.get("payload") else None
        except (TypeError, ValueError):
            data["payload"] = None
        events.append(data)
    counts = {
        "total": len(slots),
        "ingested": sum(1 for s in slots if s.get("attempt_id")),
        "in_progress": sum(1 for s in slots if s["status"] == "in_progress"),
        "completed": sum(1 for s in slots if s["status"] == "completed"),
        "waiting": sum(1 for s in slots if s["status"] == "waiting"),
        "anomaly": sum(1 for s in slots if s["status"] == "anomaly"),
        "terminated": sum(1 for s in slots if s["status"] == "terminated"),
        "stopped": sum(1 for s in slots if s["status"] == "stopped"),
    }
    max_round = max((s["round"] for s in slots), default=1)
    ingested = counts["ingested"]
    integrity = {
        "ingested": ingested,
        "missing": max(0, len(slots) - ingested),
        "coverage": round(ingested / len(slots), 3) if slots else 0.0,
        "completed_without_attempt": sum(
            1 for slot in slots if slot["status"] == "completed" and not slot.get("attempt_id")
        ),
        "media_gap": sum(
            1 for slot in slots if slot.get("attempt") and slot["attempt"]["integrity_gap"]
        ),
    }
    return {
        **run,
        "hints_enabled": bool(run["hints_enabled"]),
        "hint_overrides": overrides,
        "max_round": max_round,
        "slots": slots,
        "events": events,
        "counts": counts,
        "integrity": integrity,
    }


def latest_run(conn) -> dict[str, Any] | None:
    row = conn.execute("SELECT run_id FROM exam_runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get_run(conn, row["run_id"]) if row else None


def advance_run(conn, run_id: str, *, actor: str = "考务") -> dict[str, Any]:
    run = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("考试执行不存在")
    if run["status"] != "running":
        raise ValueError("只有进行中的考试可以推进轮次")
    current = int(run["current_round"])
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE exam_run_slots SET status = 'completed', updated_at = ? WHERE run_id = ? AND round = ? "
        "AND status IN ('in_progress','waiting')",
        (timestamp, run_id, current),
    )
    max_round = conn.execute(
        "SELECT MAX(round) AS m FROM exam_run_slots WHERE run_id = ?", (run_id,)
    ).fetchone()["m"] or current
    if current >= max_round:
        conn.execute(
            "UPDATE exam_runs SET status = 'completed', ended_at = ? WHERE run_id = ?",
            (timestamp, run_id),
        )
        missing = conn.execute(
            "SELECT COUNT(*) AS n FROM exam_run_slots WHERE run_id = ? AND attempt_id IS NULL",
            (run_id,),
        ).fetchone()["n"]
        if missing:
            _event(
                conn,
                run_id,
                "run_integrity_gap",
                f"考试结束：{missing} 个槽位未关联场次，归档前需补齐或标记异常",
                severity="warn",
            )
        _event(conn, run_id, "run_completed", "全部轮次完成，考试执行结束", severity="info")
    else:
        next_round = current + 1
        conn.execute(
            "UPDATE exam_runs SET current_round = ? WHERE run_id = ?", (next_round, run_id)
        )
        conn.execute(
            "UPDATE exam_run_slots SET status = 'in_progress', updated_at = ? WHERE run_id = ? AND round = ? "
            "AND status = 'waiting'",
            (timestamp, run_id, next_round),
        )
        _event(conn, run_id, "round_advanced", f"进入第 {next_round} 轮", payload={"round": next_round})
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.run_advance",
        object_type="ExamRun",
        object_id=run_id,
        after={"round": current + 1},
    )
    return get_run(conn, run_id)


def slot_action(
    conn,
    run_id: str,
    slot_key: str,
    *,
    action: str,
    note: str = "",
    actor: str = "考务",
    compensation: str | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM exam_run_slots WHERE run_id = ? AND slot_key = ?", (run_id, slot_key)
    ).fetchone()
    if not row:
        raise ValueError("考站槽位不存在")
    compensation = (compensation or "").strip() or None
    compensation_labels = {"extend": "延时补时", "retake": "另行安排重考", "exclude": "不计入本次成绩"}
    if action == "complete":
        status = "completed"
        event_type = "slot_completed"
        severity = "info"
        message = f"{row['candidate_name']} · {row['station_name']} 完成"
    elif action == "anomaly":
        status = "anomaly"
        event_type = "slot_anomaly"
        severity = "warn"
        message = f"{row['candidate_name']} · {row['station_name']} 异常：{note or '未填写说明'}"
    elif action == "integrity_gap":
        status = "anomaly"
        event_type = "integrity_gap"
        severity = "critical"
        message = f"{row['candidate_name']} · {row['station_name']} 媒体完整性缺口：{note or '已标记'}"
    elif action == "terminate":
        status = "terminated"
        event_type = "slot_terminated"
        severity = "critical"
        message = f"{row['candidate_name']} · {row['station_name']} 被人工终止：{note or '未填写理由'}"
    elif action == "compensate":
        if compensation not in compensation_labels:
            raise ValueError("补偿方式必须是 extend / retake / exclude")
        if not note.strip():
            raise ValueError("补偿判定必须填写说明")
        status = "compensated"
        event_type = "slot_compensated"
        severity = "warn"
        message = (
            f"{row['candidate_name']} · {row['station_name']} 补偿判定："
            f"{compensation_labels[compensation]}（{note}）"
        )
    else:
        raise ValueError("非法槽位操作")
    conn.execute(
        "UPDATE exam_run_slots SET status = ?, note = ?, compensation = ?, updated_at = ? "
        "WHERE run_id = ? AND slot_key = ?",
        (status, note, compensation, db.now_iso(), run_id, slot_key),
    )
    _event(
        conn,
        run_id,
        event_type,
        message,
        severity=severity,
        slot_key=slot_key,
        payload={"note": note, "actor": actor, "compensation": compensation},
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action=f"exam.{event_type}",
        object_type="ExamRunSlot",
        object_id=f"{run_id}:{slot_key}",
        after={"status": status, "note": note, "compensation": compensation},
    )
    return get_run(conn, run_id)


def slot_for_ingest(conn, run_id: str, slot_key: str) -> dict[str, Any]:
    """校验槽位可录入：存在、未录入、未终止。"""
    row = conn.execute(
        "SELECT * FROM exam_run_slots WHERE run_id = ? AND slot_key = ?", (run_id, slot_key)
    ).fetchone()
    if not row:
        raise ValueError("执行槽位不存在")
    slot = dict(row)
    if slot.get("attempt_id"):
        raise ValueError("该槽位已录入场次，不能重复录入")
    if slot["status"] in ("terminated", "stopped"):
        raise ValueError("该槽位已终止，不能录入场次")
    return slot


def link_attempt_to_slot(
    conn, *, run_id: str, slot_key: str, attempt_id: str, actor: str = "考务"
) -> None:
    """录入完成后回写槽位状态与场次关联，并写入事件与审计。"""
    slot = slot_for_ingest(conn, run_id, slot_key)
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE exam_run_slots SET status = 'completed', attempt_id = ?, note = ?, updated_at = ? "
        "WHERE run_id = ? AND slot_key = ?",
        (attempt_id, "已录入场次", timestamp, run_id, slot_key),
    )
    _event(
        conn,
        run_id,
        "slot_ingested",
        f"{slot['candidate_name']} · {slot['station_name']} 已录入场次",
        severity="info",
        slot_key=slot_key,
        payload={"attempt_id": attempt_id, "actor": actor},
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.slot_ingest",
        object_type="ExamRunSlot",
        object_id=f"{run_id}:{slot_key}",
        before={"status": slot["status"]},
        after={"status": "completed", "attempt_id": attempt_id},
    )


def toggle_hints(conn, run_id: str, *, enabled: bool, actor: str = "考务") -> dict[str, Any]:
    run = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("考试执行不存在")
    conn.execute("UPDATE exam_runs SET hints_enabled = ? WHERE run_id = ?", (1 if enabled else 0, run_id))
    _event(
        conn,
        run_id,
        "hints_toggled",
        f"事实层提示已{'开启' if enabled else '关闭'}（操作者：{actor}）",
        severity="info",
        payload={"enabled": enabled, "actor": actor},
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.hints_toggle",
        object_type="ExamRun",
        object_id=run_id,
        after={"enabled": enabled},
    )
    return get_run(conn, run_id)


def toggle_station_hints(
    conn, run_id: str, station_id: str, *, enabled: bool, actor: str = "考务"
) -> dict[str, Any]:
    """按站开关事实层提示；全局关闭优先，分站设置保留以便恢复。"""
    run = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("考试执行不存在")
    station = conn.execute(
        "SELECT station_name FROM exam_run_slots WHERE run_id = ? AND station_id = ? LIMIT 1",
        (run_id, station_id),
    ).fetchone()
    if not station:
        raise ValueError("该考站不在本次考试中")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO exam_run_hint_overrides(run_id, station_id, enabled, actor, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(run_id, station_id) DO UPDATE SET "
        "enabled = excluded.enabled, actor = excluded.actor, updated_at = excluded.updated_at",
        (run_id, station_id, 1 if enabled else 0, actor, timestamp),
    )
    _event(
        conn,
        run_id,
        "hints_station_toggled",
        f"{station['station_name']} 事实层提示已{'开启' if enabled else '关闭'}（操作者：{actor}）",
        severity="info",
        payload={"station_id": station_id, "enabled": enabled, "actor": actor},
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.hints_station_toggle",
        object_type="ExamRun",
        object_id=run_id,
        after={"station_id": station_id, "enabled": enabled},
    )
    return get_run(conn, run_id)


def stop_run(conn, run_id: str, *, reason: str = "人工终止", actor: str = "考务") -> dict[str, Any]:
    run = conn.execute("SELECT * FROM exam_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError("考试执行不存在")
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE exam_runs SET status = 'stopped', ended_at = ? WHERE run_id = ?", (timestamp, run_id)
    )
    conn.execute(
        "UPDATE exam_run_slots SET status = 'stopped', updated_at = ? WHERE run_id = ? AND status IN ('in_progress','waiting')",
        (timestamp, run_id),
    )
    _event(conn, run_id, "run_stopped", f"考试执行被终止：{reason}", severity="critical", payload={"reason": reason, "actor": actor})
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.run_stop",
        object_type="ExamRun",
        object_id=run_id,
        before={"status": run["status"]},
        after={"status": "stopped", "reason": reason},
    )
    return get_run(conn, run_id)
