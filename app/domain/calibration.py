"""本地金标准校准：导入人工金标准并计算 QWK、逐项一致率与置信下限建议。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from typing import Any

from .. import db
from .stats import quadratic_weighted_kappa


def parse_gold_file(filename: str, content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("金标准文件必须是 UTF-8 编码") from exc
    stripped = text.lstrip()
    if filename.lower().endswith(".json") or stripped.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败：{exc}") from exc
        if isinstance(data, dict):
            data = data.get("scores") or data.get("records") or data.get("items")
        if not isinstance(data, list) or not data:
            raise ValueError("JSON 金标准必须是非空数组")
        return [row for row in data if isinstance(row, dict)]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    required = {"attempt_id", "item_id", "gold_score"}
    missing = required - {name.strip() for name in reader.fieldnames}
    if missing:
        raise ValueError(f"CSV 缺少列：{', '.join(sorted(missing))}")
    return [dict(row) for row in reader]


def import_gold_scores(
    conn,
    rows: list[dict[str, Any]],
    *,
    actor: str,
    filename: str,
    file_hash: str,
) -> dict[str, Any]:
    updated = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        attempt_id = str(row.get("attempt_id") or "").strip()
        item_id = str(row.get("item_id") or "").strip()
        raw_score = row.get("gold_score")
        if not attempt_id or not item_id:
            errors.append({"row": index, "error": "attempt_id 或 item_id 为空"})
            continue
        try:
            gold_score = int(raw_score)
        except (TypeError, ValueError):
            errors.append({"row": index, "error": f"gold_score 不是整数：{raw_score!r}"})
            continue
        if gold_score not in (0, 1, 2):
            errors.append({"row": index, "error": "gold_score 必须是 0/1/2"})
            continue
        key = (attempt_id, item_id)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        record = conn.execute(
            "SELECT record_id FROM score_records WHERE attempt_id = ? AND item_id = ?",
            (attempt_id, item_id),
        ).fetchone()
        if not record:
            errors.append({"row": index, "error": f"场次或评分项不存在：{attempt_id}/{item_id}"})
            continue
        conn.execute(
            "UPDATE score_records SET gold_score = ? WHERE record_id = ?",
            (gold_score, record["record_id"]),
        )
        updated += 1
    conn.commit()
    if updated == 0:
        raise ValueError("没有可导入的有效金标准记录")
    db.append_audit(
        conn,
        actor=actor,
        action="calibration.gold_import",
        object_type="CalibrationBatch",
        object_id=file_hash[:24],
        before=None,
        after={
            "filename": filename,
            "file_hash": file_hash,
            "updated": updated,
            "skipped": skipped,
            "errors": errors[:10],
        },
    )
    return {
        "filename": filename,
        "file_hash": file_hash,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }


def _confidence_sweep(
    pairs: list[tuple[int, int, float | None]],
    floors: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9),
) -> tuple[list[dict[str, Any]], float | None]:
    total = len(pairs)
    bands: list[dict[str, Any]] = []
    recommended = None
    for floor in floors:
        subset = [pair for pair in pairs if pair[2] is not None and pair[2] >= floor]
        if not subset:
            bands.append({"floor": floor, "n": 0, "accuracy": None, "coverage": 0.0})
            continue
        accuracy = round(sum(1 for ai, gold, _ in subset if ai == gold) / len(subset), 4)
        coverage = round(len(subset) / total, 4) if total else 0.0
        bands.append({"floor": floor, "n": len(subset), "accuracy": accuracy, "coverage": coverage})
        if recommended is None and len(subset) >= 10 and accuracy >= 0.9:
            recommended = floor
    return bands, recommended


def calibration_report(conn) -> dict[str, Any]:
    records = db.rows_to_dicts(
        conn.execute("SELECT * FROM score_records ORDER BY attempt_id, item_id").fetchall()
    )
    item_titles: dict[str, str] = {}
    for row in conn.execute("SELECT payload FROM bundles").fetchall():
        for item in json.loads(row["payload"]).get("rubric", []):
            item_titles[item["item_id"]] = item.get("title") or item["item_id"]

    ai_pairs: list[tuple[int, int]] = []
    human_pairs: list[tuple[int, int]] = []
    confidence_pairs: list[tuple[int, int, float | None]] = []
    per_item: dict[str, list[tuple[int, int]]] = defaultdict(list)
    confusion = [[0, 0, 0] for _ in range(3)]
    gold_total = 0
    for record in records:
        if record["gold_score"] is None:
            continue
        gold_total += 1
        gold = int(record["gold_score"])
        if record["ai_score"] is not None:
            ai = int(record["ai_score"])
            ai_pairs.append((ai, gold))
            confidence_pairs.append((ai, gold, record["ai_confidence"]))
            per_item[record["item_id"]].append((ai, gold))
            confusion[ai][gold] += 1
        if record["final_score"] is not None:
            human_pairs.append((int(record["final_score"]), gold))

    item_rows: list[dict[str, Any]] = []
    for item_id, pairs in per_item.items():
        exact = sum(1 for ai, gold in pairs if ai == gold)
        mae = sum(abs(ai - gold) for ai, gold in pairs) / len(pairs)
        item_rows.append(
            {
                "item_id": item_id,
                "item_title": item_titles.get(item_id, item_id),
                "n": len(pairs),
                "exact_agreement": round(exact / len(pairs), 4),
                "mae": round(mae, 3),
                "qwk": quadratic_weighted_kappa([p[0] for p in pairs], [p[1] for p in pairs]),
            }
        )
    item_rows.sort(key=lambda row: (row["exact_agreement"], -row["n"]))

    bands, recommended = _confidence_sweep(confidence_pairs)
    ai_total = sum(1 for record in records if record["ai_score"] is not None)
    return {
        "gold_count": gold_total,
        "ai_count": ai_total,
        "ai_gold_count": len(ai_pairs),
        "coverage": round(gold_total / len(records), 4) if records else 0.0,
        "qwk_ai": quadratic_weighted_kappa([p[0] for p in ai_pairs], [p[1] for p in ai_pairs]),
        "qwk_human": quadratic_weighted_kappa([p[0] for p in human_pairs], [p[1] for p in human_pairs]),
        "confusion_matrix": confusion,
        "items": item_rows,
        "confidence_bands": bands,
        "recommended_confidence_floor": recommended,
        "rules": {
            "csv_columns": ["attempt_id", "item_id", "gold_score"],
            "score_scale": "0/1/2",
            "qwk_target": 0.7,
            "confidence_rule": "样本 ≥ 10 且逐项完全一致率 ≥ 90% 的最低置信下限",
        },
    }
