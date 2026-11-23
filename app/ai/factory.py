"""评分提供方装配：按环境变量选择 Mock 或 OpenAI 兼容真实模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from ..config import (
    AI_API_KEY,
    AI_BASE_URL,
    AI_FALLBACK,
    AI_MAX_RETRIES,
    AI_MODEL,
    AI_PROVIDER,
    AI_TIMEOUT_SECONDS,
    MODEL_VERSION,
    PROMPT_VERSION,
)
from .http_provider import HttpScoringProvider
from .mock import MockScoringProvider


@dataclass
class ScoringProviderBundle:
    primary: Any
    fallback: Any | None
    model_version: str
    prompt_version: str
    info: dict[str, Any] = field(default_factory=dict)


def _env(env: Mapping[str, str] | None, key: str, default: Any) -> Any:
    if env is None:
        return default
    return env.get(key, default)


def build_scoring_provider(env: Mapping[str, str] | None = None) -> ScoringProviderBundle:
    requested = str(_env(env, "OSCELOOP_AI_PROVIDER", AI_PROVIDER) or "mock").strip().lower()
    base_url = str(_env(env, "OSCELOOP_AI_BASE_URL", AI_BASE_URL) or "").strip()
    model = str(_env(env, "OSCELOOP_AI_MODEL", AI_MODEL) or "").strip()
    api_key = str(_env(env, "OSCELOOP_AI_API_KEY", AI_API_KEY) or "").strip()
    fallback_mode = str(_env(env, "OSCELOOP_AI_FALLBACK", AI_FALLBACK) or "").strip().lower()
    try:
        timeout = float(_env(env, "OSCELOOP_AI_TIMEOUT_SECONDS", AI_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = 30.0
    try:
        max_retries = int(_env(env, "OSCELOOP_AI_MAX_RETRIES", AI_MAX_RETRIES))
    except (TypeError, ValueError):
        max_retries = 1

    model_version = str(
        _env(env, "OSCELOOP_MODEL_VERSION", "") or model or MODEL_VERSION
    )
    prompt_version = str(_env(env, "OSCELOOP_PROMPT_VERSION", "") or PROMPT_VERSION)

    info: dict[str, Any] = {
        "requested": requested,
        "configured": False,
        "provider": "mock-score",
        "fallback": None,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "timeout_seconds": timeout,
        "max_retries": max_retries,
        "reason": None,
    }

    if requested in ("http", "openai", "openai-compatible", "real"):
        if not base_url or not model:
            fallback = None
            info.update(
                {
                    "reason": "缺少 OSCELOOP_AI_BASE_URL 或 OSCELOOP_AI_MODEL，已回退 Mock",
                    "provider": "mock-score",
                    "model_version": MODEL_VERSION,
                    "prompt_version": PROMPT_VERSION,
                }
            )
            return ScoringProviderBundle(
                primary=MockScoringProvider(),
                fallback=fallback,
                model_version=MODEL_VERSION,
                prompt_version=PROMPT_VERSION,
                info=info,
            )
        primary = HttpScoringProvider(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
        )
        fallback = MockScoringProvider() if fallback_mode not in ("", "none", "off") else None
        info.update(
            {
                "configured": True,
                "provider": primary.name,
                "fallback": getattr(fallback, "name", None),
                "base_url_host": urlparse(base_url).netloc,
            }
        )
        return ScoringProviderBundle(
            primary=primary,
            fallback=fallback,
            model_version=model_version,
            prompt_version=prompt_version,
            info=info,
        )

    return ScoringProviderBundle(
        primary=MockScoringProvider(),
        fallback=None,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
        info=info,
    )


def scoring_provider_info(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return build_scoring_provider(env).info
