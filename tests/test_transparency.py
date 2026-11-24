"""模型卡与数据表快照刷新。"""
from __future__ import annotations

import json
import shutil

import pytest

from app import db
from app.domain.gates import evaluate_gates
from app.domain.governance import publish_model_card
from app.domain.stats import quality_report
from app.domain.transparency import attempt_ai_disclosure, refresh_transparency_assets
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
    assert conn.execute("SELECT COUNT(*) AS n FROM model_cards").fetchone()["n"] == cards_before + 1
    assert conn.execute("SELECT COUNT(*) AS n FROM data_sheets").fetchone()["n"] == sheets_before + 1

    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'model_card.refresh' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    assert json.loads(event["after"])["data_sheet_version"] == "1.1"


def test_refresh_requires_republish_for_g3(conn):
    refresh_transparency_assets(conn, actor="透明度管理员")
    gates = evaluate_gates(conn, quality_report(conn), "G3_limited")
    assert gates["metric_values"]["model_card"] == 0.0

    publish_model_card(conn, actor="治理委员会")
    gates = evaluate_gates(conn, quality_report(conn), "G3_limited")
    assert gates["metric_values"]["model_card"] == 1.0
