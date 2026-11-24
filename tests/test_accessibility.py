"""反馈可及性：学生自评、去重与公平性报告接入。"""
from __future__ import annotations

import pytest

from app import db
from app.domain.accessibility import (
    feedback_accessibility,
    submit_feedback_survey,
    survey_for_attempt,
)
from app.domain.fairness import fairness_report
from app.seed import seed_if_empty


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "accessibility.db")
    db.init_db(connection)
    seed_if_empty(connection)
    yield connection
    connection.close()


def _attempts(conn, limit: int) -> list[str]:
    return [
        row["attempt_id"]
        for row in conn.execute(
            "SELECT attempt_id FROM attempts ORDER BY attempt_id LIMIT ?", (limit,)
        ).fetchall()
    ]


def test_feedback_accessibility_starts_unmeasured(conn):
    summary = feedback_accessibility(conn)
    assert summary["status"] == "unmeasured"
    assert summary["count"] == 0
    assert summary["helpful_rate"] is None


def test_survey_submission_dedup_and_threshold(conn):
    attempts = _attempts(conn, 5)
    first = submit_feedback_survey(
        conn, attempt_id=attempts[0], rating=5, participant_label="练习学生"
    )
    assert first["rating"] == 5
    assert survey_for_attempt(conn, attempts[0])["rating"] == 5

    with pytest.raises(ValueError, match="已提交"):
        submit_feedback_survey(conn, attempt_id=attempts[0], rating=4)
    with pytest.raises(ValueError, match="1–5"):
        submit_feedback_survey(conn, attempt_id=attempts[1], rating=6)
    with pytest.raises(ValueError, match="不存在"):
        submit_feedback_survey(conn, attempt_id="att_missing", rating=5)

    for attempt_id, rating in zip(attempts[1:], (5, 4, 4, 2)):
        submit_feedback_survey(conn, attempt_id=attempt_id, rating=rating)

    summary = feedback_accessibility(conn)
    assert summary["count"] == 5
    assert summary["helpful_count"] == 4
    assert summary["helpful_rate"] == 0.8
    assert summary["status"] == "pass"
    assert fairness_report(conn)["feedback_accessibility"]["status"] == "pass"
