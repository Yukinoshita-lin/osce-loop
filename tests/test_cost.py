"""AI 成本归集：评分与 ASR 调用按场次汇总。"""
from __future__ import annotations

from app import db
from app.domain.cost import ai_cost_summary


def _insert_call(conn, call_id, subject_ref, cost, capability="score.suggest"):
    conn.execute(
        "INSERT INTO ai_calls(call_id, capability, model_version, prompt_version, subject_ref, "
        "latency_ms, status, cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (call_id, capability, "m1", "p1", subject_ref, 100.0, "ok", cost, db.now_iso()),
    )
    conn.commit()


def test_cost_is_attributed_to_attempts_and_media(tmp_path):
    conn = db.connect(tmp_path / "cost.db")
    db.init_db(conn)
    for attempt_id, media_id in (("att_1", "media_1"), ("att_2", "media_2")):
        conn.execute(
            "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
            "risk_level, media_id, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (attempt_id, "bnd", "C001", "考生", "SES", "L2", media_id, "scored", db.now_iso()),
        )
    conn.commit()

    _insert_call(conn, "c1", "att_1:HX.01", 0.1)
    _insert_call(conn, "c2", "media:media_1", 0.2, capability="asr.transcribe")
    _insert_call(conn, "c3", "att_1:HX.02", None)
    _insert_call(conn, "c4", "att_2:AS.01", 0.3)
    _insert_call(conn, "c5", "sp_session_1", 0.5)

    summary = ai_cost_summary(conn)
    assert summary["total_usd"] == 0.6
    assert summary["priced_calls"] == 3
    assert summary["attempts"] == 2
    assert summary["attempts_with_cost"] == 2
    assert summary["per_attempt_usd"] == 0.3
    conn.close()


def test_cost_summary_is_empty_without_priced_calls(tmp_path):
    conn = db.connect(tmp_path / "cost_empty.db")
    db.init_db(conn)
    summary = ai_cost_summary(conn)
    assert summary["total_usd"] == 0
    assert summary["per_attempt_usd"] is None
    assert summary["attempts_with_cost"] == 0
    conn.close()
