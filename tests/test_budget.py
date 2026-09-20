"""三层调用预算（§2.8）：预占 / 结算 / 三层上限 / 优先级保留 / 暂停 / 管理面视图。"""

from __future__ import annotations

import time

from isekai_core.runtime import budget
from isekai_core.runtime.service import RuntimeService
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _service(store, **over) -> RuntimeService:
    params = {
        "instance_tokens_per_day": 10_000,
        "timeline_tokens_per_day": 6_000,
        "task_tokens_per_day": 3_000,
        "priority_reserve_ratio": 0.25,
    }
    params.update(over)
    return RuntimeService(store, **params)


def _instance(store, world_service):
    info, timeline_id, character_id = make_instance(store, world_service)
    return info["id"], timeline_id, character_id


def _info(store, world_service):
    """需要完整实例字典的用例（activate 要 instance 对象）。"""
    return make_instance(store, world_service)


def test_priority_table_is_fixed_and_ordered() -> None:
    """优先级固定序（§2.8）：安全 > 对话提交 > 确定性事实 > 回填/展开 > 计划记忆 > 主动文本 > 向量化。"""
    assert budget.PRIORITIES[0] == "safety" and budget.PRIORITIES[-1] == "embedding_polish"
    assert budget.task_index("event_render") == budget.priority_index("confirmed_backfill")
    assert budget.task_index("intent_propose") == budget.priority_index("plan_memory")
    assert budget.task_index("proactive_text") == budget.priority_index("proactive_text")
    assert budget.task_index("event_render") < budget.task_index("proactive_text"), "回填比主动文本高"


def test_three_layers_each_can_refuse(store) -> None:
    """三层预算逐层生效：实例总预算、时间线预算、单任务预算（§2.8）。"""
    world_service = _service(store)
    instance_id, timeline_id, _ = _instance(store, world_service)
    limits = world_service.budget_limits_for(instance_id)

    ok = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="event_render", bucket=0,
        priority=budget.task_index("event_render"), tokens_est=100, limits=limits,
    )
    assert ok["ok"] is True
    store.budget_settle(ok["id"], tokens_actual=100)

    # 单任务预算：再要一大笔就顶到 task 上限
    tight = {**limits, "task_tokens_per_day": 150, "timeline_tokens_per_day": 10_000, "instance_tokens_per_day": 10_000}
    refused = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="event_render", bucket=0,
        priority=budget.task_index("event_render"), tokens_est=200, limits=tight,
    )
    assert refused["ok"] is False and refused["blocked"] == ["task"]

    # 时间线预算：另一条任务在同一线上也吃这条线的额度
    line_tight = {**limits, "timeline_tokens_per_day": 150, "instance_tokens_per_day": 10_000, "task_tokens_per_day": 10_000}
    refused = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="event_expand", bucket=0,
        priority=budget.task_index("event_expand"), tokens_est=200, limits=line_tight,
    )
    assert refused["ok"] is False and refused["blocked"] == ["timeline"]

    # 实例总预算：换任务 ID 也绕不过（实例层跨任务共享）
    inst_tight = {**limits, "instance_tokens_per_day": 150, "timeline_tokens_per_day": 10_000, "task_tokens_per_day": 10_000}
    refused = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="另一个任务名", bucket=0,
        priority=budget.task_index("event_expand"), tokens_est=200, limits=inst_tight,
    )
    assert refused["ok"] is False and refused["blocked"] == ["instance"], "改任务 ID 绕不过总预算"


def test_low_priority_keeps_room_for_higher(store) -> None:
    """预算紧张时给更高优先级留额度：低优先级先被停（§2.8）。"""
    world_service = _service(store)
    instance_id, timeline_id, _ = _instance(store, world_service)
    limits = {"instance_tokens_per_day": 1_000, "timeline_tokens_per_day": 10_000, "task_tokens_per_day": 10_000}
    first = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="event_render", bucket=0,
        priority=budget.task_index("event_render"), tokens_est=700, limits=limits,
    )
    assert first["ok"] is True
    store.budget_settle(first["id"], tokens_actual=700)
    reserve = {budget.task_index("proactive_text"): 250}  # 保留 25%
    low = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="proactive_text", bucket=0,
        priority=budget.task_index("proactive_text"), tokens_est=100, limits=limits,
        reserved_for_higher=reserve,
    )
    assert low["ok"] is False and "reserved_for_higher" in low["blocked"], "低优先级不占保留额度"
    high = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="event_render", bucket=0,
        priority=budget.task_index("event_render"), tokens_est=100, limits=limits,
        reserved_for_higher=reserve,
    )
    assert high["ok"] is True, "高优先级仍能拿到保留额度"


def test_settle_books_actual_and_release_frees(store) -> None:
    """结算按真实消耗记账（失败也算），取消的预占释放不记账（§2.8）。"""
    world_service = _service(store)
    instance_id, timeline_id, _ = _instance(store, world_service)
    limits = world_service.budget_limits_for(instance_id)
    held = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="event_render", bucket=0,
        priority=budget.task_index("event_render"), tokens_est=800, limits=limits,
    )
    usage = store.budget_usage(instance_id, bucket=0)
    assert usage["instance"] == 800, "在途预占也算占用"
    store.budget_settle(held["id"], tokens_actual=120, outcome="error", calls=2)
    usage = store.budget_usage(instance_id, bucket=0)
    assert usage["instance"] == 120, "按真实消耗结算，未用的预占释放"
    rows = store.budget_rows(instance_id, bucket=0)
    assert rows and rows[0]["calls"] == 2 and rows[0]["tokens"] == 120, "失败调用也记账"

    dropped = store.budget_reserve(
        instance_id=instance_id, timeline_id=timeline_id, task="event_expand", bucket=0,
        priority=budget.task_index("event_expand"), tokens_est=500, limits=limits,
    )
    assert store.budget_release(dropped["id"]) is True
    usage = store.budget_usage(instance_id, bucket=0)
    assert usage["instance"] == 120, "取消的预占释放且不记账"


def test_reserve_refuses_when_paused(store) -> None:
    """管理面可以暂停低优先级任务：暂停后发起前就被拒（§2.8）。"""
    world_service = _service(store)
    instance_id, timeline_id, _ = _instance(store, world_service)
    store.budget_policy_set(instance_id, paused_tasks=["proactive_text"])
    view = world_service.budget_view(instance_id)
    assert view["paused_tasks"] == ["proactive_text"]
    refused = world_service.reserve_call(instance_id, timeline_id, "proactive_text", prompt_text="喂")
    assert refused["ok"] is False and refused["blocked"] == ["paused"]
    allowed = world_service.reserve_call(instance_id, timeline_id, "event_render", prompt_text="喂")
    assert allowed["ok"] is True


def test_render_task_call_cap_and_settle_counts_attempts(store) -> None:
    """单任务的调用次数上限与「重试共用剩余预算」（§2.8）。"""
    world_service = _service(store, render_calls_per_day=1)
    info, timeline_id, _ = _info(store, world_service)
    instance_id = info["id"]
    first = world_service.reserve_call(instance_id, timeline_id, "event_render", prompt_text="骨架")
    assert first["ok"] is True
    world_service.settle_call(first, prompt_text="骨架", reply="文本", calls=2)
    second = world_service.reserve_call(instance_id, timeline_id, "event_render", prompt_text="骨架")
    assert second["ok"] is False and second["blocked"] == ["task_calls"], "同任务当日次数用尽"
    assert store.call_ledger_get(instance_id, timeline_id, "event_render", bucket=int(time.time() // 86400)) == 2


def test_budget_view_is_content_free(store) -> None:
    """预算视图只有非内容性账目：用量 / 上限 / 暂停项 / 优先级序（§2.8）。"""
    world_service = _service(store)
    instance_id, timeline_id, _ = _instance(store, world_service)
    world_service.reserve_call(instance_id, timeline_id, "event_render", prompt_text="秘密骨架文本")
    view = world_service.budget_view(instance_id)
    blob = repr(view)
    assert "秘密骨架文本" not in blob, "不记正文"
    assert view["limits"]["instance_tokens_per_day"] == 10_000
    assert view["priority_order"][0] == "safety"
    assert isinstance(view["rows"], list)


def test_deterministic_advance_survives_exhausted_budget(store) -> None:
    """预算耗尽不影响确定性事实推进：世界照常前进（§2.8 / §2.4）。"""
    world_service = _service(store, instance_tokens_per_day=1)
    info, timeline_id, _ = _info(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 3 * DAY)
    assert int(store.clock_get(timeline_id)["processed_world"]) > 0, "事实推进不因语言预算不足而跳过"
    assert world_service.reserve_call(info["id"], timeline_id, "event_render", prompt_text="x")["ok"] is False
