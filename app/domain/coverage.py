"""蓝图覆盖度：按能力域汇总最新站包，识别空白域与过度重复。"""
from __future__ import annotations

from typing import Any

from .bundle import get_bundle, list_station_bundles

DEFAULT_BLUEPRINT = [
    {"domain": "communication", "label": "沟通与人文", "target_share": 0.20},
    {"domain": "history_taking", "label": "病史采集", "target_share": 0.25},
    {"domain": "assessment", "label": "评估与判断", "target_share": 0.20},
    {"domain": "clinical_reasoning", "label": "临床推理", "target_share": 0.15},
    {"domain": "procedures", "label": "操作技能", "target_share": 0.10},
    {"domain": "patient_safety", "label": "患者安全", "target_share": 0.10},
]
DOMAIN_LABELS = {entry["domain"]: entry["label"] for entry in DEFAULT_BLUEPRINT}


def blueprint_coverage(conn) -> dict[str, Any]:
    bundles = []
    for summary in list_station_bundles(conn):
        bundle = get_bundle(conn, summary["bundle_id"])
        if bundle:
            bundles.append(bundle)

    buckets: dict[str, dict[str, Any]] = {}
    total_weight = 0.0
    for bundle in bundles:
        for item in bundle["payload"].get("rubric", []):
            domain = str(item.get("domain") or "other")
            weight = float(item.get("weight", 0))
            bucket = buckets.setdefault(
                domain, {"domain": domain, "label": DOMAIN_LABELS.get(domain, domain), "items": 0, "weight": 0.0, "stations": set()}
            )
            bucket["items"] += 1
            bucket["weight"] += weight
            bucket["stations"].add(bundle["station_id"])
            total_weight += weight

    rows = []
    for bucket in buckets.values():
        share = round(bucket["weight"] / total_weight, 3) if total_weight else 0.0
        blueprint = next((entry for entry in DEFAULT_BLUEPRINT if entry["domain"] == bucket["domain"]), None)
        target = blueprint["target_share"] if blueprint else None
        if len(bucket["stations"]) <= 1:
            status = "single_station"
        elif target is not None and share > target + 0.10:
            status = "overrepresented"
        else:
            status = "ok"
        rows.append(
            {
                "domain": bucket["domain"],
                "label": bucket["label"],
                "stations": len(bucket["stations"]),
                "items": bucket["items"],
                "weight": round(bucket["weight"], 2),
                "share": share,
                "target": target,
                "status": status,
            }
        )
    rows.sort(key=lambda entry: entry["share"], reverse=True)

    covered = {row["domain"] for row in rows}
    uncovered = [
        {
            "domain": entry["domain"],
            "label": entry["label"],
            "target": entry["target_share"],
        }
        for entry in DEFAULT_BLUEPRINT
        if entry["domain"] not in covered
    ]
    overrepresented = [
        row["domain"]
        for row in rows
        if row["target"] is not None and row["share"] > row["target"] + 0.10
    ]
    extra = [row["domain"] for row in rows if row["domain"] not in DOMAIN_LABELS]
    return {
        "domains": rows,
        "uncovered": uncovered,
        "overrepresented": overrepresented,
        "extra_domains": extra,
        "stations": len(bundles),
        "total_weight": round(total_weight, 2),
        "note": "覆盖度按每个考站最新冻结版本统计；目标占比来自默认六域蓝图，可按院校培养目标调整。",
    }
