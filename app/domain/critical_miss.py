"""S6 关键项漏判根因分析：AI 判阴性但金标准/人工判阳性，必须逐条归因。"""
from __future__ import annotations

import json
from typing import Any

from .. import db

ROOT_CAUSES = {
    "evidence_gap": "证据缺失或定位失败",
    "asr_error": "ASR 转写错误",
    "anchor_ambiguity": "行为锚歧义",
    "model_bias": "模型系统性偏差",
    "context_missing": "上下文或病例信息不足",
    "other": "其他原因",
}
ACTIONS = {
    "rubric_revision": "修订行为锚",
    "evidence_probe": "补充证据探针",
    "asr_adaptation": "ASR 词表/声学适配",
    "model_calibration": "重新校准模型",
    "training": "评分员培训",
    "monitor": "继续观察",
}
MIN_NOTE = 6


def _item_map(conn) -> dict[tuple[str, str], dict[str, Any]]:
    mapping: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute("SELECT bundle_id, payload FROM bundles").fetchall():
        payload = json.loads(row["payload"])
        for item in payload.get("rubric", []):
            mapping[(row["bundle_id"], item["item_id"])] = item
    return mapping


def _miss_rows(conn) -> list[dict[str, Any]]:
    items = _item_map(conn)
    rows = conn.execute(
        "SELECT sr.record_id, sr.attempt_id, sr.item_id, sr.ai_score, sr.ai_confidence, "
        "sr.ai_rationale, sr.gold_score, sr.final_score, sr.status, sr.created_at, "
        "a.bundle_id, a.candidate_code, a.candidate_name, a.risk_level, "
        "a.status AS attempt_status "
        "FROM score_records sr JOIN attempts a ON a.attempt_id = sr.attempt_id "
        "WHERE sr.ai_score = 0"
    ).fetchall()
    misses: list[dict[str, Any]] = []
    for row in rows:
        item = items.get((row["bundle_id"], row["item_id"]))
        if not item or not item.get("critical"):
            continue
        gold = row["gold_score"]
        final = row["final_score"]
        reference = gold if gold is not None else final
        if reference is None or int(reference) <= 0:
            continue
        misses.append(
            {
                "record_id": row["record_id"],
                "attempt_id": row["attempt_id"],
                "candidate": f"{row['candidate_code']} · {row['candidate_name']}",
                "risk_level": row["risk_level"],
                "bundle_id": row["bundle_id"],
                "item_id": row["item_id"],
                "item_title": item.get("title"),
                "ai_score": int(row["ai_score"]),
                "ai_confidence": row["ai_confidence"],
                "ai_rationale": row["ai_rationale"],
                "reference_score": int(reference),
                "reference_source": "gold" if gold is not None else "human",
                "record_status": row["status"],
                "attempt_status": row["attempt_status"],
                "created_at": row["created_at"],
            }
        )
    return misses


def critical_misses(conn, limit: int = 50) -> dict[str, Any]:
    reviews = {
        row["record_id"]: dict(row)
        for row in conn.execute("SELECT * FROM critical_miss_reviews").fetchall()
    }
    items = []
    for miss in _miss_rows(conn):
        review = reviews.get(miss["record_id"])
        items.append({**miss, "review": review, "resolved": review is not None})
    items.sort(key=lambda entry: (entry["resolved"], entry["created_at"]), reverse=False)
    unresolved = sum(1 for entry in items if not entry["resolved"])
    return {
        "total": len(items),
        "unresolved": unresolved,
        "resolved": len(items) - unresolved,
        "items": items[:limit],
        "root_causes": [
            {"key": key, "label": label} for key, label in ROOT_CAUSES.items()
        ],
        "actions": [{"key": key, "label": label} for key, label in ACTIONS.items()],
        "note": "关键项漏判必须逐条归因并绑定纠正动作；未归因项会持续计入 G3 的关键项漏判指标。",
    }


def record_critical_miss_review(
    conn,
    record_id: str,
    *,
    root_cause: str,
    action: str,
    note: str,
    actor: str,
) -> dict[str, Any]:
    if root_cause not in ROOT_CAUSES:
        raise ValueError("未知的根因分类")
    if action not in ACTIONS:
        raise ValueError("未知的纠正动作")
    note = (note or "").strip()
    if len(note) < MIN_NOTE:
        raise ValueError(f"根因说明不少于 {MIN_NOTE} 字")
    if len(note) > 500:
        raise ValueError("根因说明不能超过 500 字")
    miss = next(
        (entry for entry in _miss_rows(conn) if entry["record_id"] == record_id), None
    )
    if miss is None:
        raise ValueError("该评分记录不是关键项漏判")
    before_row = conn.execute(
        "SELECT * FROM critical_miss_reviews WHERE record_id = ?", (record_id,)
    ).fetchone()
    before = dict(before_row) if before_row else None
    timestamp = db.now_iso()
    if before:
        conn.execute(
            "UPDATE critical_miss_reviews SET root_cause = ?, action = ?, note = ?, "
            "actor = ?, updated_at = ? WHERE record_id = ?",
            (root_cause, action, note, actor, timestamp, record_id),
        )
    else:
        conn.execute(
            "INSERT INTO critical_miss_reviews(record_id, root_cause, action, note, actor, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (record_id, root_cause, action, note, actor, timestamp, timestamp),
        )
    conn.commit()
    review = dict(
        conn.execute(
            "SELECT * FROM critical_miss_reviews WHERE record_id = ?", (record_id,)
        ).fetchone()
    )
    db.append_audit(
        conn,
        actor=actor,
        action="quality.critical_miss_review",
        object_type="ScoreRecord",
        object_id=record_id,
        before=before,
        after={
            "item_id": miss["item_id"],
            "root_cause": root_cause,
            "action": action,
            "note": note,
            "reference_score": miss["reference_score"],
        },
    )
    return review
