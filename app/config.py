"""OSCE·Loop 全局配置。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
WEB_DIR = BASE_DIR / "web"

DB_PATH = Path(os.getenv("OSCELOOP_DB", str(DATA_DIR / "osceloop.db")))

# AI 输出三元版本戳：任何一个变化都必须重新校准权限（见方案 6.3）
MODEL_VERSION = os.getenv("OSCELOOP_MODEL_VERSION", "mock-score-1.0")
PROMPT_VERSION = os.getenv("OSCELOOP_PROMPT_VERSION", "score-prompt-1.0")
SP_MODEL_VERSION = os.getenv("OSCELOOP_SP_MODEL_VERSION", "mock-sp-1.0")
SP_PROMPT_VERSION = os.getenv("OSCELOOP_SP_PROMPT_VERSION", "sp-prompt-1.0")

# 当前能力闸门阶段。P0 为离线历史录像，尚未进入真实考生场景。
RELEASE_STAGE = os.getenv("OSCELOOP_RELEASE_STAGE", "G0_offline")

RISK_LEVELS = ("L0", "L1", "L2", "L3")
RELEASE_STAGES = ("G0_offline", "G1_practice", "G2_simulation", "G3_limited")

SAMPLE_RATES = {"L0": 0.0, "L1": 0.10, "L2": 0.20, "L3": 0.30}
PASS_LINE_MARGIN = 5.0  # 边缘案例：距及格线 ±5%

REASON_LABELS = {
    "integrity_gap": "证据链不完整",
    "critical_negative": "关键项 AI 判阴性",
    "near_cutoff": "总分处于及格线边缘",
    "confidence_below_floor": "置信度低于项阈值",
    "item_disagreement": "该项历史人机分歧偏高",
    "appeal": "考生申诉，全量重评",
    "sampling": "风险抽检命中",
    "manual_only": "该项 AI 无权限，人工评分",
    "extract_only": "AI 仅提取证据，人工评分",
    "ai_refusal": "AI 拒答（无法定位证据）",
    "risk_escalation": "风险等级上调，全站人工",
}

ROUTE_LABELS = {
    "full_station_human": "全站人工",
    "dual_review": "双人复核",
    "senior_arbitration": "高年资裁决",
    "send_review": "送审",
    "full_reevaluation": "全量重评",
    "manual": "人工评分",
    "sampling": "抽检",
    "auto": "自动通过",
}
