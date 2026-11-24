"""反馈可及性：学生自评“看懂了并有帮助”的闭环。"""
from __future__ import annotations

from typing import Any

from .. import db

MIN_SAMPLES = 5
HELPFUL_RATING = 4
TARGET_RATE = 0.3


def survey_for_attempt(conn, attempt_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM feedback_surveys WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return dict(row) if row else None


def submit_feedback_survey(
    conn,
    *,
    attempt_id: str,
    rating: int,
    participant_label: str = "",
    actor: str = "学生",
) -> dict[str, Any]:
    if rating not in (1, 2, 3, 4, 5):
        raise ValueError("评分必须是 1–5")
    attempt = conn.execute(
        "SELECT attempt_id FROM attempts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if not attempt:
        raise ValueError("场次不存在")
    if survey_for_attempt(conn, attempt_id):
        raise ValueError("该场次已提交反馈问卷")
    survey_id = db.new_id("fs")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO feedback_surveys(survey_id, attempt_id, rating, participant_label, created_at) "
        "VALUES(?,?,?,?,?)",
        (survey_id, attempt_id, rating, participant_label.strip() or "匿名", timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="feedback.survey",
        object_type="StationAttempt",
        object_id=attempt_id,
        before=None,
        after={"survey_id": survey_id, "rating": rating},
    )
    return {"survey_id": survey_id, "attempt_id": attempt_id, "rating": rating, "created_at": timestamp}


def feedback_accessibility(conn) -> dict[str, Any]:
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT * FROM feedback_surveys ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    )
    count = len(rows)
    helpful = sum(1 for row in rows if row["rating"] >= HELPFUL_RATING)
    helpful_rate = round(helpful / count, 3) if count else None
    average = round(sum(row["rating"] for row in rows) / count, 2) if count else None
    if count == 0:
        status = "unmeasured"
        note = "尚未采集学生反馈可及性自评。"
    elif count < MIN_SAMPLES:
        status = "insufficient"
        note = f"已收集 {count}/{MIN_SAMPLES} 份，样本不足，暂不结论。"
    elif helpful_rate >= TARGET_RATE:
        status = "pass"
        note = f"{count} 份自评中 {helpful} 份认为看懂了并有帮助。"
    else:
        status = "fail"
        note = f"{count} 份自评中仅 {helpful} 份认为看懂了并有帮助，需改进反馈表达。"
    recent = [
        {
            "attempt_id": row["attempt_id"],
            "rating": row["rating"],
            "participant_label": row["participant_label"],
            "created_at": row["created_at"],
        }
        for row in rows[:5]
    ]
    return {
        "status": status,
        "count": count,
        "average": average,
        "helpful_count": helpful,
        "helpful_rate": helpful_rate,
        "threshold": TARGET_RATE,
        "min_samples": MIN_SAMPLES,
        "note": note,
        "recent": recent,
    }
