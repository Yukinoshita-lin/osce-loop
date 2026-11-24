"""AI 能力网关：业务层只面向“能力”，不直连模型。"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import db
from ..config import AI_INPUT_PRICE_PER_1K, AI_OUTPUT_PRICE_PER_1K, ASR_PRICE_PER_MINUTE

_CACHE_MISS = object()


@dataclass
class EvidenceSpan:
    media_id: str
    start_ms: int
    end_ms: int
    speaker: str
    quote: str
    transcript_hash: str
    evidence_kind: str = "utterance"
    note: str | None = None


@dataclass
class Suggestion:
    item_id: str
    score: int | None
    confidence: float | None
    evidence: list[EvidenceSpan] = field(default_factory=list)
    rationale: str = ""
    refusal: bool = False
    refusal_reason: str | None = None


class CapabilityGateway:
    """所有 AI 调用的唯一入口：版本绑定、证据强制、降级兜底、耗时与状态留痕。"""

    def __init__(
        self,
        conn,
        provider,
        model_version: str,
        prompt_version: str,
        *,
        fallback_provider=None,
    ):
        self.conn = conn
        self.provider = provider
        self.model_version = model_version
        self.prompt_version = prompt_version
        self.fallback_provider = fallback_provider

    def score_item(
        self,
        *,
        item: dict[str, Any],
        transcript: list[dict[str, Any]],
        attempt_meta: dict[str, Any],
        subject_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        def primary():
            return self.provider.score_item(
                item=item,
                transcript=transcript,
                attempt_meta=attempt_meta,
            )

        def fallback():
            return self.fallback_provider.score_item(
                item=item,
                transcript=transcript,
                attempt_meta=attempt_meta,
            )

        return self._invoke(
            "score.suggest",
            primary,
            subject_ref=subject_ref,
            require_evidence=True,
            fallback_fn=fallback if self.fallback_provider else None,
            idempotency_key=idempotency_key,
        )

    def draft_bundle(
        self,
        *,
        topic: str,
        difficulty: str,
        duration_min: int,
        guidelines: list[dict[str, Any]],
        patient: dict[str, Any],
        quotas: dict[str, Any],
        subject_ref: str = "draft",
    ) -> dict[str, Any]:
        """S1 命题辅助也走同一网关：绑定模型/提示版本并计量 Token 与成本。"""
        if not hasattr(self.provider, "draft_bundle"):
            raise RuntimeError("当前提供方不支持命题草案生成")
        key_material = json.dumps(
            {
                "topic": topic,
                "difficulty": difficulty,
                "duration_min": duration_min,
                "guidelines": guidelines,
                "patient": patient,
                "quotas": quotas,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        return self._invoke(
            "draft.bundle",
            lambda: self.provider.draft_bundle(
                topic=topic,
                difficulty=difficulty,
                duration_min=duration_min,
                guidelines=guidelines,
                patient=patient,
                quotas=quotas,
            ),
            subject_ref=subject_ref,
            idempotency_key=f"draft:{digest}",
        )

    def transcribe_media(
        self,
        *,
        media_path,
        language: str = "zh",
        speaker_map=None,
        first_speaker: str = "patient",
        subject_ref: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """ASR 也必须经过网关：统一模型版本、耗时、状态与调用日志。"""
        if not hasattr(self.provider, "transcribe_media"):
            raise RuntimeError("当前提供方不支持 ASR")
        return self._invoke(
            "asr.transcribe",
            lambda: self.provider.transcribe_media(
                media_path,
                language=language,
                speaker_map=speaker_map,
                first_speaker=first_speaker,
            ),
            subject_ref=subject_ref,
            idempotency_key=idempotency_key,
        )

    def _invoke(
        self,
        capability: str,
        fn,
        *,
        subject_ref: str,
        require_evidence: bool = False,
        fallback_fn=None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key:
            cached = self._cache_lookup(capability, idempotency_key)
            if cached is not _CACHE_MISS:
                db.log_ai_call(
                    self.conn,
                    capability=capability,
                    model_version=self.model_version,
                    prompt_version=self.prompt_version,
                    subject_ref=subject_ref,
                    latency_ms=0.0,
                    status="cached",
                    detail={"cached": True, "idempotency_key": idempotency_key},
                )
                return {
                    "status": "cached",
                    "degraded": False,
                    "cached": True,
                    "output": cached,
                    "model_version": self.model_version,
                    "prompt_version": self.prompt_version,
                    "latency_ms": 0.0,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost_usd": 0.0,
                }
        started = time.perf_counter()
        status = "ok"
        used_model = self.model_version
        used_prompt = self.prompt_version
        detail: dict[str, Any] = {}
        try:
            try:
                output = fn()
            except Exception as exc:
                if fallback_fn is None:
                    raise
                output = fallback_fn()
                status = "degraded"
                used_model = getattr(self.fallback_provider, "model_version", self.model_version)
                used_prompt = getattr(self.fallback_provider, "prompt_version", self.prompt_version)
                detail.update(
                    {
                        "primary_error": str(exc),
                        "primary_model_version": self.model_version,
                        "fallback": getattr(self.fallback_provider, "name", "fallback"),
                    }
                )
            if require_evidence and isinstance(output, Suggestion):
                if output.score is not None and not output.evidence:
                    output.refusal = True
                    output.refusal_reason = "无法定位到具体证据片段，网关拒答并转人工"
                    output.score = None
                    output.confidence = None
                    status = "refused_no_evidence"
        except Exception as exc:  # 网关兜底：任何模型异常都降级到人工
            db.log_ai_call(
                self.conn,
                capability=capability,
                model_version=self.model_version,
                prompt_version=self.prompt_version,
                subject_ref=subject_ref,
                latency_ms=(time.perf_counter() - started) * 1000,
                status="error",
                detail={"error": str(exc)},
            )
            raise

        latency_ms = (time.perf_counter() - started) * 1000
        input_tokens, output_tokens, cost_usd = self._usage_metrics(capability)
        detail.update(
            {
                "status": status,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
        )
        if isinstance(output, Suggestion):
            detail.update(
                {
                    "item_id": output.item_id,
                    "score": output.score,
                    "confidence": output.confidence,
                    "evidence_count": len(output.evidence),
                    "refusal_reason": output.refusal_reason,
                }
            )
        elif isinstance(output, dict):
            detail.update(
                {
                    "source": output.get("source"),
                    "utterances": len(output.get("utterances") or []),
                    "candidate_utterances": output.get("candidate_utterances"),
                    "patient_utterances": output.get("patient_utterances"),
                }
            )
            if capability.startswith("draft."):
                detail.update(
                    {
                        "title": output.get("title"),
                        "rubric_items": len(output.get("rubric") or []),
                    }
                )
        if idempotency_key and status == "ok":
            self._cache_store(capability, idempotency_key, output)
        db.log_ai_call(
            self.conn,
            capability=capability,
            model_version=used_model,
            prompt_version=used_prompt,
            subject_ref=subject_ref,
            latency_ms=latency_ms,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            detail=detail,
        )
        return {
            "status": status,
            "degraded": status == "degraded",
            "cached": False,
            "output": output,
            "model_version": used_model,
            "prompt_version": used_prompt,
            "latency_ms": round(latency_ms, 2),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }

    def _cache_key(self, capability: str, idempotency_key: str) -> str:
        return (
            f"{capability}|{self.model_version}|{self.prompt_version}|{idempotency_key}"
        )

    def _cache_lookup(self, capability: str, idempotency_key: str) -> Any:
        row = self.conn.execute(
            "SELECT output FROM ai_call_cache WHERE cache_key = ?",
            (self._cache_key(capability, idempotency_key),),
        ).fetchone()
        if not row:
            return _CACHE_MISS
        try:
            return _deserialize_output(row["output"])
        except (TypeError, ValueError, KeyError):
            return _CACHE_MISS

    def _cache_store(self, capability: str, idempotency_key: str, output: Any) -> None:
        raw = _serialize_output(output)
        if raw is None:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO ai_call_cache(cache_key, capability, model_version, "
            "prompt_version, output, created_at) VALUES(?,?,?,?,?,?)",
            (
                self._cache_key(capability, idempotency_key),
                capability,
                self.model_version,
                self.prompt_version,
                raw,
                db.now_iso(),
            ),
        )
        self.conn.commit()

    def _usage_metrics(self, capability: str) -> tuple[int | None, int | None, float | None]:
        usage = getattr(self.provider, "last_usage", None) or {}

        def as_int(*keys: str) -> int | None:
            for key in keys:
                value = usage.get(key)
                if value is None:
                    continue
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
            return None

        input_tokens = as_int("input_tokens", "prompt_tokens")
        output_tokens = as_int("output_tokens", "completion_tokens")
        measured = False
        cost = 0.0
        if input_tokens:
            cost += input_tokens / 1000 * AI_INPUT_PRICE_PER_1K
            measured = True
        if output_tokens:
            cost += output_tokens / 1000 * AI_OUTPUT_PRICE_PER_1K
            measured = True
        duration = usage.get("duration_seconds")
        if duration is None:
            duration = usage.get("duration")
        if capability == "asr.transcribe" and duration is not None:
            try:
                cost += float(duration) / 60 * ASR_PRICE_PER_MINUTE
                measured = True
            except (TypeError, ValueError):
                pass
        return input_tokens, output_tokens, round(cost, 6) if measured else None


def _serialize_output(output: Any) -> str | None:
    if isinstance(output, Suggestion):
        return json.dumps(
            {"kind": "suggestion", "value": asdict(output)}, ensure_ascii=False
        )
    if isinstance(output, dict):
        try:
            return json.dumps({"kind": "dict", "value": output}, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
    return None


def _deserialize_output(raw: str) -> Any:
    data = json.loads(raw)
    if data.get("kind") == "suggestion":
        value = dict(data["value"])
        value["evidence"] = [EvidenceSpan(**entry) for entry in value.get("evidence") or []]
        return Suggestion(**value)
    return data.get("value")


def suggestion_to_dict(suggestion: Suggestion) -> dict[str, Any]:
    data = asdict(suggestion)
    data["evidence"] = [asdict(ev) for ev in suggestion.evidence]
    return data


def evidence_to_json(evidence: list[EvidenceSpan]) -> str:
    return json.dumps([asdict(ev) for ev in evidence], ensure_ascii=False)
