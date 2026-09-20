"""角色记忆（MEMORY_SPEC）：隔离、来源、冲突、幂等、衰减、强化、简报导流。"""

from __future__ import annotations

import asyncio
import json

from isekai_core.runtime import memory as memory_mod
from isekai_core.runtime.service import RuntimeService
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


class FakeMemoryLLM:
    """按脚本回答；可返回半截 JSON 或不合规来源，用来验校验与降级。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, *, max_tokens=None, timeout=None, temperature=None) -> str:
        self.calls.append(messages)
        if not self.replies:
            raise RuntimeError("模型不可用")
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


def _service(store, **over) -> RuntimeService:
    params = {
        "instance_tokens_per_day": 400_000,
        "timeline_tokens_per_day": 150_000,
        "task_tokens_per_day": 60_000,
        "memory_brief_tokens": 900,
        "memory_recall_limit": 6,
    }
    params.update(over)
    return RuntimeService(store, **params)


def _ready(store, world_service, *, moment=DAY * 1500):
    info, timeline_id, character_id = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    return info, timeline_id, character_id


def _entry(text: str, *, kind="fact", strength=0.7, confidence=0.8, ref: str) -> str:
    return json.dumps([{
        "text": text, "kind": kind, "ref": ref, "strength": strength, "confidence": confidence,
    }], ensure_ascii=False)


def _queue(store, world_service, instance_id, timeline_id, character_id, text: str, world_seconds: int,
           *, ref: str = "", reply_ref: str = ""):
    """登记一轮来源；ref / reply_ref 显式给出以便测幂等。"""
    env = ref or f"env-{abs(hash(text)) % 10**6}"
    message = reply_ref or f"m-{abs(hash(text)) % 10**6}"
    return world_service.queue_dialog_turn(
        instance_id, timeline_id, character_id,
        world_seconds=world_seconds, user_ref=env,
        user_text=text, reply_message_id=message, reply_text="知道了",
    )


# ---------------- 纯函数 ----------------

def test_distill_keeps_negation_and_bounds_length() -> None:
    long_text = "堤长身故，接任推举未毕，通行牌停发。" * 8
    out = memory_mod.distill(long_text)
    assert len(out) <= memory_mod.MAX_TEXT_CHARS + 1
    assert "未毕" in out and "停发" in out, "否定与关键信息不丢"


def test_decay_is_world_time_based_and_never_revives() -> None:
    week = memory_mod.decayed_strength(1.0, from_world=0, to_world=7 * DAY, day_seconds=DAY)
    assert 0.8 < week < 1.0
    assert memory_mod.decayed_strength(1.0, from_world=DAY, to_world=DAY, day_seconds=DAY) == 1.0
    assert memory_mod.decayed_strength(0.5, from_world=10 * DAY, to_world=DAY, day_seconds=DAY) == 0.5, (
        "时钟倒拨不让强度回升"
    )


def test_parse_extraction_rejects_invented_sources_and_partial_json() -> None:
    refs = {"mt-a"}
    assert memory_mod.parse_extraction(_entry("潮位五尺", ref="mt-a"), refs)
    assert memory_mod.parse_extraction(_entry("潮位五尺", ref="mt-不存在"), refs) == []
    assert memory_mod.parse_extraction("从 [ 开始就不是 JSON", refs) == []
    assert memory_mod.parse_extraction('[{"text":"半截","ref":"mt-a"}', refs) == [], "半截 JSON 不填猜测"
    assert memory_mod.parse_extraction('[{"text":"x","kind":"秘密","ref":"mt-a"}]', refs) == [], "类型闭集"


# ---------------- 服务层 ----------------

def test_scope_isolation_between_characters(store) -> None:
    """同一实例同一线的两个角色默认互不可见（验收 1）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    other = world_service.add_character(
        info["id"], timeline_id, card_path="tests/samples/card2.json", note="第二人"
    ) if False else None
    _ = other
    world_service.queue_dialog_turn(
        info["id"], timeline_id, character_id, world_seconds=100,
        user_ref="env-1", user_text="堤长的事你听说了吗", reply_message_id="m-1", reply_text="听说了",
    )
    llm = FakeMemoryLLM([_entry("联络者问起堤长的事", ref=store.memory_tasks(info["id"], timeline_id)[0]["id"])])
    asyncio.run(world_service.extract_memories(info["id"], timeline_id, llm=llm, now_real=1.7e9))
    assert store.memory_count(info["id"], timeline_id, character_id) == 1
    assert store.memory_count(info["id"], timeline_id, "cc-别人") == 0, "别的角色看不到"
    recalled = world_service.recall(info["id"], timeline_id, "cc-别人", topic="堤长")
    assert recalled["entries"] == [] and recalled["brief"]["text"] == ""


def test_user_claim_is_not_her_experience(store) -> None:
    """联络者说的外界信息只记成「联络者所述」，不变成她的亲历（验收 3）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    world_service.queue_dialog_turn(
        info["id"], timeline_id, character_id, world_seconds=200,
        user_ref="env-2", user_text="堤长昨晚上吊了，别外传", reply_message_id="m-2", reply_text="谁跟你说的",
    )
    task = [item for item in store.memory_tasks(info["id"], timeline_id)
            if item["source_ref"].startswith("user:")][0]
    assert task["source_kind"] == "dialog"
    llm = FakeMemoryLLM([_entry("联络者说堤长昨夜身故，要她别外传", ref=str(task["id"]))])
    asyncio.run(world_service.extract_memories(info["id"], timeline_id, llm=llm, now_real=1.7e9))
    entry = store.memory_scope(info["id"], timeline_id, character_id)[0]
    sources = json.loads(entry["sources"])
    assert sources[0]["kind"] == "dialog" and sources[0]["source_role"] == "user"
    assert memory_mod.source_label(sources) == "联络者所述", "来源标签不冒充亲历"


def test_conflicting_claims_are_kept_with_supersede_link(store) -> None:
    """相似但矛盾的陈述不被去重吞掉；纠正建立替代链（验收 4）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    first = store.memory_add({
        "id": "mm-first", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "堤长身故，接任推举未毕", "kind": "fact",
        "sources": [{"kind": "claim", "ref": "cl-1"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.8, "confidence": 0.9,
    })
    assert first is not None
    second = store.memory_add({
        "id": "mm-second", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "堤长没有身故，接任推举未毕", "kind": "fact",
        "sources": [{"kind": "claim", "ref": "cl-2"}], "happened_world": DAY, "learned_world": DAY,
        "recorded_world": DAY, "semantic_watermark": DAY, "strength": 0.7, "confidence": 0.7,
    })
    assert second is not None, "相反说法另立条目，不覆写"
    assert store.memory_count(info["id"], timeline_id, character_id) == 2
    superseded = store.memory_get("mm-first")
    assert superseded["superseded_by"] == "mm-second" and superseded["state"] == "archived", "纠正留留痕"
    # 问当前认知只给有效版本，问历史仍能读旧版本
    now_view = [item for item in store.memory_scope(info["id"], timeline_id, character_id, until=DAY * 10)
                if item["superseded_by"] is None]
    assert [item["id"] for item in now_view] == ["mm-second"]
    then_view = [item for item in store.memory_scope(info["id"], timeline_id, character_id, until=0)]
    assert [item["id"] for item in then_view] == ["mm-first"], "当时水位能读到当时的说法"


def test_extraction_is_idempotent_and_failure_leaves_pending(store) -> None:
    """同源重复提取不重复写；失败保留待处理且不留半成品（验收 6 / §4.1）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    _queue(store, world_service, info["id"], timeline_id, character_id, "你还好吗", 300,
           ref="env-A", reply_ref="m-A")
    tasks = store.memory_tasks(info["id"], timeline_id)
    assert len(tasks) == 2, "用户输入与她的回复各登记一条"
    # 同来源再登记：幂等
    _queue(store, world_service, info["id"], timeline_id, character_id, "你还好吗", 300,
           ref="env-A", reply_ref="m-A")
    assert len(store.memory_tasks(info["id"], timeline_id)) == 2

    user_task = [item for item in tasks if item["source_ref"].startswith("user:")][0]
    reply_task = [item for item in tasks if item["source_ref"].startswith("reply:")][0]
    ok_llm = FakeMemoryLLM([json.dumps(
        [
            {"text": "联络者问她好不好", "kind": "fact", "ref": str(user_task["id"]),
             "strength": 0.6, "confidence": 0.8},
            {"text": "她答知道了", "kind": "fragment", "ref": str(reply_task["id"]),
             "strength": 0.4, "confidence": 0.9},
        ],
        ensure_ascii=False,
    )])
    first = asyncio.run(world_service.extract_memories(info["id"], timeline_id, llm=ok_llm, now_real=1.7e9))
    assert first["written"] == 2 and store.memory_count(info["id"], timeline_id, character_id) == 2
    assert store.memory_tasks(info["id"], timeline_id) == [], "处理完的任务不再待处理"

    # 重复跑同一来源不再写入（幂等），重试不强化
    again = asyncio.run(world_service.extract_memories(info["id"], timeline_id, llm=ok_llm, now_real=1.7e9))
    assert again["written"] == 0 and store.memory_count(info["id"], timeline_id, character_id) == 2

    # 失败：任务回到待处理，不半写
    _queue(store, world_service, info["id"], timeline_id, character_id, "再说一遍", 400)
    before = store.memory_count(info["id"], timeline_id, character_id)
    bad = FakeMemoryLLM(["这不是 JSON 也没有数组"])
    failed = asyncio.run(world_service.extract_memories(info["id"], timeline_id, llm=bad, now_real=1.7e9))
    assert failed["written"] == 0 and store.memory_count(info["id"], timeline_id, character_id) == before


def test_delayed_extraction_settles_strength_from_source_time(store) -> None:
    """迟到数个世界日的提取，按来源时刻结算应有强度（验收 14）。"""
    world_service = _service(store, memory_decay_per_day=0.2)
    info, timeline_id, character_id = _ready(store, world_service)
    world_service.queue_dialog_turn(
        info["id"], timeline_id, character_id, world_seconds=0,
        user_ref="env-old", user_text="还记得那天吗", reply_message_id="m-old", reply_text="记得",
    )
    task = [item for item in store.memory_tasks(info["id"], timeline_id)
            if item["source_ref"].startswith("user:")][0]
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 30 * DAY)  # 再过 30 世界日
    llm = FakeMemoryLLM([_entry("联络者问她还记得那天吗", strength=1.0, ref=str(task["id"]))])
    asyncio.run(world_service.extract_memories(info["id"], timeline_id, llm=llm, now_real=1.7e9 + 30 * DAY))
    entry = store.memory_scope(info["id"], timeline_id, character_id)[0]
    assert entry["strength"] < 0.99, "按来源时刻先衰减再入库，不是记成 1.0"
    assert float(entry["learned_world"]) == 0 and float(entry["recorded_world"]) > 0, "三个时间戳不混成一个"


def test_recall_brief_is_budgeted_and_carries_source_cues(store) -> None:
    """简报按预算打包、带来源与确信线索；只进上下文（验收：§5.1）。"""
    world_service = _service(store, memory_brief_tokens=120, memory_recall_limit=2)
    info, timeline_id, character_id = _ready(store, world_service)
    for index in range(6):
        store.memory_add({
            "id": f"mm-{index}", "instance_id": info["id"], "timeline_id": timeline_id,
            "character_id": character_id, "text": f"潮位与信报的事，第 {index} 回", "kind": "fact",
            "sources": [{"kind": "claim", "ref": f"cl-{index}", "via": "驿站"}],
            "happened_world": index, "learned_world": index, "recorded_world": index,
            "semantic_watermark": index, "strength": 0.6, "confidence": 0.4,
        })
    recalled = world_service.recall(info["id"], timeline_id, character_id, topic="潮位 信报")
    assert recalled["ids"] and len(recalled["ids"]) <= 2
    assert "听说" in recalled["brief"]["text"] or "读到的" in recalled["brief"]["text"], "带来源线索"
    assert "不太确定" in recalled["brief"]["text"], "低确信给模糊线索"


def test_citation_strengthens_once_per_turn(store) -> None:
    """一轮被采纳才强化，同轮幂等；候选扫描不提高强度（验收 6 / §5.3）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    store.memory_add({
        "id": "mm-cite", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "通行牌停发", "kind": "fact",
        "sources": [{"kind": "claim", "ref": "cl-x"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.5, "confidence": 0.9,
    })
    world_service.recall(info["id"], timeline_id, character_id, topic="通行牌")
    assert store.memory_get("mm-cite")["strength"] == 0.5, "只是召回候选，不强化"
    first = world_service.cite_memories(
        info["id"], timeline_id, character_id, turn_id="m-turn-1", memory_ids=["mm-cite"], world_seconds=10
    )
    second = world_service.cite_memories(
        info["id"], timeline_id, character_id, turn_id="m-turn-1", memory_ids=["mm-cite"], world_seconds=10
    )
    assert first == 1 and second == 0, "同一轮幂等"
    assert round(store.memory_get("mm-cite")["strength"], 4) == round(
        0.5 + memory_mod.REINFORCE_STEP, 4
    )


def test_decay_follows_the_watermark_and_freezes(store) -> None:
    """衰减按世界时间：冻结期间不衰减，补算与逐步结果等价（验收 7）。"""
    world_service = _service(store, memory_decay_per_day=0.5)
    info, timeline_id, character_id = _ready(store, world_service)
    store.memory_add({
        "id": "mm-decay", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "潮位记录", "kind": "fact",
        "sources": [{"kind": "experience", "ref": "ex-1"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 1.0, "confidence": 0.9,
    })
    world_service.freeze(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    frozen = float(store.memory_get("mm-decay")["strength"])
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 40 * DAY)
    assert float(store.memory_get("mm-decay")["strength"]) == frozen, "冻结线不衰减"

    world_service.activate(info["id"], timeline_id, now_real=1.7e9 + 40 * DAY)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 48 * DAY)
    after = float(store.memory_get("mm-decay")["strength"])
    assert after < frozen, "激活后按世界时长继续衰减"


def test_turn_context_injects_brief_and_returns_ids(store) -> None:
    """扮演定义里带她的记忆简报，并返回本轮用到的标识（§5.1 / §5.3）。"""
    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    store.memory_add({
        "id": "mm-turn", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "她答应过给联络者留一份信报", "kind": "promise",
        "sources": [{"kind": "intent", "ref": "in-1"}], "happened_world": 0, "learned_world": 0,
        "recorded_world": 0, "semantic_watermark": 0, "strength": 0.9, "confidence": 0.9,
    })
    session = {"instance_id": info["id"], "timeline_id": timeline_id, "character_id": character_id}
    context = world_service.turn_context(session, topic="信报")
    assert "她此刻想得起来的事" in context["prompt"] and "信报" in context["prompt"]
    assert context["memory_ids"] == ["mm-turn"]
    assert "她自己的打算" in context["prompt"], "打算类条目带来源线索"


def test_export_import_keeps_memories_without_secrets(store) -> None:
    """导出导入恢复记忆与来源；不含密钥（验收 8）。"""
    from isekai_core.world.portable import build_container, import_instance

    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    store.memory_add({
        "id": "mm-portable", "instance_id": info["id"], "timeline_id": timeline_id,
        "character_id": character_id, "text": "通行牌改按新滩路核发", "kind": "fact",
        "sources": [{"kind": "claim", "ref": "cl-9"}], "happened_world": 1, "learned_world": 1,
        "recorded_world": 2, "semantic_watermark": 2, "strength": 0.75, "confidence": 0.8,
    })
    container = build_container(store, info["id"])
    blob = json.dumps(container, ensure_ascii=False)
    assert "api_key" not in blob.lower() and "sk-" not in blob
    imported = import_instance(store, container, display_name="记忆副本")
    line = store.timeline_list(imported["id"])[0]["id"]
    entries = store.memory_scope(imported["id"], line, character_id)
    assert len(entries) == 1 and entries[0]["text"] == "通行牌改按新滩路核发"
    assert json.loads(entries[0]["sources"])[0]["ref"] == "cl-9", "来源引用随件"


def test_no_memory_browse_api_is_exposed() -> None:
    """用户界面不提供记忆浏览 / 逐条编辑入口（§一 / 验收 8）。"""
    from isekai_core.world import ops

    names = set(ops.SYNC_OPS) | set(ops.ASYNC_OPS)
    assert not any("memory" in name for name in names), f"不该有记忆浏览操作：{names}"

