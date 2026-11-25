"""模型卡与数据表快照刷新。"""
from __future__ import annotations

import json
import shutil

import pytest

from app import db
from app.domain.gates import evaluate_gates
from app.domain.governance import change_model_version, publish_model_card
from app.domain.stats import quality_report
from app.domain.transparency import (
    acknowledge_disclosure,
    attempt_ai_disclosure,
    disclosure_acknowledged,
    public_transparency,
    refresh_transparency_assets,
)
from app.seed import seed_if_empty


@pytest.fixture(scope="module")
def seeded_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("transparency") / "seed.db"
    conn = db.connect(path)
    db.init_db(conn)
    seed_if_empty(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(seeded_path, tmp_path):
    path = tmp_path / "transparency.db"
    shutil.copy(seeded_path, path)
    connection = db.connect(path)
    yield connection
    connection.close()


def test_disclosure_acknowledgement_is_idempotent_and_audited(tmp_path):
    conn = db.connect(tmp_path / "ack.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
        "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("att_ack", "bnd", "C001", "考生", "SES", "L1", "published", db.now_iso()),
    )
    conn.commit()
    first = acknowledge_disclosure(conn, attempt_id="att_ack", user_id="usr_student", actor="练习学生")
    second = acknowledge_disclosure(conn, attempt_id="att_ack", user_id="usr_student", actor="练习学生")
    assert first["ack_id"] == second["ack_id"]
    assert disclosure_acknowledged(conn, "att_ack", "usr_student")["ack_id"] == first["ack_id"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM ai_disclosure_acks WHERE attempt_id = 'att_ack'"
    ).fetchone()["n"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'attempt.ai_disclosure_ack'"
    ).fetchone()["n"] == 1
    conn.close()


def test_public_transparency_only_returns_published_assets(conn):
    data = public_transparency(conn)
    assert data["model_cards"] == []
    assert data["data_sheet"] is None

    publish_model_card(conn, actor="治理委员会")
    data = public_transparency(conn)
    assert len(data["model_cards"]) == 1
    card = data["model_cards"][0]
    assert card["capability"]
    assert card["model_version"]
    assert data["data_sheet"]["version"]
    assert data["data_sheet"]["content"]["name"]


def test_ai_disclosure_lists_stages_and_human_confirmation():
    disclosure = attempt_ai_disclosure(
        {"status": "signed_off", "signed_off_by": "考务 李萍"},
        [
            {
                "ai_score": 2,
                "model_version": "score-model-2.0",
                "prompt_version": "score-prompt-1.1",
                "reviewer": "主治 张蕾",
            },
            {"ai_score": 2, "reviewer": "AI", "model_version": "score-model-2.0"},
        ],
        has_transcript=True,
    )
    assert disclosure["stages"] == ["语音转写（ASR）", "证据定位与评分建议"]
    assert disclosure["model_versions"] == ["score-model-2.0"]
    assert disclosure["prompt_versions"] == ["score-prompt-1.1"]
    assert disclosure["human_confirmed"] is True
    assert set(disclosure["human_reviewers"]) == {"主治 张蕾", "考务 李萍"}
    assert "AI" not in disclosure["human_reviewers"]
    assert "人工复核确认" in disclosure["statement"]


def test_ai_disclosure_without_ai_participation():
    disclosure = attempt_ai_disclosure(
        {"status": "pending_review", "signed_off_by": None}, [], has_transcript=False
    )
    assert disclosure["stages"] == []
    assert disclosure["human_confirmed"] is False
    assert "人工评定" in disclosure["statement"]


def test_refresh_creates_new_private_assets(conn):
    report = quality_report(conn)
    cards_before = conn.execute("SELECT COUNT(*) AS n FROM model_cards").fetchone()["n"]
    sheets_before = conn.execute("SELECT COUNT(*) AS n FROM data_sheets").fetchone()["n"]

    result = refresh_transparency_assets(conn, actor="透明度管理员")
    assert result["metrics"]["qwk_ai"] == report["qwk_ai"]
    assert result["metrics"]["near_cutoff_agreement"] == report["near_cutoff_agreement"]
    assert result["metrics"]["item_disagreement_hotspots"] == report["item_disagreement_hotspots"]
    assert result["card_id"].startswith("mc_")
    assert result["data_sheet_version"] == "1.1"

    card = conn.execute(
        "SELECT * FROM model_cards WHERE card_id = ?", (result["card_id"],)
    ).fetchone()
    sheet = conn.execute(
        "SELECT * FROM data_sheets WHERE data_sheet_id = ?", (result["data_sheet_id"],)
    ).fetchone()
    assert card["is_public"] == 0
    assert sheet["is_public"] == 0
    assert json.loads(card["metrics"])["qwk_ai"] == report["qwk_ai"]
    assert isinstance(json.loads(card["metrics"])["subgroup_performance"], list)
    assert conn.execute("SELECT COUNT(*) AS n FROM model_cards").fetchone()["n"] == cards_before + 1
    assert conn.execute("SELECT COUNT(*) AS n FROM data_sheets").fetchone()["n"] == sheets_before + 1

    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'model_card.refresh' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    assert json.loads(event["after"])["data_sheet_version"] == "1.1"


def test_version_change_creates_private_model_card_snapshot(conn):
    cards_before = conn.execute("SELECT COUNT(*) AS n FROM model_cards").fetchone()["n"]
    result = change_model_version(
        conn, model_version="score-9.9", prompt_version="prompt-9.9", actor="测试"
    )
    card = conn.execute(
        "SELECT * FROM model_cards WHERE model_version = ? ORDER BY rowid DESC LIMIT 1",
        ("score-9.9",),
    ).fetchone()
    assert card is not None
    assert card["prompt_version"] == "prompt-9.9"
    assert card["is_public"] == 0
    assert result["model_card"]["card_id"] == card["card_id"]
    assert result["model_card"]["is_public"] is False
    assert "历史版本" in card["limitations"]
    assert conn.execute("SELECT COUNT(*) AS n FROM model_cards").fetchone()["n"] == cards_before + 1
    with pytest.raises(ValueError, match="人工比对校准"):
        publish_model_card(conn, actor="治理委员会")


def test_refresh_requires_republish_for_g3(conn):
    refresh_transparency_assets(conn, actor="透明度管理员")
    gates = evaluate_gates(conn, quality_report(conn), "G3_limited")
    assert gates["metric_values"]["model_card"] == 0.0

    publish_model_card(conn, actor="治理委员会")
    gates = evaluate_gates(conn, quality_report(conn), "G3_limited")
    assert gates["metric_values"]["model_card"] == 1.0


def test_quality_report_near_agreement_and_disagreement_hotspots(tmp_path):
    conn = db.connect(tmp_path / "quality.db")
    db.init_db(conn)
    conn.execute(
        "INSERT INTO bundles(bundle_id, station_id, title, version, bundle_hash, status, "
        "payload, derived, validation, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "bnd_q",
            "ST01",
            "质量测试站",
            "1.0",
            "hash_q",
            "frozen",
            json.dumps(
                {"rubric": [{"item_id": "HX.01", "title": "放射部位与持续时间"}]},
                ensure_ascii=False,
            ),
            "{}",
            "{}",
            db.now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, "
        "session_id, risk_level, near_cutoff, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("att_q", "bnd_q", "C900", "考生", "SES_Q", "L2", 1, db.now_iso()),
    )
    conn.execute(
        "INSERT INTO score_records(record_id, attempt_id, item_id, ai_score, gold_score, "
        "routing, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("rec_q", "att_q", "HX.01", 0, 2, "{}", "corrected", db.now_iso()),
    )
    conn.execute(
        "INSERT INTO review_actions(action_id, record_id, reviewer, action, created_at) "
        "VALUES(?,?,?,?,?)",
        ("act_q", "rec_q", "主治 张蕾", "correct", db.now_iso()),
    )
    conn.commit()

    report = quality_report(conn)
    assert report["near_cutoff_agreement"] == 0.0
    assert report["item_disagreement"] == {"HX.01": 1.0}
    assert report["item_disagreement_hotspots"] == [
        {
            "item_id": "HX.01",
            "item_title": "放射部位与持续时间",
            "rate": 1.0,
            "reviews": 1,
        }
    ]

    result = refresh_transparency_assets(conn, actor="透明度管理员")
    assert any("HX.01" in item for item in result["limitations"])
    conn.close()
