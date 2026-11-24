"""站级心理测量：难度、区分度、α 与地板/天花板标记。"""
from __future__ import annotations

from app import db
from app.domain.bundle import freeze_bundle
from app.domain.psychometrics import station_psychometrics
from app.seed import build_bundle_payload


def test_station_psychometrics_flags_ceiling_floor_and_computes_alpha(tmp_path):
    conn = db.connect(tmp_path / "psycho.db")
    db.init_db(conn)
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")
    item_ids = [item["item_id"] for item in bundle["payload"]["rubric"]]

    for index in range(6):
        attempt_id = f"att_p{index}"
        conn.execute(
            "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
            "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (attempt_id, bundle["bundle_id"], f"C{index}", "考生", "SES", "L2", "accepted", db.now_iso()),
        )
        high = index < 3
        for item_id in item_ids:
            if item_id == item_ids[0]:
                score = 2
            elif item_id == item_ids[1]:
                score = 0
            else:
                score = 2 if high else 0
            conn.execute(
                "INSERT INTO score_records(record_id, attempt_id, item_id, final_score, routing, status, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"rec_{attempt_id}_{item_id}", attempt_id, item_id, score, "{}", "accepted", db.now_iso()),
            )
    conn.commit()

    stats = station_psychometrics(conn)
    assert len(stats) == 1
    station = stats[0]
    assert station["scored_attempts"] == 6
    assert station["mean_total"] is not None
    assert station["alpha"] is not None
    assert "ceiling" in station["flags"]
    assert "floor" in station["flags"]
    assert item_ids[0] in station["ceiling_items"]
    assert item_ids[1] in station["floor_items"]
    items = {item["item_id"]: item for item in station["items"]}
    assert items[item_ids[0]]["difficulty"] == 1.0
    assert items[item_ids[1]]["difficulty"] == 0.0
    assert items[item_ids[2]]["difficulty"] == 0.5
    assert items[item_ids[2]]["discrimination"] == 1.0
    assert items[item_ids[0]]["discrimination"] is None
    conn.close()


def test_inter_item_correlation_flags_redundant_pairs(tmp_path):
    conn = db.connect(tmp_path / "psycho_corr.db")
    db.init_db(conn)
    bundle = freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")
    item_ids = [item["item_id"] for item in bundle["payload"]["rubric"]]
    pair_items = (item_ids[0], item_ids[1])
    pattern = [0, 1, 2, 0, 1]

    for index in range(5):
        attempt_id = f"att_c{index}"
        conn.execute(
            "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, "
            "risk_level, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (attempt_id, bundle["bundle_id"], f"C{index}", "考生", "SES", "L2", "accepted", db.now_iso()),
        )
        for offset, item_id in enumerate(item_ids):
            score = pattern[index] if item_id in pair_items else (index + offset) % 3
            conn.execute(
                "INSERT INTO score_records(record_id, attempt_id, item_id, final_score, routing, status, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (f"rec_{attempt_id}_{item_id}", attempt_id, item_id, score, "{}", "accepted", db.now_iso()),
            )
    conn.commit()

    station = station_psychometrics(conn)[0]
    pair = next(
        entry
        for entry in station["inter_item"]
        if {entry["item_a"], entry["item_b"]} == set(pair_items)
    )
    assert pair["r"] == 1.0
    assert pair["n"] == 5
    assert any(
        {entry["item_a"], entry["item_b"]} == set(pair_items)
        for entry in station["redundant_pairs"]
    )
    assert "redundant_items" in station["flags"]
    conn.close()


def test_station_psychometrics_marks_insufficient_sample(tmp_path):
    conn = db.connect(tmp_path / "psycho_empty.db")
    db.init_db(conn)
    freeze_bundle(conn, build_bundle_payload(), actor="test", version="1.0")
    stats = station_psychometrics(conn)
    assert len(stats) == 1
    station = stats[0]
    assert station["scored_attempts"] == 0
    assert station["mean_total"] is None
    assert station["alpha"] is None
    assert "sample_insufficient" in station["flags"]
    conn.close()
