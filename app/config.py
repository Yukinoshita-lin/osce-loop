"""OSCE·Loop 全局配置。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
WEB_DIR = BASE_DIR / "web"

DB_PATH = Path(os.getenv("OSCELOOP_DB", str(DATA_DIR / "osceloop.db")))
# 审计链外部锚点文件：生产环境应指向独立卷/WORM 存储，避免与数据库同盘被一起改写。
AUDIT_ANCHOR_PATH = Path(
    os.getenv("OSCELOOP_AUDIT_ANCHOR_PATH", str(DATA_DIR / "audit_anchors.jsonl"))
)

# AI 输出三元版本戳：任何一个变化都必须重新校准权限（见方案 6.3）
MODEL_VERSION = os.getenv("OSCELOOP_MODEL_VERSION", "mock-score-1.0")
PROMPT_VERSION = os.getenv("OSCELOOP_PROMPT_VERSION", "score-prompt-1.0")
SP_MODEL_VERSION = os.getenv("OSCELOOP_SP_MODEL_VERSION", "mock-sp-1.0")
SP_PROMPT_VERSION = os.getenv("OSCELOOP_SP_PROMPT_VERSION", "sp-prompt-1.0")
# SP 认证有效期：按季度更新红队用例并复测；过期后 G1 的 SP 指标按未采集处理。
SP_CERT_VALID_DAYS = int(os.getenv("OSCELOOP_SP_CERT_VALID_DAYS", "90"))

# 登录防爆破：窗口内失败次数达到阈值即暂时锁定；Cookie Secure 在 HTTPS 部署时开启。
LOGIN_MAX_FAILURES = int(os.getenv("OSCELOOP_LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_SECONDS = int(os.getenv("OSCELOOP_LOGIN_WINDOW_SECONDS", "300"))
SESSION_COOKIE_SECURE = os.getenv("OSCELOOP_COOKIE_SECURE", "0") == "1"

# 评分能力提供方：默认 mock；配置 http 后走 OpenAI 兼容接口。
# L2–L3 真实考生场景应使用私有化/专有云部署，避免音视频与转写出院/校网。
AI_PROVIDER = os.getenv("OSCELOOP_AI_PROVIDER", "mock").strip().lower()
AI_BASE_URL = os.getenv("OSCELOOP_AI_BASE_URL", "").strip()
AI_API_KEY = os.getenv("OSCELOOP_AI_API_KEY", "").strip()
AI_MODEL = os.getenv("OSCELOOP_AI_MODEL", "").strip()
AI_TIMEOUT_SECONDS = float(os.getenv("OSCELOOP_AI_TIMEOUT_SECONDS", "30"))
AI_MAX_RETRIES = int(os.getenv("OSCELOOP_AI_MAX_RETRIES", "1"))
AI_FALLBACK = os.getenv("OSCELOOP_AI_FALLBACK", "mock").strip().lower()
# 成本计量：单价未配置时只记录 Token/时长，不虚报金额。
AI_INPUT_PRICE_PER_1K = float(os.getenv("OSCELOOP_AI_INPUT_PRICE_PER_1K", "0"))
AI_OUTPUT_PRICE_PER_1K = float(os.getenv("OSCELOOP_AI_OUTPUT_PRICE_PER_1K", "0"))
ASR_PRICE_PER_MINUTE = float(os.getenv("OSCELOOP_ASR_PRICE_PER_MINUTE", "0"))

# ASR：默认关闭，需显式配置 OpenAI 兼容 /audio/transcriptions 服务。
# 必须返回带说话人标签的分段转写，否则拒绝自动评分，避免角色错位。
ASR_PROVIDER = os.getenv("OSCELOOP_ASR_PROVIDER", "off").strip().lower()
ASR_BASE_URL = os.getenv("OSCELOOP_ASR_BASE_URL", "").strip()
ASR_API_KEY = os.getenv("OSCELOOP_ASR_API_KEY", "").strip()
ASR_MODEL = os.getenv("OSCELOOP_ASR_MODEL", "").strip()
ASR_LANGUAGE = os.getenv("OSCELOOP_ASR_LANGUAGE", "zh").strip()
ASR_PROMPT_VERSION = os.getenv("OSCELOOP_ASR_PROMPT_VERSION", "asr-transcribe-1.0").strip()
ASR_TIMEOUT_SECONDS = float(os.getenv("OSCELOOP_ASR_TIMEOUT_SECONDS", "60"))
ASR_MAX_RETRIES = int(os.getenv("OSCELOOP_ASR_MAX_RETRIES", "1"))
# 转写平均置信度低于该阈值时，视为 ASR 在该条件下不可靠，整站转人工全量复核。
ASR_LOW_CONFIDENCE = float(os.getenv("OSCELOOP_ASR_LOW_CONFIDENCE", "0.70"))

# 当前能力闸门阶段。P0 为离线历史录像，尚未进入真实考生场景。
RELEASE_STAGE = os.getenv("OSCELOOP_RELEASE_STAGE", "G0_offline")

RISK_LEVELS = ("L0", "L1", "L2", "L3")
RELEASE_STAGES = ("G0_offline", "G1_practice", "G2_simulation", "G3_limited")

SAMPLE_RATES = {"L0": 0.0, "L1": 0.10, "L2": 0.20, "L3": 0.30}
PASS_LINE_MARGIN = 5.0  # 边缘案例：距及格线 ±5%

REASON_LABELS = {
    "integrity_gap": "证据链不完整",
    "asr_low_confidence": "ASR 置信度偏低，整站转人工",
    "critical_negative": "关键项 AI 判阴性",
    "review_disagreement": "双人复核不一致，待高年资裁决",
    "near_cutoff": "总分处于及格线边缘",
    "confidence_below_floor": "置信度低于项阈值",
    "item_disagreement": "该项历史人机分歧偏高",
    "appeal": "考生申诉，全量重评",
    "sampling": "风险抽检命中",
    "manual_only": "该项 AI 无权限，人工评分",
    "extract_only": "AI 仅提取证据，人工评分",
    "ai_refusal": "AI 拒答（无法定位证据）",
    "ai_degraded": "主模型不可用，兜底评分需人工确认",
    "risk_escalation": "风险等级上调，全站人工",
    "l3_human_confirmation": "L3 高利害，AI 建议必须人工确认",
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
