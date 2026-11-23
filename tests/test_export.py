"""申诉举证包：内容完整性、哈希可复算与导出审计。"""
from __future__ import annotations

import json
import shutil

import pytest

from app import db
from app.domain.export import build_attempt_package, package_hash
from app.seed import seed_if_empty


@pytest.fixture(scope="module")
def seeded_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("export") / "seed.db"
    conn = db.connect(path)
    db.init_db(conn)
    seed_if_empty(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(seeded_path, tmp_path):
    path = tmp_path / "export.db"
    shutil.copy(seeded_path, path)
    connection = db.connect(path)
    yield connection
    connection.close()


def test_package_contains_case_evidence_and_audit(conn):
    attempt_id = conn.execute("SELECT attempt_id FROM attempts ORDER BY attempt_id LIMIT 1").fetchone()[
        "attempt_id"
    ]
    result = build_attempt_package(conn, attempt_id, exported_by="审计员 周宁")
    package = result["package"]

    assert result["package_hash"].startswith("sha256:")
    assert package_hash(package) == result["package_hash"]
    assert package["schema_version"] == "osce-loop-appeal-v1"
    assert package["attempt"]["attempt_id"] == attempt_id
    assert package["bundle"]["bundle_hash"].startswith("sha256:")
    assert package["transcript"]["utterances"]
    assert package["transcript"]["sha256"].startswith("sha256:")
    assert package["transcript"]["integrity"] == "ok"
    assert package["score_records"]
    assert all(row["attempt_id"] == attempt_id for row in package["score_records"])
    assert package["review_actions"]
    assert package["audit_events"]
    assert package["integrity"]["media_url"]
    assert package["integrity"]["evidence"]["checked"] > 0
    assert package["integrity"]["evidence"]["passed"] is True
    assert package["attempt"]["media_sha256"].startswith("sha256:")

    export_event = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'attempt.export' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert export_event is not None
    detail = json.loads(export_event["after"])
    assert detail["package_hash"] == result["package_hash"]
    assert detail["records"] == len(package["score_records"])


def test_package_records_media_retention_state(conn):
    attempt_id = conn.execute("SELECT attempt_id FROM attempts ORDER BY attempt_id LIMIT 1").fetchone()[
        "attempt_id"
    ]
    conn.execute(
        "UPDATE attempts SET media_purged_at = ?, media_purged_by = ? WHERE attempt_id = ?",
        (db.now_iso(), "留存任务", attempt_id),
    )
    conn.commit()
    result = build_attempt_package(conn, attempt_id, exported_by="审计员")
    assert result["package"]["integrity"]["media_purged_at"]
    assert result["package"]["attempt"]["media_url"]


def test_package_missing_attempt_is_rejected(conn):
    with pytest.raises(ValueError):
        build_attempt_package(conn, "att_missing", exported_by="审计员")
