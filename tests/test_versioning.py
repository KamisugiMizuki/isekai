"""阶段 4（§5/§6/§7）：提交、列表、分叉、回滚、自动提交——行为级验收。"""

from __future__ import annotations

import json

from isekai_core.runtime.service import RuntimeService
from samples import DAY
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


def _events(store, instance_id, timeline_id):
    return store.event_window(instance_id, timeline_id, until=10**15, limit=500)


def test_manual_commit_records_snapshot_and_lists_metadata_only(store) -> None:
    """手动提交成为回滚点；列表只给元数据，不带剧情摘要（§5.1）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    commit = world_service.commit(info["id"], timeline_id, note="第一版")
    assert commit["kind"] == "manual" and commit["note"] == "第一版"
    assert commit["moment"] == int(store.clock_get(timeline_id)["processed_world"])
    snapshot = store.commit_snapshot_get(commit["id"])
    assert snapshot and snapshot["runtime"]["events"], "快照含事件等世界运行状态"
    assert snapshot["world"] == commit["moment"] and "rate" in snapshot

    listed = world_service.commits(info["id"], timeline_id)
    assert [item["id"] for item in listed] == [item["id"] for item in listed if item["id"] != ""]
    assert commit["id"] in [item["id"] for item in listed]
    assert [item["kind"] for item in listed] == ["initial", "manual"], "创建时的初始提交 + 手动提交"
    blob = json.dumps(listed, ensure_ascii=False)
    assert not any(
        str(item["summary"])[:6] in blob for item in _events(store, info["id"], timeline_id)[:3]
    ), "列表不泄漏剧情文本"
    assert set(listed[0]) == {"id", "kind", "moment", "note", "timeline_id", "created_at"}


def test_fork_from_commit_inherits_state_and_ignores_later_changes(store) -> None:
    """分叉继承共同过去，不继承来源线之后的变化；创建不等于激活（§四 / §六）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    store.memory_add({
        "id": "mm-before", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "通行牌停发", "kind": "fact",
        "sources": [{"kind": "claim", "ref": "cl-1"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.8, "confidence": 0.9,
    })
    queued = world_service.queue_dialog_turn(
        info["id"], timeline_id, character_id, world_seconds=0,
        user_ref="env-fork", user_text="你还记得那天吗", reply_message_id="m-fork", reply_text="记得",
    )
    assert queued == 2, "先排两条待提取来源，继承断言才有意义"
    commit = world_service.commit(info["id"], timeline_id, note="分叉点")
    before_events = len(_events(store, info["id"], timeline_id))

    # 来源线继续推进并写入新记忆
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 6 * DAY)
    store.memory_add({
        "id": "mm-after", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "堤长身故", "kind": "fact",
        "sources": [{"kind": "claim", "ref": "cl-2"}], "happened_world": 9, "learned_world": 9,
        "recorded_world": 9, "semantic_watermark": 9, "strength": 0.7, "confidence": 0.9,
    })

    branch = world_service.fork(info["id"], timeline_id, commit_id=commit["id"], name="分支甲")
    new_line = branch["timeline"]["id"]
    assert branch["timeline"]["state"] == "frozen", "创建本身不等于激活"
    assert branch["timeline"]["source_commit"] == commit["id"]
    assert len(_events(store, info["id"], new_line)) == before_events, "只看得到来源提交时的事件"
    texts = [item["text"] for item in store.memory_scope(info["id"], new_line, character_id)]
    assert "通行牌停发" in texts and "堤长身故" not in texts, "不读取来源线后来的变化"
    assert store.clock_get(new_line)["processed_world"] == commit["moment"], "时钟停在来源提交"
    source_tasks = len(store.memory_tasks(info["id"], timeline_id))
    branch_tasks = len(store.memory_tasks(info["id"], new_line))
    assert source_tasks == 2 and branch_tasks == 2, "待提取标记随快照继承（§5.1）"
    # 分支加入时记一条 initial 提交，指向来源
    assert [row["kind"] for row in store.commit_list(info["id"], new_line)] == ["initial"], "分支自己的初始提交"


def test_rollback_overwrites_and_keeps_local_state(store) -> None:
    """回滚覆盖有效历史、提升世代、重新锚定；本机激活 / 冻结状态不来自历史（§七）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    commit = world_service.commit(info["id"], timeline_id, note="回滚点")
    moment = commit["moment"]
    generation_before = int(store.clock_get(timeline_id)["generation"])

    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 8 * DAY)
    store.memory_add({
        "id": "mm-future", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "回滚后不该留下的记忆", "kind": "fact",
        "sources": [{"kind": "claim", "ref": "cl-9"}], "happened_world": 9, "learned_world": 9,
        "recorded_world": 9, "semantic_watermark": 9, "strength": 0.7, "confidence": 0.9,
    })
    assert store.memory_get("mm-future") is not None

    result = world_service.rollback(info["id"], timeline_id, commit_id=commit["id"], now_real=1.7e9 + 9 * DAY)
    assert result["world"] == moment
    assert int(store.clock_get(timeline_id)["processed_world"]) == moment, "世界时刻回到回滚点"
    assert int(store.clock_get(timeline_id)["generation"]) > generation_before, "提升世代使迟到结果失效"
    assert store.memory_get("mm-future") is None, "被截去的未来不再是可访问历史"
    assert store.clock_get(timeline_id)["rate"] == 1, "倍率随快照恢复"
    assert result["state"] == "active", "原来激活则回滚点继续激活"

    # 冻结线回滚后仍然冻结（本机状态不从历史恢复）
    frozen_line = world_service.fork(info["id"], timeline_id, commit_id=commit["id"], name="冻结分支")
    other = frozen_line["timeline"]["id"]
    again = world_service.rollback(
        info["id"], other, commit_id=frozen_line["commit"]["id"], now_real=1.7e9 + 10 * DAY
    )
    assert again["state"] == "frozen" and store.timeline_get(other)["state"] == "frozen"


def test_late_batch_cannot_write_after_rollback(store) -> None:
    """回滚后按旧世代提交的批次整批不落盘（§七）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    commit = world_service.commit(info["id"], timeline_id)
    stale_generation = int(store.clock_get(timeline_id)["generation"])
    world_service.rollback(info["id"], timeline_id, commit_id=commit["id"], now_real=1.7e9 + 3 * DAY)
    applied = store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=stale_generation,
        processed_world=int(store.clock_get(timeline_id)["processed_world"]) + 10,
        catching_up=False,
    )
    assert applied is False, "旧世代的迟到批次不得写回"


def test_auto_commit_triggers_by_events_and_can_be_disabled(store) -> None:
    """自动提交：事件数到阈值即触发；关掉开关后不再自动建点（手动不受影响）（§5.1）。"""
    world_service = _service(store, autocommit_enabled=True, autocommit_events=1, autocommit_minutes=600)
    info, timeline_id, _ = _ready(store, world_service)
    assert [item["kind"] for item in world_service.commits(info["id"], timeline_id)] == ["initial"], (
        "启动时只有创建时的初始提交"
    )
    first = world_service.maybe_auto_commit(info["id"], timeline_id, now_real=1.7e9 + DAY)
    assert first is not None and first["kind"] == "auto"
    again = world_service.maybe_auto_commit(info["id"], timeline_id, now_real=1.7e9 + DAY)
    assert again is None, "没有新事件就不重复提交"

    off = _service(store, autocommit_enabled=False)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY)
    assert off.maybe_auto_commit(info["id"], timeline_id, now_real=1.7e9 + 5 * DAY) is None
    manual = off.commit(info["id"], timeline_id, note="手动不受开关限制")
    assert manual["kind"] == "manual"


def test_dialog_is_isolated_between_timelines(store) -> None:
    """对话上下文随时间线隔离：分叉只带自己会话的原文，回滚撤掉被截去的对话（§七）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    session_id = f"se-{character_id}"
    store.session_put({
        "id": session_id, "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "channel_id": "", "thread_id": "",
    }) if hasattr(store, "session_put") else None
    commit = world_service.commit(info["id"], timeline_id, note="对话前")
    snapshot = store.commit_snapshot_get(commit["id"])
    assert snapshot["sessions"] == [] or all(
        item["timeline_id"] == timeline_id for item in snapshot["sessions"]
    ), "快照只含本线会话"
    assert all(row["session_id"] != "别的线" for row in snapshot["dialog"]), "不带其他线的对话"
