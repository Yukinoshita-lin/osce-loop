"""AI 能力网关：业务层只面向“能力”，不直连模型。"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import db
from ..config import AI_INPUT_PRICE_PER_1K, AI_OUTPUT_PRICE_PER_1K, ASR_PRICE_PER_MINUTE


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
        )

    def transcribe_media(
        self,
        *,
        media_path,
        language: str = "zh",
        speaker_map=None,
        first_speaker: str = "patient",
        subject_ref: str,
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
        )

    def _invoke(
        self,
        capability: str,
        fn,
        *,
        subject_ref: str,
        require_evidence: bool = False,
        fallback_fn=None,
    ) -> dict[str, Any]:
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
            "output": output,
            "model_version": used_model,
            "prompt_version": used_prompt,
            "latency_ms": round(latency_ms, 2),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }

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


def suggestion_to_dict(suggestion: Suggestion) -> dict[str, Any]:
    data = asdict(suggestion)
    data["evidence"] = [asdict(ev) for ev in suggestion.evidence]
    return data


def evidence_to_json(evidence: list[EvidenceSpan]) -> str:
    return json.dumps([asdict(ev) for ev in evidence], ensure_ascii=False)
