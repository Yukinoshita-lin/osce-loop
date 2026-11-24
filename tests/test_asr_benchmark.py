"""ASR 医学专名词错率：指标计算、留痕与原文不落库。"""
from __future__ import annotations

from app import db
from app.domain.asr_benchmark import (
    benchmark_summary,
    evaluate_asr,
    list_benchmarks,
    record_benchmark,
)
from app.seed import seed_if_empty


def test_evaluate_asr_exact_and_medical_terms():
    reference = "患者胸痛向肩部放射，建议做心电图。"
    metrics = evaluate_asr(reference, reference, ["胸痛", "肩部", "心电图"])
    assert metrics["error_rate"] == 0
    assert metrics["status"] == "pass"
    assert metrics["medical_term_error_rate"] == 0
    assert metrics["medical_term_recall"] == 1
    assert metrics["missing_terms"] == []


def test_evaluate_asr_missing_medical_term():
    reference = "患者胸痛向肩部放射，建议做心电图。"
    hypothesis = "患者胸痛向肩部放射，建议做心脏彩超。"
    metrics = evaluate_asr(reference, hypothesis, ["胸痛", "肩部", "心电图"])
    assert metrics["medical_term_error_rate"] == 0.3333
    assert metrics["missing_terms"] == ["心电图"]
    assert metrics["status"] == "fail"


def test_record_benchmark_persists_hash_not_text(tmp_path):
    conn = db.connect(tmp_path / "asr_benchmark.db")
    db.init_db(conn)
    seed_if_empty(conn)
    reference = "患者胸痛向肩部放射。"
    hypothesis = "患者胸痛向肩部放射。"
    record = record_benchmark(
        conn,
        label="胸痛样本",
        reference=reference,
        hypothesis=hypothesis,
        terms=["胸痛", "肩部"],
        actor="评测员",
    )
    row = conn.execute(
        "SELECT * FROM asr_benchmarks WHERE benchmark_id = ?", (record["benchmark_id"],)
    ).fetchone()
    assert row["reference_chars"] == len(reference)
    assert reference not in row["metrics"]
    assert record["metrics"]["status"] == "pass"
    assert record["reference_hash"].startswith("sha256:")

    summary = benchmark_summary(conn)
    assert summary["count"] == 1
    assert summary["latest"]["label"] == "胸痛样本"
    assert summary["status"] == "pass"
    assert len(list_benchmarks(conn)) == 1
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'asr.benchmark'"
    ).fetchone()["n"]
    assert audit == 1
    conn.close()
