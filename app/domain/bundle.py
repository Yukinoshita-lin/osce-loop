"""站包冻结、哈希与版本治理。"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import db
from ..config import MODEL_VERSION, PROMPT_VERSION
from .rubric import AUTHORITY_ORDER, cap_authority, derive_assets, validate_bundle


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_bundle_hash(payload: dict[str, Any], derived: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json(payload).encode("utf-8"))
    digest.update(b"::derived::")
    digest.update(canonical_json(derived).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def freeze_bundle(
    conn,
    payload: dict[str, Any],
    *,
    actor: str = "命题专家",
    version: str = "1.0",
    supersedes: str | None = None,
    reason: str = "首次冻结",
) -> dict[str, Any]:
    """冻结即不可变：写入新行，绝不原地修改。"""
    validation = validate_bundle(payload)
    if not validation["can_freeze"]:
        raise ValueError("站包校验未通过，禁止冻结：" + "；".join(validation["blocking"][:5]))

    derived = derive_assets(payload)
    bundle_hash = compute_bundle_hash(payload, derived)
    bundle_id = f"bnd_{bundle_hash.split(':')[1][:12]}"
    existing = conn.execute("SELECT bundle_id FROM bundles WHERE bundle_hash = ?", (bundle_hash,)).fetchone()
    if existing:
        return get_bundle(conn, existing["bundle_id"])

    timestamp = db.now_iso()
    conn.execute(
        "INSERT INTO bundles(bundle_id, station_id, title, version, bundle_hash, status, payload, "
        "derived, validation, created_at, frozen_at, supersedes, reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            bundle_id,
            payload.get("station_id"),
            payload.get("title"),
            version,
            bundle_hash,
            "frozen",
            json.dumps(payload, ensure_ascii=False),
            json.dumps(derived, ensure_ascii=False),
            json.dumps(validation, ensure_ascii=False),
            timestamp,
            timestamp,
            supersedes,
            reason,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="bundle.freeze",
        object_type="StationBundle",
        object_id=bundle_id,
        before=None,
        after={
            "bundle_hash": bundle_hash,
            "version": version,
            "supersedes": supersedes,
            "reason": reason,
            "derived_counts": derived["counts"],
        },
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
    )
    return get_bundle(conn, bundle_id)


def get_bundle(conn, bundle_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM bundles WHERE bundle_id = ?", (bundle_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    data["derived"] = json.loads(data["derived"])
    data["validation"] = json.loads(data["validation"])
    return data


def list_bundles(conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM bundles ORDER BY frozen_at DESC").fetchall()
    return [get_bundle(conn, row["bundle_id"]) for row in rows]


def get_override(conn, bundle_id: str, item_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM authority_overrides WHERE bundle_id = ? AND item_id = ?",
        (bundle_id, item_id),
    ).fetchone()
    return dict(row) if row else None


def effective_authority(
    conn,
    bundle: dict[str, Any],
    item: dict[str, Any],
    risk_level: str,
    release_stage: str,
) -> str:
    configured = item.get("ai_authority", "none")
    override = get_override(conn, bundle["bundle_id"], item.get("item_id"))
    if override:
        configured = override["authority"]
    capped = cap_authority({**item, "ai_authority": configured}, risk_level, release_stage)
    # 兜底：任何权限不得高于原配置与覆盖值
    if AUTHORITY_ORDER[capped] > AUTHORITY_ORDER[configured]:
        return configured
    return capped


def apply_authority_reset(
    conn,
    *,
    reason: str,
    actor: str,
    model_version: str,
    prompt_version: str,
    floor: str = "extract",
) -> list[dict[str, Any]]:
    """模型/提示词版本变化后，把 suggest/decide 自动降为 extract，禁止静默升级。"""
    bundles = list_bundles(conn)
    changed: list[dict[str, Any]] = []
    timestamp = db.now_iso()
    for bundle in bundles:
        for item in bundle["payload"].get("rubric", []):
            current = effective_authority(
                conn, bundle, item, "L1", "G1_practice"
            )
            configured = item.get("ai_authority", "none")
            target = floor if AUTHORITY_ORDER[configured] > AUTHORITY_ORDER[floor] else configured
            if AUTHORITY_ORDER[current] > AUTHORITY_ORDER[target]:
                conn.execute(
                    "INSERT INTO authority_overrides(bundle_id, item_id, authority, reason, updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(bundle_id, item_id) DO UPDATE SET "
                    "authority = excluded.authority, reason = excluded.reason, updated_at = excluded.updated_at",
                    (bundle["bundle_id"], item["item_id"], target, reason, timestamp),
                )
                changed.append(
                    {
                        "bundle_id": bundle["bundle_id"],
                        "item_id": item["item_id"],
                        "from": current,
                        "to": target,
                    }
                )
    if changed:
        conn.commit()
        db.append_audit(
            conn,
            actor=actor,
            action="authority.auto_downgrade",
            object_type="AiAuthority",
            object_id="all_items",
            before=None,
            after={"reason": reason, "changes": changed},
            model_version=model_version,
            prompt_version=prompt_version,
        )
    return changed


def list_overrides(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM authority_overrides ORDER BY updated_at DESC"
    ).fetchall()
    return db.rows_to_dicts(rows)
