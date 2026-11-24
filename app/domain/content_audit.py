"""内容审计抽样：按场次扫描 SP 越界话术与高风险表述。"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .. import db

RULES: list[dict[str, Any]] = [
    {
        "key": "sp_diagnosis",
        "role": "patient",
        "severity": "high",
        "label": "SP 给出诊断或病情结论",
        "patterns": (r"你得了", r"确诊为", r"应该是.{0,6}病", r"就是.{0,4}病"),
    },
    {
        "key": "sp_prescription",
        "role": "patient",
        "severity": "high",
        "label": "SP 给出用药或处置建议",
        "patterns": (r"你吃", r"吃点", r"应该吃", r"可以吃", r"含服", r"打一针", r"剂量", r"不用去医院"),
    },
    {
        "key": "sp_evaluation",
        "role": "patient",
        "severity": "medium",
        "label": "SP 评价考生表现",
        "patterns": (r"你不行", r"不合格", r"太差", r"水平不行", r"不会看病"),
    },
    {
        "key": "candidate_false_reassurance",
        "role": "candidate",
        "severity": "medium",
        "label": "考生给出无依据保证",
        "patterns": (r"肯定没事", r"保证没事", r"不用检查", r"不用来医院"),
    },
]
_COMPILED = [
    {
        "key": rule["key"],
        "role": rule["role"],
        "severity": rule["severity"],
        "label": rule["label"],
        "regex": [re.compile(pattern) for pattern in rule["patterns"]],
    }
    for rule in RULES
]


def audit_utterances(utterances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for rule in _COMPILED:
        for utt in utterances:
            if utt.get("speaker") != rule["role"]:
                continue
            text = str(utt.get("text") or "")
            if not text:
                continue
            for regex in rule["regex"]:
                match = regex.search(text)
                if not match:
                    continue
                findings.append(
                    {
                        "utt_id": utt.get("utt_id"),
                        "role": rule["role"],
                        "severity": rule["severity"],
                        "rule": rule["key"],
                        "label": rule["label"],
                        "match": match.group(0),
                        "quote": text,
                        "start_ms": utt.get("start_ms"),
                        "end_ms": utt.get("end_ms"),
                    }
                )
                break
    findings.sort(key=lambda entry: (entry.get("start_ms") or 0, entry.get("rule") or ""))
    return findings


def _sampled(attempt_id: str, sample_rate: float) -> bool:
    if sample_rate >= 1:
        return True
    bucket = int(hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:8], 16) % 1000
    return bucket < int(sample_rate * 1000)


def run_content_audit(
    conn,
    *,
    sample_rate: float = 0.2,
    limit: int = 20,
    attempt_id: str | None = None,
    actor: str = "审计员",
) -> dict[str, Any]:
    """按固定比例抽取场次并扫描转写；抽样结果可复现，不随机漂移。"""
    if attempt_id:
        rows = conn.execute(
            "SELECT attempt_id, candidate_code, bundle_id FROM attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchall()
        if not rows:
            raise ValueError("场次不存在")
    else:
        rows = conn.execute(
            "SELECT attempt_id, candidate_code, bundle_id FROM attempts ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    candidates = [dict(row) for row in rows]
    if attempt_id is None:
        candidates = [row for row in candidates if _sampled(row["attempt_id"], sample_rate)]
    candidates = candidates[: max(1, min(limit, 200))]

    findings: list[dict[str, Any]] = []
    scanned: list[str] = []
    for candidate in candidates:
        transcript = conn.execute(
            "SELECT payload FROM transcripts WHERE attempt_id = ?", (candidate["attempt_id"],)
        ).fetchone()
        if not transcript:
            continue
        try:
            data = json.loads(transcript["payload"])
        except (TypeError, ValueError):
            continue
        utterances = data.get("utterances") if isinstance(data, dict) else data
        if not isinstance(utterances, list):
            continue
        scanned.append(candidate["attempt_id"])
        for finding in audit_utterances(utterances):
            findings.append(
                {
                    **finding,
                    "attempt_id": candidate["attempt_id"],
                    "candidate_code": candidate["candidate_code"],
                }
            )

    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for finding in findings:
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1
        by_rule[finding["rule"]] = by_rule.get(finding["rule"], 0) + 1
    attempts_with_findings = len({finding["attempt_id"] for finding in findings})
    result = {
        "sample_rate": sample_rate,
        "attempts_considered": len(rows),
        "attempts_scanned": len(scanned),
        "attempts_with_findings": attempts_with_findings,
        "findings": findings[:100],
        "finding_count": len(findings),
        "by_severity": by_severity,
        "by_rule": by_rule,
        "scanned_attempts": scanned,
        "note": (
            "内容审计为抽样线索，不替代人工判断；高风险命中应作为 P0 安全事件复核，"
            "并回写红队用例与禁用话术清单。"
        ),
        "generated_at": db.now_iso(),
    }
    db.append_audit(
        conn,
        actor=actor,
        action="content.audit_run",
        object_type="AuditSample",
        object_id=attempt_id or "recent_attempts",
        after={
            "sample_rate": sample_rate,
            "attempts_scanned": len(scanned),
            "findings": len(findings),
            "by_severity": by_severity,
        },
    )
    return result
