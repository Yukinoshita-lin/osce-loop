"""运行健康检查：数据库、审计链与锚点、媒体目录与 AI 提供方。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .. import db
from ..ai.factory import scoring_provider_info
from ..config import MEDIA_DIR
from .governance import emergency_stop_active

PENDING_STATUSES = ("pending_review", "manual_pending")


def _media_check(path: Path) -> dict[str, Any]:
    exists = path.exists()
    writable = exists and os.access(path, os.W_OK)
    return {
        "ok": writable,
        "path": str(path),
        "detail": "可写" if writable else ("目录不存在" if not exists else "目录不可写"),
    }


def system_health(
    conn,
    *,
    media_dir: Path | str | None = None,
    anchor_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """汇总运行依赖状态；任一硬依赖失败即判定为 degraded。"""
    db_error = ""
    try:
        conn.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception as exc:  # 健康检查本身必须兜住数据库异常
        db_ok = False
        db_error = str(exc)

    chain = db.verify_audit_chain(conn) if db_ok else {"ok": False, "message": "数据库不可用"}
    anchors = (
        db.verify_audit_anchors(conn, anchor_path=anchor_path)
        if db_ok
        else {"ok": False, "message": "数据库不可用"}
    )
    media = _media_check(Path(media_dir or MEDIA_DIR))
    ai = scoring_provider_info(env)

    checks = [
        {
            "key": "database",
            "label": "数据库",
            "ok": db_ok,
            "detail": "可读写" if db_ok else db_error,
        },
        {
            "key": "audit_chain",
            "label": "审计哈希链",
            "ok": bool(chain.get("ok")),
            "detail": chain.get("message", ""),
        },
        {
            "key": "audit_anchors",
            "label": "外部审计锚点",
            "ok": bool(anchors.get("ok")),
            "detail": anchors.get("message", ""),
        },
        {
            "key": "media_dir",
            "label": "媒体目录",
            "ok": media["ok"],
            "detail": media["detail"],
        },
    ]
    if db_ok:
        counts = {
            "attempts": conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"],
            "score_records": conn.execute("SELECT COUNT(*) AS n FROM score_records").fetchone()["n"],
            "pending_reviews": conn.execute(
                "SELECT COUNT(*) AS n FROM score_records WHERE status IN (?,?)", PENDING_STATUSES
            ).fetchone()["n"],
            "audit_events": conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"],
        }
        release_stage = db.get_state(conn, "release_stage", "G0_offline")
        emergency_stop = emergency_stop_active(conn)
    else:
        counts = {"attempts": 0, "score_records": 0, "pending_reviews": 0, "audit_events": 0}
        release_stage = "unknown"
        emergency_stop = False
    return {
        "status": "ok" if all(check["ok"] for check in checks) else "degraded",
        "checks": checks,
        "counts": counts,
        "media_path": media["path"],
        "ai": ai,
        "release_stage": release_stage,
        "emergency_stop": emergency_stop,
        "generated_at": db.now_iso(),
    }
