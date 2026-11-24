"""S5 自动异常检测：沉默、情绪、设备中断与超时。

只记录与预警，不替考官做判断；异常不直接改变分数，由人工在复核或执行台处置。
"""
from __future__ import annotations

from typing import Any

SILENCE_THRESHOLD_MS = 60_000
TIMEOUT_GRACE_MS = 60_000
EMOTION_TERMS = (
    "疼死",
    "受不了",
    "难受死",
    "救命",
    "别碰",
    "不要碰",
    "烦躁",
    "生气",
    "吵",
    "投诉",
    "不想查",
    "不查了",
)


def _anomaly(
    kind: str,
    label: str,
    severity: str,
    detail: str,
    *,
    at_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "type": kind,
        "label": label,
        "severity": severity,
        "detail": detail,
        "at_ms": at_ms,
    }


def detect_anomalies(
    transcript: list[dict[str, Any]],
    *,
    duration_ms: int,
    station_duration_min: int | float | None = None,
    integrity_gap: bool = False,
    media_present: bool = True,
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    utterances = sorted(
        (entry for entry in transcript if int(entry.get("end_ms") or 0) > 0),
        key=lambda entry: int(entry.get("start_ms") or 0),
    )

    if utterances:
        gaps: list[tuple[int, int]] = []
        first_start = int(utterances[0].get("start_ms") or 0)
        if first_start > 0:
            gaps.append((0, first_start))
        for previous, current in zip(utterances, utterances[1:]):
            previous_end = int(previous.get("end_ms") or 0)
            current_start = int(current.get("start_ms") or 0)
            if current_start > previous_end:
                gaps.append((previous_end, current_start))
        last_end = int(utterances[-1].get("end_ms") or 0)
        if duration_ms > last_end:
            gaps.append((last_end, int(duration_ms)))
        if gaps:
            start, end = max(gaps, key=lambda gap: gap[1] - gap[0])
            if end - start >= SILENCE_THRESHOLD_MS:
                anomalies.append(
                    _anomaly(
                        "silence",
                        "长时间沉默",
                        "low",
                        f"{start // 1000}–{end // 1000} 秒无对话，需确认设备或考场状态。",
                        at_ms=start,
                    )
                )

    for entry in utterances:
        if entry.get("speaker") != "patient":
            continue
        text = str(entry.get("text") or "")
        hit = next((term for term in EMOTION_TERMS if term in text), None)
        if hit:
            anomalies.append(
                _anomaly(
                    "emotion",
                    "患者情绪激烈",
                    "medium",
                    f"患者表达“{text[:48]}”，建议考官关注沟通与安抚。",
                    at_ms=int(entry.get("start_ms") or 0),
                )
            )
            break

    if integrity_gap or not media_present:
        anomalies.append(
            _anomaly(
                "device",
                "设备/媒体中断",
                "high",
                "媒体缺失或完整性缺口，相关场次需全站人工复核。",
            )
        )

    if station_duration_min and duration_ms > int(float(station_duration_min) * 60_000) + TIMEOUT_GRACE_MS:
        anomalies.append(
            _anomaly(
                "timeout",
                "考试超时",
                "high",
                f"场次时长 {round(duration_ms / 60000, 1)} 分钟，超过站包设定 {station_duration_min} 分钟。",
            )
        )

    return anomalies
