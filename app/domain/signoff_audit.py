"""签字链抽查：抽样核对人工签字、发布时间与关键项确认。"""
from __future__ import annotations

import json
from typing import Any


def _critical_ids(payload: dict[str, Any]) -> set[str]:
    return {
        item.get("item_id")
        for item in payload.get("rubric") or []
        if item.get("critical")
    }


def audit_signoff_chain(conn, sample_size: int = 20) -> dict[str, Any]:
    sample_size = max(1, min(sample_size, 200))
    rows = conn.execute(
        "SELECT * FROM attempts WHERE status IN ('signed_off', 'published') "
        "ORDER BY COALESCE(published_at, signed_off_at, created_at) DESC, rowid DESC LIMIT ?",
        (sample_size,),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        attempt = dict(row)
        issues: list[str] = []
        if not attempt.get("signed_off_by") or not attempt.get("signed_off_at"):
            issues.append("缺少人工签字人或签字时间")
        if attempt.get("signed_off_at") and attempt.get("published_at"):
            if attempt["published_at"] < attempt["signed_off_at"]:
                issues.append("发布时间早于人工签字时间")
        if attempt.get("status") == "published" and not attempt.get("published_at"):
            issues.append("状态为已发布但缺少发布时间")

        bundle_row = conn.execute(
            "SELECT payload FROM bundles WHERE bundle_id = ?", (attempt["bundle_id"],)
        ).fetchone()
        records = [
            dict(record)
            for record in conn.execute(
                "SELECT * FROM score_records WHERE attempt_id = ?", (attempt["attempt_id"],)
            ).fetchall()
        ]
        if bundle_row:
            critical = _critical_ids(json.loads(bundle_row["payload"] or "{}"))
            for record in records:
                if record["item_id"] not in critical:
                    continue
                if record["status"] in ("pending_review", "manual_pending") or record["final_score"] is None:
                    issues.append(f"关键项 {record['item_id']} 尚未完成人工确认")
        for record in records:
            if record.get("ai_score") is not None and not (
                record.get("model_version") and record.get("prompt_version") and record.get("rubric_version")
            ):
                issues.append(f"{record['item_id']} 缺少模型/提示词/评分表版本戳")
                break
        if attempt.get("integrity_gap"):
            pending = [record for record in records if record["status"] in ("pending_review", "manual_pending")]
            if pending:
                issues.append(f"完整性缺口场次仍有 {len(pending)} 条记录待人工复核")
        items.append(
            {
                "attempt_id": attempt["attempt_id"],
                "candidate_code": attempt["candidate_code"],
                "status": attempt["status"],
                "signed_off_by": attempt.get("signed_off_by"),
                "signed_off_at": attempt.get("signed_off_at"),
                "published_at": attempt.get("published_at"),
                "ok": not issues,
                "issues": issues,
            }
        )
    passed = sum(1 for item in items if item["ok"])
    return {
        "sample_size": sample_size,
        "checked": len(items),
        "passed": passed,
        "failed": len(items) - passed,
        "pass_rate": round(passed / len(items), 3) if items else None,
        "issues": [item for item in items if not item["ok"]],
        "items": items,
        "note": (
            "抽查范围：最近已签字/已发布场次。通过标准：签字人与时间完整、发布时间不早于签字、"
            "关键项已完成人工确认、AI 记录带三元版本戳、完整性缺口场次无待复核记录。"
        ),
    }
