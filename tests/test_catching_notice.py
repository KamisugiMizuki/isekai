"""会话层「世界还在追赶」通知以推进器记账为准（工单 W2）。

判据（§2.6 条 5，commit 099c3f9）：追赶与否只看推进器写入 clock 的
`catching_up` / `limited`（按批写、追平清 0），不看 `processed < target` 的投影——
持续运行的线在两个推进点之间总有未处理区间，投影口径会把追赶报成常态。

这些用例走真实回复链路（`SessionService._generate`，通知调用点 session.py:541 附近），
断言真实产物：message 行（outbound / notice）、session_notice 行与投递状态。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from isekai_core.config import load_config
from isekai_core.llm import FakeLLM
from isekai_core.session import SessionService
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


def _setup(store, world, *, rate: int | None = None, now_real: float | None = None,
           catching_up: int = 0, limited: int = 0) -> dict:
    """建一个可对话的真实实例 / 会话 / 通道绑定，并按需预置推进器记账。"""
    info, timeline_id, character_id = make_instance(store, world)
    now = time.time() if now_real is None else float(now_real)
    world.activate(info["id"], timeline_id, now_real=now)
    if rate is not None:
        world.set_rate(info["id"], timeline_id, rate=rate, now_real=now)
    session = store.session_ensure(info["id"], timeline_id, character_id)
    channel = store.channel_register(
        name="w2", display_name="w2", version="1", protocol="1", capabilities={}
    )[0]
    thread = store.thread_bind(channel["id"], "tier-dm", session["id"])
    clock = store.clock_get(timeline_id) or {}
    store.clock_put({**clock, "catching_up": int(catching_up), "limited": int(limited)})
    return {
        "info": info,
        "timeline_id": timeline_id,
        "character_id": character_id,
        "session": session,
        "channel": channel,
        "thread": thread,
        "now": now,
    }


def _service(store, tmp_path, world) -> tuple[SessionService, list[dict]]:
    delivered: list[dict] = []

    async def deliver(channel_id: str, thread_id: str, envelope: dict) -> bool:
        delivered.append(envelope)
        return True

    service = SessionService(
        store=store,
        cfg=load_config(tmp_path),
        llm=FakeLLM(["我在，慢慢说。"]),
        deliver=deliver,
        runtime=world,
    )
    return service, delivered


def _reply(service: SessionService, store, ctx: dict, *, env_id: str, text: str) -> None:
    """把一条用户消息走完真实回复链路（含 session.py:541 的追赶通知调用点）。"""
    row, created = store.inbound_put(
        session_id=str(ctx["session"]["id"]),
        channel_id=str(ctx["channel"]["id"]),
        thread_id=str(ctx["thread"]["thread_id"]),
        env_id=env_id,
        binding_version=int(ctx["thread"]["binding_version"]),
        text=text,
    )
    assert created, "测试用信封标识不该重复"
    asyncio.run(service._generate(row, batch=[row]))


def _notice_rows(store, session_id: str) -> list[dict]:
    rows = store.history_page(session_id, limit=50)["messages"]
    return [row for row in rows if str(row.get("role") or "") == "notice"]


def _set_accounting(store, timeline_id: str, *, catching_up: int, limited: int) -> None:
    store.clock_put({**(store.clock_get(timeline_id) or {}), "catching_up": catching_up, "limited": limited})


@pytest.mark.parametrize(
    "flags",
    [{"catching_up": 1, "limited": 0}, {"catching_up": 0, "limited": 1}],
    ids=["catching_up", "limited"],
)
def test_accounting_lag_sends_notice_once(store, world, tmp_path, flags) -> None:  # noqa: F811
    """记账落后（catching_up 或 limited）→ 每个追赶档只发一条 notice，投递真实发生。"""
    ctx = _setup(store, world, **flags)
    service, delivered = _service(store, tmp_path, world)
    session_id = str(ctx["session"]["id"])

    _reply(service, store, ctx, env_id="e-1", text="在吗")

    notices = _notice_rows(store, session_id)
    assert len(notices) == 1, f"记账落后应发一条通知，实际 {len(notices)}"
    notice = store.session_notice_get(session_id, "catching_up")
    assert notice is not None and notice["message_id"], "catching_up 通知未登记"
    assert str(notice["message_id"]) == str(notices[0]["message_id"]), "登记与 outbound 行对不上"
    assert "世界还在追赶" in store.message_text(notices[0]), "通知正文不对"
    assert store.delivery_rollup(int(notices[0]["seq"])) == "sent", "通知没有实际投递"
    assert any(
        env["type"] == "system_notice" and str(env["payload"]["message_id"]) == str(notice["message_id"])
        for env in delivered
    ), "通道端没收到 system_notice"

    # 同一追赶档再走一条：不重复发
    _reply(service, store, ctx, env_id="e-2", text="还在吗")
    assert len(_notice_rows(store, session_id)) == 1, "同一追赶档重复发通知"
    assert str(store.session_notice_get(session_id, "catching_up")["message_id"]) == str(
        notice["message_id"]
    )


def test_projection_lag_without_accounting_sends_nothing(store, world, tmp_path) -> None:  # noqa: F811
    """仅投影落后（processed < target）而记账为 0 → 不发通知（修误报的核心）。"""
    ctx = _setup(store, world, rate=60, now_real=time.time() - 5.0)
    service, delivered = _service(store, tmp_path, world)
    timeline_id = str(ctx["timeline_id"])
    session_id = str(ctx["session"]["id"])

    # 前提：投影确实领先于已完成水位，而推进器记账仍是 0（这正是旧口径误报的场景）
    view = world.view(str(ctx["info"]["id"]), timeline_id, now_real=time.time())
    clock = store.clock_get(timeline_id) or {}
    assert view["world_seconds"] > view["processed_world"], f"投影没有落后：{view}"
    assert int(clock["catching_up"]) == 0 and int(clock["limited"]) == 0

    _reply(service, store, ctx, env_id="e-1", text="现在是什么时辰？")

    assert _notice_rows(store, session_id) == [], "投影落后不该发追赶通知"
    assert store.session_notice_get(session_id, "catching_up") is None
    assert not any(env["type"] == "system_notice" for env in delivered)


def test_catch_up_clears_notice_and_allows_the_next_one(store, world, tmp_path) -> None:  # noqa: F811
    """追平（记账清 0）后处理消息 → 登记被清除；再次落后可再发一条新的。"""
    ctx = _setup(store, world, catching_up=1, limited=0)
    service, _delivered = _service(store, tmp_path, world)
    timeline_id = str(ctx["timeline_id"])
    session_id = str(ctx["session"]["id"])

    _reply(service, store, ctx, env_id="e-1", text="在吗")
    first = store.session_notice_get(session_id, "catching_up")
    assert first is not None and len(_notice_rows(store, session_id)) == 1

    # 追平：推进器记账清 0，随后一条消息应把登记清掉，且不再发通知
    _set_accounting(store, timeline_id, catching_up=0, limited=0)
    _reply(service, store, ctx, env_id="e-2", text="追上了吗")
    assert store.session_notice_get(session_id, "catching_up") is None, "追平后通知登记没有被清除"
    assert len(_notice_rows(store, session_id)) == 1, "追平这一轮不该再发通知"

    # 再次落后：新的追赶档可以再发一条（新 message_id）
    _set_accounting(store, timeline_id, catching_up=1, limited=0)
    _reply(service, store, ctx, env_id="e-3", text="又落后了")
    second = store.session_notice_get(session_id, "catching_up")
    assert second is not None and str(second["message_id"]) != str(first["message_id"])
    assert len(_notice_rows(store, session_id)) == 2
