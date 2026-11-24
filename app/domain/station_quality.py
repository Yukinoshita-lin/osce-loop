"""分站质量：按冻结版本汇总 QWK、证据定位、编辑率与完整性缺口。"""
from __future__ import annotations

from typing import Any

from .bundle import list_bundles
from .fairness import fairness_report
from .stats import quality_report


def station_quality_report(conn) -> list[dict[str, Any]]:
    bundles = list_bundles(conn)
    latest_by_station: dict[str, str] = {}
    for bundle in bundles:
        latest_by_station.setdefault(bundle["station_id"], bundle["bundle_id"])
    rows: list[dict[str, Any]] = []
    for bundle in bundles:
        report = quality_report(conn, bundle["bundle_id"])
        fairness = fairness_report(conn, bundle["bundle_id"])
        attempts = report["counts"]["attempts"]
        is_latest = latest_by_station.get(bundle["station_id"]) == bundle["bundle_id"]
        if attempts == 0 and not is_latest:
            continue
        rows.append(
            {
                "bundle_id": bundle["bundle_id"],
                "station_id": bundle["station_id"],
                "title": bundle["title"],
                "version": bundle["version"],
                "is_latest": is_latest,
                "attempts": attempts,
                "records": report["counts"]["records"],
                "qwk_ai": report["qwk_ai"],
                "qwk_human": report["qwk_human"],
                "qwk_near_cutoff": report["qwk_near_cutoff"],
                "evidence_rate": report["evidence"]["rate"],
                "edit_rate": report["review"]["edit_rate"],
                "integrity_gap_rate": report["integrity_gap_rate"],
                "fairness_gap_sd": fairness["overall_gap_sd"],
                "fairness_edit_gap_pp": fairness["edit_rate"]["gap_pp"],
            }
        )
    return rows
