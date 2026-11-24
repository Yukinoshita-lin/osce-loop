"""考生纵向趋势：同站历史场次与能力域变化。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.history import candidate_history
from app.seed import build_bundle_payload


def _add_attempt(conn, bundle, attempt_id, candidate, score, created_at):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (attempt_id, bundle["bundle_id"], candidate, "考生", "SES", "L2", "published", created_at),
    )
    for item in bundle["payload"]["rubric"]:
        conn.execute(
            "INSERT INTO score_records(record_id, attempt_id, item_id, final_score, routing, status, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"rec_{attempt_id}_{item['item_id']}", attempt_id, item["item_id"], score, "{}", "accepted", created_at),
        )
    conn.commit()


def test_candidate_history_compares_same_station(tmp_path):
    conn = db.connect(tmp_path / "history.db")
    db.init_db(conn)
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")
    _add_attempt(conn, bundle, "att_old", "C900", 2, "2026-01-01T09:00:00+08:00")
    _add_attempt(conn, bundle, "att_new", "C900", 1, "2026-02-01T09:00:00+08:00")

    history = candidate_history(conn, "att_new")
    assert len(history["series"]) == 2
    assert history["previous"]["attempt_id"] == "att_old"
    assert history["previous"]["total"] == 100.0
    assert history["delta_total"] == -50.0
    assert history["domain_deltas"]
    assert all(delta["delta"] == -50.0 for delta in history["domain_deltas"])
    conn.close()


def test_candidate_history_without_previous_same_station(tmp_path):
    conn = db.connect(tmp_path / "history_empty.db")
    db.init_db(conn)
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")
    _add_attempt(conn, bundle, "att_only", "C901", 2, "2026-01-01T09:00:00+08:00")

    history = candidate_history(conn, "att_only")
    assert history["previous"] is None
    assert history["delta_total"] is None
    assert "暂无同站历史场次" in history["note"]
    conn.close()
