"""世界运行层（阶段 2）：历法、时钟、倍率、激活冻结、补算、性格单元、认知切片。"""

from __future__ import annotations

import json
import time

import pytest

from isekai_core.runtime import cognition, life, personality
from isekai_core.runtime.calendar import calendar_from_package
from isekai_core.runtime.clock import ClockState, RateCommand, natural_second, settle, target_world
from isekai_core.runtime.service import RuntimeService, RuntimeStateError
from isekai_core.store import Store
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package

# ---------- 历法 ----------


def test_calendar_is_deterministic_and_invertible() -> None:
    calendar = calendar_from_package(sample_package())
    assert calendar.day_seconds == DAY
    assert calendar.year_days == 90
    moment = calendar.to_world_seconds(year=13, month=2, day=3, offset=3600)
    view = calendar.to_calendar(moment)
    assert (view["year"], view["month"], view["day"]) == (13, 2, 3)
    assert view["hour"] == 1
    assert calendar.to_calendar(moment) == view, "相同输入必得相同结果"
    assert "灰潮纪" in calendar.describe(moment)
    # 纪元之前：负数可用（出生时刻）
    earlier = calendar.to_calendar(-DAY)
    assert earlier["year"] == 0
    assert calendar.day_index(-1) == -1


def test_calendar_segments_cover_the_day() -> None:
    calendar = calendar_from_package(sample_package())
    names = {calendar.segment_of(offset)["name"] for offset in range(0, DAY, 3600)}
    assert names == {"夜", "晨", "昼", "暮"}


# ---------- 时钟（纯函数） ----------


def test_natural_second_boundary() -> None:
    assert natural_second(100.0) == 101, "恰在整秒输入归属下一整秒"
    assert natural_second(100.2) == 101
    assert natural_second(100.999) == 101


def test_rate_segments_accumulate_without_jumps() -> None:
    state = ClockState(base_real=1000.0, base_world=0, rate=1, high_water_real=1000.0)
    # 1000→1010 以 rate=1 走 10 世界秒；1010 起切到 rate=10
    command = RateCommand(input_real=1005.0, effective_real=1010, rate=10, seq=1)
    settled, consumed = settle(state, 1020.0, [command])
    assert [item.rate for item in consumed] == [10]
    assert target_world(settled, 1020.0) == 10 + 100, "分段累计：旧段 10 + 新段 10×10"


def test_same_effective_second_last_wins() -> None:
    state = ClockState(base_real=0.0, base_world=0, rate=1, high_water_real=0.0)
    commands = [
        RateCommand(input_real=1.0, effective_real=5, rate=2, seq=1),
        RateCommand(input_real=2.0, effective_real=5, rate=4, seq=2),
    ]
    settled, consumed = settle(state, 10.0, commands)
    assert settled.rate == 4, "同一生效点以最后一个有效请求为准"
    assert len(consumed) == 2


def test_clock_rollback_does_not_rewind_world() -> None:
    state = ClockState(base_real=1000.0, base_world=0, rate=1, high_water_real=1000.0)
    assert target_world(state, 900.0) == 0, "时钟倒拨不倒退世界时间"
    assert target_world(state, 1100.0) == 100


# ---------- 运行层服务 ----------


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "data" / "isekai.db")
    handle.ensure_schema()
    yield handle
    handle.close()


@pytest.fixture
def world(store):
    return RuntimeService(store)


def make_instance(store, world, *, moment: int = DAY * 1500) -> tuple[dict, str, str]:
    package = sample_package(moment=moment)
    card = sample_card(package)
    info = create_instance(store, package, [card])
    timeline = store.timeline_list(info["id"])[0]
    world.ensure_instance(info["id"], now_real=time.time())
    return info, timeline["id"], str(card["meta"]["card_id"])


def test_instance_starts_frozen_with_initial_clock(store, world) -> None:
    info, timeline_id, character_id = make_instance(store, world)
    view = world.view(info["id"], timeline_id, now_real=time.time())
    assert view["state"] == "frozen"
    assert view["processed_world"] == DAY * 1500
    units = world.character_snapshot(
        info["id"], timeline_id, character_id, world_seconds=DAY * 1500
    )["all_units"]
    assert units and len(units) == 2, "初始单元来自角色卡"
    plan = store.plan_latest(info["id"], timeline_id, character_id)
    assert plan is not None, "首日计划已固化"


def test_activate_reanchors_and_advances_watermark(store, world) -> None:
    info, timeline_id, character_id = make_instance(store, world)
    now = time.time()
    view = world.activate(info["id"], timeline_id, now_real=now)
    assert view["state"] == "active"
    assert view["world_seconds"] == DAY * 1500, "激活即以当下现实时间重锚，不补冻结间隔"
    advance = world.advance(info["id"], timeline_id, now_real=now + 5)
    assert advance["state"] == "current"
    assert advance["processed_world"] <= advance["target"]
    assert advance["batches"] <= 1, "小步推进不该多跑批"


def test_frozen_timeline_does_not_advance(store, world) -> None:
    info, timeline_id, _ = make_instance(store, world)
    result = world.advance(info["id"], timeline_id, now_real=time.time() + 86400)
    assert result["state"] == "frozen"
    assert result["processed_world"] == DAY * 1500


def test_rate_change_effective_at_natural_second(store, world) -> None:
    info, timeline_id, _ = make_instance(store, world)
    now = 1_700_000_000.4
    world.activate(info["id"], timeline_id, now_real=now)
    result = world.set_rate(info["id"], timeline_id, rate=60, now_real=now)
    assert result["changed"] is True
    assert result["effective_real"] == 1_700_000_001, "严格晚于输入时刻的第一个自然整秒"
    repeat = world.set_rate(info["id"], timeline_id, rate=60, now_real=now)
    assert repeat.get("duplicate") is True, "重试同一请求不产生第二次变更"
    # 生效前仍是旧倍率
    assert world.view(info["id"], timeline_id, now_real=now)["rate"] == 1
    # 生效后按新倍率推进
    later = 1_700_000_001 + 10
    view = world.view(info["id"], timeline_id, now_real=later)
    assert view["rate"] == 60
    assert view["world_seconds"] == DAY * 1500 + 600


def test_rate_bounds_and_frozen_rejection(store, world) -> None:
    info, timeline_id, _ = make_instance(store, world)
    with pytest.raises(RuntimeStateError):
        world.set_rate(info["id"], timeline_id, rate=2, now_real=time.time())
    world.activate(info["id"], timeline_id, now_real=time.time())
    with pytest.raises(RuntimeStateError):
        world.set_rate(info["id"], timeline_id, rate=world.rate_max + 1, now_real=time.time())
    with pytest.raises(RuntimeStateError):
        world.set_rate(info["id"], timeline_id, rate=0, now_real=time.time())


def test_freeze_cancels_pending_and_settles(store, world) -> None:
    info, timeline_id, _ = make_instance(store, world)
    now = 1_700_000_000.0
    world.activate(info["id"], timeline_id, now_real=now)
    world.set_rate(info["id"], timeline_id, rate=3600, now_real=now)
    result = world.freeze(info["id"], timeline_id, now_real=now + 1)
    assert result["state"] == "frozen"
    assert result["cancelled_commands"] == 0, "已到期命令先结算"
    assert store.rate_pending(timeline_id) == []
    frozen_world = result["world_seconds"]
    again = world.view(info["id"], timeline_id, now_real=now + 100000)
    assert again["processed_world"] == frozen_world, "冻结后不再跳时"


def test_catchup_produces_life_experiences_once(store, world) -> None:
    info, timeline_id, character_id = make_instance(store, world)
    now = 1_700_000_000.0
    world.activate(info["id"], timeline_id, now_real=now)
    world.set_rate(info["id"], timeline_id, rate=100000, now_real=now)
    later = now + 2  # 世界时间前进约 2 日
    first = world.advance(info["id"], timeline_id, now_real=later + 0.5)
    assert first["batches"] >= 1
    assert first["experiences"] >= 1, "生活线窗口完成即产生经历"
    experiences = store.experience_window(info["id"], timeline_id, character_id, until=first["processed_world"])
    assert experiences
    # 重复推进不重复产生同一经历（幂等）
    second = world.advance(info["id"], timeline_id, now_real=later + 0.5)
    again = store.experience_window(info["id"], timeline_id, character_id, until=first["processed_world"])
    assert len(again) == len(experiences)
    assert second["batches"] >= 0


def test_advance_is_bounded_and_reports_catching_up(store, world) -> None:
    info, timeline_id, _ = make_instance(store, world)
    now = 1_700_000_000.0
    world.activate(info["id"], timeline_id, now_real=now)
    world.set_rate(info["id"], timeline_id, rate=1000000, now_real=now)
    result = world.advance(info["id"], timeline_id, now_real=now + 100, max_batches=2)
    assert result["state"] == "catching_up", "超出单次批数上限时报追赶中，不假装追平"
    assert result["batches"] == 2


def test_personality_units_drive_and_archive() -> None:
    card = sample_card(sample_package())
    rows = personality.initial_rows(
        card, instance_id="in-1", timeline_id="tl-1", world_seconds=0
    )
    anchor = next(item for item in rows if item["mode"] == "anchor")
    assert anchor["confidence"] >= 0.75

    # 对话驱动强化既有同义单元
    target = next(item for item in rows if item["mode"] == "dialog")
    stronger = personality.apply_drive(
        rows,
        mode="dialog",
        source_key="dmg-1",
        semantic=str(target["semantic"]),
        strength=1.0,
        positive=True,
        world_seconds=10,
    )
    after = next(item for item in stronger if item["id"] == target["id"])
    assert after["confidence"] > target["confidence"]
    # 同一来源键只消费一次
    repeat = personality.apply_drive(
        stronger,
        mode="dialog",
        source_key="dmg-1",
        semantic=str(target["semantic"]),
        strength=1.0,
        positive=True,
        world_seconds=20,
    )
    assert next(item for item in repeat if item["id"] == target["id"])["confidence"] == after["confidence"]

    # 负向驱动可把普通单元压到归档阈值以下
    crushed = rows
    for index in range(20):
        crushed = personality.apply_drive(
            crushed,
            mode="dialog",
            source_key=f"neg-{index}",
            semantic=str(target["semantic"]),
            strength=1.0,
            positive=False,
            world_seconds=30 + index,
        )
    archived = next(item for item in crushed if item["id"] == target["id"])
    assert archived["archived"] == 1, "非锚点低于阈值即归档"
    assert archived["confidence"] < 0.05
    assert not any(item["semantic"] == target["semantic"] for item in personality.visible(crushed))


def test_anchors_survive_weak_drives_and_pass_time() -> None:
    card = sample_card(sample_package())
    rows = personality.initial_rows(card, instance_id="in-1", timeline_id="tl-1", world_seconds=0)
    anchor = next(item for item in rows if item["mode"] == "anchor")
    for index in range(50):
        rows = personality.apply_drive(
            rows,
            mode="dialog",
            source_key=f"w-{index}",
            semantic=str(anchor["semantic"]),
            strength=1.0,
            positive=False,
            world_seconds=index,
        )
    decelerated = personality.apply_time(rows, from_world=0, to_world=DAY * 400, day_seconds=DAY)
    still = next(item for item in decelerated if item["id"] == anchor["id"])
    assert still["archived"] == 0, "锚点不归档"
    assert still["confidence"] >= personality.ANCHOR_FLOOR


def test_time_decay_is_equivalent_stepwise_and_bulk() -> None:
    card = sample_card(sample_package())
    rows = personality.initial_rows(card, instance_id="in-1", timeline_id="tl-1", world_seconds=0)
    bulk = personality.apply_time(rows, from_world=0, to_world=DAY, day_seconds=DAY)
    stepwise = personality.apply_time(
        personality.apply_time(rows, from_world=0, to_world=DAY // 2, day_seconds=DAY),
        from_world=DAY // 2,
        to_world=DAY,
        day_seconds=DAY,
    )
    for left, right in zip(bulk, stepwise):
        assert left["confidence"] == pytest.approx(right["confidence"], abs=1e-9)


def test_life_plan_expands_across_midnight() -> None:
    package = sample_package()
    card = sample_card(package)
    card["life_template"]["windows"] = [
        {"start": 79200, "end": 108000, "activity": "sleep"},
        {"start": 21600, "end": 72000, "activity": "duty"},
        {"start": 72000, "end": 79200, "activity": "rest"},
    ]
    calendar = calendar_from_package(package)
    plan = life.expand_plan(
        card, calendar, day_index=10, instance_id="in-1", timeline_id="tl-1", created_world=0
    )
    windows = json.loads(plan["windows"])["windows"]
    assert windows[0]["end"] > windows[0]["start"]
    assert max(item["end"] for item in windows) > (10 + 1) * DAY, "跨日窗口按世界秒展开"
    payload = json.loads(plan["windows"])
    assert payload["day_end"] - payload["day_start"] == DAY


def test_plan_is_not_experience(store, world) -> None:
    """计划不等于经历：只到当前水位为止完成的窗口才成为经历。"""
    info, timeline_id, character_id = make_instance(store, world)
    plan = store.plan_latest(info["id"], timeline_id, character_id)
    windows = json.loads(plan["windows"])["windows"]
    early = int(windows[0]["start"]) + 60
    experiences = store.experience_window(info["id"], timeline_id, character_id, until=early)
    assert experiences == [], "未来活动块不算已发生"
    current = life.current_window(plan, early)
    assert current is not None


# ---------- 认知 ----------


def test_cognition_slice_never_leaks_truth_or_creator_background() -> None:
    package = sample_package()
    card = sample_card(package)
    moment = int(package["calendar"]["initial_moment"])
    context = cognition.play_context(
        package,
        card,
        world_seconds=moment,
        calendar_label="灰潮纪13年雾月1日",
        units=personality.initial_rows(card, instance_id="in-1", timeline_id="tl-1", world_seconds=0),
    )
    text = cognition.render_prompt(context)
    creator = str(card["background"]["creator"])
    assert creator and creator not in text, "幕后设定不进扮演上下文"
    assert "实情" not in text
    # 卡片只从传本里读到 cf-1：以「史料」形式出现，而不是以实情口吻
    assert any(item["source"].startswith("史料") for item in context["knowledge"])
    assert all(item["source"] != "实情层" for item in context["knowledge"])


def test_cognition_filters_uncontacted_sources() -> None:
    package = sample_package()
    card = sample_card(package)
    # 把卡片的知识改到未接触的来源上：不应出现在切片里
    card["initial_knowledge"] = [
        {"ref_type": "narrative", "ref_id": "nv-2", "obtained_at": DAY * 1300},
        {"ref_type": "self", "claim": "她在盐滩长大。"},
    ]
    slice_ = cognition.knowledge_slice(package, card, world_seconds=DAY * 2000)
    texts = [item["text"] for item in slice_]
    assert "她在盐滩长大。" in texts
    assert all("碑刻" not in text for text in texts), "没接触的渠道不能说"


def test_hard_cognition_only_keeps_allowed_sources() -> None:
    package = sample_package()
    card = sample_card(package)
    card["cognition"] = {"mode": "hard", "sources": ["self_experience", "user_contact"]}
    slice_ = cognition.knowledge_slice(package, card, world_seconds=DAY * 2000)
    assert slice_, "自身经历与用户通讯仍可用"
    assert all(item["source"] in ("自己的记忆", "自己的经历") for item in slice_)


def test_catch_up_all_skips_frozen_lines(store, world) -> None:
    info, timeline_id, _ = make_instance(store, world)
    assert world.active_timelines() == []
    assert world.catch_up_all(now_real=1_700_000_000.0) == {}

    world.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world.set_rate(info["id"], timeline_id, rate=60, now_real=1_700_000_000.0)
    assert [pair[1] for pair in world.active_timelines()] == [timeline_id]
    advanced = world.catch_up_all(now_real=1_700_000_600.0)  # 10 世界分钟
    assert advanced[timeline_id]["processed_world"] >= 600

    world.freeze(info["id"], timeline_id, now_real=1_700_000_600.0)
    assert world.active_timelines() == []


def test_session_prompt_comes_from_runtime(store, world) -> None:
    """会话层拿到的扮演定义来自运行层（真实实例），不再用占位提示词。"""
    info, timeline_id, character_id = make_instance(store, world)
    session = store.session_ensure(info["id"], timeline_id, character_id)
    prompt = world.system_prompt(session)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    assert package["meta"]["description"] in prompt
    assert "灰潮纪" in prompt
    assert "堤禾" in prompt or "堤荇" in prompt
    creator = next(
        card["background"]["creator"]
        for card in json.loads(store.instance_get(info["id"])["setting"])["cards"]
    )
    assert creator not in prompt, "幕后设定不进会话提示"
