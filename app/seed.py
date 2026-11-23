"""演示数据种子：一个「急性胸痛」站 + 13 场历史录像的离线评分。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import wave
from array import array
from pathlib import Path
from typing import Any

from . import db
from .ai.base import CapabilityGateway
from .ai.mock import MockScoringProvider
from .ai.pipeline import run_scoring
from .config import (
    MEDIA_DIR,
    MODEL_VERSION,
    PROMPT_VERSION,
    SP_MODEL_VERSION,
    SP_PROMPT_VERSION,
)
from .domain.bundle import apply_authority_reset, freeze_bundle
from .domain.rubric import PHASE_RANK
from .domain.stats import quality_report
from .domain.sus import compute_sus

MEDIA_ID = "v_demo_chest_pain"
MEDIA_FILE = MEDIA_DIR / "demo_chest_pain.wav"
MEDIA_URL = "/media/demo_chest_pain.wav"
AUDIO_SECONDS = 180

GUIDELINE_KB = [
    {
        "ref": "ESC-ACS-2023 §3.2",
        "text": "胸痛评估应采集疼痛性质、部位、持续时间、放射部位及伴随症状。",
    },
    {
        "ref": "ESC-ACS-2023 §4.1",
        "text": "疑似急性冠脉综合征应在 10 分钟内完成 12 导联心电图。",
    },
    {
        "ref": "中国胸痛中心规范 2023 §5",
        "text": "胸痛患者应评估生命体征、危险因素与用药史，并尽早完成风险分层。",
    },
]


def _item(
    *,
    item_id: str,
    seq: int,
    domain: str,
    title: str,
    observable: str,
    positive: str,
    partial: str,
    negative: str,
    evidence_type: str,
    phase: str,
    weight: float,
    authority: str,
    guideline: str,
    critical: bool = False,
    confidence_floor: float = 0.55,
    slot: dict[str, Any] | None = None,
    probe: dict[str, Any] | None = None,
    feedback_alt: str = "",
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "seq": seq,
        "domain": domain,
        "title": title,
        "anchor": {
            "observable": observable,
            "positive": positive,
            "partial": partial,
            "negative": negative,
        },
        "evidence_type": evidence_type,
        "phase": phase,
        "weight": weight,
        "ai_authority": authority,
        "critical": critical,
        "confidence_floor": confidence_floor,
        "guideline_ref": guideline,
        "slot": slot,
        "probe": probe or {"positive": [], "partial": []},
        "feedback_alt": feedback_alt,
    }


def build_bundle_payload() -> dict[str, Any]:
    rubric = [
        _item(
            item_id="OP.01",
            seq=1,
            domain="communication",
            title="自我介绍与身份确认",
            observable="向患者说明自己是接诊医生，并核对患者姓名",
            positive="主动说明身份（如“我是急诊科医生”），并核对患者姓名",
            partial="只说明身份或只核对姓名之一",
            negative="未说明身份，也未核对姓名",
            evidence_type="utterance",
            phase="opening",
            weight=1,
            authority="suggest",
            guideline="中国胸痛中心规范 2023 §5",
            slot={
                "content": "我叫王建国，今年六十五。",
                "triggers": ["姓名", "怎么称呼", "年龄"],
                "entities": ["王建国", "六十五"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["我是.*医生", "怎么称呼", "姓名", "核对"], "partial": ["您好"]},
            feedback_alt="开场可加一句：“您好，我是急诊科张医生，请问您怎么称呼？”",
        ),
        _item(
            item_id="OP.02",
            seq=2,
            domain="communication",
            title="说明接诊目的并征得配合",
            observable="告知患者将询问胸痛情况并做检查，征得同意",
            positive="说明将询问病史并进行检查，且征得患者同意",
            partial="只说明目的，未明确征得同意",
            negative="未说明接诊目的",
            evidence_type="utterance",
            phase="opening",
            weight=1,
            authority="suggest",
            guideline="中国胸痛中心规范 2023 §5",
            slot={
                "content": "行，你问吧，我配合。",
                "triggers": ["配合", "问吧", "同意"],
                "entities": ["配合"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["了解.*情况", "问.*情况", "做.*检查", "可以吗", "同意"], "partial": ["我们开始"]},
            feedback_alt="可说：“我想先了解您胸痛的情况，再做个体格检查，您看可以吗？”",
        ),
        _item(
            item_id="HX.01",
            seq=3,
            domain="history_taking",
            title="胸痛部位与性质",
            observable="询问胸痛的部位和性质（压榨样/闷痛/刺痛）",
            positive="询问部位并追问性质（如“是压榨样还是刺痛”）",
            partial="只询问部位或只询问性质",
            negative="未询问部位与性质",
            evidence_type="utterance",
            phase="information_gathering",
            weight=2,
            authority="suggest",
            guideline="ESC-ACS-2023 §3.2",
            slot={
                "content": "胸口正中这块，压着疼，像有块石头。",
                "triggers": ["哪里", "部位", "什么样", "性质", "怎么疼"],
                "entities": ["胸口正中", "压着疼", "石头"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["哪里.*疼", "什么位置", "什么性质", "压榨", "闷痛", "怎么个疼"], "partial": ["胸痛"]},
            feedback_alt="可追问：“是压着疼、闷痛，还是像针扎一样？”",
        ),
        _item(
            item_id="HX.02",
            seq=4,
            domain="history_taking",
            title="持续时间与诱因",
            observable="询问胸痛开始时间、持续时长与诱发因素",
            positive="询问开始时间、持续时长，并追问活动等诱因",
            partial="只询问时长或只询问诱因",
            negative="未询问持续时间与诱因",
            evidence_type="utterance",
            phase="information_gathering",
            weight=2,
            authority="suggest",
            guideline="ESC-ACS-2023 §3.2",
            slot={
                "content": "从早上八点开始，到现在两个多小时了，爬楼后更厉害。",
                "triggers": ["多久", "多长时间", "什么时候", "诱因", "活动"],
                "entities": ["两个多小时", "早上八点", "爬楼"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["多久", "多长时间", "什么时候开始", "持续", "诱因", "活动后"], "partial": ["多长时间"]},
            feedback_alt="可追问：“什么时候开始的？持续多久？活动以后会不会加重？”",
        ),
        _item(
            item_id="HX.03",
            seq=5,
            domain="history_taking",
            title="放射部位",
            observable="询问疼痛是否向肩、臂或下颌放射",
            positive="主动询问是否放射至左臂、肩部或下颌",
            partial="仅笼统询问是否向其他部位扩散",
            negative="未询问放射部位",
            evidence_type="utterance",
            phase="information_gathering",
            weight=2,
            authority="suggest",
            guideline="ESC-ACS-2023 §3.2",
            slot={
                "content": "这疼会窜到左边胳膊，有时候下巴也发紧，一阵一阵的，大概二十分钟。",
                "triggers": ["放射", "窜", "胳膊", "肩膀", "下巴", "扩散"],
                "entities": ["左边胳膊", "下巴", "二十分钟"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["放射", "窜", "左臂", "肩膀", "下巴", "放散"], "partial": ["别的地方", "其他部位"]},
            feedback_alt="可追加：“这种疼会不会窜到肩膀或下巴？”一句话即可覆盖该评分项。",
        ),
        _item(
            item_id="HX.04",
            seq=6,
            domain="history_taking",
            title="伴随症状",
            observable="询问出汗、恶心、呼吸困难等伴随症状",
            positive="询问出汗、恶心、呼吸困难中的至少两项",
            partial="只询问其中一项",
            negative="未询问伴随症状",
            evidence_type="utterance",
            phase="information_gathering",
            weight=2,
            authority="suggest",
            guideline="ESC-ACS-2023 §3.2",
            critical=True,
            confidence_floor=0.60,
            slot={
                "content": "出了不少汗，还有点恶心，喘气倒还行。",
                "triggers": ["出汗", "恶心", "呼吸", "伴随"],
                "entities": ["不少汗", "恶心", "喘气"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["出汗", "恶心", "呼吸", "伴随", "冷汗", "想吐", "虚汗", "反胃"], "partial": ["还有.*不舒服"]},
            feedback_alt="可追加：“有没有出汗、恶心或者喘不上气？”",
        ),
        _item(
            item_id="HX.05",
            seq=7,
            domain="history_taking",
            title="危险因素与既往史",
            observable="询问高血压、糖尿病、吸烟等心血管危险因素",
            positive="询问至少两项心血管危险因素或既往病史",
            partial="只询问一项",
            negative="未询问危险因素与既往史",
            evidence_type="utterance",
            phase="information_gathering",
            weight=1,
            authority="suggest",
            guideline="ESC-ACS-2023 §3.2",
            slot={
                "content": "高血压十来年了，烟抽了三十年，一天一包。",
                "triggers": ["高血压", "糖尿病", "吸烟", "血脂", "既往", "危险因素"],
                "entities": ["高血压", "三十年", "一天一包"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["高血压", "糖尿病", "吸烟", "血脂", "既往", "危险因素"], "partial": ["以前.*病"]},
            feedback_alt="可补充：“以前有高血压、糖尿病吗？抽烟吗？”",
        ),
        _item(
            item_id="HX.06",
            seq=8,
            domain="history_taking",
            title="用药与过敏史",
            observable="询问长期用药与药物过敏史",
            positive="询问正在服用的药物，并确认药物过敏史",
            partial="只询问用药或只询问过敏史",
            negative="未询问用药与过敏史",
            evidence_type="utterance",
            phase="information_gathering",
            weight=1,
            authority="suggest",
            guideline="中国胸痛中心规范 2023 §5",
            slot={
                "content": "平时吃降压药，名字记不清了；没有药物过敏。",
                "triggers": ["吃什么药", "降压药", "用药", "过敏"],
                "entities": ["降压药", "过敏"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["吃什么药", "降压药", "用药", "过敏"], "partial": ["用药"]},
            feedback_alt="可补充：“平时吃什么药？有没有药物过敏？”",
        ),
        _item(
            item_id="AS.01",
            seq=9,
            domain="assessment",
            title="查体告知与同意",
            observable="告知将进行针对性查体并征得患者同意",
            positive="说明查体内容（如听诊、测量血压）并征得同意",
            partial="只告知查体，未说明内容或未征得同意",
            negative="未告知即开始查体",
            evidence_type="action",
            phase="assessment",
            weight=1,
            authority="extract",
            guideline="中国胸痛中心规范 2023 §5",
            slot={
                "content": "行，你听吧。",
                "triggers": ["听诊", "查体", "检查一下", "量血压"],
                "entities": ["听吧"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["听诊", "查体", "检查一下", "量血压"], "partial": ["我看看"]},
            feedback_alt="查体前可说：“我给您听一下心肺，可能会有点凉，可以吗？”",
        ),
        _item(
            item_id="AS.02",
            seq=10,
            domain="assessment",
            title="危险信号识别",
            observable="说出需要紧急处理的特征（如持续胸痛伴出汗、心电图异常）",
            positive="明确指出持续胸痛伴出汗等危险信号，并提出紧急处理",
            partial="提及危险但未说明具体特征或处理",
            negative="未识别危险信号",
            evidence_type="utterance",
            phase="assessment",
            weight=3,
            authority="suggest",
            guideline="ESC-ACS-2023 §4.1",
            critical=True,
            confidence_floor=0.60,
            slot={
                "content": "我以前没这样疼过，这回疼得我心里发慌。",
                "triggers": ["危险", "严重", "心梗", "紧急"],
                "entities": ["没这样疼过", "心里发慌"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["危险", "紧急", "心梗", "急性冠脉", "马上", "立即", "大汗.*胸痛"], "partial": ["需要检查", "可能.*严重"]},
            feedback_alt="可明确说：“持续胸痛伴出汗属于危险信号，需要立即做心电图并监护。”",
        ),
        _item(
            item_id="PL.01",
            seq=11,
            domain="clinical_reasoning",
            title="初步诊断与鉴别",
            observable="提出初步诊断并至少给出一个鉴别诊断",
            positive="提出急性冠脉综合征/心绞痛等初步诊断，并给出至少一个鉴别诊断",
            partial="只提出初步诊断，未做鉴别",
            negative="未提出诊断或鉴别",
            evidence_type="utterance",
            phase="plan",
            weight=2,
            authority="suggest",
            guideline="ESC-ACS-2023 §4.1",
            slot={
                "content": "您说啥就是啥，我听您的。",
                "triggers": ["诊断", "什么病", "考虑"],
                "entities": ["听您的"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["考虑.*心绞痛|急性冠脉|心梗", "初步诊断", "鉴别"], "partial": ["可能是心脏"]},
            feedback_alt="可补一句：“目前首先考虑急性冠脉综合征，也需要排除肺栓塞和主动脉夹层。”",
        ),
        _item(
            item_id="PL.02",
            seq=12,
            domain="clinical_reasoning",
            title="下一步计划",
            observable="给出心电图、监护、抽血等具体下一步计划",
            positive="明确提出心电图、心电监护与心肌标志物检查中的至少两项",
            partial="只提出一项检查或笼统说“进一步检查”",
            negative="未给出下一步计划",
            evidence_type="utterance",
            phase="plan",
            weight=2,
            authority="suggest",
            guideline="ESC-ACS-2023 §4.1",
            critical=True,
            confidence_floor=0.60,
            slot={
                "content": "行，我都听安排。",
                "triggers": ["计划", "怎么办", "下一步", "检查", "住院"],
                "entities": ["听安排"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["心电图", "监护", "抽血", "肌钙蛋白", "住院", "转"], "partial": ["进一步检查"]},
            feedback_alt="可说：“我们先做心电图和心肌标志物，同时上心电监护，必要时请心内科会诊。”",
        ),
        _item(
            item_id="CM.01",
            seq=13,
            domain="communication",
            title="共情回应",
            observable="患者表达担忧后，考生用一句确认或安抚性语句回应",
            positive="在患者表达担忧后 2 轮内，用具体语句回应其担忧（如“我理解您担心……”）",
            partial="仅用一句笼统安抚回应，未针对患者具体担忧",
            negative="未回应患者担忧",
            evidence_type="global",
            phase="information_gathering",
            weight=2,
            authority="none",
            guideline="中国胸痛中心规范 2023 §5",
            feedback_alt="可回应：“我理解您现在很担心，我们会尽快把最危险的情况排除掉。”",
        ),
        _item(
            item_id="CM.02",
            seq=14,
            domain="communication",
            title="用患者能理解的语言解释",
            observable="用通俗语句解释病情或检查目的，避免未解释的术语",
            positive="用通俗语句解释至少一项检查或判断的目的",
            partial="有解释但夹杂未说明的术语",
            negative="未做任何解释",
            evidence_type="utterance",
            phase="plan",
            weight=1,
            authority="suggest",
            guideline="中国胸痛中心规范 2023 §5",
            slot={
                "content": "你这么说我就明白点了。",
                "triggers": ["明白", "听懂", "解释"],
                "entities": ["明白点"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["简单说", "也就是说", "不用太担心", "我解释", "通俗"], "partial": ["就是说"]},
            feedback_alt="可说：“心电图就像给心脏拍张电活动的照片，几分钟就能做完。”",
        ),
        _item(
            item_id="CL.01",
            seq=15,
            domain="communication",
            title="总结与确认",
            observable="总结关键信息并确认患者理解或提问",
            positive="复述关键判断/计划，并询问患者是否还有问题",
            partial="只总结或只询问是否理解",
            negative="未总结也未确认",
            evidence_type="utterance",
            phase="closing",
            weight=1,
            authority="suggest",
            guideline="中国胸痛中心规范 2023 §5",
            slot={
                "content": "好，那我先去检查，有问题再问您。",
                "triggers": ["清楚", "明白", "还有问题", "总结"],
                "entities": ["先去检查", "再问您"],
                "disclosure": "on_direct_ask",
            },
            probe={"positive": ["总结", "刚才.*说", "还有.*问题", "都清楚"], "partial": ["明白吗"]},
            feedback_alt="结束前可加：“我总结一下：先做心电图和抽血，期间不要下床，有问题随时叫我。”",
        ),
    ]

    return {
        "station_id": "ST-CARDIO-001",
        "title": "急性胸痛：病史采集与紧急处置",
        "version": "1.0",
        "duration_min": 8,
        "pass_line": 60,
        "risk_level_default": "L2",
        "candidate_instructions": [
            "你接诊一位因胸痛就诊的 65 岁男性患者，请在 8 分钟内完成接诊。",
            "请完成病史采集、针对性查体告知、危险信号识别与下一步计划。",
            "请用患者能理解的语言解释，并在结束前做总结。",
        ],
        "instruction_item_refs": [item["item_id"] for item in rubric],
        "extra_slots": [
            {
                "content": "我从早上开始胸口就一直不舒服，您给我看看吧。",
                "phase": "opening",
                "disclosure": "active",
                "triggers": [],
                "entities": ["胸口", "一直不舒服"],
            },
            {
                "content": "最近还瘦了五六斤，我也不知道跟这个有没有关系。",
                "phase": "information_gathering",
                "disclosure": "on_trust",
                "triggers": [],
                "entities": ["瘦了五六斤"],
            },
        ],
        "sp_profile": {
            "name": "王建国",
            "age": 65,
            "gender": "male",
            "occupation": "退休教师",
            "persona": {
                "tone": "焦虑但克制",
                "language": "普通话清晰",
                "memory": "清晰",
                "cognition": 0,
                "disclosure": "部分信息需建立信任后才披露",
            },
            "forbidden": ["不给出诊断", "不给出用药建议", "不评价考生"],
            "opening_line": "医生，我这胸口从早上就不舒服……",
        },
        "guideline_kb": GUIDELINE_KB,
        "rubric": rubric,
    }


ITEM_IDS = [
    "OP.01",
    "OP.02",
    "HX.01",
    "HX.02",
    "HX.03",
    "HX.04",
    "HX.05",
    "HX.06",
    "AS.01",
    "AS.02",
    "PL.01",
    "PL.02",
    "CM.01",
    "CM.02",
    "CL.01",
]


def _levels(**overrides: int) -> dict[str, int]:
    base = {item_id: 2 for item_id in ITEM_IDS}
    base.update(overrides)
    return base


PROFILES: dict[str, dict[str, int]] = {
    "excellent": _levels(),
    "good": _levels(**{"HX.03": 1, "HX.06": 1}),
    "mid": _levels(
        **{
            "HX.03": 0,
            "HX.04": 1,
            "AS.02": 1,
            "PL.02": 1,
            "CM.01": 1,
        }
    ),
    "weak": _levels(
        **{
            "OP.01": 1,
            "OP.02": 1,
            "HX.01": 1,
            "HX.02": 1,
            "HX.03": 0,
            "HX.04": 0,
            "HX.05": 1,
            "HX.06": 0,
            "AS.01": 1,
            "AS.02": 1,
            "PL.01": 0,
            "PL.02": 0,
            "CM.01": 1,
            "CM.02": 0,
            "CL.01": 0,
        }
    ),
    "near_low": _levels(
        **{
            "OP.01": 2,
            "OP.02": 2,
            "HX.03": 1,
            "HX.04": 1,
            "HX.05": 0,
            "HX.06": 0,
            "AS.01": 1,
            "AS.02": 1,
            "PL.01": 1,
            "PL.02": 1,
            "CM.01": 1,
            "CM.02": 1,
            "CL.01": 0,
        }
    ),
    "high_comm_weak": _levels(**{"CM.01": 1, "CM.02": 1, "CL.01": 1}),
    "low": _levels(
        **{
            "OP.01": 0,
            "OP.02": 1,
            "HX.01": 1,
            "HX.02": 0,
            "HX.03": 0,
            "HX.04": 1,
            "HX.05": 0,
            "HX.06": 0,
            "AS.01": 0,
            "AS.02": 0,
            "PL.01": 0,
            "PL.02": 1,
            "CM.01": 0,
            "CM.02": 0,
            "CL.01": 0,
        }
    ),
    "near_high": _levels(
        **{
            "OP.01": 0,
            "OP.02": 1,
            "HX.03": 1,
            "HX.04": 1,
            "HX.05": 1,
            "HX.06": 1,
            "AS.01": 1,
            "AS.02": 1,
            "PL.01": 1,
            "PL.02": 1,
            "CM.01": 1,
            "CM.02": 1,
            "CL.01": 1,
        }
    ),
    "critical_miss": _levels(**{"HX.06": 1}),
}

ATTEMPT_META = [
    {"code": "C2026-001", "name": "陈雨桐", "profile": "excellent", "asr": 0.96},
    {"code": "C2026-002", "name": "王一帆", "profile": "good", "asr": 0.95},
    {"code": "C2026-003", "name": "李昊然", "profile": "mid", "asr": 0.94},
    {"code": "C2026-004", "name": "赵思远", "profile": "weak", "asr": 0.92},
    {"code": "C2026-005", "name": "周可欣", "profile": "near_low", "asr": 0.95},
    {"code": "C2026-006", "name": "孙明哲", "profile": "high_comm_weak", "asr": 0.96},
    {"code": "C2026-007", "name": "吴嘉怡", "profile": "low", "asr": 0.60},
    {"code": "C2026-008", "name": "郑子涵", "profile": "excellent", "asr": 0.97},
    {"code": "C2026-009", "name": "冯语彤", "profile": "near_high", "asr": 0.93},
    {"code": "C2026-010", "name": "何俊杰", "profile": "excellent", "asr": 0.95},
    {"code": "C2026-011", "name": "许安然", "profile": "critical_miss", "asr": 0.94},
    {"code": "C2026-012", "name": "沈亦舟", "profile": "good", "asr": 0.91},
    {"code": "C2026-013", "name": "唐若溪", "profile": "mid", "asr": 0.90},
]

# 演示用 A/B 分组：AI 辅助组 vs 纯人工组，刻意让两组都包含高低分与边缘案例
AB_GROUPS: dict[int, str] = {
    1: "ai_assisted",
    2: "human_only",
    3: "human_only",
    4: "ai_assisted",
    5: "ai_assisted",
    6: "ai_assisted",
    7: "human_only",
    8: "ai_assisted",
    9: "human_only",
    10: "ai_assisted",
    11: "human_only",
    12: "human_only",
    13: "ai_assisted",
}

# 合成考生亚组，仅用于公平性监测演示；真实系统不得向评分员暴露这些字段
CANDIDATE_PROFILES: list[dict[str, str]] = [
    {"gender": "女", "origin": "城市", "language": "普通话"},
    {"gender": "男", "origin": "城市", "language": "方言口音"},
    {"gender": "男", "origin": "农村", "language": "方言口音"},
    {"gender": "女", "origin": "农村", "language": "方言口音"},
    {"gender": "男", "origin": "城市", "language": "普通话"},
    {"gender": "女", "origin": "城市", "language": "普通话"},
    {"gender": "男", "origin": "城市", "language": "普通话"},
    {"gender": "男", "origin": "农村", "language": "方言口音"},
    {"gender": "女", "origin": "农村", "language": "方言口音"},
    {"gender": "男", "origin": "农村", "language": "普通话"},
    {"gender": "女", "origin": "城市", "language": "方言口音"},
    {"gender": "男", "origin": "农村", "language": "普通话"},
    {"gender": "女", "origin": "农村", "language": "普通话"},
]

SPECIAL_UTTERANCES: dict[tuple[str, str], str] = {
    ("C2026-004", "HX.03"): "皮肤别的地方有没有起疹子？",
    ("C2026-007", "HX.05"): "家里人有心脏病吗？",
    ("C2026-010", "HX.03"): "您这种疼有没有牵涉痛？",
    ("C2026-011", "HX.04"): "身上有没有冒虚汗、反胃的感觉？",
}

UTTERANCES: dict[str, dict[int, list[str]]] = {
    "OP.01": {1: ["您好，我是张医生。"], 2: ["您好，我是急诊科张医生，请问您怎么称呼？"]},
    "OP.02": {1: ["我们开始问一下情况。"], 2: ["我想先了解一下您胸痛的情况，再做个体格检查，您看可以吗？"]},
    "HX.01": {1: ["您胸口哪里不舒服？"], 2: ["您能说说胸口具体是哪个位置疼吗？是压榨样的还是刺痛？"]},
    "HX.02": {1: ["疼多长时间了？"], 2: ["疼了多久了？什么时候开始的？活动以后会不会加重？"]},
    "HX.03": {1: ["会不会窜到别的地方？"], 2: ["这种疼会不会向肩膀、左臂或者下巴放射？"]},
    "HX.04": {1: ["还有别的不舒服吗？"], 2: ["有没有出汗、恶心或者呼吸困难？"]},
    "HX.05": {1: ["以前生过什么病？"], 2: ["以前有高血压、糖尿病吗？抽烟吗？"]},
    "HX.06": {1: ["平时用药吗？"], 2: ["平时吃什么药？有没有药物过敏？"]},
    "AS.01": {1: ["我给您检查一下。"], 2: ["我先给您听诊一下心肺，再量个血压，可以吗？"]},
    "AS.02": {1: ["这个情况需要进一步检查，可能比较严重。"], 2: ["持续胸痛还伴有出汗，这是危险信号，需要马上做心电图并监护。"]},
    "PL.01": {1: ["可能是心脏方面的问题。"], 2: ["目前首先考虑急性冠脉综合征，也需要排除肺栓塞和主动脉夹层。"]},
    "PL.02": {1: ["先做进一步检查。"], 2: ["我们先做心电图和心肌标志物，同时上心电监护，必要时请心内科会诊。"]},
    "CM.01": {1: ["别太担心。"], 2: ["我理解您现在很担心，我们会尽快把最危险的情况排除掉。"]},
    "CM.02": {1: ["就是说先查一下。"], 2: ["简单说，心电图就是给心脏拍一张电活动的照片，几分钟就好。"]},
    "CL.01": {1: ["您明白了吗？"], 2: ["我总结一下：先做心电图和抽血，期间不要下床，有问题随时叫我。您还有别的问题吗？"]},
}

PATIENT_LINES: dict[str, str] = {
    "OP.01": "我叫王建国，今年六十五。",
    "OP.02": "行，你问吧，我配合。",
    "HX.01": "胸口正中这块，压着疼，像有块石头。",
    "HX.02": "从早上八点开始，到现在两个多小时了，爬楼后更厉害。",
    "HX.03": "这疼会窜到左边胳膊，有时候下巴也发紧，一阵一阵的，大概二十分钟。",
    "HX.04": "出了不少汗，还有点恶心，喘气倒还行。",
    "HX.05": "高血压十来年了，烟抽了三十年，一天一包。",
    "HX.06": "平时吃降压药，名字记不清了；没有药物过敏。",
    "AS.01": "行，你听吧。",
    "AS.02": "我以前没这样疼过，这回疼得我心里发慌。",
    "PL.01": "那……到底是什么病啊？",
    "PL.02": "那我接下来该怎么办？",
    "CM.02": "你这么说我就明白点了。",
    "CL.01": "好，那我先去检查，有问题再问您。",
}


def _duration_for(text: str) -> int:
    return max(2600, min(8200, len(text) * 105))


def build_transcript(
    payload: dict[str, Any],
    attempt_code: str,
    profile: dict[str, int],
    base_asr: float,
) -> list[dict[str, Any]]:
    items = sorted(
        payload["rubric"],
        key=lambda i: (PHASE_RANK.get(i.get("phase"), 99), int(i.get("seq", 99))),
    )
    utterances: list[dict[str, Any]] = []

    def add(speaker: str, text: str, start: int, phase: str) -> int:
        duration = _duration_for(text)
        asr = max(0.5, min(0.99, base_asr + ((len(utterances) % 5) - 2) * 0.01))
        utterances.append(
            {
                "utt_id": f"u{len(utterances) + 1:03d}",
                "speaker": speaker,
                "start_ms": start,
                "end_ms": start + duration,
                "text": text,
                "asr_confidence": round(asr, 3),
                "phase": phase,
            }
        )
        return start + duration

    cursor = 0
    opening = payload["extra_slots"][0]["content"]
    cursor = add("patient", opening, cursor, "opening") + 900

    for item in items:
        item_id = item["item_id"]
        level = int(profile.get(item_id, 0))
        special = SPECIAL_UTTERANCES.get((attempt_code, item_id))
        text = None
        if special:
            text = special
        elif level > 0:
            variants = UTTERANCES.get(item_id, {}).get(level, [])
            if variants:
                seed = int(hashlib.sha256(f"{attempt_code}:{item_id}".encode()).hexdigest()[:6], 16)
                text = variants[seed % len(variants)]
        if not text:
            continue
        cursor = add("candidate", text, cursor, item["phase"]) + 700
        patient_line = PATIENT_LINES.get(item_id)
        if patient_line and item["evidence_type"] != "global":
            cursor = add("patient", patient_line, cursor, item["phase"]) + 800

    cursor = add("patient", "谢谢医生。", cursor, "closing") + 500
    return utterances


def ensure_demo_media() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if MEDIA_FILE.exists():
        return
    sample_rate = 8000
    rng = random.Random(20261123)
    samples = array("h")
    segment_left = 0
    freq = 180.0
    for sec in range(AUDIO_SECONDS):
        if segment_left <= 0:
            segment_left = rng.randint(2, 5) * sample_rate if rng.random() < 0.72 else rng.randint(1, 2) * sample_rate
            freq = rng.choice([150.0, 180.0, 210.0, 240.0])
            active = rng.random() < 0.72
        for i in range(sample_rate):
            t = (sec * sample_rate + i) / sample_rate
            base = math.sin(2 * math.pi * 50 * t) * 0.008
            if active:
                speech = math.sin(2 * math.pi * freq * t + math.sin(2 * math.pi * 3.1 * t)) * 0.05
                speech *= 0.65 + 0.35 * math.sin(2 * math.pi * 4.3 * t)
            else:
                speech = 0.0
            noise = (rng.random() - 0.5) * 0.012
            value = max(-1.0, min(1.0, base + speech + noise))
            samples.append(int(value * 32767))
            segment_left -= 1
    with wave.open(str(MEDIA_FILE), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(samples.tobytes())


def _choose_reviewer(record_id: str) -> str:
    reviewers = ["主治 张蕾", "主治 周敏", "高年资 王志强"]
    return reviewers[int(hashlib.sha256(record_id.encode()).hexdigest()[:4], 16) % len(reviewers)]


def _review_record(
    conn,
    record: dict[str, Any],
    *,
    action: str,
    to_score: int,
    reason_code: str | None,
    reviewer: str,
    seconds: float,
) -> None:
    from_score = record["ai_score"]
    final = from_score if action == "accept" else to_score
    status_map = {
        "accept": "accepted",
        "correct": "corrected",
        "reject": "rejected",
        "manual_score": "manual_scored",
    }
    conn.execute(
        "INSERT INTO review_actions(action_id, record_id, reviewer, action, from_score, to_score, "
        "reason_code, seconds_spent, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            db.new_id("rv"),
            record["record_id"],
            reviewer,
            action,
            from_score,
            final,
            reason_code,
            round(seconds, 1),
            db.now_iso(),
        ),
    )
    conn.execute(
        "UPDATE score_records SET status = ?, final_score = ?, reviewer = ?, reviewed_at = ? WHERE record_id = ?",
        (status_map[action], final, reviewer, db.now_iso(), record["record_id"]),
    )
    db.append_audit(
        conn,
        actor=reviewer,
        action=f"record.{action}",
        object_type="ScoreRecord",
        object_id=record["record_id"],
        before={"ai_score": from_score, "status": record["status"]},
        after={"final_score": final, "reason_code": reason_code},
    )


def _pick_action(record: dict[str, Any], gold: int) -> tuple[str, int, str | None]:
    if record["status"] == "manual_pending" or record["ai_score"] is None:
        return "manual_score", gold, None
    ai = int(record["ai_score"])
    if ai == gold:
        return "accept", ai, None
    bucket = int(hashlib.sha256(record["record_id"].encode()).hexdigest()[:4], 16) % 10
    if bucket < 2:
        return "accept", ai, None
    if bucket < 7:
        return "correct", gold, "判定过严" if ai < gold else "判定过宽"
    return "reject", gold, "行为锚歧义"


def seed_if_empty(conn) -> bool:
    if conn.execute("SELECT COUNT(*) AS n FROM bundles").fetchone()["n"] > 0:
        return False
    ensure_demo_media()
    payload = build_bundle_payload()
    bundle = freeze_bundle(conn, payload, actor="命题专家 林岚", version="1.0", reason="演示站包首次冻结")
    gateway = CapabilityGateway(conn, MockScoringProvider(), MODEL_VERSION, PROMPT_VERSION)

    for index, meta in enumerate(ATTEMPT_META, start=1):
        profile = PROFILES[meta["profile"]]
        transcript = build_transcript(payload, meta["code"], profile, meta["asr"])
        duration_ms = max(90000, transcript[-1]["end_ms"] + 5000)
        attempt_id = f"att_2026_{index:03d}"
        integrity_gap = meta["code"] == "C2026-012"
        appeal = meta["code"] == "C2026-013"
        conn.execute(
            "INSERT INTO attempts(attempt_id, bundle_id, candidate_code, candidate_name, session_id, risk_level, "
            "media_id, media_url, duration_ms, integrity_gap, integrity_note, appeal, asr_confidence, status, "
            "ab_group, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                bundle["bundle_id"],
                meta["code"],
                meta["name"],
                "SES-2026-A" if index <= 5 else ("SES-2026-B" if index <= 9 else "SES-2026-C"),
                "L2",
                MEDIA_ID,
                MEDIA_URL,
                duration_ms,
                1 if integrity_gap else 0,
                "第 2 段音频缺失 38 秒，转写存在断裂" if integrity_gap else None,
                1 if appeal else 0,
                meta["asr"],
                "scored",
                AB_GROUPS.get(index, "ai_assisted"),
                db.now_iso(),
            ),
        )
        profile_row = CANDIDATE_PROFILES[index - 1]
        conn.execute(
            "INSERT INTO candidate_profiles(attempt_id, gender, origin, language, created_at) VALUES(?,?,?,?,?)",
            (attempt_id, profile_row["gender"], profile_row["origin"], profile_row["language"], db.now_iso()),
        )
        conn.execute(
            "INSERT INTO transcripts(attempt_id, payload, created_at) VALUES(?,?,?)",
            (attempt_id, json.dumps(transcript, ensure_ascii=False), db.now_iso()),
        )
        conn.commit()
        attempt = {
            "attempt_id": attempt_id,
            "risk_level": "L2",
            "release_stage": "G0_offline",
            "media_id": MEDIA_ID,
            "duration_ms": duration_ms,
            "asr_confidence": meta["asr"],
            "integrity_gap": integrity_gap,
            "appeal": appeal,
        }
        run_scoring(conn, gateway, bundle=bundle, attempt=attempt, transcript=transcript)
        for item_id, level in profile.items():
            gold = level
            if meta["code"] == "C2026-004" and item_id == "HX.03":
                gold = 0
            if meta["code"] == "C2026-007" and item_id == "HX.05":
                gold = 2
            if meta["code"] == "C2026-010" and item_id == "HX.03":
                gold = 2
            if meta["code"] == "C2026-011" and item_id == "HX.04":
                gold = 2
            conn.execute(
                "UPDATE score_records SET gold_score = ? WHERE attempt_id = ? AND item_id = ?",
                (gold, attempt_id, item_id),
            )
        conn.commit()

    _seed_reviews(conn)
    _seed_ab_reviews(conn)
    _write_model_card(conn)
    _write_data_sheet(conn)
    for key, value in {
        "model_version": MODEL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "sp_model_version": SP_MODEL_VERSION,
        "sp_prompt_version": SP_PROMPT_VERSION,
        "release_stage": "G0_offline",
        "sus_score": "80.0",
        "seeded_at": db.now_iso(),
    }.items():
        db.set_state(conn, key, value)
    demo_answers = [5, 2, 4, 1, 4, 2, 4, 2, 4, 2]
    conn.execute(
        "INSERT INTO sus_responses(response_id, participant_label, answers, score, created_at) "
        "VALUES(?,?,?,?,?)",
        (
            db.new_id("sus"),
            "演示样本",
            json.dumps(demo_answers),
            compute_sus(demo_answers),
            db.now_iso(),
        ),
    )
    conn.commit()
    return True


def _seed_reviews(conn) -> None:
    attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall()]
    for attempt in attempts:
        records = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM score_records WHERE attempt_id = ? ORDER BY item_id",
                (attempt["attempt_id"],),
            ).fetchall()
        ]
        if attempt["integrity_gap"]:
            continue
        full_review = attempt["candidate_code"] == "C2026-002"
        review_rate = 0.45
        if attempt["appeal"]:
            review_rate = 0.20
        for record in records:
            unresolved = record["status"] in ("pending_review", "manual_pending")
            if not unresolved:
                continue
            should_review = full_review or (
                int(hashlib.sha256(record["record_id"].encode()).hexdigest()[:4], 16) % 100 < review_rate * 100
            )
            if not should_review:
                continue
            gold = int(record["gold_score"] or 0)
            action, to_score, reason = _pick_action(record, gold)
            if full_review:
                if record["item_id"] == "HX.05" and action == "accept":
                    action, to_score, reason = "reject", 1, "行为锚歧义"
                elif record["item_id"] == "HX.03" and action == "accept":
                    action, to_score, reason = "correct", 2, "判定过严"
            _review_record(
                conn,
                record,
                action=action,
                to_score=to_score,
                reason_code=reason,
                reviewer=_choose_reviewer(record["record_id"]),
                seconds=42 + int(hashlib.sha256(record["record_id"].encode()).hexdigest()[:2], 16) % 40,
            )
        conn.commit()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM score_records WHERE attempt_id = ? AND status IN ('pending_review','manual_pending')",
            (attempt["attempt_id"],),
        ).fetchone()["n"]
        if attempt["candidate_code"] == "C2026-002" and pending == 0:
            timestamp = db.now_iso()
            conn.execute(
                "UPDATE attempts SET status = 'signed_off', signed_off_by = ?, signed_off_at = ? WHERE attempt_id = ?",
                ("考务 李萍", timestamp, attempt["attempt_id"]),
            )
            db.append_audit(
                conn,
                actor="考务 李萍",
                action="attempt.human_signoff",
                object_type="StationAttempt",
                object_id=attempt["attempt_id"],
                before={"status": "pending_review"},
                after={"status": "signed_off", "signed_off_by": "考务 李萍"},
            )
            conn.execute(
                "UPDATE attempts SET status = 'published', published_at = ? WHERE attempt_id = ?",
                (timestamp, attempt["attempt_id"]),
            )
            db.append_audit(
                conn,
                actor="考务 李萍",
                action="attempt.publish",
                object_type="StationAttempt",
                object_id=attempt["attempt_id"],
                before={"status": "signed_off"},
                after={"status": "published"},
            )
        elif pending == 0 and attempt["status"] not in ("published",):
            conn.execute("UPDATE attempts SET status = 'reviewed' WHERE attempt_id = ?", (attempt["attempt_id"],))
        conn.commit()


def _seed_ab_reviews(conn) -> None:
    """生成演示用 A/B 评分记录：AI 辅助组与纯人工组的耗时、QWK 与通过率对照。"""
    attempts = [dict(r) for r in conn.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall()]
    for attempt in attempts:
        group = attempt.get("ab_group") or "ai_assisted"
        records = conn.execute(
            "SELECT * FROM score_records WHERE attempt_id = ? ORDER BY item_id",
            (attempt["attempt_id"],),
        ).fetchall()
        for record in records:
            gold = int(record["gold_score"] or 0)
            digest = int(hashlib.sha256(f"ab:{record['record_id']}".encode()).hexdigest()[:4], 16)
            if group == "human_only":
                noise = 1 if digest % 100 < 18 else 0
                score = max(0, min(2, gold - noise))
                seconds = 105 + digest % 45
                reviewer = "纯人工评分"
            else:
                score = record["final_score"]
                if score is None:
                    score = record["ai_score"]
                if score is None:
                    score = gold
                seconds = 45 + digest % 30
                reviewer = "AI 辅助复核"
            conn.execute(
                "INSERT INTO ab_reviews(review_id, attempt_id, item_id, group_name, score, reviewer, "
                "seconds_spent, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    db.new_id("abr"),
                    attempt["attempt_id"],
                    record["item_id"],
                    group,
                    int(score),
                    reviewer,
                    float(seconds),
                    db.now_iso(),
                ),
            )
    conn.commit()


def _write_data_sheet(conn) -> None:
    content = {
        "name": "OSCE·Loop 合成演示数据集",
        "version": "1.0",
        "source": "合成考站、合成对话与程序生成音频；不含真实考生、患者或可识别个人信息。",
        "attempts": 13,
        "rubric_items": 15,
        "labels": "gold_score 为合成人工金标准，仅用于离线评测与闸门演示。",
        "media": "data/media/demo_chest_pain.wav 为程序生成的占位音轨，仅用于证据片段播放。",
        "limitations": [
            "不能用于真实高利害考试。",
            "ASR 与评分器均为 Mock，不代表真实模型性能。",
            "公平性亚组为合成标签，仅验证监测回路。",
        ],
        "retention": "演示数据随数据库重置；真实部署需按 L1–L3 分级留存策略执行。",
    }
    conn.execute(
        "INSERT INTO data_sheets(data_sheet_id, version, content, is_public, published_at, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            db.new_id("ds"),
            "1.0",
            json.dumps(content, ensure_ascii=False),
            0,
            None,
            db.now_iso(),
        ),
    )
    conn.commit()


def _write_model_card(conn) -> None:
    report = quality_report(conn)
    metrics = {
        "qwk_ai": report["qwk_ai"],
        "qwk_human": report["qwk_human"],
        "evidence_rate": report["evidence"]["rate"],
        "edit_rate": report["review"]["edit_rate"],
        "sample_size": report["counts"]["records"],
    }
    conn.execute(
        "INSERT INTO model_cards(card_id, capability, model_version, prompt_version, metrics, limitations, "
        "is_public, published_at, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            db.new_id("mc"),
            "score.suggest",
            MODEL_VERSION,
            PROMPT_VERSION,
            json.dumps(metrics, ensure_ascii=False),
            "演示数据为合成录像；真实部署必须用本地金标准重新校准阈值。ASR 对非标准口音的词错率会直接压低证据召回。",
            0,
            None,
            db.now_iso(),
        ),
    )
    conn.commit()
    db.append_audit(
        conn,
        actor="系统",
        action="model_card.create",
        object_type="ModelCard",
        object_id="score.suggest",
        after=metrics,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
    )


def apply_startup_governance(conn) -> None:
    """模型或提示词版本一旦变化，权限自动降为 extract，禁止静默升级。

    默认版本只在首次初始化时写入；之后以数据库记录为准。只有显式设置
    OSCELOOP_MODEL_VERSION / OSCELOOP_PROMPT_VERSION 环境变量时才触发新一轮降级，
    避免“重启把界面里刚切换的版本又降回去”。
    """
    stored_model = db.get_state(conn, "model_version")
    stored_prompt = db.get_state(conn, "prompt_version")
    if stored_model is None:
        db.set_state(conn, "model_version", MODEL_VERSION)
        db.set_state(conn, "prompt_version", PROMPT_VERSION)
        return

    env_model = os.getenv("OSCELOOP_MODEL_VERSION")
    env_prompt = os.getenv("OSCELOOP_PROMPT_VERSION")
    if not env_model and not env_prompt:
        return

    configured_model = env_model or stored_model
    configured_prompt = env_prompt or stored_prompt
    if configured_model != stored_model or configured_prompt != stored_prompt:
        apply_authority_reset(
            conn,
            reason=f"模型/提示词版本变更：{stored_model}/{stored_prompt} → {configured_model}/{configured_prompt}",
            actor="系统",
            model_version=configured_model,
            prompt_version=configured_prompt,
        )
        db.set_state(conn, "model_version", configured_model)
        db.set_state(conn, "prompt_version", configured_prompt)
