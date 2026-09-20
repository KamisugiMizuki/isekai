"""环境事实状态（WORLD_RUNTIME_SPEC §11.2）：声明驱动、自然变化、事件改值、观察投影。"""

from __future__ import annotations

import json

from isekai_core.runtime import environment
from isekai_core.runtime.calendar import calendar_from_package
from isekai_core.runtime.service import RuntimeService
from isekai_core.world.validate import validate_package
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _package_without_env() -> dict:
    package = sample_package()
    package["environment"] = {"types": []}
    return package


def test_undeclared_environment_has_no_truth() -> None:
    """未声明的环境类型不存在可引用的真值（§11.2）。"""
    package = _package_without_env()
    assert environment.declared(package, "env-1") is None
    assert environment.initial_rows(package, instance_id="i", timeline_id="t", world_seconds=0) == []
    card = sample_card(package)
    assert environment.observations([], {}, card, world_seconds=0) == []


def test_declared_environment_requires_machine_readable_parts() -> None:
    """声明的环境类型必须带单位 / 取值域 / 观察条件 / 失效方式；环境效果只取取值域内的值。"""
    package = sample_package()
    package["environment"]["types"][0]["values"] = []
    assert any("取值域" in item for item in validate_package(package))

    package = sample_package()
    package["events"]["families"][0]["templates"][0]["effects"][2] = {
        "kind": "environment_state",
        "target": "env-2",
        "value": "上",
        "expiry": "until_cleared",
    }
    assert any("取值域内的值" in item for item in validate_package(package))

    package = sample_package()
    package["events"]["families"][0]["templates"][0]["effects"][2] = {
        "kind": "environment_state",
        "target": "env-未声明",
        "value": "北",
        "expiry": "until_cleared",
    }
    assert any("已声明的环境类型" in item for item in validate_package(package))


def test_natural_variation_is_deterministic_and_event_free(store) -> None:
    """声明的自然变化按世界时间确定地走；只由事件改变的类型不动（§11.2）。"""
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    calendar = calendar_from_package(package)
    types = environment.env_types(package)
    rows = environment.initial_rows(package, instance_id=info["id"], timeline_id=timeline_id, world_seconds=0)

    first = environment.advance_rows(rows, types, from_world=0, to_world=DAY * 10, day_seconds=DAY)
    again = environment.advance_rows(rows, types, from_world=0, to_world=DAY * 10, day_seconds=DAY)
    assert [(r["type_id"], r["value"]) for r in first] == [(r["type_id"], r["value"]) for r in again], (
        "同一时刻处处同值"
    )
    assert all(str(r["source"]).startswith("natural:") for r in first), "只有声明了自然来源的才动"
    assert not any(str(r["type_id"]) == "env-2" for r in first), "只由事件改变的类型不随时间为变"

    # 事件效果改值
    effect = {
        "kind": "environment_state",
        "target": "env-2",
        "value": "西",
        "event_id": "ev-x",
        "expiry": "until_cleared",
    }
    changed = environment.apply_effects(rows, [effect], types, world_seconds=DAY * 10)
    assert [(r["type_id"], r["value"], r["source"]) for r in changed] == [("env-2", "西", "event:ev-x")]

    out_of_domain = environment.apply_effects(
        rows, [{**effect, "value": "天上"}], types, world_seconds=DAY * 10
    )
    assert out_of_domain == [], "越出取值域的效果被挡下"
    assert environment.apply_effects(
        rows, [{**effect, "target": "env-不存在"}], types, world_seconds=DAY * 10
    ) == [], "未声明的类型改不动"


def test_observation_projection_respects_observers(store) -> None:
    """观察投影：谁在条件下看得到什么精度；没声明观察者的类型对谁都不可见（§11.2）。"""
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    types = environment.env_types(package)
    rows = environment.initial_rows(package, instance_id=info["id"], timeline_id=timeline_id, world_seconds=0)
    rows = [row for row in rows if row["type_id"] == "env-1"]

    insider = sample_card(package)  # role_id = rl-1
    seen = environment.observations(rows, types, insider, world_seconds=DAY)
    assert [item["type_id"] for item in seen] == ["env-1"]
    assert "水位尺" in seen[0]["observe"], "带上她能有的精度说明"
    assert seen[0]["value"] == "2" and seen[0]["unit"] == "尺"

    outsider = sample_card(package)
    outsider["role_id"] = "rl-other"
    outsider["identity"]["region"] = ""
    assert environment.observations(rows, types, outsider, world_seconds=DAY) == [], (
        "不在观察条件里的人拿不到真值（可以由别人转述，但不是他的观察）"
    )

    # 未声明观察者的类型：对谁都不可观察
    types_anon = {**types, "env-1": {**types["env-1"], "observers": None}}
    assert environment.observations(rows, types_anon, insider, world_seconds=DAY) == []


def test_environment_state_advances_with_the_batch(store) -> None:
    """环境状态随水位在同一批提交，事件效果改值、未声明的类型不出现（§11.2）。"""
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 5 * DAY)
    rows = store.environment_list(info["id"], timeline_id)
    assert {row["type_id"] for row in rows} == {"env-1", "env-2"}, "只推进已声明的类型"
    tide = next(row for row in rows if row["type_id"] == "env-1")
    assert int(tide["updated_world"]) <= int(store.clock_get(timeline_id)["processed_world"]), (
        "环境变化不越过已完成水位"
    )
    assert str(tide["source"]).startswith("initial") or str(tide["source"]).startswith("natural:")
    wind = next(row for row in rows if row["type_id"] == "env-2")
    if str(wind["source"]).startswith("event:"):
        assert wind["value"] == "西", "事件效果把风信改了"


def test_observations_reach_the_play_definition(store) -> None:
    """角色能观察到的环境进入扮演定义，并带「不能加数字、不能扩大范围」的约束。"""
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    prompt = world_service.system_prompt(
        {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id}
    )
    assert "她此刻能直接观察到的环境" in prompt
    assert "潮位" in prompt and "水位尺" in prompt
    assert "未列入上面的环境信息她并不知道" in prompt


def test_environment_survives_export_import(store) -> None:
    from isekai_core.world.portable import build_container, import_instance

    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 3 * DAY)
    container = build_container(store, info["id"])
    state = next(iter(container["runtime"]["state"].values()))
    assert state["environment"], "环境状态随容器导出"
    imported = import_instance(store, container, display_name="环境副本")
    new_line = store.timeline_list(imported["id"])[0]["id"]
    assert len(store.environment_list(imported["id"], new_line)) == len(
        store.environment_list(info["id"], timeline_id)
    )
