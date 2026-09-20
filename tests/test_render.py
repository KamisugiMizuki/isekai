"""LLM 文本表述（§3.2）与惰性展开（§3.4）、调用账本（§2.8）——真实链路自测。"""

from __future__ import annotations

import json
import time

import pytest

from isekai_core.runtime import render
from isekai_core.runtime.service import RuntimeService
from isekai_core.ump import UmpError
from isekai_core.world import ops as world_ops
from samples import DAY, sample_card, sample_package
from isekai_core.config import load_config
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


class FakeRenderLLM:
    """按脚本回答：可以返回不合规文本，用来验校验与回退。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, *, max_tokens=None, timeout=None, temperature=None) -> str:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]


def _prepared(store, world_service, *, moment=DAY * 1500):
    info, timeline_id, character_id = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 6 * DAY)
    events = [
        item
        for item in store.event_window(info["id"], timeline_id, until=10**12, limit=200)
        if item["source"] == "engine"
    ]
    assert events, "推进后要有世界级事件"
    return info, timeline_id, character_id, events[0]


async def test_render_writes_text_but_never_facts(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, character_id, event = _prepared(store, world)
    skeleton = str(event["summary"])
    detail = f"（信报抄存）{skeleton}"
    llm = FakeRenderLLM([json.dumps({"detail": detail, "claims": {"src-1": f"驿站传：{skeleton}"}}, ensure_ascii=False)])
    result = await world_ops.dispatch_async(
        cfg,
        llm,
        "event.render",
        {"instance_id": info["id"], "timeline_id": timeline_id, "event_id": event["id"]},
        store=store,
    )
    assert result["text_source"] == "llm" and result["detail"] == detail
    saved = store.event_get(info["id"], timeline_id, event["id"])
    assert saved["detail"] == detail and saved["text_source"] == "llm"
    assert saved["summary"] == skeleton, "骨架不被语言产物改写"
    claims = store.claim_list(info["id"], timeline_id, event_id=event["id"])
    assert any(str(item["text"]).startswith("驿站传：") for item in claims), "说法文本一并固化"
    # 骨架里没有数字时，渲染后还会花一次便宜调用做忠实度判断（§3.2 第二道护栏），账本如实记 2 次
    assert result["budget"]["calls"] == 2

    # 第二次调用复用已固化文本，不重新生成（§3.2）
    again = await world_ops.dispatch_async(
        cfg,
        llm,
        "event.render",
        {"instance_id": info["id"], "timeline_id": timeline_id, "event_id": event["id"]},
        store=store,
    )
    # 只有第一次渲染花了两步（表述 + 忠实度判断）；复用不再调用模型
    assert again.get("reused") is True and len(llm.calls) == 2


async def test_render_rejects_facts_changed_by_language(store, world, tmp_path) -> None:
    """模型改了数字（骨架外的事实）→ 不给过，退回模板（附录 B #4）。"""
    cfg = load_config(tmp_path)
    info, timeline_id, _ = make_instance(store, world)
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    event_id = "ev-num-test"
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(store.clock_get(timeline_id)["generation"]),
        processed_world=watermark,
        catching_up=False,
        events=[
            {
                "id": event_id,
                "instance_id": info["id"],
                "timeline_id": timeline_id,
                "world_seconds": watermark,
                "seq": 1,
                "kind": "world",
                "family": "ef-1",
                "template": "et-num",
                "source": "engine",
                "summary": "堤长身故，享年 34",
                "detail": "堤长身故，享年 34",
                "text_source": "template",
                "effects": "[]",
                "share_value": 0,
                "importance": 0.6,
                "created_real": 0.0,
            }
        ],
    )
    event = {"id": event_id}
    bad = json.dumps({"detail": "堤长身故，享年 12 岁", "claims": {}}, ensure_ascii=False)
    llm = FakeRenderLLM([bad, bad])
    result = await world_ops.dispatch_async(
        cfg,
        llm,
        "event.render",
        {"instance_id": info["id"], "timeline_id": timeline_id, "event_id": event["id"]},
        store=store,
    )
    assert result["text_source"] == "template", "不合规表述不落盘"
    assert "未过校验" in result["note"]
    saved = store.event_get(info["id"], timeline_id, event["id"])
    assert saved["text_source"] == "template"
    assert len(llm.calls) == 2, "有界重试两次即止"


async def test_render_budget_stops_calls(store, world, tmp_path) -> None:
    """单任务预算耗尽 → 停止新调用并说明，不重复烧调用（§2.8）。"""
    cfg = load_config(tmp_path)
    info, timeline_id, _, event = _prepared(store, world)
    bucket = int(time.time() // 86400)
    store.call_ledger_add(info["id"], timeline_id, "event_render", bucket=bucket, calls=cfg.runtime.render_calls_per_day)
    llm = FakeRenderLLM([json.dumps({"detail": "不该被调用"}, ensure_ascii=False)])
    result = await world_ops.dispatch_async(
        cfg,
        llm,
        "event.render",
        {"instance_id": info["id"], "timeline_id": timeline_id, "event_id": event["id"]},
        store=store,
    )
    assert result["budget"]["paused"] is True and llm.calls == [], "预算耗尽即不再调用"
    assert result["text_source"] == "template"


async def test_expand_only_for_known_claims_and_is_derived(store, world, tmp_path) -> None:
    """惰性展开：只展开既定内容、产出派生记录、没接触过的角色不给（§3.4）。"""
    cfg = load_config(tmp_path)
    info, timeline_id, character_id, event = _prepared(store, world)
    claims = [
        item
        for item in store.claim_list(info["id"], timeline_id, event_id=event["id"])
        if character_id in store.knowledge_holders(info["id"], timeline_id, str(item["id"]))
    ]
    assert claims, "她得先掌握这条记载"
    claim = claims[0]

    outsider = "cc-outsider"
    with pytest.raises(UmpError):
        await world_ops.dispatch_async(
            cfg,
            FakeRenderLLM(["随便写点什么"]),
            "event.expand",
            {"instance_id": info["id"], "timeline_id": timeline_id, "claim_id": claim["id"], "character_id": outsider},
            store=store,
        )

    expansion = "据驿站抄存，这一段只记了拖延与排队，其余不载。"
    llm = FakeRenderLLM([expansion])
    result = await world_ops.dispatch_async(
        cfg,
        llm,
        "event.expand",
        {
            "instance_id": info["id"],
            "timeline_id": timeline_id,
            "claim_id": claim["id"],
            "character_id": character_id,
            "question": "当时还记了什么？",
        },
        store=store,
    )
    assert result["text"] == expansion and result["derived"]
    derived = store.claim_derived(info["id"], timeline_id, claim["id"])
    assert derived is not None and derived["derived_from"] == claim["id"], "派生记录关联原条目"
    original_now = [item for item in store.claim_list(info["id"], timeline_id) if item["id"] == claim["id"]][0]
    assert original_now["text"] == claim["text"], "原记录不被改写"
    # 同一传本复用已采纳的展开（不按角色各写一份）
    again = await world_ops.dispatch_async(
        cfg,
        FakeRenderLLM(["另一份不一样的说法"]),
        "event.expand",
        {"instance_id": info["id"], "timeline_id": timeline_id, "claim_id": claim["id"], "character_id": character_id},
        store=store,
    )
    # 只有第一次渲染花了两步（表述 + 忠实度判断）；复用不再调用模型
    assert again.get("reused") is True and len(llm.calls) == 2


async def test_expand_drops_ungrounded_output(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, character_id, event = _prepared(store, world)
    claim = store.claim_list(info["id"], timeline_id, event_id=event["id"])[0]
    result = await world_ops.dispatch_async(
        cfg,
        FakeRenderLLM(["碑上还记着 300 年前的第二任堤长名字。"]),
        "event.expand",
        {"instance_id": info["id"], "timeline_id": timeline_id, "claim_id": claim["id"]},
        store=store,
    )
    assert result["text"] == "" and "保留不知道" in result["note"]
    assert store.claim_derived(info["id"], timeline_id, claim["id"]) is None, "不合规展开不落盘"


def test_facts_preserved_helper() -> None:
    assert render.facts_preserved("享年 34 岁", "堤长身故，享年 34")
    assert not render.facts_preserved("享年 12 岁", "堤长身故，享年 34")
    assert not render.facts_preserved("享年 34 岁，另有 5 人", "堤长身故，享年 34")
    assert render.facts_preserved("没有任何数字", "也没有数字")


def test_parse_render_tolerates_fences() -> None:
    text = '```json\n{"detail": "堤长身故", "claims": {"src-1": "传闻如此"}}\n```'
    parsed = render.parse_render(text, [{"source_id": "src-1"}, {"source_id": "src-2"}])
    assert parsed and parsed["detail"] == "堤长身故"
    assert parsed["claims"] == {"src-1": "传闻如此"}, "对不上的来源不算产出"
    assert render.parse_render("不是 JSON", []) is None
