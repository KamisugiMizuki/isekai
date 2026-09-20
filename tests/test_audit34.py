"""阶段 3/4 审计修正 A 的回归：回滚覆盖面、快照元数据、时间线管理。"""

from __future__ import annotations

import json
import time

from isekai_core.runtime.service import RuntimeService, RuntimeStateError
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _service(store, **over) -> RuntimeService:
    params = {
        "instance_tokens_per_day": 400_000,
        "timeline_tokens_per_day": 150_000,
        "task_tokens_per_day": 60_000,
        "autocommit_enabled": False,
    }
    params.update(over)
    return RuntimeService(store, **params)


def _ready(store, world_service, *, moment=DAY * 1500):
    info, timeline_id, character_id = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    return info, timeline_id, character_id


def test_commit_snapshot_carries_semantic_metadata(store) -> None:
    """快照带来源 / 种子 / 规则版本（§5.1）：恢复与复算要能对上。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    commit = world_service.commit(info["id"], timeline_id)
    snapshot = store.commit_snapshot_get(commit["id"])
    assert snapshot["rules_version"] and snapshot["data_format"], "语义元数据随快照"
    assert "seed" in snapshot
    from isekai_core.version import DATA_FORMAT_VERSION, RULES_VERSION

    assert snapshot["data_format"] == DATA_FORMAT_VERSION and snapshot["rules_version"] == RULES_VERSION
    assert snapshot["seed"] == world_service.seed_of(store.instance_get(info["id"])), "与抽样同源的种子"


def test_rollback_across_join_point_removes_joined_character(store) -> None:
    """回滚跨越补卡加入点时，补入角色在本线退出（§七）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    world_service.ensure_instance(info["id"], now_real=1.7e9)  # 初始角色入册（核心启动时的那一步）
    commit = world_service.commit(info["id"], timeline_id, note="补卡前")
    joined = sample_card(sample_package(), name="堤砚")
    world_service.add_character(
        info["id"], timeline_id, joined, now_real=1.7e9 + 3 * DAY,
        joined_world=int(store.clock_get(timeline_id)["processed_world"]), note="补卡",
    )
    joined_rows = store.character_join_list(info["id"], timeline_id)
    assert [row["character_id"] for row in joined_rows] == ["cc-堤砚"], "补卡的角色入册"
    result = world_service.rollback(info["id"], timeline_id, commit_id=commit["id"], now_real=1.7e9 + 4 * DAY)
    assert store.character_join_list(info["id"], timeline_id) == [], "补入角色随回滚退出本线"
    assert result["timeline"] and result["world"] == commit["moment"]


def test_rollback_clears_pending_rate_commands(store) -> None:
    """历史里的待生效倍率命令不作为现时控制命令重发（§七）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    commit = world_service.commit(info["id"], timeline_id, note="倍率前")
    store.rate_add(
        timeline_id, input_real=time.time(), effective_real=int(time.time()) + 60, rate=3600, seq=99
    )
    assert store.rate_pending(timeline_id), "先有一条待生效命令"
    world_service.rollback(info["id"], timeline_id, commit_id=commit["id"], now_real=1.7e9 + 3 * DAY)
    assert store.rate_pending(timeline_id) == [], "回滚后不残留"


def test_rollback_voids_inflight_input_and_cancels_undelivered_reply(store) -> None:
    """回滚作废处理中的输入、取消未投递的回复：迟到的结果不得写回或继续发送（§七）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    session = store.session_ensure(info["id"], timeline_id, character_id)
    store.inbound_put(
        session_id=session["id"], channel_id="cli-dev", thread_id="t-1", env_id="env-late",
        text="飞在半路上的问题", binding_version=1,
    )
    outbound = store.outbound_put(
        session_id=session["id"], message_id="m-late", reply_to="env-late", covers=["env-late"],
        batches=[["还没送出去"]], target_channel="cli-dev", target_thread="t-1",
        binding_version=1, binding_token="tok",
    )
    commit = world_service.commit(info["id"], timeline_id, note="回滚点")
    result = world_service.rollback(info["id"], timeline_id, commit_id=commit["id"], now_real=1.7e9 + 3 * DAY)
    assert result["voided_inputs"] >= 1, "处理中的输入被登记作废"
    assert store.void_has("cli-dev", "t-1", "env-late") is True
    assert result["cancelled_replies"] >= 1, "未投递的回复在回滚时被取消"
    row = store.message_get(outbound["seq"])
    assert row is None or row["state"] == "cancelled", "回滚后它既不在投递队列里、也不残留可投状态"


def test_timeline_rename_archive_delete(store) -> None:
    """命名 / 描述 / 归档（先冻结）/ 删除，且删除保留被其他线引用的提交（§四）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    renamed = world_service.rename_timeline(info["id"], timeline_id, name="主线·改", description="第一人称线")
    assert renamed["name"] == "主线·改" and renamed["description"] == "第一人称线"

    commit = world_service.commit(info["id"], timeline_id)
    branch = world_service.fork(info["id"], timeline_id, commit_id=commit["id"], name="分支")
    other = branch["timeline"]["id"]

    archived = world_service.archive_timeline(info["id"], timeline_id, now_real=1.7e9 + 3 * DAY)
    assert archived["state"] == "archived" and store.timeline_get(timeline_id)["state"] == "archived"

    counts = world_service.delete_timeline(info["id"], timeline_id)["counts"]
    assert counts["timeline"] == 1
    assert store.timeline_get(timeline_id) is None, "线本体已删"
    assert store.commit_get(commit["id"]) is not None, "其他线引用的提交保留"


def test_cannot_delete_the_last_timeline(store) -> None:
    """实例至少保留一条线（§四：删除需确认，这里先挡住会把实例删空的情况）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    try:
        world_service.delete_timeline(info["id"], timeline_id)
    except RuntimeStateError as exc:
        assert "至少" in str(exc)
    else:
        raise AssertionError("删最后一条线应当被拒")


def test_festival_over_budget_is_rejected_before_creation() -> None:
    """包内同日固定事件超过密度档上限 → 创建前校验报错（附录 B #3 后半）。"""
    from isekai_core.world.validate import validate_package

    package = sample_package()
    family = package["events"]["families"][0]["id"]
    package["events"]["density"] = "稀疏"  # 上限 1
    package["events"]["calendar"] = [
        {"id": "fx-a", "name": "开滩节", "month": 1, "day": 1, "family": family},
        {"id": "fx-b", "name": "祭堤日", "month": 1, "day": 1, "family": family},
    ]
    errors = validate_package(package)
    assert any("密度档" in item and "固定事件" in item for item in errors), errors
    package["events"]["calendar"] = package["events"]["calendar"][:1]
    assert not [item for item in validate_package(package) if "密度档" in item]


def test_fabricated_accusation_in_annals_has_no_effect(store) -> None:
    """完全虚构的指控可以入史料，但不产生事实效果（附录 B #8）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    before = len(store.effect_window(info["id"], timeline_id, until=10**15))
    event_id = store.event_window(info["id"], timeline_id, until=10**15, limit=1)[0]["id"]
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    store.claim_put({
        "id": "cl-annals", "instance_id": info["id"], "timeline_id": timeline_id, "event_id": event_id,
        "source_id": "src-1", "text": "信报载：堤长私吞了修堤的粮", "audience": "public",
        "earliest_world": watermark + 1, "credibility": 0.9, "derived_from": None,
    })
    def _mentions_accusation() -> list[str]:
        """事件与效果里有没有把「私吞」当成发生过的事（才叫指控变成实情）。"""
        hits = [
            str(row["summary"]) for row in store.event_window(info["id"], timeline_id, until=10**15, limit=500)
            if "私吞" in str(row["summary"])
        ]
        for row in store.effect_window(info["id"], timeline_id, until=10**15):
            if "私吞" in str(row.get("value") or "") or "私吞" in str(row.get("recovery") or ""):
                hits.append(f"effect:{row['id']}")
        return hits

    assert _mentions_accusation() == [], "写进说法不会产生事实效果"
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY)
    assert _mentions_accusation() == [], "获知与传播之后仍不成为实情"
    learned = store.knowledge_window(info["id"], timeline_id, character_id, until=10**15, limit=500)
    assert any("私吞" in str(row["text"]) for row in learned), "指控经渠道可被获知与讨论"
    _ = before
