"""站包去重初筛：跨考站文本相似度提示。"""
from __future__ import annotations

import copy

from app import db
from app.domain.bundle import find_similar_bundles, freeze_bundle
from app.seed import build_bundle_payload


def test_similar_bundles_flags_duplicate_content_across_stations(tmp_path):
    conn = db.connect(tmp_path / "similarity.db")
    db.init_db(conn)

    first = build_bundle_payload()
    first["station_id"] = "ST-A"
    freeze_bundle(conn, first, actor="test", version="1.0")

    assert find_similar_bundles(conn, first) == []

    second = copy.deepcopy(first)
    second["station_id"] = "ST-B"
    matches = find_similar_bundles(conn, second)
    assert matches
    assert matches[0]["station_id"] == "ST-A"
    assert matches[0]["similarity"] >= 0.99

    unrelated = {"station_id": "ST-C", "title": "术后伤口换药", "candidate_instructions": ["请完成换药"], "rubric": []}
    assert find_similar_bundles(conn, unrelated) == []
    conn.close()
