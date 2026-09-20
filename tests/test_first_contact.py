"""初见（SESSION_CORE_SPEC §5.6）行为验收：一次性开场、reply_to=null、不占配额、不改事实。"""

from __future__ import annotations

import asyncio
import time

from isekai_core.runtime.service import RuntimeService
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


class ScriptedLLM:
    def __init__(self, text: str = "正好你在——风向变了，我这边也换了时辰。") -> None:
        self.text = text
        self.calls = 0

    async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return self.text


def _ready(store, world) -> tuple[str, str, str]:
    service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, service)
    service.activate(info["id"], timeline_id, now_real=time.time())
    channel = store.channel_register(
        name="builtin", display_name="b", version="0", protocol="1", capabilities={}
    )[0]
    session = store.session_ensure(info["id"], timeline_id, character_id)
    store.thread_bind(channel["id"], "view-1", session["id"])
    return info["id"], timeline_id, character_id


def test_first_contact_is_one_shot_and_does_not_touch_facts(store, world) -> None:
    """开场只发生一次；不改变事实、获知与主动配额。"""
    instance_id, timeline_id, character_id = _ready(store, world)
    service = RuntimeService(store)
    before_events = len(store.event_window(instance_id, timeline_id, until=10**12, limit=500))
    before_knowledge = store.knowledge_window(
        instance_id, timeline_id, character_id, until=10**12, limit=100
    )

    llm = ScriptedLLM()
    first = asyncio.run(
        service.first_contact(
            instance_id, timeline_id, character_id, channel_id="builtin", thread_id="view-1", llm=llm
        )
    )
    assert first["spoken"] is True and llm.calls == 1

    row = store.outbound_by_message_id(str(first["message_id"]))
    assert row is not None
    assert row["reply_to"] is None, "开场是主动消息：reply_to=null"
    assert row["channel_id"] == "builtin" and row["thread_id"] == "view-1", "只投向触发视图的 thread"
    # 令牌不落在消息行上（既有投递路径按 thread 取），这里只确认目标被固定住
    assert row["binding_version"] is not None, "固化时固定该 thread 的绑定版本"

    # 二次调用：复用既有开场，不重新生成
    again = asyncio.run(
        service.first_contact(
            instance_id, timeline_id, character_id, channel_id="builtin", thread_id="view-1", llm=llm
        )
    )
    assert again.get("reused") is True and llm.calls == 1, again

    assert len(store.event_window(instance_id, timeline_id, until=10**12, limit=500)) == before_events
    assert len(
        store.knowledge_window(instance_id, timeline_id, character_id, until=10**12, limit=100)
    ) == len(before_knowledge)
    assert store.proactive_list(instance_id, timeline_id) == [], "开场不占世界源主动配额"


def test_first_contact_without_material_still_speaks_but_invents_nothing(store, world) -> None:
    """无可用素材时照实说（只打招呼），不编造时间进展。"""
    instance_id, timeline_id, character_id = _ready(store, world)
    service = RuntimeService(store)
    card = service.card_of(store.instance_get(instance_id), character_id, timeline_id=timeline_id,
                           world_seconds=int(service.clock_row(timeline_id)["processed_world"]))
    snapshot = service.character_snapshot(
        instance_id, timeline_id, character_id,
        world_seconds=int(service.clock_row(timeline_id)["processed_world"]),
    )
    prompt = service._first_contact_prompt(card, {**snapshot, "knowledge": []})
    body = " ".join(str(item.get("content") or "") for item in prompt)
    assert "没有就只打个招呼" in body and "不要假装刚做完什么大事" in body

    # 生成失败不留半成品、不消费一次资格
    class Broken:
        async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
            return ""

    result = asyncio.run(
        service.first_contact(
            instance_id, timeline_id, character_id, channel_id="builtin", thread_id="view-1", llm=Broken()
        )
    )
    assert result["spoken"] is False
    assert store.first_contact_get(str(store.session_ensure(instance_id, timeline_id, character_id)["id"])) is None

    # 之后仍可以正常开场
    ok = asyncio.run(
        service.first_contact(
            instance_id, timeline_id, character_id, channel_id="builtin", thread_id="view-1",
            llm=ScriptedLLM("在的。"),
        )
    )
    assert ok["spoken"] is True
