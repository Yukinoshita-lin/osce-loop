"""AI 提供方连通性自检：真实部署前验证 base_url/model/key 与证据链路。"""
from __future__ import annotations

import time
from typing import Any

from ..ai.base import CapabilityGateway
from ..ai.factory import build_scoring_provider

FIXTURE_ITEM = {
    "item_id": "OP.01",
    "title": "自我介绍与身份确认",
    "phase": "opening",
    "evidence_type": "utterance",
    "anchor": {
        "observable": "向患者说明自己是接诊医生，并核对患者姓名",
        "positive": "主动说明身份并核对姓名",
        "partial": "只说明身份或只核对姓名",
        "negative": "未说明身份也未核对姓名",
    },
    "probe": {"positive": ["我是.*医生", "怎么称呼"], "partial": ["您好"]},
    "critical": False,
    "confidence_floor": 0.55,
}
FIXTURE_TRANSCRIPT = [
    {
        "utt_id": "u001",
        "speaker": "candidate",
        "start_ms": 0,
        "end_ms": 3000,
        "text": "您好，我是急诊科张医生，请问您怎么称呼？",
        "phase": "opening",
    },
    {
        "utt_id": "u002",
        "speaker": "patient",
        "start_ms": 3200,
        "end_ms": 5000,
        "text": "我叫王建国。",
        "phase": "opening",
    },
]
FIXTURE_META = {"media_id": "ops_ai_check", "duration_ms": 5000, "asr_confidence": 0.98}


def check_scoring_provider(conn, provider_bundle=None) -> dict[str, Any]:
    bundle = provider_bundle or build_scoring_provider()
    primary = bundle.primary
    name = getattr(primary, "name", "unknown")
    if name == "mock-score":
        return {
            "ok": True,
            "mode": "mock",
            "status": "mock",
            "provider": name,
            "model_version": bundle.model_version,
            "prompt_version": bundle.prompt_version,
            "latency_ms": None,
            "note": "当前为 Mock 提供方，不会发起真实网络调用；配置 OSCELOOP_AI_PROVIDER=http 后可做连通性验证。",
        }
    gateway = CapabilityGateway(
        conn,
        primary,
        bundle.model_version,
        bundle.prompt_version,
        fallback_provider=bundle.fallback,
    )
    started = time.perf_counter()
    try:
        response = gateway.score_item(
            item=FIXTURE_ITEM,
            transcript=FIXTURE_TRANSCRIPT,
            attempt_meta=FIXTURE_META,
            subject_ref="ops:ai_check",
        )
        output = response.get("output")
        evidence = getattr(output, "evidence", []) or []
        return {
            "ok": response["status"] in ("ok", "degraded"),
            "mode": "live",
            "status": response["status"],
            "degraded": response["degraded"],
            "provider": name,
            "model_version": response["model_version"],
            "prompt_version": response["prompt_version"],
            "latency_ms": response["latency_ms"],
            "score": getattr(output, "score", None),
            "evidence_count": len(evidence),
            "refusal_reason": getattr(output, "refusal_reason", None),
            "note": "自检使用固定小样本，只验证连通性、版本戳与证据结构，不代表评分质量。",
        }
    except Exception as exc:
        return {
            "ok": False,
            "mode": "live",
            "status": "error",
            "provider": name,
            "model_version": bundle.model_version,
            "prompt_version": bundle.prompt_version,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": str(exc),
            "note": "模型调用失败；请检查 base_url、model、API key 与网络连通性。",
        }
