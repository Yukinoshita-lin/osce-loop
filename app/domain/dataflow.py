"""数据流向审计：模型与 ASR 端点是否在内网，外部端点必须显式放行。

设计约束：真实考生数据不得出院/校内网。系统无法替代网络管控，但可以在每次
质量报告生成时核对端点主机，把“配置了公网地址却未声明放行”暴露为治理告警。
"""
from __future__ import annotations

import ipaddress
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from .. import db
from ..config import (
    AI_ALLOW_EXTERNAL,
    AI_BASE_URL,
    AI_PROVIDER,
    ASR_ALLOW_EXTERNAL,
    ASR_BASE_URL,
    ASR_PROVIDER,
    TRUSTED_AI_HOSTS,
)

PRIVATE_SUFFIXES = (".local", ".internal", ".lan", ".intranet")
DISABLED_PROVIDERS = {"", "off", "none", "mock"}
REVIEW_VALID_DAYS = 365
REVIEW_DUE_SOON_DAYS = 30


def host_kind(host: str | None) -> str:
    """把主机归类为 loopback / private / trusted / public / unconfigured。"""
    if not host:
        return "unconfigured"
    normalized = str(host).strip().strip("[]").lower()
    if normalized in TRUSTED_AI_HOSTS:
        return "trusted"
    if normalized in ("localhost", "localhost.localdomain"):
        return "loopback"
    if normalized.endswith(PRIVATE_SUFFIXES):
        return "private"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "public"
    if address.is_loopback:
        return "loopback"
    if address.is_private or address.is_link_local or address.is_reserved:
        return "private"
    return "public"


def _flow(kind: str, label: str, provider: str, base_url: str, allow_external: bool) -> dict[str, Any] | None:
    if str(provider or "").strip().lower() in DISABLED_PROVIDERS or not base_url:
        return None
    host = urlparse(base_url).hostname
    scope = host_kind(host)
    external = scope == "public"
    return {
        "kind": kind,
        "label": label,
        "provider": provider,
        "host": host or "—",
        "scope": scope,
        "external": external,
        "allowed": (not external) or bool(allow_external),
    }


def data_flow_status() -> dict[str, Any]:
    flows = [
        flow
        for flow in (
            _flow("scoring", "评分模型", AI_PROVIDER, AI_BASE_URL, AI_ALLOW_EXTERNAL),
            _flow("asr", "语音转写", ASR_PROVIDER, ASR_BASE_URL, ASR_ALLOW_EXTERNAL),
        )
        if flow
    ]
    external = [flow for flow in flows if flow["external"]]
    return {
        "flows": flows,
        "external": external,
        "compliant": all(flow["allowed"] for flow in flows),
        "trusted_hosts": sorted(TRUSTED_AI_HOSTS),
        "note": (
            "评分与 ASR 默认应使用本机或内网端点；公网端点必须在配置中显式放行，"
            "并确认数据出境/出校合规审查。"
        ),
    }


def record_data_flow_review(
    conn,
    *,
    actor: str,
    note: str = "",
    valid_days: int = REVIEW_VALID_DAYS,
) -> dict[str, Any]:
    """记录年度数据流向复核：固化当时的端点快照与合规结论，并写入审计链。"""
    status = data_flow_status()
    now = datetime.now(timezone.utc).astimezone()
    days = max(30, min(int(valid_days), 730))
    valid_until = (now + timedelta(days=days)).isoformat(timespec="seconds")
    endpoints = [
        {
            "kind": flow["kind"],
            "label": flow["label"],
            "host": flow["host"],
            "scope": flow["scope"],
            "external": flow["external"],
            "allowed": flow["allowed"],
        }
        for flow in status["flows"]
    ]
    review_id = db.new_id("dfr")
    timestamp = now.isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO data_flow_reviews(review_id, actor, endpoints, note, valid_until, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            review_id,
            actor,
            json.dumps(endpoints, ensure_ascii=False),
            note.strip() or None,
            valid_until,
            timestamp,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="dataflow.review",
        object_type="DataFlowReview",
        object_id=review_id,
        before=None,
        after={
            "endpoints": endpoints,
            "compliant": status["compliant"],
            "valid_until": valid_until,
            "note": note.strip(),
        },
    )
    return {
        "review_id": review_id,
        "actor": actor,
        "endpoints": endpoints,
        "compliant": status["compliant"],
        "note": note.strip(),
        "valid_until": valid_until,
        "created_at": timestamp,
    }


def latest_data_flow_review(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM data_flow_reviews ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        endpoints = json.loads(row["endpoints"] or "[]")
    except (TypeError, ValueError):
        endpoints = []
    expires = None
    if row["valid_until"]:
        try:
            expires = datetime.fromisoformat(row["valid_until"])
        except ValueError:
            expires = None
    now = datetime.now(timezone.utc).astimezone()
    if expires is None:
        expired = True
        days_remaining = 0
    else:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        remaining = expires - now
        expired = remaining.total_seconds() <= 0
        days_remaining = max(0, int(remaining.total_seconds() // 86400))
    return {
        "review_id": row["review_id"],
        "actor": row["actor"],
        "endpoints": endpoints,
        "note": row["note"],
        "valid_until": row["valid_until"],
        "created_at": row["created_at"],
        "expired": expired,
        "days_remaining": days_remaining,
        "due_soon": (not expired) and days_remaining <= REVIEW_DUE_SOON_DAYS,
        "valid": not expired,
    }
