"""ASR 适配器：OpenAI 兼容转写、说话人分离与拒绝猜测。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.ai.asr import ASRError, HttpASRProvider, build_asr_provider

ASR_OUTPUT = {
    "duration": 6.0,
    "segments": [
        {
            "start": 0.0,
            "end": 2.5,
            "text": "大夫，我胸口疼。",
            "speaker": "SPEAKER_00",
            "avg_logprob": -0.2,
        },
        {
            "start": 3.0,
            "end": 6.0,
            "text": "疼多久了？有没有向肩膀放射？",
            "speaker": "SPEAKER_01",
            "avg_logprob": -0.1,
        },
    ],
}


class FakeASR:
    def __init__(self):
        self.responses: list[tuple[int, object]] = []
        self.requests: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def enqueue(self, status: int, payload: object) -> None:
        self.responses.append((status, payload))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                fake.requests.append({"path": self.path, "body": body})
                status, payload = fake.responses.pop(0) if fake.responses else (500, {"error": "empty"})
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
def fake_asr():
    server = FakeASR()
    yield server
    server.stop()


@pytest.fixture()
def audio_path(tmp_path):
    path = tmp_path / "consult.wav"
    path.write_bytes(b"RIFF" + b"0" * 128)
    return path


def _provider(fake_asr, max_retries: int = 0) -> HttpASRProvider:
    return HttpASRProvider(
        base_url=fake_asr.url,
        model="test-asr",
        api_key="test-key",
        timeout=3,
        max_retries=max_retries,
    )


def test_http_asr_transcribes_with_speaker_separation(fake_asr, audio_path):
    fake_asr.enqueue(200, ASR_OUTPUT)
    transcript = _provider(fake_asr).transcribe_media(audio_path)

    assert transcript["source"] == "asr"
    assert transcript["model_version"] == "test-asr"
    assert transcript["speaker_labels"] == {"SPEAKER_00": "patient", "SPEAKER_01": "candidate"}
    assert len(transcript["utterances"]) == 2
    first, second = transcript["utterances"]
    assert first["speaker"] == "patient"
    assert first["start_ms"] == 0
    assert second["speaker"] == "candidate"
    assert second["start_ms"] == 3000
    assert second["end_ms"] == 6000
    assert second["phase"] == "information_gathering"
    assert transcript["candidate_utterances"] == 1
    assert transcript["patient_utterances"] == 1

    request = fake_asr.requests[0]
    assert request["path"].endswith("/audio/transcriptions")
    body = request["body"].decode("utf-8", errors="replace")
    assert 'name="model"' in body and "test-asr" in body
    assert 'name="file"' in body and "consult.wav" in body


def test_missing_speaker_labels_are_rejected(fake_asr, audio_path):
    fake_asr.enqueue(
        200,
        {
            "segments": [
                {"start": 0, "end": 2, "text": "大夫，我胸口疼。"},
                {"start": 3, "end": 5, "text": "疼多久了？"},
            ]
        },
    )
    with pytest.raises(ASRError):
        _provider(fake_asr).transcribe_media(audio_path)


def test_single_speaker_is_rejected(fake_asr, audio_path):
    fake_asr.enqueue(
        200,
        {
            "segments": [
                {"start": 0, "end": 2, "text": "大夫，我胸口疼。", "speaker": "SPEAKER_00"},
                {"start": 3, "end": 5, "text": "还有别的不舒服吗？", "speaker": "SPEAKER_00"},
            ]
        },
    )
    with pytest.raises(ASRError):
        _provider(fake_asr).transcribe_media(audio_path)


def test_explicit_speaker_map_can_reverse_roles(fake_asr, audio_path):
    fake_asr.enqueue(200, ASR_OUTPUT)
    transcript = _provider(fake_asr).transcribe_media(
        audio_path,
        speaker_map={"SPEAKER_00": "candidate", "SPEAKER_01": "patient"},
    )
    assert transcript["utterances"][0]["speaker"] == "candidate"
    assert transcript["utterances"][1]["speaker"] == "patient"


def test_retry_on_5xx_then_success(fake_asr, audio_path):
    fake_asr.enqueue(503, {"error": "busy"})
    fake_asr.enqueue(200, ASR_OUTPUT)
    transcript = _provider(fake_asr, max_retries=1).transcribe_media(audio_path)
    assert len(transcript["utterances"]) == 2
    assert len(fake_asr.requests) == 2


def test_factory_is_off_by_default():
    bundle = build_asr_provider({})
    assert bundle.provider is None
    assert bundle.info["configured"] is False
    assert "ASR" in bundle.info["reason"]


def test_factory_builds_configured_provider(fake_asr):
    bundle = build_asr_provider(
        {
            "OSCELOOP_ASR_PROVIDER": "http",
            "OSCELOOP_ASR_BASE_URL": fake_asr.url,
            "OSCELOOP_ASR_MODEL": "test-asr",
            "OSCELOOP_ASR_MAX_RETRIES": "0",
        }
    )
    assert bundle.provider is not None
    assert bundle.info["configured"] is True
    assert bundle.info["model_version"] == "test-asr"
    assert bundle.info["base_url_host"].startswith("127.0.0.1")
