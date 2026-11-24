"""真实评分提供方：HTTP 接口、证据核验、重试与降级兜底。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import db
from app.ai.base import CapabilityGateway, Suggestion
from app.ai.factory import build_scoring_provider
from app.ai.http_provider import HttpScoringProvider, ProviderError
from app.ai.mock import MockScoringProvider
from app.ai.pipeline import run_scoring
from app.domain.bundle import freeze_bundle
from app.seed import build_bundle_payload

ITEM = {
    "item_id": "HX.01",
    "title": "胸痛特征采集",
    "phase": "information_gathering",
    "evidence_type": "utterance",
    "anchor": {
        "positive": "询问疼痛持续时间与放射部位",
        "partial": "只询问疼痛持续时间",
        "negative": "未询问胸痛特征",
    },
    "probe": {"positive": ["疼多久"], "partial": ["疼"]},
    "critical": True,
}
TRANSCRIPT = [
    {
        "utt_id": "u1",
        "speaker": "patient",
        "start_ms": 0,
        "end_ms": 2000,
        "text": "大夫，我胸口疼。",
        "phase": "information_gathering",
    },
    {
        "utt_id": "u2",
        "speaker": "candidate",
        "start_ms": 3000,
        "end_ms": 6000,
        "text": "疼多久了？有没有向肩膀放射？",
        "phase": "information_gathering",
    },
]
ATTEMPT_META = {"media_id": "media_test", "duration_ms": 60000, "asr_confidence": 0.96}
VALID_OUTPUT = {
    "score": 2,
    "confidence": 0.91,
    "evidence": [{"utt_id": "u2", "quote": "疼多久了？有没有向肩膀放射？", "evidence_kind": "utterance"}],
    "rationale": "考生询问了持续时间与放射部位，命中阳性行为锚。",
    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
}


class FakeAI:
    def __init__(self):
        self.responses: list[tuple[int, object]] = []
        self.requests: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def enqueue(self, status: int, content: object) -> None:
        self.responses.append((status, content))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append(body)
                status, payload = fake.responses.pop(0) if fake.responses else (500, {"error": "empty"})
                if isinstance(payload, dict) and "choices" not in payload and "error" not in payload:
                    usage = payload.get("usage")
                    payload = {"choices": [{"message": {"content": json.dumps(payload)}}]}
                    if usage is not None:
                        payload["usage"] = usage
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        return Handler


@pytest.fixture()
def fake_ai():
    server = FakeAI()
    yield server
    server.stop()


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "ai.db")
    db.init_db(connection)
    yield connection
    connection.close()


def _provider(fake_ai, max_retries: int = 0) -> HttpScoringProvider:
    return HttpScoringProvider(
        base_url=fake_ai.url,
        model="test-model",
        api_key="test-key",
        timeout=3,
        max_retries=max_retries,
    )


def test_http_provider_scores_with_verifiable_evidence(fake_ai):
    fake_ai.enqueue(200, VALID_OUTPUT)
    provider = _provider(fake_ai)
    suggestion = provider.score_item(item=ITEM, transcript=TRANSCRIPT, attempt_meta=ATTEMPT_META)
    assert provider.last_usage == {"prompt_tokens": 1000, "completion_tokens": 500}
    assert isinstance(suggestion, Suggestion)
    assert suggestion.score == 2
    assert suggestion.confidence == 0.91
    assert len(suggestion.evidence) == 1
    evidence = suggestion.evidence[0]
    assert evidence.start_ms == 3000
    assert evidence.end_ms == 6000
    assert evidence.speaker == "candidate"
    assert evidence.transcript_hash.startswith("sha256:")
    request = fake_ai.requests[0]
    assert request["model"] == "test-model"
    assert "疼多久了" in request["messages"][1]["content"]


def test_unverifiable_evidence_is_rejected(fake_ai):
    fake_ai.enqueue(
        200,
        {
            "score": 2,
            "confidence": 0.9,
            "evidence": [{"quote": "这句台词并不在转写里"}],
            "rationale": "编造",
        },
    )
    with pytest.raises(ProviderError):
        _provider(fake_ai).score_item(item=ITEM, transcript=TRANSCRIPT, attempt_meta=ATTEMPT_META)


def test_score_without_evidence_is_rejected(fake_ai):
    fake_ai.enqueue(200, {"score": 2, "confidence": 0.9, "evidence": [], "rationale": "无证据"})
    with pytest.raises(ProviderError):
        _provider(fake_ai).score_item(item=ITEM, transcript=TRANSCRIPT, attempt_meta=ATTEMPT_META)


def test_retry_on_5xx_then_success(fake_ai):
    fake_ai.enqueue(503, {"error": "busy"})
    fake_ai.enqueue(200, VALID_OUTPUT)
    suggestion = _provider(fake_ai, max_retries=1).score_item(
        item=ITEM, transcript=TRANSCRIPT, attempt_meta=ATTEMPT_META
    )
    assert suggestion.score == 2
    assert len(fake_ai.requests) == 2


def test_gateway_falls_back_to_mock_with_correct_version_stamp(fake_ai, conn):
    fake_ai.enqueue(500, {"error": "down"})
    gateway = CapabilityGateway(
        conn,
        _provider(fake_ai),
        "test-model",
        "test-prompt",
        fallback_provider=MockScoringProvider(),
    )
    response = gateway.score_item(
        item=ITEM,
        transcript=TRANSCRIPT,
        attempt_meta=ATTEMPT_META,
        subject_ref="att_test:HX.01",
    )
    assert response["status"] == "degraded"
    assert response["degraded"] is True
    assert response["model_version"] == "mock-score-1.0"
    assert response["prompt_version"] == "score-prompt-1.0"
    assert response["output"].score == 2

    call = conn.execute("SELECT * FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    assert call["status"] == "degraded"
    assert call["model_version"] == "mock-score-1.0"
    detail = json.loads(call["detail"])
    assert detail["fallback"] == "mock-score"
    assert "down" in detail["primary_error"]


class FailingProvider:
    name = "broken-model"

    def score_item(self, *, item, transcript, attempt_meta):
        raise RuntimeError("primary model down")


def test_degraded_scores_are_forced_to_human_review(conn):
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")
    gateway = CapabilityGateway(
        conn,
        FailingProvider(),
        "real-model",
        "real-prompt",
        fallback_provider=MockScoringProvider(),
    )
    run_scoring(
        conn,
        gateway,
        bundle=bundle,
        attempt={
            "attempt_id": "att_degraded",
            "risk_level": "L2",
            "release_stage": "G0_offline",
            "media_id": "media_test",
            "duration_ms": 60000,
            "asr_confidence": 0.96,
            "integrity_gap": False,
            "appeal": False,
        },
        transcript=TRANSCRIPT,
        actor="test",
    )
    records = conn.execute("SELECT * FROM score_records WHERE attempt_id = 'att_degraded'").fetchall()
    assert len(records) == len(bundle["payload"]["rubric"])
    scored = [row for row in records if row["ai_score"] is not None]
    assert scored
    for row in scored:
        routing = json.loads(row["routing"])
        assert routing["reason"] == "ai_degraded"
        assert routing["requires_human"] is True
        assert row["status"] == "pending_review"
        assert row["final_score"] is None
        assert row["model_version"] == "mock-score-1.0"


def test_low_asr_confidence_forces_full_station_review(conn):
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, duration_ms, integrity_gap, appeal, status, ab_group, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "att_low_asr", bundle["bundle_id"], "C2026-001", "考生", "SES", "L2",
            60000, 0, 0, "scored", "ai_assisted", db.now_iso(),
        ),
    )
    conn.commit()
    gateway = CapabilityGateway(
        conn, MockScoringProvider(), "mock-score-1.0", "score-prompt-1.0"
    )
    run_scoring(
        conn,
        gateway,
        bundle=bundle,
        attempt={
            "attempt_id": "att_low_asr",
            "risk_level": "L2",
            "release_stage": "G0_offline",
            "media_id": "media_test",
            "duration_ms": 60000,
            "asr_confidence": 0.42,
            "integrity_gap": False,
            "appeal": False,
        },
        transcript=TRANSCRIPT,
        actor="test",
    )
    records = conn.execute(
        "SELECT * FROM score_records WHERE attempt_id = 'att_low_asr'"
    ).fetchall()
    scored = [row for row in records if row["ai_score"] is not None]
    assert scored
    for row in scored:
        routing = json.loads(row["routing"])
        assert routing["reason"] == "asr_low_confidence"
        assert routing["route"] == "full_station_human"
        assert routing["requires_human"] is True
        assert routing["detail"]["asr_confidence"] == 0.42
        assert row["status"] == "pending_review"
        assert row["final_score"] is None
    attempt = conn.execute(
        "SELECT status FROM attempts WHERE attempt_id = 'att_low_asr'"
    ).fetchone()
    assert attempt["status"] == "pending_review"


def test_gateway_records_tokens_and_cost(fake_ai, conn, monkeypatch):
    monkeypatch.setattr("app.ai.base.AI_INPUT_PRICE_PER_1K", 0.01)
    monkeypatch.setattr("app.ai.base.AI_OUTPUT_PRICE_PER_1K", 0.02)
    fake_ai.enqueue(200, VALID_OUTPUT)
    gateway = CapabilityGateway(conn, _provider(fake_ai), "test-model", "test-prompt")
    response = gateway.score_item(
        item=ITEM,
        transcript=TRANSCRIPT,
        attempt_meta=ATTEMPT_META,
        subject_ref="att_cost:HX.01",
    )
    assert response["input_tokens"] == 1000
    assert response["output_tokens"] == 500
    assert response["cost_usd"] == 0.02
    call = conn.execute("SELECT * FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    assert call["input_tokens"] == 1000
    assert call["output_tokens"] == 500
    assert call["cost_usd"] == 0.02
    detail = json.loads(call["detail"])
    assert detail["input_tokens"] == 1000


def test_gateway_without_fallback_raises_and_logs_error(fake_ai, conn):
    fake_ai.enqueue(500, {"error": "down"})
    gateway = CapabilityGateway(conn, _provider(fake_ai), "test-model", "test-prompt")
    with pytest.raises(ProviderError):
        gateway.score_item(
            item=ITEM,
            transcript=TRANSCRIPT,
            attempt_meta=ATTEMPT_META,
            subject_ref="att_test:HX.01",
        )
    call = conn.execute("SELECT * FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    assert call["status"] == "error"
    assert call["model_version"] == "test-model"


def test_factory_defaults_to_mock():
    bundle = build_scoring_provider({})
    assert bundle.primary.name == "mock-score"
    assert bundle.fallback is None
    assert bundle.info["configured"] is False
    assert bundle.model_version == "mock-score-1.0"


def test_factory_builds_http_provider_with_fallback(fake_ai):
    bundle = build_scoring_provider(
        {
            "OSCELOOP_AI_PROVIDER": "http",
            "OSCELOOP_AI_BASE_URL": fake_ai.url,
            "OSCELOOP_AI_MODEL": "test-model",
            "OSCELOOP_AI_FALLBACK": "mock",
            "OSCELOOP_AI_MAX_RETRIES": "0",
        }
    )
    assert bundle.primary.name == "openai-compatible"
    assert bundle.fallback.name == "mock-score"
    assert bundle.info["configured"] is True
    assert bundle.info["fallback"] == "mock-score"
    assert bundle.model_version == "test-model"


def test_factory_missing_config_falls_back_to_mock():
    bundle = build_scoring_provider({"OSCELOOP_AI_PROVIDER": "http", "OSCELOOP_AI_MODEL": ""})
    assert bundle.primary.name == "mock-score"
    assert bundle.info["configured"] is False
    assert "OSCELOOP_AI_BASE_URL" in bundle.info["reason"]
