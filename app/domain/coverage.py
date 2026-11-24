"""蓝图覆盖度：按年度生效蓝图汇总最新站包，识别空白域与过度重复。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .blueprint import DEFAULT_BLUEPRINT, DOMAIN_LABELS, latest_approved
from .bundle import get_bundle, list_station_bundles


def blueprint_coverage(conn) -> dict[str, Any]:
    current_year = datetime.now().year
    active = latest_approved(conn, current_year) or latest_approved(conn)
    targets = active["domains"] if active else DEFAULT_BLUEPRINT
    target_by_domain = {entry["domain"]: entry for entry in targets}
    labels = {**DOMAIN_LABELS, **{entry["domain"]: entry["label"] for entry in targets}}

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
                domain, {"domain": domain, "label": labels.get(domain, domain), "items": 0, "weight": 0.0, "stations": set()}
            )
            bucket["items"] += 1
            bucket["weight"] += weight
            bucket["stations"].add(bundle["station_id"])
            total_weight += weight

    rows = []
    for bucket in buckets.values():
        share = round(bucket["weight"] / total_weight, 3) if total_weight else 0.0
        blueprint_entry = target_by_domain.get(bucket["domain"])
        target = blueprint_entry["target_share"] if blueprint_entry else None
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
        for entry in targets
        if entry["domain"] not in covered
    ]
    overrepresented = [
        row["domain"]
        for row in rows
        if row["target"] is not None and row["share"] > row["target"] + 0.10
    ]
    extra = [row["domain"] for row in rows if row["domain"] not in target_by_domain]
    if active:
        source_note = (
            f"目标占比来自 {active['year']} 年度蓝图 v{active['version']}"
            f"（{active['approved_by']} 批准）。"
        )
    else:
        source_note = "尚未批准年度蓝图，暂按默认六域目标计算；请由考务起草并人工批准。"
    return {
        "domains": rows,
        "uncovered": uncovered,
        "overrepresented": overrepresented,
        "extra_domains": extra,
        "stations": len(bundles),
        "total_weight": round(total_weight, 2),
        "blueprint": {
            "source": "approved" if active else "default",
            "year": active["year"] if active else None,
            "version": active["version"] if active else None,
            "approved_by": active["approved_by"] if active else None,
            "approved_at": active["approved_at"] if active else None,
            "stale": bool(active and active["year"] != current_year),
        },
        "note": "覆盖度按每个考站最新冻结版本统计；" + source_note,
    }
