"""S1 命题辅助：生成 L0 站包草案，并执行强制配额、难度预判与跨站去重。

草案只用于把空白页变成可修改的初稿：行为锚改写、指南适用性与临床真实性
必须由命题专家在 S2 完成，不能直接进入考试。
"""
from __future__ import annotations

import copy
from typing import Any

from .. import db
from ..config import DRAFT_MODEL_VERSION, DRAFT_PROMPT_VERSION
from .bundle import find_similar_bundles
from .coverage import blueprint_coverage
from .psychometrics import station_psychometrics
from .rubric import validate_bundle

DIFFICULTIES = ("easy", "medium", "hard")
DURATION_DEFAULTS = {"easy": 10, "medium": 8, "hard": 6}
DRAFT_LEVEL = "L0_draft"
DRAFT_DISCLOSURE = (
    "本草案由模板化 L0 命题辅助生成，仅用于命题讨论与初稿改写，不可直接进入考试；"
    "行为锚、指南适用性、临床真实性与考站组合必须由命题专家签字确认。"
)


def _item(
    *,
    item_id: str,
    domain: str,
    title: str,
    observable: str,
    positive: str,
    partial: str,
    negative: str,
    evidence_type: str,
    phase: str,
    weight: float,
    guideline_ref: str,
    slot_content: str,
    triggers: list[str],
    entities: list[str],
    probe_positive: list[str],
    probe_partial: list[str],
    feedback_alt: str,
    authority: str = "suggest",
    critical: bool = False,
    disclosure: str = "on_direct_ask",
    confidence_floor: float = 0.55,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "seq": 0,
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
        "confidence_floor": 0.60 if critical else confidence_floor,
        "guideline_ref": guideline_ref,
        "slot": {
            "content": slot_content,
            "triggers": triggers,
            "entities": entities,
            "disclosure": disclosure,
        },
        "probe": {"positive": probe_positive, "partial": probe_partial},
        "feedback_alt": feedback_alt,
    }


def _common_items(profile: dict[str, Any]) -> list[dict[str, Any]]:
    patient = profile["patient"]
    general_ref = profile["guidelines"][2]["ref"]
    return [
        _item(
            item_id="OP.01",
            domain="communication",
            title="自我介绍与身份确认",
            observable="向患者说明自己是接诊医生，并核对患者姓名",
            positive="主动说明身份（如“我是急诊科医生”），并核对患者姓名",
            partial="只说明身份或只核对姓名之一",
            negative="未说明身份，也未核对姓名",
            evidence_type="utterance",
            phase="opening",
            weight=1,
            guideline_ref=general_ref,
            slot_content=f"我叫{patient['name']}，今年{patient['age']}。",
            triggers=["姓名", "怎么称呼", "年龄"],
            entities=[patient["name"], str(patient["age"])],
            probe_positive=["我是.*医生", "怎么称呼", "姓名", "核对"],
            probe_partial=["您好"],
            feedback_alt="开场可加一句：“您好，我是急诊科张医生，请问您怎么称呼？”",
        ),
        _item(
            item_id="OP.02",
            domain="communication",
            title="说明接诊目的并征得配合",
            observable="告知患者将询问病情并做检查，征得患者同意",
            positive="说明将询问病史并进行检查，且征得患者同意",
            partial="只说明目的，未明确征得同意",
            negative="未说明接诊目的",
            evidence_type="utterance",
            phase="opening",
            weight=1,
            guideline_ref=general_ref,
            slot_content="行，你问吧，我配合。",
            triggers=["配合", "问吧", "同意"],
            entities=["配合"],
            probe_positive=["了解.*情况", "问.*情况", "做.*检查", "可以吗", "同意"],
            probe_partial=["我们开始"],
            feedback_alt="可说：“我想先了解您的情况，再做个体格检查，您看可以吗？”",
        ),
    ]


CASES: dict[str, dict[str, Any]] = {
    "abdominal_pain": {
        "label": "急性腹痛",
        "station_id": "ST-GI-001",
        "title": "急性腹痛：病史采集与初步处置",
        "primary_domain": "history_taking",
        "patient": {"name": "李国强", "age": 48, "gender": "male", "occupation": "出租车司机"},
        "tone": "疲惫但克制",
        "opening_line": "医生，我这肚子从昨晚就不舒服……",
        "chief_complaint": "肚子这块儿不舒服，说不上来怎么个疼。",
        "comorbidity": "糖尿病",
        "unrelated": "对了，我明天还得出车，这检查大概要多久？",
        "extra_distractor": "家里孙子还等着接，能不能快点儿？",
        "guidelines": [
            {
                "ref": "WSES-AA-2020 §2",
                "text": "急性腹痛评估应采集起病方式、疼痛部位与性质、伴随症状、既往腹部手术史及用药过敏史。",
            },
            {
                "ref": "WSES-AA-2020 §4",
                "text": "出现腹膜刺激征、血流动力学不稳定或感染征象时应紧急外科会诊。",
            },
            {
                "ref": "中国急性腹痛诊疗指南 2022 §3",
                "text": "接诊应核对身份、说明检查目的并征得配合；离院时交代复诊指征与安全网。",
            },
        ],
        "items": [
            _item(
                item_id="HX.01",
                domain="history_taking",
                title="腹痛部位与性质",
                observable="询问腹痛的具体部位与疼痛性质",
                positive="询问部位并追问性质（如“是胀痛、绞痛还是刀割样疼”）",
                partial="只询问部位或只询问性质",
                negative="未询问部位与性质",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="WSES-AA-2020 §2",
                slot_content="一开始是肚脐周围疼，后来转到右下腹，闷闷地疼。",
                triggers=["哪里", "部位", "性质", "怎么疼", "什么样的疼"],
                entities=["肚脐周围", "右下腹", "闷闷地疼"],
                probe_positive=["哪里.*疼", "什么位置", "什么性质", "怎么个疼", "胀痛", "绞痛"],
                probe_partial=["肚子疼"],
                feedback_alt="可追问：“一开始是哪个位置？现在疼在哪儿？是胀痛、绞痛还是刀割样疼？”",
            ),
            _item(
                item_id="HX.02",
                domain="history_taking",
                title="起病时间与诱因",
                observable="询问腹痛开始时间、持续时长与诱发因素",
                positive="询问开始时间、持续时长，并追问饮食等诱因",
                partial="只询问时长或只询问诱因",
                negative="未询问持续时间与诱因",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="WSES-AA-2020 §2",
                slot_content="昨天晚上开始的，吃了火锅以后更明显。",
                triggers=["多久", "什么时候", "诱因", "吃了什么"],
                entities=["昨天晚上", "火锅"],
                probe_positive=["多久", "什么时候开始", "持续", "诱因", "吃了什么"],
                probe_partial=["多长时间"],
                feedback_alt="可追问：“什么时候开始疼的？吃了什么以后加重？”",
            ),
            _item(
                item_id="HX.03",
                domain="history_taking",
                title="伴随症状",
                observable="询问恶心、呕吐、发热、排便改变等伴随症状",
                positive="询问恶心呕吐、发热、排便改变中的至少两项",
                partial="只询问其中一项",
                negative="未询问伴随症状",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="WSES-AA-2020 §2",
                slot_content="有点恶心，吐了一次，没发烧，大便两天没解了。",
                triggers=["恶心", "呕吐", "发烧", "大便", "排便"],
                entities=["恶心", "吐了一次", "没发烧", "两天没解"],
                probe_positive=["恶心", "呕吐", "发热", "发烧", "大便", "排便", "排气"],
                probe_partial=["还有.*不舒服"],
                feedback_alt="可追加：“有没有恶心、呕吐、发烧，或者大便解不出来？”",
                critical=True,
            ),
            _item(
                item_id="HX.04",
                domain="history_taking",
                title="既往史与共病",
                observable="询问既往腹部手术史与糖尿病等慢性病史",
                positive="询问腹部手术史，并追问糖尿病等慢性病史",
                partial="只询问手术史或只询问慢性病史",
                negative="未询问既往史与手术史",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="WSES-AA-2020 §2",
                slot_content="我有糖尿病，打胰岛素。以前没做过肚子手术。",
                triggers=["糖尿病", "手术", "既往", "病史"],
                entities=["糖尿病", "没做过手术"],
                probe_positive=["手术", "糖尿病", "既往", "病史", "开过刀"],
                probe_partial=["以前.*病"],
                feedback_alt="可补充：“以前做过腹部手术吗？有没有糖尿病这类慢性病？”",
            ),
            _item(
                item_id="HX.05",
                domain="history_taking",
                title="用药与过敏史",
                observable="询问长期用药与药物过敏史",
                positive="询问正在使用的药物，并确认药物过敏史",
                partial="只询问用药或只询问过敏史",
                negative="未询问用药与过敏史",
                evidence_type="utterance",
                phase="information_gathering",
                weight=1,
                guideline_ref="WSES-AA-2020 §2",
                slot_content="平时打胰岛素，没有药物过敏。",
                triggers=["吃什么药", "用药", "胰岛素", "过敏"],
                entities=["胰岛素", "没有过敏"],
                probe_positive=["吃什么药", "用药", "胰岛素", "过敏"],
                probe_partial=["用药"],
                feedback_alt="可补充：“平时用什么药？有没有药物过敏？”",
            ),
            _item(
                item_id="AS.01",
                domain="assessment",
                title="查体告知与同意",
                observable="告知将进行腹部触诊并征得患者同意",
                positive="说明将进行腹部触诊（如按压腹部），并征得同意",
                partial="只告知查体，未说明内容或未征得同意",
                negative="未告知即开始查体",
                evidence_type="action",
                phase="assessment",
                weight=1,
                guideline_ref="中国急性腹痛诊疗指南 2022 §3",
                slot_content="行，你按吧，疼的地方轻点儿。",
                triggers=["查体", "按", "触诊", "检查"],
                entities=["按吧", "轻点儿"],
                probe_positive=["查体", "触诊", "按.*肚子", "检查.*腹部", "可以吗", "同意"],
                probe_partial=["我看一下"],
                feedback_alt="查体前可说明：“我需要按压一下腹部，可能会有点不舒服，可以吗？”",
            ),
            _item(
                item_id="PL.01",
                domain="clinical_reasoning",
                title="初步评估与处置计划",
                observable="提出进一步检查与处置计划，并说明理由",
                positive="提出抽血、影像学检查或外科会诊中的至少两项，并说明理由",
                partial="只提出检查或只提出处置，未说明理由",
                negative="未提出检查与处置计划",
                evidence_type="utterance",
                phase="plan",
                weight=2,
                guideline_ref="WSES-AA-2020 §4",
                slot_content="要抽血、做个腹部 CT 吗？我听你的。",
                triggers=["检查", "CT", "抽血", "怎么办", "治疗"],
                entities=["抽血", "腹部 CT"],
                probe_positive=["抽血", "CT", "超声", "外科会诊", "住院", "检查"],
                probe_partial=["看看再说"],
                feedback_alt="可提出：“建议先抽血并做腹部 CT，同时请外科评估是否需要急诊处理。”",
            ),
            _item(
                item_id="CL.01",
                domain="patient_safety",
                title="总结与安全网",
                observable="总结关键信息并告知复诊指征与安全网",
                positive="复述关键病史，并告知疼痛加重、呕吐不止等复诊指征",
                partial="只做总结或只交代复诊，未形成安全网",
                negative="未总结也未交代复诊",
                evidence_type="utterance",
                phase="closing",
                weight=1,
                guideline_ref="中国急性腹痛诊疗指南 2022 §3",
                slot_content="好，我记住了，疼得厉害就来。",
                triggers=["注意", "复诊", "什么时候", "回家"],
                entities=["疼得厉害", "来"],
                probe_positive=["总结", "复诊", "加重", "随时", "注意", "回来"],
                probe_partial=["还有.*问题"],
                feedback_alt="结束前可总结：“目前考虑急腹症，先抽血和做 CT；如果疼得更厉害、反复呕吐，随时回来。”",
            ),
        ],
    },
    "fever": {
        "label": "发热待查",
        "station_id": "ST-ID-001",
        "title": "发热待查：病史采集与初步评估",
        "primary_domain": "history_taking",
        "patient": {"name": "陈晓梅", "age": 32, "gender": "female", "occupation": "小学教师"},
        "tone": "乏力而焦虑",
        "opening_line": "医生，我烧了好几天了，浑身没劲……",
        "chief_complaint": "这几天老是发烧，身上没劲儿，具体多少度我也没量准。",
        "comorbidity": "慢性肾病",
        "unrelated": "医生，我这请假条能开吗？单位催得紧。",
        "extra_distractor": "我孩子还在家，能不能先开点药回去？",
        "guidelines": [
            {
                "ref": "IDSA-FUO-2021 §2",
                "text": "发热待查应完整采集热型、伴随症状、旅行史、动物接触史与用药过敏史。",
            },
            {
                "ref": "IDSA-FUO-2021 §5",
                "text": "出现意识改变、低血压或呼吸窘迫时应按脓毒症流程评估。",
            },
            {
                "ref": "中国发热待查诊治专家共识 2021 §3",
                "text": "接诊应核对身份并说明检查目的；应询问近期旅行、蚊虫叮咬与结核接触史，离院时交代复诊指征。",
            },
        ],
        "items": [
            _item(
                item_id="HX.01",
                domain="history_taking",
                title="热型与最高体温",
                observable="询问发热的热型、最高体温与测量方式",
                positive="询问最高体温，并追问发热是持续还是忽冷忽热",
                partial="只询问体温或只询问热型",
                negative="未询问热型与最高体温",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="IDSA-FUO-2021 §2",
                slot_content="忽冷忽热的，最高到过三十九度二。",
                triggers=["多少度", "体温", "热型", "发烧"],
                entities=["三十九度二", "忽冷忽热"],
                probe_positive=["多少度", "体温", "热型", "最高", "量过"],
                probe_partial=["发烧"],
                feedback_alt="可追问：“最高烧到多少度？是一直烧，还是忽冷忽热？”",
            ),
            _item(
                item_id="HX.02",
                domain="history_taking",
                title="病程与诱因",
                observable="询问发热开始时间、持续时长与诱因",
                positive="询问开始时间、持续时长，并追问受凉等诱因",
                partial="只询问时长或只询问诱因",
                negative="未询问病程与诱因",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="IDSA-FUO-2021 §2",
                slot_content="五天了，一开始以为是感冒，吃了退烧药能降一点。",
                triggers=["几天", "多久", "什么时候", "诱因"],
                entities=["五天", "感冒", "退烧药"],
                probe_positive=["几天", "多久", "什么时候开始", "持续", "诱因"],
                probe_partial=["多长时间"],
                feedback_alt="可追问：“烧了几天？之前有没有受凉或接触生病的人？”",
            ),
            _item(
                item_id="HX.03",
                domain="history_taking",
                title="伴随症状",
                observable="询问咳嗽、咳痰、皮疹、腹泻等伴随症状",
                positive="询问呼吸系统、皮疹、腹泻等伴随症状中的至少两项",
                partial="只询问其中一项",
                negative="未询问伴随症状",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="IDSA-FUO-2021 §5",
                slot_content="有点咳嗽，没痰；身上酸痛，不想吃东西。",
                triggers=["咳嗽", "咳痰", "酸痛", "胃口", "伴随"],
                entities=["咳嗽", "没痰", "身上酸痛", "不想吃东西"],
                probe_positive=["咳嗽", "咳痰", "皮疹", "腹泻", "胃口", "呼吸"],
                probe_partial=["还有.*不舒服"],
                feedback_alt="可追加：“有没有咳嗽、咳痰、皮疹、腹泻，或者胃口明显变差？”",
                critical=True,
            ),
            _item(
                item_id="HX.04",
                domain="history_taking",
                title="流行病学史与既往史",
                observable="询问旅行史、结核接触史与慢性病史",
                positive="询问近期旅行、结核接触史，并追问慢性病史",
                partial="只询问流行病学史或只询问慢性病史",
                negative="未询问流行病学史与既往史",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="中国发热待查诊治专家共识 2021 §3",
                slot_content="我有慢性肾病，没出过远门，家里也没人得结核。",
                triggers=["慢性病", "肾病", "旅行", "结核", "接触"],
                entities=["慢性肾病", "没出远门", "没人得结核"],
                probe_positive=["慢性病", "肾病", "旅行", "结核", "接触", "动物"],
                probe_partial=["以前.*病"],
                feedback_alt="可补充：“最近出过远门吗？家里有人得结核吗？以前有什么慢性病？”",
            ),
            _item(
                item_id="HX.05",
                domain="history_taking",
                title="用药与过敏史",
                observable="询问退热药使用情况与药物过敏史",
                positive="询问已使用的退热药，并确认药物过敏史",
                partial="只询问用药或只询问过敏史",
                negative="未询问用药与过敏史",
                evidence_type="utterance",
                phase="information_gathering",
                weight=1,
                guideline_ref="IDSA-FUO-2021 §2",
                slot_content="吃过布洛芬，没有过敏。",
                triggers=["吃什么药", "布洛芬", "用药", "过敏"],
                entities=["布洛芬", "没有过敏"],
                probe_positive=["吃什么药", "布洛芬", "退烧药", "用药", "过敏"],
                probe_partial=["用药"],
                feedback_alt="可补充：“吃过什么退烧药？有没有药物过敏？”",
            ),
            _item(
                item_id="AS.01",
                domain="assessment",
                title="查体告知与同意",
                observable="告知将进行查体并征得患者同意",
                positive="说明将测量体温、听诊或检查皮肤，并征得同意",
                partial="只告知查体，未说明内容或未征得同意",
                negative="未告知即开始查体",
                evidence_type="action",
                phase="assessment",
                weight=1,
                guideline_ref="中国发热待查诊治专家共识 2021 §3",
                slot_content="行，你听吧，我有点怕冷。",
                triggers=["查体", "听诊", "检查"],
                entities=["怕冷"],
                probe_positive=["查体", "听诊", "测量", "血压", "皮肤", "可以吗", "同意"],
                probe_partial=["我看一下"],
                feedback_alt="查体前可说明：“我先给您测体温、听一下肺，再看有没有皮疹，可以吗？”",
            ),
            _item(
                item_id="PL.01",
                domain="clinical_reasoning",
                title="初步评估与处置计划",
                observable="提出化验、影像或留观计划，并说明理由",
                positive="提出抽血化验、血培养或影像检查中的至少两项，并说明理由",
                partial="只提出检查或只提出处置，未说明理由",
                negative="未提出检查与处置计划",
                evidence_type="utterance",
                phase="plan",
                weight=2,
                guideline_ref="IDSA-FUO-2021 §5",
                slot_content="要抽血培养吗？我听你的。",
                triggers=["检查", "抽血", "怎么办", "住院"],
                entities=["抽血培养"],
                probe_positive=["抽血", "血培养", "影像", "会诊", "留观", "住院"],
                probe_partial=["看看再说"],
                feedback_alt="可提出：“建议先抽血和血培养，必要时做影像检查，并留观观察生命体征。”",
            ),
            _item(
                item_id="CL.01",
                domain="patient_safety",
                title="总结与安全网",
                observable="总结关键信息并告知复诊指征与安全网",
                positive="复述发热病程，并告知持续高热、意识改变等复诊指征",
                partial="只做总结或只交代复诊，未形成安全网",
                negative="未总结也未交代复诊",
                evidence_type="utterance",
                phase="closing",
                weight=1,
                guideline_ref="中国发热待查诊治专家共识 2021 §3",
                slot_content="好，我回去多喝水，烧不退就来。",
                triggers=["注意", "复诊", "什么时候", "回家"],
                entities=["多喝水", "烧不退"],
                probe_positive=["总结", "复诊", "不退", "加重", "随时", "注意"],
                probe_partial=["还有.*问题"],
                feedback_alt="结束前可总结：“先抽血和血培养；如果持续高热、精神变差或喘不上气，随时回来。”",
            ),
        ],
    },
    "dyspnea": {
        "label": "呼吸困难",
        "station_id": "ST-RESP-001",
        "title": "呼吸困难：病史采集与初步处置",
        "primary_domain": "history_taking",
        "patient": {"name": "周德海", "age": 68, "gender": "male", "occupation": "退休工人"},
        "tone": "气促而克制",
        "opening_line": "医生，我这两天喘得厉害……",
        "chief_complaint": "这两天喘不上气，说不好是哪儿堵得慌。",
        "comorbidity": "慢阻肺",
        "unrelated": "我这口罩戴不住，能不能不戴？",
        "extra_distractor": "我老伴儿在楼下等着，她腿脚不好。",
        "guidelines": [
            {
                "ref": "GOLD-2024 §3",
                "text": "呼吸困难评估应采集起病速度、诱因、活动耐量变化、既往心肺疾病史与用药史。",
            },
            {
                "ref": "GOLD-2024 §4",
                "text": "出现静息低氧、意识改变或辅助呼吸肌参与时应紧急评估并考虑升级治疗。",
            },
            {
                "ref": "中国慢性阻塞性肺疾病诊治指南 2021 §2",
                "text": "接诊应核对身份并说明检查目的；应询问吸烟史、职业暴露与既往急性加重次数，离院时交代复诊指征。",
            },
        ],
        "items": [
            _item(
                item_id="HX.01",
                domain="history_taking",
                title="呼吸困难性质与程度",
                observable="询问呼吸困难的主观感受与活动耐量变化",
                positive="询问是气短、胸闷还是吸气困难，并追问活动耐量变化",
                partial="只询问感受或只询问活动耐量",
                negative="未询问呼吸困难性质与程度",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="GOLD-2024 §3",
                slot_content="就是觉得气不够用，走几步就得歇。",
                triggers=["什么感觉", "气短", "活动", "走几步"],
                entities=["气不够用", "走几步就得歇"],
                probe_positive=["什么感觉", "气短", "喘", "活动", "走.*路", "程度"],
                probe_partial=["喘不上气"],
                feedback_alt="可追问：“是觉得气不够用，还是胸口发闷？以前能走多远，现在走几步要歇？”",
            ),
            _item(
                item_id="HX.02",
                domain="history_taking",
                title="起病速度与诱因",
                observable="询问呼吸困难开始时间、起病速度与诱因",
                positive="询问开始时间、是突然还是逐渐加重，并追问感冒等诱因",
                partial="只询问时间或只询问诱因",
                negative="未询问起病速度与诱因",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="GOLD-2024 §3",
                slot_content="三天了，前两天感冒后加重。",
                triggers=["多久", "什么时候", "诱因", "感冒"],
                entities=["三天", "感冒后加重"],
                probe_positive=["多久", "什么时候开始", "突然", "诱因", "感冒", "加重"],
                probe_partial=["多长时间"],
                feedback_alt="可追问：“三天前开始的？是突然喘起来，还是感冒后慢慢加重？”",
            ),
            _item(
                item_id="HX.03",
                domain="history_taking",
                title="伴随症状",
                observable="询问咳嗽、咳痰、胸痛、发热等伴随症状",
                positive="询问咳嗽咳痰、胸痛、发热中的至少两项",
                partial="只询问其中一项",
                negative="未询问伴随症状",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="GOLD-2024 §4",
                slot_content="有点咳嗽，痰是白的，没胸痛，也没发烧。",
                triggers=["咳嗽", "咳痰", "胸痛", "发烧", "痰"],
                entities=["咳嗽", "白痰", "没胸痛", "没发烧"],
                probe_positive=["咳嗽", "咳痰", "胸痛", "发热", "发烧", "痰"],
                probe_partial=["还有.*不舒服"],
                feedback_alt="可追加：“有没有咳嗽、咳痰、胸痛或者发烧？痰是什么颜色？”",
                critical=True,
            ),
            _item(
                item_id="HX.04",
                domain="history_taking",
                title="既往史与危险因素",
                observable="询问慢阻肺等心肺疾病史、吸烟史与急性加重次数",
                positive="询问既往心肺疾病史，并追问吸烟史或急性加重次数",
                partial="只询问既往史或只询问危险因素",
                negative="未询问既往史与危险因素",
                evidence_type="utterance",
                phase="information_gathering",
                weight=2,
                guideline_ref="GOLD-2024 §3",
                slot_content="慢阻肺好几年了，烟抽了四十年。",
                triggers=["慢阻肺", "吸烟", "职业", "加重", "既往"],
                entities=["慢阻肺", "烟龄四十年"],
                probe_positive=["慢阻肺", "吸烟", "职业", "加重", "既往", "心肺"],
                probe_partial=["以前.*病"],
                feedback_alt="可补充：“以前有慢阻肺或心脏病吗？抽烟多少年？一年会急性加重几次？”",
            ),
            _item(
                item_id="HX.05",
                domain="history_taking",
                title="用药与过敏史",
                observable="询问吸入剂使用情况与药物过敏史",
                positive="询问正在使用的吸入剂或平喘药，并确认药物过敏史",
                partial="只询问用药或只询问过敏史",
                negative="未询问用药与过敏史",
                evidence_type="utterance",
                phase="information_gathering",
                weight=1,
                guideline_ref="GOLD-2024 §3",
                slot_content="用着吸入剂，名字记不清；没过敏。",
                triggers=["用药", "吸入剂", "过敏"],
                entities=["吸入剂", "没过敏"],
                probe_positive=["用药", "吸入剂", "平喘药", "过敏"],
                probe_partial=["用什么药"],
                feedback_alt="可补充：“平时用哪种吸入剂？最近有没有加量？有没有药物过敏？”",
            ),
            _item(
                item_id="AS.01",
                domain="assessment",
                title="查体告知与同意",
                observable="告知将进行肺部听诊与血氧测量并征得同意",
                positive="说明将听诊肺部、测量血氧或呼吸频率，并征得同意",
                partial="只告知查体，未说明内容或未征得同意",
                negative="未告知即开始查体",
                evidence_type="action",
                phase="assessment",
                weight=1,
                guideline_ref="中国慢性阻塞性肺疾病诊治指南 2021 §2",
                slot_content="行，你听吧，我尽量配合。",
                triggers=["查体", "听诊", "血氧", "检查"],
                entities=["尽量配合"],
                probe_positive=["查体", "听诊", "血氧", "呼吸频率", "可以吗", "同意"],
                probe_partial=["我看一下"],
                feedback_alt="查体前可说明：“我给您听一下肺、测个血氧，可能需要您配合深呼吸，可以吗？”",
            ),
            _item(
                item_id="PL.01",
                domain="clinical_reasoning",
                title="初步评估与处置计划",
                observable="提出氧疗、雾化或进一步检查计划，并说明理由",
                positive="提出吸氧、雾化或血气分析等至少两项处置，并说明理由",
                partial="只提出处置或只提出检查，未说明理由",
                negative="未提出处置与检查计划",
                evidence_type="utterance",
                phase="plan",
                weight=2,
                guideline_ref="GOLD-2024 §4",
                slot_content="要吸氧和做肺功能吗？",
                triggers=["吸氧", "检查", "怎么办", "治疗"],
                entities=["吸氧", "肺功能"],
                probe_positive=["吸氧", "雾化", "血气", "肺功能", "影像", "住院"],
                probe_partial=["看看再说"],
                feedback_alt="可提出：“先吸氧并雾化，查血气评估缺氧程度，必要时做影像并考虑住院。”",
            ),
            _item(
                item_id="CL.01",
                domain="patient_safety",
                title="总结与安全网",
                observable="总结关键信息并告知复诊指征与安全网",
                positive="复述呼吸困难变化，并告知气促加重、意识改变等复诊指征",
                partial="只做总结或只交代复诊，未形成安全网",
                negative="未总结也未交代复诊",
                evidence_type="utterance",
                phase="closing",
                weight=1,
                guideline_ref="中国慢性阻塞性肺疾病诊治指南 2021 §2",
                slot_content="好，喘得厉害我就来。",
                triggers=["注意", "复诊", "什么时候", "回家"],
                entities=["喘得厉害"],
                probe_positive=["总结", "复诊", "加重", "意识", "随时", "注意"],
                probe_partial=["还有.*问题"],
                feedback_alt="结束前可总结：“先吸氧和雾化；如果喘得更厉害、说话都费劲或人犯迷糊，马上回来。”",
            ),
        ],
    },
}


def case_topics() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": case["label"],
            "station_id": case["station_id"],
            "primary_domain": case["primary_domain"],
            "duration_min": DURATION_DEFAULTS["medium"],
        }
        for key, case in CASES.items()
    ]


def _quota_check(payload: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    texts: list[str] = []
    for slot in payload.get("extra_slots") or []:
        if isinstance(slot, dict):
            texts.append(str(slot.get("content") or ""))
    for item in payload.get("rubric") or []:
        if isinstance(item, dict):
            texts.append(str((item.get("slot") or {}).get("content") or ""))
    checks = []
    for key, label, expected in (
        ("comorbidity", "至少 1 项共病", case["comorbidity"]),
        ("unrelated_concern", "至少 1 项与主诉无关的合理诉求", case["unrelated"]),
        ("ambiguity", "主诉存在模糊处", case["chief_complaint"]),
    ):
        evidence = next((text for text in texts if expected in text), None)
        checks.append(
            {
                "key": key,
                "label": label,
                "expected": expected,
                "found": evidence is not None,
                "evidence": evidence,
            }
        )
    return {
        "passed": all(check["found"] for check in checks),
        "checks": checks,
        "note": "强制配额用于避免“教科书式病人”：共病、无关合理诉求与主诉模糊必须同时存在。",
    }


def _difficulty_report(
    payload: dict[str, Any],
    *,
    requested: str,
    quota: dict[str, Any],
    historical: list[dict[str, Any]],
) -> dict[str, Any]:
    items = [item for item in payload.get("rubric") or [] if isinstance(item, dict)]
    item_count = len(items)
    critical_count = sum(1 for item in items if item.get("critical"))
    distractors = max(0, len(payload.get("extra_slots") or []) - 1)
    ambiguity = 1 if next(
        (check for check in quota["checks"] if check["key"] == "ambiguity"), {"found": False}
    )["found"] else 0
    duration = max(1, int(payload.get("duration_min") or 8))
    load = item_count / duration
    predicted = (
        0.86
        - 0.035 * (item_count - 8)
        - 0.07 * critical_count
        - 0.05 * distractors
        - 0.05 * ambiguity
        - 0.25 * max(0.0, load - 1.0)
    )
    predicted = round(min(0.92, max(0.25, predicted)), 3)
    estimated = "easy" if predicted >= 0.68 else ("medium" if predicted >= 0.45 else "hard")
    scored = [row for row in historical if (row.get("scored_attempts") or 0) > 0]
    avg_pass = (
        round(sum(float(row["pass_rate"]) for row in scored) / len(scored), 3)
        if scored and all(row.get("pass_rate") is not None for row in scored)
        else None
    )
    return {
        "requested": requested,
        "estimated": estimated,
        "aligned": estimated == requested,
        "expected_pass_rate": predicted,
        "range": f"{max(0.0, predicted - 0.1):.0%}–{min(1.0, predicted + 0.1):.0%}",
        "signals": [
            {"key": "items", "label": "评分项数", "value": item_count},
            {"key": "critical", "label": "关键项数", "value": critical_count},
            {"key": "distractors", "label": "干扰性诉求", "value": distractors},
            {"key": "ambiguity", "label": "主诉模糊", "value": ambiguity},
            {"key": "minutes", "label": "建议时长", "value": duration},
            {"key": "items_per_minute", "label": "每分钟评分项", "value": round(load, 2)},
        ],
        "historical": {
            "stations": len(scored),
            "avg_pass_rate": avg_pass,
        },
        "basis": "启发式预判：按评分项数、关键项、干扰诉求与时间压力估算；S2 必须用真实通过率与区分度反查。",
    }


def _quality_context(historical: list[dict[str, Any]]) -> dict[str, Any]:
    """把上次质量报告转成命题提示：低区分度项、地板/天花板站。"""
    low_items: list[dict[str, Any]] = []
    weak_stations: list[dict[str, Any]] = []
    for row in historical:
        if (row.get("scored_attempts") or 0) < 5:
            continue
        flags = row.get("flags") or []
        if "ceiling" in flags or "floor" in flags:
            weak_stations.append(
                {
                    "station_id": row.get("station_id"),
                    "title": row.get("title"),
                    "flags": [flag for flag in flags if flag in ("ceiling", "floor")],
                    "mean_total": row.get("mean_total"),
                }
            )
        for item in row.get("items") or []:
            discrimination = item.get("discrimination")
            if discrimination is not None and discrimination < 0.2:
                low_items.append(
                    {
                        "station_id": row.get("station_id"),
                        "item_id": item.get("item_id"),
                        "title": item.get("title"),
                        "discrimination": discrimination,
                    }
                )
    advisories: list[str] = []
    if low_items:
        sample = "、".join(
            f"{entry['station_id']}/{entry['item_id']}" for entry in low_items[:3]
        )
        advisories.append(
            f"既往站包有 {len(low_items)} 个低区分度项（如 {sample}），"
            "草案应避免同类模糊锚，并增加干扰信息或鉴别性症状。"
        )
    if weak_stations:
        sample = "、".join(str(entry["station_id"]) for entry in weak_stations[:3])
        advisories.append(
            f"{len(weak_stations)} 个站出现地板/天花板效应（如 {sample}），"
            "起草时需调整难度或关键项配额。"
        )
    if not advisories:
        qualified = [row for row in historical if (row.get("scored_attempts") or 0) >= 5]
        if qualified:
            advisories.append(
                "既往质量报告未发现低区分度项或地板/天花板效应，可按常规难度起草；仍需 S2 复核。"
            )
        else:
            advisories.append("暂无足够历史场次形成质量提示；难度预判仅作启发式参考。")
    return {
        "low_discrimination_items": low_items[:10],
        "weak_stations": weak_stations[:10],
        "advisories": advisories,
        "note": "质量提示来自各站最新冻结版本的心理测量；样本不足 5 场的站不纳入。",
    }


def generate_bundle_draft(
    conn,
    *,
    topic: str,
    difficulty: str = "medium",
    duration_min: int | None = None,
    actor: str = "命题专家",
    llm_provider=None,
    llm_gateway=None,
) -> dict[str, Any]:
    """生成 L0 站包草案；返回草案本身、四项检查与预判信息，并写入审计。"""
    case = CASES.get(topic)
    if not case:
        raise ValueError(f"暂不支持该命题主题：{topic}")
    if difficulty not in DIFFICULTIES:
        raise ValueError("difficulty 只能是 easy / medium / hard")
    case = copy.deepcopy(case)
    duration = int(duration_min or DURATION_DEFAULTS[difficulty])
    if duration < 5 or duration > 30:
        raise ValueError("duration_min 应在 5–30 分钟之间")

    rubric = _common_items(case) + case["items"]
    if difficulty == "easy":
        for item in rubric:
            item["critical"] = False
            item["confidence_floor"] = 0.55
    elif difficulty == "hard":
        for item in rubric:
            if item["item_id"] == "HX.04":
                item["critical"] = True
                item["confidence_floor"] = 0.60
    for index, item in enumerate(rubric, start=1):
        item["seq"] = index

    patient = case["patient"]
    extra_slots = [
        {
            "content": case["chief_complaint"],
            "phase": "opening",
            "disclosure": "active",
            "purpose": "chief_complaint",
            "triggers": [],
            "entities": ["不舒服"],
        },
        {
            "content": case["unrelated"],
            "phase": "information_gathering",
            "disclosure": "on_trust",
            "purpose": "unrelated_concern",
            "triggers": [],
            "entities": ["额外诉求"],
        },
    ]
    if difficulty == "hard":
        extra_slots.append(
            {
                "content": case["extra_distractor"],
                "phase": "closing",
                "disclosure": "on_trust",
                "purpose": "ambiguity",
                "triggers": [],
                "entities": ["时间压力"],
            }
        )

    payload = {
        "station_id": case["station_id"],
        "title": case["title"],
        "version": "0.1",
        "duration_min": duration,
        "pass_line": 60,
        "risk_level_default": "L0",
        "candidate_instructions": [
            f"你接诊一位因{case['label']}就诊的 {patient['age']} 岁患者，请在 {duration} 分钟内完成接诊。",
            "请完成病史采集、针对性查体告知、危险信号识别与下一步计划。",
            "请用患者能理解的语言解释，并在结束前做总结与安全网交代。",
        ],
        "instruction_item_refs": [item["item_id"] for item in rubric],
        "extra_slots": extra_slots,
        "sp_profile": {
            "name": patient["name"],
            "age": patient["age"],
            "gender": patient["gender"],
            "occupation": patient["occupation"],
            "persona": {
                "tone": case["tone"],
                "language": "普通话清晰",
                "memory": "清晰",
                "cognition": 0,
                "disclosure": "部分信息需建立信任后才披露",
            },
            "forbidden": ["不给出诊断", "不给出用药建议", "不评价考生"],
            "opening_line": case["opening_line"],
        },
        "guideline_kb": case["guidelines"],
        "rubric": rubric,
    }

    draft_source = "template"
    llm_error = None
    template_payload = payload
    draft_client = llm_gateway or llm_provider
    if draft_client is not None and hasattr(draft_client, "draft_bundle"):
        try:
            draft_response = draft_client.draft_bundle(
                topic=case["label"],
                difficulty=difficulty,
                duration_min=duration,
                guidelines=case["guidelines"],
                patient=patient,
                quotas={
                    "comorbidity": case["comorbidity"],
                    "unrelated_concern": case["unrelated"],
                    "ambiguity": case["chief_complaint"],
                },
            )
            candidate = (
                draft_response.get("output")
                if llm_gateway is not None and isinstance(draft_response, dict)
                else draft_response
            )
            if not isinstance(candidate, dict):
                raise ValueError("LLM 草案必须是 JSON 对象")
            if not isinstance(candidate.get("rubric"), list) or not candidate["rubric"]:
                raise ValueError("LLM 草案缺少 rubric")
            if not all(isinstance(item, dict) for item in candidate["rubric"]):
                raise ValueError("LLM 草案的 rubric 项必须是对象")
            if not _quota_check(candidate, case)["passed"]:
                raise ValueError("LLM 草案未满足强制配额（共病/无关诉求/主诉模糊）")
            payload = copy.deepcopy(candidate)
            payload["station_id"] = case["station_id"]
            payload["title"] = str(payload.get("title") or case["title"])
            payload["version"] = "0.1"
            payload["duration_min"] = duration
            payload.setdefault("pass_line", 60)
            payload["risk_level_default"] = "L0"
            if not isinstance(payload.get("guideline_kb"), list) or not payload["guideline_kb"]:
                payload["guideline_kb"] = case["guidelines"]
            for index, item in enumerate(payload["rubric"], start=1):
                if isinstance(item, dict):
                    item.setdefault("seq", index)
            payload.setdefault(
                "instruction_item_refs",
                [item.get("item_id") for item in payload["rubric"] if isinstance(item, dict)],
            )
            draft_source = "llm"
        except Exception as exc:
            llm_error = str(exc)
            payload = template_payload

    validation = validate_bundle(payload)
    quota = _quota_check(payload, case)
    historical = station_psychometrics(conn)
    difficulty_report = _difficulty_report(
        payload,
        requested=difficulty,
        quota=quota,
        historical=historical,
    )
    quality_context = _quality_context(historical)
    coverage = blueprint_coverage(conn)
    domain_row = next(
        (row for row in coverage["domains"] if row["domain"] == case["primary_domain"]), None
    )
    blueprint = {
        "domain": case["primary_domain"],
        "label": domain_row["label"] if domain_row else case["primary_domain"],
        "status": domain_row["status"] if domain_row else "uncovered",
        "share": domain_row["share"] if domain_row else 0.0,
        "target": domain_row["target"] if domain_row else None,
        "uncovered": coverage["uncovered"],
        "note": "覆盖度按当前最新冻结站包统计；本草案冻结前不会计入蓝图。",
    }
    similarity = find_similar_bundles(conn, payload, threshold=0.75)
    generated_at = db.now_iso()
    draft_model_version = (
        getattr(draft_client, "model_version", None) if draft_source == "llm" else None
    )
    draft_prompt_version = (
        getattr(draft_client, "prompt_version", None) if draft_source == "llm" else None
    )
    result = {
        "draft": payload,
        "validation": validation,
        "quota": quota,
        "difficulty": difficulty_report,
        "quality_context": quality_context,
        "blueprint": blueprint,
        "similarity": similarity,
        "similarity_threshold": 0.75,
        "draft_source": draft_source,
        "llm_error": llm_error,
        "llm_model_version": (
            getattr(draft_client, "model_version", None)
            if draft_source == "llm"
            else None
        ),
        "disclosure": {
            "level": DRAFT_LEVEL,
            "text": DRAFT_DISCLOSURE,
        },
        "versions": {
            "model_version": draft_model_version or DRAFT_MODEL_VERSION,
            "prompt_version": draft_prompt_version or DRAFT_PROMPT_VERSION,
            "rubric_version": "draft-0.1",
        },
        "generated_at": generated_at,
    }
    db.append_audit(
        conn,
        actor=actor,
        action="bundle.draft_generated",
        object_type="StationBundle",
        object_id=case["station_id"],
        before=None,
        after={
            "topic": topic,
            "difficulty": difficulty,
            "duration_min": duration,
            "items": len(payload.get("rubric") or []),
            "quota_passed": quota["passed"],
            "can_freeze": validation["can_freeze"],
            "draft_source": draft_source,
            "llm_error": llm_error,
            "similarity": similarity[:3],
        },
        model_version=draft_model_version or DRAFT_MODEL_VERSION,
        prompt_version=draft_prompt_version or DRAFT_PROMPT_VERSION,
    )
    return result
