"""可复现的 Mock 评分器：用于 P0 离线跑通证据链与复核闭环。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from ..domain.rubric import PHASE_LABELS
from .base import EvidenceSpan, Suggestion


def _mmss(ms: int) -> str:
    total = max(0, ms // 1000)
    return f"{total // 60:02d}:{total % 60:02d}"


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class MockScoringProvider:
    """规则证据提取器。刻意保留转写/同义词遗漏，以便暴露人机分歧。"""

    name = "mock-score"
    model_version = "mock-score-1.0"
    prompt_version = "score-prompt-1.0"

    def score_item(
        self,
        *,
        item: dict[str, Any],
        transcript: list[dict[str, Any]],
        attempt_meta: dict[str, Any],
    ) -> Suggestion:
        probe = item.get("probe") or {}
        positive_patterns = [re.compile(p) for p in probe.get("positive", [])]
        partial_patterns = [re.compile(p) for p in probe.get("partial", [])]
        phase = item.get("phase")
        candidates = [
            utt
            for utt in transcript
            if utt.get("speaker") == "candidate" and (not phase or utt.get("phase") == phase)
        ]

        match_type: str | None = None
        matched: dict[str, Any] | None = None
        extra_hits = 0
        for pattern in positive_patterns:
            for utt in candidates:
                if pattern.search(utt.get("text", "")):
                    matched = utt
                    match_type = "positive"
                    extra_hits = sum(1 for u in candidates if pattern.search(u.get("text", ""))) - 1
                    break
            if matched:
                break
        if not matched:
            for pattern in partial_patterns:
                for utt in candidates:
                    if pattern.search(utt.get("text", "")):
                        matched = utt
                        match_type = "partial"
                        break
                if matched:
                    break

        asr = float(attempt_meta.get("asr_confidence") or 0.95)
        asr_factor = max(0.5, min(1.05, asr / 0.95))

        if matched and match_type == "positive":
            base_conf = 0.92 + min(0.04, max(0, extra_hits) * 0.01)
            score = 2
            quote = matched["text"]
            evidence = EvidenceSpan(
                media_id=attempt_meta.get("media_id") or "demo_media",
                start_ms=int(matched["start_ms"]),
                end_ms=int(matched["end_ms"]),
                speaker=matched["speaker"],
                quote=quote,
                transcript_hash=_hash_text(quote),
            )
            rationale = f"在 {_mmss(matched['start_ms'])} 捕获“{quote}”，匹配 positive 行为锚。"
        elif matched and match_type == "partial":
            base_conf = 0.78
            score = 1
            quote = matched["text"]
            evidence = EvidenceSpan(
                media_id=attempt_meta.get("media_id") or "demo_media",
                start_ms=int(matched["start_ms"]),
                end_ms=int(matched["end_ms"]),
                speaker=matched["speaker"],
                quote=quote,
                transcript_hash=_hash_text(quote),
            )
            rationale = f"在 {_mmss(matched['start_ms'])} 捕获“{quote}”，仅覆盖部分要素，匹配 partial 行为锚。"
        else:
            base_conf = 0.86
            score = 0
            phase_utts = [u for u in transcript if u.get("phase") == phase]
            if phase_utts:
                start_ms = min(int(u["start_ms"]) for u in phase_utts)
                end_ms = max(int(u["end_ms"]) for u in phase_utts)
            else:
                start_ms = 0
                end_ms = int(attempt_meta.get("duration_ms") or 0)
            label = PHASE_LABELS.get(phase, phase)
            evidence = EvidenceSpan(
                media_id=attempt_meta.get("media_id") or "demo_media",
                start_ms=start_ms,
                end_ms=end_ms,
                speaker="system",
                quote=f"该{label}阶段未观察到与「{item.get('title', item.get('item_id'))}」相关的询问",
                transcript_hash=_hash_text(f"{item.get('item_id')}:{start_ms}:{end_ms}"),
                evidence_kind="absence",
                note="阴性判定以阶段窗口作为缺席证据，仍需人工确认边界。",
            )
            rationale = f"{label}阶段未捕获相关询问 → negative（缺席证据）。"

        confidence = round(max(0.2, min(0.98, base_conf * asr_factor)), 3)
        return Suggestion(
            item_id=item["item_id"],
            score=score,
            confidence=confidence,
            evidence=[evidence],
            rationale=rationale,
        )
