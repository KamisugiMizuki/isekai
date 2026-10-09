"""B-4 v2 的**内容供给**样例：三港纪（多区域 + 邻接 + 通行档 + 每跳传播延迟）。

为什么单开一个文件：主样例世界 `灰潮纪` 只有 `pl-1` 一个区域、无邻接 ⇒ B-4 v2 的拓扑在它上面**空转**。
本文件用 `example_topology_package()`（独立包，不动主样例）把拓扑**跑成可观察的行为**：

- 区域图：`pl-1 南港 —1— pl-2 河口 —2— pl-3 内陆`；`pl-3 —受阻— pl-4 远山`（远山不可达）。
- 每跳延迟 `events.hop_delay_seconds = 6 小时` ⇒ **消息每多隔一跳晚到 6 小时**。
- 三个来源各带 `region`（南港 / 河口 / 内陆），因此同一条事件的三个来源**到达时刻不同**。

这一组断言的价值：它是 B-4 v2 的**交叉验收**——前面 `test_space_topology.py` 验的是机制与边界，
这里验的是「机制在**一份真实内容**上确实产生可观察的差异」。
"""

from __future__ import annotations

from isekai_core.runtime import events, space
from isekai_core.world.example import DAY, example_topology_package
from isekai_core.world.validate import validate_package
from test_runtime import store, world  # noqa: F401  夹具在那边

HOP = DAY // 4  # 6 小时


def _package() -> dict:
    return example_topology_package()


def test_topology_package_passes_validation() -> None:
    """内容供给的第一条硬门槛：包本身必须合法（否则一切都无从谈起）。"""
    errors = validate_package(_package())
    assert errors == [], errors


def test_topology_package_declares_a_four_region_graph() -> None:
    package = _package()
    assert [item["id"] for item in package["world"]["regions"]] == ["pl-1", "pl-2", "pl-3", "pl-4"]
    assert package["events"]["hop_delay_seconds"] == HOP, "每跳延迟必须显式声明"


def test_reachability_matches_the_declared_topology() -> None:
    """可达性：南港→河口的代价 1、→内陆 3（1+2）；远山因唯一的路受阻而不可达。"""
    regions = _package()["world"]["regions"]
    from_south = space.reachable(regions, "pl-1")
    assert from_south["pl-2"] == 1
    assert from_south["pl-3"] == 3
    assert "pl-4" not in from_south, "受阻的边不得通过 ⇒ 远山不可达"
    # 双向：声明单向即可（路是通的）
    assert space.reachable(regions, "pl-3")["pl-1"] == 3


def _claims_for(event_region: str) -> dict[str, int]:
    """同一条事件（影响范围 = `event_region`）在各来源上的最早传播时刻。"""
    package = _package()
    event = {
        "id": "ev-topo", "summary": "河口封航", "template": "",
        "effects": [{"kind": "route_blocked", "target": event_region}],
    }
    rows = events.claim_rows(
        event, package=package, instance_id="in-1", timeline_id="tl-1",
        event_ident="ev-topo", world_seconds=1000, calendar=None,
        regions=package["world"]["regions"], hop_delay_seconds=HOP,
    )
    return {str(row["source_id"]): int(row["earliest_world"]) for row in rows}


def test_sources_hear_the_same_event_at_different_times_by_distance() -> None:
    """**本包的核心可观察效果**：同一条事件，离得越远的来源越晚拿到。

    事件发生在 `pl-2`（河口）：
    - `src-1` 驿站信报在河口 ⇒ 0 跳 = 立刻；
    - `src-2` 堤岸榜文在南港 ⇒ 1 跳 = +6 小时；
    - `src-3` 山民口信在内陆 ⇒ 2 跳 = +12 小时。
    """
    arrivals = _claims_for("pl-2")
    assert arrivals["src-1"] == 1000, "同区域来源必须立刻拿到"
    assert arrivals["src-2"] == 1000 + 1 * HOP
    assert arrivals["src-3"] == 1000 + 2 * HOP
    assert arrivals["src-1"] < arrivals["src-2"] < arrivals["src-3"], "距离必须体现为到达顺序"


def test_unreachable_region_does_not_invent_a_delay() -> None:
    """远山不可达 ⇒ 跳数取 0（**不臆造延迟**）。这条把「不可达」与「很远」区分开。"""
    arrivals = _claims_for("pl-4")
    assert arrivals["src-1"] == 1000, "事件在不可达区域时不得凭空增加传播延迟"


def test_event_scope_covers_near_regions_on_this_package() -> None:
    """影响范围：事件在 `pl-2` 时，南港（1 跳）的角色也算受影响；远山不算。"""
    package = _package()
    regions = package["world"]["regions"]
    event = {
        "id": "ev-scope", "instance_id": "in-1", "timeline_id": "tl-1", "summary": "河口封航",
        "effects": [{"kind": "route_blocked", "target": "pl-2"}],
    }
    near = {"meta": {"card_id": "cc-near"}, "region": "pl-1"}
    same = {"meta": {"card_id": "cc-same"}, "region": "pl-2"}
    far = {"meta": {"card_id": "cc-far"}, "region": "pl-4"}

    assert events.grants(event, [], same, world_seconds=2000, calendar=None, regions=regions), "同区域"
    assert events.grants(event, [], near, world_seconds=2000, calendar=None, regions=regions), "相邻区域"
    assert events.grants(event, [], far, world_seconds=2000, calendar=None, regions=regions) == [], "不可达"


def test_the_main_sample_world_still_has_no_topology() -> None:
    """**不能顺手改主样例世界**：它只有一个区域、无邻接，因此拓扑在它上面仍然是空转的。

    这条断言的作用是防止「为了看效果」去改主样例——那会让 700+ 条既有测试换靶子。
    """
    from samples import sample_package  # noqa: F401  仓库既有夹具

    regions = sample_package()["world"]["regions"]
    assert len(regions) == 1 and not regions[0].get("adjacent")


def test_topology_package_can_actually_be_instantiated_and_advanced(store, world) -> None:  # noqa: F811
    """**最强的一条**：这份内容不只是「能过校验」，还要能真的建实例并推进。

    包一旦能被创建并推进，说明邻接声明、来源 `region`、每跳延迟三者与运行层的其余约束**同时**成立
    （否则会在创建期或推进期抛错）。这是内容供给从「纸面合法」到「真的能跑」的分界。
    """
    from isekai_core.world.example import example_card
    from isekai_core.world.instances import create_instance

    package = _package()
    card = example_card(package)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world.ensure_instance(info["id"], now_real=1.7e9)
    world.activate(info["id"], timeline_id, now_real=1.7e9)

    result = world.advance(info["id"], timeline_id, now_real=1.7e9 + 3 * DAY, max_batches=3)
    assert int(result.get("batches") or 0) >= 1, f"必须真的推进了批次（实际 {result}）"

    # 推进后事件与说法都应落在库里；说法带来源 ⇒ 拓扑折算真的参与了这条路径
    assert store.has_events(info["id"], timeline_id), "推进后必须留下事件"
    claims = store.claim_list(info["id"], timeline_id)
    assert claims, "推进后必须留下说法"
