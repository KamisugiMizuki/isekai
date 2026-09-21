"""阶段 6（世界自演化）：制度与惯例——声明范围、空缺可判定、沿合法效果变化、认知路径、确定性。

行为级：只断言可观察结果（状态行、判定答案、角色能看到的投影），不看实现符号。
"""

from __future__ import annotations

import json

from isekai_core.runtime import institutions
from isekai_core.runtime.service import RuntimeService
from isekai_core.world.validate import change_allowed, office_index, validate_package
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _seeded(store, world) -> tuple[dict, str, str, dict]:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    return info, timeline_id, character_id, package


def test_initial_state_comes_from_declaration(store, world) -> None:
    """职位与在任者、惯例现行做法都来自世界包声明；空缺是合法初始状态。"""
    info, timeline_id, _, _ = _seeded(store, world)
    offices = {row["office_id"]: row for row in store.institution_list(info["id"], timeline_id)}
    assert offices["off-1"]["holder"] == "en-1", "有在任者"
    assert offices["off-2"]["holder"] == "", "空缺也是一个正常初始状态"
    assert offices["off-1"]["institution_name"] == "堤长议会"
    customs = store.custom_list(info["id"], timeline_id)
    assert [row["custom_id"] for row in customs] == ["cus-1"]
    assert customs[0]["form"] == "大退潮首日在滩口设盐与旧堤砖，读水位尺后散去"
    assert customs[0]["forms"], "允许范围随行保存，判定不用再查包"


def test_declared_institutions_must_be_consistently_interpretable() -> None:
    """制度/惯例的一致解释（§十 残余 + 附录 C #13）：跨制度职位 id 唯一、在任者闭合、空缺可判定。

    「与史料、节庆对齐」的落点是**结构引用闭合**：节庆模板与史料条目的 `institution_state` /
    `custom_state` 目标走 `_all_ids`（含制度 / 职位 / 惯例），指向未声明对象在创建期即失败。
    """
    assert validate_package(sample_package()) == [], "样本包本身要合法"

    dup = sample_package()
    other = json.loads(json.dumps(dup["world"]["institutions"][0]))
    other["id"] = "inst-2"
    other["offices"][0]["id"] = dup["world"]["institutions"][0]["offices"][0]["id"]
    dup["world"]["institutions"].append(other)
    assert any("跨制度重名" in item for item in validate_package(dup)), validate_package(dup)

    dangling = sample_package()
    dangling["world"]["institutions"][0]["offices"][0]["holder"] = "en-不存在"
    assert any("在任者不是已登记的实体" in item for item in validate_package(dangling)), validate_package(dangling)

    vacant_ok = sample_package()
    vacant_ok["world"]["institutions"][0]["offices"][0]["holder"] = ""
    assert validate_package(vacant_ok) == [], "空串 = 空缺，合法"

    undecidable = sample_package()
    undecidable["world"]["institutions"][0]["offices"][0]["holder"] = ""
    undecidable["world"]["institutions"][0]["vacancy_policy"] = {"continues": [], "suspended": []}
    assert any("无法被一致解释" in item for item in validate_package(undecidable)), validate_package(undecidable)


def test_vacancy_rules_are_decidable() -> None:
    """空缺期间哪件事继续、哪件事暂停：按声明判，未声明的事务不给答案。"""
    package = sample_package()
    office = office_index(package)["off-2"]
    assert institutions.matter_status({**office, "holder": "en-1"}, "通行牌发放") == institutions.ACTIVE
    vacant = {**office, "holder": ""}
    assert institutions.matter_status(vacant, "日常堤务") == institutions.CONTINUES
    assert institutions.matter_status(vacant, "通行牌发放") == institutions.SUSPENDED
    assert institutions.matter_status(vacant, "发放盐引") is None, "未声明的事务不默认照旧也不默认停摆"


def test_change_only_within_declared_range() -> None:
    """越界的变化在校验期就被挡：未声明的职位 / 惯例、不在范围内的做法、非登记在任者。"""
    package = sample_package()
    assert change_allowed(package, kind="institution_state", target="off-1", value="en-1")[0]
    assert not change_allowed(package, kind="institution_state", target="off-9", value="")[0]
    assert not change_allowed(package, kind="institution_state", target="off-1", value="某人")[0]
    assert not change_allowed(package, kind="custom_state", target="cus-1", value="随便编个做法")[0]
    assert change_allowed(
        package, kind="custom_state", target="cus-1", value="改在城邦石阶设本地盐样，环节顺序不改"
    )[0]

    broken = sample_package()
    broken["events"]["families"][0]["templates"][0]["effects"][3] = {
        "kind": "custom_state",
        "target": "cus-1",
        "value": "凭空多出来的做法",
        "expiry": "until_cleared",
    }
    assert any("允许变化范围" in item for item in validate_package(broken))

    undeclared = sample_package()
    undeclared["events"]["families"][0]["templates"][0]["effects"][3] = {
        "kind": "institution_state",
        "target": "off-不存在",
        "value": "",
        "expiry": "until_cleared",
    }
    assert any("未声明的职位" in item for item in validate_package(undeclared))


def test_effects_land_with_source_and_time(store, world) -> None:
    """合法事件效果改制度状态；变化带来源标识与发生时刻，同一效果不重复落。"""
    info, timeline_id, _, package = _seeded(store, world)
    world_service = RuntimeService(store)
    instance = store.instance_get(info["id"])
    effect = {
        "kind": "institution_state",
        "target": "off-2",
        "value": "en-1",
        "event_id": "ev-inst-1",
        "expiry": "until_cleared",
    }
    rows = store.institution_list(info["id"], timeline_id)
    result = world_service._institution_rows(
        instance, info["id"], timeline_id, [effect], from_world=DAY * 1500, to_world=DAY * 1501
    )
    assert [row["office_id"] for row in result["institution"]] == ["off-2"]
    changed = result["institution"][0]
    assert changed["holder"] == "en-1" and changed["source"] == "ev-inst-1"
    assert changed["from_world"] == DAY * 1501, "变化带发生时刻"

    # 同值再来一次：不是变化，不落
    applied, _ = institutions.apply_effects(
        [dict(row) for row in rows], [], [effect], package, world_seconds=DAY * 1501
    )
    same = [row for row in applied if row["office_id"] == "off-2"][0]
    again, _ = institutions.apply_effects(
        [dict(same)], [],
        [{**effect, "event_id": "ev-inst-2"}],
        package,
        world_seconds=DAY * 1502,
    )
    assert [row for row in again if row["office_id"] == "off-2"][0]["source"] == "ev-inst-1", (
        "值没变就不算新事实"
    )


def test_nothing_changes_without_legal_candidates(store, world) -> None:
    """没有合法候选的时间段不凭空产生变化：不相关的效果与越界效果都改不动状态。"""
    info, timeline_id, _, package = _seeded(store, world)
    rows = store.institution_list(info["id"], timeline_id)
    customs = store.custom_list(info["id"], timeline_id)
    unrelated = [
        {"kind": "route_blocked", "target": "src-2", "event_id": "ev-x"},
        {"kind": "environment_state", "target": "env-2", "value": "西", "event_id": "ev-x"},
        {"kind": "custom_state", "target": "cus-1", "value": "越界做法", "event_id": "ev-x"},
        {"kind": "institution_state", "target": "off-1", "value": "不存在的实体", "event_id": "ev-x"},
    ]
    new_rows, new_customs = institutions.apply_effects(
        [dict(row) for row in rows], [dict(row) for row in customs], unrelated, package,
        world_seconds=DAY * 1600,
    )
    assert {(row["office_id"], row["holder"], row["updated_world"]) for row in new_rows} == {
        (row["office_id"], row["holder"], row["updated_world"]) for row in rows
    }
    assert [row["form"] for row in new_customs] == [row["form"] for row in customs]


def test_split_batches_yield_same_facts(store, world) -> None:
    """固定规则与前序状态下，一次推进与分批推进产生相同事实（阶段 6 验证）。"""
    _, _, _, package = _seeded(store, world)
    rows = institutions.office_rows(package, instance_id="i", timeline_id="t", world_seconds=0)
    customs = institutions.custom_rows(package, instance_id="i", timeline_id="t", world_seconds=0)
    first = [
        {"kind": "institution_state", "target": "off-2", "value": "en-1", "event_id": "ev-a"},
    ]
    second = [
        {"kind": "custom_state", "target": "cus-1", "value": "改在城邦石阶设本地盐样，环节顺序不改", "event_id": "ev-b"},
        {"kind": "institution_state", "target": "off-2", "value": "", "event_id": "ev-c"},
    ]
    one_shot = institutions.apply_effects(
        [dict(row) for row in rows], [dict(row) for row in customs], first + second, package,
        world_seconds=DAY * 1502,
    )[0]
    step_one = institutions.apply_effects(
        [dict(row) for row in rows], [dict(row) for row in customs], first, package, world_seconds=DAY * 1501
    )
    step_two = institutions.apply_effects(
        step_one[0], step_one[1], second, package, world_seconds=DAY * 1502
    )[0]
    key = lambda items: sorted((row["office_id"], row["holder"], row["from_world"]) for row in items)
    assert key(one_shot) == key(step_two), "同一前序状态与效果序列：分批与一次推进同结果"


def test_character_sees_state_only_after_knowing(store, world) -> None:
    """认知路径：初始状态是公开常识，后续变化只有获知了来源事件的角色才看得见。"""
    info, timeline_id, character_id, package = _seeded(store, world)
    rows = store.institution_list(info["id"], timeline_id)
    customs = store.custom_list(info["id"], timeline_id)
    holder_names = {
        str(item.get("id")): str(item.get("name")) for item in package.get("entities") or []
    }

    before = institutions.observations(rows, customs, set(), world_seconds=DAY * 1500, holder_names=holder_names)
    assert {item["name"] for item in before} == {"堤长议会·堤长", "堤长议会·守碑人", "退潮祭"}

    # 世界已变：堤长身故出缺（来源 ev-death-1）；她听没听说，决定她按哪种说法讲
    changes = [
        {"kind": "institution_state", "target": "off-1", "value": "en-1",
         "event_id": "ev-prev", "from_world": DAY * 1501},
        {"kind": "institution_state", "target": "off-1", "value": "",
         "event_id": "ev-death-1", "from_world": DAY * 1502},
    ]
    unaware = institutions.observations(
        rows, customs, {"cf-1"}, world_seconds=DAY * 1503, holder_names=holder_names, effects=changes
    )
    assert [item["value"] for item in unaware if item["name"] == "堤长议会·堤长"] == ["堤禾在任"], (
        "没获知就还按旧状态说，也不会因为别人改了就把这件事忘了"
    )

    half = institutions.observations(
        rows, customs, {"ev-prev"}, world_seconds=DAY * 1503, holder_names=holder_names, effects=changes
    )
    assert [item["value"] for item in half if item["name"] == "堤长议会·堤长"] == ["堤禾在任"], (
        "只听说前一次变化的人停在他知道的那一版"
    )

    aware = institutions.observations(
        rows, customs, {"ev-death-1"}, world_seconds=DAY * 1503, holder_names=holder_names, effects=changes
    )
    line = [item for item in aware if item["name"] == "堤长议会·堤长"][0]
    assert line["value"] == "空缺"
    assert "通行牌发放" in line["note"] and "日常堤务" in line["note"], "空缺期的事务规则一起可见"

    # 惯例同理：换了做法，但她没听说就还是照老规矩讲
    custom_changes = [
        {"kind": "custom_state", "target": "cus-1", "value": "改在城邦石阶设本地盐样，环节顺序不改",
         "event_id": "ev-cus-1", "from_world": DAY * 1502},
    ]
    old_custom = institutions.observations(
        rows, customs, set(), world_seconds=DAY * 1503, holder_names=holder_names, effects=custom_changes
    )
    assert [item["value"] for item in old_custom if item["name"] == "退潮祭"] == [
        "现行做法：大退潮首日在滩口设盐与旧堤砖，读水位尺后散去"
    ]
    new_custom = institutions.observations(
        rows, customs, {"ev-cus-1"}, world_seconds=DAY * 1503, holder_names=holder_names,
        effects=custom_changes,
    )
    assert [item["value"] for item in new_custom if item["name"] == "退潮祭"] == [
        "现行做法：改在城邦石阶设本地盐样，环节顺序不改"
    ]

    # 进扮演定义：投影确实被服务层带上（不是只存在于纯函数里）
    snapshot = world.character_snapshot(
        info["id"], timeline_id, character_id, world_seconds=DAY * 1500
    )
    assert [item["name"] for item in snapshot["institutions"]], "认知投影接进了角色快照"
