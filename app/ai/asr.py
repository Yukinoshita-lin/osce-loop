"""ASR 适配器：OpenAI 兼容 /audio/transcriptions，强制说话人分离。"""
from __future__ import annotations

import json
import math
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from ..config import (
    ASR_API_KEY,
    ASR_BASE_URL,
    ASR_LANGUAGE,
    ASR_MAX_RETRIES,
    ASR_MODEL,
    ASR_PROVIDER,
    ASR_TIMEOUT_SECONDS,
)
from ..domain.transcript import map_speaker, normalize_items


class ASRError(RuntimeError):
    """ASR 不可用、响应不合法或说话人分离缺失。"""


def _segment_confidence(segment: dict[str, Any]) -> float | None:
    value = segment.get("confidence")
    if value is None:
        value = segment.get("avg_logprob")
        if value is not None:
            try:
                value = math.exp(float(value))
            except (TypeError, ValueError, OverflowError):
                value = None
    if value is None:
        return None
    try:
        return round(max(0.3, min(0.99, float(value))), 3)
    except (TypeError, ValueError):
        return None


def normalize_asr_segments(
    segments: list[dict[str, Any]],
    *,
    speaker_map: Mapping[str, str] | None = None,
    first_speaker: str = "patient",
    provider: str = "asr",
    model_version: str = "",
) -> dict[str, Any]:
    """把 ASR 分段规范为 utterance；缺少说话人标签时拒绝而不是猜测。"""
    if not segments:
        raise ASRError("ASR 未返回任何转写片段")
    if first_speaker not in ("patient", "candidate"):
        raise ASRError("first_speaker 必须是 patient 或 candidate")

    labels: list[str] = []
    for segment in segments:
        raw = str(segment.get("speaker") or segment.get("channel") or "").strip()
        if not raw:
            raise ASRError("ASR 返回的片段缺少说话人标签，无法自动区分考生与患者")
        if raw not in labels:
            labels.append(raw)
    if len(labels) < 2:
        raise ASRError("ASR 只识别到一个说话人，无法自动区分考生与患者")
    if len(labels) > 2:
        raise ASRError(f"ASR 识别到 {len(labels)} 个说话人，当前考站仅支持考生与患者两人")

    explicit = {str(key): str(value) for key, value in (speaker_map or {}).items()}
    role_map: dict[str, str] = {}
    for raw in labels:
        role = explicit.get(raw) or map_speaker(raw)
        role_map[raw] = role if role in ("patient", "candidate") else ""

    warnings: list[str] = []
    missing_roles = [role for role in ("patient", "candidate") if role not in role_map.values()]
    for raw in labels:
        if role_map[raw]:
            continue
        role = missing_roles.pop(0) if missing_roles else ("patient" if first_speaker == "patient" else "candidate")
        role_map[raw] = role
    if set(role_map.values()) != {"patient", "candidate"}:
        raise ASRError("ASR 说话人映射未同时覆盖考生与患者，请检查 speaker_map")
    if not explicit:
        warnings.append(
            "ASR 未提供标准角色标签，已按首次出现顺序映射为患者/考生；如顺序不符请改用带角色标注的转写。"
        )

    raw_items: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        raw_speaker = str(segment.get("speaker") or segment.get("channel") or "").strip()
        if segment.get("start_ms") is not None:
            start_ms = int(segment.get("start_ms") or 0)
        else:
            try:
                start_ms = int(round(float(segment.get("start") or 0) * 1000))
            except (TypeError, ValueError):
                start_ms = 0
        if segment.get("end_ms") is not None:
            end_ms = int(segment.get("end_ms") or 0)
        else:
            try:
                end_ms = int(round(float(segment.get("end") or 0) * 1000))
            except (TypeError, ValueError):
                end_ms = 0
        confidence = _segment_confidence(segment)
        raw_items.append(
            {
                "speaker": role_map[raw_speaker],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
                "asr_confidence": confidence if confidence is not None else 0.9,
            }
        )

    if not raw_items:
        raise ASRError("ASR 分段没有有效文本")
    transcript = normalize_items(raw_items, fmt="asr", warnings=warnings)
    transcript["source"] = "asr"
    transcript["provider"] = provider
    transcript["model_version"] = model_version
    transcript["speaker_labels"] = role_map
    return transcript


class HttpASRProvider:
    """OpenAI 兼容的 /audio/transcriptions 客户端。"""

    name = "openai-compatible-asr"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 60.0,
        max_retries: int = 1,
    ):
        base = base_url.rstrip("/")
        if not base:
            raise ValueError("ASR base_url 不能为空")
        self.base_url = base
        self.endpoint = base if base.endswith("/audio/transcriptions") else base + "/audio/transcriptions"
        self.model = model
        self.model_version = model
        self.api_key = api_key
        self.timeout = max(1.0, float(timeout))
        self.max_retries = max(0, min(3, int(max_retries)))

    def transcribe_media(
        self,
        path: Path | str,
        *,
        language: str = "zh",
        speaker_map: Mapping[str, str] | None = None,
        first_speaker: str = "patient",
    ) -> dict[str, Any]:
        media_path = Path(path)
        if not media_path.is_file():
            raise ASRError(f"媒体文件不存在：{media_path.name}")
        segments = self._post(media_path, language=language)
        return normalize_asr_segments(
            segments,
            speaker_map=speaker_map,
            first_speaker=first_speaker,
            provider=self.name,
            model_version=self.model,
        )

    def _post(self, path: Path, *, language: str) -> list[dict[str, Any]]:
        boundary = "----osceloop" + uuid.uuid4().hex
        parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode(
                    "utf-8"
                )
            )

        add_field("model", self.model)
        add_field("language", language or "zh")
        add_field("response_format", "verbose_json")
        add_field("temperature", "0")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        )
        parts.append(path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ASRError("ASR 响应不是合法 JSON") from exc
                return self._segments(payload)
            except urllib.error.HTTPError as exc:
                reason = str(exc.reason or "")
                try:
                    parsed = json.loads(exc.read().decode("utf-8", errors="replace"))
                    if isinstance(parsed, dict):
                        reason = str(parsed.get("error") or parsed.get("message") or reason)
                except Exception:
                    pass
                last_error = ASRError(f"HTTP {exc.code}: {reason[:200]}")
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(min(0.5, 0.1 * (2 ** attempt)))
        raise ASRError(f"ASR 服务调用失败：{last_error}") from last_error

    def _segments(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            raise ASRError("ASR 响应必须是 JSON 对象")
        raw_segments = payload.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            text = str(payload.get("text") or "").strip()
            if text:
                raise ASRError("ASR 只返回整段文本，缺少带说话人的分段；请使用支持说话人分离的模型")
            raise ASRError("ASR 响应缺少 segments")
        segments: list[dict[str, Any]] = []
        for entry in raw_segments:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            segments.append(
                {
                    "start": entry.get("start", 0),
                    "end": entry.get("end", 0),
                    "start_ms": entry.get("start_ms"),
                    "end_ms": entry.get("end_ms"),
                    "text": text,
                    "speaker": entry.get("speaker") or entry.get("channel"),
                    "confidence": entry.get("confidence"),
                    "avg_logprob": entry.get("avg_logprob"),
                }
            )
        if not segments:
            raise ASRError("ASR 分段没有有效文本")
        return segments


@dataclass
class ASRProviderBundle:
    provider: HttpASRProvider | None
    info: dict[str, Any] = field(default_factory=dict)


def build_asr_provider(env: Mapping[str, str] | None = None) -> ASRProviderBundle:
    def value(key: str, default: Any) -> Any:
        return (env if env is not None else {}).get(key, default)

    requested = str(value("OSCELOOP_ASR_PROVIDER", ASR_PROVIDER) or "off").strip().lower()
    base_url = str(value("OSCELOOP_ASR_BASE_URL", ASR_BASE_URL) or "").strip()
    model = str(value("OSCELOOP_ASR_MODEL", ASR_MODEL) or "").strip()
    api_key = str(value("OSCELOOP_ASR_API_KEY", ASR_API_KEY) or "").strip()
    language = str(value("OSCELOOP_ASR_LANGUAGE", ASR_LANGUAGE) or "zh").strip()
    try:
        timeout = float(value("OSCELOOP_ASR_TIMEOUT_SECONDS", ASR_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        timeout = 60.0
    try:
        retries = int(value("OSCELOOP_ASR_MAX_RETRIES", ASR_MAX_RETRIES))
    except (TypeError, ValueError):
        retries = 1

    info: dict[str, Any] = {
        "requested": requested,
        "configured": False,
        "provider": "off",
        "model_version": model or None,
        "language": language,
        "base_url_host": None,
        "timeout_seconds": timeout,
        "max_retries": retries,
        "reason": None,
    }
    if requested not in ("http", "openai", "openai-compatible"):
        info["reason"] = "ASR 未启用；可直接上传带说话人标注的转写"
        return ASRProviderBundle(provider=None, info=info)
    if not base_url or not model:
        info["reason"] = "缺少 OSCELOOP_ASR_BASE_URL 或 OSCELOOP_ASR_MODEL"
        return ASRProviderBundle(provider=None, info=info)
    provider = HttpASRProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=timeout,
        max_retries=retries,
    )
    info.update(
        {
            "configured": True,
            "provider": provider.name,
            "base_url_host": urlparse(base_url).netloc,
        }
    )
    return ASRProviderBundle(provider=provider, info=info)
