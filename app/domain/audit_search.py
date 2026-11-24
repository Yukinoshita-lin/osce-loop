"""审计检索：按动作、操作者、对象与时间范围查询并分页。"""
from __future__ import annotations

from typing import Any

from .. import db

MAX_LIMIT = 200


def search_audit_events(
    conn,
    *,
    action: str = "",
    actor: str = "",
    object_id: str = "",
    since: str = "",
    until: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    limit = max(1, min(MAX_LIMIT, int(limit if limit is not None else 50)))
    offset = max(0, int(offset or 0))
    clauses: list[str] = []
    params: list[Any] = []
    if action:
        clauses.append("action LIKE ?")
        params.append(f"{action}%")
    if actor:
        clauses.append("actor LIKE ?")
        params.append(f"%{actor}%")
    if object_id:
        clauses.append("object_id LIKE ?")
        params.append(f"%{object_id}%")
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at <= ?")
        params.append(until)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM audit_events{where}", tuple(params)
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT * FROM audit_events{where} ORDER BY rowid DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "events": db.rows_to_dicts(rows),
        "total": total,
        "offset": offset,
        "limit": limit,
        "filters": {
            "action": action,
            "actor": actor,
            "object_id": object_id,
            "since": since,
            "until": until,
        },
    }
