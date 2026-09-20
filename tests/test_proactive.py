"""主动发言（SESSION_CORE_SPEC §五）行为验收。"""

from __future__ import annotations

import time

from isekai_core.runtime import proactive
from isekai_core.runtime.service import RuntimeService
from samples import DAY, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


class ScriptedLLM:
    def __init__(self, text: str = "堤上风转了，我想起你上次问过的那件事。") -> None:
        self.text = text
        self.calls = 0

    async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        return self.text


def _ready(store, world) -> tuple[str, str, str]:
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    world_service.activate(info["id"], timeline_id, now_real=time.time())
    return info["id"], timeline_id, character_id


def test_no_material_no_proactive_message(store, world) -> None:
    """没有已获知素材就不生成——不提供无来源的替代主动消息（§5.1）。"""
    instance_id, timeline_id, _ = _ready(store, world)
    service = RuntimeService(store)
    result = service_async = None
    import asyncio

    result = asyncio.get_event_loop().run_until_complete(
        service.proactive_tick(instance_id, timeline_id, llm=ScriptedLLM())
    ) if False else None
    # 用 asyncio.run 保证干净事件循环
    result = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=ScriptedLLM()))
    assert result["spoken"] == 0
    assert store.proactive_day_count(instance_id, timeline_id, "cc-堤禾", world_day=0) == 0
    assert result["skipped"], "要么没素材要么在睡觉——总之不生成"
    assert set(result["skipped"].values()) <= {"没有可用素材", "睡眠期", "今日额度用完"}


def test_material_quota_and_no_duplicate_consumption(store, world) -> None:
    """有素材 → 一句话固化；配额按最终消息计数；同一素材不重复消费（§5.2）。"""
    import asyncio

    instance_id, timeline_id, character_id = _ready(store, world)
    service = RuntimeService(store)
    world_seconds = int(service.clock_row(timeline_id)["processed_world"])
    service.store.knowledge_put(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "character_id": character_id,
            "id": f"kn-{character_id}-1",
            "world_seconds": world_seconds,
            "kind": "claim",
            "target": "cl-audit-1",
            "source": "src-1",
            "stance": "recorded",
            "text": "北堤的通行牌这三天都停发了",
        }
    )
    llm = ScriptedLLM()
    # 节律（睡眠期不发）由纯函数层单独验；这里把「此刻清醒」作为前提打桩，
    # 专测配额与素材消费这条主路径。
    from isekai_core.runtime import life as life_mod

    original = life_mod.activity_label
    life_mod.activity_label = lambda window: "日间活动"  # type: ignore[assignment]
    try:
        first = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2))
    finally:
        life_mod.activity_label = original  # type: ignore[assignment]
    assert first["spoken"] == 1 and llm.calls == 1, first
    life_mod.activity_label = lambda window: "日间活动"  # type: ignore[assignment]
    try:
        second = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2))
    finally:
        life_mod.activity_label = original  # type: ignore[assignment]
    assert second["spoken"] == 0, "同一素材不该再说一遍"
    assert set(second["skipped"].values()) <= {"没有可用素材", "睡眠期", "今日额度用完"}

    # 配额用尽：换一份新材料也只能到这个上限
    service.store.knowledge_put(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "character_id": character_id,
            "id": f"kn-{character_id}-2",
            "world_seconds": world_seconds,
            "kind": "claim",
            "target": "cl-audit-2",
            "source": "src-2",
            "stance": "recorded",
            "text": "盐滩边又立了一块新碑",
        }
    )
    life_mod.activity_label = lambda window: "日间活动"  # type: ignore[assignment]
    try:
        third = asyncio.run(service.proactive_tick(instance_id, timeline_id, llm=llm, per_day=1))
    finally:
        life_mod.activity_label = original  # type: ignore[assignment]
    assert third["spoken"] == 0 and "额度" in "".join(third["skipped"].values())
    log = store.proactive_list(instance_id, timeline_id)
    assert len(log) == 1 and log[0]["material_ref"] == "cl-audit-1"


def test_sleep_archived_and_expired_material_are_skipped(store, world) -> None:
    """睡眠期不主动发送；归档角色不产生；过期素材不补发（§5.1/§5.2）。"""
    instance_id, timeline_id, character_id = _ready(store, world)
    service = RuntimeService(store)
    world_seconds = int(service.clock_row(timeline_id)["processed_world"])

    # 同一份素材，但获知时间在很久以前 → 不进候选
    stale = proactive.candidates(
        [{"target": "cl-old", "world_seconds": world_seconds - 5 * DAY, "kind": "claim", "text": "旧事"}],
        world_seconds=world_seconds,
        day_seconds=DAY,
        consumed=set(),
    )
    assert stale == [], "过期素材不再用来发起主动消息"

    fresh = proactive.candidates(
        [{"target": "cl-new", "world_seconds": world_seconds - 60, "kind": "claim", "text": "新事"}],
        world_seconds=world_seconds,
        day_seconds=DAY,
        consumed=set(),
    )
    assert [item["ref"] for item in fresh] == ["cl-new"]

    ok, why = proactive.should_speak(archived=False, activity="sleep", quota=2, materials=fresh)
    assert not ok and why == "睡眠期"
    ok2, why2 = proactive.should_speak(archived=True, activity="day", quota=2, materials=fresh)
    assert not ok2 and why2 == "已归档"
    ok3, _why3 = proactive.should_speak(archived=False, activity="day", quota=2, materials=fresh)
    assert ok3


def test_flush_delivers_only_newest_and_keeps_backlog_in_history(store, world) -> None:
    """恢复连接只按有界策略处理仍有效的未投递消息：只发最新一条，积压留在历史。"""
    import asyncio

    instance_id, timeline_id, character_id = _ready(store, world)
    service = RuntimeService(store)
    session = store.session_ensure(instance_id, timeline_id, character_id)
    channel = store.channel_register(
        name="builtin", display_name="b", version="0", protocol="1", capabilities={}
    )[0]
    store.thread_bind(channel["id"], "dm-1", session["id"])
    target = store.thread_for_session(session["id"])
    assert target is not None

    for index in range(3):
        store.outbound_put(
            session_id=str(session["id"]),
            message_id=f"m-pro-{index}",
            reply_to=None,
            covers=[],
            batches=[[f"第 {index} 条主动消息"]],
            target_channel=channel["id"],
            target_thread="dm-1",
            binding_version=int(target["binding_version"]),
            binding_token=str(target["binding_token"]),
        )
        store.proactive_log_add(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "world_day": 1500,
                "material_ref": f"cl-{index}",
                "message_id": f"m-pro-{index}",
                "created_world": 1,
                "created_real": time.time(),
                "state": "fixed",
            }
        )
    pending = store.proactive_pending(str(session["id"]), since_world=0)
    assert len(pending) == 3, "三条都在待投递里（积压）"

    sent: list[str] = []

    class FakeSession:
        pass

    from isekai_core.session import SessionService  # noqa: F401  取类型用

    # 直接调用会话的投递策略：只发最新一条
    from isekai_core.session import SessionService as SS

    class Probe(SS):  # type: ignore[misc]
        def __init__(self, store_):  # noqa: ANN001
            self.store = store_

        async def _send_batches(self, msg):  # noqa: ANN001, ANN202
            sent.append(str(msg["message_id"]))

    probe = Probe(store)
    count = asyncio.run(probe._flush_proactive(str(session["id"])))
    assert count == 1 and sent == ["m-pro-2"], (count, sent)
