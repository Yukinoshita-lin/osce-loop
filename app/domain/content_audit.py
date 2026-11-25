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


def scan_attempt(conn, attempt_id: str, *, actor: str = "系统") -> dict[str, Any]:
    """每场次入库后自动扫描转写并把线索落库；重复扫描按唯一键幂等。"""
    row = conn.execute(
        "SELECT payload FROM transcripts WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    if not row:
        return {"attempt_id": attempt_id, "finding_count": 0, "new_findings": 0, "high": 0, "findings": []}
    try:
        data = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {"attempt_id": attempt_id, "finding_count": 0, "new_findings": 0, "high": 0, "findings": []}
    utterances = data.get("utterances") if isinstance(data, dict) else data
    if not isinstance(utterances, list):
        return {"attempt_id": attempt_id, "finding_count": 0, "new_findings": 0, "high": 0, "findings": []}
    findings = audit_utterances(utterances)
    timestamp = db.now_iso()
    inserted = 0
    for finding in findings:
        start = finding.get("start_ms")
        start_ms = int(start) if isinstance(start, (int, float)) else -1
        cursor = conn.execute(
            "INSERT OR IGNORE INTO content_audit_findings(finding_id, attempt_id, rule, severity, role, "
            "label, quote, start_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                db.new_id("caf"),
                attempt_id,
                finding["rule"],
                finding["severity"],
                finding["role"],
                finding["label"],
                finding.get("quote") or "",
                start_ms,
                timestamp,
            ),
        )
        if cursor.rowcount and cursor.rowcount > 0:
            inserted += 1
    conn.commit()
    high = sum(1 for finding in findings if finding["severity"] == "high")
    if inserted:
        db.append_audit(
            conn,
            actor=actor,
            action="content.audit_findings",
            object_type="StationAttempt",
            object_id=attempt_id,
            before=None,
            after={
                "new_findings": inserted,
                "high": high,
                "rules": sorted({finding["rule"] for finding in findings}),
            },
        )
    return {
        "attempt_id": attempt_id,
        "finding_count": len(findings),
        "new_findings": inserted,
        "high": high,
        "findings": findings[:20],
    }


def attempt_audit_summary(conn, attempt_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM content_audit_findings WHERE attempt_id = ? ORDER BY start_ms, rowid",
        (attempt_id,),
    ).fetchall()
    findings = [dict(row) for row in rows]
    return {
        "attempt_id": attempt_id,
        "finding_count": len(findings),
        "high": sum(1 for finding in findings if finding["severity"] == "high"),
        "findings": findings[:20],
    }


def content_audit_summary(conn, *, recent_limit: int = 20) -> dict[str, Any]:
    rows = db.rows_to_dicts(
        conn.execute(
            "SELECT f.*, a.candidate_code FROM content_audit_findings f "
            "LEFT JOIN attempts a ON a.attempt_id = f.attempt_id "
            "ORDER BY f.created_at DESC, f.rowid DESC"
        ).fetchall()
    )
    open_rows = [row for row in rows if (row.get("status") or "open") == "open"]
    by_rule: dict[str, dict[str, Any]] = {}
    for row in open_rows:
        bucket = by_rule.setdefault(
            row["rule"],
            {"rule": row["rule"], "label": row["label"], "severity": row["severity"], "count": 0},
        )
        bucket["count"] += 1
    return {
        "finding_count": len(rows),
        "open_count": len(open_rows),
        "high_count": sum(1 for row in open_rows if row["severity"] == "high"),
        "medium_count": sum(1 for row in open_rows if row["severity"] == "medium"),
        "confirmed_count": sum(1 for row in rows if row.get("status") == "confirmed"),
        "confirmed_high_count": sum(
            1 for row in rows if row.get("status") == "confirmed" and row["severity"] == "high"
        ),
        "confirmed_medium_count": sum(
            1 for row in rows if row.get("status") == "confirmed" and row["severity"] == "medium"
        ),
        "dismissed_count": sum(1 for row in rows if row.get("status") == "dismissed"),
        "by_rule": sorted(by_rule.values(), key=lambda item: (-item["count"], item["rule"])),
        "recent": rows[: max(1, min(recent_limit, 100))],
        "last_at": rows[0]["created_at"] if rows else None,
        "note": (
            "自动内容审计在每场次入库后运行；高风险线索应逐条复核并记录结论，"
            "确认越界后回写红队用例，未处置线索持续计入告警。"
        ),
    }


FINDING_DECISIONS = {"confirmed": "确认越界", "dismissed": "误报关闭"}


def review_finding(
    conn,
    finding_id: str,
    *,
    decision: str,
    note: str,
    actor: str,
) -> dict[str, Any]:
    """处置内容审计线索：确认越界或判定误报，必须留理由并写入审计链。"""
    if decision not in FINDING_DECISIONS:
        raise ValueError(f"decision 必须是 {', '.join(FINDING_DECISIONS)}")
    if not str(note or "").strip():
        raise ValueError("处置理由不能为空")
    row = conn.execute(
        "SELECT * FROM content_audit_findings WHERE finding_id = ?", (finding_id,)
    ).fetchone()
    if not row:
        raise ValueError("内容审计线索不存在")
    if (row["status"] or "open") != "open":
        raise ValueError("该线索已处置，不能重复复核")
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE content_audit_findings SET status = ?, reviewed_by = ?, reviewed_at = ?, "
        "review_note = ? WHERE finding_id = ?",
        (decision, actor, timestamp, note.strip(), finding_id),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=actor,
        action="content.finding_reviewed",
        object_type="ContentAuditFinding",
        object_id=finding_id,
        before={"status": "open"},
        after={
            "status": decision,
            "decision_label": FINDING_DECISIONS[decision],
            "note": note.strip(),
            "attempt_id": row["attempt_id"],
            "rule": row["rule"],
            "severity": row["severity"],
        },
    )
    updated = conn.execute(
        "SELECT * FROM content_audit_findings WHERE finding_id = ?", (finding_id,)
    ).fetchone()
    return dict(updated)


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
