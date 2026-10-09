"""批后置调用的自愈性（A-9d 的评估依据）。

`advance` 的批循环里，`decay_memories` 与 `apply_due_pending_events` 跑在
`store.apply_runtime_batch`（批事务）**之外**。A-9d 原本要求把它们移进同一事务。

在动手重构之前，先验证一个前提：**这两个操作是否幂等且自愈**——

- 记忆衰减从每行自己的 `decay_world` 起算，且衰减是指数可组合的 ⇒ 晚结算与逐批结算数值等价；
- 预约事件用**稳定事件 id**写入，INSERT 走 `ON CONFLICT DO NOTHING`，状态置位幂等 ⇒ 重跑不会重复施加。

若成立，崩溃窗口只造成「最多一批的延迟」，而不是「半批状态」；把它们塞进同一个大事务反而会拉长锁持有时间。
"""

from __future__ import annotations

from isekai_core.runtime import memory as memory_mod
from samples import DAY
from test_memory import _ready, _service  # noqa: F401  复用既有夹具
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def test_memory_decay_self_heals_and_reads_stay_correct(store, world) -> None:  # noqa: ANN001, F811
    """模拟「批已提交、后置调用没跑」：窗口内读取仍正确；下一批一次补齐（且 `decay_world` 推进到位）。"""
    world_service = _service(store, memory_decay_per_day=0.5, memory_decay_batch=256)
    info, timeline_id, character_id = _ready(store, world_service)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    store.memory_add({
        "id": "mm-heal", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "盐场记档", "kind": "fact",
        "sources": [{"kind": "experience", "ref": "ex-heal"}],
        "happened_world": watermark, "learned_world": watermark, "recorded_world": watermark,
        "semantic_watermark": watermark, "strength": 0.8, "confidence": 0.9,
    })

    # 第一步：把后置衰减换成 no-op，推进一批（世界前进了，但衰减没跑）
    original = world_service.decay_memories
    world_service.decay_memories = lambda *a, **k: 0  # type: ignore[assignment]
    try:
        world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 4 * DAY)
    finally:
        world_service.decay_memories = original  # type: ignore[assignment]

    row = store.memory_get("mm-heal")
    mid_world = int(store.clock_get(timeline_id)["processed_world"])
    assert int(row["decay_world"]) == watermark, "后置调用没跑：存储强度仍停在旧锚点"
    live = memory_mod.effective_strength(
        row, now_world=mid_world, day_seconds=DAY, per_day=0.5
    )
    assert live < float(row["strength"]), "窗口内读路径必须按当前水位惰性结算（不能读到过期的强度）"

    # 第二步：恢复正常后推进一批 —— 一次补齐到新水位，且不重复施加
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 6 * DAY)
    row = store.memory_get("mm-heal")
    final_world = int(store.clock_get(timeline_id)["processed_world"])
    expected = memory_mod.decayed_strength(
        0.8, from_world=watermark, to_world=final_world, day_seconds=DAY, per_day=0.5
    )
    assert int(row["decay_world"]) == final_world, "自愈后锚点推进到当前水位"
    assert abs(float(row["strength"]) - expected) < 1e-9, "补齐结果与「从未跳过」逐值等价"
