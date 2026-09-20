"""事件引擎（阶段 3）：确定性抽样、预算、效果闭集与失效、获知链、回填、经历引用。"""

from __future__ import annotations

import json
import time

import pytest

from isekai_core.runtime import events, life
from isekai_core.runtime.calendar import calendar_from_package
from isekai_core.runtime.service import RuntimeService
from isekai_core.world.validate import validate_package
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边

DENSITY_MAX = {"稀疏": 1, "常规": 3, "丰盛": 5}


def _seed(store, instance_id: str) -> str:
    return str(store.instance_get(instance_id)["seed"])


# ---------- 确定性（附录 A / 附录 B #1） ----------


def test_sampling_is_deterministic_and_seed_dependent() -> None:
    package = sample_package()
    calendar = calendar_from_package(package)
    first = events.plan_day(
        package, seed="seed-a", rules_version="0.1", day_index=1500, calendar=calendar, events=set(), effects=set()
    )
    again = events.plan_day(
        package, seed="seed-a", rules_version="0.1", day_index=1500, calendar=calendar, events=set(), effects=set()
    )
    assert [item["slot"] for item in first] == [item["slot"] for item in again], "同一输入必得同一候选"
    assert events.daily_budget("seed-a", "0.1", 1500, "常规") == events.daily_budget("seed-a", "0.1", 1500, "常规")
    other = events.daily_budget("seed-b", "0.1", 1500, "常规")
    assert 1 <= events.daily_budget("seed-a", "0.1", 1500, "常规") <= 3
    assert 1 <= other <= 3
    # 事件标识只由种子 / 规则 / 历法日 / 槽决定
    assert events.event_id("seed-a", "0.1", 1500, "s0") == events.event_id("seed-a", "0.1", 1500, "s0")
    assert events.event_id("seed-a", "0.1", 1500, "s0") != events.event_id("seed-b", "0.1", 1500, "s0")


def test_no_python_hash_dependency() -> None:
    """固定哈希：不同进程（PYTHONHASHSEED 不同）也必须同键。"""
    import os
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, 'isekai_core'); "
        "from isekai_core.runtime.events import stable_key; print(stable_key('seed','0.1',1500,'slot',0))"
    )
    env_a = {**os.environ, "PYTHONHASHSEED": "1"}
    env_b = {**os.environ, "PYTHONHASHSEED": "2"}
    out_a = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env_a).stdout.strip()
    out_b = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env_b).stdout.strip()
    assert out_a and out_a == out_b


# ---------- 预算与固定事件（§四 / 附录 B #3） ----------


def test_daily_events_stay_within_budget(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    density = str(package["events"]["density"])
    world_service.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world_service.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 20 * DAY)
    rows = store.event_window(info["id"], timeline_id, until=10**12, limit=500)
    engine_rows = [item for item in rows if item["source"] == "engine"]
    assert engine_rows, "推进多日应有世界级事件"
    by_day: dict[int, int] = {}
    for item in engine_rows:
        by_day[int(item["world_seconds"]) // DAY] = by_day.get(int(item["world_seconds"]) // DAY, 0) + 1
    assert max(by_day.values()) <= DENSITY_MAX[density], f"不得越过每日硬预算：{by_day}"


def test_fixed_festival_does_not_drift(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    calendar = calendar_from_package(package)
    festival = next(item for item in events.fixed_events(package, day_index=32, calendar=calendar))
    assert festival["summary"] == "开滩祭"
    assert events.fixed_events(package, day_index=31, calendar=calendar) == []
    # 固定事件先占名额：预算为 1 时只出固定事件
    planned = events.plan_day(
        package,
        seed=_seed(store, info["id"]),
        rules_version="0.1",
        day_index=32,
        calendar=calendar,
        events=set(),
        effects=set(),
    )
    if events.daily_budget(_seed(store, info["id"]), "0.1", 32, "常规") >= 1:
        assert planned[0]["slot"].startswith("fixed-"), "固定节庆优先于随机事件"


def test_precondition_without_record_means_no_event() -> None:
    package = sample_package()
    calendar = calendar_from_package(package)
    template = package["events"]["families"][0]["templates"][0]
    template["preconditions"] = ["ev-does-not-exist"]
    planned = events.plan_day(
        package, seed="s", rules_version="0.1", day_index=1500, calendar=calendar, events=set(), effects=set()
    )
    assert [item for item in planned if not item["fixed"]] == [], "前置条件不足即不发生，且不重抽到成功"
    later = events.plan_day(
        package,
        seed="s",
        rules_version="0.1",
        day_index=1500,
        calendar=calendar,
        events={"ev-does-not-exist"},
        effects=set(),
    )
    assert any(not item["fixed"] for item in later), "条件满足后可以发生"


# ---------- 效果闭集与失效方式（§二 / §六 / 附录 B #12） ----------


def test_unsupported_effect_kind_is_rejected_at_creation() -> None:
    package = sample_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "health_pool", "target": "src-1", "expiry": "with_cause"}
    ]
    errors = validate_package(package)
    assert any("未支持的效果类型" in item for item in errors)


def test_effect_expiry_kinds_are_required() -> None:
    package = sample_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [{"kind": "source_delay", "target": "src-1"}]
    assert any("expiry" in item for item in validate_package(package))
    package = sample_package()
    package["events"]["families"][0]["templates"][0]["effects"] = [
        {"kind": "source_delay", "target": "src-1", "expiry": "natural_recovery"}
    ]
    assert any("恢复" in item for item in validate_package(package))


def test_with_cause_effect_clears_and_until_cleared_persists(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world_service.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 3 * DAY)
    assert store.effect_window(info["id"], timeline_id, until=10**12), "推进后仍有有效的后果"
    raw = [
        dict(row)
        for row in store._conn.execute(
            "SELECT * FROM effect_state WHERE timeline_id=? ORDER BY from_world, id", (timeline_id,)
        ).fetchall()
    ]
    assert raw, "推进后应有效果状态"
    kinds = {str(item["expiry"]) for item in raw}
    assert kinds <= {"with_cause", "until_cleared", "natural_recovery"}
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    past_with_cause = [
        item
        for item in raw
        if str(item["expiry"]) == "with_cause" and int(item["from_world"]) + DAY <= watermark
    ]
    assert past_with_cause, "存在跨日的 with_cause 后果"
    assert all(int(item["active"]) == 0 and item["cleared_at"] is not None for item in past_with_cause), (
        "随诱因结束即失效，且解除要留下时刻"
    )
    same_day = [item for item in raw if int(item["from_world"]) + DAY > watermark]
    assert any(int(item["active"]) == 1 for item in same_day), "同日的后果仍然有效（还没到解除条件）"
    # 有条件的自然恢复：同族后续事件出现即解除；没有依据就保持有效（§3.1.5）
    natural = [item for item in raw if str(item["expiry"]) == "natural_recovery"]
    assert natural
    latest_by_family: dict[str, int] = {}
    for row in store.event_window(info["id"], timeline_id, until=10**12, limit=500):
        family = str(row["family"] or "")
        if family:
            latest_by_family[family] = max(latest_by_family.get(family, 0), int(row["world_seconds"]))
    moments_by_family: dict[str, list[int]] = {}
    for row in store.event_window(info["id"], timeline_id, until=10**12, limit=500):
        family = str(row["family"] or "")
        if family:
            moments_by_family.setdefault(family, []).append(int(row["world_seconds"]))
    # 判定发生在批的开始：本批新写的事件不参与本批判定，所以解除最快在「后一批」生效
    seen_before_last_batch = max(0, watermark - DAY)
    for item in natural:
        started = int(item["from_world"])
        family = str(item["family"] or "")
        later = [
            moment
            for moment in moments_by_family.get(family, [])
            if started < moment <= seen_before_last_batch
        ]
        if later:
            assert int(item["active"]) == 0, "同族后续事件已被观察到 → 自然恢复条件成立"
        else:
            assert int(item["active"]) == 1, "没有已被观察到的后续依据 → 保留后果（§3.1.5）"


def test_effects_appear_in_current_situation_without_rewriting_plan(store) -> None:
    """仍有效的后果进入当前处境描述，但不改写已固化的计划（§六）。"""
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world_service.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 2 * DAY)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    plan_before = store.plan_get(info["id"], timeline_id, character_id, 1501)
    window_before = json.dumps(plan_before["windows"], ensure_ascii=False) if plan_before else ""

    card = sample_card(sample_package())
    card["role_id"] = "rl-1"
    effect = {
        "id": "fx-test-1",
        "instance_id": info["id"],
        "timeline_id": timeline_id,
        "event_id": "ev-test-1",
        "target": "rl-1",
        "kind": "route_blocked",
        "family": "ef-1",
        "from_world": watermark,
        "expiry": "until_cleared",
        "recovery": "",
        "active": 1,
        "cleared_at": None,
    }
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=watermark,
        catching_up=False,
        effects=[effect],
    )
    snapshot = world_service.character_snapshot(info["id"], timeline_id, character_id, world_seconds=watermark)
    assert snapshot["effects"], "仍有效的后果进入角色状态"
    assert "route_blocked" in str(snapshot["current_activity"]), "当前处境体现后果"
    plan_after = store.plan_get(info["id"], timeline_id, character_id, 1501)
    assert (json.dumps(plan_after["windows"], ensure_ascii=False) if plan_after else "") == window_before, (
        "计划本身不被改写"
    )


# ---------- 获知链（§五 / 附录 B #5） ----------


def test_claims_grant_knowledge_only_through_channels(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    calendar = calendar_from_package(package)
    card = sample_card(package)  # 渠道只有 src-1
    event = {
        "id": "ev-x",
        "instance_id": info["id"],
        "timeline_id": timeline_id,
        "summary": "驿站停摆一日",
        "effects": [],
    }
    claims = events.claim_rows(
        event,
        package=package,
        instance_id=info["id"],
        timeline_id=timeline_id,
        event_ident="ev-x",
        world_seconds=DAY * 1500,
        calendar=calendar,
    )
    granted = events.grants(event, claims, card, world_seconds=DAY * 1500, calendar=calendar)
    sources = {item["source"] for item in granted}
    assert "src-1" in sources, "渠道匹配的获知成立"
    assert "src-2" not in sources, "没有该渠道就不能仅凭公开标签获知"
    # 尚未传播：把最早传播时刻推到未来
    for claim in claims:
        claim["earliest_world"] = DAY * 1600
    assert [item for item in events.grants(event, claims, card, world_seconds=DAY * 1500, calendar=calendar)
            if item["kind"] == "claim"] == [], "还没传播到就不算已经听到"


def test_observation_only_for_involved_characters(store) -> None:
    package = sample_package()
    calendar = calendar_from_package(package)
    card = sample_card(package)
    card["role_id"] = "rl-1"
    event = {
        "id": "ev-y",
        "instance_id": "in-x",
        "timeline_id": "tl-x",
        "summary": "堤岸巡查中止",
        "effects": [{"kind": "activity_constraint", "target": "rl-1", "expiry": "with_cause"}],
    }
    granted = events.grants(event, [], card, world_seconds=DAY, calendar=calendar)
    assert [item["kind"] for item in granted] == ["observation"], "参与者亲历形成观察"
    outsider = sample_card(package)
    outsider["role_id"] = "rl-other"
    assert events.grants(event, [], outsider, world_seconds=DAY, calendar=calendar) == []


def test_knowledge_reaches_the_play_definition(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=watermark,
        catching_up=False,
        knowledge=[
            {
                "id": "kn-x",
                "instance_id": info["id"],
                "timeline_id": timeline_id,
                "character_id": character_id,
                "world_seconds": watermark,
                "kind": "claim",
                "target": "cl-x",
                "source": "src-1",
                "stance": "recorded",
                "text": "信报上说：北堤一带的退潮推迟了两日",
            }
        ],
    )
    prompt = world_service.system_prompt(
        {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id}
    )
    assert "北堤一带的退潮推迟了两日" in prompt, "已获知的说法进入扮演定义"


# ---------- 历史回填（§3.3 / 附录 B #10） ----------


def test_backfill_writes_history_without_effects_or_knowledge(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    rows = store.event_window(info["id"], timeline_id, until=10**12, limit=100)
    backfilled = [item for item in rows if item["source"] == "backfill"]
    assert backfilled, "创建期把包内既定内容落成历史条目"
    assert store.effect_window(info["id"], timeline_id, until=10**12) == [], "回填不施加效果"
    assert store.claim_list(info["id"], timeline_id), "回填的说法可供后续认知使用"
    assert world_service.backfill(info["id"], timeline_id) == 0, "重复回填不产生新条目"


# ---------- 重放幂等（附录 B #6） ----------


def test_advance_is_idempotent_over_the_same_window(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    now = 1_700_000_000.0 + 5 * DAY
    world_service.advance(info["id"], timeline_id, now_real=now)
    snapshot = (
        len(store.event_window(info["id"], timeline_id, until=10**12, limit=500)),
        len(store.knowledge_window(info["id"], timeline_id, character_id, until=10**12, limit=500)),
        len(store.experience_window(info["id"], timeline_id, character_id, until=10**12, limit=500)),
    )
    world_service.advance(info["id"], timeline_id, now_real=now)
    assert (
        len(store.event_window(info["id"], timeline_id, until=10**12, limit=500)),
        len(store.knowledge_window(info["id"], timeline_id, character_id, until=10**12, limit=500)),
        len(store.experience_window(info["id"], timeline_id, character_id, until=10**12, limit=500)),
    ) == snapshot, "同一区间重放不重复产事件 / 获知 / 经历"


def test_export_import_carries_event_log(store) -> None:
    from isekai_core.world.portable import build_container, import_instance

    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1_700_000_000.0)
    world_service.advance(info["id"], timeline_id, now_real=1_700_000_000.0 + 4 * DAY)
    before = len(store.event_window(info["id"], timeline_id, until=10**12, limit=500))
    container = build_container(store, info["id"])
    state = next(iter(container["runtime"]["state"].values()))
    assert state["events"] and state["claims"], "事件与说法随容器导出"
    imported = import_instance(store, container, display_name="事件副本")
    new_line = store.timeline_list(imported["id"])[0]["id"]
    assert len(store.event_window(imported["id"], new_line, until=10**12, limit=500)) == before
    assert store.knowledge_window(imported["id"], new_line, character_id, until=10**12, limit=500), "获知也随件"


def test_share_material_requires_knowledge_and_value() -> None:
    assert events.share_qualified({"share_value": 1, "importance": 0.1})
    assert events.share_qualified({"share_value": 0, "importance": 0.6})
    assert not events.share_qualified({"share_value": 0, "importance": 0.2})


def test_same_moment_events_have_stable_order(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, _ = make_instance(store, world_service)
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    calendar = calendar_from_package(package)
    seed = _seed(store, info["id"])
    moments = [
        events.event_moment(seed, "0.1", 1500, slot, calendar.day_seconds) for slot in ("s0", "s1", "s2", "s3")
    ]
    assert moments == [events.event_moment(seed, "0.1", 1500, slot, calendar.day_seconds)
                       for slot in ("s0", "s1", "s2", "s3")]
    assert all(1500 * DAY <= item < 1501 * DAY for item in moments), "事件时刻落在当日"
