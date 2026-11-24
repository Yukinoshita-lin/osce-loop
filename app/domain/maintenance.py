"""运维维护：过期会话、留存清理与审计锚点合并执行。"""
from __future__ import annotations

from typing import Any

from .. import db
from .auth import cleanup_expired_sessions
from .retention import purge_expired, refresh_retention


def run_maintenance(
    conn,
    *,
    actor: str = "系统",
    anchor_threshold: int = 20,
    anchor_path=None,
) -> dict[str, Any]:
    """执行可重复的日常维护；锚点仅在事件增量达到阈值时写入。"""
    sessions_cleaned = cleanup_expired_sessions(conn)
    retention_updated = refresh_retention(conn)
    purge = purge_expired(conn, actor=actor, dry_run=False)

    anchors = db.list_audit_anchors(conn, limit=1)
    last_count = int(anchors[0]["event_count"]) if anchors else 0
    total = conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
    events_since_anchor = max(0, total - last_count)
    anchor = None
    if events_since_anchor >= anchor_threshold:
        anchor = db.create_audit_anchor(conn, actor=actor, anchor_path=anchor_path)

    summary = {
        "sessions_cleaned": sessions_cleaned,
        "retention_updated": retention_updated,
        "purge_candidates": purge["summary"]["total"],
        "purged_attempts": purge["summary"].get("purged_attempts", 0),
        "purge_failed": purge["summary"].get("failed", 0),
        "events_since_anchor": events_since_anchor,
        "anchor_id": anchor["anchor_id"] if anchor else None,
        "anchor_event_count": anchor["event_count"] if anchor else None,
    }
    db.append_audit(
        conn,
        actor=actor,
        action="ops.maintenance",
        object_type="System",
        object_id="maintenance",
        before=None,
        after=summary,
    )
    return {"ran_at": db.now_iso(), **summary}
