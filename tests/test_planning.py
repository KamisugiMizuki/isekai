"""角色自主生成打算（§11.3 / EVENT_ENGINE_SPEC §六）：闭集、可知目标、预算、失败不留痕。"""

from __future__ import annotations

import asyncio
import json

from isekai_core.runtime import planning
from isekai_core.runtime.service import RuntimeService
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


class FakePlanLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, *, max_tokens=None, timeout=None, temperature=None) -> str:
        self.calls.append(messages)
        if not self.replies:
            raise RuntimeError("模型不可用")
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


def _ready(store, world_service, *, moment=DAY * 1500):
    info, timeline_id, character_id = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 3 * DAY)
    return info, timeline_id, character_id


def _proposal(**over) -> str:
    payload = {
        "object": "把北堤缺口的临时路条先核发给滩户",
        "basis": "她读到信报说通行牌停发，自己手上还有一批未核发的路条",
        "strength": 0.7,
        "action_kind": "public_notice",
        "target": "src-1",
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False)


def test_parse_accepts_only_closed_set_and_known_targets() -> None:
    """类型必须在闭集、目标必须在她可知集合里、动机不能空（§11.3）。"""
    allowed = {"public_notice": ["src-1"], "activity_constraint": ["海堤"]}
    assert planning.parse(_proposal(), allowed) is not None
    assert planning.parse(_proposal(action_kind="world_melt"), allowed) is None
    assert planning.parse(_proposal(target="src-9"), allowed) is None, "指向她不可知的目标=替她生成目标"
    assert planning.parse(_proposal(basis=""), allowed) is None
    assert planning.parse('{"object": ""}', allowed) is None, "她自己不动手是合法结果，不产生打算"
    assert planning.parse("模型今天不想输出 JSON", allowed) is None


def test_allowed_targets_come_from_her_own_kit() -> None:
    """可知目标来自她自己的角色/地区/渠道/组织，不是整包（§六）。"""
    package = sample_package()
    card = sample_card(package)
    allowed = planning.allowed_targets(package, card, knowledge=[], observations=[])
    assert allowed["public_notice"] == ["src-1"], "她掌握的渠道"
    assert allowed["activity_constraint"], "自己的角色与地区"
    assert "et-1" not in sum(allowed.values(), []), "世界事件模板不是她的目标"


def test_proposal_lands_as_intent_and_records_basis(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = _ready(store, world_service)
    llm = FakePlanLLM([_proposal()])
    result = asyncio.run(
        world_service.propose_intents(info["id"], timeline_id, llm=llm, now_real=1.7e9 + 4 * DAY)
    )
    assert result["proposed"] == 1 and len(llm.calls) == 1
    rows = [row for row in store.intent_list(info["id"], timeline_id, character_id) if row["id"].startswith("in-auto-")]
    assert len(rows) == 1
    assert rows[0]["stage"] == "adopted" and rows[0]["basis"]
    effect = json.loads(rows[0]["effect"])
    assert effect["kind"] == "public_notice" and effect["target"] == "src-1"
    assert "她已知的消息" in llm.calls[0][0]["content"], "提案提示带上她已知的东西"


def test_bad_proposals_leave_nothing_behind(store) -> None:
    """越出闭集或指向不可知目标 → 不落盘；模型说不动手 → 也不落盘。"""
    world_service = RuntimeService(store)
    info, timeline_id, character_id = _ready(store, world_service)
    llm = FakePlanLLM([_proposal(action_kind="source_delay"), _proposal(target="et-1"), '{"object": ""}'])
    for step in range(3):
        asyncio.run(
            world_service.propose_intents(
                info["id"], timeline_id, llm=llm, now_real=1.7e9 + (4 + step) * DAY
            )
        )
    auto = [row for row in store.intent_list(info["id"], timeline_id, character_id) if row["id"].startswith("in-auto-")]
    assert auto == [], "拒掉的结果不落盘"
    assert len(llm.calls) == 3, "拒掉的是结果，不是调用（预算照记）"


def test_model_failure_does_not_break_the_world(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = _ready(store, world_service)
    llm = FakePlanLLM([])
    result = asyncio.run(
        world_service.propose_intents(info["id"], timeline_id, llm=llm, now_real=1.7e9 + 4 * DAY)
    )
    assert result["proposed"] == 0
    assert [row for row in store.intent_list(info["id"], timeline_id, character_id) if row["id"].startswith("in-auto-")] == []
    assert result["budget"]["calls"] == 1, "失败的调用也记账（§2.8）"


def test_budget_and_live_intent_caps(store) -> None:
    """现实日预算用尽就不再调用；未竟之事满了不再提案（§2.8 / §11.3）。"""
    world_service = RuntimeService(store, render_calls_per_day=1)
    info, timeline_id, character_id = _ready(store, world_service)
    llm = FakePlanLLM([_proposal(), _proposal(object="第二件")])
    first = asyncio.run(
        world_service.propose_intents(info["id"], timeline_id, llm=llm, now_real=1.7e9 + 4 * DAY)
    )
    second = asyncio.run(
        world_service.propose_intents(info["id"], timeline_id, llm=llm, now_real=1.7e9 + 5 * DAY)
    )
    assert first["proposed"] == 1 and second["proposed"] == 0
    assert len(llm.calls) == 1, "预算用尽后不再调用模型"

    world_service2 = RuntimeService(store)
    info2, timeline2, character2 = _ready(store, world_service2, moment=DAY * 2600)
    for index in range(planning.MAX_LIVE_INTENTS):
        store.apply_runtime_batch(
            timeline_id=timeline2,
            generation=int(store.clock_get(timeline2)["generation"]),
            processed_world=int(store.clock_get(timeline2)["processed_world"]),
            catching_up=False,
            intents=[
                {
                    "id": f"in-manual-{index}",
                    "instance_id": info2["id"],
                    "timeline_id": timeline2,
                    "character_id": character2,
                    "object": f"手头的事{index}",
                    "basis": "既有",
                    "strength": 0.5,
                    "window_from": 0,
                    "window_to": DAY * 3000,
                    "preconditions": "[]",
                    "effect": json.dumps({"kind": "public_notice", "target": "src-1"}),
                    "stage": "adopted",
                    "note": "",
                    "source_world": 0,
                    "updated_world": 0,
                }
            ],
        )
    llm2 = FakePlanLLM([_proposal()])
    result = asyncio.run(
        world_service2.propose_intents(info2["id"], timeline2, llm=llm2, now_real=1.7e9 + 9 * DAY)
    )
    assert result["proposed"] == 0 and llm2.calls == [], "未竟之事已满，不再想新事"


def test_frozen_timeline_never_proposes(store) -> None:
    world_service = RuntimeService(store)
    info, timeline_id, _ = _ready(store, world_service)
    world_service.freeze(info["id"], timeline_id, now_real=1.7e9 + 3 * DAY)
    llm = FakePlanLLM([_proposal()])
    result = asyncio.run(
        world_service.propose_intents(info["id"], timeline_id, llm=llm, now_real=1.7e9 + 4 * DAY)
    )
    assert result == {"proposed": 0, "reason": "frozen"} and llm.calls == []


class AdvancingLLM(FakePlanLLM):
    """提案调用期间世界继续推进（真机踩到过的竞态）：写入必须按最新水位与世代。"""

    def __init__(self, replies, world_service, info, timeline_id, now_real) -> None:
        super().__init__(replies)
        self.world_service = world_service
        self.info = info
        self.timeline_id = timeline_id
        self.now_real = now_real

    async def chat(self, messages, *, max_tokens=None, timeout=None, temperature=None) -> str:
        text = await super().chat(messages, max_tokens=max_tokens, timeout=timeout, temperature=temperature)
        self.world_service.advance(self.info["id"], self.timeline_id, now_real=self.now_real)
        self.world_service.freeze(self.info["id"], self.timeline_id, now_real=self.now_real)
        self.world_service.activate(self.info["id"], self.timeline_id, now_real=self.now_real)  # 世代+1
        return text


def test_concurrent_advance_does_not_fake_a_proposal(store) -> None:
    """提案期间水位与世代变了：要么按最新世代落盘，要么如实不报——不能报成功却没写（真机踩过）。"""
    world_service = RuntimeService(store)
    info, timeline_id, character_id = _ready(store, world_service)
    llm = AdvancingLLM([_proposal()], world_service, info, timeline_id, 1.7e9 + 4 * DAY)
    result = asyncio.run(
        world_service.propose_intents(info["id"], timeline_id, llm=llm, now_real=1.7e9 + 4 * DAY)
    )
    auto = [row for row in store.intent_list(info["id"], timeline_id, character_id) if row["id"].startswith("in-auto-")]
    assert result["proposed"] == len(auto), "报的条数必须与库里一致"
    if auto:
        watermark = int(store.clock_get(timeline_id)["processed_world"])
        assert auto[0]["source_world"] <= watermark, "按提交时的水位记账"
