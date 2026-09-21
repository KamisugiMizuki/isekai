"""创建期历史回填的编纂计划与联合校验（WORLD_SETTING_SPEC §3.6 条 2/3/4）。

判据：分时代（十年一卷）× 传本组织，10–20 条一批；传本选载范围可读；产物不合法就不固化。
"""

from __future__ import annotations

import json

import pytest

from isekai_core.config import load_config
from isekai_core.runtime import events
from isekai_core.runtime.calendar import calendar_from_package
from isekai_core.runtime.service import RuntimeService, RuntimeStateError
from isekai_core.world import ops as world_ops
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


def _plan(package, seed="seed-a", rules="0.1"):
    return events.backfill_plan(
        package, seed=seed, rules_version=rules, calendar=calendar_from_package(package)
    )


def test_plan_groups_by_volume_and_source() -> None:
    package = sample_package()
    plan = _plan(package)
    fixed = package["initial_state"]
    expected = len(fixed.get("events") or []) + len(fixed.get("rumors") or [])
    assert plan["total"] == expected and plan["total"] > 0
    assert plan["volume_years"] == events.BACKFILL_VOLUME_YEARS == 10
    for volume in plan["volumes"]:
        assert volume["to_world"] - volume["from_world"] == 10 * calendar_from_package(package).year_seconds
    # 传闻按来源归卷：src-1 的条目只出现在 src-1 的卷里
    selection = plan["selection"]
    assert set(selection) <= {"", "src-1", "src-2"}
    by_source = {sid: set(idents) for sid, idents in selection.items()}
    for batch in plan["batches"]:
        assert set(batch["entries"]) <= by_source[batch["source_id"]], "批里的条目属于该批的传本"
    assert all(set(idents) for idents in selection.values())


def test_batches_stay_within_bounds() -> None:
    assert events.backfill_batch_sizes(7) == [7], "不足一批就不凑量"
    assert events.backfill_batch_sizes(20) == [20]
    assert events.backfill_batch_sizes(23) == [12, 11], "拆成均衡的两批，都落在 10–20"
    assert events.backfill_batch_sizes(45) == [15, 15, 15]
    for count in (21, 30, 41, 60):
        sizes = events.backfill_batch_sizes(count)
        assert sum(sizes) == count
        assert all(size <= events.BACKFILL_BATCH_MAX for size in sizes)
        assert all(size >= events.BACKFILL_BATCH_MIN for size in sizes), sizes


def test_plan_is_deterministic_per_seed() -> None:
    package = sample_package()
    first, again = _plan(package), _plan(package)
    assert first == again, "同一创建种子 → 同一计划"
    other = _plan(package, seed="seed-b")
    assert other["total"] == first["total"], "换种子只改编纂顺序，不改材料总量"
    assert sorted(i for b in other["batches"] for i in b["entries"]) == sorted(
        i for b in first["batches"] for i in b["entries"]
    ), "换种子只改编纂顺序，不改材料集合"


def test_joint_validation_catches_missing_one_liner() -> None:
    package = sample_package()
    rumor_id = str(package["initial_state"]["rumors"][0])
    for item in package["narratives"]:
        if str(item.get("id")) == rumor_id:
            item.pop("text", None)
            item.pop("statement", None)
    rows, claims = events.backfill_rows(
        package, instance_id="in-x", timeline_id="tl-x", seed="seed-a", rules_version="0.1"
    )
    errors = events.backfill_product_errors(package, rows, claims)
    assert any("缺少一句话文本" in item for item in errors), errors


def test_joint_validation_catches_undeclared_source() -> None:
    package = sample_package()
    for item in package["narratives"]:
        item["source_id"] = "src-ghost"
    rows, claims = events.backfill_rows(
        package, instance_id="in-x", timeline_id="tl-x", seed="seed-a", rules_version="0.1"
    )
    errors = events.backfill_product_errors(package, rows, claims)
    assert any("未声明的传本" in item for item in errors), errors


def test_creation_keeps_valid_backfill(store, world) -> None:
    """正常的包照旧过：回填 + 联合校验一起跑通（不是把校验做成拦路虎）。"""
    info, timeline_id, character_id = make_instance(store, world)
    world.backfill(info["id"], timeline_id)  # 不抛异常 = 产物通过了创建期联合校验
    assert store.event_ids(info["id"], timeline_id), "回填后历史条目在库里"


def test_overlong_era_halves_sampling_instead_of_scaling() -> None:
    """超长时代：卷数超阈值即折半抽样，未列卷 = 跨时代留白（跨度不能线性放大）。"""
    assert events.backfill_sample_volumes(24)[1] == 1, "阈值内全列"
    kept, step = events.backfill_sample_volumes(25)
    assert step == 2 and kept == list(range(0, 25, 2))
    kept, step = events.backfill_sample_volumes(200)
    assert len(kept) <= events.BACKFILL_MAX_VOLUMES and kept == list(range(0, 200, step))

    package = sample_package()
    year = calendar_from_package(package).year_seconds
    package["canon"][0]["at"], package["canon"][1]["at"] = 300 * year, 10 * year
    package["initial_state"]["events"] = [str(package["canon"][0]["id"]), str(package["canon"][1]["id"])]
    plan = _plan(package)
    assert plan["sampling"]["volumes"] == 31 and plan["sampling"]["step"] == 2
    assert plan["sampling"]["blanked"] >= 1 and plan["listed"] < plan["total"], "被抽掉的卷算留白，不凑跨度"
    assert all(volume["volume"] % 2 == 0 for volume in plan["volumes"]), plan["volumes"]
    assert all(volume["volume"] % 2 == 0 for volume in plan["batches"]), plan["batches"]


def test_key_figures_are_picked_by_seed_and_capped() -> None:
    """要点人物挑选：只挑人物、按种子定序、封顶（同种子同人选，换种子换人选）。"""
    package = sample_package()
    package["entities"] = [
        {"id": f"en-{index:02d}", "kind": "person" if index % 3 == 0 else "org", "name": f"某人{index}"}
        for index in range(1, 41)
    ]
    picked = events.backfill_key_figures(package, seed="seed-a", rules_version="0.1")
    assert len(picked) == events.BACKFILL_KEY_FIGURE_LIMIT == 12
    assert picked == events.backfill_key_figures(package, seed="seed-a", rules_version="0.1")
    assert picked != events.backfill_key_figures(package, seed="seed-b", rules_version="0.1")
    persons = {str(item["id"]) for item in package["entities"] if item["kind"] == "person"}
    assert set(picked) <= persons, "只挑人物：组织 / 地点没有寿命推演"

    plan = _plan(package)
    assert plan["key_figures"] == picked
    assert plan["roster_budget"]["limit"] == events.BACKFILL_ENTITY_LIMIT
    assert plan["roster_budget"]["registered"] == len(persons)
    assert plan["roster_budget"]["room"] == max(0, events.BACKFILL_ENTITY_LIMIT - len(persons))


def test_backfill_fixes_death_only_for_key_figures(store, world) -> None:
    """回填期一并确定生死的只到要点人物：没被挑中的登记人物保留在册、不补造生死；固死不受上限影响。"""
    package = sample_package(moment=DAY * 1500)
    year = calendar_from_package(package).year_seconds
    race_id = str(package["races"][0]["id"])
    born = -10 * year
    # 名册自带寿命覆盖（个体覆盖优先于种族带）：纪元前十年的起点 + 两年寿限 → 早于初始时刻
    band = {"lifespan": {"min_years": 1, "max_years": 2}}
    package["entities"] = [*package["entities"], *[
        {"id": f"en-p{index:02d}", "kind": "person", "name": f"某甲{index}", "race_id": race_id, "born": born, "died": None, **band}
        for index in range(20)
    ], *[
        {"id": f"en-d{index:02d}", "kind": "person", "name": f"某乙{index}", "race_id": race_id, "born": born, "died": DAY * 5, **band}
        for index in range(3)
    ]]
    info = create_instance(store, package, [sample_card(package)])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world.backfill(info["id"], timeline_id)
    deaths = [
        ident
        for ident in store.event_ids(info["id"], timeline_id)
        if str((store.event_get(info["id"], timeline_id, ident) or {}).get("template") or "").startswith("death:")
    ]
    assert len(deaths) >= 3, "固死者在回填期照旧登记寿终，不受要点名单限制"
    assert len(deaths) <= 3 + events.BACKFILL_KEY_FIGURE_LIMIT, "只有要点人物才推算生死，名册不能整体铺开"
    assert len(deaths) > 3, "要点人物里没写固死的，由寿命带推出寿终"


def test_backfill_plan_op_reads_from_instance(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, character_id = make_instance(store, world)
    out = world_ops.dispatch(cfg, store, "world.backfill.plan", {"instance_id": info["id"]})
    plan = out["plan"]
    package = json.loads(store.instance_get(info["id"])["setting"])["world_package"]
    assert plan["total"] == len(package["initial_state"].get("events") or []) + len(
        package["initial_state"].get("rumors") or []
    )
    assert plan["selection"], "选载范围要读得到"
