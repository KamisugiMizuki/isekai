"""示例世界包与角色卡（内容填满、可通过校验）。

两个用途：
1. 生成器的「形状参考」——模型照此填写字段类型与粒度，避免往标识字段塞描述；
2. 测试夹具的单一真源。
"""

from __future__ import annotations

from typing import Any

from .package import template_package

DAY = 86400


def example_package(name: str = "灰潮纪", *, moment: int = DAY * 1500) -> dict[str, Any]:
    package = template_package(name, density="normal")
    calendar = package["calendar"]
    calendar["era"] = "灰潮纪"
    calendar["day_seconds"] = DAY
    calendar["months"] = [{"name": "雾月", "days": 30}, {"name": "霜月", "days": 30}, {"name": "融月", "days": 30}]
    calendar["week"] = {"name": "旬", "days": 10}
    calendar["segments"] = [
        {"id": "seg-night", "name": "夜", "start": 0, "end": 21600},
        {"id": "seg-morning", "name": "晨", "start": 21600, "end": 43200},
        {"id": "seg-day", "name": "昼", "start": 43200, "end": 64800},
        {"id": "seg-evening", "name": "暮", "start": 64800, "end": DAY},
    ]
    calendar["initial_moment"] = moment

    package["meta"]["description"] = "潮水退去后留下盐碱与旧堤的世界。"
    package["world"] = {
        "axioms": [
            {"id": "ax-1", "text": "潮汐每三十日一次，退潮时露出可通行的盐滩。"},
            {"id": "ax-2", "text": "没有跨越远海的常备航线，消息靠沿岸驿站传递。"},
        ],
        "geography": "三条沿岸城邦带，内陆为盐碱荒原，通行依赖退潮后的盐滩。",
        "society": "城邦由堤长议会治理，驿站信报是主要的公共消息来源。",
        "lexicon": {
            "note": "城邦名为两字，人名带堤或潮的偏旁。",
            "terms": [{"term": "堤长", "meaning": "城邦议会的执事者"}],
        },
        "institutions": [
            {
                "id": "inst-1",
                "name": "堤长议会",
                "mandate": "议定堤务、征发修补人力与发放退潮通行牌",
                "scope": "三条沿岸城邦",
                "succession": "堤长身故或去职时由同城邦议席推举接任，空缺期间日常堤务照旧、通行牌暂停发放",
                "validity": "自崩塌后第 2 年沿用至今",
            }
        ],
        "customs": [
            {
                "id": "cus-1",
                "name": "退潮祭",
                "applies_to": "沿岸城邦的堤务吏与盐户",
                "practice": "大退潮首日在滩口设盐与旧堤砖，读水位尺后散去",
                "basis": "崩堤后为记住水位而设",
                "variation": "城邦之间可换用本地盐样，环节顺序不改",
            }
        ],
    }
    package["sources"] = [
        {"id": "src-1", "name": "驿站信报", "kind": "official", "reach": "在城邦驿站停留并支付铜钱即可取阅"},
        {"id": "src-2", "name": "旧堤碑刻", "kind": "document", "reach": "退潮后可在盐滩实地拓印"},
    ]
    package["canon"] = [
        {"id": "cf-1", "statement": "十二年前北堤崩塌，三城邦的粮仓被淹。", "tags": ["灾害"]},
        {"id": "cf-2", "statement": "崩塌前夜，堤长议会收到过一份未被采信的潮位告警。", "tags": ["谜团"]},
    ]
    package["narratives"] = [
        {
            "id": "nv-1",
            "text": "北堤崩塌被记作天罚，因为告警从未公开。",
            "source_id": "src-1",
            "canon_ref": "cf-1",
            "obtain": ["在城邦驿站读到刊行的灾年编年"],
            "confidence": "believed",
        },
        {
            "id": "nv-2",
            "text": "有碑刻提到崩堤当夜曾有人登堤敲钟。",
            "source_id": "src-2",
            "canon_ref": "cf-2",
            "obtain": ["退潮时在盐滩亲自拓印碑文"],
            "confidence": "doubted",
        },
    ]
    package["races"] = [{"id": "rc-1", "name": "岸民", "lifespan": {"min_years": 55, "max_years": 80}}]
    package["entities"] = [
        {"id": "en-1", "kind": "person", "name": "堤禾", "race_id": "rc-1", "born": 0, "died": None},
        {"id": "en-2", "kind": "org", "name": "堤长议会", "race_id": None, "born": None, "died": None},
    ]
    package["historiography"] = [
        {
            "id": "hs-1",
            "title": "灾年编年",
            "contributors": [{"name": "堤南史馆", "role": "编纂", "period": "崩塌后第 3 年"}],
            "written_at": DAY * 1080,
            "compiled_at": DAY * 1200,
            "coverage": {"from": DAY * 1, "to": DAY * 1080},
            "genre": "官方编年",
            "stance": "维护议会口径",
            "entries": ["cf-1", "nv-1"],
        },
        {
            "id": "hs-2",
            "title": "盐滩碑录",
            "contributors": [{"name": "无名拓者", "role": "拓印", "period": "年代不详"}],
            "written_at": DAY * 1300,
            "compiled_at": DAY * 1300,
            "coverage": {"from": DAY * 1300, "to": DAY * 1300},
            "genre": "民间碑录",
            "stance": "零散",
            "entries": ["nv-2"],
        },
    ]
    package["events"] = {
        "families": [
            {
                "id": "ef-1",
                "name": "潮汐与堤务",
                "templates": [
                    {
                        "id": "et-1",
                        "summary": "退潮延误导致城外驿站停摆一日",
                        "preconditions": ["cf-1"],
                        "effects": [{"kind": "source_delay", "target": "src-1"}],
                        "weight": 1,
                    }
                ],
            }
        ]
    }
    package["life"] = [
        {
            "id": "lf-1",
            "name": "堤务吏日常",
            "sleep": True,
            "windows": [
                {"start": 0, "end": 25200, "activity": "sleep"},
                {"start": 25200, "end": 72000, "activity": "duty"},
                {"start": 72000, "end": DAY, "activity": "rest"},
            ],
        }
    ]
    package["roles"] = [
        {
            "id": "rl-1",
            "name": "堤务吏",
            "description": "登记水位与盐滩通行的低级执事。",
            "life_template": "lf-1",
            "channels": ["src-1"],
        }
    ]
    package["comms"] = {
        "mechanisms": [
            {"id": "cm-1", "name": "沿岸信箱", "limits": "只在退潮日通行时收信，信使不进入内陆"}
        ]
    }
    package["initial_state"] = {
        "events": ["cf-1"],
        "rumors": ["nv-1", "nv-2"],
        "mysteries": [{"id": "my-1", "question": "那份告警是谁写的", "refs": ["cf-2", "nv-2"]}],
    }
    return package


def example_card(
    package: dict[str, Any], *, name: str = "堤禾", born: int | None = None, confirmed: bool = True
) -> dict[str, Any]:
    moment = int(package["calendar"]["initial_moment"])
    calendar = package["calendar"]
    year_seconds = sum(int(item["days"]) for item in calendar["months"]) * int(calendar["day_seconds"])
    if born is None:
        born = moment - 25 * year_seconds  # 二十五岁；纪元开始之前为负数
    return {
        "meta": {"schema": "1.0", "card_id": f"cc-{name}", "confirmed": confirmed},
        "identity": {
            "name": name,
            "race_id": package["races"][0]["id"],
            "born": born,
            "gender": "女",
            "occupation": "堤务吏",
            "self_identity": "替议会照看水位尺的人，不算官。",
        },
        "background": {
            "creator": "她父亲的旧账本里有那份告警的一页抄件，但她从没翻到那一页。",
            "self_knowledge": "十二年前崩堤时她还小，只记得盐味。",
        },
        "region": "南堤城邦的盐滩一带",
        "role_id": "rl-1",
        "channels": [{"source_id": "src-1", "conditions": "凭堤务吏身份在驿站取阅信报"}],
        "initial_knowledge": [
            {"ref_type": "historiography", "ref_id": "hs-1", "scope": ["cf-1", "nv-1"], "obtained_at": DAY * 1200},
            {"ref_type": "canon", "ref_id": "cf-1", "obtained_at": DAY * 1200},
            {"ref_type": "self", "claim": "她记得崩堤那年的盐味和搬家的车。"},
        ],
        "comms": [{"mechanism_id": "cm-1", "note": "退潮日把信投进沿岸信箱"}],
        "first_contact": {"stance": "谨慎但不回避", "intent": "先弄清对方是从哪条盐滩来的"},
        "initial_units": [
            {"id": "iu-1", "semantic": "先量再说话", "driver": "anchor", "confidence": 0.9, "basis": "十年记水位尺养成的习惯"},
            {"id": "iu-2", "semantic": "对议会的说法留一半", "driver": "dialog", "confidence": 0.45, "basis": "父辈的告诫"},
        ],
        "cognition": {"mode": "soft", "sources": ["self_experience", "small_env", "user_contact"]},
        "life_template": {
            "sleep": True,
            "routine_note": "退潮日提前一个时辰上堤。",
            "windows": [
                {"start": 0, "end": 25200, "activity": "sleep"},
                {"start": 25200, "end": 72000, "activity": "duty", "alternatives": ["rest"]},
                {"start": 72000, "end": DAY, "activity": "rest"},
            ],
        },
        "appearance": "袖口常年沾盐渍。",
    }
