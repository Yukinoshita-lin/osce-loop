"""AI 成本归集：评分与 ASR 调用按场次汇总，媒体按留存期摊销。"""
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
    targets = {row["key"]: row for row in summary["targets"]}
    assert targets["practice"]["status"] == "unmeasured"
    assert targets["exam"]["status"] == "unmeasured"
    conn.close()


def test_cost_targets_split_practice_and_exam(tmp_path):
    conn = db.connect(tmp_path / "cost_targets.db")
    db.init_db(conn)
    for attempt_id, risk in (("att_practice", "L1"), ("att_exam", "L2")):
        conn.execute(
            "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
            "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (attempt_id, "bnd", "C001", "考生", "SES", risk, "scored", db.now_iso()),
        )
    conn.commit()
    _insert_call(conn, "c1", "att_practice:HX.01", 1.0)
    _insert_call(conn, "c2", "att_exam:HX.01", 10.0)

    summary = ai_cost_summary(conn)
    targets = {row["key"]: row for row in summary["targets"]}
    assert targets["practice"]["attempts"] == 1
    assert targets["practice"]["avg_usd"] == 1.0
    assert targets["practice"]["avg_cny"] == round(1.0 * summary["cny_per_usd"], 2)
    assert targets["practice"]["status"] == "pass"
    assert targets["exam"]["attempts"] == 1
    assert targets["exam"]["avg_usd"] == 10.0
    assert targets["exam"]["status"] == "fail"
    assert summary["per_attempt_cny"] == round(5.5 * summary["cny_per_usd"], 2)
    conn.close()


def test_cost_includes_storage_amortization(tmp_path, monkeypatch):
    monkeypatch.setattr("app.domain.cost.STORAGE_PRICE_CNY_PER_GB_MONTH", 0.5)
    conn = db.connect(tmp_path / "cost_storage.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, media_size_bytes, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("att_store", "bnd", "C001", "考生", "SES", "L2", 1024**3, "scored", db.now_iso()),
    )
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, media_size_bytes, media_purged_at, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("att_purged", "bnd", "C002", "考生", "SES", "L2", 1024**3, db.now_iso(), "scored", db.now_iso()),
    )
    conn.commit()

    expected_storage = 0.5 * 365 / 30
    summary = ai_cost_summary(conn)
    assert summary["total_ai_cny"] == 0
    assert summary["total_storage_cny"] == round(expected_storage, 2)
    assert summary["attempts_with_cost"] == 1
    assert summary["per_attempt_cny"] == round(expected_storage, 2)
    targets = {row["key"]: row for row in summary["targets"]}
    exam = targets["exam"]
    assert exam["attempts"] == 1
    assert exam["ai_attempts"] == 0
    assert exam["avg_cny"] == round(expected_storage, 2)
    assert exam["status"] == "warn"

    _insert_call(conn, "c1", "att_store:HX.01", 0.1)
    summary = ai_cost_summary(conn)
    exam = {row["key"]: row for row in summary["targets"]}["exam"]
    assert exam["ai_attempts"] == 1
    assert exam["ai_cny"] == round(0.1 * summary["cny_per_usd"], 2)
    assert exam["storage_cny"] == round(expected_storage, 2)
    assert exam["avg_cny"] == round(0.1 * summary["cny_per_usd"] + expected_storage, 2)
    assert exam["status"] == "pass"
    conn.close()
