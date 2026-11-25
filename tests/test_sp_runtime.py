"""SP 运行时质量汇总：降级轮次与最近降级会话。"""
from __future__ import annotations

from app import db
from app.domain.spqa import sp_runtime_summary


def _conn(tmp_path):
    conn = db.connect(tmp_path / "sp_runtime.db")
    db.init_db(conn)
    return conn


def _call(conn, status, detail=None):
    db.log_ai_call(
        conn,
        capability="sp.generate+verify",
        model_version="sp-model",
        prompt_version="sp-prompt",
        subject_ref="sp_test",
        latency_ms=12.5,
        status=status,
        detail=detail,
    )


def test_empty_runtime_summary(tmp_path):
    conn = _conn(tmp_path)
    summary = sp_runtime_summary(conn)
    assert summary["turns"] == 0
    assert summary["degraded"] == 0
    assert summary["degradation_rate"] is None
    assert summary["recent_degraded"] == []
    conn.close()


def test_runtime_summary_counts_and_details(tmp_path):
    conn = _conn(tmp_path)
    _call(conn, "ok")
    _call(conn, "ok")
    _call(
        conn,
        "degraded",
        {"total": 0.6, "violations": [{"kind": "clinical_safety", "entities": ["你得了"]}]},
    )
    summary = sp_runtime_summary(conn)
    assert summary["turns"] == 3
    assert summary["degraded"] == 1
    assert summary["degradation_rate"] == 0.3333
    assert summary["by_status"] == [{"key": "degraded", "count": 1}, {"key": "ok", "count": 2}]
    assert len(summary["recent_degraded"]) == 1
    recent = summary["recent_degraded"][0]
    assert recent["session_id"] == "sp_test"
    assert recent["total"] == 0.6
    assert recent["violations"][0]["kind"] == "clinical_safety"
    conn.close()
