"""跨站能力画像：把同一考生在不同站包的表现映射到六个能力域。"""
from __future__ import annotations

import json
from typing import Any

from .coverage import DOMAIN_LABELS


def _scored_domains(
    items: list[dict[str, Any]], scores: dict[str, int | None]
) -> list[dict[str, Any]]:
    """只统计实际有评分的项，避免未评分项稀释能力域画像。"""
    buckets: dict[str, dict[str, Any]] = {}
    for item in items:
        score = scores.get(item.get("item_id"))
        if score is None:
            continue
        domain = item.get("domain") or "other"
        bucket = buckets.setdefault(
            domain, {"earned": 0.0, "weight": 0.0, "items": 0}
        )
        weight = float(item.get("weight", 0))
        bucket["weight"] += weight
        bucket["earned"] += weight * float(score) / 2.0
        bucket["items"] += 1
    return [
        {
            "domain": domain,
            "label": DOMAIN_LABELS.get(domain, domain),
            "score": round(bucket["earned"] / bucket["weight"] * 100, 1) if bucket["weight"] else None,
            "weight": bucket["weight"],
            "items": bucket["items"],
        }
        for domain, bucket in buckets.items()
    ]


def _attempt_total(items: list[dict[str, Any]], scores: dict[str, int | None]) -> float | None:
    earned = 0.0
    weight = 0.0
    for item in items:
        score = scores.get(item.get("item_id"))
        if score is None:
            continue
        item_weight = float(item.get("weight", 0))
        weight += item_weight
        earned += item_weight * float(score) / 2.0
    return round(earned / weight * 100, 1) if weight else None


def candidate_cross_station_profile(conn, attempt_id: str) -> dict[str, Any] | None:
    """聚合已发布场次与当前场次；未发布的其他场次不进入学生可见画像。"""
    current = conn.execute(
        "SELECT attempt_id, candidate_code, candidate_name FROM attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    if not current:
        return None
    rows = conn.execute(
        "SELECT attempt_id, bundle_id, status, created_at FROM attempts "
        "WHERE candidate_code = ? AND (status = 'published' OR attempt_id = ?) "
        "ORDER BY created_at, rowid",
        (current["candidate_code"], attempt_id),
    ).fetchall()

    aggregates: dict[str, dict[str, Any]] = {}
    attempt_summaries: list[dict[str, Any]] = []
    station_ids: set[str] = set()
    for row in rows:
        bundle_row = conn.execute(
            "SELECT station_id, title, version, payload FROM bundles WHERE bundle_id = ?",
            (row["bundle_id"],),
        ).fetchone()
        if not bundle_row:
            continue
        payload = json.loads(bundle_row["payload"] or "{}")
        items = payload.get("rubric") or []
        scores: dict[str, int | None] = {}
        for record in conn.execute(
            "SELECT item_id, final_score, ai_score FROM score_records WHERE attempt_id = ?",
            (row["attempt_id"],),
        ).fetchall():
            score = record["final_score"] if record["final_score"] is not None else record["ai_score"]
            if score is not None:
                scores[record["item_id"]] = int(score)
        if not scores:
            continue
        station_ids.add(bundle_row["station_id"])
        domains = _scored_domains(items, scores)
        for entry in domains:
            bucket = aggregates.setdefault(
                entry["domain"],
                {
                    "domain": entry["domain"],
                    "label": DOMAIN_LABELS.get(entry["domain"], entry["domain"]),
                    "earned": 0.0,
                    "weight": 0.0,
                    "items": 0,
                    "attempts": set(),
                },
            )
            if entry["score"] is not None and entry["weight"]:
                bucket["earned"] += entry["score"] / 100.0 * entry["weight"]
                bucket["weight"] += entry["weight"]
                bucket["attempts"].add(row["attempt_id"])
            bucket["items"] += entry["items"]
        attempt_summaries.append(
            {
                "attempt_id": row["attempt_id"],
                "station_id": bundle_row["station_id"],
                "station_title": bundle_row["title"],
                "version": bundle_row["version"],
                "status": row["status"],
                "created_at": row["created_at"],
                "total": _attempt_total(items, scores),
                "domains": [
                    {
                        "domain": entry["domain"],
                        "label": DOMAIN_LABELS.get(entry["domain"], entry["domain"]),
                        "score": entry["score"],
                    }
                    for entry in domains
                ],
            }
        )

    domains = []
    for bucket in aggregates.values():
        score = round(bucket["earned"] / bucket["weight"] * 100, 1) if bucket["weight"] else None
        domains.append(
            {
                "domain": bucket["domain"],
                "label": bucket["label"],
                "score": score,
                "weight": round(bucket["weight"], 1),
                "items": bucket["items"],
                "attempts": len(bucket["attempts"]),
            }
        )
    domains.sort(key=lambda entry: (-(entry["score"] or 0), -entry["weight"]))
    scored = [entry for entry in domains if entry["score"] is not None]
    strongest = None
    weakest = None
    if len(scored) >= 2 and max(entry["score"] for entry in scored) != min(entry["score"] for entry in scored):
        strongest = max(scored, key=lambda entry: entry["score"])
        weakest = min(scored, key=lambda entry: entry["score"])
    return {
        "candidate_code": current["candidate_code"],
        "candidate_name": current["candidate_name"],
        "attempts_count": len(attempt_summaries),
        "stations_count": len(station_ids),
        "domains": domains,
        "attempts": attempt_summaries,
        "strongest": strongest,
        "weakest": weakest,
        "note": (
            "跨站画像按同一考生代码聚合已发布场次与当前场次；不同站包的能力域权重按实际评分项汇总，"
            "仅用于形成性反馈，不用于跨站排名。"
        ),
    }
