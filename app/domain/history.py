"""考生纵向趋势：同站历史场次对比与能力域变化。"""
from __future__ import annotations

from typing import Any

from .bundle import get_bundle
from .stats import domain_scores


def _weighted_total(items: list[dict[str, Any]], scores: dict[str, int | None]) -> float | None:
    earned = 0.0
    weight_sum = 0.0
    for item in items:
        score = scores.get(item["item_id"])
        if score is None:
            continue
        weight = float(item.get("weight", 0))
        weight_sum += weight
        earned += weight * float(score) / 2.0
    if weight_sum <= 0:
        return None
    return round(earned / weight_sum * 100, 2)


def _attempt_scores(conn, attempt_id: str) -> dict[str, int | None]:
    return {
        row["item_id"]: row["final_score"] if row["final_score"] is not None else row["ai_score"]
        for row in conn.execute(
            "SELECT item_id, final_score, ai_score FROM score_records WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchall()
    }


def candidate_history(conn, attempt_id: str) -> dict[str, Any]:
    current = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    if not current:
        return {"series": [], "previous": None, "delta_total": None, "domain_deltas": [], "note": "场次不存在"}
    current_bundle = get_bundle(conn, current["bundle_id"])
    if not current_bundle:
        return {"series": [], "previous": None, "delta_total": None, "domain_deltas": [], "note": "站包不存在"}
    station_id = current_bundle["station_id"]

    rows = conn.execute(
        "SELECT * FROM attempts WHERE candidate_code = ? ORDER BY created_at, rowid",
        (current["candidate_code"],),
    ).fetchall()
    series = []
    previous_entry = None
    current_entry = None
    for row in rows:
        bundle = get_bundle(conn, row["bundle_id"])
        if not bundle or bundle["station_id"] != station_id:
            continue
        scores = _attempt_scores(conn, row["attempt_id"])
        total = _weighted_total(bundle["payload"].get("rubric", []), scores)
        entry = {
            "attempt_id": row["attempt_id"],
            "bundle_id": bundle["bundle_id"],
            "version": bundle["version"],
            "status": row["status"],
            "total": total,
            "created_at": row["created_at"],
        }
        series.append(entry)
        if row["attempt_id"] == attempt_id:
            current_entry = entry
        elif current_entry is None and total is not None:
            previous_entry = entry

    if not previous_entry or not current_entry or current_entry["total"] is None or previous_entry["total"] is None:
        return {
            "series": series,
            "previous": previous_entry,
            "delta_total": None,
            "domain_deltas": [],
            "note": "该考生暂无同站历史场次可比对。" if not previous_entry else "历史场次缺少可比较分数。",
        }

    previous_scores = _attempt_scores(conn, previous_entry["attempt_id"])
    current_scores = _attempt_scores(conn, current_entry["attempt_id"])
    previous_bundle = get_bundle(conn, previous_entry["bundle_id"])
    previous_domains = {
        row["domain"]: row["score"]
        for row in domain_scores(previous_bundle["payload"], previous_scores)
    }
    current_domains = {
        row["domain"]: row["score"]
        for row in domain_scores(current_bundle["payload"], current_scores)
    }
    deltas = []
    for domain in sorted(set(previous_domains) | set(current_domains)):
        before = previous_domains.get(domain)
        after = current_domains.get(domain)
        if before is None or after is None:
            continue
        deltas.append(
            {
                "domain": domain,
                "previous": before,
                "current": after,
                "delta": round(after - before, 1),
            }
        )
    return {
        "series": series,
        "previous": previous_entry,
        "delta_total": round(current_entry["total"] - previous_entry["total"], 2),
        "domain_deltas": deltas,
        "note": "同站历史对比仅用于形成性反馈，不改变本次成绩。",
    }
