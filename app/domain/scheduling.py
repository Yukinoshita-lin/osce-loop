"""考务编排：在约束下生成轮转排考表，并检查难度均衡、疲劳与特殊需求。"""
from __future__ import annotations

import json
import random
from collections import deque
from statistics import mean
from typing import Any

from .. import db

STATIONS = [
    ("ST01", "急性胸痛", 10, 0.90, "抢救室", "心电图机", "焦虑型", "急诊主治", 1),
    ("ST02", "呼吸困难", 10, 0.80, "抢救室", "监护仪", "焦虑型", "呼吸科主治", 1),
    ("ST03", "意识障碍", 12, 0.85, "抢救室", "监护仪", "认知障碍型", "神经科主治", 1),
    ("ST04", "急性腹痛", 10, 0.65, "普通诊室", "超声", "克制型", "普外科主治", 1),
    ("ST05", "发热待查", 8, 0.55, "普通诊室", "无", "配合型", "感染科主治", 1),
    ("ST06", "用药咨询", 8, 0.50, "诊室", "处方系统", "依赖型", "临床药师", 1),
    ("ST07", "知情同意", 10, 0.60, "谈话间", "知情同意书", "焦虑型", "主治医师", 0),
    ("ST08", "坏消息告知", 12, 0.90, "谈话间", "无", "情绪化型", "高年资医师", 0),
    ("ST09", "交接班", 10, 0.70, "示教室", "病历系统", "配合型", "主治医师", 0),
    ("ST10", "出院指导", 8, 0.50, "诊室", "出院小结", "依赖型", "住院医师", 1),
]

CANDIDATES = [
    ("C2026-001", "陈雨桐", 0, 0, "", 0, 0),
    ("C2026-002", "王一帆", 5, 0, "阅读速度较慢，每站延长 5 分钟", 0, 0),
    ("C2026-003", "李昊然", 0, 1, "需要安静候考区", 0, 0),
    ("C2026-004", "赵思远", 0, 0, "需无障碍通行，并使用单独进出通道", 1, 1),
    ("C2026-005", "周可欣", 5, 0, "语言表达需更多时间", 0, 0),
    ("C2026-006", "孙明哲", 0, 0, "", 0, 0),
    ("C2026-007", "吴嘉怡", 0, 1, "需要安静候考区", 0, 0),
    ("C2026-008", "郑子涵", 0, 0, "", 0, 0),
]

RESOURCE_COUNTS = {"rooms": 10, "sps": 10, "examiners": 10}
EQUIPMENT_COUNTS = {
    "心电图机": 1,
    "监护仪": 1,
    "超声": 1,
    "处方系统": 1,
    "知情同意书": 2,
    "病历系统": 1,
    "出院小结": 1,
}
FATIGUE_LIMIT_MIN = 240
MIN_BLACKOUT_REASON = 4
MAX_BLACKOUT_REASON = 200


def list_blackouts(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM schedule_blackouts ORDER BY from_round, station_id, created_at"
    ).fetchall()
    return [dict(row) for row in rows]


def add_blackout(
    conn,
    *,
    station_id: str,
    from_round: int,
    to_round: int,
    reason: str,
    actor: str = "考务编排",
) -> dict[str, Any]:
    """登记考站在若干轮次不可用（考官请假、场地占用等），生成排考时强制避开。"""
    station = conn.execute(
        "SELECT station_id FROM schedule_stations WHERE station_id = ?", (station_id,)
    ).fetchone()
    if not station:
        raise ValueError("考站不存在")
    try:
        from_round = int(from_round)
        to_round = int(to_round)
    except (TypeError, ValueError) as exc:
        raise ValueError("不可用轮次必须是整数") from exc
    if from_round < 1 or to_round < 1 or to_round < from_round:
        raise ValueError("不可用轮次范围不合法，需满足 1 ≤ 开始轮次 ≤ 结束轮次")
    if to_round > 50:
        raise ValueError("不可用轮次不能超过 50")
    reason = (reason or "").strip()
    if len(reason) < MIN_BLACKOUT_REASON:
        raise ValueError(f"不可用原因不少于 {MIN_BLACKOUT_REASON} 字")
    if len(reason) > MAX_BLACKOUT_REASON:
        raise ValueError(f"不可用原因不能超过 {MAX_BLACKOUT_REASON} 字")
    overlap = conn.execute(
        "SELECT blackout_id FROM schedule_blackouts "
        "WHERE station_id = ? AND NOT (to_round < ? OR from_round > ?)",
        (station_id, from_round, to_round),
    ).fetchone()
    if overlap:
        raise ValueError("该考站在所选轮次已有不可用记录，请先删除或调整范围")
    blackout_id = db.new_id("blk")
    created_at = db.now_iso()
    conn.execute(
        "INSERT INTO schedule_blackouts(blackout_id, station_id, from_round, to_round, "
        "reason, actor, created_at) VALUES(?,?,?,?,?,?,?)",
        (blackout_id, station_id, from_round, to_round, reason, actor, created_at),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.schedule_blackout_add",
        object_type="ScheduleBlackout",
        object_id=blackout_id,
        before=None,
        after={
            "station_id": station_id,
            "from_round": from_round,
            "to_round": to_round,
            "reason": reason,
        },
    )
    return {
        "blackout_id": blackout_id,
        "station_id": station_id,
        "from_round": from_round,
        "to_round": to_round,
        "reason": reason,
        "actor": actor,
        "created_at": created_at,
    }


def delete_blackout(conn, blackout_id: str, *, actor: str = "考务编排") -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM schedule_blackouts WHERE blackout_id = ?", (blackout_id,)
    ).fetchone()
    if not row:
        raise ValueError("不可用时段记录不存在")
    conn.execute("DELETE FROM schedule_blackouts WHERE blackout_id = ?", (blackout_id,))
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.schedule_blackout_delete",
        object_type="ScheduleBlackout",
        object_id=blackout_id,
        before=dict(row),
        after=None,
    )
    return dict(row)


def blackouts_by_round(conn, rounds: int) -> dict[int, set[str]]:
    """返回每一轮被封锁的考站集合，供生成与修复共用。"""
    mapping: dict[int, set[str]] = {}
    for row in list_blackouts(conn):
        start = max(1, int(row["from_round"]))
        end = min(int(rounds), int(row["to_round"]))
        for round_no in range(start, end + 1):
            mapping.setdefault(round_no, set()).add(row["station_id"])
    return mapping


def blackout_warnings(blackouts: list[dict[str, Any]], rounds: int) -> list[str]:
    messages: list[str] = []
    for row in blackouts:
        if int(row["from_round"]) > rounds:
            continue
        start = max(1, int(row["from_round"]))
        end = min(int(rounds), int(row["to_round"]))
        span = f"第 {start} 轮" if start == end else f"第 {start}–{end} 轮"
        messages.append(
            f"{row['station_id']} 在{span}为不可用时段，已避开（{row['reason']}）。"
        )
    return messages


def equipment_conflicts(
    round_rows: list[dict[str, Any]],
    station_map: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """按轮次检查共享设备需求是否超过可用台账。"""
    conflicts: list[dict[str, Any]] = []
    for round_row in round_rows:
        counts: dict[str, int] = {}
        for slot in round_row.get("slots") or []:
            equipment = slot.get("equipment")
            if not equipment and station_map:
                equipment = (station_map.get(slot.get("station_id")) or {}).get("equipment")
            if not equipment or equipment == "无":
                continue
            counts[equipment] = counts.get(equipment, 0) + 1
        for equipment, demand in sorted(counts.items()):
            available = EQUIPMENT_COUNTS.get(equipment)
            if available is not None and demand > available:
                conflicts.append(
                    {
                        "round": round_row["round"],
                        "equipment": equipment,
                        "demand": demand,
                        "available": available,
                    }
                )
    return conflicts


def ensure_schedule_inputs(conn) -> None:
    if conn.execute("SELECT COUNT(*) AS n FROM schedule_stations").fetchone()["n"] == 0:
        conn.executemany(
            "INSERT INTO schedule_stations(station_id, name, duration_min, difficulty, room_type, "
            "equipment, sp_type, examiner_type, accessible) VALUES(?,?,?,?,?,?,?,?,?)",
            STATIONS,
        )
    if conn.execute("SELECT COUNT(*) AS n FROM schedule_candidates").fetchone()["n"] == 0:
        conn.executemany(
            "INSERT INTO schedule_candidates(candidate_id, name, extra_time_min, quiet_room, note, "
            "accessible_required, separate_channel) VALUES(?,?,?,?,?,?,?)",
            CANDIDATES,
        )
    conn.commit()


def schedule_inputs(conn) -> dict[str, Any]:
    stations = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM schedule_stations ORDER BY station_id"
        ).fetchall()
    ]
    candidates = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM schedule_candidates ORDER BY candidate_id"
        ).fetchall()
    ]
    return {
        "stations": stations,
        "candidates": candidates,
        "resources": {**RESOURCE_COUNTS, "equipment": EQUIPMENT_COUNTS},
        "blackouts": list_blackouts(conn),
        "fatigue_limit_min": FATIGUE_LIMIT_MIN,
    }


def _solve_assignments(
    candidates: list[dict[str, Any]],
    stations: list[dict[str, Any]],
    rounds: int,
    target: float,
    seed: int,
    attempts: int = 40,
    fairness_weight: float = 0.5,
    blocked_by_round: dict[int, set[str]] | None = None,
) -> dict[str, list[tuple[int, str]]]:
    """多随机种子回溯，按确认的公平权重平衡难度与考站使用。"""
    if len(candidates) > len(stations):
        raise ValueError("考生人数不能超过单轮并行考站数（演示版约束）")
    blocked_by_round = blocked_by_round or {}
    station_map = {s["station_id"]: s for s in stations}
    best_result: dict[str, list[tuple[int, str]]] | None = None
    best_score: tuple[float, ...] | None = None
    for attempt in range(attempts):
        rng = random.Random(seed + attempt)
        visited: dict[str, set[str]] = {c["candidate_id"]: set() for c in candidates}
        current: dict[str, float] = {c["candidate_id"]: 0.0 for c in candidates}
        usage: dict[str, int] = {s["station_id"]: 0 for s in stations}
        result: dict[str, list[tuple[int, str]]] = {c["candidate_id"]: [] for c in candidates}
        used_in_round = [set() for _ in range(rounds)]

        def backtrack(round_index: int) -> bool:
            if round_index == rounds:
                return True
            order = candidates[:]
            rng.shuffle(order)
            order.sort(key=lambda c: target - current[c["candidate_id"]], reverse=True)

            def place(index: int) -> bool:
                if index == len(order):
                    return backtrack(round_index + 1)
                candidate = order[index]
                candidate_id = candidate["candidate_id"]
                options = [
                    station
                    for station in stations
                    if station["station_id"] not in visited[candidate_id]
                    and station["station_id"] not in used_in_round[round_index]
                    and station["station_id"] not in blocked_by_round.get(round_index + 1, set())
                    and (
                        not candidate.get("accessible_required")
                        or bool(station.get("accessible"))
                    )
                ]
                options.sort(
                    key=lambda s: (
                        abs(current[candidate_id] + s["difficulty"] - target)
                        + usage[s["station_id"]] * 0.35,
                        usage[s["station_id"]],
                        rng.random(),
                    )
                )
                for station in options:
                    station_id = station["station_id"]
                    visited[candidate_id].add(station_id)
                    used_in_round[round_index].add(station_id)
                    usage[station_id] += 1
                    current[candidate_id] += station["difficulty"]
                    result[candidate_id].append((round_index, station_id))
                    if place(index + 1):
                        return True
                    result[candidate_id].pop()
                    current[candidate_id] -= station["difficulty"]
                    usage[station_id] -= 1
                    used_in_round[round_index].discard(station_id)
                    visited[candidate_id].discard(station_id)
                return False

            return place(0)

        if not backtrack(0):
            continue
        totals = [current[c["candidate_id"]] for c in candidates]
        late = [
            sum(
                station_map[station_id]["difficulty"]
                for round_index, station_id in result[c["candidate_id"]]
                if round_index >= rounds - 2
            )
            for c in candidates
        ]
        difficulty_spread = max(totals) - min(totals)
        late_spread = max(late) - min(late)
        usage_spread = max(usage.values()) - min(usage.values())
        score = policy_score(
            difficulty_spread=difficulty_spread,
            late_spread=late_spread,
            usage_spread=usage_spread,
            max_usage=max(usage.values()),
            fairness_weight=fairness_weight,
        ) + (attempt,)
        if best_score is None or score < best_score:
            best_score = score
            best_result = result
    if best_result is None:
        raise ValueError("在当前约束下未找到可行排考方案")
    return best_result


def policy_score(
    *,
    difficulty_spread: float,
    late_spread: float,
    usage_spread: float,
    max_usage: int,
    fairness_weight: float,
) -> tuple[float, ...]:
    """软约束权重下的方案排序：硬阈值优先，其余按确认的公平/效率权重加权。"""
    over_limit = max(0.0, difficulty_spread - 1.0) + max(0.0, late_spread - 1.0)
    balance = round(
        fairness_weight * (difficulty_spread + late_spread)
        + (1.0 - fairness_weight) * usage_spread,
        3,
    )
    return (
        round(over_limit, 3),
        balance,
        usage_spread,
        max_usage,
        round(difficulty_spread, 3),
        round(late_spread, 3),
    )


def generate_schedule(
    conn,
    *,
    exam_date: str,
    rounds: int = 6,
    buffer_min: int = 2,
    seed: int = 2026,
    fairness_weight: float = 0.5,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_schedule_inputs(conn)
    inputs = schedule_inputs(conn)
    stations = inputs["stations"]
    candidates = inputs["candidates"]
    if rounds <= 0 or rounds > len(stations):
        raise ValueError("轮次数必须在 1 到考站数之间")
    if buffer_min < 0:
        raise ValueError("缓冲时间不能为负")
    try:
        fairness_weight = float(fairness_weight)
    except (TypeError, ValueError):
        raise ValueError("公平权重必须是 0 到 1 之间的数字") from None
    if fairness_weight < 0 or fairness_weight > 1:
        raise ValueError("公平权重必须在 0 到 1 之间")

    target = round(mean(s["difficulty"] for s in stations) * rounds, 3)
    blackout_rows = list_blackouts(conn)
    assignments = _solve_assignments(
        candidates,
        stations,
        rounds,
        target,
        seed,
        fairness_weight=fairness_weight,
        blocked_by_round=blackouts_by_round(conn, rounds),
    )
    station_map = {s["station_id"]: s for s in stations}
    candidate_map = {c["candidate_id"]: c for c in candidates}

    candidate_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        rows = sorted(assignments[candidate_id], key=lambda x: x[0])
        scheduled = []
        total_difficulty = 0.0
        late_difficulty = 0.0
        for round_index, station_id in rows:
            station = station_map[station_id]
            total_difficulty += station["difficulty"]
            if round_index >= rounds - 2:
                late_difficulty += station["difficulty"]
            scheduled.append(
                {
                    "round": round_index + 1,
                    "station_id": station_id,
                    "station_name": station["name"],
                    "difficulty": station["difficulty"],
                    "duration_min": station["duration_min"] + candidate["extra_time_min"],
                    "extra_time_min": candidate["extra_time_min"],
                    "equipment": station.get("equipment"),
                }
            )
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "name": candidate["name"],
                "extra_time_min": candidate["extra_time_min"],
                "quiet_room": bool(candidate["quiet_room"]),
                "accessible_required": bool(candidate.get("accessible_required")),
                "separate_channel": bool(candidate.get("separate_channel")),
                "note": candidate["note"],
                "total_difficulty": round(total_difficulty, 2),
                "late_difficulty": round(late_difficulty, 2),
                "stations": scheduled,
            }
        )

    round_rows: list[dict[str, Any]] = []
    cursor = 0
    examiner_minutes: dict[str, float] = {}
    room_minutes: dict[str, float] = {}
    sp_minutes: dict[str, float] = {}
    for round_index in range(rounds):
        slots = []
        for candidate in candidates:
            station_id = next(
                (sid for ridx, sid in assignments[candidate["candidate_id"]] if ridx == round_index),
                None,
            )
            if not station_id:
                continue
            station = station_map[station_id]
            extra = int(candidate["extra_time_min"])
            duration = station["duration_min"] + extra
            station_index = stations.index(station)
            room = f"R{station_index + 1:02d}"
            sp = f"SP{station_index + 1:02d}"
            examiner = f"E{station_index + 1:02d}"
            slots.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_name": candidate["name"],
                    "station_id": station_id,
                    "station_name": station["name"],
                    "difficulty": station["difficulty"],
                    "start_min": cursor,
                    "end_min": cursor + duration,
                    "duration_min": duration,
                    "room": room,
                    "sp": sp,
                    "examiner": examiner,
                    "extra_time_min": extra,
                    "equipment": station.get("equipment"),
                }
            )
            examiner_minutes[examiner] = examiner_minutes.get(examiner, 0) + duration
            room_minutes[room] = room_minutes.get(room, 0) + duration
            sp_minutes[sp] = sp_minutes.get(sp, 0) + duration
        round_duration = max((s["duration_min"] for s in slots), default=0) + buffer_min
        round_rows.append(
            {
                "round": round_index + 1,
                "start_min": cursor,
                "end_min": cursor + round_duration,
                "duration_min": round_duration,
                "slots": slots,
            }
        )
        cursor += round_duration

    totals = [row["total_difficulty"] for row in candidate_rows]
    late_totals = [row["late_difficulty"] for row in candidate_rows]
    difficulty_spread = round(max(totals) - min(totals), 2) if totals else 0.0
    late_spread = round(max(late_totals) - min(late_totals), 2) if late_totals else 0.0
    usage_counts = [0] * len(stations)
    for round_row in round_rows:
        for slot in round_row["slots"]:
            index = next(i for i, s in enumerate(stations) if s["station_id"] == slot["station_id"])
            usage_counts[index] += 1

    equipment_issues = equipment_conflicts(round_rows)

    warnings: list[str] = []
    for conflict in equipment_issues:
        warnings.append(
            f"第 {conflict['round']} 轮 {conflict['equipment']} 同时需求 {conflict['demand']} 台，"
            f"可用 {conflict['available']} 台，存在设备冲突。"
        )
    warnings.extend(blackout_warnings(blackout_rows, rounds))
    for candidate in candidate_rows:
        if candidate["extra_time_min"]:
            warnings.append(
                f"{candidate['candidate_id']} 需每站延长 {candidate['extra_time_min']} 分钟，轮次时长已顺延；建议设置延长通道。"
            )
        if candidate["quiet_room"]:
            warnings.append(f"{candidate['candidate_id']} 需要安静候考区，请在候考安排中单独标注。")
        if candidate["accessible_required"]:
            warnings.append(
                f"{candidate['candidate_id']} 需无障碍考站，已限制在具备无障碍条件的考站轮转。"
            )
        if candidate["separate_channel"]:
            warnings.append(
                f"{candidate['candidate_id']} 需使用单独通道进出，请在候考与转场安排中单独引导。"
            )
    if difficulty_spread > 1.0:
        warnings.append(f"考生加权难度差 {difficulty_spread}，超过 1.0，建议重新排考。")
    if late_spread > 1.0:
        warnings.append(f"后两轮难度差 {late_spread}，存在顺序效应风险。")
    if usage_counts and max(usage_counts) - min(usage_counts) > 1:
        warnings.append(
            f"考站使用次数差 {max(usage_counts) - min(usage_counts)}，建议调整轮次或考站配置。"
        )
    for examiner, minutes in examiner_minutes.items():
        if minutes > FATIGUE_LIMIT_MIN:
            warnings.append(f"考官 {examiner} 连续评分 {minutes:.0f} 分钟，超过疲劳阈值。")
    if RESOURCE_COUNTS["rooms"] < len(stations) or RESOURCE_COUNTS["sps"] < len(stations) or RESOURCE_COUNTS["examiners"] < len(stations):
        warnings.append("房间/SP/考官数量少于考站数，存在资源冲突。")

    schedule = {
        "schedule_id": db.new_id("sched"),
        "exam_date": exam_date,
        "rounds": rounds,
        "buffer_min": buffer_min,
        "seed": seed,
        "blackouts": blackout_rows,
        "candidates": candidate_rows,
        "rounds_detail": round_rows,
        "quality": {
            "target_difficulty": target,
            "difficulty_min": min(totals) if totals else None,
            "difficulty_max": max(totals) if totals else None,
            "difficulty_spread": difficulty_spread,
            "late_difficulty_spread": late_spread,
            "station_usage_min": min(usage_counts) if usage_counts else 0,
            "station_usage_max": max(usage_counts) if usage_counts else 0,
            "total_minutes": cursor,
            "equipment_conflicts": equipment_issues,
            "warnings": warnings,
            "status": "review" if warnings else "ok",
        },
        "resources": {
            "rooms": {"count": RESOURCE_COUNTS["rooms"], "used": len(room_minutes), "max_minutes": max(room_minutes.values()) if room_minutes else 0},
            "sps": {"count": RESOURCE_COUNTS["sps"], "used": len(sp_minutes), "max_minutes": max(sp_minutes.values()) if sp_minutes else 0},
            "examiners": {"count": RESOURCE_COUNTS["examiners"], "used": len(examiner_minutes), "max_minutes": max(examiner_minutes.values()) if examiner_minutes else 0},
            "equipment": {"available": EQUIPMENT_COUNTS},
        },
        "policy": {
            "fairness_weight": round(fairness_weight, 3),
            "policy_id": (policy or {}).get("policy_id"),
            "label": (policy or {}).get("label") or "默认权重（未确认）",
            "preset": (policy or {}).get("preset"),
            "note": (policy or {}).get("note"),
            "confirmed_by": (policy or {}).get("actor"),
            "confirmed_at": (policy or {}).get("created_at"),
        },
        "created_at": db.now_iso(),
    }
    conn.execute(
        "INSERT INTO exam_schedules(schedule_id, exam_date, params, schedule, created_at) VALUES(?,?,?,?,?)",
        (
            schedule["schedule_id"],
            exam_date,
            json.dumps(
                {
                    "rounds": rounds,
                    "buffer_min": buffer_min,
                    "seed": seed,
                    "fairness_weight": round(fairness_weight, 3),
                    "policy_id": (policy or {}).get("policy_id"),
                },
                ensure_ascii=False,
            ),
            json.dumps(schedule, ensure_ascii=False),
            schedule["created_at"],
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor="考务编排",
        action="exam.schedule_generate",
        object_type="ExamSchedule",
        object_id=schedule["schedule_id"],
        before=None,
        after={
            "exam_date": exam_date,
            "rounds": rounds,
            "difficulty_spread": difficulty_spread,
            "warnings": len(warnings),
        },
    )
    return schedule


def latest_schedule(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT schedule FROM exam_schedules ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["schedule"]) if row else None


CONFLICT_TYPES = ("candidate_round", "station_round")


def _round_map(schedule: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {row["round"]: row for row in schedule.get("rounds_detail") or []}


def _candidate_map(schedule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["candidate_id"]: c for c in schedule.get("candidates") or []}


def _normalize_conflicts(
    schedule: dict[str, Any], conflicts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rounds = int(schedule.get("rounds") or 0)
    round_rows = _round_map(schedule)
    candidate_ids = set(_candidate_map(schedule))
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw in conflicts or []:
        ctype = str(raw.get("type") or "")
        try:
            round_no = int(raw.get("round") or 0)
        except (TypeError, ValueError):
            raise ValueError("轮次必须是整数") from None
        if ctype not in CONFLICT_TYPES:
            raise ValueError("冲突类型必须是 candidate_round 或 station_round")
        if round_no < 1 or round_no > rounds:
            raise ValueError(f"第 {round_no} 轮不存在（共 {rounds} 轮）")
        if ctype == "candidate_round":
            candidate_id = str(raw.get("candidate_id") or "")
            if candidate_id not in candidate_ids:
                raise ValueError(f"考生 {candidate_id} 不在排考表中")
            key = (ctype, candidate_id, round_no)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"type": ctype, "candidate_id": candidate_id, "round": round_no})
        else:
            station_id = str(raw.get("station_id") or "")
            if not any(
                slot["station_id"] == station_id for slot in round_rows[round_no]["slots"]
            ):
                raise ValueError(f"考站 {station_id} 在第 {round_no} 轮没有安排")
            key = (ctype, station_id, round_no)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"type": ctype, "station_id": station_id, "round": round_no})
    if not normalized:
        raise ValueError("至少提供一条冲突")
    return normalized


def _resource_labels(station_index: int) -> tuple[str, str, str]:
    return (f"R{station_index + 1:02d}", f"SP{station_index + 1:02d}", f"E{station_index + 1:02d}")


def _candidate_entry(candidate: dict[str, Any], round_no: int) -> dict[str, Any]:
    for entry in candidate.get("stations") or []:
        if entry.get("round") == round_no:
            return entry
    raise ValueError(f"考生 {candidate['candidate_id']} 在第 {round_no} 轮没有安排")


def _apply_station(
    *,
    candidate: dict[str, Any],
    station: dict[str, Any],
    station_index: int,
    candidate_entry: dict[str, Any],
    slot: dict[str, Any] | None,
) -> None:
    duration = station["duration_min"] + int(candidate.get("extra_time_min") or 0)
    candidate_entry.update(
        {
            "station_id": station["station_id"],
            "station_name": station["name"],
            "difficulty": station["difficulty"],
            "duration_min": duration,
            "equipment": station.get("equipment"),
        }
    )
    if slot is not None:
        room, sp, examiner = _resource_labels(station_index)
        slot.update(
            {
                "station_id": station["station_id"],
                "station_name": station["name"],
                "difficulty": station["difficulty"],
                "duration_min": duration,
                "room": room,
                "sp": sp,
                "examiner": examiner,
                "equipment": station.get("equipment"),
            }
        )


def _recompute_schedule(schedule: dict[str, Any]) -> None:
    """换站后重算考生难度合计、轮次时间、资源占用与质量指标。"""
    round_rows = sorted(schedule.get("rounds_detail") or [], key=lambda row: row["round"])
    candidates = schedule.get("candidates") or []
    rounds = len(round_rows)
    for candidate in candidates:
        total = sum(float(s.get("difficulty") or 0) for s in candidate.get("stations") or [])
        late = sum(
            float(s.get("difficulty") or 0)
            for s in candidate.get("stations") or []
            if int(s.get("round") or 0) >= rounds - 1
        )
        candidate["total_difficulty"] = round(total, 2)
        candidate["late_difficulty"] = round(late, 2)

    buffer_min = int(schedule.get("buffer_min") or 0)
    cursor = 0
    room_minutes: dict[str, int] = {}
    sp_minutes: dict[str, int] = {}
    examiner_minutes: dict[str, int] = {}
    usage: dict[str, int] = {}
    for round_row in round_rows:
        round_row["start_min"] = cursor
        round_duration = 0
        for slot in round_row["slots"]:
            slot["start_min"] = cursor
            slot["end_min"] = cursor + int(slot.get("duration_min") or 0)
            round_duration = max(round_duration, int(slot.get("duration_min") or 0))
            room_minutes[slot["room"]] = room_minutes.get(slot["room"], 0) + int(slot.get("duration_min") or 0)
            sp_minutes[slot["sp"]] = sp_minutes.get(slot["sp"], 0) + int(slot.get("duration_min") or 0)
            examiner_minutes[slot["examiner"]] = examiner_minutes.get(slot["examiner"], 0) + int(
                slot.get("duration_min") or 0
            )
            usage[slot["station_id"]] = usage.get(slot["station_id"], 0) + 1
        round_row["duration_min"] = round_duration + buffer_min
        round_row["end_min"] = cursor + round_duration + buffer_min
        cursor = round_row["end_min"]

    totals = [c["total_difficulty"] for c in candidates]
    late_totals = [c["late_difficulty"] for c in candidates]
    difficulty_spread = round(max(totals) - min(totals), 2) if totals else 0.0
    late_spread = round(max(late_totals) - min(late_totals), 2) if late_totals else 0.0
    quality = schedule.setdefault("quality", {})
    quality["difficulty_min"] = min(totals) if totals else None
    quality["difficulty_max"] = max(totals) if totals else None
    quality["difficulty_spread"] = difficulty_spread
    quality["late_difficulty_spread"] = late_spread
    quality["total_minutes"] = cursor
    quality["station_usage_min"] = min(usage.values()) if usage else 0
    quality["station_usage_max"] = max(usage.values()) if usage else 0

    warnings = [
        w
        for w in quality.get("warnings") or []
        if not (
            w.startswith("考生加权难度差")
            or w.startswith("后两轮难度差")
            or w.startswith("考站使用次数差")
            or w.startswith("考官 ")
            or "设备冲突" in w
        )
    ]
    equipment_issues = equipment_conflicts(round_rows)
    for conflict in equipment_issues:
        warnings.append(
            f"第 {conflict['round']} 轮 {conflict['equipment']} 同时需求 {conflict['demand']} 台，"
            f"可用 {conflict['available']} 台，存在设备冲突。"
        )
    quality["equipment_conflicts"] = equipment_issues
    if difficulty_spread > 1.0:
        warnings.append(f"考生加权难度差 {difficulty_spread}，超过 1.0，建议重新排考。")
    if late_spread > 1.0:
        warnings.append(f"后两轮难度差 {late_spread}，存在顺序效应风险。")
    if usage and max(usage.values()) - min(usage.values()) > 1:
        warnings.append(
            f"考站使用次数差 {max(usage.values()) - min(usage.values())}，建议调整轮次或考站配置。"
        )
    for examiner, minutes in examiner_minutes.items():
        if minutes > FATIGUE_LIMIT_MIN:
            warnings.append(f"考官 {examiner} 连续评分 {minutes:.0f} 分钟，超过疲劳阈值。")
    quality["warnings"] = warnings
    quality["status"] = "review" if warnings else "ok"

    schedule["resources"] = {
        "rooms": {
            "count": RESOURCE_COUNTS["rooms"],
            "used": len(room_minutes),
            "max_minutes": max(room_minutes.values()) if room_minutes else 0,
        },
        "sps": {
            "count": RESOURCE_COUNTS["sps"],
            "used": len(sp_minutes),
            "max_minutes": max(sp_minutes.values()) if sp_minutes else 0,
        },
        "examiners": {
            "count": RESOURCE_COUNTS["examiners"],
            "used": len(examiner_minutes),
            "max_minutes": max(examiner_minutes.values()) if examiner_minutes else 0,
        },
        "equipment": {"available": EQUIPMENT_COUNTS},
    }


def _find_relocation(
    round_no: int,
    affected_id: str,
    round_row: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    stations: list[dict[str, Any]],
    blocked_stations: set[str],
    conflicted_rounds: set[tuple[str, int]],
) -> list[dict[str, Any]] | None:
    """用增广路径找最小换站链：优先空闲站，否则顺次顶替直到空闲站。"""
    occupied = {slot["station_id"]: slot["candidate_id"] for slot in round_row["slots"]}
    current_station = next(
        entry["station_id"]
        for entry in candidates[affected_id]["stations"]
        if entry["round"] == round_no
    )
    free_station_ids = {
        station["station_id"]
        for station in stations
        if station["station_id"] not in occupied
        and station["station_id"] not in blocked_stations
    }
    if current_station not in blocked_stations:
        # 考生本人让出的考站对其他人重新可用
        free_station_ids.add(current_station)

    def allowed(candidate_id: str, station_id: str, used: set[str]) -> list[dict[str, Any]]:
        visited = {
            entry["station_id"]
            for entry in candidates[candidate_id]["stations"]
            if entry["round"] != round_no
        }
        return [
            station
            for station in stations
            if station["station_id"] not in used
            and station["station_id"] not in blocked_stations
            and station["station_id"] != station_id
            and station["station_id"] not in visited
        ]

    queue: deque[tuple[str, str, list[dict[str, Any]], set[str]]] = deque(
        [(affected_id, current_station, [], set())]
    )
    while queue:
        candidate_id, station_id, path, used = queue.popleft()
        current_difficulty = float(
            next(
                entry.get("difficulty") or 0
                for entry in candidates[candidate_id]["stations"]
                if entry["round"] == round_no
            )
        )
        options = sorted(
            allowed(candidate_id, station_id, used | blocked_stations),
            key=lambda station: (
                0 if station["station_id"] in free_station_ids else 1,
                abs(float(station["difficulty"]) - current_difficulty),
                station["station_id"],
            ),
        )
        for station in options:
            target_id = station["station_id"]
            move = {
                "candidate_id": candidate_id,
                "round": round_no,
                "from_station_id": station_id,
                "to_station_id": target_id,
            }
            if target_id in free_station_ids:
                return path + [move]
            occupant = occupied.get(target_id)
            if not occupant or occupant == candidate_id:
                continue
            if (occupant, round_no) in conflicted_rounds:
                continue
            queue.append((occupant, target_id, path + [move], used | {target_id}))
    return None


def _repair_schedule(
    schedule: dict[str, Any],
    conflicts: list[dict[str, Any]],
    stations: list[dict[str, Any]],
    blocked_by_round: dict[int, set[str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """以换站链为主的最小改动修复：优先空闲站，必要时顺次顶替直到空闲站。"""
    blocked_by_round = blocked_by_round or {}
    repaired = json.loads(json.dumps(schedule))
    round_rows = _round_map(repaired)
    candidates = _candidate_map(repaired)
    slot_map = {
        (slot["candidate_id"], round_row["round"]): slot
        for round_row in round_rows.values()
        for slot in round_row["slots"]
    }
    station_index = {station["station_id"]: index for index, station in enumerate(stations)}
    station_map = {station["station_id"]: station for station in stations}
    normalized = _normalize_conflicts(repaired, conflicts)

    conflicted_rounds = {
        (item["candidate_id"], item["round"])
        for item in normalized
        if item["type"] == "candidate_round"
    }
    all_down_stations = {
        item["station_id"] for item in normalized if item["type"] == "station_round"
    }

    changes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for conflict in normalized:
        round_no = conflict["round"]
        round_row = round_rows[round_no]
        if conflict["type"] == "candidate_round":
            affected_id = conflict["candidate_id"]
        else:
            down_station = conflict["station_id"]
            affected_slot = next(
                (slot for slot in round_row["slots"] if slot["station_id"] == down_station), None
            )
            if affected_slot is None:
                unresolved.append(
                    {
                        "type": conflict["type"],
                        "round": round_no,
                        "station_id": down_station,
                        "reason": "该考站在本轮已无安排（可能已被前一次修复改派）",
                    }
                )
                continue
            affected_id = affected_slot["candidate_id"]
            if (affected_id, round_no) in conflicted_rounds:
                raise ValueError(
                    f"考生 {affected_id} 在第 {round_no} 轮同时被标记为不可用，请只保留考站故障冲突"
                )

        moves = _find_relocation(
            round_no,
            affected_id,
            round_row,
            candidates,
            stations,
            all_down_stations | blocked_by_round.get(round_no, set()),
            conflicted_rounds,
        )
        if not moves:
            unresolved.append(
                {
                    "type": conflict["type"],
                    "round": round_no,
                    "candidate_id": affected_id,
                    "reason": "本轮无可用空闲站或换站链，需人工调整或整场重排",
                }
            )
            continue
        for index, move in enumerate(moves):
            candidate = candidates[move["candidate_id"]]
            entry = _candidate_entry(candidate, round_no)
            station = station_map[move["to_station_id"]]
            _apply_station(
                candidate=candidate,
                station=station,
                station_index=station_index[station["station_id"]],
                candidate_entry=entry,
                slot=slot_map.get((candidate["candidate_id"], round_no)),
            )
            changes.append(
                {
                    **move,
                    "method": "chain" if len(moves) > 1 else "free_station",
                    "swapped_with": moves[index + 1]["candidate_id"]
                    if index + 1 < len(moves)
                    else None,
                    "chain_length": len(moves),
                }
            )

    for round_row in round_rows.values():
        station_ids = [slot["station_id"] for slot in round_row["slots"]]
        if len(station_ids) != len(set(station_ids)):
            raise ValueError("修复后同一轮出现重复考站，请改为整场重排")
    for candidate in candidates.values():
        station_ids = [entry["station_id"] for entry in candidate["stations"]]
        if len(station_ids) != len(set(station_ids)):
            raise ValueError(
                f"修复后考生 {candidate['candidate_id']} 出现重复考站，请改为整场重排"
            )

    before = {
        "difficulty_spread": (schedule.get("quality") or {}).get("difficulty_spread"),
        "late_difficulty_spread": (schedule.get("quality") or {}).get("late_difficulty_spread"),
        "total_minutes": (schedule.get("quality") or {}).get("total_minutes"),
    }
    _recompute_schedule(repaired)
    after = {
        "difficulty_spread": repaired["quality"]["difficulty_spread"],
        "late_difficulty_spread": repaired["quality"]["late_difficulty_spread"],
        "total_minutes": repaired["quality"]["total_minutes"],
    }
    report = {
        "requested": normalized,
        "changes": changes,
        "unresolved": unresolved,
        "before": before,
        "after": after,
    }
    if not changes:
        reasons = "；".join(item["reason"] for item in unresolved[:3])
        raise ValueError(f"无法最小改动修复：{reasons or '没有可改派的考站'}")
    return repaired, report


def repair_schedule(
    conn,
    *,
    conflicts: list[dict[str, Any]],
    schedule_id: str | None = None,
    actor: str = "考务编排",
) -> dict[str, Any]:
    """按现场冲突生成修复版排考表；原表保留，新表以 repaired_from 追溯。"""
    if schedule_id:
        row = conn.execute(
            "SELECT schedule FROM exam_schedules WHERE schedule_id = ?", (schedule_id,)
        ).fetchone()
        if not row:
            raise ValueError("排考表不存在")
    else:
        row = conn.execute(
            "SELECT schedule FROM exam_schedules ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise ValueError("尚未生成排考表")
    original = json.loads(row["schedule"])
    inputs = schedule_inputs(conn)
    repaired, report = _repair_schedule(
        original,
        conflicts,
        inputs["stations"],
        blocked_by_round=blackouts_by_round(conn, int(original.get("rounds") or 0)),
    )

    repaired["schedule_id"] = db.new_id("sched")
    repaired["created_at"] = db.now_iso()
    repaired["repaired_from"] = original.get("schedule_id")
    repaired["repair_report"] = report
    if report["unresolved"]:
        repaired["quality"]["warnings"].append(
            f"仍有 {len(report['unresolved'])} 条冲突未解决，需人工处理。"
        )
        repaired["quality"]["status"] = "review"

    conn.execute(
        "INSERT INTO exam_schedules(schedule_id, exam_date, params, schedule, created_at) VALUES(?,?,?,?,?)",
        (
            repaired["schedule_id"],
            repaired["exam_date"],
            json.dumps(
                {
                    "repaired_from": original.get("schedule_id"),
                    "conflicts": len(report["requested"]),
                    "unresolved": len(report["unresolved"]),
                },
                ensure_ascii=False,
            ),
            json.dumps(repaired, ensure_ascii=False),
            repaired["created_at"],
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="exam.schedule_repair",
        object_type="ExamSchedule",
        object_id=repaired["schedule_id"],
        before={"schedule_id": original.get("schedule_id")},
        after={
            "changes": len(report["changes"]),
            "unresolved": len(report["unresolved"]),
            "difficulty_spread": repaired["quality"]["difficulty_spread"],
        },
    )
    return repaired

