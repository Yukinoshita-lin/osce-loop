"""ASR 医学专名词错率：用参考文本与 ASR 输出计算错误率并留痕。

中文按单字、英文按词切分；医学术语按连续 token 序列匹配。只存哈希与指标，
不把参考文本/ASR 原文写入数据库，避免评测样本意外携带真实考生信息。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .. import db

ERROR_RATE_LIMIT = 0.08
MAX_BENCHMARK_CHARS = 4000
TOKEN_RE = re.compile(r"[a-z]+|\d+|[\u4e00-\u9fff]", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _distance(reference: list[str], hypothesis: list[str]) -> int:
    """两行滚动 Levenshtein，避免长文本的二维矩阵内存开销。"""
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for i, ref_token in enumerate(reference, 1):
        current = [i]
        for j, hyp_token in enumerate(hypothesis, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ref_token != hyp_token),
                )
            )
        previous = current
    return previous[-1]


def _contains(sequence: list[str], needle: list[str]) -> bool:
    if not needle:
        return False
    size = len(needle)
    return any(
        sequence[index : index + size] == needle
        for index in range(len(sequence) - size + 1)
    )


def evaluate_asr(reference: str, hypothesis: str, terms: list[str] | None = None) -> dict[str, Any]:
    reference_tokens = _tokens(reference)
    hypothesis_tokens = _tokens(hypothesis)
    if not reference_tokens:
        raise ValueError("参考文本没有可比较的中文/英文/数字 token")
    if not hypothesis_tokens:
        raise ValueError("ASR 输出没有可比较的中文/英文/数字 token")
    distance = _distance(reference_tokens, hypothesis_tokens)
    token_error_rate = round(distance / len(reference_tokens), 4)

    term_rows: list[dict[str, Any]] = []
    missing_terms: list[str] = []
    present_terms = 0
    for term in terms or []:
        label = str(term).strip()
        needle = _tokens(label)
        if not label or not needle or not _contains(reference_tokens, needle):
            continue
        present_terms += 1
        hit = _contains(hypothesis_tokens, needle)
        if not hit:
            missing_terms.append(label)
        term_rows.append({"term": label, "present_in_reference": True, "recognized": hit})

    medical_error_rate = (
        round(len(missing_terms) / present_terms, 4) if present_terms else None
    )
    error_rate = (
        medical_error_rate if medical_error_rate is not None else token_error_rate
    )
    metric = (
        "medical_term_error_rate"
        if medical_error_rate is not None
        else "token_error_rate"
    )
    status = "pass" if error_rate <= ERROR_RATE_LIMIT else "fail"
    return {
        "metric": metric,
        "error_rate": error_rate,
        "threshold": ERROR_RATE_LIMIT,
        "status": status,
        "token_error_rate": token_error_rate,
        "medical_term_error_rate": medical_error_rate,
        "medical_term_recall": (
            round(1 - medical_error_rate, 4) if medical_error_rate is not None else None
        ),
        "reference_tokens": len(reference_tokens),
        "hypothesis_tokens": len(hypothesis_tokens),
        "edit_distance": distance,
        "term_count": present_terms,
        "missing_terms": missing_terms,
        "terms": term_rows,
        "note": (
            "按医学专名召回计算；术语均未出现在参考文本时退回整体 token 错误率。"
            if not present_terms
            else "医学术语以独立词或连续 token 出现在 ASR 输出中即视为识别成功。"
        ),
    }


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_benchmark(
    conn,
    *,
    label: str,
    reference: str,
    hypothesis: str,
    terms: list[str] | None,
    actor: str = "ASR 评测员",
) -> dict[str, Any]:
    if len(reference) > MAX_BENCHMARK_CHARS or len(hypothesis) > MAX_BENCHMARK_CHARS:
        raise ValueError(f"参考文本与 ASR 输出均不得超过 {MAX_BENCHMARK_CHARS} 字符")
    metrics = evaluate_asr(reference, hypothesis, terms or [])
    benchmark_id = db.new_id("asrb")
    timestamp = db.now_iso()
    clean_terms = [str(term).strip() for term in (terms or []) if str(term).strip()]
    conn.execute(
        "INSERT INTO asr_benchmarks(benchmark_id, label, reference_hash, hypothesis_hash, "
        "reference_chars, hypothesis_chars, terms, metrics, actor, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            benchmark_id,
            label.strip() or "ASR 样本",
            _hash_text(reference),
            _hash_text(hypothesis),
            len(reference),
            len(hypothesis),
            json.dumps(clean_terms, ensure_ascii=False),
            json.dumps(metrics, ensure_ascii=False),
            actor,
            timestamp,
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="asr.benchmark",
        object_type="AsrBenchmark",
        object_id=benchmark_id,
        before=None,
        after={
            "label": label.strip() or "ASR 样本",
            "metric": metrics["metric"],
            "error_rate": metrics["error_rate"],
            "status": metrics["status"],
            "reference_hash": _hash_text(reference),
            "hypothesis_hash": _hash_text(hypothesis),
            "missing_terms": metrics["missing_terms"],
        },
    )
    return {
        "benchmark_id": benchmark_id,
        "label": label.strip() or "ASR 样本",
        "reference_hash": _hash_text(reference),
        "hypothesis_hash": _hash_text(hypothesis),
        "reference_chars": len(reference),
        "hypothesis_chars": len(hypothesis),
        "terms": clean_terms,
        "metrics": metrics,
        "actor": actor,
        "created_at": timestamp,
    }


def _row_to_benchmark(row) -> dict[str, Any]:
    return {
        "benchmark_id": row["benchmark_id"],
        "label": row["label"],
        "reference_hash": row["reference_hash"],
        "hypothesis_hash": row["hypothesis_hash"],
        "reference_chars": row["reference_chars"],
        "hypothesis_chars": row["hypothesis_chars"],
        "terms": json.loads(row["terms"] or "[]"),
        "metrics": json.loads(row["metrics"]),
        "actor": row["actor"],
        "created_at": row["created_at"],
    }


def list_benchmarks(conn, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM asr_benchmarks ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [_row_to_benchmark(row) for row in rows]


def benchmark_summary(conn) -> dict[str, Any]:
    rows = list_benchmarks(conn, limit=5)
    total = conn.execute("SELECT COUNT(*) AS n FROM asr_benchmarks").fetchone()["n"]
    if not rows:
        return {
            "count": 0,
            "latest": None,
            "recent": [],
            "threshold": ERROR_RATE_LIMIT,
            "status": "unmeasured",
        }
    return {
        "count": total,
        "latest": rows[0],
        "recent": rows,
        "threshold": ERROR_RATE_LIMIT,
        "status": rows[0]["metrics"]["status"],
    }
