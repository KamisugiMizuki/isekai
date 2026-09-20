"""阶段 2 审计补齐（对照 WORLD_RUNTIME_SPEC §2.2–2.8 / §3 / §4 / §10 / §11 / §13 与附录 B）。

这里只放审计新加的行为断言：上限与确认倍率、激活数量上限、世代失效、批的原子性、
追赶受限与一致性错误、导出导入的运行层快照、补卡、查询主题、生活线边界形态。
"""

from __future__ import annotations

import json
import time

import pytest

from isekai_core.runtime import cognition, life, personality
from isekai_core.runtime.calendar import calendar_from_package
from isekai_core.runtime.service import RuntimeService, RuntimeStateError
from isekai_core.world.instances import create_instance
from isekai_core.world.portable import build_container, import_instance
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  运行层夹具定义在那边


# ---------- §2.2 / §2.4：倍率上限与确认 ----------


def test_rate_max_comes_from_settings_and_bounds_set_rate(store) -> None:
    world = RuntimeService(store, rate_max=100)
    info, timeline_id, _ = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    with pytest.raises(RuntimeStateError):
        world.set_rate(info["id"], timeline_id, rate=101, now_real=1_700_000_000.0)
    assert world.set_rate(info["id"], timeline_id, rate=100, now_real=1_700_000_000.0)["rate"] == 100


def test_activate_requires_confirmation_when_stored_rate_exceeds_cap(store) -> None:
    """上限调低 / 导入端上限更低：不静默改写历史倍率，保持冻结并要求确认（§2.4）。"""
    open_world = RuntimeService(store, rate_max=100000)
    info, timeline_id, _ = make_instance(store, open_world)
    open_world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    open_world.set_rate(info["id"], timeline_id, rate=50000, now_real=1_700_000_000.0)
    open_world.advance(info["id"], timeline_id, now_real=1_700_000_060.0)
    open_world.freeze(info["id"], timeline_id, now_real=1_700_000_060.0)
    assert store.clock_get(timeline_id)["rate"] == 50000

    tight = RuntimeService(store, rate_max=1000)
    with pytest.raises(RuntimeStateError) as excinfo:
        tight.activate(info["id"], timeline_id, now_real=1_700_000_100.0)
    assert "确认" in str(excinfo.value)
    assert store.clock_get(timeline_id)["rate"] == 50000, "拒绝时不改写历史倍率"
    view = tight.activate(info["id"], timeline_id, now_real=1_700_000_100.0, rate=1000)
    assert view["rate"] == 1000 and store.clock_get(timeline_id)["rate"] == 1000


def test_max_active_timelines_is_enforced(store) -> None:
    world = RuntimeService(store, max_active_timelines=1)
    first, first_line, _ = make_instance(store, world, moment=DAY * 1500)
    second = create_instance(store, sample_package(moment=DAY * 2000), [sample_card(sample_package())])
    second_line = store.timeline_list(second["id"])[0]["id"]
    world.ensure_instance(second["id"], now_real=1_700_000_000.0)

    world.activate(first["id"], first_line, now_real=1_700_000_000.0)
    with pytest.raises(RuntimeStateError) as excinfo:
        world.activate(second["id"], second_line, now_real=1_700_000_000.0)
    assert "上限" in str(excinfo.value)
    world.freeze(first["id"], first_line, now_real=1_700_000_010.0)
    assert world.activate(second["id"], second_line, now_real=1_700_000_020.0)["state"] == "active"


# ---------- §4 / §2.2：运行世代 ----------


def test_freeze_bumps_generation_and_stale_batch_is_rejected(store) -> None:
    world = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    generation = int(store.clock_get(timeline_id)["generation"])
    world.advance(info["id"], timeline_id, now_real=1_700_000_060.0)
    before = int(store.clock_get(timeline_id)["processed_world"])

    world.freeze(info["id"], timeline_id, now_real=1_700_000_060.0)
    after_freeze = int(store.clock_get(timeline_id)["generation"])
    assert after_freeze == generation + 1, "冻结使旧世代任务失效"

    landed = store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=generation,  # 迟到任务带旧世代
        processed_world=before + 10 * DAY,
        catching_up=False,
        experiences=[
            {
                "id": "xp-late",
                "instance_id": info["id"],
                "timeline_id": timeline_id,
                "character_id": character_id,
                "world_seconds": before + DAY,
                "kind": "life",
                "summary": "迟到批次",
                "source_ref": None,
                "confidence": "experienced",
            }
        ],
    )
    assert landed is False, "旧世代批次整批不落盘"
    assert int(store.clock_get(timeline_id)["processed_world"]) == before
    assert store.experience_window(info["id"], timeline_id, character_id, until=before + 10 * DAY) == []


def test_batch_is_atomic(store) -> None:
    """一批失败即整批回到批前水位，不留半批数据（§2.7）。"""
    world = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world.advance(info["id"], timeline_id, now_real=1_700_000_030.0)
    row = store.clock_get(timeline_id)
    before = int(row["processed_world"])
    plans_before = len(json.loads(str(store.plan_latest(info["id"], timeline_id, character_id)["windows"]))["windows"])

    with pytest.raises(Exception):
        store.apply_runtime_batch(
            timeline_id=timeline_id,
            generation=int(row["generation"]),
            processed_world=before + DAY,
            catching_up=True,
            plans=[{"id": "lp-bad"}],  # 缺列 → 中途失败
        )
    assert int(store.clock_get(timeline_id)["processed_world"]) == before, "水位不动"
    assert int(store.clock_get(timeline_id)["catching_up"]) == 0, "标记也不动"
    latest = store.plan_latest(info["id"], timeline_id, character_id)
    assert len(json.loads(str(latest["windows"]))["windows"]) == plans_before


# ---------- §2.6：追赶受限与一致性错误 ----------


def test_catching_up_and_limited_are_persisted(store) -> None:
    world = RuntimeService(store, catch_up_batches=1, catch_up_lag_seconds=DAY)
    info, timeline_id, _ = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    result = world.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 3 * DAY * 4)
    assert result["state"] == "catching_up" and result["batches"] == 1
    row = store.clock_get(timeline_id)
    assert int(row["catching_up"]) == 1, "追赶状态持久化"
    assert int(row["limited"]) == 1, "滞后超过预算 → 追赶受限"

    target_now = 1_700_000_000.0 + 3 * DAY * 4
    for _ in range(30):  # 单批预算：分多轮继续推进直到追平
        final = world.advance(info["id"], timeline_id, now_real=target_now)
        if final["state"] == "current":
            break
    assert final["state"] == "current", final
    row = store.clock_get(timeline_id)
    assert int(row["catching_up"]) == 0 and int(row["limited"]) == 0, "追平后退出受限状态"


def test_watermark_ahead_of_target_is_reported_as_inconsistent(store) -> None:
    world = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    store.clock_set_processed(timeline_id, 10**9)
    result = world.advance(info["id"], timeline_id, now_real=1_700_000_001.0)
    assert result["state"] == "inconsistent", "处理水位超过合法目标另记为一致性错误"


# ---------- §2.6：导出导入带完成水位快照 ----------


def test_export_import_carries_runtime_state_at_watermark(store) -> None:
    world = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 2 * DAY)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    units_before = len(store.unit_list(info["id"], timeline_id, character_id))
    experiences_before = len(store.experience_window(info["id"], timeline_id, character_id, until=watermark, limit=99))

    container = build_container(store, info["id"])
    state = container["runtime"]["state"][timeline_id]
    assert state["watermark"] == watermark
    assert len(state["experiences"]) == experiences_before, "按已完成水位导出"
    assert not any(key in container["runtime"] for key in ("rate_command", "deliveries", "credentials"))
    payload = json.dumps(container, ensure_ascii=False)
    assert "cr-" not in payload and "tk-" not in payload

    imported = import_instance(store, container, display_name="快照线")
    new_line = store.timeline_list(imported["id"])[0]["id"]
    assert store.timeline_list(imported["id"])[0]["state"] == "frozen", "导入线一律冻结"
    assert len(store.unit_list(imported["id"], new_line, character_id)) == units_before
    assert (
        len(store.experience_window(imported["id"], new_line, character_id, until=watermark, limit=99))
        == experiences_before
    )
    clock = store.clock_get(new_line)
    assert int(clock["processed_world"]) == watermark, "导入时钟停在导出水位（不补算旧间隔）"
    assert store.rate_pending(new_line) == [], "不恢复待生效倍率命令"

    # 导入的线保持冻结：不激活就不推进
    world.advance(imported["id"], new_line, now_real=1_700_000_000.0 + 5 * DAY)
    assert int(store.clock_get(new_line)["processed_world"]) == watermark


# ---------- §九 / 附录 B #18：补卡 ----------


def test_add_character_anchors_join_and_projects_knowledge(store) -> None:
    world = RuntimeService(store)
    info, timeline_id, first_character = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 2 * DAY)
    watermark = int(store.clock_get(timeline_id)["processed_world"])

    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    newcomer = sample_card(package)
    newcomer["meta"]["card_id"] = "cc-newcomer"
    newcomer["meta"]["confirmed"] = True
    newcomer["identity"]["name"] = "后来的角色"

    join = world.add_character(
        info["id"], timeline_id, newcomer, now_real=1_700_000_100.0, acquainted=True, note="第二卡"
    )
    assert join["joined_world"] == watermark, "缺省锚定该线已完成水位"
    assert join["timeline_state"] == "active", "补入本身不改变线的状态"

    # 从加入点起才出现在该线的角色集合里
    assert "cc-newcomer" not in {
        str((card.get("meta") or {}).get("card_id"))
        for card in world.cards(store.instance_get(info["id"]), timeline_id=timeline_id, world_seconds=watermark - 1)
    }
    assert "cc-newcomer" in {
        str((card.get("meta") or {}).get("card_id"))
        for card in world.cards(store.instance_get(info["id"]), timeline_id=timeline_id, world_seconds=watermark)
    }
    units = store.unit_list(info["id"], timeline_id, "cc-newcomer")
    assert units and any(row["semantic"] == "与联络者已相识" for row in units), "已相识只补一条对话单元"
    prompt = world.system_prompt(
        {
            "instance_id": info["id"],
            "timeline_id": timeline_id,
            "character_id": "cc-newcomer",
        }
    )
    assert "后来的角色" in prompt, "补入角色可正常进入扮演定义"

    # 越权与重复
    with pytest.raises(RuntimeStateError):
        world.add_character(info["id"], timeline_id, newcomer, now_real=1_700_000_200.0)
    too_late = dict(newcomer)
    with pytest.raises(RuntimeStateError):
        world.add_character(
            info["id"], timeline_id, too_late, now_real=1_700_000_200.0, joined_world=watermark + DAY
        )
    third = sample_card(package)
    third["meta"]["card_id"] = "cc-third"
    third["meta"]["confirmed"] = True
    third["identity"]["born"] = watermark + 10 * DAY
    with pytest.raises(RuntimeStateError):
        world.add_character(info["id"], timeline_id, third, now_real=1_700_000_300.0)

    fourth = sample_card(package)
    fourth["meta"]["card_id"] = "cc-fourth"
    fourth["meta"]["confirmed"] = False
    with pytest.raises(RuntimeStateError):
        world.add_character(info["id"], timeline_id, fourth, now_real=1_700_000_300.0)


def test_add_character_on_frozen_line_does_not_activate_it(store) -> None:
    world = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    frozen_card = sample_card(package)
    frozen_card["meta"]["card_id"] = "cc-frozen"
    frozen_card["meta"]["confirmed"] = True
    join = world.add_character(info["id"], timeline_id, frozen_card, now_real=1_700_000_000.0)
    assert join["timeline_state"] == "frozen"
    assert store.timeline_list(info["id"])[0]["state"] == "frozen", "补卡不激活冻结线"
    assert join["joined_world"] == DAY * 1500, "冻结线补入锚定冻结时刻"


# ---------- §13.1：查询主题 ----------


def test_knowledge_slice_orders_by_topic_without_hiding() -> None:
    package = sample_package()
    card = sample_card(package)
    everything = cognition.knowledge_slice(package, card, world_seconds=10**9)
    ordered = cognition.knowledge_slice(
        package, card, world_seconds=10**9, topic=everything[-1]["text"][:6]
    )
    assert {item["text"] for item in ordered} == {item["text"] for item in everything}, "主题只排序不过滤"
    assert ordered[0]["text"] == everything[-1]["text"], "命中的条目排前面"


# ---------- §11 / 附录 B #7：生活线边界形态 ----------


def _night_and_sleepless_cards(package):
    night = sample_card(package)
    night["life_template"] = {
        "sleep": True,
        "routine_note": "夜班",
        "windows": [
            {"start": 0, "end": 10800, "activity": "sleep", "alternatives": [], "note": ""},
            {"start": 10800, "end": 28800, "activity": "duty", "alternatives": [], "note": ""},
            {"start": 28800, "end": 54000, "activity": "sleep", "alternatives": [], "note": "白天补觉"},
            {"start": 54000, "end": 86400, "activity": "duty", "alternatives": [], "note": ""},
        ],
    }
    sleepless = sample_card(package)
    sleepless["life_template"] = {
        "sleep": False,
        "routine_note": "无睡眠",
        "windows": [{"start": 0, "end": DAY, "activity": "watch", "alternatives": [], "note": ""}],
    }
    return night, sleepless


def test_life_plan_shares_one_day_boundary(store) -> None:
    package = sample_package()
    calendar = calendar_from_package(package)
    night, sleepless = _night_and_sleepless_cards(package)
    day_index = 1500

    night_plan = life.expand_plan(
        night, calendar, day_index=day_index, instance_id="in-x", timeline_id="tl-x", created_world=0
    )
    sleepless_plan = life.expand_plan(
        sleepless, calendar, day_index=day_index, instance_id="in-x", timeline_id="tl-x", created_world=0
    )
    night_windows = json.loads(night_plan["windows"])["windows"]
    sleepless_windows = json.loads(sleepless_plan["windows"])["windows"]
    for windows in (night_windows, sleepless_windows):
        assert min(item["start"] for item in windows) >= day_index * DAY
        assert max(item["end"] for item in windows) <= (day_index + 1) * DAY, "活动不跨越世界日界"
    assert night_windows[-1]["activity"] == "duty", "夜班晚段仍在同一世界日"
    assert sleepless_windows[0]["end"] - sleepless_windows[0]["start"] == DAY, "无睡眠角色覆盖整日"
    current = life.current_window(night_plan, day_index * DAY + 60000)
    assert current and current["activity"] == "duty"
    assert life.current_window(night_plan, (day_index + 1) * DAY - 1) is not None


def test_cross_midnight_sleep_is_split_and_not_experienced_early(store) -> None:
    """跨日睡眠：日首段属前一日的尾巴；未来窗口不算经历（§11、附录 B #7）。"""
    package = sample_package()
    calendar = calendar_from_package(package)
    card = sample_card(package)
    # 跨日窗口是 sleep：它的尾巴落在次日日首（[0, 21600)），其余活动不得与之重叠
    card["life_template"] = {
        "sleep": True,
        "routine_note": "",
        "windows": [
            {"start": 21600, "end": 82800, "activity": "duty", "alternatives": [], "note": ""},
            {"start": 82800, "end": 86400 + 21600, "activity": "sleep", "alternatives": [], "note": "跨午夜睡眠"},
        ],
    }
    errors = __import__("isekai_core.world.cards", fromlist=["validate_card"]).validate_card(
        card, package, moment=DAY * 1500
    )
    assert errors == [], f"跨日模板应通过校验：{errors}"
    plan = life.expand_plan(
        card, calendar, day_index=1500, instance_id="in-x", timeline_id="tl-x", created_world=0
    )
    windows = json.loads(plan["windows"])["windows"]
    tail = windows[-1]
    assert tail["end"] == 1501 * DAY + 21600, "跨日睡眠按世界秒展开到次日日首"
    assert life.current_window(plan, 1501 * DAY + 3600) == tail, "次日日首仍在前一日的睡眠块里"

    world = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + DAY)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    plan = store.plan_get(info["id"], timeline_id, sample_card(package)["meta"]["card_id"], calendar.day_index(watermark))
    assert plan is not None
    windows = json.loads(plan["windows"])["windows"]
    future = [item for item in windows if int(item["end"]) > watermark]
    assert future, "存在尚未发生的时间窗"
    experiences = store.experience_window(info["id"], timeline_id, sample_card(package)["meta"]["card_id"], until=watermark, limit=99)
    assert all(int(row["world_seconds"]) <= watermark for row in experiences), "经历不越过完成水位"


# ---------- §10：四驱动的真实入口 ----------


def test_dialog_and_event_drives_change_units_and_stay_hidden(store) -> None:
    world = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + DAY)
    watermark = int(store.clock_get(timeline_id)["processed_world"])

    unit = world.drive_unit(
        info["id"],
        timeline_id,
        character_id,
        driver="dialog",
        semantic="对联络者起了兴趣",
        basis="连续两轮对话都在追问同一个人",
        strength=0.8,
        direction=1,
        source_key="turn-1",
        world_seconds=watermark,
    )
    assert unit and unit["mode"] == "dialog"
    again = world.drive_unit(
        info["id"],
        timeline_id,
        character_id,
        driver="dialog",
        semantic="对联络者起了兴趣",
        basis="重复的同一来源",
        strength=0.8,
        direction=1,
        source_key="turn-1",
        world_seconds=watermark,
    )
    assert again is None, "同一来源只消费一次（重试不重复强化）"
    snapshot = world.character_snapshot(info["id"], timeline_id, character_id, world_seconds=watermark)
    assert any(row["semantic"] == "对联络者起了兴趣" for row in snapshot["all_units"])
    prompt = world.system_prompt(
        {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id}
    )
    assert "对联络者起了兴趣" in prompt, "单元进入表达倾向"
    assert "置信" not in prompt and "stability" not in prompt, "驱动迁移与数值不对用户可见"

def test_cross_day_window_experience_is_harvested_once(store) -> None:
    """跨日窗口（睡到次日日首）的经历在次日批次里产成，且只产一次（§11 附录 B #7）。"""
    package = sample_package()
    calendar = calendar_from_package(package)
    card = sample_card(package)
    card["life_template"] = {
        "sleep": True,
        "routine_note": "",
        "windows": [
            {"start": 21600, "end": 82800, "activity": "duty", "alternatives": [], "note": ""},
            {"start": 82800, "end": 86400 + 21600, "activity": "sleep", "alternatives": [], "note": "跨午夜睡眠"},
        ],
    }
    world = RuntimeService(store)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    character_id = card["meta"]["card_id"]
    day = calendar.day_index(DAY * 1500)
    store.plan_put(
        life.expand_plan(
            card,
            calendar,
            day_index=day,
            instance_id=info["id"],
            timeline_id=timeline_id,
            created_world=DAY * 1500,
        )
    )
    assert store.plan_get(info["id"], timeline_id, character_id, day) is not None

    # 次日批次：跨日窗口的尾部落在这一批里
    plans, units, experiences = world._collect_batch(
        info["id"],
        timeline_id,
        [card],
        calendar,
        from_world=DAY * 1501,
        to_world=DAY * 1501 + 25200,  # 跨日睡眠的尾巴落在这一批里
    )
    wrap = [item for item in experiences if item["id"] == f"xp-{character_id}-{day * DAY + 82800}"]
    assert len(wrap) == 1, "跨日窗口在次日批次里被收割"
    assert wrap[0]["world_seconds"] == (day + 1) * DAY + 21600
    assert plans and units is not None

    # 同一批重放：同一窗口不重复产成
    _, _, again = world._collect_batch(
        info["id"],
        timeline_id,
        [card],
        calendar,
        from_world=DAY * 1501,
        to_world=DAY * 1501 + 25200,  # 跨日睡眠的尾巴落在这一批里
    )
    assert [item["id"] for item in again] == [item["id"] for item in experiences]


def test_import_remaps_message_identifiers(store) -> None:
    """导入=新实例：对话消息标识重新签发，不与源实例撞唯一索引（§7.3）。"""
    world = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    session = store.session_ensure(info["id"], timeline_id, character_id)
    store.instance_import_messages(
        session["id"],
        [
            {"role": "user", "text": "在吗", "state": "fixed", "message_id": "m-fixed-1", "created_at": 1.0},
            {"role": "character", "text": "在", "state": "fixed", "message_id": "m-fixed-2", "created_at": 2.0},
        ],
    )
    container = build_container(store, info["id"])
    imported = import_instance(store, container, display_name="带对话的副本")
    rows = store.instance_messages(imported["id"])
    assert len(rows) == 2, "对话随容器导入"
    assert {row["message_id"] for row in rows}.isdisjoint({"m-fixed-1", "m-fixed-2"}), "消息标识已重映射"


def test_import_keeps_plans_and_delete_cleans_runtime(store) -> None:
    """导入保留计划（行标识重映射）；删除实例连带清理运行层，不留孤儿。"""
    world = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 3 * DAY)
    plans_before = len(store.plan_latest(info["id"], timeline_id, character_id) and [1]) and len(
        [
            store.plan_get(info["id"], timeline_id, character_id, day)
            for day in range(1499, 1504)
        ]
    )

    container = build_container(store, info["id"])
    imported = import_instance(store, container, display_name="计划副本")
    new_line = store.timeline_list(imported["id"])[0]["id"]
    payload = next(iter(container["runtime"]["state"].values()))
    assert len([row for row in payload["plans"] if row["character_id"] == character_id]) >= 1
    kept = [
        store.plan_get(imported["id"], new_line, character_id, day)
        for day in range(1499, 1504)
    ]
    assert any(item is not None for item in kept), f"导入保留计划（计划应在，plans_before={plans_before}）"
    assert store.plan_get(imported["id"], new_line, character_id, 1500)["id"].startswith("lp-")
    assert store.plan_get(imported["id"], new_line, character_id, 1500)["id"] != store.plan_get(
        info["id"], timeline_id, character_id, 1500
    )["id"], "导入的行标识必须重映射"

    store.instance_delete(imported["id"])
    assert store.plan_get(imported["id"], new_line, character_id, 1500) is None
    for table in ("unit", "life_plan", "experience", "character_join", "timeline_clock"):
        if table == "timeline_clock":
            assert store.clock_get(new_line) is None
        else:
            rows = store._conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE instance_id=?", (imported["id"],)
            ).fetchone()[0]
            assert rows == 0, f"{table} 未清理"
    assert store.instance_get(info["id"]) is not None, "源实例不受影响"
