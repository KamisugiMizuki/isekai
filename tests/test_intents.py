"""角色打算与局部故事收束（阶段 3 收尾，WORLD_RUNTIME_SPEC §11.3 / §11.4）。

覆盖 DESIGN 阶段 3 的验收形态：正常活动 → 在意的目标 → 可知阻碍 → 选择 / 执行 → 持续后果与收束。
"""

from __future__ import annotations

import json
import time

from isekai_core.runtime import intents
from isekai_core.runtime.service import RuntimeService
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _card_with(package, **intent) -> dict:
    card = sample_card(package)
    card["intents"] = [intent]
    return card


def _make(store, world_service, card):
    info = create_instance(store, sample_package(), [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world_service.ensure_instance(info["id"], now_real=1.7e9)
    return info, timeline_id, str(card["meta"]["card_id"])


def test_card_intents_land_as_character_state(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    rows = store.intent_list(info["id"], timeline_id)
    assert len(rows) == 1, "卡片的打算落成角色状态"
    row = rows[0]
    assert row["stage"] == "adopted"
    assert row["object"] and row["basis"], "打算要有对象与角色已知依据"
    assert int(row["window_to"]) > int(row["window_from"])


def test_intent_waits_for_preconditions(store) -> None:
    """条件未到 → 等条件，且记下角色可知的阻碍（§11.3）。

    卡片的打算只能引用包内已登记内容；指向运行期事件（`ev-`）的打算由运行层写入
    （新获知 / 用户建议之后角色有依据地采纳）。
    """
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=watermark,
        catching_up=False,
        intents=[
            {
                "id": "in-x",
                "instance_id": info["id"],
                "timeline_id": timeline_id,
                "character_id": character_id,
                "object": "等那份潮位告警的抄本重新归档",
                "basis": "她见过编号，但抄本不在她手上",
                "strength": 0.6,
                "window_from": DAY * 1500,
                "window_to": DAY * 1600,
                "preconditions": '["ev-not-yet"]',
                "effect": '{"kind": "public_notice", "target": "src-1", "expiry": "with_cause"}',
                "stage": "adopted",
                "note": "",
                "source_world": watermark,
                "updated_world": watermark,
            }
        ],
    )
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + DAY)
    row = next(item for item in store.intent_list(info["id"], timeline_id, character_id) if item["id"] == "in-x")
    assert row["stage"] == "waiting", "条件不足不能提交事件"
    assert "条件未到" in str(row["note"]), "记下角色可知的阻碍"
    events = store.event_window(info["id"], timeline_id, until=10**12, limit=200)
    assert not [
        item
        for item in events
        if item["source"] == "character_action" and str(item["template"]) == "in-x"
    ], "没执行就没有事件"


def test_intent_acts_when_window_arrives_and_effect_is_supported(store) -> None:
    """窗口到来且条件满足 → 提交受支持行动事件、效果与经历，打算进入达成（§11.3）。"""
    world_service = RuntimeService(store)
    package = sample_package()
    moment = DAY * 1500
    card = _card_with(
        package,
        id="in-y",
        object="把通行牌延误记进抄存并递进信报",
        basis="她经手的交接记录",
        strength=0.8,
        window={"from": moment, "to": moment + 5 * DAY},
        preconditions=["cf-1"],
        effect={"kind": "public_notice", "target": "src-1", "expiry": "with_cause"},
    )
    info, timeline_id, character_id = _make(store, world_service, card)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + DAY)

    row = store.intent_list(info["id"], timeline_id, character_id)[0]
    assert row["stage"] == "done", f"条件满足即执行，实际 {row['stage']}：{row['note']}"
    actions = [
        item
        for item in store.event_window(info["id"], timeline_id, until=10**12, limit=200)
        if item["source"] == "character_action"
    ]
    assert len(actions) == 1, "行动只提交一次"
    assert actions[0]["summary"] == "把通行牌延误记进抄写并递进信报".replace("抄写", "抄存")
    effects = store.effect_window(info["id"], timeline_id, until=10**12)
    assert any(str(item["event_id"]) == str(actions[0]["id"]) for item in effects), "效果挂在行动事件上"
    experiences = store.experience_window(info["id"], timeline_id, character_id, until=10**12, limit=200)
    assert any(str(item.get("source_ref")) == "in-y" for item in experiences), "行动留下自己的经历"

    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    again = [item for item in store.event_window(info["id"], timeline_id, until=10**12, limit=200)
             if item["source"] == "character_action"]
    assert len(again) == 1, "重放不重复提交"


def test_intent_defers_then_abandons(store) -> None:
    """窗口已过且条件仍未满足：先延期、再放弃，都留原因（§11.3）。"""
    world_service = RuntimeService(store)
    moment = DAY * 1500
    info, timeline_id, character_id = make_instance(store, world_service)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=watermark,
        catching_up=False,
        intents=[
            {
                "id": "in-z",
                "instance_id": info["id"],
                "timeline_id": timeline_id,
                "character_id": character_id,
                "object": "在汛前把堤志补录完",
                "basis": "汛期逼得紧，可她手上没有编年全本",
                "strength": 0.5,
                "window_from": moment,
                "window_to": moment + 100,
                "preconditions": '["ev-never"]',
                "effect": '{"kind": "public_notice", "target": "src-1", "expiry": "with_cause"}',
                "stage": "adopted",
                "note": "",
                "source_world": watermark,
                "updated_world": watermark,
            }
        ],
    )
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + DAY)
    row = next(item for item in store.intent_list(info["id"], timeline_id, character_id) if item["id"] == "in-z")
    assert row["stage"] == "deferred", row["note"]
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    row = next(item for item in store.intent_list(info["id"], timeline_id, character_id) if item["id"] == "in-z")
    assert row["stage"] == "abandoned", "延期后仍不满足即放弃，不无限拖着"
    assert "放弃" in str(row["note"])


def test_blocked_intent_records_known_obstacle(store) -> None:
    """仍有效的后果挡住打算时，只记「受阻于什么」，不假装执行（§六、§11.3）。"""
    world_service = RuntimeService(store)
    package = sample_package()
    moment = DAY * 1500
    card = _card_with(
        package,
        id="in-w",
        object="下滩去量新刻线",
        basis="值守的活，她自己排的班",
        strength=0.7,
        window={"from": moment, "to": moment + 5 * DAY},
        preconditions=["cf-1"],
        effect={"kind": "activity_constraint", "target": "rl-1", "expiry": "until_cleared"},
    )
    info, timeline_id, character_id = _make(store, world_service, card)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    generation = int(store.clock_get(timeline_id)["generation"])
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=generation,
        processed_world=watermark,
        catching_up=False,
        effects=[
            {
                "id": "fx-block",
                "instance_id": info["id"],
                "timeline_id": timeline_id,
                "event_id": "ev-block",
                "target": "rl-1",
                "kind": "route_blocked",
                "family": "",
                "from_world": watermark,
                "expiry": "until_cleared",
                "recovery": "",
                "active": 1,
                "cleared_at": None,
            }
        ],
    )
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + DAY)
    row = store.intent_list(info["id"], timeline_id, character_id)[0]
    assert row["stage"] == "waiting" and "受阻于" in str(row["note"]), row["note"]


def test_story_units_are_a_derived_view(store) -> None:
    """局部故事单元 = 可追溯组合视图，不另立事实（§11.4）。"""
    world_service = RuntimeService(store)
    package = sample_package()
    moment = DAY * 1500
    card = _card_with(
        package,
        id="in-s",
        object="把信报里那条抄进自己的册子",
        basis="她抄存信报的差事",
        strength=0.7,
        window={"from": moment, "to": moment + 5 * DAY},
        preconditions=["cf-1"],
        effect={"kind": "public_notice", "target": "src-1", "expiry": "with_cause"},
    )
    info, timeline_id, character_id = _make(store, world_service, card)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + DAY)

    rows = store.intent_list(info["id"], timeline_id, character_id)
    events = store.event_window(info["id"], timeline_id, until=10**12, limit=200)
    units = intents.story_units(rows, events)
    assert units and units[0]["terminal"] == "达成"
    assert units[0]["events"], "故事单元引用产生它的事件"
    assert units[0]["basis"], "保留起点依据"
    assert units[0]["unresolved"] is False
    # 已固化的计划与事件不受影响：故事视图不写任何表
    before = len(store.event_window(info["id"], timeline_id, until=10**12, limit=300))
    intents.story_units(rows, events)
    assert len(store.event_window(info["id"], timeline_id, until=10**12, limit=300)) == before


def test_intents_survive_export_import(store) -> None:
    from isekai_core.world.portable import build_container, import_instance

    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    container = build_container(store, info["id"])
    state = next(iter(container["runtime"]["state"].values()))
    assert state["intents"], "打算随容器导出"
    imported = import_instance(store, container, display_name="打算副本")
    new_line = store.timeline_list(imported["id"])[0]["id"]
    assert len(store.intent_list(imported["id"], new_line, character_id)) == len(
        store.intent_list(info["id"], timeline_id, character_id)
    )


def test_intent_reaches_play_definition(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    prompt = world_service.system_prompt(
        {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id}
    )
    assert "她自己惦记着的事" in prompt, "打算进入扮演定义（问到可以说，不必主动播报）"
    assert "把今年春汛的通行牌发放延误记进抄存" in prompt
    _ = time
    _ = json
