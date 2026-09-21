"""记忆积压的三道口子（MEMORY_SPEC §4.1）：入队筛选 / 队列有界 / 积压汇总。

背景（2026-09-22 实测本地库）：世界跑 626 个世界日产出 2443 条来源，而提取受现实日预算
限速只跑了 19 次调用 → 2359 条 pending。产出按世界时间、预算按现实时间，缺口是结构性的。
"""

from __future__ import annotations

import asyncio
import time

from isekai_core.runtime import memory as memory_mod
from isekai_core.runtime.service import RuntimeService
from isekai_core.store import MEMORY_PENDING_CAP, Store
from samples import DAY, sample_card, sample_package
from isekai_core.world.instances import create_instance


def make_instance(store: Store, world: RuntimeService):
    package = sample_package(moment=DAY * 1500)
    card = sample_card(package)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    world.ensure_instance(info["id"], now_real=time.time())
    return info, timeline_id, character_id


def test_life_slices_are_sampled_per_world_day() -> None:
    """日程切片按世界日采样、睡眠不算候选；真实行动照登。"""
    rows = [
        {"id": "exp-sleep-1", "kind": "life", "world_seconds": DAY * 10, "summary": "sleep（夜）"},
        {"id": "exp-life-1", "kind": "life", "world_seconds": DAY * 10 + 100, "summary": "滩口值守与水位尺读数（晨）"},
        {"id": "exp-life-2", "kind": "life", "world_seconds": DAY * 10 + 200, "summary": "回屋补网（午）"},
        {"id": "exp-life-3", "kind": "life", "world_seconds": DAY * 11 + 50, "summary": "巡堤（晨）"},
        {"id": "exp-act-1", "kind": "action", "world_seconds": DAY * 10 + 300, "summary": "自己动手了：修船"},
    ]
    kept, skipped = memory_mod.select_experience_sources(rows, day_seconds=DAY)
    assert kept == {"exp-life-1", "exp-life-3", "exp-act-1"}, "每天留一条代表项 + 非 life 全留"
    assert skipped == 2, "睡眠与同日多出的切片被跳过（不是入队后再淘汰）"


def test_pending_queue_is_bounded_and_drops_cheap_material_first(tmp_path) -> None:
    """§4.1「有界重试队列」：超上限时淘汰最旧的低价值项，对话与转述留到最后。"""
    store = Store(tmp_path / "data" / "isekai.db")
    store.ensure_schema()
    try:
        base = {"instance_id": "in-x", "timeline_id": "tl-x", "character_id": "cc-x",
                "source_world": 100, "created_world": 100, "text": ""}
        for index in range(MEMORY_PENDING_CAP + 20):
            store.memory_task_add({**base, "id": f"mt-exp-{index:04d}", "source_kind": "experience",
                                   "source_ref": f"exp-{index:04d}", "source_world": 100 + index})
        pending = store.memory_tasks("in-x", "tl-x")
        assert len(pending) == MEMORY_PENDING_CAP, "队列必须有界"
        dropped = [row for row in store.memory_tasks("in-x", "tl-x", state="dropped")]
        assert dropped, "淘汰要留痕（dropped + 原因），不是静默删除"
        assert all(str(row["source_kind"]) == "experience" for row in dropped), "先淘汰流水账"

        # 之后进的对话挤掉的是低价值项，自己一定活得下来
        store.memory_task_add({**base, "id": "mt-dlg-1", "source_kind": "dialog", "source_ref": "user:1"})
        kinds = {str(row["source_kind"]) for row in store.memory_tasks("in-x", "tl-x")}
        assert "dialog" in kinds and "experience" in kinds
        assert len(store.memory_tasks("in-x", "tl-x")) == MEMORY_PENDING_CAP
    finally:
        store.close()


class _CompactLLM:
    """按要价回一条概括：ref 取材料里的第一条，验证引用必须来自给定批次。"""

    def __init__(self, *, bad: bool = False) -> None:
        self.calls = 0
        self.bad = bad
        self.seen_prompt = ""

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        self.seen_prompt = "\n".join(str(m.get("content") or "") for m in messages)
        if self.bad:
            return "抱歉，我没有任何格式。"
        first_ref = self.seen_prompt.split("[")[-1].split("]")[0]
        return (
            '[{"text":"那阵子她大多在滩口值守","kind":"fragment","ref":"%s",'
            '"strength":0.6,"confidence":0.7}]' % first_ref
        )


def _seed_backlog(store: Store, instance_id: str, timeline_id: str, character_id: str, count: int) -> None:
    for index in range(count):
        store.memory_task_add({
            "id": f"mt-exp-{index:04d}", "instance_id": instance_id, "timeline_id": timeline_id,
            "character_id": character_id, "source_kind": "experience", "source_ref": f"exp-{index:04d}",
            "source_world": DAY * 100 + index, "created_world": DAY * 100 + index, "text": "",
        }, pending_cap=0)  # 测试里自己灌量，别让有界淘汰插手


def _experience_rows(store: Store, instance_id: str, timeline_id: str, character_id: str, count: int) -> None:
    rows = [
        {"id": f"exp-{index:04d}", "instance_id": instance_id, "timeline_id": timeline_id,
         "character_id": character_id, "world_seconds": DAY * 100 + index,
         "kind": "life", "summary": f"滩口值守与水位尺读数（第 {index} 天）",
         "source_ref": None, "confidence": 1.0}
        for index in range(count)
    ]
    for row in rows:
        store.experience_add(row)


def test_compact_backlog_digests_a_batch_into_few_memories(tmp_path) -> None:
    """积压汇总：一次调用吃一批旧流水账 → 少量概括条目；整批合上账。"""
    store = Store(tmp_path / "data" / "isekai.db")
    store.ensure_schema()
    try:
        world = RuntimeService(store, instance_tokens_per_day=10_000_000)
        info, timeline_id, character_id = make_instance(store, world)
        _experience_rows(store, info["id"], timeline_id, character_id, 12)
        _seed_backlog(store, info["id"], timeline_id, character_id, 12)
        llm = _CompactLLM()
        result = asyncio.run(world.compact_backlog(
            info["id"], timeline_id, llm=llm, now_real=time.time(), batch=12, limit=1
        ))
        assert result["calls"] == 1, result
        assert result["materials"] == 12 and result["written"] == 1, result
        assert llm.calls == 1
        assert "滩口值守与水位尺读数" in llm.seen_prompt, "材料正文要真的进 prompt（曾被读成空串）"
        assert store.memory_tasks(info["id"], timeline_id) == [], "这批要合上账"
        memories = store.memory_scope(info["id"], timeline_id, character_id, until=10**15)
        assert memories and "滩口值守" in str(memories[-1]["text"])
        assert store.call_ledger_get(
            info["id"], timeline_id, "memory_compact", bucket=int(time.time() // 86400)
        ) == 1
    finally:
        store.close()


def test_compact_backlog_keeps_unparsable_batches_pending(tmp_path) -> None:
    """汇总结果解析不出来 → 整批保持待处理，不假装做过。"""
    store = Store(tmp_path / "data" / "isekai.db")
    store.ensure_schema()
    try:
        world = RuntimeService(store, instance_tokens_per_day=10_000_000)
        info, timeline_id, character_id = make_instance(store, world)
        _experience_rows(store, info["id"], timeline_id, character_id, 6)
        _seed_backlog(store, info["id"], timeline_id, character_id, 6)
        llm = _CompactLLM(bad=True)
        result = asyncio.run(world.compact_backlog(
            info["id"], timeline_id, llm=llm, now_real=time.time(), batch=6, limit=1
        ))
        assert result["written"] == 0
        pending = store.memory_tasks(info["id"], timeline_id)
        assert len(pending) == 6 and all("不可解析" in str(row["note"]) for row in pending)
    finally:
        store.close()
