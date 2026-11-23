"""FastAPI 接口：复核工作台、反馈、质量闸门与 SP 演练。"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .ai.base import CapabilityGateway
from .ai.mock import MockScoringProvider
from .config import (
    MEDIA_DIR,
    MODEL_VERSION,
    PROMPT_VERSION,
    REASON_LABELS,
    ROUTE_LABELS,
    SP_MODEL_VERSION,
    SP_PROMPT_VERSION,
    WEB_DIR,
)
from .domain.bundle import (
    apply_authority_reset,
    effective_authority,
    get_bundle,
    list_bundles,
    list_overrides,
)
from .domain.gates import evaluate_gates
from .domain.practice import build_practice_report
from .domain.rubric import AUTHORITY_LABELS, PHASE_LABELS
from .domain.slots import DialogueStateMachine, SPController
from .domain.spqa import certify_bundle, ensure_certification, latest_certification
from .domain.stats import domain_scores, quality_report
from .seed import apply_startup_governance, seed_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    seed_if_empty(conn)
    apply_startup_governance(conn)
    ensure_certification(conn)
    conn.close()
    yield


app = FastAPI(title="OSCE·Loop", version="0.1.0", lifespan=lifespan)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _stage(conn) -> str:
    return db.get_state(conn, "release_stage", "G0_offline") or "G0_offline"


def _bundle_or_404(conn, bundle_id: str) -> dict[str, Any]:
    bundle = get_bundle(conn, bundle_id)
    if not bundle:
        raise HTTPException(404, "站包不存在")
    return bundle


def _resolve_bundle(conn, bundle_id: str | None = None) -> dict[str, Any]:
    """单站包演示环境：请求了失效 bundle_id 时回退到当前冻结站包，避免页面 500。"""
    bundles_list = list_bundles(conn)
    if not bundles_list:
        raise HTTPException(404, "无站包")
    if bundle_id:
        bundle = get_bundle(conn, bundle_id)
        if bundle:
            return bundle
    return bundles_list[0]


def _attempt_row(conn, attempt_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
    if not row:
        raise HTTPException(404, "场次不存在")
    return dict(row)


def _item_map(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["item_id"]: item for item in bundle["payload"].get("rubric", [])}


def _weighted(scores: dict[str, int | None], items: list[dict[str, Any]]) -> tuple[float | None, float, float]:
    earned = 0.0
    scored_weight = 0.0
    total_weight = 0.0
    for item in items:
        weight = float(item.get("weight", 0))
        total_weight += weight
        score = scores.get(item["item_id"])
        if score is None:
            continue
        scored_weight += weight
        earned += weight * float(score) / 2.0
    if scored_weight <= 0:
        return None, 0.0, total_weight
    return round(earned / scored_weight * 100, 2), scored_weight, total_weight


def _attempt_summary(conn, attempt: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_or_404(conn, attempt["bundle_id"])
    items = bundle["payload"].get("rubric", [])
    records = db.rows_to_dicts(
        conn.execute("SELECT * FROM score_records WHERE attempt_id = ?", (attempt["attempt_id"],)).fetchall()
    )
    by_item = {r["item_id"]: r for r in records}
    ai_scores = {item_id: rec["ai_score"] for item_id, rec in by_item.items()}
    final_scores = {item_id: rec["final_score"] for item_id, rec in by_item.items()}
    ai_total, _, _ = _weighted(ai_scores, items)
    final_total, final_weight, total_weight = _weighted(final_scores, items)
    pending = [r for r in records if r["status"] in ("pending_review", "manual_pending")]
    critical_pending = [
        r
        for r in pending
        if (_item_map(bundle).get(r["item_id"]) or {}).get("critical")
    ]
    return {
        "attempt_id": attempt["attempt_id"],
        "candidate_code": attempt["candidate_code"],
        "candidate_name": attempt["candidate_name"],
        "session_id": attempt["session_id"],
        "risk_level": attempt["risk_level"],
        "status": attempt["status"],
        "station_title": bundle["payload"].get("title"),
        "bundle_id": bundle["bundle_id"],
        "bundle_hash": bundle["bundle_hash"],
        "pass_line": float(bundle["payload"].get("pass_line", 60)),
        "ai_total": ai_total,
        "final_total": final_total,
        "coverage": round(final_weight / total_weight, 3) if total_weight else 0,
        "records": len(records),
        "pending": len(pending),
        "critical_pending": len(critical_pending),
        "reviewed": len(records) - len(pending),
        "near_cutoff": bool(attempt.get("near_cutoff")),
        "integrity_gap": bool(attempt.get("integrity_gap")),
        "integrity_note": attempt.get("integrity_note"),
        "appeal": bool(attempt.get("appeal")),
        "media_url": attempt.get("media_url"),
        "duration_ms": attempt.get("duration_ms"),
        "asr_confidence": attempt.get("asr_confidence"),
        "signed_off_by": attempt.get("signed_off_by"),
        "signed_off_at": attempt.get("signed_off_at"),
        "published_at": attempt.get("published_at"),
    }


def _record_public(conn, record: dict[str, Any], bundle: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    item = _item_map(bundle).get(record["item_id"], {})
    authority = effective_authority(conn, bundle, item, attempt["risk_level"], _stage(conn))
    routing = json.loads(record["routing"] or "{}")
    try:
        evidence = json.loads(record["ai_evidence"] or "[]")
    except (TypeError, ValueError):
        evidence = []
    for ev in evidence:
        ev["media_url"] = attempt.get("media_url")
    return {
        "record_id": record["record_id"],
        "attempt_id": record["attempt_id"],
        "candidate": f"{attempt['candidate_code']} · {attempt['candidate_name']}",
        "item_id": record["item_id"],
        "item_title": item.get("title"),
        "domain": item.get("domain"),
        "phase": item.get("phase"),
        "phase_label": PHASE_LABELS.get(item.get("phase"), item.get("phase")),
        "critical": bool(item.get("critical")),
        "anchor": item.get("anchor"),
        "evidence_type": item.get("evidence_type"),
        "weight": item.get("weight"),
        "authority": authority,
        "authority_label": AUTHORITY_LABELS.get(authority, authority),
        "ai_score": record["ai_score"],
        "ai_confidence": record["ai_confidence"],
        "ai_rationale": record["ai_rationale"],
        "evidence": evidence,
        "routing": {
            **routing,
            "route_label": ROUTE_LABELS.get(routing.get("route"), routing.get("route")),
            "reason_label": REASON_LABELS.get(routing.get("reason"), routing.get("reason")),
        },
        "status": record["status"],
        "final_score": record["final_score"],
        "reviewer": record["reviewer"],
        "reviewed_at": record["reviewed_at"],
        "model_version": record["model_version"],
        "prompt_version": record["prompt_version"],
        "media_url": attempt.get("media_url"),
    }


def _priority(routing: dict[str, Any]) -> int:
    order = {
        "integrity_gap": 0,
        "critical_negative": 1,
        "near_cutoff": 2,
        "appeal": 3,
        "confidence_below_floor": 4,
        "item_disagreement": 5,
        "manual_only": 6,
        "extract_only": 6,
        "sampling": 7,
        "ai_refusal": 5,
    }
    return order.get(routing.get("reason"), 9)


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/overview")
def overview(conn=Depends(get_conn)):
    report = quality_report(conn)
    attempts = db.rows_to_dicts(conn.execute("SELECT * FROM attempts ORDER BY created_at DESC").fetchall())
    summaries = [_attempt_summary(conn, a) for a in attempts]
    gates = evaluate_gates(conn, report, _stage(conn))
    recent_audit = db.rows_to_dicts(
        conn.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT 8").fetchall()
    )
    return {
        "bundle": list_bundles(conn)[0]["payload"].get("title") if list_bundles(conn) else None,
        "release_stage": _stage(conn),
        "versions": {
            "model": db.get_state(conn, "model_version", MODEL_VERSION),
            "prompt": db.get_state(conn, "prompt_version", PROMPT_VERSION),
        },
        "stats": {
            "attempts": len(summaries),
            "pending": sum(s["pending"] for s in summaries),
            "published": sum(1 for s in summaries if s["status"] == "published"),
            "qwk_ai": report["qwk_ai"],
            "qwk_human": report["qwk_human"],
            "avg_review_seconds": report["review"]["avg_seconds"],
            "edit_rate": report["review"]["edit_rate"],
            "integrity_gap_rate": report["integrity_gap_rate"],
            "evidence_rate": report["evidence"]["rate"],
        },
        "routing": report["routing"],
        "attempts": summaries,
        "p0_gate": next((g for g in gates["gates"] if g["id"] == "G0"), None),
        "alerts": gates["alerts"],
        "recent_audit": recent_audit,
    }


@app.get("/api/attempts")
def attempts(conn=Depends(get_conn)):
    rows = db.rows_to_dicts(conn.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall())
    return [_attempt_summary(conn, a) for a in rows]


@app.get("/api/attempts/{attempt_id}")
def attempt_detail(attempt_id: str, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    bundle = _bundle_or_404(conn, attempt["bundle_id"])
    records = db.rows_to_dicts(
        conn.execute("SELECT * FROM score_records WHERE attempt_id = ? ORDER BY item_id", (attempt_id,)).fetchall()
    )
    public_records = [_record_public(conn, r, bundle, attempt) for r in records]
    summary = _attempt_summary(conn, attempt)
    items = bundle["payload"].get("rubric", [])
    final_scores = {r["item_id"]: r["final_score"] if r["final_score"] is not None else r["ai_score"] for r in records}
    summary["domains"] = domain_scores(bundle["payload"], final_scores)
    critical = [r for r in public_records if r["critical"]]
    summary["critical_items"] = [
        {
            "item_id": r["item_id"],
            "title": r["item_title"],
            "status": r["status"],
            "final_score": r["final_score"],
            "ai_score": r["ai_score"],
        }
        for r in critical
    ]
    summary["signoff_ready"] = summary["pending"] == 0
    return {
        **summary,
        "records": public_records,
        "items_count": len(items),
        "audit": db.rows_to_dicts(
            conn.execute(
                "SELECT * FROM audit_events WHERE object_id IN (?, ?) ORDER BY created_at DESC LIMIT 20",
                (attempt_id, attempt["bundle_id"]),
            ).fetchall()
        ),
    }


@app.post("/api/attempts/{attempt_id}/appeal")
def appeal_attempt(attempt_id: str, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    if attempt["status"] == "published":
        raise HTTPException(400, "已发布场次需通过考务申诉流程重新开启")
    conn.execute("UPDATE attempts SET appeal = 1, status = 'pending_review' WHERE attempt_id = ?", (attempt_id,))
    for rec in conn.execute("SELECT * FROM score_records WHERE attempt_id = ?", (attempt_id,)).fetchall():
        routing = json.loads(rec["routing"] or "{}")
        routing.update(
            {
                "requires_human": True,
                "route": "full_reevaluation",
                "reason": "appeal",
                "reasons": ["appeal"],
                "reason_text": "考生发起申诉，全量重评",
            }
        )
        conn.execute(
            "UPDATE score_records SET status = 'pending_review', final_score = NULL, routing = ? WHERE record_id = ?",
            (json.dumps(routing, ensure_ascii=False), rec["record_id"]),
        )
    conn.commit()
    db.append_audit(
        conn,
        actor="考务",
        action="attempt.appeal",
        object_type="StationAttempt",
        object_id=attempt_id,
        before={"status": attempt["status"]},
        after={"status": "pending_review", "appeal": True},
    )
    return {"ok": True, "attempt": _attempt_summary(conn, _attempt_row(conn, attempt_id))}


class SignoffBody(BaseModel):
    reviewer: str = Field(min_length=1)


@app.post("/api/attempts/{attempt_id}/signoff")
def signoff_attempt(attempt_id: str, body: SignoffBody, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    if attempt["status"] == "published":
        raise HTTPException(400, "该场次已发布")
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ? AND status IN ('pending_review','manual_pending')",
        (attempt_id,),
    ).fetchone()["n"]
    if pending:
        raise HTTPException(400, f"仍有 {pending} 项未完成人工复核，不能签字")
    if attempt["risk_level"] == "L3":
        bundle = _bundle_or_404(conn, attempt["bundle_id"])
        critical_ids = {
            item["item_id"] for item in bundle["payload"]["rubric"] if item.get("critical")
        }
        if critical_ids:
            placeholders = ",".join("?" * len(critical_ids))
            critical_rows = conn.execute(
                f"SELECT item_id, status FROM score_records WHERE attempt_id = ? AND item_id IN ({placeholders})",
                (attempt_id, *critical_ids),
            ).fetchall()
            unconfirmed = [row["item_id"] for row in critical_rows if row["status"] == "auto"]
            if unconfirmed:
                raise HTTPException(400, f"L3 关键项必须人工确认：{', '.join(unconfirmed)}")
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE attempts SET status = 'signed_off', signed_off_by = ?, signed_off_at = ? WHERE attempt_id = ?",
        (body.reviewer, timestamp, attempt_id),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=body.reviewer,
        action="attempt.human_signoff",
        object_type="StationAttempt",
        object_id=attempt_id,
        before={"status": attempt["status"]},
        after={"status": "signed_off", "signed_off_by": body.reviewer},
    )
    return {"ok": True, "attempt": _attempt_summary(conn, _attempt_row(conn, attempt_id))}


class PublishBody(BaseModel):
    reviewer: str = Field(min_length=1)


@app.post("/api/attempts/{attempt_id}/publish")
def publish_attempt(attempt_id: str, body: PublishBody, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    if not attempt["signed_off_by"]:
        raise HTTPException(400, "缺少 human_signoff，成绩不可发布")
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE attempts SET status = 'published', published_at = ? WHERE attempt_id = ?",
        (timestamp, attempt_id),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=body.reviewer,
        action="attempt.publish",
        object_type="StationAttempt",
        object_id=attempt_id,
        before={"status": attempt["status"], "signed_off_by": attempt["signed_off_by"]},
        after={"status": "published"},
    )
    return {"ok": True, "attempt": _attempt_summary(conn, _attempt_row(conn, attempt_id))}


@app.get("/api/review/queue")
def review_queue(conn=Depends(get_conn), limit: int = Query(120, ge=1, le=500)):
    rows = conn.execute(
        "SELECT r.*, a.candidate_name, a.candidate_code, a.risk_level, a.media_url, a.status AS attempt_status, "
        "a.bundle_id FROM score_records r JOIN attempts a ON a.attempt_id = r.attempt_id "
        "WHERE r.status IN ('pending_review','manual_pending')"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        bundle = get_bundle(conn, rec["bundle_id"])
        item = _item_map(bundle).get(rec["item_id"], {})
        routing = json.loads(rec["routing"] or "{}")
        items.append(
            {
                "record_id": rec["record_id"],
                "attempt_id": rec["attempt_id"],
                "candidate": f"{rec['candidate_code']} · {rec['candidate_name']}",
                "risk_level": rec["risk_level"],
                "item_id": rec["item_id"],
                "item_title": item.get("title"),
                "critical": bool(item.get("critical")),
                "weight": item.get("weight"),
                "routing": {
                    **routing,
                    "route_label": ROUTE_LABELS.get(routing.get("route"), routing.get("route")),
                    "reason_label": REASON_LABELS.get(routing.get("reason"), routing.get("reason")),
                },
                "priority": _priority(routing),
                "status": rec["status"],
                "authority": effective_authority(conn, bundle, item, rec["risk_level"], _stage(conn)),
                "media_url": rec["media_url"],
            }
        )
    items.sort(key=lambda x: (x["priority"], not x["critical"], x["attempt_id"], x["item_id"]))
    return {
        "total": len(items),
        "items": items[:limit],
        "by_reason": quality_report(conn)["routing"],
    }


@app.get("/api/review/{record_id}")
def review_detail(record_id: str, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM score_records WHERE record_id = ?", (record_id,)).fetchone()
    if not row:
        raise HTTPException(404, "评分记录不存在")
    record = dict(row)
    attempt = _attempt_row(conn, record["attempt_id"])
    bundle = _bundle_or_404(conn, attempt["bundle_id"])
    public = _record_public(conn, record, bundle, attempt)
    transcript = conn.execute("SELECT payload FROM transcripts WHERE attempt_id = ?", (attempt["attempt_id"],)).fetchone()
    public["attempt"] = _attempt_summary(conn, attempt)
    public["transcript"] = json.loads(transcript["payload"]) if transcript else []
    return public


class ReviewBody(BaseModel):
    action: str
    score: int | None = None
    reason_code: str | None = None
    reviewer: str = Field(min_length=1)
    seconds_spent: float | None = None


REASON_CODES = ("证据不足", "判定过严", "判定过宽", "行为锚歧义", "其他")


@app.post("/api/review/{record_id}/action")
def review_action(record_id: str, body: ReviewBody, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM score_records WHERE record_id = ?", (record_id,)).fetchone()
    if not row:
        raise HTTPException(404, "评分记录不存在")
    record = dict(row)
    if record["status"] not in ("pending_review", "manual_pending"):
        raise HTTPException(400, "该记录已完成复核")
    attempt = _attempt_row(conn, record["attempt_id"])
    if attempt["status"] == "published":
        raise HTTPException(400, "已发布场次不可直接修改，请走申诉流程")

    action = body.action
    if action not in ("accept", "correct", "reject", "manual_score"):
        raise HTTPException(400, "未知复核动作")
    if action == "accept" and record["ai_score"] is None:
        raise HTTPException(400, "该项没有 AI 分数，不能接受")
    if action in ("correct", "reject", "manual_score"):
        if body.score not in (0, 1, 2):
            raise HTTPException(400, "修正/拒绝/人工评分必须给出 0/1/2 档位")
    if action == "reject" and body.reason_code not in REASON_CODES:
        raise HTTPException(400, "拒绝必须选择理由码")
    if action == "manual_score" and record["ai_score"] is not None:
        raise HTTPException(400, "该记录存在 AI 建议，请使用接受/修正/拒绝")

    final_score = record["ai_score"] if action == "accept" else int(body.score)
    status_map = {
        "accept": "accepted",
        "correct": "corrected",
        "reject": "rejected",
        "manual_score": "manual_scored",
    }
    status = status_map[action]
    conn.execute(
        "INSERT INTO review_actions(action_id, record_id, reviewer, action, from_score, to_score, reason_code, "
        "seconds_spent, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            db.new_id("rv"),
            record_id,
            body.reviewer,
            action,
            record["ai_score"],
            final_score,
            body.reason_code,
            body.seconds_spent,
            db.now_iso(),
        ),
    )
    conn.execute(
        "UPDATE score_records SET status = ?, final_score = ?, reviewer = ?, reviewed_at = ? WHERE record_id = ?",
        (status, final_score, body.reviewer, db.now_iso(), record_id),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=body.reviewer,
        action=f"record.{action}",
        object_type="ScoreRecord",
        object_id=record_id,
        before={"ai_score": record["ai_score"], "status": record["status"]},
        after={"final_score": final_score, "reason_code": body.reason_code},
    )
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ? AND status IN ('pending_review','manual_pending')",
        (attempt["attempt_id"],),
    ).fetchone()["n"]
    if pending == 0:
        conn.execute("UPDATE attempts SET status = 'reviewed' WHERE attempt_id = ? AND status = 'pending_review'", (attempt["attempt_id"],))
        conn.commit()
    next_row = conn.execute(
        "SELECT record_id FROM score_records WHERE status IN ('pending_review','manual_pending') "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    return {
        "ok": True,
        "record_status": status,
        "final_score": final_score,
        "attempt_pending": pending,
        "next_record_id": next_row["record_id"] if next_row else None,
    }


@app.get("/api/bundles")
def bundles(conn=Depends(get_conn)):
    return [
        {
            "bundle_id": b["bundle_id"],
            "station_id": b["station_id"],
            "title": b["title"],
            "version": b["version"],
            "bundle_hash": b["bundle_hash"],
            "status": b["status"],
            "frozen_at": b["frozen_at"],
        }
        for b in list_bundles(conn)
    ]


@app.get("/api/bundles/{bundle_id}")
def bundle_detail(bundle_id: str, conn=Depends(get_conn)):
    bundle = _bundle_or_404(conn, bundle_id)
    stage = _stage(conn)
    items = []
    for item in bundle["payload"]["rubric"]:
        authority = effective_authority(conn, bundle, item, "L2", stage)
        override = conn.execute(
            "SELECT * FROM authority_overrides WHERE bundle_id = ? AND item_id = ?",
            (bundle_id, item["item_id"]),
        ).fetchone()
        items.append(
            {
                **item,
                "effective_authority": authority,
                "authority_label": AUTHORITY_LABELS.get(authority, authority),
                "override": dict(override) if override else None,
            }
        )
    return {
        "bundle_id": bundle["bundle_id"],
        "station_id": bundle["station_id"],
        "title": bundle["title"],
        "version": bundle["version"],
        "bundle_hash": bundle["bundle_hash"],
        "status": bundle["status"],
        "frozen_at": bundle["frozen_at"],
        "supersedes": bundle["supersedes"],
        "reason": bundle["reason"],
        "payload": bundle["payload"],
        "derived": bundle["derived"],
        "validation": bundle["validation"],
        "items": items,
        "audit": db.rows_to_dicts(
            conn.execute(
                "SELECT * FROM audit_events WHERE object_id = ? ORDER BY created_at DESC", (bundle_id,)
            ).fetchall()
        ),
    }


@app.get("/api/feedback/{attempt_id}")
def feedback(attempt_id: str, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    bundle = _bundle_or_404(conn, attempt["bundle_id"])
    items = _item_map(bundle)
    templates = {
        t["item_id"]: t for t in bundle["derived"].get("feedback_templates", [])
    }
    records = db.rows_to_dicts(
        conn.execute("SELECT * FROM score_records WHERE attempt_id = ? ORDER BY item_id", (attempt_id,)).fetchall()
    )
    summary = _attempt_summary(conn, attempt)
    deductions: list[dict[str, Any]] = []
    strengths: list[dict[str, Any]] = []
    final_scores: dict[str, int | None] = {}
    for rec in records:
        item = items.get(rec["item_id"], {})
        score = rec["final_score"] if rec["final_score"] is not None else rec["ai_score"]
        final_scores[rec["item_id"]] = score
        if score is None:
            continue
        try:
            evidence = json.loads(rec["ai_evidence"] or "[]")
        except (TypeError, ValueError):
            evidence = []
        ev = evidence[0] if evidence else None
        unit = {
            "item_id": rec["item_id"],
            "item_title": item.get("title"),
            "domain": item.get("domain"),
            "score": score,
            "score_label": ("negative" if score == 0 else "partial" if score == 1 else "positive"),
            "critical": bool(item.get("critical")),
            "evidence": ev,
            "media_url": attempt.get("media_url"),
            "behavior": f"{item.get('item_id')}（{item.get('title')}）："
            + ("未达成" if score == 0 else "部分达成" if score == 1 else "达成")
            + "，对应行为锚："
            + ((item.get("anchor") or {}).get("positive" if score == 2 else "negative" if score == 0 else "partial") or ""),
            "alternative": (templates.get(rec["item_id"]) or {}).get("alternative")
            or item.get("feedback_alt"),
            "reviewed_by": rec["reviewer"],
        }
        if score == 2:
            strengths.append(unit)
        else:
            deductions.append(unit)
    return {
        **summary,
        "status_note": "该场次尚未发布，以下为阶段性反馈" if attempt["status"] != "published" else "已发布",
        "domains": domain_scores(bundle["payload"], final_scores),
        "deductions": deductions,
        "strengths": strengths,
        "feedback_rule": "每条扣分项必须包含：可回放证据 + 对应评分项档位 + 可直接背诵的替代话术。",
    }


@app.get("/api/quality")
def quality(conn=Depends(get_conn)):
    report = quality_report(conn)
    gates = evaluate_gates(conn, report, _stage(conn))
    model_cards = db.rows_to_dicts(conn.execute("SELECT * FROM model_cards ORDER BY created_at DESC").fetchall())
    for card in model_cards:
        card["metrics"] = json.loads(card["metrics"])
    calls = db.rows_to_dicts(conn.execute("SELECT * FROM ai_calls ORDER BY created_at DESC LIMIT 400").fetchall())
    latencies = sorted(c["latency_ms"] for c in calls)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
    return {
        "report": report,
        "gates": gates,
        "model_cards": model_cards,
        "ai_calls": {
            "count": len(calls),
            "p95_latency_ms": p95,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "by_capability": _count_by(calls, "capability"),
            "by_status": _count_by(calls, "status"),
        },
        "overrides": list_overrides(conn),
        "sp_certification": latest_certification(conn),
        "versions": {
            "model": db.get_state(conn, "model_version", MODEL_VERSION),
            "prompt": db.get_state(conn, "prompt_version", PROMPT_VERSION),
            "sp_model": db.get_state(conn, "sp_model_version", SP_MODEL_VERSION),
            "sp_prompt": db.get_state(conn, "sp_prompt_version", SP_PROMPT_VERSION),
        },
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.get(key) or "unknown"] = counts.get(row.get(key) or "unknown", 0) + 1
    return [{"key": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]


class VersionBody(BaseModel):
    model_version: str | None = None
    prompt_version: str | None = None
    actor: str = "系统管理员"


@app.post("/api/governance/version-bump")
def version_bump(body: VersionBody, conn=Depends(get_conn)):
    old_model = db.get_state(conn, "model_version", MODEL_VERSION)
    old_prompt = db.get_state(conn, "prompt_version", PROMPT_VERSION)
    new_model = body.model_version or f"{old_model}-next"
    new_prompt = body.prompt_version or old_prompt
    changes = apply_authority_reset(
        conn,
        reason=f"模型/提示词版本变更：{old_model}/{old_prompt} → {new_model}/{new_prompt}",
        actor=body.actor,
        model_version=new_model,
        prompt_version=new_prompt,
    )
    db.set_state(conn, "model_version", new_model)
    db.set_state(conn, "prompt_version", new_prompt)
    db.append_audit(
        conn,
        actor=body.actor,
        action="governance.version_change",
        object_type="ModelVersion",
        object_id="score.suggest",
        before={"model_version": old_model, "prompt_version": old_prompt},
        after={"model_version": new_model, "prompt_version": new_prompt, "downgraded": len(changes)},
        model_version=new_model,
        prompt_version=new_prompt,
    )
    return {
        "ok": True,
        "model_version": new_model,
        "prompt_version": new_prompt,
        "changes": changes,
        "overrides": list_overrides(conn),
    }


@app.get("/api/audit")
def audit(conn=Depends(get_conn), limit: int = Query(100, ge=1, le=500)):
    return db.rows_to_dicts(
        conn.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    )


@app.get("/api/sp/persona")
def sp_persona(bundle_id: str | None = None, conn=Depends(get_conn)):
    bundle = _resolve_bundle(conn, bundle_id)
    machine = DialogueStateMachine(bundle["payload"], bundle["derived"])
    masked_slots = []
    for s in machine.slots:
        masked_slots.append(
            {
                "slot_id": s["slot_id"],
                "phase": s["phase"],
                "disclosure": s["disclosure"],
                "linked_items": s.get("linked_items", []),
                "content": s["content"] if s["disclosure"] == "active" else "（未释放，内容不可见）",
            }
        )
    return {
        "bundle_id": bundle["bundle_id"],
        "station_title": bundle["payload"]["title"],
        "persona": bundle["payload"].get("sp_profile"),
        "persona_version": bundle["bundle_hash"],
        "phases": [
            {"phase": p, "label": PHASE_LABELS.get(p, p)}
            for p in bundle["derived"].get("phase_order", [])
        ],
        "slots": masked_slots,
        "certification": latest_certification(conn, bundle["bundle_id"]),
    }


class SpSessionBody(BaseModel):
    bundle_id: str | None = None
    candidate_label: str = "练习学生"


@app.post("/api/sp/sessions")
def create_sp_session(body: SpSessionBody, conn=Depends(get_conn)):
    bundle = _resolve_bundle(conn, body.bundle_id)
    machine = DialogueStateMachine(bundle["payload"], bundle["derived"])
    controller = SPController(machine)
    state = machine.start()
    allowed = machine.slots_by_ids(state["released"])
    reply = controller.generate(allowed, machine.persona, 0)
    session_id = db.new_id("sp")
    messages = [{"role": "patient", "text": reply, "turn": 0, "phase": "opening", "at_ms": 0}]
    conn.execute(
        "INSERT INTO sp_sessions(session_id, bundle_id, candidate_label, state, transcript, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            session_id,
            bundle["bundle_id"],
            body.candidate_label,
            json.dumps(state, ensure_ascii=False),
            json.dumps(messages, ensure_ascii=False),
            db.now_iso(),
            db.now_iso(),
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=body.candidate_label,
        action="sp.session_start",
        object_type="SpSession",
        object_id=session_id,
        after={"bundle_id": bundle["bundle_id"]},
    )
    return _sp_payload(session_id, state, messages, reply, None, False, machine, report=None)


class SpMessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@app.post("/api/sp/sessions/{session_id}/message")
def sp_message(session_id: str, body: SpMessageBody, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM sp_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "会话不存在")
    finished = conn.execute("SELECT 1 FROM sp_reports WHERE session_id = ?", (session_id,)).fetchone()
    if finished:
        raise HTTPException(400, "该练习已生成反馈，请开始新会话")
    session = dict(row)
    bundle = _bundle_or_404(conn, session["bundle_id"])
    machine = DialogueStateMachine(bundle["payload"], bundle["derived"])
    controller = SPController(machine)
    state = json.loads(session["state"])
    messages = json.loads(session["transcript"])
    candidate_at = _next_at(messages)
    messages.append(
        {
            "role": "candidate",
            "text": body.text,
            "turn": state.get("turn", 0) + 1,
            "phase": state.get("phase") or "opening",
            "at_ms": candidate_at,
        }
    )
    started = time.perf_counter()
    result = controller.respond(state, body.text)
    latency = (time.perf_counter() - started) * 1000
    new_state = result["state"]
    messages.append(
        {
            "role": "patient",
            "text": result["reply"],
            "turn": new_state["turn"],
            "phase": new_state.get("phase"),
            "at_ms": candidate_at + 4500,
        }
    )
    conn.execute(
        "UPDATE sp_sessions SET state = ?, transcript = ?, updated_at = ? WHERE session_id = ?",
        (
            json.dumps(new_state, ensure_ascii=False),
            json.dumps(messages, ensure_ascii=False),
            db.now_iso(),
            session_id,
        ),
    )
    conn.commit()
    db.log_ai_call(
        conn,
        capability="sp.generate+verify",
        model_version=db.get_state(conn, "sp_model_version", SP_MODEL_VERSION),
        prompt_version=db.get_state(conn, "sp_prompt_version", SP_PROMPT_VERSION),
        subject_ref=session_id,
        latency_ms=latency,
        status="degraded" if result["degraded"] else "ok",
        detail=result["verification"],
    )
    return _sp_payload(
        session_id,
        new_state,
        messages,
        result["reply"],
        result["verification"],
        result["degraded"],
        machine,
        released_now=result["released_now"],
        transitioned=result["transitioned"],
        intent=result["intent"],
        report=None,
    )


class SpCertBody(BaseModel):
    bundle_id: str | None = None
    actor: str = "SP 认证员"


@app.post("/api/sp/certification/run")
def run_sp_certification(body: SpCertBody, conn=Depends(get_conn)):
    bundle = _resolve_bundle(conn, body.bundle_id)
    report = certify_bundle(conn, bundle, actor=body.actor)
    return {"ok": True, "certification": report}


@app.get("/api/sp/certifications")
def sp_certifications(conn=Depends(get_conn), bundle_id: str | None = None):
    if bundle_id:
        rows = conn.execute(
            "SELECT * FROM sp_certifications WHERE bundle_id = ? ORDER BY created_at DESC",
            (bundle_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sp_certifications ORDER BY created_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        report = json.loads(row["report"])
        result.append(
            {
                "cert_id": row["cert_id"],
                "bundle_id": row["bundle_id"],
                "persona_version": row["persona_version"],
                "actor": row["actor"],
                "status": row["status"],
                "created_at": row["created_at"],
                "checks": report.get("checks", []),
                "red_team": {
                    "case_count": report.get("red_team", {}).get("case_count"),
                    "passed_cases": report.get("red_team", {}).get("passed_cases"),
                    "leak_count": report.get("red_team", {}).get("leak_count"),
                    "fact_error_rate": report.get("red_team", {}).get("fact_error_rate"),
                    "persona_first_pass_rate": report.get("red_team", {}).get("persona_first_pass_rate"),
                    "p95_latency_ms": report.get("red_team", {}).get("p95_latency_ms"),
                },
                "consistency": report.get("consistency", {}).get("rate"),
            }
        )
    return result


@app.post("/api/sp/sessions/{session_id}/finish")
def finish_sp_session(session_id: str, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM sp_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "会话不存在")
    session = dict(row)
    bundle = _bundle_or_404(conn, session["bundle_id"])
    existing = conn.execute("SELECT report FROM sp_reports WHERE session_id = ?", (session_id,)).fetchone()
    if existing:
        return {"ok": True, "report": json.loads(existing["report"])}
    messages = json.loads(session["transcript"])
    candidate_turns = sum(1 for m in messages if m.get("role") == "candidate")
    if candidate_turns == 0:
        raise HTTPException(400, "至少完成一轮提问后才能生成反馈")
    report = build_practice_report(bundle, messages)
    report["session_id"] = session_id
    report["created_at"] = db.now_iso()
    conn.execute(
        "INSERT INTO sp_reports(session_id, bundle_id, report, created_at) VALUES(?,?,?,?)",
        (session_id, bundle["bundle_id"], json.dumps(report, ensure_ascii=False), report["created_at"]),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=session.get("candidate_label") or "练习学生",
        action="sp.session_finish",
        object_type="SpSession",
        object_id=session_id,
        before=None,
        after={
            "total": report["total"],
            "coverage": report["coverage"],
            "deductions": len(report["deductions"]),
            "uncovered": report["uncovered_count"],
        },
        model_version=db.get_state(conn, "sp_model_version", SP_MODEL_VERSION),
        prompt_version=db.get_state(conn, "sp_prompt_version", SP_PROMPT_VERSION),
    )
    return {"ok": True, "report": report}


@app.get("/api/sp/sessions/{session_id}/report")
def get_sp_report(session_id: str, conn=Depends(get_conn)):
    row = conn.execute("SELECT report FROM sp_reports WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "该会话尚未生成反馈")
    return json.loads(row["report"])


def _next_at(messages: list[dict[str, Any]]) -> int:
    if not messages:
        return 0
    last = messages[-1]
    last_at = last.get("at_ms")
    if last_at is None:
        return len(messages) * 6000
    return int(last_at) + 6000


def _sp_payload(
    session_id: str,
    state: dict[str, Any],
    messages: list[dict[str, Any]],
    reply: str,
    verification: dict[str, Any] | None,
    degraded: bool,
    machine: DialogueStateMachine,
    *,
    released_now: list[str] | None = None,
    transitioned: bool = False,
    intent: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    released_ids = set(state.get("released", []))
    released_slots = [
        {
            "slot_id": s["slot_id"],
            "content": s["content"],
            "disclosure": s["disclosure"],
            "linked_items": s.get("linked_items", []),
        }
        for s in machine.slots
        if s["slot_id"] in released_ids
    ]
    return {
        "session_id": session_id,
        "reply": reply,
        "messages": messages,
        "phase": state.get("phase"),
        "phase_label": PHASE_LABELS.get(state.get("phase"), state.get("phase")),
        "turn": state.get("turn"),
        "trust": state.get("trust"),
        "released_slots": released_slots,
        "released_now": released_now or [],
        "unreleased_count": len(machine.slots) - len(released_slots),
        "verification": verification,
        "degraded": degraded,
        "transitioned": transitioned,
        "intent": intent,
        "slot_total": len(machine.slots),
        "degraded_total": state.get("degraded_count", 0),
        "report": report,
        "finished": report is not None,
    }


MEDIA_DIR.mkdir(parents=True, exist_ok=True)
WEB_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
