"""历史录像转写解析：JSON / SRT / VTT → 带时间戳与阶段的 utterance 表。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .rubric import PHASE_RANK, PHASES

CANDIDATE_ALIASES = (
    "医生",
    "考生",
    "医学生",
    "doctor",
    "dr",
    "dr.",
    "candidate",
    "student",
    "clinician",
)
PATIENT_ALIASES = (
    "患者",
    "病人",
    "模拟患者",
    "sp",
    "patient",
    "家属",
)
_CANDIDATE_ALIASES_NORMALIZED = {alias.lower().replace(".", "") for alias in CANDIDATE_ALIASES}
_PATIENT_ALIASES_NORMALIZED = {alias.lower().replace(".", "") for alias in PATIENT_ALIASES}
_SPEAKER_TOKEN = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")

SPEAKER_PREFIX = re.compile(
    r"^\s*(医生|考生|医学生|Doctor|Dr\.?|Candidate|Student|Clinician|患者|病人|模拟患者|SP|Patient|家属)\s*[：:]\s*",
    re.IGNORECASE,
)

CLOSING_TRIGGERS = ("总结", "还有问题", "都清楚", "明白吗", "回去注意", "先去检查")
PLAN_TRIGGERS = ("心电图", "心电监护", "监护", "抽血", "肌钙蛋白", "诊断", "考虑", "治疗", "方案", "住院", "转科", "处理", "开药", "会诊")
ASSESSMENT_TRIGGERS = ("听诊", "查体", "检查一下", "量血压", "触诊", "叩诊", "我看看", "体格检查")
OPENING_TRIGGERS = ("您好", "我是", "怎么称呼", "姓名", "核对", "年龄")
INFO_TRIGGERS = ("多久", "多长时间", "哪里", "哪里疼", "有没有", "什么", "怎么", "吗", "呢", "疼", "痛", "诱因", "放射", "过敏", "用药", "高血压", "糖尿病", "吸烟")


def _parse_time(value: Any, *, assume_ms: bool = False) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        if assume_ms:
            return int(number)
        return int(number * 1000) if number < 10000 else int(number)
    text = str(value).strip().replace(",", ".")
    if not text:
        return 0
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [float(p) for p in parts]
        except ValueError:
            return 0
        if len(numbers) == 3:
            hours, minutes, seconds = numbers
            return int((hours * 3600 + minutes * 60 + seconds) * 1000)
        if len(numbers) == 2:
            minutes, seconds = numbers
            return int((minutes * 60 + seconds) * 1000)
        return int(numbers[0] * 1000)
    try:
        number = float(text)
    except ValueError:
        return 0
    if assume_ms:
        return int(number)
    return int(number * 1000) if number < 10000 else int(number)


def _map_speaker(raw: str) -> str | None:
    value = (raw or "").strip().lower().replace(".", "")
    if not value:
        return None
    tokens = [token for token in _SPEAKER_TOKEN.split(value) if token]
    if value in _CANDIDATE_ALIASES_NORMALIZED or any(
        token in _CANDIDATE_ALIASES_NORMALIZED for token in tokens
    ):
        return "candidate"
    if value in _PATIENT_ALIASES_NORMALIZED or any(
        token in _PATIENT_ALIASES_NORMALIZED for token in tokens
    ):
        return "patient"
    # 较长的别名允许多语言标签内嵌，例如“模拟患者A”；短别名 sp/dr 只做精确匹配。
    if any(len(alias) >= 3 and alias in value for alias in _CANDIDATE_ALIASES_NORMALIZED):
        return "candidate"
    if any(len(alias) >= 3 and alias in value for alias in _PATIENT_ALIASES_NORMALIZED):
        return "patient"
    return None


def _split_prefix(text: str) -> tuple[str | None, str]:
    match = SPEAKER_PREFIX.match(text)
    if not match:
        return None, text
    return _map_speaker(match.group(1)), text[match.end():].strip()


def _infer_phase(text: str, current: str, speaker: str) -> str:
    if speaker != "candidate":
        return current
    lowered = text
    if any(word in lowered for word in CLOSING_TRIGGERS) and PHASE_RANK[current] >= PHASE_RANK["plan"]:
        return "closing"
    has_assessment = any(word in lowered for word in ASSESSMENT_TRIGGERS)
    has_plan = any(word in lowered for word in PLAN_TRIGGERS)
    if has_assessment and PHASE_RANK[current] < PHASE_RANK["assessment"]:
        return "assessment"
    if has_plan:
        return "plan" if PHASE_RANK[current] < PHASE_RANK["plan"] else current
    if has_assessment:
        return "assessment" if PHASE_RANK[current] < PHASE_RANK["assessment"] else current
    if any(word in lowered for word in OPENING_TRIGGERS) and current == "opening":
        return "opening"
    if any(word in lowered for word in INFO_TRIGGERS):
        return "information_gathering" if PHASE_RANK[current] < PHASE_RANK["information_gathering"] else current
    return current


def _normalize(
    raw_items: list[dict[str, Any]],
    *,
    fmt: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    utterances: list[dict[str, Any]] = []
    unknown_speakers = 0
    previous_end = -1
    current_phase = "opening"

    for index, item in enumerate(raw_items):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        speaker = _map_speaker(str(item.get("speaker") or item.get("role") or ""))
        if speaker is None:
            prefix_speaker, text = _split_prefix(text)
            speaker = prefix_speaker
        if speaker is None:
            speaker = "patient" if index % 2 == 0 else "candidate"
            unknown_speakers += 1

        if "start_ms" in item:
            start = _parse_time(item.get("start_ms"), assume_ms=True)
        else:
            start = _parse_time(item.get("start", item.get("start_time")))
        if "end_ms" in item:
            end = _parse_time(item.get("end_ms"), assume_ms=True)
        else:
            end = _parse_time(item.get("end", item.get("end_time")))
        if end <= start:
            end = start + max(1800, len(text) * 80)
        if start < previous_end:
            start = previous_end + 200
            end = max(end, start + max(1800, len(text) * 80))
        previous_end = end

        explicit_phase = item.get("phase")
        if explicit_phase in PHASES:
            phase = explicit_phase
            current_phase = phase
        else:
            current_phase = _infer_phase(text, current_phase, speaker)
            phase = current_phase

        confidence = item.get("asr_confidence")
        try:
            confidence = float(confidence) if confidence is not None else 0.95
        except (TypeError, ValueError):
            confidence = 0.95

        utterances.append(
            {
                "utt_id": f"u{len(utterances) + 1:03d}",
                "speaker": speaker,
                "start_ms": start,
                "end_ms": end,
                "text": text,
                "asr_confidence": round(max(0.3, min(0.99, confidence)), 3),
                "phase": phase,
            }
        )

    if unknown_speakers:
        warnings.append(f"有 {unknown_speakers} 条转写未标注说话人前缀，已按患者/考生交替推断。")
    if not any(u["speaker"] == "candidate" for u in utterances):
        warnings.append("未识别到考生发言，评分将全部缺少证据。")
    if not utterances:
        raise ValueError("转写内容为空或无法解析")
    return utterances


def _parse_srt(content: str, warnings: list[str], *, vtt: bool = False) -> list[dict[str, Any]]:
    text = content.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    if vtt:
        text = re.sub(r"^WEBVTT.*?\n", "", text, count=1)
        text = re.sub(r"^NOTE.*?(?=\n\n)", "", text, flags=re.MULTILINE | re.DOTALL)
    blocks = re.split(r"\n{2,}", text)
    items: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        left, _, right = lines[time_index].partition("-->")
        start = _parse_time(left)
        end = _parse_time(right.split()[0] if right.split() else right)
        body = " ".join(lines[time_index + 1:]).strip()
        if not body:
            continue
        speaker, body = _split_prefix(body)
        items.append(
            {
                "speaker": speaker,
                "start_ms": start,
                "end_ms": end,
                "text": body,
                "asr_confidence": 0.95,
            }
        )
    if not items:
        raise ValueError("未能解析出 SRT/VTT 字幕块")
    warnings.append("字幕格式按文本时间轴解析；建议在每句前标注“医生：”或“患者：”。")
    return items


def _parse_json(content: str, warnings: list[str]) -> list[dict[str, Any]]:
    data = json.loads(content)
    if isinstance(data, dict):
        for key in ("utterances", "transcript", "segments", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("JSON 转写必须是数组，或包含 utterances/transcript 数组")
    items = [item for item in data if isinstance(item, dict)]
    if not items:
        raise ValueError("JSON 转写没有有效 utterance")
    warnings.append("JSON 转写使用显式 start_ms/end_ms 时间戳。")
    return items


def map_speaker(raw: str) -> str | None:
    return _map_speaker(raw)


def normalize_items(
    raw_items: list[dict[str, Any]],
    *,
    fmt: str = "json",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    warnings = [] if warnings is None else warnings
    utterances = _normalize(raw_items, fmt=fmt, warnings=warnings)
    return {
        "format": fmt,
        "utterances": utterances,
        "warnings": warnings,
        "duration_ms": max((u["end_ms"] for u in utterances), default=0),
        "candidate_utterances": sum(1 for u in utterances if u["speaker"] == "candidate"),
        "patient_utterances": sum(1 for u in utterances if u["speaker"] == "patient"),
    }


NOTE_TIME_PREFIX = re.compile(
    r"^\s*[\[\(]?(\d{1,3}:\d{2}(?::\d{2})?)[\]\)]?\s*[-–—:：]?\s*(.+)$"
)


def parse_notes(raw: str, *, duration_ms: int = 0) -> list[dict[str, Any]]:
    """把考生笔记/操作日志转成考生侧 utterance，作为证据来源参与评分。

    支持 JSON 数组（字符串或 {text, at_ms/start/phase}）与逐行文本；
    无时间戳的笔记按场次末尾顺序排布，并在证据 note 中提示。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    items: list[dict[str, Any]] = []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        data = data.get("notes") or data.get("items")
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, str):
                items.append({"text": entry})
            elif isinstance(entry, dict):
                items.append(entry)
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            match = NOTE_TIME_PREFIX.match(line)
            if match:
                items.append({"start": match.group(1), "text": match.group(2)})
            else:
                items.append({"text": line})

    utterances: list[dict[str, Any]] = []
    cursor = max(0, int(duration_ms))
    for index, item in enumerate(items, start=1):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        has_time = any(key in item for key in ("start_ms", "start", "at_ms", "at"))
        if "start_ms" in item:
            start = _parse_time(item.get("start_ms"), assume_ms=True)
        elif "at_ms" in item:
            start = _parse_time(item.get("at_ms"), assume_ms=True)
        else:
            start = _parse_time(item.get("start", item.get("at")))
        if not has_time or start <= 0:
            start = cursor if not has_time else start
        if "end_ms" in item:
            end = _parse_time(item.get("end_ms"), assume_ms=True)
        elif "end" in item:
            end = _parse_time(item.get("end"))
        else:
            end = 0
        if end <= start:
            end = start + max(1800, len(text) * 80)
        cursor = max(cursor, end) + 700
        phase = item.get("phase")
        if phase not in PHASES:
            phase = _infer_phase(text, "information_gathering", "candidate")
        utterances.append(
            {
                "utt_id": f"n{index:03d}",
                "speaker": "candidate",
                "start_ms": start,
                "end_ms": end,
                "text": text,
                "asr_confidence": None,
                "phase": phase,
                "source": "note",
                "note_timestamp_supplied": has_time,
            }
        )
    return utterances


def parse_transcript(filename: str, content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("转写文件必须是 UTF-8 编码") from exc
    suffix = Path(filename or "").suffix.lower()
    warnings: list[str] = []
    if suffix == ".json" or text.lstrip().startswith(("[", "{")):
        fmt = "json"
        raw_items = _parse_json(text, warnings)
    elif suffix == ".vtt" or text.lstrip().startswith("WEBVTT"):
        fmt = "vtt"
        raw_items = _parse_srt(text, warnings, vtt=True)
    else:
        fmt = "srt"
        raw_items = _parse_srt(text, warnings)
    return normalize_items(raw_items, fmt=fmt, warnings=warnings)
