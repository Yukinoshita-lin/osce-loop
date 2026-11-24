"""S7 冻结站包完整性核验：哈希、派生资产、评分版本戳与冻结审计锚点。"""
from __future__ import annotations

import json
from typing import Any

from .. import db
from .bundle import canonical_json, compute_bundle_hash
from .rubric import derive_assets


def _check(
    key: str,
    label: str,
    passed: bool,
    *,
    expected: Any = None,
    actual: Any = None,
    detail: Any = None,
    missing: bool = False,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "passed": bool(passed),
        "missing": missing,
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }


def verify_bundle_integrity(
    conn,
    bundle_id: str,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """只读重算并核对冻结内容；不修改站包，核验结论写入审计。"""
    row = conn.execute("SELECT * FROM bundles WHERE bundle_id = ?", (bundle_id,)).fetchone()
    if not row:
        raise ValueError("站包不存在")
    bundle = dict(row)
    payload = json.loads(bundle["payload"])
    derived = json.loads(bundle["derived"])
    stored_hash = bundle["bundle_hash"]

    checks: list[dict[str, Any]] = []
    recomputed = compute_bundle_hash(payload, derived)
    checks.append(
        _check(
            "hash",
            "bundle_hash 与 payload + derived 重算一致",
            recomputed == stored_hash,
            expected=stored_hash,
            actual=recomputed,
        )
    )

    rederived = derive_assets(payload)
    checks.append(
        _check(
            "derived",
            "四类派生资产可由 payload 重算且与冻结值一致",
            canonical_json(rederived) == canonical_json(derived),
            detail={
                "slots": len(derived.get("slots") or []),
                "probes": len(derived.get("probes") or []),
                "checkpoints": len(derived.get("checkpoints") or []),
                "feedback": len(derived.get("feedback") or []),
            },
        )
    )

    audit_row = conn.execute(
        "SELECT after FROM audit_events WHERE action = 'bundle.freeze' AND object_id = ? "
        "ORDER BY rowid LIMIT 1",
        (bundle_id,),
    ).fetchone()
    audit_hash = None
    if audit_row:
        try:
            audit_hash = json.loads(audit_row["after"]).get("bundle_hash")
        except (TypeError, ValueError):
            audit_hash = None
    checks.append(
        _check(
            "audit",
            "冻结审计记录中的版本哈希与当前站包一致",
            audit_hash == stored_hash,
            expected=stored_hash,
            actual=audit_hash,
            missing=audit_hash is None,
        )
    )

    record_rows = conn.execute(
        "SELECT sr.rubric_version AS rubric_version, COUNT(*) AS n "
        "FROM score_records sr JOIN attempts a ON a.attempt_id = sr.attempt_id "
        "WHERE a.bundle_id = ? GROUP BY sr.rubric_version",
        (bundle_id,),
    ).fetchall()
    mismatched = [
        {"rubric_version": entry["rubric_version"], "records": entry["n"]}
        for entry in record_rows
        if entry["rubric_version"] != stored_hash
    ]
    checks.append(
        _check(
            "records",
            "评分记录的 rubric_version 均指向本站包哈希",
            not mismatched,
            detail={
                "records": sum(entry["n"] for entry in record_rows),
                "mismatched": mismatched,
            },
        )
    )

    hard_failures = [c for c in checks if not c["passed"] and not c["missing"]]
    missing = [c for c in checks if c["missing"]]
    if hard_failures:
        status = "fail"
    elif missing:
        status = "warn"
    else:
        status = "pass"

    checked_at = db.now_iso()
    result = {
        "bundle_id": bundle_id,
        "station_id": bundle["station_id"],
        "title": bundle["title"],
        "version": bundle["version"],
        "bundle_hash": stored_hash,
        "status": status,
        "checks": checks,
        "checked_at": checked_at,
        "note": "核验只读重算，不修改冻结内容；失败说明站包或审计记录与冻结基准不一致，需人工排查后再使用。",
    }
    if actor:
        db.append_audit(
            conn,
            actor=actor,
            action="bundle.integrity_verify",
            object_type="StationBundle",
            object_id=bundle_id,
            before=None,
            after={
                "status": status,
                "bundle_hash": stored_hash,
                "failed": [c["key"] for c in checks if not c["passed"]],
            },
        )
    return result
