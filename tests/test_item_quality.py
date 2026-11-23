"""项级可判性：编辑率、金标准一致率与改写标记。"""
from __future__ import annotations

import shutil

import pytest

from app import db
from app.domain.item_quality import item_quality_report
from app.seed import seed_if_empty


@pytest.fixture(scope="module")
def seeded_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("item_quality") / "seed.db"
    conn = db.connect(path)
    db.init_db(conn)
    seed_if_empty(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(seeded_path, tmp_path):
    path = tmp_path / "item_quality.db"
    shutil.copy(seeded_path, path)
    connection = db.connect(path)
    yield connection
    connection.close()


def test_item_quality_covers_full_rubric(conn):
    bundle_id = conn.execute("SELECT bundle_id FROM bundles LIMIT 1").fetchone()["bundle_id"]
    rows = item_quality_report(conn, bundle_id)
    assert len(rows) == 15
    assert sum(row["records"] for row in rows) > 0
    assert sum(row["reviewed"] for row in rows) > 0
    assert all(row["exact_match_rate"] is None or 0 <= row["exact_match_rate"] <= 1 for row in rows)
    assert all(row["edit_rate"] is None or 0 <= row["edit_rate"] <= 1 for row in rows)
    assert all("needs_review" in row for row in rows)


def test_item_edit_rate_matches_review_actions(conn):
    bundle_id = conn.execute("SELECT bundle_id FROM bundles LIMIT 1").fetchone()["bundle_id"]
    rows = item_quality_report(conn, bundle_id)
    edited_rows = [row for row in rows if row["edited"] > 0]
    assert edited_rows
    for row in edited_rows:
        assert row["edit_rate"] == round(row["edited"] / row["reviewed"], 3)
        expected = (row["reviewed"] >= 3 and row["edit_rate"] >= 0.3) or (
            row["gold_records"] >= 3
            and row["exact_match_rate"] is not None
            and row["exact_match_rate"] <= 0.6
        )
        assert row["needs_review"] is expected


def test_item_quality_unknown_bundle_is_empty(conn):
    assert item_quality_report(conn, "bnd_missing") == []
