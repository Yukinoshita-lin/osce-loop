"""复核动作：普通记录单次复核；关键项双人独立复核，分歧转高年资裁决。"""
from __future__ import annotations

import json
from typing import Any

from .. import db


def _insert_action(
    conn,
    record: dict[str, Any],
    *,
    reviewer: str,
    action: str,
    final_score: int | None,
    reason_code: str | None,
    seconds_spent: float | None,
    now: str,
) -> None:
    conn.execute(
        "INSERT INTO review_actions(action_id, record_id, reviewer, action, from_score, to_score, reason_code, "
        "seconds_spent, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            db.new_id("rv"),
            record["record_id"],
            reviewer,
            action,
            record["ai_score"],
            final_score,
            reason_code,
            seconds_spent,
            now,
        ),
    )


def apply_review(
    conn,
    record: dict[str, Any],
    *,
    action: str,
    status: str,
    final_score: int | None,
    reviewer: str,
    reason_code: str | None = None,
    seconds_spent: float | None = None,
) -> dict[str, Any]:
    now = db.now_iso()
    routing = json.loads(record["routing"] or "{}")
    dual = routing.get("dual_review")
    if not dual:
        _insert_action(
            conn,
            record,
            reviewer=reviewer,
            action=action,
            final_score=final_score,
            reason_code=reason_code,
            seconds_spent=seconds_spent,
            now=now,
        )
        conn.execute(
            "UPDATE score_records SET status = ?, final_score = ?, reviewer = ?, reviewed_at = ? WHERE record_id = ?",
            (status, final_score, reviewer, now, record["record_id"]),
        )
        conn.commit()
        db.append_audit(
            conn,
            actor=reviewer,
            action=f"record.{action}",
            object_type="ScoreRecord",
            object_id=record["record_id"],
            before={"ai_score": record["ai_score"], "status": record["status"]},
            after={"final_score": final_score, "reason_code": reason_code},
        )
        return {"finalized": True, "dual_review": None}

    reviews = list(dual.get("reviews") or [])
    if any(str(entry.get("reviewer", "")).strip() == reviewer.strip() for entry in reviews):
        raise ValueError("双人独立复核需由不同评分员完成")
    reviews.append(
        {
            "reviewer": reviewer,
            "action": action,
            "score": final_score,
            "reason_code": reason_code,
            "at": now,
        }
    )
    dual.update({"reviews": reviews, "stage": len(reviews), "required": int(dual.get("required", 2))})

    if len(reviews) == 1:
        dual["status"] = "awaiting_second"
        routing["dual_review"] = dual
        _insert_action(
            conn,
            record,
            reviewer=reviewer,
            action=action,
            final_score=final_score,
            reason_code=reason_code,
            seconds_spent=seconds_spent,
            now=now,
        )
        conn.execute(
            "UPDATE score_records SET routing = ? WHERE record_id = ?",
            (json.dumps(routing, ensure_ascii=False), record["record_id"]),
        )
        conn.commit()
        db.append_audit(
            conn,
            actor=reviewer,
            action="record.dual_review_first",
            object_type="ScoreRecord",
            object_id=record["record_id"],
            before={"ai_score": record["ai_score"], "status": record["status"]},
            after={"score": final_score, "awaiting": "second_reviewer"},
        )
        return {"finalized": False, "needs_second_review": True, "dual_review": dual}

    first_score = reviews[0].get("score")
    if len(reviews) == 2 and first_score != final_score:
        dual["status"] = "disagreement"
        routing.update(
            {
                "requires_human": True,
                "route": "senior_arbitration",
                "reason": "review_disagreement",
                "reasons": list(dict.fromkeys([*(routing.get("reasons") or []), "review_disagreement"])),
                "reason_text": f"双人独立复核不一致（{first_score} vs {final_score}），需高年资裁决",
            }
        )
        routing["dual_review"] = dual
        _insert_action(
            conn,
            record,
            reviewer=reviewer,
            action=action,
            final_score=final_score,
            reason_code=reason_code,
            seconds_spent=seconds_spent,
            now=now,
        )
        conn.execute(
            "UPDATE score_records SET routing = ? WHERE record_id = ?",
            (json.dumps(routing, ensure_ascii=False), record["record_id"]),
        )
        conn.commit()
        db.append_audit(
            conn,
            actor=reviewer,
            action="record.dual_review_disagreement",
            object_type="ScoreRecord",
            object_id=record["record_id"],
            before={"ai_score": record["ai_score"], "status": record["status"]},
            after={"first_score": first_score, "second_score": final_score},
        )
        return {"finalized": False, "needs_senior_review": True, "dual_review": dual}

    dual["status"] = "resolved"
    routing["dual_review"] = dual
    _insert_action(
        conn,
        record,
        reviewer=reviewer,
        action=action,
        final_score=final_score,
        reason_code=reason_code,
        seconds_spent=seconds_spent,
        now=now,
    )
    conn.execute(
        "UPDATE score_records SET status = ?, final_score = ?, reviewer = ?, reviewed_at = ?, routing = ? "
        "WHERE record_id = ?",
        (status, final_score, reviewer, now, json.dumps(routing, ensure_ascii=False), record["record_id"]),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=reviewer,
        action=f"record.{action}",
        object_type="ScoreRecord",
        object_id=record["record_id"],
        before={"ai_score": record["ai_score"], "status": record["status"]},
        after={"final_score": final_score, "dual_review": dual["status"], "reviews": len(reviews)},
    )
    return {"finalized": True, "dual_review": dual}
