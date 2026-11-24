"""模拟患者知情同意：记录、查询与有效性判定。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .. import db

CONSENT_TYPES = {
    "informed_use": "模拟患者知情同意",
    "minor_guardian": "未成年人监护人同意",
    "data_use": "音视频数据使用同意",
}
SP_ROLES = {
    "adult_sp": "成年模拟患者",
    "minor_sp": "未成年模拟患者",
    "guardian": "监护人",
}
CONSENT_VALID_DAYS = 365


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def record_consent(
    conn,
    *,
    sp_name: str,
    sp_role: str,
    consent_type: str,
    version: str,
    bundle_id: str | None = None,
    notes: str | None = None,
    actor: str = "考务",
) -> dict[str, Any]:
    if not str(sp_name or "").strip():
        raise ValueError("SP 姓名不能为空")
    if sp_role not in SP_ROLES:
        raise ValueError(f"sp_role 必须是 {', '.join(SP_ROLES)}")
    if consent_type not in CONSENT_TYPES:
        raise ValueError(f"consent_type 必须是 {', '.join(CONSENT_TYPES)}")
    if not str(version or "").strip():
        raise ValueError("同意书版本不能为空")
    if bundle_id and not conn.execute(
        "SELECT 1 FROM bundles WHERE bundle_id = ?", (bundle_id,)
    ).fetchone():
        raise ValueError("bundle_id 指向的站包不存在")
    consent_id = db.new_id("csp")
    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO sp_consents(consent_id, sp_name, sp_role, bundle_id, consent_type, version, "
        "signed_at, notes, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            consent_id,
            str(sp_name).strip(),
            sp_role,
            bundle_id,
            consent_type,
            str(version).strip(),
            timestamp,
            (notes or "").strip() or None,
            actor,
            timestamp,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="sp.consent_recorded",
        object_type="SpConsent",
        object_id=consent_id,
        after={
            "sp_name": sp_name,
            "sp_role": sp_role,
            "consent_type": consent_type,
            "version": version,
            "bundle_id": bundle_id,
        },
    )
    row = conn.execute("SELECT * FROM sp_consents WHERE consent_id = ?", (consent_id,)).fetchone()
    return dict(row)


def list_consents(conn, bundle_id: str | None = None) -> list[dict[str, Any]]:
    if bundle_id:
        rows = conn.execute(
            "SELECT * FROM sp_consents WHERE bundle_id = ? OR bundle_id IS NULL "
            "ORDER BY signed_at DESC, rowid DESC",
            (bundle_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sp_consents ORDER BY signed_at DESC, rowid DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def consent_status(conn, bundle_id: str | None) -> dict[str, Any]:
    rows = list_consents(conn, bundle_id) if bundle_id else list_consents(conn)
    required = ["informed_use"]
    if any(row["sp_role"] == "minor_sp" for row in rows):
        required.append("minor_guardian")
    now = datetime.now().astimezone()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        signed = _parse(row.get("signed_at"))
        row["expires_at"] = (
            (signed + timedelta(days=CONSENT_VALID_DAYS)).isoformat(timespec="seconds")
            if signed
            else None
        )
        row["valid"] = bool(signed and now <= signed + timedelta(days=CONSENT_VALID_DAYS))
        existing = latest.get(row["consent_type"])
        if existing is None or (row["valid"] and not existing["valid"]):
            latest[row["consent_type"]] = row
    missing = [key for key in required if key not in latest]
    expired = [key for key in required if key in latest and not latest[key]["valid"]]
    return {
        "bundle_id": bundle_id,
        "required": required,
        "valid": not missing and not expired,
        "missing": missing,
        "expired": expired,
        "consents": rows,
        "type_labels": CONSENT_TYPES,
        "role_labels": SP_ROLES,
        "note": "知情同意有效期 365 天；未成年 SP 还需监护人同意。",
    }
