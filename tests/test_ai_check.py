"""AI 提供方连通性自检：Mock 模式、真实调用与失败兜底。"""
from __future__ import annotations

from types import SimpleNamespace

from app import db
from app.ai.base import EvidenceSpan, Suggestion
from app.domain.ai_check import check_scoring_provider


class _LiveProvider:
    name = "fixture-live"
    model_version = "fixture-1.0"

    def score_item(self, *, item, transcript, attempt_meta):
        first = transcript[0]
        return Suggestion(
            item_id=item["item_id"],
            score=2,
            confidence=0.9,
            evidence=[
                EvidenceSpan(
                    media_id="ops_ai_check",
                    start_ms=first["start_ms"],
                    end_ms=first["end_ms"],
                    speaker=first["speaker"],
                    quote=first["text"],
                    transcript_hash="sha256:" + "0" * 24,
                )
            ],
            rationale="固定自检样本",
        )


class _BrokenProvider:
    name = "broken-live"
    model_version = "broken-1.0"

    def score_item(self, *, item, transcript, attempt_meta):
        raise RuntimeError("connection refused")


def _bundle(provider, configured=True):
    return SimpleNamespace(
        primary=provider,
        fallback=None,
        model_version=getattr(provider, "model_version", "test"),
        prompt_version="test-prompt",
        info={"configured": configured},
    )


def test_ai_check_mock_mode(tmp_path):
    conn = db.connect(tmp_path / "mock.db")
    db.init_db(conn)
    result = check_scoring_provider(conn)
    assert result["ok"] is True
    assert result["mode"] == "mock"
    assert result["latency_ms"] is None
    conn.close()


def test_ai_check_live_success(tmp_path):
    conn = db.connect(tmp_path / "live.db")
    db.init_db(conn)
    result = check_scoring_provider(conn, _bundle(_LiveProvider()))
    assert result["ok"] is True
    assert result["mode"] == "live"
    assert result["status"] == "ok"
    assert result["score"] == 2
    assert result["evidence_count"] == 1
    assert result["latency_ms"] is not None
    call = conn.execute("SELECT * FROM ai_calls ORDER BY rowid DESC LIMIT 1").fetchone()
    assert call["capability"] == "score.suggest"
    assert call["status"] == "ok"
    conn.close()


def test_ai_check_live_failure(tmp_path):
    conn = db.connect(tmp_path / "broken.db")
    db.init_db(conn)
    result = check_scoring_provider(conn, _bundle(_BrokenProvider()))
    assert result["ok"] is False
    assert result["status"] == "error"
    assert "connection refused" in result["error"]
    conn.close()
