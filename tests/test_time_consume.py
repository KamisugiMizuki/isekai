"""场景内时间消耗（TRPG_CAMPAIGN_RUNTIME_SPEC §十四）：世界时间往前跳 = 动锚点。

行为断言：锚点前移 + 这段时间按正常批次结算 + 留下可回滚的提交点 +
没有原因的跳跃直接被拒 + 回滚能把世界时间拉回消耗之前。
"""

from __future__ import annotations

import time

import pytest

from isekai_core.runtime.service import RuntimeService, RuntimeStateError
from isekai_core.store import Store
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package

T = 1_700_000_000.0  # 固定现实时基
START = DAY * 1500  # 初始世界时刻


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "data" / "isekai.db")
    handle.ensure_schema()
    yield handle
    handle.close()


@pytest.fixture
def world(store):
    return RuntimeService(store)


def make_instance(store, world):
    package = sample_package(moment=START)
    card = sample_card(package)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world.ensure_instance(info["id"], now_real=time.time())
    world.activate(info["id"], timeline_id, now_real=T)
    return info, timeline_id


def test_consume_moves_anchor_then_settles(store, world) -> None:
    """消耗 N 秒：锚点前移 N，水位跟着结算到那里，因果记进提交说明。"""
    info, timeline_id = make_instance(store, world)
    world.advance(info["id"], timeline_id, now_real=T + 1)
    before = store.clock_get(timeline_id)
    assert before["processed_world"] == START + 1

    out = world.consume_time(
        info["id"], timeline_id, seconds=3600, cause="潜入夜行", source="player_action", now_real=T + 1
    )

    assert out["consumed_seconds"] == 3600
    assert out["source"] == "player_action", "来源分类要带出来（世界过程 / 玩家行动 / GM 裁定）"
    assert out["state"] == "current", "跳过去的时间要当场结算，不留半截"
    assert out["processed_world"] == before["processed_world"] + 3600
    assert out["batches"] >= 1
    row = store.clock_get(timeline_id)
    assert row["base_world"] == before["base_world"] + 3600, "改的是锚点，不是水位"
    assert row["processed_world"] == before["processed_world"] + 3600, "水位被常规批次带上去"
    mark = store.commit_get(out["commit_id"])
    assert mark["kind"] == "time_consume" and "潜入夜行" in mark["note"], "时间跳跃要有因"


def test_consume_needs_cause_and_positive_seconds(store, world) -> None:
    """没有原因 / 非正秒数的时间跳跃不成立——以后没人答得上这段时间为什么过去了。"""
    info, timeline_id = make_instance(store, world)
    with pytest.raises(RuntimeStateError):
        world.consume_time(info["id"], timeline_id, seconds=0, cause="随便看看")
    with pytest.raises(RuntimeStateError):
        world.consume_time(info["id"], timeline_id, seconds=600, cause="   ")
    with pytest.raises(RuntimeStateError):
        world.consume_time(info["id"], timeline_id, seconds=600, cause="随手", source="随便")
    assert store.clock_get(timeline_id)["processed_world"] == START, "被拒的消耗不留痕"


def test_rollback_pulls_world_time_back(store, world) -> None:
    """回滚越过消耗点：世界时间跟着回到提交那一刻，不是永久超前。"""
    info, timeline_id = make_instance(store, world)
    world.advance(info["id"], timeline_id, now_real=T + 1)
    landmark = world.commit(info["id"], timeline_id, kind="manual", note="消耗之前")

    out = world.consume_time(info["id"], timeline_id, seconds=7200, cause="长途跋涉", now_real=T + 1)
    assert store.clock_get(timeline_id)["processed_world"] == landmark["moment"] + 7200

    world.rollback(info["id"], timeline_id, commit_id=landmark["id"], now_real=T + 3)
    row = store.clock_get(timeline_id)
    assert row["processed_world"] == landmark["moment"], "世界时间回到提交那一刻"
    assert row["base_world"] == landmark["moment"], "锚点也一起回退"
    assert store.commit_get(out["commit_id"]) is not None, "被截去的未来仍可读（覆盖语义）"
