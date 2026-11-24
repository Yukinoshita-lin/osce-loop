"""冻结前待核验项的逐条裁决：指南待核验与三方一致差异都必须给结论并留痕。

差异由 validate_bundle 生成，带稳定 diff_id。人工只能选择：
- accept：确认差异可接受（必须写明理由），按当前内容冻结；
- fix：要求返回修改（冻结被阻断，修改后差异消失再重新提交）。
"""
from __future__ import annotations

from typing import Any

from .. import db

DECISIONS = ("accept", "fix")
DECISION_LABELS = {"accept": "接受差异", "fix": "返回修改"}
MIN_ACCEPT_NOTE = 4


def adjudicate_diffs(
    diffs: list[dict[str, Any]],
    decisions: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """把人工裁决与当前差异清单对齐，返回是否可冻结及未完成原因。"""
    diff_by_id = {str(diff.get("diff_id")): diff for diff in diffs if diff.get("diff_id")}
    results: list[dict[str, Any]] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in decisions or []:
        diff_id = str(raw.get("diff_id") or "").strip()
        decision = str(raw.get("decision") or "").strip()
        note = str(raw.get("note") or "").strip()
        if diff_id not in diff_by_id:
            invalid.append(f"未知差异 {diff_id or '(空)'}")
            continue
        if decision not in DECISIONS:
            invalid.append(f"{diff_id}: 裁决必须是 accept 或 fix")
            continue
        if decision == "accept" and len(note) < MIN_ACCEPT_NOTE:
            invalid.append(f"{diff_id}: 接受差异必须填写不少于 {MIN_ACCEPT_NOTE} 字的理由")
            continue
        if diff_id in seen:
            invalid.append(f"{diff_id}: 同一差异重复裁决")
            continue
        seen.add(diff_id)
        diff = diff_by_id[diff_id]
        results.append(
            {
                "diff_id": diff_id,
                "type": diff.get("type"),
                "severity": diff.get("severity"),
                "decision": decision,
                "decision_label": DECISION_LABELS[decision],
                "note": note,
            }
        )
    pending = [diff_id for diff_id in diff_by_id if diff_id not in seen]
    fix_required = [item["diff_id"] for item in results if item["decision"] == "fix"]
    return {
        "total": len(diff_by_id),
        "decided": len(results),
        "pending": pending,
        "fix_required": fix_required,
        "invalid": invalid,
        "complete": not pending and not invalid,
        "can_freeze": not pending and not invalid and not fix_required,
        "results": results,
    }


def record_adjudications(
    conn,
    bundle_id: str,
    results: list[dict[str, Any]],
    *,
    actor: str,
) -> int:
    if not results:
        return 0
    created_at = db.now_iso()
    for item in results:
        conn.execute(
            "INSERT OR IGNORE INTO consistency_adjudications(adj_id, bundle_id, diff_id, diff_type, "
            "severity, decision, note, actor, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                db.new_id("adj"),
                bundle_id,
                item["diff_id"],
                item.get("type") or "",
                item.get("severity") or "",
                item["decision"],
                item.get("note") or "",
                actor,
                created_at,
            ),
        )
    conn.commit()
    return len(results)


def list_adjudications(conn, bundle_id: str) -> list[dict[str, Any]]:
    return db.rows_to_dicts(
        conn.execute(
            "SELECT * FROM consistency_adjudications WHERE bundle_id = ? ORDER BY created_at",
            (bundle_id,),
        ).fetchall()
    )
