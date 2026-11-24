"""S5 证据链缺口：现场补充记录校验、审计与签字前置条件。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.integrity import (
    integrity_supplement_missing,
    record_integrity_supplement,
)


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "integrity_supplement.db")
    db.init_db(connection)
    for attempt_id, gap in (("att_gap", 1), ("att_ok", 0)):
        connection.execute(
            "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, "
            "session_id, risk_level, integrity_gap, integrity_note, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                "bnd",
                "C001",
                "考生",
                "SES",
                "L2",
                gap,
                "设备 2 音频中断 8 秒" if gap else None,
                db.now_iso(),
            ),
        )
    connection.commit()
    yield connection
    connection.close()


def test_supplement_requires_gap_and_minimum_length(conn):
    assert integrity_supplement_missing(dict(conn.execute(
        "SELECT * FROM attempts WHERE attempt_id = 'att_gap'"
    ).fetchone())) is True
    assert integrity_supplement_missing(dict(conn.execute(
        "SELECT * FROM attempts WHERE attempt_id = 'att_ok'"
    ).fetchone())) is False

    with pytest.raises(ValueError, match="未标记证据链缺口"):
        record_integrity_supplement(conn, "att_ok", note="设备异常已现场核验", actor="考务 李萍")
    with pytest.raises(ValueError, match="不少于 10 字"):
        record_integrity_supplement(conn, "att_gap", note="设备中断", actor="考务 李萍")


def test_supplement_is_saved_and_audited(conn):
    result = record_integrity_supplement(
        conn,
        "att_gap",
        note="设备 2 音频中断 8 秒，监考现场确认考生未受影响，已书面记录。",
        actor="考务 李萍",
    )
    assert result["actor"] == "考务 李萍"
    row = conn.execute(
        "SELECT * FROM attempts WHERE attempt_id = 'att_gap'"
    ).fetchone()
    assert row["integrity_supplement"].startswith("设备 2 音频中断")
    assert row["integrity_supplement_by"] == "考务 李萍"
    assert row["integrity_supplement_at"]
    assert integrity_supplement_missing(dict(row)) is False

    audit = conn.execute(
        "SELECT * FROM audit_events WHERE action = 'attempt.integrity_supplement'"
    ).fetchone()
    assert audit is not None
    assert audit["object_id"] == "att_gap"

    second = record_integrity_supplement(
        conn, "att_gap", note="补充：已核对备份录音并归档。", actor="考务 李萍"
    )
    assert second["note"].startswith("补充：")
    assert conn.execute(
        "SELECT integrity_supplement FROM attempts WHERE attempt_id = 'att_gap'"
    ).fetchone()["integrity_supplement"].startswith("补充：")
