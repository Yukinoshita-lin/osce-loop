"""AI 能力网关限流：阈值、兜底降级、缓存不计数与无兜底报错。"""
from __future__ import annotations

import pytest

from app import db
from app.ai.base import CapabilityGateway
from app.ai.mock import MockScoringProvider

ITEM = {
    "item_id": "HX.01",
    "phase": "information_gathering",
    "probe": {"positive": ["疼多久"], "partial": []},
}
TRANSCRIPT = [
    {
        "speaker": "candidate",
        "phase": "information_gathering",
        "text": "您疼多久了？",
        "start_ms": 1000,
        "end_ms": 3000,
    }
]
META = {"media_id": "media_rate", "duration_ms": 10000, "asr_confidence": 0.95}


class MockASRProvider:
    name = "mock-asr"
    model_version = "mock-asr-1.0"
    prompt_version = "asr-prompt-1.0"

    def transcribe_media(self, media_path, *, language="zh", speaker_map=None, first_speaker="patient"):
        return {"utterances": [], "source": "mock", "language": language}


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "rate_limit.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _gateway(conn, *, limit, fallback=True):
    return CapabilityGateway(
        conn,
        MockScoringProvider(),
        "mock-score-1.0",
        "score-prompt-1.0",
        fallback_provider=MockScoringProvider() if fallback else None,
        rate_limit_per_minute=limit,
    )


def _score(gateway, ref: str):
    return gateway.score_item(
        item=ITEM,
        transcript=TRANSCRIPT,
        attempt_meta=META,
        subject_ref=ref,
        idempotency_key=None,
    )


def test_rate_limit_falls_back_and_requires_manual_confirmation(conn):
    gateway = _gateway(conn, limit=2)
    assert _score(gateway, "att_1")["status"] == "ok"
    assert _score(gateway, "att_2")["status"] == "ok"
    third = _score(gateway, "att_3")
    assert third["status"] == "rate_limited"
    assert third["degraded"] is True
    assert third["rate_limited"] is True
    assert third["output"].score is not None

    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM ai_calls GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    assert counts == {"ok": 2, "rate_limited": 1}
    detail = conn.execute(
        "SELECT detail FROM ai_calls WHERE status = 'rate_limited'"
    ).fetchone()["detail"]
    assert '"limit": 2' in detail


def test_rate_limit_without_fallback_raises_for_asr(conn):
    gateway = CapabilityGateway(
        conn,
        MockASRProvider(),
        "mock-asr-1.0",
        "asr-prompt-1.0",
        rate_limit_per_minute=1,
    )
    first = gateway.transcribe_media(media_path="demo.wav", subject_ref="att_1")
    assert first["status"] == "ok"
    with pytest.raises(RuntimeError, match="触发限流"):
        gateway.transcribe_media(media_path="demo.wav", subject_ref="att_2")
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM ai_calls GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["n"] for row in rows}
    assert counts == {"ok": 1, "rate_limited": 1}


def test_rate_limited_score_without_fallback_turns_into_refusal(conn):
    gateway = _gateway(conn, limit=1, fallback=False)
    assert _score(gateway, "att_1")["status"] == "ok"
    limited = _score(gateway, "att_2")
    assert limited["status"] == "rate_limited"
    assert limited["degraded"] is True
    assert limited["output"].refusal is True
    assert limited["output"].score is None
    assert "限流" in limited["output"].refusal_reason


def test_cached_calls_do_not_consume_rate_limit(conn):
    gateway = _gateway(conn, limit=2)
    first = gateway.score_item(
        item=ITEM,
        transcript=TRANSCRIPT,
        attempt_meta=META,
        subject_ref="att_1",
        idempotency_key="key-1",
    )
    second = gateway.score_item(
        item=ITEM,
        transcript=TRANSCRIPT,
        attempt_meta=META,
        subject_ref="att_1",
        idempotency_key="key-1",
    )
    third = _score(gateway, "att_2")
    assert first["status"] == "ok"
    assert second["status"] == "cached"
    assert third["status"] == "ok"
