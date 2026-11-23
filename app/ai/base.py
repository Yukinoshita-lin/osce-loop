"""AI 能力网关：业务层只面向“能力”，不直连模型。"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import db


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
    """所有 AI 调用的唯一入口：版本绑定、证据强制、耗时与状态留痕。"""

    def __init__(self, conn, provider, model_version: str, prompt_version: str):
        self.conn = conn
        self.provider = provider
        self.model_version = model_version
        self.prompt_version = prompt_version

    def score_item(
        self,
        *,
        item: dict[str, Any],
        transcript: list[dict[str, Any]],
        attempt_meta: dict[str, Any],
        subject_ref: str,
    ) -> dict[str, Any]:
        return self._invoke(
            "score.suggest",
            lambda: self.provider.score_item(
                item=item,
                transcript=transcript,
                attempt_meta=attempt_meta,
            ),
            subject_ref=subject_ref,
            require_evidence=True,
        )

    def _invoke(self, capability: str, fn, *, subject_ref: str, require_evidence: bool = False) -> dict[str, Any]:
        started = time.perf_counter()
        status = "ok"
        try:
            output = fn()
            if require_evidence and isinstance(output, Suggestion):
                if output.score is not None and not output.evidence:
                    output.refusal = True
                    output.refusal_reason = "无法定位到具体证据片段，网关拒答并转人工"
                    output.score = None
                    output.confidence = None
                    status = "refused_no_evidence"
        except Exception as exc:  # 网关兜底：任何模型异常都降级到人工
            output = None
            status = "error"
            db.log_ai_call(
                self.conn,
                capability=capability,
                model_version=self.model_version,
                prompt_version=self.prompt_version,
                subject_ref=subject_ref,
                latency_ms=(time.perf_counter() - started) * 1000,
                status=status,
                detail={"error": str(exc)},
            )
            raise

        latency_ms = (time.perf_counter() - started) * 1000
        detail: dict[str, Any] = {"status": status}
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
        db.log_ai_call(
            self.conn,
            capability=capability,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            subject_ref=subject_ref,
            latency_ms=latency_ms,
            status=status,
            detail=detail,
        )
        return {
            "status": status,
            "output": output,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "latency_ms": round(latency_ms, 2),
        }


def suggestion_to_dict(suggestion: Suggestion) -> dict[str, Any]:
    data = asdict(suggestion)
    data["evidence"] = [asdict(ev) for ev in suggestion.evidence]
    return data


def evidence_to_json(evidence: list[EvidenceSpan]) -> str:
    return json.dumps([asdict(ev) for ev in evidence], ensure_ascii=False)
