"""SUS 可用性问卷：10 项 1–5 分，自动折算为 0–100。"""
from __future__ import annotations

import json
from typing import Any

from .. import db

SUS_ITEMS = [
    "我愿意频繁使用这个系统",
    "我觉得系统没有必要这么复杂",
    "我认为系统容易使用",
    "我需要技术人员帮助才能使用系统",
    "我觉得系统的各项功能整合得很好",
    "我觉得系统里不一致的地方太多",
    "我认为大多数人能很快学会使用系统",
    "我觉得系统使用起来很麻烦",
    "我对使用系统很有信心",
    "我需要先学习很多知识才能开始使用系统",
]


def compute_sus(answers: list[int]) -> float:
    if len(answers) != len(SUS_ITEMS):
        raise ValueError(f"SUS 需要 {len(SUS_ITEMS)} 项答案")
    contributions: list[int] = []
    for index, answer in enumerate(answers):
        if isinstance(answer, bool) or not isinstance(answer, int) or not 1 <= answer <= 5:
            raise ValueError("SUS 每项必须是 1–5 的整数")
        contributions.append(answer - 1 if index % 2 == 0 else 5 - answer)
    return round(sum(contributions) * 2.5, 2)


def submit_sus(
    conn,
    *,
    answers: list[int],
    participant_label: str | None = None,
    actor: str = "SUS 受访者",
) -> dict[str, Any]:
    score = compute_sus(answers)
    response_id = db.new_id("sus")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO sus_responses(response_id, participant_label, answers, score, created_at) "
        "VALUES(?,?,?,?,?)",
        (response_id, participant_label, json.dumps(answers), score, timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="sus.submit",
        object_type="SusResponse",
        object_id=response_id,
        before=None,
        after={
            "score": score,
            "participant_label": participant_label,
            "answers": answers,
        },
    )
    return {
        "response_id": response_id,
        "score": score,
        "participant_label": participant_label,
        "created_at": timestamp,
    }


def sus_summary(conn, limit: int = 100) -> dict[str, Any]:
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT * FROM sus_responses ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    )
    scores = [float(row["score"]) for row in rows]
    return {
        "count": len(scores),
        "average": round(sum(scores) / len(scores), 2) if scores else None,
        "latest_at": rows[0]["created_at"] if rows else None,
        "recent_scores": scores[:10],
        "items": SUS_ITEMS,
    }
