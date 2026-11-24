"""SP 人格卡人工批准：披露阈值、禁用话术与试演结论由考官签字负责。"""
from __future__ import annotations

from typing import Any

from .. import db

DECISIONS = ("approved", "rejected")
SCOPES = ("L1", "L2")
SCOPE_LABELS = {"L1": "仅练习", "L2": "可进入模拟考"}


def record_persona_approval(
    conn,
    bundle: dict[str, Any],
    *,
    decision: str,
    scope: str,
    disclosure_approved: bool,
    forbidden_approved: bool,
    trial_approved: bool,
    note: str = "",
    actor: str = "考官",
) -> dict[str, Any]:
    if decision not in DECISIONS:
        raise ValueError("decision 必须是 approved 或 rejected")
    if scope not in SCOPES:
        raise ValueError("scope 必须是 L1 或 L2")
    if decision == "approved" and not (disclosure_approved and forbidden_approved and trial_approved):
        raise ValueError("批准人格卡必须同时确认披露阈值、禁用话术与真人试演")
    approval_id = db.new_id("spappr")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO sp_persona_approvals(approval_id, bundle_id, persona_version, decision, scope, "
        "disclosure_approved, forbidden_approved, trial_approved, note, actor, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            approval_id,
            bundle["bundle_id"],
            bundle["bundle_hash"],
            decision,
            scope,
            1 if disclosure_approved else 0,
            1 if forbidden_approved else 0,
            1 if trial_approved else 0,
            note.strip(),
            actor,
            timestamp,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="sp.persona_approval",
        object_type="PersonaCard",
        object_id=bundle["bundle_id"],
        before=None,
        after={
            "decision": decision,
            "scope": scope,
            "disclosure_approved": disclosure_approved,
            "forbidden_approved": forbidden_approved,
            "trial_approved": trial_approved,
            "note": note.strip(),
            "persona_version": bundle["bundle_hash"],
        },
    )
    return {
        "approval_id": approval_id,
        "bundle_id": bundle["bundle_id"],
        "persona_version": bundle["bundle_hash"],
        "decision": decision,
        "scope": scope,
        "disclosure_approved": disclosure_approved,
        "forbidden_approved": forbidden_approved,
        "trial_approved": trial_approved,
        "note": note.strip(),
        "actor": actor,
        "created_at": timestamp,
    }


def latest_persona_approval(conn, bundle_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM sp_persona_approvals WHERE bundle_id = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (bundle_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "approval_id": row["approval_id"],
        "bundle_id": row["bundle_id"],
        "persona_version": row["persona_version"],
        "decision": row["decision"],
        "scope": row["scope"],
        "disclosure_approved": bool(row["disclosure_approved"]),
        "forbidden_approved": bool(row["forbidden_approved"]),
        "trial_approved": bool(row["trial_approved"]),
        "note": row["note"],
        "actor": row["actor"],
        "created_at": row["created_at"],
    }


def persona_approval_status(conn, bundle: dict[str, Any]) -> dict[str, Any]:
    approval = latest_persona_approval(conn, bundle["bundle_id"])
    if approval is None:
        row = conn.execute(
            "SELECT a.* FROM sp_persona_approvals a "
            "JOIN bundles b ON b.bundle_id = a.bundle_id "
            "WHERE b.station_id = ? ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1",
            (bundle.get("station_id"),),
        ).fetchone()
        if row:
            approval = {
                "approval_id": row["approval_id"],
                "bundle_id": row["bundle_id"],
                "persona_version": row["persona_version"],
                "decision": row["decision"],
                "scope": row["scope"],
                "disclosure_approved": bool(row["disclosure_approved"]),
                "forbidden_approved": bool(row["forbidden_approved"]),
                "trial_approved": bool(row["trial_approved"]),
                "note": row["note"],
                "actor": row["actor"],
                "created_at": row["created_at"],
            }
    stale = bool(
        approval
        and (
            approval["bundle_id"] != bundle["bundle_id"]
            or approval["persona_version"] != bundle["bundle_hash"]
        )
    )
    approved = bool(
        approval
        and approval["decision"] == "approved"
        and not stale
        and approval["disclosure_approved"]
        and approval["forbidden_approved"]
        and approval["trial_approved"]
    )
    return {
        "approved": approved,
        "stale": stale,
        "scope": approval["scope"] if approval else None,
        "scope_label": SCOPE_LABELS.get(approval["scope"]) if approval else None,
        "approval": approval,
    }


def list_persona_approvals(conn, bundle_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    if bundle_id:
        rows = conn.execute(
            "SELECT * FROM sp_persona_approvals WHERE bundle_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (bundle_id, max(1, min(limit, 100))),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sp_persona_approvals ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    return [
        {
            "approval_id": row["approval_id"],
            "bundle_id": row["bundle_id"],
            "persona_version": row["persona_version"],
            "decision": row["decision"],
            "scope": row["scope"],
            "disclosure_approved": bool(row["disclosure_approved"]),
            "forbidden_approved": bool(row["forbidden_approved"]),
            "trial_approved": bool(row["trial_approved"]),
            "note": row["note"],
            "actor": row["actor"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
