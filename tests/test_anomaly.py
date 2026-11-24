"""S5 自动异常检测：沉默、情绪、设备与超时。"""
from __future__ import annotations

from app.domain.anomaly import detect_anomalies


def test_detect_silence_emotion_device_and_timeout():
    transcript = [
        {"speaker": "patient", "start_ms": 0, "end_ms": 3000, "text": "大夫，我胸痛。"},
        {"speaker": "candidate", "start_ms": 6000, "end_ms": 9000, "text": "疼多久了？"},
        {"speaker": "patient", "start_ms": 150000, "end_ms": 153000, "text": "疼死了，受不了！"},
    ]
    anomalies = detect_anomalies(
        transcript,
        duration_ms=600000,
        station_duration_min=8,
        integrity_gap=True,
        media_present=False,
    )
    types = {entry["type"] for entry in anomalies}
    assert {"silence", "emotion", "device", "timeout"} <= types
    for entry in anomalies:
        assert entry["label"]
        assert entry["detail"]
        assert entry["severity"] in ("low", "medium", "high")


def test_normal_transcript_has_no_anomalies():
    transcript = [
        {"speaker": "candidate", "start_ms": 0, "end_ms": 10000, "text": "您好，我是张医生。"},
        {"speaker": "patient", "start_ms": 11000, "end_ms": 20000, "text": "我胸口疼。"},
    ]
    assert (
        detect_anomalies(
            transcript,
            duration_ms=20000,
            station_duration_min=8,
            integrity_gap=False,
            media_present=True,
        )
        == []
    )
