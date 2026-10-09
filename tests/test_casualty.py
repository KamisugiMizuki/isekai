"""B-5 v1：身体后果（`casualty`）进闭集——声明、档位校验、消费、优先级。

**死亡档位在 v1 明确保留**：它要「死亡事件与 `character_state.archived` 同批原子」，属独立验收项。
本组测试同时锁住「不做数值系统」这条原则约束。
"""

from __future__ import annotations

from isekai_core.world.validate import CASUALTY_GRADES, SUPPORTED_EFFECTS, validate_package
from samples import DAY, sample_package  # noqa: F401  仓库既有夹具
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _package_with_effect(effect: dict) -> dict:
    package = sample_package()
    template = package["events"]["families"][0]["templates"][0]
    template["effects"] = [effect]
    return package


def _character_target(package: dict) -> str:
    """取一个已登记的对象 id 作为效果目标（校验要求 target 已登记）。"""
    for entity in package.get("entities") or []:
        ident = str(entity.get("id") or "")
        if ident:
            return ident
    raise AssertionError("样例包没有可用目标")


def test_casualty_is_in_the_closed_set() -> None:
    """声明：`casualty` 必须在受支持闭集里，否则包无法声明身体后果（规格本来就允许受伤）。"""
    assert "casualty" in SUPPORTED_EFFECTS
    assert CASUALTY_GRADES == ("轻伤", "重伤", "失能", "死亡"), "档位是枚举（含死亡，其消费路径已落地）"


def test_valid_grade_passes_validation() -> None:
    package = sample_package()
    errors = validate_package(_package_with_effect(
        {"kind": "casualty", "target": _character_target(package), "value": "重伤", "expiry": "natural_recovery"}
    ))
    assert not [item for item in errors if "casualty" in item or ".value" in item], errors


def test_unknown_grade_is_rejected() -> None:
    """反例：档位不在枚举内必须创建期拒绝——否则「程度档」会变成自由数值入口。"""
    package = sample_package()
    errors = validate_package(_package_with_effect(
        {"kind": "casualty", "target": _character_target(package), "value": "HP-3", "expiry": "until_cleared"}
    ))
    assert any("档位必须是" in item for item in errors), errors


def test_death_grade_is_accepted_now_that_it_is_consumed() -> None:
    """`死亡` 档位已可用：它的消费路径（死亡事件 + 归档同批）已落地。

    与上一条「非枚举被拒」的区别在于**有没有消费方**：有消费方才允许声明。
    这正是 A-9b 的教训——不允许「包能声明、校验通过、没人消费」的空效果。
    """
    package = sample_package()
    errors = validate_package(_package_with_effect(
        {"kind": "casualty", "target": _character_target(package), "value": "死亡", "expiry": "with_cause"}
    ))
    assert not [item for item in errors if "档位必须是" in item], errors


def test_lethal_casualty_kills_and_archives_in_the_same_batch(store, world) -> None:  # noqa: ANN001, F811
    """集成：仍有效的 `casualty`+`死亡` ⇒ 本批同时产生**死亡事件**与**归档态**。"""
    from test_memory import _ready, _service  # noqa: F401  复用既有夹具

    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    clock = store.clock_get(timeline_id)
    watermark = int(clock["processed_world"])
    effect_id = "ef-lethal-1"
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(clock["generation"]),
        processed_world=watermark,
        catching_up=False,
        effects=[{
            "instance_id": info["id"], "timeline_id": timeline_id, "id": effect_id,
            "event_id": "ev-lethal-1", "target": character_id, "kind": "casualty",
            "family": "lethal", "value": "死亡", "from_world": watermark,
            "expiry": "with_cause", "recovery": "", "active": 1, "cleared_at": None,
        }],
    )
    assert character_id not in world_service._archived_ids(
        info["id"], timeline_id, until=watermark
    ), "前置：施加致死后果前角色未归档"

    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY)

    archived = world_service._archived_ids(
        info["id"], timeline_id, until=int(store.clock_get(timeline_id)["processed_world"])
    )
    assert character_id in archived, "致死后果当批必须置位归档态"
    produced = [
        row for row in store.event_window(info["id"], timeline_id, until=10**15, limit=200)
        if int(row.get("world_seconds") or 0) >= watermark
    ]
    assert produced, "致死后果当批必须产生死亡事件（有死讯才有说法可派生）"


def test_effect_note_carries_the_grade() -> None:
    """消费：生活线约束说明必须读出**档位**，否则轻伤与失能没有区别。"""
    from isekai_core.runtime import life

    effects = [
        {"target": "cc-x", "kind": "casualty", "value": "失能"},
        {"target": "cc-x", "kind": "route_blocked", "value": None},
        {"target": "cc-other", "kind": "casualty", "value": "轻伤"},
    ]
    note = life.effect_note(effects, "cc-x", "", "")
    assert "casualty:失能" in note, note
    assert "route_blocked" in note
    assert "轻伤" not in note, "别人的后果不得出现在这位角色的约束说明里"


def test_casualty_has_the_highest_priority() -> None:
    """身体后果压过通行 / 制度 / 环境：它决定角色能否行动。"""
    from isekai_core.runtime.events import EFFECT_PRIORITY

    assert EFFECT_PRIORITY["casualty"] > max(
        value for key, value in EFFECT_PRIORITY.items() if key != "casualty"
    )


def test_casualty_grade_picks_the_most_severe() -> None:
    """同一角色身上多个档位时取最重的一档（轻伤不该盖过失能）。"""
    from isekai_core.runtime import life

    effects = [
        {"kind": "casualty", "target": "cc-x", "value": "轻伤"},
        {"kind": "casualty", "target": "cc-x", "value": "失能"},
        {"kind": "casualty", "target": "cc-other", "value": "死亡"},
    ]
    assert life.casualty_grade(effects, "cc-x") == "失能"
    assert life.casualty_grade(effects, "cc-other") == "死亡"
    assert life.casualty_grade(effects, "cc-none") == ""


def test_incapacitated_plan_collapses_to_rest() -> None:
    """`失能` 必须**改写计划**：整日窗口合并为静养（这是「消费」的实质，不只是说明）。"""
    import json as _json

    from isekai_core.runtime import life

    plan = {"windows": _json.dumps({"windows": [
        {"start": 0, "end": 3600, "activity": "巡岸", "alternatives": ["补网"], "note": ""},
        {"start": 3600, "end": 7200, "activity": "赶集", "alternatives": ["访友"], "note": ""},
    ]}, ensure_ascii=False)}
    out = life.apply_casualty(plan, "失能")
    body = _json.loads(out["windows"])
    assert len(body["windows"]) == 1
    assert body["windows"][0]["activity"] == "静养"
    assert body["windows"][0]["alternatives"] == []
    assert (body["windows"][0]["start"], body["windows"][0]["end"]) == (0, 7200)


def test_minor_injury_does_not_change_the_plan() -> None:
    """`轻伤` 不改窗口（只进约束说明）——档位纪律要有下限，不能一受伤就躺平。"""
    import json as _json

    from isekai_core.runtime import life

    plan = {"windows": _json.dumps({"windows": [
        {"start": 0, "end": 3600, "activity": "巡岸", "alternatives": ["补网"], "note": ""},
    ]}, ensure_ascii=False)}
    assert life.apply_casualty(plan, "轻伤") == plan


def test_serious_injury_keeps_windows_but_drops_alternatives() -> None:
    """`重伤` 保留原活动，但清空备选并标注——受了重伤不该还留着活动弹性。"""
    import json as _json

    from isekai_core.runtime import life

    plan = {"windows": _json.dumps({"windows": [
        {"start": 0, "end": 3600, "activity": "巡岸", "alternatives": ["补网"], "note": "例行"},
    ]}, ensure_ascii=False)}
    out = life.apply_casualty(plan, "重伤")
    body = _json.loads(out["windows"])
    assert body["windows"][0]["activity"] == "巡岸"
    assert body["windows"][0]["alternatives"] == []
    assert "重伤" in body["windows"][0]["note"]
