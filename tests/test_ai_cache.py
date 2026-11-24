"""AI 网关幂等缓存：同输入不重复调用、模型版本隔离、建议对象可还原。"""
from __future__ import annotations

from app import db
from app.ai.base import CapabilityGateway, EvidenceSpan, Suggestion


class CountingDraftProvider:
    name = "counting-draft"

    def __init__(self):
        self.calls = 0
        self.last_usage = {"input_tokens": 100, "output_tokens": 50}

    def draft_bundle(self, **kwargs):
        self.calls += 1
        return {
            "title": f"草案 {kwargs['topic']}",
            "difficulty": kwargs["difficulty"],
            "rubric": [{"item_id": "HX.01", "title": "测试项"}],
            "quota": kwargs["quotas"],
        }


class CountingAsrProvider:
    name = "counting-asr"

    def __init__(self):
        self.calls = 0
        self.last_usage = {"duration_seconds": 60}

    def transcribe_media(self, media_path, language="zh", speaker_map=None, first_speaker="patient"):
        self.calls += 1
        return {
            "provider": self.name,
            "utterances": [{"utt_id": "u1", "speaker": "patient", "start_ms": 0, "end_ms": 1000, "text": "胸口疼"}],
        }


class CountingScoreProvider:
    name = "counting-score"

    def __init__(self):
        self.calls = 0
        self.last_usage = {"prompt_tokens": 200, "completion_tokens": 80}

    def score_item(self, *, item, transcript, attempt_meta):
        self.calls += 1
        return Suggestion(
            item_id=item["item_id"],
            score=2,
            confidence=0.9,
            evidence=[
                EvidenceSpan(
                    media_id=attempt_meta.get("media_id") or "media",
                    start_ms=1000,
                    end_ms=3000,
                    speaker="candidate",
                    quote="疼多久了？",
                    transcript_hash="sha256:abc",
                )
            ],
            rationale="命中阳性行为锚",
        )


def _draft_kwargs(difficulty: str = "medium"):
    return {
        "topic": "胸痛",
        "difficulty": difficulty,
        "duration_min": 8,
        "guidelines": [{"ref": "ESC-ACS-2023", "text": "胸痛评估"}],
        "patient": {"name": "王建国", "age": 65},
        "quotas": {"comorbidity": "高血压", "unrelated_concern": "失眠", "ambiguity": "胸口不舒服"},
    }


def test_draft_is_cached_by_inputs_and_model_version(tmp_path):
    conn = db.connect(tmp_path / "cache.db")
    db.init_db(conn)
    provider = CountingDraftProvider()
    gateway = CapabilityGateway(conn, provider, "draft-m1", "prompt-p1")

    first = gateway.draft_bundle(**_draft_kwargs())
    assert first["status"] == "ok"
    assert first["cached"] is False
    assert provider.calls == 1

    second = gateway.draft_bundle(**_draft_kwargs())
    assert second["status"] == "cached"
    assert second["cached"] is True
    assert second["cost_usd"] == 0.0
    assert provider.calls == 1
    assert second["output"] == first["output"]

    other = gateway.draft_bundle(**_draft_kwargs("hard"))
    assert other["cached"] is False
    assert provider.calls == 2

    upgraded = CapabilityGateway(conn, provider, "draft-m2", "prompt-p1")
    versioned = upgraded.draft_bundle(**_draft_kwargs())
    assert versioned["cached"] is False
    assert provider.calls == 3

    statuses = [
        row["status"]
        for row in conn.execute(
            "SELECT status FROM ai_calls WHERE capability = 'draft.bundle' ORDER BY rowid"
        ).fetchall()
    ]
    assert statuses == ["ok", "cached", "ok", "ok"]
    conn.close()


def test_asr_cache_requires_explicit_idempotency_key(tmp_path):
    conn = db.connect(tmp_path / "asr_cache.db")
    db.init_db(conn)
    provider = CountingAsrProvider()
    gateway = CapabilityGateway(conn, provider, "asr-m1", "asr-p1")

    no_cache_one = gateway.transcribe_media(media_path="a.wav", subject_ref="media:1")
    no_cache_two = gateway.transcribe_media(media_path="a.wav", subject_ref="media:2")
    assert no_cache_one["cached"] is False
    assert no_cache_two["cached"] is False
    assert provider.calls == 2

    key = "asr:sha256:aaa:zh:patient:"
    cached_one = gateway.transcribe_media(media_path="b.wav", subject_ref="media:3", idempotency_key=key)
    cached_two = gateway.transcribe_media(media_path="b.wav", subject_ref="media:4", idempotency_key=key)
    assert cached_one["cached"] is False
    assert cached_two["cached"] is True
    assert provider.calls == 3
    assert cached_two["output"]["utterances"][0]["text"] == "胸口疼"
    conn.close()


def test_suggestion_cache_restores_evidence_objects(tmp_path):
    conn = db.connect(tmp_path / "score_cache.db")
    db.init_db(conn)
    provider = CountingScoreProvider()
    gateway = CapabilityGateway(conn, provider, "score-m1", "score-p1")
    item = {"item_id": "HX.01", "confidence_floor": 0.55}

    first = gateway.score_item(
        item=item,
        transcript=[],
        attempt_meta={"media_id": "media_1"},
        subject_ref="att:1:HX.01",
        idempotency_key="score:test",
    )
    second = gateway.score_item(
        item=item,
        transcript=[],
        attempt_meta={"media_id": "media_1"},
        subject_ref="att:2:HX.01",
        idempotency_key="score:test",
    )
    assert provider.calls == 1
    assert second["cached"] is True
    suggestion = second["output"]
    assert isinstance(suggestion, Suggestion)
    assert isinstance(suggestion.evidence[0], EvidenceSpan)
    assert suggestion.evidence[0].quote == "疼多久了？"
    assert suggestion.score == 2
    conn.close()
