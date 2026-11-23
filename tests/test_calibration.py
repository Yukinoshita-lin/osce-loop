"""本地金标准导入与校准报告。"""
from __future__ import annotations

import json
import shutil

import pytest

from app import db
from app.domain.calibration import calibration_report, import_gold_scores, parse_gold_file
from app.seed import seed_if_empty


@pytest.fixture(scope="module")
def seeded_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("calibration") / "seed.db"
    conn = db.connect(path)
    db.init_db(conn)
    seed_if_empty(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(seeded_path, tmp_path):
    path = tmp_path / "calibration.db"
    shutil.copy(seeded_path, path)
    connection = db.connect(path)
    yield connection
    connection.close()


def _first_record(conn):
    return conn.execute(
        "SELECT attempt_id, item_id, gold_score FROM score_records ORDER BY attempt_id, item_id LIMIT 1"
    ).fetchone()


def test_parse_gold_csv_and_json():
    csv_rows = parse_gold_file(
        "gold.csv",
        b"attempt_id,item_id,gold_score\natt_2026_001,HX.01,2\n",
    )
    assert csv_rows[0]["gold_score"] == "2"
    json_rows = parse_gold_file(
        "gold.json",
        json.dumps({"scores": [{"attempt_id": "a", "item_id": "i", "gold_score": 1}]}).encode(),
    )
    assert json_rows[0]["item_id"] == "i"
    with pytest.raises(ValueError):
        parse_gold_file("bad.csv", b"attempt_id,item_id\nx,y\n")


def test_import_updates_gold_and_audits(conn):
    record = _first_record(conn)
    new_gold = 0 if record["gold_score"] != 0 else 2
    summary = import_gold_scores(
        conn,
        [
            {"attempt_id": record["attempt_id"], "item_id": record["item_id"], "gold_score": new_gold},
            {"attempt_id": record["attempt_id"], "item_id": record["item_id"], "gold_score": 2},
            {"attempt_id": "missing", "item_id": "HX.01", "gold_score": 1},
        ],
        actor="校准员",
        filename="gold.csv",
        file_hash="sha256:test",
    )
    assert summary["updated"] == 1
    assert summary["skipped"] == 1
    assert len(summary["errors"]) == 1
    updated = conn.execute(
        "SELECT gold_score FROM score_records WHERE attempt_id = ? AND item_id = ?",
        (record["attempt_id"], record["item_id"]),
    ).fetchone()
    assert updated["gold_score"] == new_gold

    event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'calibration.gold_import' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    detail = json.loads(event["after"])
    assert detail["updated"] == 1
    assert detail["filename"] == "gold.csv"

    expected_gold = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE gold_score IS NOT NULL"
    ).fetchone()["n"]
    report = calibration_report(conn)
    assert report["gold_count"] == expected_gold
    assert 0 <= report["coverage"] <= 1
    assert report["qwk_ai"] is not None
    assert len(report["confidence_bands"]) == 5
    assert report["items"]


def test_import_without_valid_rows_is_rejected(conn):
    with pytest.raises(ValueError):
        import_gold_scores(
            conn,
            [{"attempt_id": "missing", "item_id": "missing", "gold_score": 1}],
            actor="校准员",
            filename="bad.csv",
            file_hash="sha256:bad",
        )
