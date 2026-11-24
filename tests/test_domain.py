"""核心领域规则测试：评分表协议、路由、QWK、信息槽与冻结。"""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from app import db
from app.ai.base import Suggestion
from app.ai.pipeline import _assert_schema
from app.domain.bundle import (
    apply_authority_reset,
    diff_payloads,
    effective_authority,
    freeze_bundle,
    get_bundle,
    list_station_bundles,
)
from app.domain.rubric import (
    cap_authority,
    derive_assets,
    is_observable,
    validate_bundle,
    validate_rubric_item,
)
from app.domain.ab import ab_report
from app.domain.fairness import fairness_report
from app.domain.gates import evaluate_gates
from app.domain.governance import (
    emergency_stop_active,
    governance_status,
    publish_model_card,
    register_approval,
    run_appeal_drill,
    run_self_check,
    run_termination_drill,
    set_emergency_stop,
    set_release_stage,
)
from app.domain.practice import build_practice_report
from app.domain.risk import escalate_risk
from app.domain.routing import is_near_cutoff, route_item, sampling_hit
from app.domain.slots import DialogueStateMachine, SPController, scan_leaks
from app.domain.spqa import (
    certify_bundle,
    ensure_certification,
    latest_certification,
    run_consistency,
    run_red_team,
)
from app.domain.stats import quadratic_weighted_kappa, quality_report
from app.seed import build_bundle_payload, seed_if_empty


@pytest.fixture(scope="module")
def seeded_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("osceloop") / "test.db"
    conn = db.connect(path)
    db.init_db(conn)
    seed_if_empty(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(seeded_path):
    connection = db.connect(seeded_path)
    yield connection
    connection.close()


@pytest.fixture()
def bundle(conn):
    row = conn.execute("SELECT bundle_id FROM bundles LIMIT 1").fetchone()
    return get_bundle(conn, row["bundle_id"])


def test_bundle_passes_four_checks():
    payload = build_bundle_payload()
    validation = validate_bundle(payload)
    assert validation["can_freeze"] is True
    assert [c["status"] for c in validation["checks"]] == ["pass", "pass", "pass", "pass"]
    assert validation["counts"]["items"] == 15
    assert validation["counts"]["critical"] == 3


def test_vague_anchor_blocks_freeze():
    payload = build_bundle_payload()
    bad = copy.deepcopy(payload)
    bad["rubric"][2]["anchor"]["observable"] = "具备良好的沟通能力"
    validation = validate_bundle(bad)
    assert validation["can_freeze"] is False
    assert any("不可判定" in issue for issue in validation["blocking"])


def test_incomplete_bundle_payload_is_blocked():
    missing_station = build_bundle_payload()
    missing_station["station_id"] = ""
    validation = validate_bundle(missing_station)
    assert validation["can_freeze"] is False
    assert any("station_id" in issue for issue in validation["blocking"])

    empty_rubric = build_bundle_payload()
    empty_rubric["rubric"] = []
    validation = validate_bundle(empty_rubric)
    assert validation["can_freeze"] is False
    assert any("rubric" in issue for issue in validation["blocking"])


def test_missing_feedback_alternative_blocks_freeze():
    payload = build_bundle_payload()
    payload["rubric"][0]["feedback_alt"] = ""
    validation = validate_bundle(payload)
    assert validation["can_freeze"] is False
    assert any("替代话术" in issue for issue in validation["blocking"])


def test_diff_payloads_reports_station_and_rubric_changes():
    base = build_bundle_payload()
    proposal = copy.deepcopy(base)
    proposal["title"] = "急性胸痛：修订版"
    proposal["rubric"][0]["anchor"]["positive"] += "，并复述患者姓名"
    proposal["rubric"].pop(1)
    added_item = copy.deepcopy(base["rubric"][2])
    added_item["item_id"] = "NEW.01"
    proposal["rubric"].append(added_item)

    diff = diff_payloads(base, proposal)
    assert diff["has_changes"] is True
    assert diff["summary"]["station_changes"] == 1
    assert diff["summary"]["changed_items"] == 1
    assert diff["summary"]["removed_items"] == 1
    assert diff["summary"]["added_items"] == 1
    assert diff["station"][0]["field"] == "title"
    assert diff["rubric"]["changed"][0]["item_id"] == "OP.01"
    assert "anchor" in diff["rubric"]["changed"][0]["fields"]
    assert diff["rubric"]["removed"] == ["OP.02"]
    assert diff["rubric"]["added"] == ["NEW.01"]
    assert diff_payloads(base, copy.deepcopy(base))["has_changes"] is False


def test_observability_requires_verifiable_verb():
    ok, why = is_observable("询问胸痛的部位与性质")
    assert ok and why == "ok"
    ok, _ = is_observable("理解患者的感受")
    assert not ok


def test_authority_cap_by_risk_and_evidence_type():
    payload = build_bundle_payload()
    items = {item["item_id"]: item for item in payload["rubric"]}
    assert cap_authority(items["HX.01"], "L3", "G0_offline") == "extract"
    assert cap_authority(items["HX.01"], "L3", "G3_limited") == "suggest"
    assert cap_authority(items["HX.04"], "L3", "G3_limited") == "extract"
    assert cap_authority(items["HX.01"], "L2", "G0_offline") == "suggest"
    assert cap_authority(items["CM.01"], "L1", "G1_practice") == "none"
    assert cap_authority(items["AS.01"], "L1", "G1_practice") == "extract"
    assert cap_authority(items["HX.04"], "L1", "G1_practice") == "suggest"


def test_derivation_generates_four_asset_types():
    payload = build_bundle_payload()
    derived = derive_assets(payload)
    assert derived["counts"] == {
        "slots": 16,
        "probes": 15,
        "feedback_templates": 15,
        "checkpoints": 5,
    }
    assert any(s["disclosure"] == "on_trust" for s in derived["slots"])
    assert any(s["disclosure"] == "active" for s in derived["slots"])
    linked = {item for slot in derived["slots"] for item in slot.get("linked_items", [])}
    rubric_ids = {item["item_id"] for item in payload["rubric"] if item["evidence_type"] != "global"}
    assert rubric_ids <= linked


def test_routing_priority_and_sampling():
    item = {
        "item_id": "HX.03",
        "critical": False,
        "confidence_floor": 0.6,
    }
    common = dict(
        record_id="rec_x",
        item=item,
        authority="suggest",
        ai_score=1,
        confidence=0.9,
        risk_level="L2",
        release_stage="G0_offline",
    )
    assert route_item(**{**common, "integrity_gap": True, "near_cutoff": True})["reason"] == "integrity_gap"
    assert route_item(**{**common, "asr_confidence": 0.4})["reason"] == "asr_low_confidence"
    assert route_item(**{**common, "asr_confidence": 0.4})["route"] == "full_station_human"
    assert route_item(**{**common, "asr_confidence": 0.95, "integrity_gap": True})["reason"] == "integrity_gap"
    assert route_item(**{**common, "ai_score": None, "authority": "none"})["reason"] == "manual_only"
    dual = route_item(**{**common, "item": {**item, "critical": True}, "ai_score": 0})
    assert dual["reason"] == "critical_negative"
    assert dual["route"] == "dual_review"
    assert dual["dual_review"]["required"] == 2
    assert route_item(**{**common, "near_cutoff": True})["reason"] == "near_cutoff"
    assert route_item(**{**common, "confidence": 0.3})["reason"] == "confidence_below_floor"
    assert route_item(**{**common, "item_disagreement_rate": 0.2})["reason"] == "item_disagreement"
    assert route_item(**{**common, "appeal": True})["reason"] == "appeal"
    l3 = route_item(**{**common, "risk_level": "L3", "item": {**item, "critical": True}})
    assert l3["reason"] == "l3_human_confirmation"
    auto = None
    for i in range(200):
        rid = f"rec_{i}"
        result = route_item(**{**common, "record_id": rid})
        if result["route"] == "auto":
            auto = result
            break
    assert auto is not None and auto["requires_human"] is False
    assert sampling_hit("rec_1", 0.2) == sampling_hit("rec_1", 0.2)
    assert is_near_cutoff(62.0, 60.0)
    assert not is_near_cutoff(70.0, 60.0)


def test_qwk_values():
    assert quadratic_weighted_kappa([0, 1, 2], [0, 1, 2]) == 1.0
    assert quadratic_weighted_kappa([0, 1, 2], [2, 1, 0]) == -1.0
    mid = quadratic_weighted_kappa([0, 1, 2, 2], [1, 1, 2, 2])
    assert -1.0 < mid < 1.0
    assert quadratic_weighted_kappa([], []) is None


def test_sp_slots_are_gated():
    payload = build_bundle_payload()
    derived = derive_assets(payload)
    machine = DialogueStateMachine(payload, derived)
    state = machine.start()
    assert len(state["released"]) == 1
    assert not any("左边胳膊" in s["content"] for s in machine.slots_by_ids(state["released"]))

    state, released, intent, _ = machine.step_state(state, "疼多久了？")
    assert state["phase"] == "information_gathering"
    assert any(s["slot_id"] in released for s in machine.slots if "两个多小时" in s["content"])

    leaks = scan_leaks("这疼会窜到左边胳膊", machine.unreleased(state))
    assert any(v["kind"] == "information_boundary" for v in leaks)


def test_sp_trust_threshold_releases_slot():
    payload = build_bundle_payload()
    derived = derive_assets(payload)
    machine = DialogueStateMachine(payload, derived)
    controller = SPController(machine)
    state = machine.start()
    result = controller.respond(state, "我理解您现在很担心")
    state = result["state"]
    assert state["trust"] == 1
    assert not any("瘦了五六斤" in s["content"] for s in machine.slots_by_ids(state["released"]))
    result = controller.respond(state, "您别着急，我会陪着您")
    state = result["state"]
    assert state["trust"] == 2
    assert any("瘦了五六斤" in s["content"] for s in machine.slots_by_ids(state["released"]))


def test_sp_verifier_blocks_leak():
    payload = build_bundle_payload()
    derived = derive_assets(payload)
    machine = DialogueStateMachine(payload, derived)
    controller = SPController(machine)
    state = machine.start()
    verification = controller.verify(
        "唉，医生，这疼会窜到左边胳膊。",
        machine.persona,
        machine.unreleased(state),
    )
    assert verification["boundary"] == 0
    assert verification["passed"] is False
    assert verification["repairable"] is False


def test_ai_score_without_evidence_is_rejected():
    response = {
        "output": Suggestion(item_id="HX.01", score=2, confidence=0.9, evidence=[]),
        "model_version": "m1",
        "prompt_version": "p1",
    }
    with pytest.raises(ValueError):
        _assert_schema("[]", response, "HX.01", allow_null_score=False)


def test_freeze_is_idempotent_and_hash_bound(conn):
    payload = build_bundle_payload()
    first = freeze_bundle(conn, payload, actor="test", version="1.0")
    count = conn.execute("SELECT COUNT(*) AS n FROM bundles").fetchone()["n"]
    assert count == 1
    assert first["bundle_hash"].startswith("sha256:")
    same = freeze_bundle(conn, payload, actor="test", version="1.0")
    assert same["bundle_id"] == first["bundle_id"]
    changed = copy.deepcopy(payload)
    changed["title"] = changed["title"] + "（修订）"
    second = freeze_bundle(conn, changed, actor="test", version="1.1", supersedes=first["bundle_id"], reason="改标题")
    assert second["bundle_hash"] != first["bundle_hash"]
    assert second["supersedes"] == first["bundle_id"]
    assert conn.execute("SELECT COUNT(*) AS n FROM bundles").fetchone()["n"] == 2


def test_freeze_enforces_version_discipline(seeded_path, tmp_path):
    path = tmp_path / "version.db"
    shutil.copy(seeded_path, path)
    conn = db.connect(path)
    try:
        payload = build_bundle_payload()
        first = freeze_bundle(conn, payload, actor="test", version="1.0")
        changed = copy.deepcopy(payload)
        changed["title"] = changed["title"] + "（版本纪律）"

        with pytest.raises(ValueError, match="已存在"):
            freeze_bundle(conn, changed, actor="test", version="1.0")
        with pytest.raises(ValueError, match="必须高于"):
            freeze_bundle(
                conn, changed, actor="test", version="0.9", supersedes=first["bundle_id"], reason="降级"
            )

        other = copy.deepcopy(payload)
        other["station_id"] = "ST-OTHER-001"
        other["title"] = "其他考站"
        other_bundle = freeze_bundle(conn, other, actor="test", version="1.0")
        with pytest.raises(ValueError, match="同一考站"):
            freeze_bundle(
                conn, changed, actor="test", version="1.1", supersedes=other_bundle["bundle_id"], reason="跨站替代"
            )

        bad_version = copy.deepcopy(payload)
        bad_version["title"] = bad_version["title"] + "（非法版本）"
        with pytest.raises(ValueError, match="格式"):
            freeze_bundle(conn, bad_version, actor="test", version="v2")
    finally:
        conn.close()


def test_list_station_bundles_keeps_latest_per_station(seeded_path, tmp_path):
    path = tmp_path / "stations.db"
    shutil.copy(seeded_path, path)
    conn = db.connect(path)
    try:
        payload = build_bundle_payload()
        first = freeze_bundle(conn, payload, actor="test", version="1.0")
        revised = copy.deepcopy(payload)
        revised["title"] = revised["title"] + "（第二版）"
        second = freeze_bundle(
            conn, revised, actor="test", version="2.0", supersedes=first["bundle_id"], reason="修订"
        )
        other = copy.deepcopy(payload)
        other["station_id"] = "ST-OTHER-001"
        other["title"] = "其他考站"
        other_bundle = freeze_bundle(conn, other, actor="test", version="1.0")

        stations = {item["station_id"]: item for item in list_station_bundles(conn)}
        assert set(stations) == {"ST-CARDIO-001", "ST-OTHER-001"}
        assert stations["ST-CARDIO-001"]["bundle_id"] == second["bundle_id"]
        assert stations["ST-CARDIO-001"]["version"] == "2.0"
        assert stations["ST-OTHER-001"]["bundle_id"] == other_bundle["bundle_id"]
    finally:
        conn.close()


def test_seeded_attempts_and_quality(conn, bundle):
    attempts = db.rows_to_dicts(conn.execute("SELECT * FROM attempts").fetchall())
    assert len(attempts) == 13
    assert sum(1 for a in attempts if a["near_cutoff"]) == 2
    assert sum(1 for a in attempts if a["integrity_gap"]) == 1
    assert sum(1 for a in attempts if a["status"] == "published") == 1
    report = quality_report(conn)
    assert report["qwk_ai"] is not None and report["qwk_ai"] >= 0.7
    assert report["evidence"]["rate"] >= 0.9
    reasons = {r["reason"] for r in report["routing"]}
    assert {"near_cutoff", "critical_negative", "integrity_gap", "appeal"} <= reasons
    shares = {row["risk_level"]: row for row in report["review_share"]["by_risk"]}
    assert shares["L2"]["target"] == 0.35
    assert 0 <= report["review_share"]["overall"]["share"] <= 1
    assert bundle["derived"]["counts"]["slots"] == 16


def test_model_version_change_downgrades_authority(seeded_path, tmp_path):
    path = tmp_path / "governance.db"
    shutil.copy(seeded_path, path)
    conn = db.connect(path)
    bundle = get_bundle(conn, conn.execute("SELECT bundle_id FROM bundles LIMIT 1").fetchone()["bundle_id"])
    item = next(i for i in bundle["payload"]["rubric"] if i["item_id"] == "HX.01")
    assert effective_authority(conn, bundle, item, "L2", "G0_offline") == "suggest"
    changes = apply_authority_reset(
        conn,
        reason="测试版本变更",
        actor="test",
        model_version="mock-2.0",
        prompt_version="prompt-2.0",
    )
    assert changes
    assert effective_authority(conn, bundle, item, "L2", "G0_offline") == "extract"
    audit = conn.execute("SELECT COUNT(*) AS n FROM audit_events WHERE action = 'authority.auto_downgrade'").fetchone()
    assert audit["n"] == 1
    conn.close()


def test_sp_red_team_passes_and_has_no_leaks(bundle):
    report = run_red_team(bundle)
    assert report["case_count"] == 7
    assert report["passed_cases"] == 7
    assert report["leak_count"] == 0
    assert report["fact_errors"] == 0
    assert report["p0_incidents"] == 0
    assert report["persona_first_pass_rate"] >= 0.95


def test_sp_consistency_is_deterministic(bundle):
    report = run_consistency(bundle, runs=20)
    assert report["rate"] >= 0.95
    assert all(p["runs"] == 20 for p in report["paths"])
    assert all(p["unique_sequences"] == 1 for p in report["paths"])


def test_sp_certification_is_persisted(conn, bundle):
    report = certify_bundle(conn, bundle, actor="测试认证员")
    assert report["status"] == "certified"
    assert all(check["passed"] for check in report["checks"])
    latest = latest_certification(conn, bundle["bundle_id"])
    assert latest is not None
    assert latest["red_team"]["leak_count"] == 0
    assert latest["consistency"]["rate"] >= 0.95
    rows = conn.execute("SELECT COUNT(*) AS n FROM sp_certifications").fetchone()["n"]
    assert rows >= 1


def test_sp_generator_focuses_newly_released_slot(bundle):
    machine = DialogueStateMachine(bundle["payload"], bundle["derived"])
    controller = SPController(machine)
    state = machine.start()
    result = controller.respond(state, "疼多久了？")
    assert "两个多小时" in result["reply"]
    assert "王建国" not in result["reply"]


def test_practice_report_has_three_part_feedback(bundle):
    messages = [
        {"role": "patient", "text": "大夫，我胸口疼。", "turn": 0, "phase": "opening", "at_ms": 0},
        {"role": "candidate", "text": "您好，我是急诊科张医生，请问您怎么称呼？", "turn": 1, "phase": "opening", "at_ms": 6000},
        {"role": "patient", "text": "我叫王建国。", "turn": 1, "phase": "information_gathering", "at_ms": 10000},
        {"role": "candidate", "text": "疼多久了？有没有向肩膀放射？", "turn": 2, "phase": "information_gathering", "at_ms": 16000},
        {"role": "patient", "text": "两个多小时了，会窜到左边胳膊。", "turn": 2, "phase": "information_gathering", "at_ms": 20000},
        {"role": "candidate", "text": "有没有出汗、恶心？", "turn": 3, "phase": "information_gathering", "at_ms": 26000},
        {"role": "patient", "text": "出了不少汗，有点恶心。", "turn": 3, "phase": "information_gathering", "at_ms": 30000},
        {"role": "candidate", "text": "我先给您听诊一下，然后做心电图。", "turn": 4, "phase": "assessment", "at_ms": 36000},
    ]
    report = build_practice_report(bundle, messages)
    assert report["total"] is not None
    assert report["covered"] >= 4
    assert report["uncovered_count"] > 0
    assert report["deductions"]
    for unit in report["deductions"]:
        assert unit["behavior"]
        assert unit["alternative"]
        assert "evidence" in unit
    assert report["disclaimer"]


def test_gate_g1_uses_sp_certification(conn):
    bundle_row = conn.execute(
        "SELECT bundle_id FROM bundles ORDER BY frozen_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    certify_bundle(conn, get_bundle(conn, bundle_row["bundle_id"]), actor="test")
    report = quality_report(conn)
    gates = evaluate_gates(conn, report, "G0_offline")
    g1 = next(g for g in gates["gates"] if g["id"] == "G1")
    values = {c["key"]: c["value"] for c in g1["criteria"]}
    assert values["sp_fact_error"] is not None
    assert values["persona_pass"] is not None
    assert values["p95_latency"] is not None
    assert values["p0_incidents"] == 0
    assert values["sus"] == 80.0


def test_fairness_report_has_groups_and_corrective_actions(conn):
    report = fairness_report(conn)
    assert report["sample"]["attempts"] == 13
    assert report["overall_gap_sd"] is not None
    assert len(report["dimensions"]) == 3
    for dim in report["dimensions"]:
        assert dim["groups"]
        assert dim["status"] in ("pass", "fail", "insufficient")
        assert dim["corrective_action"]
    assert len(report["edit_rate"]["dimensions"]) == 3
    assert report["feedback_accessibility"]["status"] == "unmeasured"


def test_ab_report_compares_groups(conn):
    report = ab_report(conn)
    groups = {g["key"]: g for g in report["groups"]}
    assert groups["ai_assisted"]["attempts"] == 7
    assert groups["human_only"]["attempts"] == 6
    assert groups["ai_assisted"]["items"] == 105
    assert groups["human_only"]["items"] == 90
    assert report["comparison"]["teacher_hours_reduction"] is not None
    assert report["comparison"]["teacher_hours_reduction"] >= 0.40
    assert report["comparison"]["near_improvement"] is not None
    assert report["comparison"]["near_improvement"] > 0
    assert groups["ai_assisted"]["teacher_hours"] < groups["human_only"]["teacher_hours"]


def test_g2_gate_uses_ab_and_fairness_metrics(conn):
    register_approval(
        conn,
        approval_type="academic_committee",
        approver="学术委员会",
        note="A/B 报告审议（测试）",
    )
    report = quality_report(conn)
    gates = evaluate_gates(conn, report, "G0_offline")
    g2 = next(g for g in gates["gates"] if g["id"] == "G2")
    values = {c["key"]: c["value"] for c in g2["criteria"]}
    assert values["teacher_hours"] is not None
    assert values["near_improvement"] is not None
    assert values["subgroup_gap"] is not None
    assert values["qwk_ai"] is not None
    assert values["academic_committee"] == 1.0
    assert g2["status"] == "pass"


def test_risk_escalation_is_upward_only(seeded_path, tmp_path):
    path = tmp_path / "risk.db"
    shutil.copy(seeded_path, path)
    conn = db.connect(path)
    result = escalate_risk(
        conn,
        attempt_id="att_2026_001",
        new_level="L3",
        reason="测试风险升级",
        actor="考务",
    )
    assert result["old_level"] == "L2"
    assert result["new_level"] == "L3"
    attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id = 'att_2026_001'").fetchone()
    assert attempt["risk_level"] == "L3"
    assert attempt["status"] == "pending_review"
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = 'att_2026_001' "
        "AND status IN ('pending_review','manual_pending')"
    ).fetchone()["n"]
    assert pending == 15
    finals = conn.execute(
        "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = 'att_2026_001' AND final_score IS NOT NULL"
    ).fetchone()["n"]
    assert finals == 0
    with pytest.raises(ValueError):
        escalate_risk(conn, attempt_id="att_2026_001", new_level="L2", reason="试图降级", actor="考务")
    with pytest.raises(ValueError):
        escalate_risk(conn, attempt_id="att_2026_002", new_level="L3", reason="已发布", actor="考务")
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'attempt.risk_escalation'"
    ).fetchone()["n"]
    assert audit == 1
    conn.close()


def test_governance_self_check_passes_core_metrics(conn):
    result = run_self_check(conn, actor="test", apply=True)
    checks = {c["key"]: c for c in result["checks"]}
    assert checks["qwk"]["status"] == "pass"
    assert checks["subgroup_gap"]["status"] == "pass"
    assert checks["teacher_hours"]["status"] == "pass"
    assert checks["version_stamp"]["status"] == "pass"
    assert result["applied_changes"] == []


def test_governance_auto_downgrade_on_low_qwk(seeded_path, tmp_path):
    path = tmp_path / "governance_low_qwk.db"
    shutil.copy(seeded_path, path)
    conn = db.connect(path)
    conn.execute(
        "UPDATE score_records SET ai_score = CASE WHEN ai_score IS NULL THEN NULL ELSE 2 - ai_score END"
    )
    conn.commit()
    result = run_self_check(conn, actor="test", apply=True)
    checks = {c["key"]: c for c in result["checks"]}
    assert checks["qwk"]["status"] == "fail"
    assert result["applied_changes"]
    overrides = conn.execute("SELECT COUNT(*) AS n FROM authority_overrides").fetchone()["n"]
    assert overrides > 0
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'authority.auto_downgrade'"
    ).fetchone()["n"]
    assert audit >= 1
    conn.close()


def test_l3_routing_requires_human_confirmation():
    item = {"item_id": "HX.01", "critical": False, "confidence_floor": 0.5}
    result = route_item(
        record_id="rec_l3",
        item=item,
        authority="suggest",
        ai_score=2,
        confidence=0.95,
        risk_level="L3",
        release_stage="G3_limited",
    )
    assert result["reason"] == "l3_human_confirmation"
    assert result["requires_human"] is True
    critical_suggest = route_item(
        record_id="rec_l3c_suggest",
        item={**item, "critical": True},
        authority="suggest",
        ai_score=2,
        confidence=0.95,
        risk_level="L3",
        release_stage="G3_limited",
    )
    assert critical_suggest["route"] == "dual_review"
    assert critical_suggest["dual_review"]["required"] == 2
    critical = {**item, "critical": True}
    result = route_item(
        record_id="rec_l3c",
        item=critical,
        authority="extract",
        ai_score=None,
        confidence=None,
        risk_level="L3",
        release_stage="G3_limited",
    )
    assert result["reason"] == "extract_only"


def test_termination_and_appeal_drills(conn):
    drill = run_termination_drill(conn, actor="test")
    assert drill["passed"] is True
    set_emergency_stop(conn, active=True, reason="测试", actor="test")
    assert emergency_stop_active(conn) is True
    set_emergency_stop(conn, active=False, reason="测试结束", actor="test")
    assert emergency_stop_active(conn) is False
    for _ in range(2):
        appeal = run_appeal_drill(conn, actor="test")
        assert appeal["passed"] is True
    status = governance_status(conn)
    assert status["switch_drill"]["passed"] == 1
    assert status["appeal_drills"]["passed"] >= 2


def test_p3_readiness_and_release(seeded_path, tmp_path):
    path = tmp_path / "p3.db"
    shutil.copy(seeded_path, path)
    conn = db.connect(path)
    run_termination_drill(conn, actor="test")
    run_appeal_drill(conn, actor="test")
    run_appeal_drill(conn, actor="test")
    register_approval(
        conn,
        approval_type="ethics_and_data",
        approver="伦理委员会",
        note="P3 测试批准",
    )
    register_approval(
        conn,
        approval_type="academic_committee",
        approver="学术委员会",
        note="A/B 报告审议（P3 测试）",
    )
    publish_model_card(conn, actor="test")
    bundle_row = conn.execute(
        "SELECT bundle_id FROM bundles ORDER BY frozen_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    certify_bundle(conn, get_bundle(conn, bundle_row["bundle_id"]), actor="test")
    report = quality_report(conn)
    gates = evaluate_gates(conn, report, "G0_offline")
    g3 = next(g for g in gates["gates"] if g["id"] == "G3")
    assert g3["status"] == "pass", {c["key"]: c["value"] for c in g3["criteria"]}
    result = set_release_stage(
        conn,
        stage="G3_limited",
        actor="test",
        reason="P3 测试放行",
    )
    assert result["new_stage"] == "G3_limited"
    assert db.get_state(conn, "release_stage") == "G3_limited"
    bundle = get_bundle(conn, bundle_row["bundle_id"])
    item = next(i for i in bundle["payload"]["rubric"] if i["item_id"] == "HX.01")
    assert effective_authority(conn, bundle, item, "L3", "G3_limited") == "suggest"
    critical = next(i for i in bundle["payload"]["rubric"] if i["item_id"] == "HX.04")
    assert effective_authority(conn, bundle, critical, "L3", "G3_limited") == "extract"
    conn.close()
