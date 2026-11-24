"""FastAPI 接口：复核工作台、反馈、质量闸门与 SP 演练。"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from .ai.asr import ASRError, build_asr_provider
from .ai.base import CapabilityGateway
from .ai.factory import build_scoring_provider, scoring_provider_info
from .config import (
    AI_INPUT_PRICE_PER_1K,
    AI_OUTPUT_PRICE_PER_1K,
    ASR_PRICE_PER_MINUTE,
    ASR_PROMPT_VERSION,
    DRAFT_MODEL_VERSION,
    DRAFT_PROMPT_VERSION,
    LOGIN_WINDOW_SECONDS,
    MEDIA_DIR,
    MODEL_VERSION,
    PROMPT_VERSION,
    REASON_LABELS,
    RISK_LEVELS,
    ROUTE_LABELS,
    SESSION_COOKIE_SECURE,
    SP_MODEL_VERSION,
    SP_PROMPT_VERSION,
    WEB_DIR,
)
from .domain.ai_check import check_scoring_provider
from .domain.ab import ab_report
from .domain.accessibility import (
    feedback_accessibility,
    submit_feedback_survey,
    survey_for_attempt,
)
from .domain.appeals import capture_snapshot
from .domain.asr_benchmark import (
    MAX_BENCHMARK_CHARS,
    benchmark_summary,
    list_benchmarks,
    record_benchmark,
)
from .domain.audit_search import search_audit_events
from .domain.auth import (
    SESSION_TTL_HOURS,
    authenticate,
    cleanup_expired_sessions,
    create_session,
    ensure_users,
    login_rate_limited,
    media_access,
    record_login_attempt,
    resolve_token,
    revoke_token,
    rbac_allowed,
)
from .domain.adjudication import list_adjudications
from .domain.backup import create_backup, list_backups
from .domain.bundle import (
    apply_authority_reset,
    diff_payloads,
    effective_authority,
    find_similar_bundles,
    freeze_bundle,
    get_bundle,
    list_bundles,
    list_overrides,
    list_station_bundles,
)
from .domain.blueprint import approve_blueprint, blueprint_state, draft_blueprint
from .domain.bundle_integrity import verify_bundle_integrity
from .domain.calibration import calibration_report, import_gold_scores, parse_gold_file
from .domain.consent import consent_status, record_consent
from .domain.cost import ai_cost_summary
from .domain.coverage import blueprint_coverage
from .domain.critical_miss import critical_misses, record_critical_miss_review
from .domain.content_audit import run_content_audit
from .domain.drafting import (
    DRAFT_DISCLOSURE,
    DRAFT_LEVEL,
    case_topics,
    generate_bundle_draft,
)
from .domain.export import build_attempt_package
from .domain.fairness import fairness_report
from .domain.feedback import (
    ensure_feedback_evidence,
    ensure_feedback_units,
    feedback_note_requirement,
    summarize_feedback,
)
from .domain.gates import evaluate_gates
from .domain.governance import (
    emergency_stop_active,
    governance_status,
    latest_self_check,
    publish_model_card,
    register_approval,
    run_appeal_drill,
    run_self_check,
    run_termination_drill,
    set_emergency_stop,
    set_release_stage,
)
from .domain.health import system_health
from .domain.history import candidate_history
from .domain.hint_impact import hint_impact
from .domain.execution import (
    advance_run,
    get_run,
    latest_run,
    slot_action,
    start_run,
    stop_run,
    toggle_hints,
    toggle_station_hints,
)
from .domain.ingest import create_ingested_attempt
from .domain.integrity import backfill_transcript_hashes, transcript_integrity, verify_evidence
from .domain.item_quality import item_quality_report
from .domain.latency import feedback_ack_latency
from .domain.maintenance import run_maintenance
from .domain.media import backfill_media_hashes
from .domain.practice import build_practice_report
from .domain.profile import candidate_cross_station_profile
from .domain.psychometrics import station_psychometrics
from .domain.retention import purge_expired, refresh_retention, retention_overview
from .domain.readability import analyze_feedback_readability
from .domain.request_log import append_request_log, recent_requests, request_log_summary
from .domain.review import apply_review, blind_dual_review
from .domain.review_sla import SLA_HOURS, review_sla
from .domain.rewrite import PRINCIPLE as REWRITE_PRINCIPLE, suggest_anchor_rewrites
from .domain.risk import RISK_ORDER, escalate_risk
from .domain.rubric import AUTHORITY_LABELS, PHASE_LABELS, cap_authority, derive_assets, validate_bundle
from .domain.rubric_maintenance import REASON_CODES, rubric_maintenance
from .domain.run_export import build_run_package
from .domain.schedule_approval import record_special_needs_approval, special_needs_status
from .domain.schedule_policy import (
    confirm_policy,
    get_policy,
    latest_policy,
    presets_payload,
)
from .domain.scheduling import (
    add_blackout,
    delete_blackout,
    ensure_schedule_inputs,
    generate_schedule,
    latest_schedule,
    list_blackouts,
    repair_schedule,
    schedule_inputs,
)
from .domain.signoff_audit import audit_signoff_chain
from .domain.slots import DialogueStateMachine, SPController
from .domain.sp_approval import (
    list_persona_approvals,
    persona_approval_status,
    record_persona_approval,
)
from .domain.spqa import (
    certification_for_bundle,
    certify_bundle,
    ensure_certification,
    sp_session_ready,
)
from .domain.stats import domain_scores, quality_report
from .domain.station_quality import station_quality_report
from .domain.station_review import list_station_reviews, record_station_review
from .domain.sus import submit_sus, sus_summary
from .domain.trace import score_trace
from .domain.tone import analyze_feedback_tone
from .domain.transcript import parse_notes, parse_transcript
from .domain.transparency import (
    acknowledge_disclosure,
    attempt_ai_disclosure,
    disclosure_acknowledged,
    public_transparency,
    refresh_transparency_assets,
)
from .domain.users import create_user, list_users, update_user
from .seed import apply_startup_governance, seed_if_empty


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    seed_if_empty(conn)
    apply_startup_governance(conn)
    ensure_certification(conn)
    ensure_schedule_inputs(conn)
    ensure_users(conn)
    refresh_retention(conn)
    backfill_media_hashes(conn)
    backfill_transcript_hashes(conn)
    conn.close()
    yield


app = FastAPI(title="OSCE·Loop", version="0.1.0", lifespan=lifespan)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.middleware("http")
async def static_cache_control(request: Request, call_next):
    """静态资源与首页每次都回源校验，避免升级后浏览器继续使用旧前端。"""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    bearer = ""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        bearer = header[7:].strip()
    media_token = request.cookies.get("osceloop_session", "") if path.startswith("/media/") else ""
    if path.startswith("/api/") and path.rstrip("/") != "/api/auth/login":
        token = bearer or request.cookies.get("osceloop_session", "")
        if not token:
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
        conn = db.connect()
        try:
            user = resolve_token(conn, token)
        finally:
            conn.close()
        if not user:
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
        if not rbac_allowed(request.method, path, user["role"]):
            return JSONResponse(
                {"detail": f"角色 {user['role_label']} 无权执行该操作"},
                status_code=403,
            )
        request.state.user = user
        request.state.token = token
    elif path.startswith("/media/"):
        token = media_token or bearer
        if not token:
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
        conn = db.connect()
        try:
            user = resolve_token(conn, token)
            if user:
                access = media_access(conn, user, path)
                if access == "purged":
                    return JSONResponse(
                        {"detail": "该媒体已按留存策略清理，证据时间戳与哈希仍可查"}, status_code=410
                    )
                if access == "expired":
                    return JSONResponse(
                        {"detail": "该媒体已超过留存期限，当前不可访问，等待清理"}, status_code=410
                    )
                if access != "ok":
                    return JSONResponse({"detail": "无权访问该媒体文件"}, status_code=403)
        finally:
            conn.close()
        if not user:
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
        request.state.user = user
        request.state.token = token
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    return response


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        user = getattr(request.state, "user", None) or {}
        append_request_log(
            {
                "at": db.now_iso(),
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "user": user.get("username"),
                "role": user.get("role"),
                "ip": request.client.host if request.client else "",
            }
        )


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response, conn=Depends(get_conn)):
    username = body.username.strip()
    ip = request.client.host if request.client else ""
    if login_rate_limited(conn, username=username, ip=ip):
        db.append_audit(
            conn,
            actor=username,
            action="auth.login_locked",
            object_type="User",
            object_id=username,
            after={"ip": ip, "window_seconds": LOGIN_WINDOW_SECONDS},
        )
        raise HTTPException(
            429,
            "登录失败次数过多，请稍后再试",
            headers={"Retry-After": str(LOGIN_WINDOW_SECONDS)},
        )
    user = authenticate(conn, username, body.password)
    if not user:
        record_login_attempt(conn, username=username, ip=ip, success=False)
        db.append_audit(
            conn,
            actor=username,
            action="auth.login_failed",
            object_type="User",
            object_id=username,
            after={"ip": ip},
        )
        raise HTTPException(401, "用户名或密码错误")
    record_login_attempt(conn, username=username, ip=ip, success=True)
    cleanup_expired_sessions(conn)
    session = create_session(conn, user["user_id"])
    response.set_cookie(
        "osceloop_session",
        session["token"],
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
        path="/",
    )
    db.append_audit(
        conn,
        actor=user["username"],
        action="auth.login",
        object_type="User",
        object_id=user["user_id"],
        after={"role": user["role"]},
    )
    return {**session, "user": user}


@app.get("/api/auth/me")
def me(request: Request):
    return request.state.user


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, conn=Depends(get_conn)):
    user = request.state.user
    revoke_token(conn, getattr(request.state, "token", ""))
    response.delete_cookie("osceloop_session", path="/")
    db.append_audit(
        conn,
        actor=user["username"],
        action="auth.logout",
        object_type="User",
        object_id=user["user_id"],
        after=None,
    )
    return {"ok": True}


@app.get("/healthz")
def healthz():
    """无鉴权的最小存活探针：只验证数据库可连接。"""
    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        raise HTTPException(503, "数据库不可用") from exc
    return {"status": "ok"}


@app.get("/api/ops/health")
def ops_health(conn=Depends(get_conn)):
    return system_health(conn)


@app.get("/api/ops/request-logs")
def ops_request_logs(
    limit: int = Query(100, ge=1, le=500),
    days: int = Query(7, ge=1, le=31),
):
    """API 全量请求日志：近期明细 + 多日汇总，用于故障排查与容量评估。"""
    return {"summary": request_log_summary(days=days), "recent": recent_requests(limit=limit)}


@app.get("/api/ops/signoff-audit")
def ops_signoff_audit(sample_size: int = Query(20, ge=1, le=200), conn=Depends(get_conn)):
    """抽查最近签字/发布场次，核对人工签字链与关键项确认。"""
    return audit_signoff_chain(conn, sample_size=sample_size)


@app.post("/api/ops/ai-check")
def ops_ai_check(request: Request, conn=Depends(get_conn)):
    """对当前 AI 提供方做一次固定小样本连通性与证据结构自检。"""
    _ensure_not_stopped(conn)
    return check_scoring_provider(conn)


class UserCreateBody(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    display_name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    role: str
    candidate_code: str | None = None


class UserUpdateBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=40)
    role: str | None = None
    active: bool | None = None
    candidate_code: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


@app.get("/api/users")
def users_list(conn=Depends(get_conn)):
    return {"users": list_users(conn)}


@app.post("/api/users")
def users_create(body: UserCreateBody, request: Request, conn=Depends(get_conn)):
    try:
        user = create_user(
            conn,
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            role=body.role,
            candidate_code=body.candidate_code,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "user": user}


@app.patch("/api/users/{user_id}")
def users_update(
    user_id: str,
    body: UserUpdateBody,
    request: Request,
    conn=Depends(get_conn),
):
    try:
        user = update_user(
            conn,
            user_id,
            display_name=body.display_name,
            role=body.role,
            active=body.active,
            candidate_code=body.candidate_code,
            password=body.password,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "user": user}


@app.post("/api/ops/backup")
def ops_backup(request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    return {
        "ok": True,
        "backup": create_backup(conn, actor=request.state.user["display_name"]),
    }


@app.get("/api/ops/backups")
def ops_backups():
    return {"backups": list_backups(limit=10)}


@app.post("/api/ops/maintenance")
def ops_maintenance(request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    return {
        "ok": True,
        "maintenance": run_maintenance(conn, actor=request.state.user["display_name"]),
    }


class ContentAuditBody(BaseModel):
    sample_rate: float = Field(default=0.2, ge=0, le=1)
    limit: int = Field(default=20, ge=1, le=200)
    attempt_id: str | None = None


@app.post("/api/ops/content-audit")
def ops_content_audit(body: ContentAuditBody, request: Request, conn=Depends(get_conn)):
    """按比例抽样扫描 SP 越界话术与高风险表述，作为伦理安全验证线索。"""
    try:
        return run_content_audit(
            conn,
            sample_rate=body.sample_rate,
            limit=body.limit,
            attempt_id=body.attempt_id,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

UPLOAD_DIR = MEDIA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_MEDIA_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".mp4", ".webm", ".mov"}
MAX_MEDIA_BYTES = 200 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024


def _stage(conn) -> str:
    return db.get_state(conn, "release_stage", "G0_offline") or "G0_offline"


def _ensure_not_stopped(conn) -> None:
    if emergency_stop_active(conn):
        raise HTTPException(423, "终止开关已激活，当前写操作被阻断")


def _ensure_attempt_access(user: dict[str, Any], attempt: dict[str, Any]) -> None:
    if user.get("role") == "student":
        if not user.get("candidate_code") or attempt.get("candidate_code") != user["candidate_code"]:
            raise HTTPException(403, "学生只能访问自己的场次与反馈")


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
    transcript_row = conn.execute(
        "SELECT payload, sha256 FROM transcripts WHERE attempt_id = ?", (attempt["attempt_id"],)
    ).fetchone()
    if transcript_row:
        try:
            utterances = json.loads(transcript_row["payload"])
        except (TypeError, ValueError):
            utterances = None
        transcript_sha = transcript_row["sha256"]
        transcript_state = transcript_integrity(transcript_sha, utterances)
    else:
        transcript_sha = None
        transcript_state = "missing"
    return {
        "attempt_id": attempt["attempt_id"],
        "candidate_code": attempt["candidate_code"],
        "candidate_name": attempt["candidate_name"],
        "session_id": attempt["session_id"],
        "risk_level": attempt["risk_level"],
        "ab_group": attempt.get("ab_group"),
        "risk_escalated_at": attempt.get("risk_escalated_at"),
        "risk_escalation_reason": attempt.get("risk_escalation_reason"),
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
        "anomalies": json.loads(attempt.get("anomalies") or "[]"),
        "appeal": bool(attempt.get("appeal")),
        "feedback_note": attempt.get("feedback_note"),
        "media_url": attempt.get("media_url"),
        "media_sha256": attempt.get("media_sha256"),
        "transcript_sha256": transcript_sha,
        "transcript_integrity": transcript_state,
        "media_purged_at": attempt.get("media_purged_at"),
        "retention_class": attempt.get("retention_class"),
        "retain_until": attempt.get("retain_until"),
        "duration_ms": attempt.get("duration_ms"),
        "asr_confidence": attempt.get("asr_confidence"),
        "signed_off_by": attempt.get("signed_off_by"),
        "signed_off_at": attempt.get("signed_off_at"),
        "published_at": attempt.get("published_at"),
        "run_id": attempt.get("run_id"),
        "slot_key": attempt.get("slot_key"),
    }


def _record_public(conn, record: dict[str, Any], bundle: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    item = _item_map(bundle).get(record["item_id"], {})
    authority = effective_authority(conn, bundle, item, attempt["risk_level"], _stage(conn))
    routing = json.loads(record["routing"] or "{}")
    try:
        evidence = json.loads(record["ai_evidence"] or "[]")
    except (TypeError, ValueError):
        evidence = []
    evidence_integrity = verify_evidence(evidence)
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
        "evidence_integrity": evidence_integrity,
        "routing": {
            **routing,
            "route_label": ROUTE_LABELS.get(routing.get("route"), routing.get("route")),
            "reason_label": REASON_LABELS.get(routing.get("reason"), routing.get("reason")),
        },
        "status": record["status"],
        "final_score": record["final_score"],
        "reviewer": record["reviewer"],
        "reviewed_at": record["reviewed_at"],
        "dual_review": routing.get("dual_review"),
        "model_version": record["model_version"],
        "prompt_version": record["prompt_version"],
        "media_url": attempt.get("media_url"),
    }


def _priority(routing: dict[str, Any]) -> int:
    order = {
        "risk_escalation": 0,
        "integrity_gap": 0,
        "ai_degraded": 1,
        "critical_negative": 1,
        "review_disagreement": 0,
        "near_cutoff": 2,
        "l3_human_confirmation": 2,
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
    return FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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
def attempts(request: Request, conn=Depends(get_conn)):
    user = request.state.user
    if user.get("role") == "student":
        rows = db.rows_to_dicts(
            conn.execute(
                "SELECT * FROM attempts WHERE candidate_code = ? ORDER BY attempt_id",
                (user.get("candidate_code"),),
            ).fetchall()
        )
    else:
        rows = db.rows_to_dicts(conn.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall())
    return [_attempt_summary(conn, a) for a in rows]


async def _save_media(upload: UploadFile | None) -> dict[str, Any] | None:
    if not upload or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(400, f"不支持的媒体格式：{suffix or '未知'}")
    media_id = db.new_id("media")
    stored_name = f"{media_id}{suffix}"
    target = UPLOAD_DIR / stored_name
    total = 0
    digest = hashlib.sha256()
    try:
        with target.open("wb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise HTTPException(413, "媒体文件超过 200MB 限制")
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {
        "media_id": media_id,
        "media_url": f"/media/uploads/{stored_name}",
        "filename": upload.filename,
        "media_filename": upload.filename,
        "stored_name": stored_name,
        "size_bytes": total,
        "sha256": "sha256:" + digest.hexdigest(),
    }


def _cleanup_media(media: dict[str, Any] | None) -> None:
    if media and media.get("stored_name"):
        (UPLOAD_DIR / media["stored_name"]).unlink(missing_ok=True)


@app.post("/api/attempts/ingest")
async def ingest_attempt(
    bundle_id: str = Form(""),
    run_id: str = Form(""),
    slot_key: str = Form(""),
    candidate_code: str = Form(""),
    candidate_name: str = Form(""),
    risk_level: str = Form("L2"),
    actor: str = Form("考务录入"),
    asr_language: str = Form("zh"),
    asr_first_speaker: str = Form("patient"),
    asr_speaker_map: str = Form(""),
    candidate_notes: str = Form("", max_length=20000),
    transcript_file: UploadFile | None = File(None),
    media_file: UploadFile | None = File(None),
    conn=Depends(get_conn),
):
    _ensure_not_stopped(conn)
    if risk_level not in RISK_LEVELS:
        raise HTTPException(400, "非法风险等级")
    if asr_first_speaker not in ("patient", "candidate"):
        raise HTTPException(400, "asr_first_speaker 必须是 patient 或 candidate")
    speaker_map = None
    if asr_speaker_map.strip():
        try:
            speaker_map = json.loads(asr_speaker_map)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "asr_speaker_map 必须是 JSON 对象") from exc
        if not isinstance(speaker_map, dict):
            raise HTTPException(400, "asr_speaker_map 必须是 JSON 对象")
    bundle = _resolve_bundle(conn, bundle_id)

    media = None
    asr_meta = None
    if transcript_file is not None and transcript_file.filename:
        raw = await transcript_file.read()
        if not raw:
            raise HTTPException(400, "转写文件为空")
        if len(raw) > MAX_TRANSCRIPT_BYTES:
            raise HTTPException(413, "转写文件超过 10MB 限制")
        try:
            parsed = parse_transcript(transcript_file.filename or "transcript.txt", raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"转写解析失败：{exc}") from exc
        parsed["source"] = "upload"
    else:
        if media_file is None or not media_file.filename:
            raise HTTPException(400, "请上传转写文件，或上传音视频并配置 ASR")
        media = await _save_media(media_file)
        asr_bundle = build_asr_provider()
        if not asr_bundle.provider:
            _cleanup_media(media)
            reason = asr_bundle.info.get("reason") or "无可用的 ASR 服务"
            raise HTTPException(400, f"未配置 ASR：{reason}")
        asr_gateway = CapabilityGateway(
            conn,
            asr_bundle.provider,
            asr_bundle.provider.model_version,
            ASR_PROMPT_VERSION,
        )
        try:
            asr_response = asr_gateway.transcribe_media(
                media_path=UPLOAD_DIR / media["stored_name"],
                language=asr_language or "zh",
                speaker_map=speaker_map,
                first_speaker=asr_first_speaker,
                subject_ref=f"media:{media['media_id']}",
                idempotency_key=(
                    f"asr:{media.get('sha256')}:{asr_language or 'zh'}:{asr_first_speaker}:"
                    f"{json.dumps(speaker_map, sort_keys=True, ensure_ascii=False) if speaker_map else ''}"
                ),
            )
        except ASRError as exc:
            _cleanup_media(media)
            raise HTTPException(400, f"ASR 转写失败：{exc}") from exc
        parsed = asr_response["output"]
        asr_meta = {
            "provider": parsed.get("provider"),
            "model_version": parsed.get("model_version"),
            "prompt_version": asr_response["prompt_version"],
            "latency_ms": asr_response["latency_ms"],
            "status": asr_response["status"],
            "language": asr_language or "zh",
            "speaker_labels": parsed.get("speaker_labels"),
        }

    notes = parse_notes(candidate_notes, duration_ms=int(parsed.get("duration_ms") or 0))
    if notes:
        parsed["utterances"].extend(notes)
        parsed["candidate_utterances"] = int(parsed.get("candidate_utterances") or 0) + len(notes)
        parsed["duration_ms"] = max(
            int(parsed.get("duration_ms") or 0),
            max(int(note["end_ms"]) for note in notes),
        )
        parsed["warnings"].append(
            f"已并入 {len(notes)} 条考生笔记/操作日志；未带时间戳的笔记按场次末尾排列。"
        )
    parsed["notes"] = len(notes)
    if not parsed.get("utterances"):
        _cleanup_media(media)
        raise HTTPException(400, "转写没有有效 utterance")
    if media is None:
        media = await _save_media(media_file)

    code = candidate_code.strip() or f"C-UPLOAD-{db.new_id('cu')[-6:].upper()}"
    name = candidate_name.strip() or "未命名考生"
    provider_bundle = build_scoring_provider()
    gateway = CapabilityGateway(
        conn,
        provider_bundle.primary,
        provider_bundle.model_version,
        provider_bundle.prompt_version,
        fallback_provider=provider_bundle.fallback,
    )
    try:
        attempt_id = create_ingested_attempt(
            conn,
            gateway,
            bundle=bundle,
            transcript=parsed["utterances"],
            candidate_code=code,
            candidate_name=name,
            risk_level=risk_level,
            release_stage=_stage(conn),
            media_id=(media or {}).get("media_id") or "upload_no_media",
            media_url=(media or {}).get("media_url"),
            media_filename=(media or {}).get("filename"),
            media_sha256=(media or {}).get("sha256"),
            media_size_bytes=(media or {}).get("size_bytes") or 0,
            transcript_source=parsed.get("source", "upload"),
            asr_model=(asr_meta or {}).get("model_version"),
            actor=actor,
            run_id=run_id.strip() or None,
            slot_key=slot_key.strip() or None,
        )
    except ValueError as exc:
        _cleanup_media(media)
        raise HTTPException(400, str(exc)) from exc
    attempt = _attempt_summary(conn, _attempt_row(conn, attempt_id))
    return {
        "ok": True,
        "attempt": attempt,
        "linked_slot": {
            "run_id": run_id.strip(),
            "slot_key": slot_key.strip(),
        }
        if run_id.strip() and slot_key.strip()
        else None,
        "transcript": {
            "format": parsed["format"],
            "utterances": len(parsed["utterances"]),
            "candidate_utterances": parsed["candidate_utterances"],
            "patient_utterances": parsed["patient_utterances"],
            "duration_ms": parsed["duration_ms"],
            "warnings": parsed["warnings"],
            "source": parsed.get("source", "upload"),
            "asr": asr_meta,
            "notes": parsed.get("notes", 0),
        },
        "media": media,
        "integrity_gap": not bool(media),
    }


@app.get("/api/attempts/{attempt_id}/export")
def attempt_export(attempt_id: str, request: Request, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    _ensure_attempt_access(request.state.user, attempt)
    try:
        return build_attempt_package(
            conn, attempt_id, exported_by=request.state.user["display_name"]
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/attempts/{attempt_id}/trace")
def attempt_trace(
    attempt_id: str,
    request: Request,
    item_id: str | None = Query(default=None),
    conn=Depends(get_conn),
):
    """成绩—证据—操作互查：一条成绩项的全链路追溯。"""
    attempt = _attempt_row(conn, attempt_id)
    _ensure_attempt_access(request.state.user, attempt)
    result = score_trace(conn, attempt_id, item_id=item_id)
    if result is None:
        raise HTTPException(404, "场次不存在")
    db.append_audit(
        conn,
        actor=request.state.user["display_name"],
        action="attempt.trace_viewed",
        object_type="Attempt",
        object_id=attempt_id,
        after={"item_id": item_id, "records": len(result["records"])},
    )
    return result


@app.get("/api/attempts/{attempt_id}")
def attempt_detail(attempt_id: str, request: Request, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    _ensure_attempt_access(request.state.user, attempt)
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
    _ensure_not_stopped(conn)
    attempt = _attempt_row(conn, attempt_id)
    if attempt["status"] == "published":
        raise HTTPException(400, "已发布场次需通过考务申诉流程重新开启")
    if attempt["appeal"]:
        raise HTTPException(400, "该场次已在申诉重评中")
    snapshot = capture_snapshot(conn, attempt)
    conn.execute(
        "UPDATE attempts SET appeal = 1, appeal_snapshot = ?, status = 'pending_review' WHERE attempt_id = ?",
        (snapshot, attempt_id),
    )
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


class RiskBody(BaseModel):
    risk_level: str
    reason: str = Field(min_length=1)
    actor: str = "考务"


@app.post("/api/attempts/{attempt_id}/risk-level")
def raise_attempt_risk(attempt_id: str, body: RiskBody, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    if body.risk_level not in RISK_ORDER:
        raise HTTPException(400, "非法风险等级")
    try:
        result = escalate_risk(
            conn,
            attempt_id=attempt_id,
            new_level=body.risk_level,
            reason=body.reason,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    attempt = _attempt_row(conn, attempt_id)
    return {"ok": True, "escalation": result, "attempt": _attempt_summary(conn, attempt)}


class SignoffBody(BaseModel):
    reviewer: str = Field(min_length=1)
    feedback_note: str = Field(default="", max_length=500)


@app.post("/api/attempts/{attempt_id}/signoff")
def signoff_attempt(attempt_id: str, body: SignoffBody, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
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
    summary = _attempt_summary(conn, attempt)
    note = body.feedback_note.strip()
    note_error = feedback_note_requirement(summary["final_total"], summary["pass_line"], note)
    if note_error:
        raise HTTPException(400, note_error)
    timestamp = db.now_iso()
    conn.execute(
        "UPDATE attempts SET status = 'signed_off', signed_off_by = ?, signed_off_at = ?, "
        "feedback_note = ? WHERE attempt_id = ?",
        (body.reviewer, timestamp, note or None, attempt_id),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor=body.reviewer,
        action="attempt.human_signoff",
        object_type="StationAttempt",
        object_id=attempt_id,
        before={"status": attempt["status"]},
        after={
            "status": "signed_off",
            "signed_off_by": body.reviewer,
            "feedback_note_chars": len(note),
        },
    )
    return {"ok": True, "attempt": _attempt_summary(conn, _attempt_row(conn, attempt_id))}


class PublishBody(BaseModel):
    reviewer: str = Field(min_length=1)


@app.post("/api/attempts/{attempt_id}/publish")
def publish_attempt(attempt_id: str, body: PublishBody, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
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
        sla = review_sla(rec["created_at"], rec["risk_level"])
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
                "created_at": rec["created_at"],
                "age_hours": sla["age_hours"],
                "sla_hours": sla["sla_hours"],
                "remaining_hours": sla["remaining_hours"],
                "overdue": sla["overdue"],
            }
        )
    items.sort(
        key=lambda x: (
            not x["overdue"],
            x["priority"],
            not x["critical"],
            x["attempt_id"],
            x["item_id"],
        )
    )
    return {
        "total": len(items),
        "items": items[:limit],
        "by_reason": quality_report(conn)["routing"],
        "overdue_count": sum(1 for item in items if item["overdue"]),
        "oldest_wait_hours": max(
            (item["age_hours"] for item in items if item["age_hours"] is not None),
            default=None,
        ),
        "sla_hours": SLA_HOURS,
    }


@app.get("/api/review/{record_id}")
def review_detail(record_id: str, request: Request, conn=Depends(get_conn)):
    row = conn.execute("SELECT * FROM score_records WHERE record_id = ?", (record_id,)).fetchone()
    if not row:
        raise HTTPException(404, "评分记录不存在")
    record = dict(row)
    attempt = _attempt_row(conn, record["attempt_id"])
    bundle = _bundle_or_404(conn, attempt["bundle_id"])
    public = _record_public(conn, record, bundle, attempt)
    user = request.state.user
    public["dual_review"] = blind_dual_review(
        public.get("dual_review"),
        viewer=user.get("display_name"),
        role=user.get("role"),
    )
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


@app.post("/api/review/{record_id}/action")
def review_action(record_id: str, body: ReviewBody, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
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
    try:
        outcome = apply_review(
            conn,
            record,
            action=action,
            status=status,
            final_score=final_score,
            reviewer=body.reviewer,
            reason_code=body.reason_code,
            seconds_spent=body.seconds_spent,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not outcome["finalized"]:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ? AND status IN ('pending_review','manual_pending')",
            (attempt["attempt_id"],),
        ).fetchone()["n"]
        return {
            "ok": True,
            "record_status": record["status"],
            "final_score": None,
            "attempt_pending": pending,
            "next_record_id": record_id,
            "needs_second_review": bool(outcome.get("needs_second_review")),
            "needs_senior_review": bool(outcome.get("needs_senior_review")),
            "dual_review": outcome.get("dual_review"),
        }
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
        "needs_second_review": False,
        "needs_senior_review": False,
        "dual_review": outcome.get("dual_review"),
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
            "supersedes": b["supersedes"],
            "reason": b["reason"],
        }
        for b in list_bundles(conn)
    ]


@app.get("/api/bundles/stations")
def bundle_stations(conn=Depends(get_conn)):
    return {"stations": list_station_bundles(conn)}


@app.get("/api/bundles/draft/topics")
def bundle_draft_topics():
    """S1 命题辅助支持的病例主题；草案固定为 L0，不可直接进入考试。"""
    return {"topics": case_topics(), "level": DRAFT_LEVEL, "disclosure": DRAFT_DISCLOSURE}


class BundleDraftBody(BaseModel):
    topic: str
    difficulty: str = Field(default="medium", pattern=r"^(easy|medium|hard)$")
    duration_min: int | None = Field(default=None, ge=5, le=30)


@app.post("/api/bundles/draft")
def bundle_draft(body: BundleDraftBody, request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    provider_bundle = build_scoring_provider()
    llm_gateway = None
    if provider_bundle.info.get("configured") and hasattr(provider_bundle.primary, "draft_bundle"):
        llm_gateway = CapabilityGateway(
            conn,
            provider_bundle.primary,
            provider_bundle.model_version or DRAFT_MODEL_VERSION,
            DRAFT_PROMPT_VERSION,
        )
    try:
        return generate_bundle_draft(
            conn,
            topic=body.topic,
            difficulty=body.difficulty,
            duration_min=body.duration_min,
            actor=request.state.user["display_name"],
            llm_gateway=llm_gateway,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
        l3_authority = cap_authority(item, "L3", "G3_limited")
        items.append(
            {
                **item,
                "effective_authority": authority,
                "authority_label": AUTHORITY_LABELS.get(authority, authority),
                "l3_authority": l3_authority,
                "l3_authority_label": AUTHORITY_LABELS.get(l3_authority, l3_authority),
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
        "adjudications": list_adjudications(conn, bundle_id),
        "items": items,
        "audit": db.rows_to_dicts(
            conn.execute(
                "SELECT * FROM audit_events WHERE object_id = ? ORDER BY created_at DESC", (bundle_id,)
            ).fetchall()
        ),
    }


class BundlePayloadBody(BaseModel):
    payload: dict[str, Any]
    base_bundle_id: str | None = None


class ConsistencyDecisionBody(BaseModel):
    diff_id: str = Field(min_length=1, max_length=40)
    decision: str = Field(pattern=r"^(accept|fix)$")
    note: str = Field(default="", max_length=300)


@app.post("/api/bundles/{bundle_id}/verify-integrity")
def verify_bundle_integrity_endpoint(bundle_id: str, request: Request, conn=Depends(get_conn)):
    try:
        result = verify_bundle_integrity(
            conn, bundle_id, actor=request.state.user["display_name"]
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": result["status"] != "fail", "integrity": result}


class BundleFreezeBody(BaseModel):
    payload: dict[str, Any]
    version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")
    supersedes: str | None = None
    reason: str = Field(default="站包修订", min_length=1, max_length=200)
    adjudications: list[ConsistencyDecisionBody] = Field(default_factory=list)


@app.post("/api/bundles/validate")
def validate_bundle_proposal(body: BundlePayloadBody, conn=Depends(get_conn)):
    validation = validate_bundle(body.payload)
    try:
        counts = derive_assets(body.payload)["counts"]
    except (KeyError, TypeError, ValueError):
        counts = None
    diff = None
    if body.base_bundle_id:
        base = get_bundle(conn, body.base_bundle_id)
        if not base:
            raise HTTPException(400, "base_bundle_id 指向的站包不存在")
        diff = diff_payloads(base["payload"], body.payload)
    return {
        "validation": validation,
        "derived_counts": counts,
        "diff": diff,
        "similarity": find_similar_bundles(conn, body.payload),
        "rewrites": suggest_anchor_rewrites(body.payload),
        "rewrite_principle": REWRITE_PRINCIPLE,
    }


@app.post("/api/bundles/freeze")
def freeze_bundle_proposal(body: BundleFreezeBody, request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    if body.supersedes and not get_bundle(conn, body.supersedes):
        raise HTTPException(400, "supersedes 指向的站包不存在")
    total_before = conn.execute("SELECT COUNT(*) AS n FROM bundles").fetchone()["n"]
    try:
        bundle = freeze_bundle(
            conn,
            body.payload,
            actor=request.state.user["display_name"],
            version=body.version,
            supersedes=body.supersedes,
            reason=body.reason,
            adjudications=[item.model_dump() for item in body.adjudications],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    total_after = conn.execute("SELECT COUNT(*) AS n FROM bundles").fetchone()["n"]
    return {
        "ok": True,
        "created": total_after > total_before,
        "bundle": {
            "bundle_id": bundle["bundle_id"],
            "station_id": bundle["station_id"],
            "title": bundle["title"],
            "version": bundle["version"],
            "bundle_hash": bundle["bundle_hash"],
            "frozen_at": bundle["frozen_at"],
            "supersedes": bundle["supersedes"],
        },
        "validation": bundle["validation"],
        "derived_counts": bundle["derived"]["counts"],
        "adjudications": list_adjudications(conn, bundle["bundle_id"]),
    }


@app.get("/api/feedback/{attempt_id}")
def feedback(attempt_id: str, request: Request, conn=Depends(get_conn)):
    attempt = _attempt_row(conn, attempt_id)
    _ensure_attempt_access(request.state.user, attempt)
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
    ensure_feedback_units(deductions + strengths, items)
    ensure_feedback_evidence(
        deductions,
        media_id=attempt.get("media_id"),
        duration_ms=int(attempt.get("duration_ms") or 0),
    )
    readability = analyze_feedback_readability(
        [
            unit.get(field, "")
            for unit in deductions + strengths
            for field in ("behavior", "alternative")
        ]
    )
    tone = analyze_feedback_tone(
        texts=[
            unit.get(field, "")
            for unit in deductions + strengths
            for field in ("behavior", "alternative")
        ],
        final_total=summary.get("final_total"),
        pass_line=float(bundle["payload"].get("pass_line", 60)),
    )
    has_transcript = (
        conn.execute("SELECT 1 FROM transcripts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        is not None
    )
    disclosure = attempt_ai_disclosure(attempt, records, has_transcript=has_transcript)
    acknowledgement = disclosure_acknowledged(conn, attempt_id, request.state.user["user_id"])
    disclosure["acknowledged"] = bool(acknowledgement)
    disclosure["acknowledged_at"] = acknowledgement["created_at"] if acknowledgement else None
    return {
        **summary,
        "status_note": "该场次尚未发布，以下为阶段性反馈" if attempt["status"] != "published" else "已发布",
        "feedback_note": attempt.get("feedback_note"),
        "ai_disclosure": disclosure,
        "domains": domain_scores(bundle["payload"], final_scores),
        "deductions": deductions,
        "strengths": strengths,
        "feedback_quality": summarize_feedback(deductions),
        "readability": readability,
        "tone": tone if request.state.user["role"] != "student" else None,
        "survey": survey_for_attempt(conn, attempt_id),
        "history": candidate_history(conn, attempt_id),
        "cross_station_profile": candidate_cross_station_profile(conn, attempt_id),
        "feedback_rule": "每条扣分项必须包含：可回放证据 + 对应评分项档位 + 可直接背诵的替代话术。",
    }


@app.post("/api/feedback/{attempt_id}/acknowledge")
def feedback_acknowledge(attempt_id: str, request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    user = request.state.user
    if user["role"] != "student":
        raise HTTPException(403, "仅学生需要确认 AI 参与说明")
    attempt = _attempt_row(conn, attempt_id)
    _ensure_attempt_access(user, attempt)
    acknowledgement = acknowledge_disclosure(
        conn,
        attempt_id=attempt_id,
        user_id=user["user_id"],
        actor=user["display_name"],
    )
    return {"ok": True, "acknowledgement": acknowledgement}


class FeedbackSurveyBody(BaseModel):
    rating: int = Field(ge=1, le=5)
    usefulness: int | None = Field(default=None, ge=1, le=10)
    realism: int | None = Field(default=None, ge=1, le=10)
    participant_label: str = Field(default="", max_length=40)


@app.post("/api/feedback/{attempt_id}/survey")
def feedback_survey(
    attempt_id: str,
    body: FeedbackSurveyBody,
    request: Request,
    conn=Depends(get_conn),
):
    _ensure_not_stopped(conn)
    attempt = _attempt_row(conn, attempt_id)
    _ensure_attempt_access(request.state.user, attempt)
    try:
        survey = submit_feedback_survey(
            conn,
            attempt_id=attempt_id,
            rating=body.rating,
            usefulness=body.usefulness,
            realism=body.realism,
            participant_label=body.participant_label,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "survey": survey, "accessibility": feedback_accessibility(conn)}


@app.get("/api/quality/stations")
def quality_stations(conn=Depends(get_conn)):
    return {"stations": station_quality_report(conn)}


@app.get("/api/quality/critical-misses")
def quality_critical_misses(conn=Depends(get_conn), limit: int = Query(50, ge=1, le=200)):
    return critical_misses(conn, limit)


class CriticalMissReviewBody(BaseModel):
    record_id: str = Field(min_length=1, max_length=80)
    root_cause: str
    action: str
    note: str = Field(min_length=6, max_length=500)


@app.post("/api/quality/critical-miss-review")
def quality_critical_miss_review(
    body: CriticalMissReviewBody, request: Request, conn=Depends(get_conn)
):
    try:
        review = record_critical_miss_review(
            conn,
            body.record_id,
            root_cause=body.root_cause,
            action=body.action,
            note=body.note,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "review": review, "state": critical_misses(conn)}


@app.get("/api/quality/station-reviews")
def quality_station_reviews(conn=Depends(get_conn), station_id: str | None = None):
    return {"reviews": list_station_reviews(conn, station_id)}


@app.get("/api/rubric/maintenance")
def rubric_maintenance_report(conn=Depends(get_conn), bundle_id: str | None = None):
    return rubric_maintenance(conn, bundle_id)


class BlueprintDraftBody(BaseModel):
    year: int = Field(ge=2020, le=2100)
    domains: list[dict[str, Any]] = Field(min_length=1, max_length=12)
    note: str | None = Field(default=None, max_length=300)


class BlueprintApproveBody(BaseModel):
    note: str = Field(min_length=4, max_length=300)


@app.get("/api/blueprint")
def blueprint_overview(conn=Depends(get_conn)):
    return blueprint_state(conn)


@app.post("/api/blueprint/draft")
def blueprint_draft(body: BlueprintDraftBody, request: Request, conn=Depends(get_conn)):
    try:
        blueprint = draft_blueprint(
            conn,
            year=body.year,
            domains=body.domains,
            note=body.note,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "blueprint": blueprint, "state": blueprint_state(conn)}


@app.post("/api/blueprint/{blueprint_id}/approve")
def blueprint_approve(
    blueprint_id: str,
    body: BlueprintApproveBody,
    request: Request,
    conn=Depends(get_conn),
):
    try:
        blueprint = approve_blueprint(
            conn,
            blueprint_id,
            note=body.note,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "blueprint": blueprint, "state": blueprint_state(conn)}


class StationReviewBody(BaseModel):
    station_id: str = Field(min_length=1, max_length=60)
    flag: str = Field(default="other", pattern=r"^(ceiling|floor|low_discrimination|sample_insufficient|other)$")
    decision: str = Field(pattern=r"^(rubric_revision|sp_retraining|teaching|monitor|no_action)$")
    note: str = Field(min_length=1, max_length=300)
    bundle_id: str | None = None


@app.post("/api/quality/station-review")
def quality_station_review(body: StationReviewBody, request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    try:
        review = record_station_review(
            conn,
            station_id=body.station_id,
            flag=body.flag,
            decision=body.decision,
            note=body.note,
            bundle_id=body.bundle_id,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "review": review, "reviews": list_station_reviews(conn, limit=20)}


@app.get("/api/transparency")
def transparency(conn=Depends(get_conn)):
    return public_transparency(conn)


@app.get("/api/quality")
def quality(conn=Depends(get_conn)):
    report = quality_report(conn)
    bundles_list = list_bundles(conn)
    gates = evaluate_gates(conn, report, _stage(conn))
    model_cards = db.rows_to_dicts(conn.execute("SELECT * FROM model_cards ORDER BY created_at DESC").fetchall())
    for card in model_cards:
        card["metrics"] = json.loads(card["metrics"])
    calls = db.rows_to_dicts(conn.execute("SELECT * FROM ai_calls ORDER BY created_at DESC LIMIT 400").fetchall())
    latencies = sorted(c["latency_ms"] for c in calls)
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
    input_tokens = sum(c["input_tokens"] or 0 for c in calls)
    output_tokens = sum(c["output_tokens"] or 0 for c in calls)
    total_cost = round(sum(c["cost_usd"] or 0 for c in calls), 6)
    cost_by_capability: dict[str, float] = {}
    for call in calls:
        key = call["capability"] or "unknown"
        cost_by_capability[key] = round(cost_by_capability.get(key, 0.0) + (call["cost_usd"] or 0), 6)
    latest_bundle = bundles_list[0] if bundles_list else None
    item_quality = (
        {
            "bundle_id": latest_bundle["bundle_id"],
            "station_id": latest_bundle["station_id"],
            "title": latest_bundle["title"],
            "version": latest_bundle["version"],
            "items": item_quality_report(conn, latest_bundle["bundle_id"]),
        }
        if latest_bundle
        else None
    )
    return {
        "report": report,
        "gates": gates,
        "model_cards": model_cards,
        "item_quality": item_quality,
        "psychometrics": station_psychometrics(conn),
        "station_reviews": list_station_reviews(conn, limit=20),
        "hint_impact": hint_impact(conn),
        "coverage": blueprint_coverage(conn),
        "feedback_latency": feedback_ack_latency(conn),
        "ai_calls": {
            "count": len(calls),
            "p95_latency_ms": p95,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "by_capability": _count_by(calls, "capability"),
            "by_status": _count_by(calls, "status"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_cost_usd": total_cost,
            "priced_calls": sum(1 for c in calls if c["cost_usd"] is not None),
            "cost_by_capability": cost_by_capability,
            "cost_per_attempt": ai_cost_summary(conn),
            "pricing_configured": any(
                [AI_INPUT_PRICE_PER_1K, AI_OUTPUT_PRICE_PER_1K, ASR_PRICE_PER_MINUTE]
            ),
        },
        "overrides": list_overrides(conn),
        "sp_certification": certification_for_bundle(conn, bundles_list[0])
        if bundles_list
        else None,
        "fairness": fairness_report(conn),
        "ab": ab_report(conn),
        "latest_self_check": latest_self_check(conn),
        "governance": governance_status(conn),
        "ai_provider": scoring_provider_info(),
        "asr_provider": build_asr_provider().info,
        "sus": sus_summary(conn),
        "asr_quality": benchmark_summary(conn),
        "calibration": calibration_report(conn),
        "audit_anchors": db.list_audit_anchors(conn, limit=5),
        "versions": {
            "model": db.get_state(conn, "model_version", MODEL_VERSION),
            "prompt": db.get_state(conn, "prompt_version", PROMPT_VERSION),
            "sp_model": db.get_state(conn, "sp_model_version", SP_MODEL_VERSION),
            "sp_prompt": db.get_state(conn, "sp_prompt_version", SP_PROMPT_VERSION),
        },
    }


class SusBody(BaseModel):
    answers: list[int]
    participant_label: str | None = None


MAX_GOLD_BYTES = 5 * 1024 * 1024


@app.post("/api/calibration/import")
async def calibration_import(
    request: Request,
    file: UploadFile = File(...),
    conn=Depends(get_conn),
):
    _ensure_not_stopped(conn)
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "金标准文件为空")
    if len(raw) > MAX_GOLD_BYTES:
        raise HTTPException(413, "金标准文件超过 5MB 限制")
    try:
        rows = parse_gold_file(file.filename or "gold.csv", raw)
        summary = import_gold_scores(
            conn,
            rows,
            actor=request.state.user["display_name"],
            filename=file.filename or "gold.csv",
            file_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **summary, "report": calibration_report(conn)}


@app.get("/api/calibration/report")
def calibration_report_api(conn=Depends(get_conn)):
    return calibration_report(conn)


@app.post("/api/quality/sus")
def quality_sus(body: SusBody, request: Request, conn=Depends(get_conn)):
    try:
        record = submit_sus(
            conn,
            answers=body.answers,
            participant_label=(body.participant_label or "").strip()[:40] or None,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "response": record, "sus": sus_summary(conn)}


class AsrBenchmarkBody(BaseModel):
    label: str = Field(default="ASR 样本", max_length=80)
    reference: str = Field(min_length=1, max_length=MAX_BENCHMARK_CHARS)
    hypothesis: str = Field(min_length=1, max_length=MAX_BENCHMARK_CHARS)
    terms: list[str] = Field(default_factory=list)
    actor: str = Field(default="ASR 评测员", max_length=40)


@app.post("/api/asr/benchmarks")
def asr_benchmark(body: AsrBenchmarkBody, conn=Depends(get_conn)):
    try:
        record = record_benchmark(
            conn,
            label=body.label,
            reference=body.reference,
            hypothesis=body.hypothesis,
            terms=body.terms,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "benchmark": record, "quality": benchmark_summary(conn)}


@app.get("/api/asr/benchmarks")
def asr_benchmarks(limit: int = Query(20, ge=1, le=100), conn=Depends(get_conn)):
    return {"benchmarks": list_benchmarks(conn, limit=limit), "quality": benchmark_summary(conn)}


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


@app.get("/api/fairness")
def fairness(conn=Depends(get_conn), bundle_id: str | None = None):
    return fairness_report(conn, bundle_id)


@app.get("/api/ab")
def ab(conn=Depends(get_conn)):
    return ab_report(conn)


@app.get("/api/schedule/inputs")
def get_schedule_inputs(conn=Depends(get_conn)):
    ensure_schedule_inputs(conn)
    return schedule_inputs(conn)


class ScheduleBlackoutBody(BaseModel):
    station_id: str = Field(min_length=1, max_length=40)
    from_round: int = Field(ge=1, le=50)
    to_round: int = Field(ge=1, le=50)
    reason: str = Field(min_length=4, max_length=200)


@app.get("/api/schedule/blackouts")
def get_schedule_blackouts(conn=Depends(get_conn)):
    ensure_schedule_inputs(conn)
    return {"blackouts": list_blackouts(conn)}


@app.post("/api/schedule/blackouts")
def create_schedule_blackout(
    body: ScheduleBlackoutBody, request: Request, conn=Depends(get_conn)
):
    ensure_schedule_inputs(conn)
    try:
        blackout = add_blackout(
            conn,
            station_id=body.station_id,
            from_round=body.from_round,
            to_round=body.to_round,
            reason=body.reason,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "blackout": blackout, "blackouts": list_blackouts(conn)}


@app.delete("/api/schedule/blackouts/{blackout_id}")
def remove_schedule_blackout(
    blackout_id: str, request: Request, conn=Depends(get_conn)
):
    try:
        removed = delete_blackout(
            conn, blackout_id, actor=request.state.user["display_name"]
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "removed": removed, "blackouts": list_blackouts(conn)}


class ScheduleBody(BaseModel):
    exam_date: str | None = None
    rounds: int = 6
    buffer_min: int = 2
    seed: int = 2026
    policy_id: str | None = None


class SchedulePolicyBody(BaseModel):
    preset: str = Field(default="balanced", pattern=r"^(fairness|balanced|efficiency)$")
    fairness_weight: float | None = Field(default=None, ge=0, le=1)
    note: str = Field(min_length=4, max_length=300)


@app.get("/api/schedule/policy")
def schedule_policy(conn=Depends(get_conn)):
    return {"latest": latest_policy(conn), "presets": presets_payload()}


@app.post("/api/schedule/policy")
def confirm_schedule_policy(
    body: SchedulePolicyBody, request: Request, conn=Depends(get_conn)
):
    try:
        policy = confirm_policy(
            conn,
            preset=body.preset,
            fairness_weight=body.fairness_weight,
            note=body.note,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "policy": policy}


@app.post("/api/schedule/generate")
def gen_schedule(body: ScheduleBody, conn=Depends(get_conn)):
    if not body.policy_id:
        raise HTTPException(400, "生成排考前必须先确认软约束权重")
    policy = get_policy(conn, body.policy_id)
    if not policy:
        raise HTTPException(400, "软约束权重确认记录不存在")
    try:
        schedule = generate_schedule(
            conn,
            exam_date=body.exam_date or date.today().isoformat(),
            rounds=body.rounds,
            buffer_min=body.buffer_min,
            seed=body.seed,
            fairness_weight=policy["fairness_weight"],
            policy=policy,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "schedule": schedule,
        "policy": policy,
        "special_needs": special_needs_status(conn, schedule),
    }


@app.get("/api/schedule/latest")
def get_latest_schedule(conn=Depends(get_conn)):
    schedule = latest_schedule(conn)
    return {
        "schedule": schedule,
        "policy": latest_policy(conn),
        "special_needs": special_needs_status(conn, schedule) if schedule else None,
    }


class ScheduleConflictBody(BaseModel):
    type: str = Field(pattern=r"^(candidate_round|station_round)$")
    round: int = Field(ge=1, le=50)
    candidate_id: str | None = None
    station_id: str | None = None


class ScheduleRepairBody(BaseModel):
    schedule_id: str | None = None
    conflicts: list[ScheduleConflictBody] = Field(min_length=1, max_length=50)


@app.post("/api/schedule/repair")
def repair_schedule_conflicts(
    body: ScheduleRepairBody, request: Request, conn=Depends(get_conn)
):
    try:
        schedule = repair_schedule(
            conn,
            conflicts=[item.model_dump() for item in body.conflicts],
            schedule_id=body.schedule_id,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "schedule": schedule,
        "report": schedule["repair_report"],
        "special_needs": special_needs_status(conn, schedule),
    }


class SpecialNeedsBody(BaseModel):
    schedule_id: str | None = None
    candidate_id: str = Field(min_length=1, max_length=40)
    decision: str = Field(pattern=r"^(approved|rejected)$")
    note: str = Field(default="", max_length=300)


@app.post("/api/schedule/special-needs")
def approve_special_needs(
    body: SpecialNeedsBody, request: Request, conn=Depends(get_conn)
):
    try:
        status = record_special_needs_approval(
            conn,
            candidate_id=body.candidate_id,
            decision=body.decision,
            note=body.note,
            actor=request.state.user["display_name"],
            schedule_id=body.schedule_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "special_needs": status}


@app.get("/api/execution/latest")
def get_latest_execution(conn=Depends(get_conn)):
    run = latest_run(conn)
    return {"run": run, "schedule_available": latest_schedule(conn) is not None, "emergency_stop": emergency_stop_active(conn)}


class RunStartBody(BaseModel):
    schedule_id: str | None = None
    actor: str = "考务"


@app.post("/api/execution/start")
def start_execution(body: RunStartBody, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    try:
        run = start_run(conn, schedule_id=body.schedule_id, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "run": run}


@app.get("/api/execution/{run_id}")
def get_execution(run_id: str, conn=Depends(get_conn)):
    try:
        return {"run": get_run(conn, run_id)}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/execution/{run_id}/export")
def execution_export(run_id: str, request: Request, conn=Depends(get_conn)):
    try:
        return build_run_package(
            conn, run_id, exported_by=request.state.user["display_name"]
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/execution/{run_id}/advance")
def advance_execution(run_id: str, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    try:
        run = advance_run(conn, run_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "run": run}


class SlotActionBody(BaseModel):
    action: str
    note: str = ""
    actor: str = "考务"
    compensation: str | None = None


@app.post("/api/execution/{run_id}/slots/{slot_key}/action")
def execution_slot_action(run_id: str, slot_key: str, body: SlotActionBody, conn=Depends(get_conn)):
    try:
        run = slot_action(
            conn,
            run_id,
            slot_key,
            action=body.action,
            note=body.note,
            actor=body.actor,
            compensation=body.compensation,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "run": run}


class HintsBody(BaseModel):
    enabled: bool
    station_id: str | None = None
    actor: str = "考务"


@app.post("/api/execution/{run_id}/hints")
def execution_hints(run_id: str, body: HintsBody, conn=Depends(get_conn)):
    try:
        if body.station_id:
            run = toggle_station_hints(
                conn,
                run_id,
                body.station_id,
                enabled=body.enabled,
                actor=body.actor,
            )
        else:
            run = toggle_hints(conn, run_id, enabled=body.enabled, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "run": run}


class RunStopBody(BaseModel):
    reason: str = "人工终止"
    actor: str = "考务"


@app.post("/api/execution/{run_id}/stop")
def stop_execution(run_id: str, body: RunStopBody, conn=Depends(get_conn)):
    try:
        run = stop_run(conn, run_id, reason=body.reason, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "run": run}


class GovernanceBody(BaseModel):
    apply: bool = True
    actor: str = "系统"


@app.post("/api/governance/self-check")
def governance_self_check(body: GovernanceBody, conn=Depends(get_conn)):
    result = run_self_check(conn, actor=body.actor, apply=body.apply)
    return {"ok": True, **result}


class TerminationBody(BaseModel):
    active: bool
    reason: str = ""
    actor: str = "考务"


@app.post("/api/governance/termination-switch")
def termination_switch(body: TerminationBody, conn=Depends(get_conn)):
    result = set_emergency_stop(conn, active=body.active, reason=body.reason, actor=body.actor)
    return {"ok": True, **result}


class DrillBody(BaseModel):
    actor: str = "考务"
    attempt_id: str | None = None


@app.post("/api/governance/drills/termination")
def termination_drill(body: DrillBody, conn=Depends(get_conn)):
    return {"ok": True, "drill": run_termination_drill(conn, actor=body.actor)}


@app.post("/api/governance/drills/appeal")
def appeal_drill(body: DrillBody, conn=Depends(get_conn)):
    try:
        result = run_appeal_drill(conn, actor=body.actor, attempt_id=body.attempt_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "drill": result}


class ApprovalBody(BaseModel):
    approval_type: str = "ethics_and_data"
    approver: str = "伦理委员会"
    note: str = ""


@app.post("/api/governance/approvals")
def create_approval(body: ApprovalBody, conn=Depends(get_conn)):
    return {"ok": True, "approval": register_approval(
        conn, approval_type=body.approval_type, approver=body.approver, note=body.note
    )}


class ModelCardBody(BaseModel):
    actor: str = "系统"


@app.post("/api/governance/model-card/refresh")
def refresh_model_card(request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    result = refresh_transparency_assets(
        conn, actor=request.state.user["display_name"]
    )
    return {"ok": True, **result}


@app.post("/api/governance/model-card/publish")
def publish_card(body: ModelCardBody, conn=Depends(get_conn)):
    try:
        result = publish_model_card(conn, actor=body.actor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "model_card": result}


class StageBody(BaseModel):
    stage: str
    actor: str = "系统"
    reason: str = ""


@app.post("/api/governance/release-stage")
def release_stage(body: StageBody, conn=Depends(get_conn)):
    try:
        result = set_release_stage(conn, stage=body.stage, actor=body.actor, reason=body.reason)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **result}


@app.get("/api/governance/status")
def governance(conn=Depends(get_conn)):
    status = governance_status(conn)
    report = quality_report(conn)
    status["gates"] = evaluate_gates(conn, report, _stage(conn))["gates"]
    return status


@app.get("/api/audit")
def audit(conn=Depends(get_conn), limit: int = Query(100, ge=1, le=500)):
    return db.rows_to_dicts(
        conn.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    )


@app.get("/api/audit/search")
def audit_search(
    action: str = "",
    actor: str = "",
    object_id: str = "",
    since: str = "",
    until: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    conn=Depends(get_conn),
):
    return search_audit_events(
        conn,
        action=action.strip(),
        actor=actor.strip(),
        object_id=object_id.strip(),
        since=since.strip(),
        until=until.strip(),
        offset=offset,
        limit=limit,
    )


@app.get("/api/audit/verify")
def audit_verify(conn=Depends(get_conn)):
    return db.verify_audit_chain(conn)


@app.get("/api/audit/anchors")
def audit_anchors(conn=Depends(get_conn)):
    return {
        "head": db.audit_chain_head(conn),
        "anchors": db.list_audit_anchors(conn, limit=50),
    }


@app.post("/api/audit/anchors")
def audit_anchor_create(request: Request, conn=Depends(get_conn)):
    try:
        anchor = db.create_audit_anchor(conn, actor=request.state.user["display_name"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True, "anchor": anchor, "head": db.audit_chain_head(conn)}


@app.get("/api/audit/anchors/verify")
def audit_anchors_verify(conn=Depends(get_conn)):
    return db.verify_audit_anchors(conn)


@app.get("/api/retention")
def retention(conn=Depends(get_conn)):
    return retention_overview(conn)


class RetentionPurgeBody(BaseModel):
    apply: bool = False
    as_of: str | None = None


@app.post("/api/retention/purge")
def retention_purge(body: RetentionPurgeBody, request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    try:
        return purge_expired(
            conn,
            actor=request.state.user["display_name"],
            dry_run=not body.apply,
            as_of=body.as_of,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class SpPersonaApprovalBody(BaseModel):
    bundle_id: str | None = None
    decision: str = Field(default="approved", pattern=r"^(approved|rejected)$")
    scope: str = Field(default="L1", pattern=r"^(L1|L2)$")
    disclosure_approved: bool = False
    forbidden_approved: bool = False
    trial_approved: bool = False
    note: str = Field(default="", max_length=500)
    actor: str = Field(default="考官", max_length=40)


@app.post("/api/sp/persona-approval")
def sp_persona_approval(body: SpPersonaApprovalBody, conn=Depends(get_conn)):
    bundle = _resolve_bundle(conn, body.bundle_id)
    try:
        approval = record_persona_approval(
            conn,
            bundle,
            decision=body.decision,
            scope=body.scope,
            disclosure_approved=body.disclosure_approved,
            forbidden_approved=body.forbidden_approved,
            trial_approved=body.trial_approved,
            note=body.note,
            actor=body.actor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "approval": approval,
        "status": persona_approval_status(conn, bundle),
        "session_ready": sp_session_ready(conn, bundle),
    }


@app.get("/api/sp/persona-approvals")
def sp_persona_approvals(
    bundle_id: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    conn=Depends(get_conn),
):
    return {
        "approvals": list_persona_approvals(conn, bundle_id=bundle_id, limit=limit),
        "status": persona_approval_status(conn, _resolve_bundle(conn, bundle_id)),
    }


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
        "certification": certification_for_bundle(conn, bundle),
        "approval": persona_approval_status(conn, bundle),
    }


class SpSessionBody(BaseModel):
    bundle_id: str | None = None
    candidate_label: str = "练习学生"


@app.post("/api/sp/sessions")
def create_sp_session(body: SpSessionBody, conn=Depends(get_conn)):
    bundle = _resolve_bundle(conn, body.bundle_id)
    if not sp_session_ready(conn, bundle)["ready"]:
        raise HTTPException(409, "当前站包 SP 认证失效或人格卡未获人工批准，请先完成红队认证与考官试演")
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
    _ensure_not_stopped(conn)
    row = conn.execute("SELECT * FROM sp_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        raise HTTPException(404, "会话不存在")
    finished = conn.execute("SELECT 1 FROM sp_reports WHERE session_id = ?", (session_id,)).fetchone()
    if finished:
        raise HTTPException(400, "该练习已生成反馈，请开始新会话")
    session = dict(row)
    bundle = _bundle_or_404(conn, session["bundle_id"])
    if not sp_session_ready(conn, bundle)["ready"]:
        raise HTTPException(409, "当前站包 SP 认证或人格卡批准已失效，练习已暂停；请重新认证并试演后再继续")
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
                "valid_until": row["valid_until"],
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


@app.get("/api/sp/consents")
def sp_consents(conn=Depends(get_conn), bundle_id: str | None = None):
    return consent_status(conn, bundle_id)


class SpConsentBody(BaseModel):
    sp_name: str = Field(min_length=1, max_length=40)
    sp_role: str = Field(pattern=r"^(adult_sp|minor_sp|guardian)$")
    consent_type: str = Field(pattern=r"^(informed_use|minor_guardian|data_use)$")
    version: str = Field(min_length=1, max_length=40)
    bundle_id: str | None = None
    notes: str | None = Field(default=None, max_length=300)


@app.post("/api/sp/consent")
def sp_consent_create(body: SpConsentBody, request: Request, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
    try:
        consent = record_consent(
            conn,
            sp_name=body.sp_name,
            sp_role=body.sp_role,
            consent_type=body.consent_type,
            version=body.version,
            bundle_id=body.bundle_id,
            notes=body.notes,
            actor=request.state.user["display_name"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "consent": consent,
        "status": consent_status(conn, body.bundle_id),
    }


@app.post("/api/sp/sessions/{session_id}/finish")
def finish_sp_session(session_id: str, conn=Depends(get_conn)):
    _ensure_not_stopped(conn)
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
    provider_bundle = build_scoring_provider()
    gateway = CapabilityGateway(
        conn,
        provider_bundle.primary,
        provider_bundle.model_version,
        provider_bundle.prompt_version,
        fallback_provider=provider_bundle.fallback,
    )
    report = build_practice_report(bundle, messages, gateway=gateway)
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
            "ai_versions": report.get("ai_versions"),
        },
        model_version=(report.get("ai_versions") or {}).get("model_version")
        or db.get_state(conn, "sp_model_version", SP_MODEL_VERSION),
        prompt_version=(report.get("ai_versions") or {}).get("prompt_version")
        or db.get_state(conn, "sp_prompt_version", SP_PROMPT_VERSION),
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
