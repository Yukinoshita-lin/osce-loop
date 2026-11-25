"""数据流向审计：模型与 ASR 端点是否在内网，外部端点必须显式放行。

设计约束：真实考生数据不得出院/校内网。系统无法替代网络管控，但可以在每次
质量报告生成时核对端点主机，把“配置了公网地址却未声明放行”暴露为治理告警。
"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlparse

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
