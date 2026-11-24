"""S1 年度能力蓝图：年度版本、目标占比与人工批准生效。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .. import db

DEFAULT_BLUEPRINT = [
    {"domain": "communication", "label": "沟通与人文", "target_share": 0.20},
    {"domain": "history_taking", "label": "病史采集", "target_share": 0.25},
    {"domain": "assessment", "label": "评估与判断", "target_share": 0.20},
    {"domain": "clinical_reasoning", "label": "临床推理", "target_share": 0.15},
    {"domain": "procedures", "label": "操作技能", "target_share": 0.10},
    {"domain": "patient_safety", "label": "患者安全", "target_share": 0.10},
]
DOMAIN_LABELS = {entry["domain"]: entry["label"] for entry in DEFAULT_BLUEPRINT}

DOMAIN_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,32}$")
SHARE_TOLERANCE = 0.02
MIN_APPROVAL_NOTE = 4


def _clean_domains(domains: Any) -> list[dict[str, Any]]:
    if not isinstance(domains, list) or not domains:
        raise ValueError("蓝图至少需要一个能力域")
    if len(domains) > 12:
        raise ValueError("能力域数量不能超过 12 个")
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0.0
    for entry in domains:
        if not isinstance(entry, dict):
            raise ValueError("能力域条目必须是对象")
        domain = str(entry.get("domain") or "").strip()
        label = str(entry.get("label") or "").strip()
        if not DOMAIN_PATTERN.fullmatch(domain):
            raise ValueError(f"能力域标识不合法：{domain or '空'}")
        if domain in seen:
            raise ValueError(f"能力域重复：{domain}")
        if not label or len(label) > 40:
            raise ValueError(f"能力域 {domain} 的名称不能为空且不超过 40 字")
        try:
            share = float(entry.get("target_share"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"能力域 {domain} 的目标占比必须是数字") from exc
        if share < 0 or share > 1:
            raise ValueError(f"能力域 {domain} 的目标占比应在 0–1 之间")
        seen.add(domain)
        total += share
        cleaned.append({"domain": domain, "label": label, "target_share": round(share, 3)})
    if abs(total - 1.0) > SHARE_TOLERANCE:
        raise ValueError(f"能力域目标占比合计应接近 1.0，当前为 {total:.3f}")
    return cleaned


def _row_to_dict(row) -> dict[str, Any]:
    data = dict(row)
    data["domains"] = json.loads(data["domains"])
    return data


def draft_blueprint(
    conn,
    *,
    year: int,
    domains: Any,
    note: str | None,
    actor: str,
) -> dict[str, Any]:
    if year < 2020 or year > 2100:
        raise ValueError("蓝图年度应在 2020–2100 之间")
    cleaned = _clean_domains(domains)
    row = conn.execute(
        "SELECT MAX(version) AS version FROM blueprints WHERE year = ?", (year,)
    ).fetchone()
    version = int(row["version"] or 0) + 1
    blueprint_id = db.new_id("bp")
    timestamp = db.now_iso()
    note = (note or "").strip() or None
    if note and len(note) > 300:
        raise ValueError("蓝图说明不能超过 300 字")
    conn.execute(
        "INSERT INTO blueprints(blueprint_id, year, version, domains, note, status, "
        "created_by, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            blueprint_id,
            year,
            version,
            json.dumps(cleaned, ensure_ascii=False),
            note,
            "draft",
            actor,
            timestamp,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="blueprint.draft",
        object_type="Blueprint",
        object_id=blueprint_id,
        before=None,
        after={
            "year": year,
            "version": version,
            "domains": [entry["domain"] for entry in cleaned],
            "targets": {entry["domain"]: entry["target_share"] for entry in cleaned},
            "note": note,
        },
    )
    return _row_to_dict(
        conn.execute("SELECT * FROM blueprints WHERE blueprint_id = ?", (blueprint_id,)).fetchone()
    )


def approve_blueprint(
    conn,
    blueprint_id: str,
    *,
    note: str,
    actor: str,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM blueprints WHERE blueprint_id = ?", (blueprint_id,)).fetchone()
    if not row:
        raise ValueError("蓝图不存在")
    if row["status"] != "draft":
        raise ValueError("只有草案可以批准生效")
    note = (note or "").strip()
    if len(note) < MIN_APPROVAL_NOTE:
        raise ValueError(f"批准蓝图需填写不少于 {MIN_APPROVAL_NOTE} 字的理由")
    if len(note) > 300:
        raise ValueError("批准理由不能超过 300 字")
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE blueprints SET status = 'superseded' WHERE year = ? AND status = 'approved'",
        (row["year"],),
    )
    conn.execute(
        "UPDATE blueprints SET status = 'approved', approved_by = ?, approved_at = ?, "
        "approval_note = ? WHERE blueprint_id = ?",
        (actor, timestamp, note, blueprint_id),
    )
    conn.commit()
    approved = conn.execute("SELECT * FROM blueprints WHERE blueprint_id = ?", (blueprint_id,)).fetchone()
    db.append_audit(
        conn,
        actor=actor,
        action="blueprint.approve",
        object_type="Blueprint",
        object_id=blueprint_id,
        before={"status": "draft"},
        after={
            "status": "approved",
            "year": approved["year"],
            "version": approved["version"],
            "note": note,
        },
    )
    return _row_to_dict(approved)


def latest_approved(conn, year: int | None = None) -> dict[str, Any] | None:
    if year is None:
        row = conn.execute(
            "SELECT * FROM blueprints WHERE status = 'approved' "
            "ORDER BY year DESC, approved_at DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM blueprints WHERE status = 'approved' AND year = ? "
            "ORDER BY approved_at DESC LIMIT 1",
            (year,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_blueprints(conn, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM blueprints ORDER BY year DESC, version DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def blueprint_state(conn) -> dict[str, Any]:
    """返回当前生效蓝图、历史与默认目标；未批准时明确标注来源为默认。"""
    current_year = datetime.now().year
    current = latest_approved(conn, current_year)
    stale = False
    if current is None:
        current = latest_approved(conn)
        stale = current is not None and current["year"] != current_year
    return {
        "current_year": current_year,
        "current": current,
        "source": "approved" if current else "default",
        "stale": stale,
        "history": list_blueprints(conn),
        "defaults": DEFAULT_BLUEPRINT,
        "note": (
            "年度蓝图由考务起草并人工批准后生效；未批准时覆盖度暂按默认六域目标计算。"
        ),
    }
