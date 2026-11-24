"""OpenAI 兼容评分提供方：真实模型接入、超时重试与证据逐字核验。

网关只面向 Suggestion 结构，模型返回的任何证据都必须能在转写中定位；
无法核验的证据直接拒答，交由网关降级到人工或 Mock 兜底。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..domain.rubric import PHASE_LABELS
from .base import EvidenceSpan, Suggestion

SYSTEM_PROMPT = """你是 OSCE 评分副驾，只能依据给出的逐字转写评分，禁止猜测。
只输出一个 JSON 对象，不要输出 JSON 以外的任何文字。结构：
{
  "score": 0 或 1 或 2 或 null,
  "confidence": 0.0 到 1.0,
  "evidence": [
    {"utt_id": "转写中的 utt_id", "quote": "逐字转写片段", "evidence_kind": "utterance"}
  ],
  "rationale": "一句话说明判定依据"
}
规则：
1. score=2/1/0 必须给出至少一条 evidence，且 quote 必须逐字来自转写；
2. 无法定位证据时必须返回 score=null，不要编造分数或时间戳；
3. 判定“该阶段未发生某行为”时，evidence_kind 用 "absence"，quote 写明缺席观察；
4. confidence 反映证据清晰度，不要用 1.0 表示绝对确定。"""

DRAFT_SYSTEM_PROMPT = """你是 OSCE 命题助手，只输出一个 JSON 站包草案，不要输出 JSON 以外的文字。
草案仅用于 L0 内部讨论，评分表必须由命题专家改写与签字。
结构：
{
  "title": "站名",
  "candidate_instructions": ["给考生的指令"],
  "sp_profile": {
    "name": "患者姓名", "age": 50, "gender": "male 或 female", "occupation": "职业",
    "persona": {"tone": "情绪基调", "language": "语言熟练度", "memory": "病史记忆",
                "cognition": 0, "disclosure": "披露阈值"},
    "forbidden": ["不给出诊断", "不给出用药建议", "不评价考生"],
    "opening_line": "开场白"
  },
  "extra_slots": [
    {"content": "患者可说的话", "phase": "opening", "disclosure": "active",
     "triggers": ["触发词"], "entities": ["实体"]}
  ],
  "guideline_kb": [{"ref": "指南条款编号", "text": "指南原文或摘要"}],
  "rubric": [
    {"item_id": "HX.01", "title": "评分项名", "domain": "communication 或 history_taking 或 assessment 或 clinical_reasoning 或 procedures 或 patient_safety",
     "anchor": {"observable": "可观察行为", "positive": "阳性档", "partial": "部分档", "negative": "阴性档"},
     "evidence_type": "utterance 或 action 或 document 或 global",
     "phase": "opening 或 information_gathering 或 assessment 或 plan 或 closing",
     "weight": 1, "ai_authority": "none 或 extract 或 suggest 或 decide",
     "critical": false, "confidence_floor": 0.55, "guideline_ref": "必须出现在 guideline_kb 的 ref",
     "slot": {"content": "患者在该阶段能说出的信息", "triggers": ["触发词"], "entities": ["实体"], "disclosure": "on_direct_ask"},
     "probe": {"positive": ["正则片段"], "partial": ["正则片段"]},
     "feedback_alt": "可直接背诵的替代话术"}
  ]
}
硬性规则：
1. 每条行为锚必须包含可观察动词（询问/说明/确认/完成等）和可听可见对象，禁止“理解/具备/表现出良好…”；
2. 每个评分项的 guideline_ref 必须出现在 guideline_kb；
3. 每个非 global 项必须有 slot.content 与 probe.positive；
4. 关键项不得使用 decide；
5. 必须包含给出的共病、无关合理诉求与主诉模糊内容，不得改写；
6. 不要写成“教科书病人”：信息应有干扰项与不确定性。"""


class ProviderError(RuntimeError):
    """模型不可用、响应不合法或证据无法核验。"""


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split())


def _find_utterance(transcript: list[dict[str, Any]], utt_id: Any, quote: str) -> dict[str, Any] | None:
    if utt_id:
        for utt in transcript:
            if str(utt.get("utt_id") or "") == str(utt_id):
                return utt
    target = _norm(quote)
    if not target:
        return None
    for utt in transcript:
        text = _norm(utt.get("text"))
        if text and (target == text or target in text):
            return utt
    for utt in transcript:
        text = _norm(utt.get("text"))
        if text and text in target:
            return utt
    return None


class HttpScoringProvider:
    """OpenAI 兼容的 /chat/completions 客户端。"""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: float = 30.0,
        max_retries: int = 1,
    ):
        base = base_url.rstrip("/")
        if not base:
            raise ValueError("AI base_url 不能为空")
        self.base_url = base
        self.endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self.model = model
        self.model_version = model
        self.api_key = api_key
        self.timeout = max(1.0, float(timeout))
        self.max_retries = max(0, min(5, int(max_retries)))
        self.last_usage: dict[str, Any] | None = None

    def score_item(
        self,
        *,
        item: dict[str, Any],
        transcript: list[dict[str, Any]],
        attempt_meta: dict[str, Any],
    ) -> Suggestion:
        self.last_usage = None
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(self._input_payload(item, transcript, attempt_meta), ensure_ascii=False)},
            ],
        }
        response = self._post(body)
        data = self._extract_json(response)
        return self._parse(data, item=item, transcript=transcript, attempt_meta=attempt_meta)

    def draft_bundle(
        self,
        *,
        topic: str,
        difficulty: str,
        duration_min: int,
        guidelines: list[dict[str, Any]],
        patient: dict[str, Any],
        quotas: dict[str, Any],
    ) -> dict[str, Any]:
        """返回未校验的站包草案；调用方必须再跑 validate_bundle 与配额检查。"""
        self.last_usage = None
        brief = {
            "topic": topic,
            "difficulty": difficulty,
            "duration_min": duration_min,
            "guidelines": guidelines,
            "patient": patient,
            "required_quota": quotas,
        }
        body = {
            "model": self.model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
            ],
        }
        response = self._post(body)
        return self._extract_json(response)

    def _input_payload(
        self,
        item: dict[str, Any],
        transcript: list[dict[str, Any]],
        attempt_meta: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "item": {
                "item_id": item.get("item_id"),
                "title": item.get("title"),
                "phase": item.get("phase"),
                "evidence_type": item.get("evidence_type"),
                "anchor": item.get("anchor"),
                "probe": item.get("probe"),
                "critical": bool(item.get("critical")),
            },
            "attempt": {
                "media_id": attempt_meta.get("media_id"),
                "duration_ms": attempt_meta.get("duration_ms"),
                "asr_confidence": attempt_meta.get("asr_confidence"),
            },
            "transcript": [
                {
                    "utt_id": utt.get("utt_id"),
                    "speaker": utt.get("speaker"),
                    "start_ms": utt.get("start_ms"),
                    "end_ms": utt.get("end_ms"),
                    "text": utt.get("text"),
                    "phase": utt.get("phase"),
                }
                for utt in transcript
            ],
        }

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProviderError("AI 响应不是合法 JSON") from exc
                if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
                    self.last_usage = payload["usage"]
                return payload
            except urllib.error.HTTPError as exc:
                reason = str(exc.reason or "")
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                    parsed = json.loads(body)
                    if isinstance(parsed, dict):
                        reason = str(parsed.get("error") or parsed.get("message") or reason)
                except Exception:
                    pass
                last_error = ProviderError(f"HTTP {exc.code}: {reason[:200]}")
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(min(0.5, 0.1 * (2 ** attempt)))
        raise ProviderError(f"AI 服务调用失败：{last_error}") from last_error

    def _extract_json(self, response: dict[str, Any]) -> dict[str, Any]:
        if isinstance(response, dict) and "choices" not in response and "score" in response:
            return response
        try:
            choice = response["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = (response.get("choices") or [{}])[0].get("text") if isinstance(response, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("AI 响应缺少评分内容")
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("AI 评分内容不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("AI 评分内容必须是 JSON 对象")
        return data

    def _parse(
        self,
        data: dict[str, Any],
        *,
        item: dict[str, Any],
        transcript: list[dict[str, Any]],
        attempt_meta: dict[str, Any],
    ) -> Suggestion:
        score = data.get("score")
        if score is not None:
            if isinstance(score, bool) or score not in (0, 1, 2):
                raise ProviderError(f"score 必须是 0/1/2 或 null，实际为 {score!r}")
            score = int(score)

        confidence = data.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise ProviderError("confidence 必须是数字") from exc
            if not 0.0 <= confidence <= 1.0:
                raise ProviderError("confidence 必须位于 0 到 1 之间")
            confidence = round(confidence, 3)

        raw_evidence = data.get("evidence") or []
        if not isinstance(raw_evidence, list):
            raise ProviderError("evidence 必须是数组")
        evidence: list[EvidenceSpan] = []
        for entry in raw_evidence[:8]:
            if not isinstance(entry, dict):
                raise ProviderError("evidence 条目必须是对象")
            kind = str(entry.get("evidence_kind") or "utterance")
            quote = str(entry.get("quote") or "").strip()
            if kind == "absence":
                evidence.append(self._absence_span(entry, item, attempt_meta, quote))
                continue
            match = _find_utterance(transcript, entry.get("utt_id"), quote)
            if match is None:
                raise ProviderError(f"证据片段无法在转写中核验：{quote[:40] or '空片段'}")
            text = str(match.get("text") or "")
            evidence.append(
                EvidenceSpan(
                    media_id=str(attempt_meta.get("media_id") or "ai_media"),
                    start_ms=int(match.get("start_ms") or 0),
                    end_ms=int(match.get("end_ms") or 0),
                    speaker=str(match.get("speaker") or "unknown"),
                    quote=text,
                    transcript_hash=_hash_text(text),
                )
            )

        if score is not None and not evidence:
            raise ProviderError("AI 给出分数但未提供可核验证据")

        rationale = data.get("rationale")
        return Suggestion(
            item_id=str(item.get("item_id")),
            score=score,
            confidence=confidence,
            evidence=evidence,
            rationale=str(rationale).strip() if rationale else "",
        )

    def _absence_span(
        self,
        entry: dict[str, Any],
        item: dict[str, Any],
        attempt_meta: dict[str, Any],
        quote: str,
    ) -> EvidenceSpan:
        duration = int(attempt_meta.get("duration_ms") or 0)
        try:
            start_ms = int(entry.get("start_ms") or 0)
            end_ms = int(entry.get("end_ms") or duration)
        except (TypeError, ValueError) as exc:
            raise ProviderError("absence 证据的起止时间不合法") from exc
        if end_ms < start_ms:
            raise ProviderError("absence 证据的结束时间早于开始时间")
        if duration and start_ms > duration:
            raise ProviderError("absence 证据超出媒体时长")
        end_ms = min(end_ms, duration) if duration else end_ms
        label = PHASE_LABELS.get(item.get("phase"), item.get("phase") or "本")
        text = quote or f"{label}阶段未观察到相关行为"
        return EvidenceSpan(
            media_id=str(attempt_meta.get("media_id") or "ai_media"),
            start_ms=max(0, start_ms),
            end_ms=max(start_ms, end_ms),
            speaker="system",
            quote=text,
            transcript_hash=_hash_text(text),
            evidence_kind="absence",
            note="模型判定缺席，仍需人工确认阶段边界。",
        )
