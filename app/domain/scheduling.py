"""考务编排：在约束下生成轮转排考表，并检查难度均衡、疲劳与特殊需求。"""
from __future__ import annotations

import json
import random
from statistics import mean
from typing import Any

from .. import db

STATIONS = [
    ("ST01", "急性胸痛", 10, 0.90, "抢救室", "心电图机", "焦虑型", "急诊主治"),
    ("ST02", "呼吸困难", 10, 0.80, "抢救室", "监护仪", "焦虑型", "呼吸科主治"),
    ("ST03", "意识障碍", 12, 0.85, "抢救室", "监护仪", "认知障碍型", "神经科主治"),
    ("ST04", "急性腹痛", 10, 0.65, "普通诊室", "超声", "克制型", "普外科主治"),
    ("ST05", "发热待查", 8, 0.55, "普通诊室", "无", "配合型", "感染科主治"),
    ("ST06", "用药咨询", 8, 0.50, "诊室", "处方系统", "依赖型", "临床药师"),
    ("ST07", "知情同意", 10, 0.60, "谈话间", "知情同意书", "焦虑型", "主治医师"),
    ("ST08", "坏消息告知", 12, 0.90, "谈话间", "无", "情绪化型", "高年资医师"),
    ("ST09", "交接班", 10, 0.70, "示教室", "病历系统", "配合型", "主治医师"),
    ("ST10", "出院指导", 8, 0.50, "诊室", "出院小结", "依赖型", "住院医师"),
]

CANDIDATES = [
    ("C2026-001", "陈雨桐", 0, 0, ""),
    ("C2026-002", "王一帆", 5, 0, "阅读速度较慢，每站延长 5 分钟"),
    ("C2026-003", "李昊然", 0, 1, "需要安静候考区"),
    ("C2026-004", "赵思远", 0, 0, ""),
    ("C2026-005", "周可欣", 5, 0, "语言表达需更多时间"),
    ("C2026-006", "孙明哲", 0, 0, ""),
    ("C2026-007", "吴嘉怡", 0, 1, "需要安静候考区"),
    ("C2026-008", "郑子涵", 0, 0, ""),
]

RESOURCE_COUNTS = {"rooms": 10, "sps": 10, "examiners": 10}
FATIGUE_LIMIT_MIN = 240


def ensure_schedule_inputs(conn) -> None:
    if conn.execute("SELECT COUNT(*) AS n FROM schedule_stations").fetchone()["n"] == 0:
        conn.executemany(
            "INSERT INTO schedule_stations(station_id, name, duration_min, difficulty, room_type, equipment, sp_type, examiner_type) "
            "VALUES(?,?,?,?,?,?,?,?)",
            STATIONS,
        )
    if conn.execute("SELECT COUNT(*) AS n FROM schedule_candidates").fetchone()["n"] == 0:
        conn.executemany(
            "INSERT INTO schedule_candidates(candidate_id, name, extra_time_min, quiet_room, note) VALUES(?,?,?,?,?)",
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
        "resources": RESOURCE_COUNTS,
        "fatigue_limit_min": FATIGUE_LIMIT_MIN,
    }


def _solve_assignments(
    candidates: list[dict[str, Any]],
    stations: list[dict[str, Any]],
    rounds: int,
    target: float,
    seed: int,
    attempts: int = 40,
) -> dict[str, list[tuple[int, str]]]:
    """多随机种子回溯，优先满足难度约束，再最小化考站使用次数差。"""
    if len(candidates) > len(stations):
        raise ValueError("考生人数不能超过单轮并行考站数（演示版约束）")
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
        over_limit = max(0.0, difficulty_spread - 1.0) + max(0.0, late_spread - 1.0)
        score = (
            round(over_limit, 3),
            usage_spread,
            max(usage.values()),
            round(difficulty_spread, 3),
            round(late_spread, 3),
            attempt,
        )
        if best_score is None or score < best_score:
            best_score = score
            best_result = result
    if best_result is None:
        raise ValueError("在当前约束下未找到可行排考方案")
    return best_result


def generate_schedule(
    conn,
    *,
    exam_date: str,
    rounds: int = 6,
    buffer_min: int = 2,
    seed: int = 2026,
) -> dict[str, Any]:
    ensure_schedule_inputs(conn)
    inputs = schedule_inputs(conn)
    stations = inputs["stations"]
    candidates = inputs["candidates"]
    if rounds <= 0 or rounds > len(stations):
        raise ValueError("轮次数必须在 1 到考站数之间")
    if buffer_min < 0:
        raise ValueError("缓冲时间不能为负")

    target = round(mean(s["difficulty"] for s in stations) * rounds, 3)
    assignments = _solve_assignments(candidates, stations, rounds, target, seed)
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
                }
            )
        candidate_rows.append(
            {
                "candidate_id": candidate_id,
                "name": candidate["name"],
                "extra_time_min": candidate["extra_time_min"],
                "quiet_room": bool(candidate["quiet_room"]),
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

    warnings: list[str] = []
    for candidate in candidate_rows:
        if candidate["extra_time_min"]:
            warnings.append(
                f"{candidate['candidate_id']} 需每站延长 {candidate['extra_time_min']} 分钟，轮次时长已顺延；建议设置延长通道。"
            )
        if candidate["quiet_room"]:
            warnings.append(f"{candidate['candidate_id']} 需要安静候考区，请在候考安排中单独标注。")
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
            "warnings": warnings,
            "status": "review" if warnings else "ok",
        },
        "resources": {
            "rooms": {"count": RESOURCE_COUNTS["rooms"], "used": len(room_minutes), "max_minutes": max(room_minutes.values()) if room_minutes else 0},
            "sps": {"count": RESOURCE_COUNTS["sps"], "used": len(sp_minutes), "max_minutes": max(sp_minutes.values()) if sp_minutes else 0},
            "examiners": {"count": RESOURCE_COUNTS["examiners"], "used": len(examiner_minutes), "max_minutes": max(examiner_minutes.values()) if examiner_minutes else 0},
        },
        "created_at": db.now_iso(),
    }
    conn.execute(
        "INSERT INTO exam_schedules(schedule_id, exam_date, params, schedule, created_at) VALUES(?,?,?,?,?)",
        (
            schedule["schedule_id"],
            exam_date,
            json.dumps({"rounds": rounds, "buffer_min": buffer_min, "seed": seed}, ensure_ascii=False),
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
