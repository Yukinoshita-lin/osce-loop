"""媒体分级留存：按风险等级计算保留期，提供到期预览与可审计清理。

成绩、评审与审计记录永久保留；音视频按风险等级分级留存，到期后仅清理媒体文件，
证据时间戳、转写与哈希链保持不变，删除动作本身写入审计链。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import db
from ..config import MEDIA_DIR

RETENTION_POLICY: list[dict[str, Any]] = [
    {"class": "L0", "label": "L0 内部教学", "days": 30, "note": "内部草稿与教学演示，30 天后清理媒体"},
    {"class": "L1", "label": "L1 形成性练习", "days": 90, "note": "练习场次媒体留存 90 天，成绩与证据哈希长期保留"},
    {"class": "L2", "label": "L2 模拟考", "days": 365, "note": "模拟考媒体留存 1 年"},
    {"class": "L3", "label": "L3 高利害", "days": 1095, "note": "留存至申诉期结束后 6 个月，按 3 年上限"},
]
_POLICY = {item["class"]: item for item in RETENTION_POLICY}


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 10:
        text += "T23:59:59"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def retention_plan(risk_level: str, created_at: str) -> dict[str, Any]:
    policy = _POLICY.get(risk_level) or _POLICY["L2"]
    created = _parse(created_at) or _now()
    retain_until = created + timedelta(days=int(policy["days"]))
    return {
        "retention_class": policy["class"],
        "retention_label": policy["label"],
        "retention_days": policy["days"],
        "retain_until": retain_until.isoformat(timespec="seconds"),
    }


def _media_path(media_url: str | None, media_dir: Path | str | None) -> Path | None:
    if not media_url or not media_url.startswith("/media/"):
        return None
    base = Path(media_dir or MEDIA_DIR).resolve()
    target = (base / media_url[len("/media/"):]).resolve()
    if target != base and base not in target.parents:
        return None
    return target


def _media_size(media_url: str | None, media_dir: Path | str | None) -> int:
    path = _media_path(media_url, media_dir)
    if not path or not path.is_file():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def refresh_retention(conn, *, attempt_id: str | None = None, media_dir: Path | str | None = None) -> int:
    """回填或重算场次留存元数据；风险等级上调后保留期随之延长。"""
    sql = "SELECT attempt_id, risk_level, created_at, media_url, media_size_bytes, media_purged_at FROM attempts"
    params: tuple[Any, ...] = ()
    if attempt_id:
        sql += " WHERE attempt_id = ?"
        params = (attempt_id,)
    rows = db.rows_to_dicts(conn.execute(sql, params).fetchall())
    updated = 0
    for row in rows:
        plan = retention_plan(row["risk_level"], row["created_at"])
        size = row["media_size_bytes"] or 0
        if not size and not row["media_purged_at"]:
            size = _media_size(row["media_url"], media_dir)
        conn.execute(
            "UPDATE attempts SET retention_class = ?, retain_until = ?, media_size_bytes = ? WHERE attempt_id = ?",
            (plan["retention_class"], plan["retain_until"], size, row["attempt_id"]),
        )
        updated += 1
    if updated:
        conn.commit()
    return updated


def _collect(conn, *, as_of: datetime, media_dir: Path | str | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT attempt_id, candidate_code, candidate_name, risk_level, media_url, media_filename, "
            "media_size_bytes, retention_class, retain_until, media_purged_at, appeal, created_at "
            "FROM attempts ORDER BY retain_until"
        ).fetchall()
    )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        retain_until = _parse(row["retain_until"])
        if (
            not row["media_url"]
            or row["media_purged_at"]
            or row["appeal"]
            or retain_until is None
            or retain_until > as_of
        ):
            continue
        candidates.append(row)

    candidate_ids = {row["attempt_id"] for row in candidates}
    items: list[dict[str, Any]] = []
    for row in candidates:
        path = _media_path(row["media_url"], media_dir)
        size = row["media_size_bytes"] or _media_size(row["media_url"], media_dir)
        active_refs = conn.execute(
            "SELECT COUNT(*) AS n FROM attempts WHERE media_url = ? AND media_purged_at IS NULL "
            "AND attempt_id NOT IN ({})".format(",".join("?" * len(candidate_ids))),
            (row["media_url"], *candidate_ids),
        ).fetchone()["n"]
        if active_refs:
            file_action = "shared_retain"
        elif path is None:
            file_action = "unmanaged"
        elif path.is_file():
            file_action = "delete"
        else:
            file_action = "missing"
        items.append(
            {
                "attempt_id": row["attempt_id"],
                "candidate_code": row["candidate_code"],
                "candidate_name": row["candidate_name"],
                "risk_level": row["risk_level"],
                "retention_class": row["retention_class"] or row["risk_level"],
                "retain_until": row["retain_until"],
                "media_url": row["media_url"],
                "media_filename": row["media_filename"],
                "size_bytes": size,
                "file_action": file_action,
                "file_action_label": {
                    "delete": "删除文件",
                    "shared_retain": "共享引用，仅解除本场次访问",
                    "missing": "文件已缺失",
                    "unmanaged": "非托管路径，仅解除访问",
                }[file_action],
            }
        )
    summary = {
        "total": len(items),
        "freed_bytes": sum(i["size_bytes"] for i in items if i["file_action"] == "delete"),
        "purged_attempts": len(items),
        "media_urls": len({i["media_url"] for i in items}),
    }
    return items, summary


def retention_overview(conn, *, media_dir: Path | str | None = None, as_of: str | None = None) -> dict[str, Any]:
    refresh_retention(conn, media_dir=media_dir)
    moment = _parse(as_of) if as_of else _now()
    if moment is None:
        raise ValueError("无效的时间点，请使用 ISO 日期或时间")
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT attempt_id, candidate_code, candidate_name, risk_level, media_url, media_filename, "
            "media_size_bytes, retention_class, retain_until, media_purged_at, appeal FROM attempts"
        ).fetchall()
    )
    by_class: dict[str, dict[str, Any]] = {}
    for policy in RETENTION_POLICY:
        by_class[policy["class"]] = {
            "class": policy["class"],
            "label": policy["label"],
            "days": policy["days"],
            "note": policy["note"],
            "count": 0,
            "expired": 0,
            "purged": 0,
            "bytes": 0,
        }
    media_total = 0
    expired = 0
    purged = 0
    held = 0
    freed = 0
    due_soon = 0
    soon_line = moment + timedelta(days=30)
    media_seen: set[str] = set()
    size_by_url: dict[str, int] = {}
    active_urls: set[str] = set()
    purged_urls: set[str] = set()
    for row in rows:
        bucket = by_class.setdefault(
            row["retention_class"] or row["risk_level"],
            {
                "class": row["retention_class"] or row["risk_level"],
                "label": row["retention_class"] or row["risk_level"],
                "days": None,
                "note": "",
                "count": 0,
                "expired": 0,
                "purged": 0,
                "bytes": 0,
            },
        )
        size = row["media_size_bytes"] or 0
        bucket["count"] += 1
        media_url = row["media_url"]
        if media_url:
            size_by_url[media_url] = max(size_by_url.get(media_url, 0), size)
        if row["media_purged_at"]:
            purged += 1
            bucket["purged"] += 1
            if media_url:
                purged_urls.add(media_url)
            continue
        if media_url:
            active_urls.add(media_url)
            if media_url not in media_seen:
                media_seen.add(media_url)
                media_total += size
        bucket["bytes"] += size
        retain_until = _parse(row["retain_until"])
        if retain_until is None:
            continue
        if retain_until <= moment:
            if row["appeal"]:
                held += 1
            else:
                expired += 1
                bucket["expired"] += 1
        elif retain_until <= soon_line:
            due_soon += 1
    for media_url in purged_urls - active_urls:
        path = _media_path(media_url, media_dir)
        if path is None or not path.exists():
            freed += size_by_url.get(media_url, 0)
    items, _ = _collect(conn, as_of=moment, media_dir=media_dir)
    return {
        "as_of": moment.isoformat(timespec="seconds"),
        "policy": RETENTION_POLICY,
        "by_class": [by_class[p["class"]] for p in RETENTION_POLICY],
        "counts": {
            "attempts": len(rows),
            "eligible": len(items),
            "expired": expired,
            "held": held,
            "purged": purged,
            "due_soon": due_soon,
        },
        "bytes": {
            "media_total": media_total,
            "expired": sum(i["size_bytes"] for i in items if i["file_action"] == "delete"),
            "freed": freed,
        },
        "items": items,
        "rule": "成绩与评审记录永久保留；媒体按风险等级分级留存；删除动作写入审计链。",
    }


def purge_expired(
    conn,
    *,
    actor: str,
    dry_run: bool = True,
    as_of: str | None = None,
    media_dir: Path | str | None = None,
) -> dict[str, Any]:
    refresh_retention(conn, media_dir=media_dir)
    moment = _parse(as_of) if as_of else _now()
    if moment is None:
        raise ValueError("无效的时间点，请使用 ISO 日期或时间")
    items, summary = _collect(conn, as_of=moment, media_dir=media_dir)
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "as_of": moment.isoformat(timespec="seconds"),
        "actor": actor,
        "summary": summary,
        "items": items,
        "deleted_files": 0,
        "freed_bytes": 0,
    }
    if dry_run or not items:
        return result

    timestamp = db.now_iso()
    deleted_files = 0
    freed_bytes = 0
    purged_attempts = 0
    failed = 0
    for item in items:
        if item["file_action"] == "delete":
            path = _media_path(item["media_url"], media_dir)
            if path and path.is_file():
                try:
                    path.unlink()
                    deleted_files += 1
                    freed_bytes += item["size_bytes"]
                except OSError as exc:  # 文件被占用或权限不足：不标记已清理，等待下次重试
                    item["file_action"] = "delete_failed"
                    item["file_action_label"] = f"删除失败：{exc}"
                    failed += 1
                    db.append_audit(
                        conn,
                        actor=actor,
                        action="media.purge_failed",
                        object_type="StationAttempt",
                        object_id=item["attempt_id"],
                        before={"media_url": item["media_url"], "retain_until": item["retain_until"]},
                        after={"purged_at": None, "error": str(exc)},
                    )
                    continue
        conn.execute(
            "UPDATE attempts SET media_purged_at = ?, media_purged_by = ? WHERE attempt_id = ?",
            (timestamp, actor, item["attempt_id"]),
        )
        purged_attempts += 1
        db.append_audit(
            conn,
            actor=actor,
            action="media.purge",
            object_type="StationAttempt",
            object_id=item["attempt_id"],
            before={
                "media_url": item["media_url"],
                "retention_class": item["retention_class"],
                "retain_until": item["retain_until"],
                "size_bytes": item["size_bytes"],
            },
            after={
                "purged_at": timestamp,
                "file_action": item["file_action"],
                "file_deleted": item["file_action"] == "delete",
            },
        )
    summary["purged_attempts"] = purged_attempts
    summary["failed"] = failed
    db.append_audit(
        conn,
        actor=actor,
        action="retention.purge",
        object_type="RetentionPolicy",
        object_id=moment.date().isoformat(),
        before={"candidates": summary["total"], "media_urls": summary["media_urls"]},
        after={
            "purged_attempts": purged_attempts,
            "failed": failed,
            "deleted_files": deleted_files,
            "freed_bytes": freed_bytes,
        },
    )
    conn.commit()
    result["deleted_files"] = deleted_files
    result["freed_bytes"] = freed_bytes
    return result
