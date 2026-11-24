"""站包冻结、哈希与版本治理。"""
from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
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


def _version_key(value: str) -> tuple[int, int]:
    try:
        parts = str(value).split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("版本号格式应为 主版本.次版本，例如 1.1") from exc


def _similarity_text(payload: dict[str, Any]) -> str:
    parts: list[str] = [str(payload.get("title") or "")]
    parts.extend(str(text) for text in payload.get("candidate_instructions") or [])
    for item in payload.get("rubric") or []:
        parts.append(str(item.get("title") or ""))
        anchor = item.get("anchor") or {}
        parts.extend(str(anchor.get(field) or "") for field in ("positive", "partial", "negative"))
    return re.sub(r"[\s，。；：、！？…—·,.!?;:（）()\[\]【】]", "", "".join(parts))


def find_similar_bundles(
    conn,
    payload: dict[str, Any],
    *,
    threshold: float = 0.85,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """与既往其他考站的冻结站包做文本相似度初筛，提示“换汤不换药”风险。"""
    text = _similarity_text(payload)
    if not text:
        return []
    station_id = str(payload.get("station_id") or "")
    matches = []
    for row in conn.execute("SELECT station_id, title, version, payload FROM bundles").fetchall():
        if row["station_id"] == station_id:
            continue
        other = _similarity_text(json.loads(row["payload"] or "{}"))
        if not other:
            continue
        ratio = SequenceMatcher(None, text, other).ratio()
        if ratio >= threshold:
            matches.append(
                {
                    "station_id": row["station_id"],
                    "title": row["title"],
                    "version": row["version"],
                    "similarity": round(ratio, 3),
                }
            )
    matches.sort(key=lambda entry: entry["similarity"], reverse=True)
    return matches[:limit]


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

    station_id = payload.get("station_id")
    new_version = _version_key(version)
    if supersedes:
        previous = conn.execute(
            "SELECT station_id, version FROM bundles WHERE bundle_id = ?", (supersedes,)
        ).fetchone()
        if not previous:
            raise ValueError("被替代的站包不存在")
        if previous["station_id"] != station_id:
            raise ValueError("只能替代同一考站的站包")
        if new_version <= _version_key(previous["version"]):
            raise ValueError(f"新版本号必须高于被替代版本 {previous['version']}")
    duplicate = conn.execute(
        "SELECT bundle_id FROM bundles WHERE station_id = ? AND version = ? AND bundle_hash != ?",
        (station_id, version, bundle_hash),
    ).fetchone()
    if duplicate:
        raise ValueError(f"考站 {station_id} 的版本 {version} 已存在，请提升版本号")

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


SCALAR_FIELDS = (
    "station_id",
    "title",
    "version",
    "duration_min",
    "pass_line",
    "risk_level_default",
)


def diff_payloads(base: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """冻结前差异审阅：按评分项与指南条款结构化对比两份 payload。"""
    station_changes = [
        {"field": field, "before": base.get(field), "after": proposal.get(field)}
        for field in SCALAR_FIELDS
        if base.get(field) != proposal.get(field)
    ]

    base_items = {item.get("item_id"): item for item in base.get("rubric") or []}
    new_items = {item.get("item_id"): item for item in proposal.get("rubric") or []}
    added = sorted(new_items.keys() - base_items.keys(), key=str)
    removed = sorted(base_items.keys() - new_items.keys(), key=str)
    changed: list[dict[str, Any]] = []
    for item_id in sorted(base_items.keys() & new_items.keys(), key=str):
        before = base_items[item_id]
        after = new_items[item_id]
        if canonical_json(before) == canonical_json(after):
            continue
        fields = sorted(
            key
            for key in set(before) | set(after)
            if canonical_json(before.get(key)) != canonical_json(after.get(key))
        )
        changed.append({"item_id": item_id, "fields": fields})

    base_kb = {entry.get("ref"): entry for entry in base.get("guideline_kb") or [] if entry.get("ref")}
    new_kb = {entry.get("ref"): entry for entry in proposal.get("guideline_kb") or [] if entry.get("ref")}
    guideline_added = sorted(new_kb.keys() - base_kb.keys(), key=str)
    guideline_removed = sorted(base_kb.keys() - new_kb.keys(), key=str)
    guideline_changed = sorted(
        (
            ref
            for ref in base_kb.keys() & new_kb.keys()
            if canonical_json(base_kb[ref]) != canonical_json(new_kb[ref])
        ),
        key=str,
    )

    other_changes = [
        field
        for field in ("candidate_instructions", "instruction_item_refs", "extra_slots", "sp_profile")
        if canonical_json(base.get(field)) != canonical_json(proposal.get(field))
    ]
    summary = {
        "station_changes": len(station_changes),
        "added_items": len(added),
        "removed_items": len(removed),
        "changed_items": len(changed),
        "guideline_changes": len(guideline_added) + len(guideline_removed) + len(guideline_changed),
        "other_changes": len(other_changes),
    }
    return {
        "has_changes": any(summary.values()),
        "summary": summary,
        "station": station_changes,
        "rubric": {"added": added, "removed": removed, "changed": changed},
        "guidelines": {
            "added": guideline_added,
            "removed": guideline_removed,
            "changed": guideline_changed,
        },
        "other_changes": other_changes,
    }


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
    rows = conn.execute("SELECT * FROM bundles ORDER BY frozen_at DESC, rowid DESC").fetchall()
    return [get_bundle(conn, row["bundle_id"]) for row in rows]


def list_station_bundles(conn) -> list[dict[str, Any]]:
    """每个考站只保留最新冻结版本，供录入与 SP 演练选择。"""
    latest: dict[str, dict[str, Any]] = {}
    for bundle in list_bundles(conn):
        if bundle["station_id"] in latest:
            continue
        latest[bundle["station_id"]] = {
            "bundle_id": bundle["bundle_id"],
            "station_id": bundle["station_id"],
            "title": bundle["title"],
            "version": bundle["version"],
            "bundle_hash": bundle["bundle_hash"],
            "frozen_at": bundle["frozen_at"],
        }
    return list(latest.values())


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
