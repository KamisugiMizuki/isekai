"""世界包与角色卡校验（阶段 1）：空壳、悬空引用、历法、双轨边界、知识越权、日程非法。"""

from __future__ import annotations

import copy
import json

from isekai_core.world.cards import validate_assembly, validate_card
from isekai_core.world.package import (
    load_package,
    normalize_name,
    save_package,
    template_package,
    unique_name,
)
from isekai_core.world.validate import validate_package
from samples import DAY, sample_card, sample_package


def test_template_package_is_not_valid_on_its_own() -> None:
    """骨架必须被填满才能通过：否则校验器形同虚设。"""
    errors = validate_package(template_package("空壳"))
    assert errors
    assert any("axioms" in item or "公理" in item for item in errors)


def test_filled_sample_package_passes() -> None:
    assert validate_package(sample_package()) == []


def test_dangling_references_are_rejected() -> None:
    package = sample_package()
    package["narratives"][0]["source_id"] = "src-missing"
    package["roles"][0]["channels"] = ["src-missing"]
    package["initial_state"]["rumors"] = ["nv-missing"]
    package["historiography"][0]["entries"] = ["cf-missing"]
    errors = validate_package(package)
    assert any("source_id" in item for item in errors)
    assert any("channels" in item for item in errors)
    assert any("rumors" in item for item in errors)
    assert any("entries" in item for item in errors)


def test_narrative_without_source_is_rejected() -> None:
    package = sample_package()
    package["narratives"][0]["source_id"] = None
    errors = validate_package(package)
    assert any("必须有来源" in item for item in errors)


def test_mystery_needs_grounding() -> None:
    package = sample_package()
    package["initial_state"]["mysteries"][0]["refs"] = []
    assert any("挂靠" in item for item in validate_package(package))


def test_historiography_empty_shell_is_rejected() -> None:
    package = sample_package()
    package["historiography"][0]["entries"] = []
    package["historiography"][1]["contributors"] = []
    errors = validate_package(package)
    assert any("空壳" in item for item in errors)
    assert any("贡献者" in item for item in errors)


def test_calendar_must_be_self_consistent() -> None:
    package = sample_package()
    package["calendar"]["segments"] = [
        {"id": "a", "name": "夜", "start": 0, "end": 21600},
        {"id": "b", "name": "昼", "start": 30000, "end": DAY},
    ]
    assert any("不连续" in item for item in validate_package(package))

    package = sample_package()
    package["calendar"]["segments"] = package["calendar"]["segments"][:2]
    assert any("未覆盖" in item for item in validate_package(package))

    package = sample_package()
    package["calendar"]["day_seconds"] = 0
    assert any("day_seconds" in item for item in validate_package(package))


def test_duplicate_ids_are_rejected() -> None:
    package = sample_package()
    package["canon"].append(copy.deepcopy(package["canon"][0]))
    assert any("标识重复" in item for item in validate_package(package))


def test_package_file_roundtrip_and_original_name_is_sticky(tmp_path) -> None:
    package = sample_package("灰潮纪")
    path = tmp_path / "packages" / "greytide.json"
    save_package(path, package)
    loaded = load_package(path)
    assert loaded["meta"]["original_name"] == "灰潮纪"

    loaded["meta"]["display_name"] = "改过的名字"
    save_package(path, loaded)
    reloaded = load_package(path)
    assert reloaded["meta"]["display_name"] == "改过的名字"
    assert reloaded["meta"]["original_name"] == "灰潮纪", "原始名称不随显示名改变"


def test_name_normalization_and_suffixes() -> None:
    assert normalize_name("  灰潮纪 ") == normalize_name("灰潮纪")
    assert normalize_name("AbC") == normalize_name("ａｂｃ"), "NFKC + 大小写折叠"
    taken = ["灰潮纪"]
    assert unique_name("灰潮纪", taken) == "灰潮纪_2"
    assert unique_name("灰潮纪", taken + ["灰潮纪_2"]) == "灰潮纪_3"
    assert unique_name("另起", taken) == "另起"
    assert unique_name(" 灰潮纪 ", ["灰潮纪"]) == "灰潮纪_2", "空白差异视为同名"


# ---------- 角色卡 ----------


def test_sample_card_passes_single_and_joint_validation() -> None:
    package = sample_package()
    card = sample_card(package)
    moment = int(package["calendar"]["initial_moment"])
    assert validate_card(card, package, moment=moment) == []
    assert validate_assembly(package, [card], moment=moment) == []


def test_unconfirmed_card_blocks_assembly() -> None:
    package = sample_package()
    card = sample_card(package, confirmed=False)
    errors = validate_assembly(package, [card], moment=int(package["calendar"]["initial_moment"]))
    assert any("未经用户确认" in item for item in errors)


def test_confidence_band_and_missing_anchor() -> None:
    package = sample_package()
    moment = int(package["calendar"]["initial_moment"])
    card = sample_card(package)
    card["initial_units"][0]["confidence"] = 0.5  # 锚点必须在 0.75–0.99
    card["initial_units"][1]["driver"] = "dialog"
    assert any("初始置信度" in item for item in validate_card(card, package, moment=moment))

    card = sample_card(package)
    card["initial_units"] = [unit for unit in card["initial_units"] if unit["driver"] != "anchor"]
    assert any("锚点" in item for item in validate_card(card, package, moment=moment))


def test_hard_cognition_cannot_open_world_channels() -> None:
    package = sample_package()
    moment = int(package["calendar"]["initial_moment"])
    card = sample_card(package)
    card["cognition"] = {"mode": "hard", "sources": ["self_experience", "src-1"]}
    assert any("硬约束" in item for item in validate_card(card, package, moment=moment))


def test_initial_knowledge_cannot_be_gained_after_start() -> None:
    package = sample_package()
    moment = int(package["calendar"]["initial_moment"])
    card = sample_card(package)
    card["initial_knowledge"][0]["obtained_at"] = moment + DAY
    assert any("晚于初始时刻" in item for item in validate_card(card, package, moment=moment))

    card = sample_card(package)
    card["initial_knowledge"][0]["ref_id"] = "hs-missing"
    assert any("不存在" in item for item in validate_card(card, package, moment=moment))

    card = sample_card(package)
    card["initial_knowledge"][0]["obtained_at"] = DAY  # 早于成书 DAY*1200
    assert any("早于成书" in item for item in validate_card(card, package, moment=moment))


def test_life_windows_must_be_legal() -> None:
    package = sample_package()
    moment = int(package["calendar"]["initial_moment"])

    card = sample_card(package)
    card["life_template"]["windows"][2]["start"] = 60000  # 与前一块重叠
    assert any("重叠" in item for item in validate_card(card, package, moment=moment))

    card = sample_card(package)
    card["life_template"]["windows"][1]["activity"] = "打渔"
    assert any("未在世界包" in item for item in validate_card(card, package, moment=moment))

    # 跨午夜睡眠：窗口越过世界日界并回到日首，只要不与他段重叠即合法
    card = sample_card(package)
    card["life_template"]["windows"] = [
        {"start": 79200, "end": 108000, "activity": "sleep"},
        {"start": 21600, "end": 72000, "activity": "duty"},
        {"start": 72000, "end": 79200, "activity": "rest"},
    ]
    assert validate_card(card, package, moment=moment) == []

    # 日首段与白天活动重叠：拒绝
    card = sample_card(package)
    card["life_template"]["windows"] = [
        {"start": 79200, "end": 108000, "activity": "sleep"},
        {"start": 0, "end": 72000, "activity": "duty"},
    ]
    assert any("日首段" in item for item in validate_card(card, package, moment=moment))

    # 两个窗口跨日：拒绝
    card = sample_card(package)
    card["life_template"]["windows"] = [
        {"start": 79200, "end": 108000, "activity": "sleep"},
        {"start": 50000, "end": 100000, "activity": "duty"},
        {"start": 21600, "end": 50000, "activity": "rest"},
    ]
    assert any("最多一个窗口跨世界日" in item for item in validate_card(card, package, moment=moment))


def test_race_and_birth_must_be_compatible() -> None:
    package = sample_package()
    moment = int(package["calendar"]["initial_moment"])
    card = sample_card(package)
    card["identity"]["race_id"] = "rc-missing"
    assert any("种族不存在" in item for item in validate_card(card, package, moment=moment))

    card = sample_card(package)
    card["identity"]["born"] = moment + DAY  # 出生晚于初始时刻
    assert any("不能晚于" in item for item in validate_card(card, package, moment=moment))

    package = sample_package()
    package["races"][0]["lifespan"] = {"min_years": 1, "max_years": 1}  # 一年寿命
    card = sample_card(package)
    card["identity"]["born"] = 0
    assert any("寿命覆盖不相容" in item for item in validate_card(card, package, moment=moment))


def test_loading_limits_are_enforced() -> None:
    """加载限额（§2.3）：超限明确拒绝，不静默裁掉设定。"""
    package = sample_package()
    package["world"]["geography"] = "长" * 5000
    assert any("加载限额" in item for item in validate_package(package))

    package = sample_package()
    package["world"]["axioms"] = [{"id": f"ax-{index}", "text": "x"} for index in range(600)]
    assert any("条目数" in item for item in validate_package(package))

    package = sample_package()
    node: dict = {}
    cursor = node
    for _ in range(20):
        cursor["nested"] = {}
        cursor = cursor["nested"]
    package["meta"]["extra"] = node
    assert any("嵌套深度" in item for item in validate_package(package))


def test_unknown_required_capability_is_rejected() -> None:
    """未知必需能力必须在确认前报错（§2.5）。"""
    package = sample_package()
    package["meta"]["requires"] = ["world.package.v1", "future.magic.v9"]
    errors = validate_package(package)
    assert any("future.magic.v9" in item for item in errors)
    package["meta"]["requires"] = ["world.package.v1"]
    assert validate_package(package) == []


def test_effect_target_must_be_registered() -> None:
    """效果目标指向未登记对象时创建失败（附录 C #10）。"""
    package = sample_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "rumor_spread", "target": "某个没登记的人", "expiry": "with_cause"}
    ]
    assert any("未登记对象" in item for item in validate_package(package))

    package = sample_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "rumor_spread", "target": "en-1", "expiry": "with_cause"}
    ]
    assert validate_package(package) == []


def test_declared_institutions_and_customs_must_be_explicable() -> None:
    """声明的制度与惯例若无法被一致解释，创建期即失败（附录 C #13）。"""
    package = sample_package()
    package["world"]["institutions"][0]["mandate"] = ""
    package["world"]["customs"][0]["variation"] = ""
    errors = validate_package(package)
    assert any("mandate" in item for item in errors)
    assert any("variation" in item for item in errors)

    package = sample_package()
    package["world"]["institutions"] = []
    package["world"]["customs"] = []
    # 未声明制度 / 惯例时，引用它们的制度类效果一并去掉（效果不能指向没声明的对象）
    effects = package["events"]["families"][0]["templates"][0]["effects"]
    package["events"]["families"][0]["templates"][0]["effects"] = [
        item for item in effects if item.get("kind") != "institution_state"
    ]
    assert validate_package(package) == [], "未声明即不适用，不因缺少制度拒绝合法题材"


def test_historiography_knowledge_needs_explicit_scope() -> None:
    """初始知识引用史料时必须写明掌握范围，且范围不能越权（附录 C #9）。"""
    package = sample_package()
    moment = int(package["calendar"]["initial_moment"])

    card = sample_card(package)
    card["initial_knowledge"][0].pop("scope", None)
    assert any("必须写明所掌握的条目" in item for item in validate_card(card, package, moment=moment))

    card = sample_card(package)
    card["initial_knowledge"][0]["scope"] = ["cf-1", "nv-9"]
    assert any("超出该传本" in item for item in validate_card(card, package, moment=moment))


def test_card_json_is_the_only_artifact() -> None:
    """卡片文件只保存最终版本：确认字段入盘，无生成历史字段。"""
    package = sample_package()
    card = sample_card(package)
    text = json.dumps(card, ensure_ascii=False)
    assert "history" not in text
    assert card["meta"]["confirmed"] is True
