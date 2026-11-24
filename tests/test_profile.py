"""跨站能力画像：同一考生在不同站包的能力域聚合。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.profile import candidate_cross_station_profile
from app.seed import build_bundle_payload


def _bundle(conn, station_id, version="1.0"):
    payload = build_bundle_payload()
    payload["station_id"] = station_id
    return freeze_bundle(conn, payload, actor="测试", version=version)


def _attempt(conn, attempt_id, bundle_id, code, status, created_at):
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (attempt_id, bundle_id, code, "考生甲", "SES", "L2", status, created_at),
    )


def _score(conn, record_id, attempt_id, item_id, score):
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, routing, status, "
        "final_score, model_version, prompt_version, rubric_version, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            record_id,
            attempt_id,
            item_id,
            score,
            "{}",
            "reviewed",
            score,
            "mock-score-1.0",
            "score-prompt-1.0",
            "sha256:x",
            "2026-01-01T00:00:00+08:00",
        ),
    )


def test_profile_aggregates_published_and_current_attempt(tmp_path):
    conn = db.connect(tmp_path / "profile.db")
    db.init_db(conn)
    first = _bundle(conn, "ST-P1")
    second = _bundle(conn, "ST-P2")
    _attempt(conn, "att_pub", first["bundle_id"], "C900", "published", "2026-01-01T09:00:00+08:00")
    _attempt(conn, "att_cur", second["bundle_id"], "C900", "pending_review", "2026-02-01T09:00:00+08:00")
    _attempt(conn, "att_hidden", second["bundle_id"], "C900", "pending_review", "2026-03-01T09:00:00+08:00")
    _score(conn, "r1", "att_pub", "OP.01", 2)
    _score(conn, "r2", "att_pub", "HX.01", 0)
    _score(conn, "r3", "att_cur", "OP.01", 2)
    _score(conn, "r4", "att_cur", "HX.01", 1)
    _score(conn, "r5", "att_hidden", "OP.01", 0)
    _score(conn, "r6", "att_hidden", "HX.01", 2)
    conn.commit()

    profile = candidate_cross_station_profile(conn, "att_cur")
    assert profile["candidate_code"] == "C900"
    assert profile["attempts_count"] == 2
    assert profile["stations_count"] == 2
    domains = {entry["domain"]: entry for entry in profile["domains"]}
    assert domains["communication"]["score"] == 100.0
    assert domains["history_taking"]["score"] == 25.0
    assert profile["strongest"]["domain"] == "communication"
    assert profile["weakest"]["domain"] == "history_taking"
    assert "已发布场次与当前场次" in profile["note"]
    assert {entry["attempt_id"] for entry in profile["attempts"]} == {"att_pub", "att_cur"}
    conn.close()


def test_profile_skips_attempts_without_scores(tmp_path):
    conn = db.connect(tmp_path / "profile2.db")
    db.init_db(conn)
    bundle = _bundle(conn, "ST-P3")
    _attempt(conn, "att_only", bundle["bundle_id"], "C901", "published", "2026-01-01T09:00:00+08:00")
    profile = candidate_cross_station_profile(conn, "att_only")
    assert profile["attempts_count"] == 0
    assert profile["domains"] == []
    assert profile["strongest"] is None
    conn.close()
