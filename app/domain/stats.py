"""评分质量统计：QWK、证据定位、复核行为与路由分布。"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from ..config import REASON_LABELS


def quadratic_weighted_kappa(
    ratings_a: Iterable[int],
    ratings_b: Iterable[int],
    min_rating: int = 0,
    max_rating: int = 2,
) -> float | None:
    pairs = [(int(a), int(b)) for a, b in zip(ratings_a, ratings_b)]
    if not pairs:
        return None
    n = len(pairs)
    k = max_rating - min_rating + 1
    observed = [[0] * k for _ in range(k)]
    hist_a = [0] * k
    hist_b = [0] * k
    for a, b in pairs:
        if not (min_rating <= a <= max_rating and min_rating <= b <= max_rating):
            continue
        observed[a - min_rating][b - min_rating] += 1
        hist_a[a - min_rating] += 1
        hist_b[b - min_rating] += 1
    valid_n = sum(hist_a)
    if valid_n == 0:
        return None

    weights = [
        [((i - j) ** 2) / ((k - 1) ** 2) for j in range(k)]
        for i in range(k)
    ]
    numerator = 0.0
    denominator = 0.0
    for i in range(k):
        for j in range(k):
            expected = hist_a[i] * hist_b[j] / valid_n
            numerator += weights[i][j] * observed[i][j]
            denominator += weights[i][j] * expected
    if denominator == 0:
        return 1.0 if numerator == 0 else 0.0
    return round(1.0 - numerator / denominator, 4)


def _quote_in_transcript(quote: str, transcript: list[dict[str, Any]], start_ms: int, end_ms: int) -> bool:
    if not quote or quote.startswith("（") or quote.startswith("该"):
        return False
    for utt in transcript:
        if not (str(utt.get("text", "")).casefold() in quote.casefold() or quote.casefold() in str(utt.get("text", "")).casefold()):
            continue
        utt_start = int(utt.get("start_ms", 0))
        utt_end = int(utt.get("end_ms", 0))
        overlap = min(utt_end, end_ms) - max(utt_start, start_ms)
        if overlap >= 0 or abs(start_ms - utt_start) <= 2000:
            return True
    return False


def quality_report(conn, bundle_id: str | None = None) -> dict[str, Any]:
    attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts").fetchall()]
    records = [dict(r) for r in conn.execute("SELECT * FROM score_records").fetchall()]
    actions = [dict(r) for r in conn.execute("SELECT * FROM review_actions").fetchall()]
    transcripts = {
        row["attempt_id"]: json.loads(row["payload"])
        for row in conn.execute("SELECT attempt_id, payload FROM transcripts").fetchall()
    }
    if bundle_id:
        attempts = [a for a in attempts if a["bundle_id"] == bundle_id]
        attempt_ids = {a["attempt_id"] for a in attempts}
        records = [r for r in records if r["attempt_id"] in attempt_ids]
        record_ids = {r["record_id"] for r in records}
        actions = [a for a in actions if a["record_id"] in record_ids]
        transcripts = {key: value for key, value in transcripts.items() if key in attempt_ids}
    attempt_by_id = {a["attempt_id"]: a for a in attempts}

    ai_pairs: list[tuple[int, int]] = []
    human_pairs: list[tuple[int, int]] = []
    near_pairs: list[tuple[int, int]] = []
    evidence_total = 0
    evidence_ok = 0
    ai_records = 0
    for rec in records:
        attempt = attempt_by_id.get(rec["attempt_id"], {})
        if rec["ai_score"] is not None and rec["gold_score"] is not None:
            ai_pairs.append((rec["ai_score"], rec["gold_score"]))
            if attempt.get("near_cutoff"):
                near_pairs.append((rec["ai_score"], rec["gold_score"]))
        if (
            rec["final_score"] is not None
            and rec["gold_score"] is not None
            and rec["status"] in ("accepted", "corrected", "rejected", "manual_scored")
        ):
            human_pairs.append((rec["final_score"], rec["gold_score"]))
        if rec["ai_score"] is not None:
            ai_records += 1
            try:
                evidence = json.loads(rec["ai_evidence"] or "[]")
            except (TypeError, ValueError):
                evidence = []
            for ev in evidence:
                evidence_total += 1
                transcript = transcripts.get(rec["attempt_id"], [])
                if ev.get("evidence_kind") == "absence":
                    evidence_ok += 1
                elif _quote_in_transcript(
                    ev.get("quote", ""),
                    transcript,
                    int(ev.get("start_ms", 0)),
                    int(ev.get("end_ms", 0)),
                ):
                    evidence_ok += 1

    reviewed = [a for a in actions if a["action"] in ("accept", "correct", "reject", "manual_score")]
    accepted = sum(1 for a in reviewed if a["action"] == "accept")
    corrected = sum(1 for a in reviewed if a["action"] in ("correct", "reject"))
    manual_scored = sum(1 for a in reviewed if a["action"] == "manual_score")
    edit_rate = round(corrected / len(reviewed), 3) if reviewed else None
    accept_rate = round(accepted / len(reviewed), 3) if reviewed else None
    avg_review_seconds = (
        round(sum(float(a["seconds_spent"] or 0) for a in reviewed if a["seconds_spent"]) / max(1, len([a for a in reviewed if a["seconds_spent"]])), 1)
        if any(a["seconds_spent"] for a in reviewed)
        else None
    )

    reasons = Counter()
    for rec in records:
        try:
            routing = json.loads(rec["routing"])
        except (TypeError, ValueError):
            continue
        reason = routing.get("reason")
        if reason:
            reasons[reason] += 1

    qwk_ai = quadratic_weighted_kappa([p[0] for p in ai_pairs], [p[1] for p in ai_pairs])
    qwk_human = quadratic_weighted_kappa([p[0] for p in human_pairs], [p[1] for p in human_pairs])
    qwk_near = quadratic_weighted_kappa([p[0] for p in near_pairs], [p[1] for p in near_pairs])

    integrity_attempts = sum(1 for a in attempts if a["integrity_gap"])
    item_disagreement: dict[str, float] = {}
    item_reviews: dict[str, list[str]] = {}
    record_by_id = {r["record_id"]: r for r in records}
    for action in actions:
        rec = record_by_id.get(action["record_id"])
        if not rec:
            continue
        item_reviews.setdefault(rec["item_id"], []).append(action["action"])
    for item_id, acts in item_reviews.items():
        bad = sum(1 for a in acts if a in ("correct", "reject"))
        item_disagreement[item_id] = round(bad / len(acts), 3)

    return {
        "counts": {
            "attempts": len(attempts),
            "records": len(records),
            "ai_records": ai_records,
            "gold_records": sum(1 for r in records if r["gold_score"] is not None),
            "reviewed_actions": len(reviewed),
        },
        "qwk_ai": qwk_ai,
        "qwk_human": qwk_human,
        "qwk_near_cutoff": qwk_near,
        "evidence": {
            "total": evidence_total,
            "localized": evidence_ok,
            "rate": round(evidence_ok / evidence_total, 4) if evidence_total else None,
        },
        "review": {
            "edit_rate": edit_rate,
            "accept_rate": accept_rate,
            "manual_scored": manual_scored,
            "avg_seconds": avg_review_seconds,
            "reviewed": len(reviewed),
        },
        "routing": [
            {
                "reason": reason,
                "label": REASON_LABELS.get(reason, reason),
                "count": count,
            }
            for reason, count in reasons.most_common()
        ],
        "integrity_gap_rate": round(integrity_attempts / len(attempts), 4) if attempts else 0,
        "item_disagreement": item_disagreement,
        "fairness": {
            "subgroup_gap_sd": None,
            "note": "演示数据未采集性别/生源/语言背景，公平性回路待接入真实分组字段。",
        },
    }


def domain_scores(payload: dict[str, Any], scores: dict[str, int | None]) -> list[dict[str, Any]]:
    domain_map: dict[str, dict[str, float]] = {}
    for item in payload.get("rubric", []):
        domain = item.get("domain", "other")
        weight = float(item.get("weight", 0))
        bucket = domain_map.setdefault(domain, {"earned": 0.0, "weight": 0.0})
        bucket["weight"] += weight
        score = scores.get(item.get("item_id"))
        if score is not None:
            bucket["earned"] += weight * float(score) / 2.0
    result = []
    for domain, bucket in domain_map.items():
        result.append(
            {
                "domain": domain,
                "score": round(bucket["earned"] / bucket["weight"] * 100, 1) if bucket["weight"] else None,
                "weight": bucket["weight"],
            }
        )
    return sorted(result, key=lambda x: -x["weight"])
