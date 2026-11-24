"""反馈可及性：学生自评“看懂了并有帮助”的闭环。"""
from __future__ import annotations

from typing import Any

from .. import db

MIN_SAMPLES = 5
HELPFUL_RATING = 4
TARGET_RATE = 0.3
EXPERIENCE_TARGET = 7.0


def _experience_value(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if value not in range(1, 11):
        raise ValueError(f"{label}必须在 1–10 之间")
    return value


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
    usefulness: int | None = None,
    realism: int | None = None,
    participant_label: str = "",
    actor: str = "学生",
) -> dict[str, Any]:
    if rating not in (1, 2, 3, 4, 5):
        raise ValueError("评分必须是 1–5")
    usefulness = _experience_value(usefulness, "实用性")
    realism = _experience_value(realism, "真实感")
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
        "INSERT INTO feedback_surveys(survey_id, attempt_id, rating, usefulness, realism, participant_label, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (survey_id, attempt_id, rating, usefulness, realism, participant_label.strip() or "匿名", timestamp),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="feedback.survey",
        object_type="StationAttempt",
        object_id=attempt_id,
        before=None,
        after={"survey_id": survey_id, "rating": rating, "usefulness": usefulness, "realism": realism},
    )
    return {
        "survey_id": survey_id,
        "attempt_id": attempt_id,
        "rating": rating,
        "usefulness": usefulness,
        "realism": realism,
        "created_at": timestamp,
    }


def feedback_accessibility(conn, bundle_id: str | None = None) -> dict[str, Any]:
    if bundle_id:
        rows = db.rows_to_dicts(
            conn.execute(
                "SELECT s.* FROM feedback_surveys s "
                "JOIN attempts a ON a.attempt_id = s.attempt_id "
                "WHERE a.bundle_id = ? ORDER BY s.created_at DESC, s.rowid DESC",
                (bundle_id,),
            ).fetchall()
        )
    else:
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
            "usefulness": row.get("usefulness"),
            "realism": row.get("realism"),
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
        "experience": {
            "usefulness": _experience_summary(rows, "usefulness"),
            "realism": _experience_summary(rows, "realism"),
        },
        "recent": recent,
    }


def _experience_summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [row[key] for row in rows if row.get(key) is not None]
    average = round(sum(values) / len(values), 2) if values else None
    if not values:
        status = "unmeasured"
        note = "尚未采集。"
    elif len(values) < MIN_SAMPLES:
        status = "insufficient"
        note = f"已收集 {len(values)}/{MIN_SAMPLES} 份，样本不足。"
    elif average >= EXPERIENCE_TARGET:
        status = "pass"
        note = f"{len(values)} 份评分均值 {average}，达到 {EXPERIENCE_TARGET} 目标。"
    else:
        status = "fail"
        note = f"{len(values)} 份评分均值 {average}，低于 {EXPERIENCE_TARGET} 目标。"
    return {
        "average": average,
        "count": len(values),
        "target": EXPERIENCE_TARGET,
        "status": status,
        "note": note,
    }
