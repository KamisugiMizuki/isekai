"""通道容量与限速（CHANNEL_PLUGIN_SPEC §3.2 / §六）：真核心 + 真 WS + 真 SQLite，只换 LLM。

对应 CHANNEL_PROTOCOL_APPENDIX 里原先记「未实现」的四项：入站队列上限、在线连接数上限、
速率限制、`binding.state=revoked` / `status.state=interrupted` 的产出方。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect as ws_connect

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump


def use_core(tmp_path, **values) -> None:
    """把 core 段参数写进临时根配置（只写要覆盖的键，其余走默认）。"""
    folder = tmp_path / "config"
    folder.mkdir(parents=True, exist_ok=True)
    body = "core:\n" + "".join(f"  {key}: {value}\n" for key, value in values.items())
    (folder / "config.yaml").write_text(body, encoding="utf-8")


def _hello(channel_id: str, credential: str) -> str:
    return json.dumps(
        ump.make(
            "hello",
            {
                "channel": {"id": channel_id, "name": channel_id, "version": "0.1.0"},
                "capabilities": {"segments": True, "status": True},
                "auth": {"credential": credential},
            },
        ),
        ensure_ascii=False,
    )


async def _until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


async def test_connection_limit_refuses_new_and_keeps_online(tmp_path):
    """在线连接数上限：第 N+1 条连接拿到 overloaded（可重试）并被关；在线那条照常收发。"""
    use_core(tmp_path, max_connections=1)
    async with running_core(tmp_path, replies=["在的。"]) as h:
        mgmt = await open_mgmt(h)
        first, bound = await bind_thread(h, mgmt, channel_id="a", thread_id="dm-1")
        issued = await mgmt.call("channel.ensure", name="b")
        raw = await ws_connect(h.endpoint)
        try:
            await raw.send(_hello("b", issued["credential"]))
            refused = json.loads(await asyncio.wait_for(raw.recv(), timeout=5.0))
            assert refused["type"] == "error", refused
            assert refused["payload"]["code"] == ump.Err.OVERLOADED
            assert refused["payload"]["retryable"] is True
            with pytest.raises(Exception):  # 服务端随后关闭（1013 不在断言里，关闭这件事本身要能观察到）
                await asyncio.wait_for(raw.recv(), timeout=5.0)
        finally:
            await raw.close()

        await first.send_user_message(
            thread_id="dm-1", binding_token=bound["thread"]["binding_token"], text="在吗"
        )
        reply = await first.expect(lambda env: env.type == "reply", timeout=15.0)
        assert reply.payload["message_id"]


async def test_rate_limit_answers_retryable_then_closes_the_flooder(tmp_path):
    """速率限制：超限帧回 rate_limited（可重试）且不处理；持续超限只断开这条连接。"""
    use_core(tmp_path, rate_limit_msgs=3, rate_limit_window_s=30.0)
    async with running_core(tmp_path, replies=["嗯。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = bound["thread"]["binding_token"]
        channel_instance = client.hello_ack["channel_instance"]
        for index in range(3):  # 窗口内前三帧正常
            await client.send_user_message(thread_id="dm-1", binding_token=token, text=f"第{index}条")
        await client.expect(lambda env: env.type == "accepted", timeout=10.0)

        for index in range(3):  # 第 4–6 帧：超限但还没到断线阈值
            await client.send_user_message(thread_id="dm-1", binding_token=token, text=f"灌{index}")
        limited = [await client.expect(lambda env: env.type == "error", timeout=10.0) for _ in range(3)]
        assert [item.payload["code"] for item in limited] == [ump.Err.RATE_LIMITED] * 3
        assert all(item.payload["retryable"] is True for item in limited)
        assert channel_instance in h.runtime.server.connected_channels()

        await client.send_user_message(thread_id="dm-1", binding_token=token, text="再来一条")
        assert await _until(lambda: channel_instance not in h.runtime.server.connected_channels()), (
            "持续超限应该断开这条连接："
            f"connected={h.runtime.server.connected_channels()}"
        )
        # 只影响它自己：核心与别的连接还在服务
        other, other_bound = await bind_thread(h, mgmt, channel_id="other", thread_id="dm-2")
        await other.send_user_message(
            thread_id="dm-2", binding_token=other_bound["thread"]["binding_token"], text="我还在"
        )
        assert (await other.expect(lambda env: env.type == "reply", timeout=15.0)).payload["message_id"]


async def test_inbound_queue_cap_rejects_new_until_drained(tmp_path):
    """入站队列上限：排队满了拒新输入（overloaded/可重试），已接受的不丢；排干后恢复接收。"""
    use_core(tmp_path, max_queued_inbound=2)
    async with running_core(tmp_path, replies=["好。"]) as h:
        gate = asyncio.Event()
        original = h.fake.chat

        async def blocked(messages, **kwargs):
            await gate.wait()
            return await original(messages, **kwargs)

        h.fake.chat = blocked  # 第一条卡在生成里，后面的才会在队列里排队
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = bound["thread"]["binding_token"]
        session_id = bound["session"]["id"]
        channel_instance = client.hello_ack["channel_instance"]

        first_env = await client.send_user_message(thread_id="dm-1", binding_token=token, text="第一条")
        # 等它真的进入处理（不是「还没被接受所以队列为空」这种假通过）
        assert await _until(
            lambda: (h.store.inbound_find(channel_instance, "dm-1", first_env) or {}).get("state") == "processing",
            timeout=15.0,
        ), "第一条应已进入处理"

        await client.send_user_message(thread_id="dm-1", binding_token=token, text="第二条")
        await client.send_user_message(thread_id="dm-1", binding_token=token, text="第三条")
        assert await _until(lambda: h.store.inbound_queued_count(session_id) == 2, timeout=15.0), "队列应已排满"

        await client.send_user_message(thread_id="dm-1", binding_token=token, text="第四条")
        overflow = await client.expect(lambda env: env.type == "error", timeout=10.0)
        assert overflow.payload["code"] == ump.Err.OVERLOADED
        assert overflow.payload["retryable"] is True

        gate.set()
        replies = 0
        while replies < 1:
            envelope = await client.expect(
                lambda env: env.type in ("reply", "status", "error"), timeout=20.0
            )
            if envelope.type == "reply":
                replies += 1
        assert await _until(lambda: h.store.inbound_queued_count(session_id) == 0, timeout=20.0)
        # 排干后又能接
        await client.send_user_message(thread_id="dm-1", binding_token=token, text="第五条")
        ack = await client.expect(lambda env: env.type == "accepted", timeout=10.0)
        assert ack.payload["state"] in ("queued", "done", "processing")


async def test_rebind_pushes_revoked_then_active(tmp_path):
    """换绑要通知在线通道：先 revoked（旧版本号），再 active（新令牌可用）。"""
    async with running_core(tmp_path, replies=["嗯。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        old_version = int(bound["thread"]["binding_version"])
        old_token = str(bound["thread"]["binding_token"])

        await mgmt.call("thread.bind", channel="builtin", thread_id="dm-1", session_id=bound["session"]["id"])

        revoked = await client.expect(lambda env: env.type == "binding", timeout=10.0)
        active = await client.expect(lambda env: env.type == "binding", timeout=10.0)
        assert revoked.payload["state"] == "revoked", revoked.payload
        assert int(revoked.payload["binding_version"]) == old_version
        assert revoked.payload["binding_token"] == old_token
        assert active.payload["state"] == "active", active.payload
        assert int(active.payload["binding_version"]) == old_version + 1

        await client.send_user_message(
            thread_id="dm-1", binding_token=str(active.payload["binding_token"]), text="换绑之后"
        )
        ack = await client.expect(lambda env: env.type == "accepted", timeout=10.0)
        assert ack.payload["state"] in ("queued", "processing", "done")


async def test_status_interrupted_when_turn_is_voided_midway(tmp_path):
    """作废打断：客户端看到 status=interrupted（再回 idle），不是静默结束、也不是成功回复。"""
    async with running_core(tmp_path, replies=["不该出现。"]) as h:
        gate = asyncio.Event()
        original = h.fake.chat

        async def blocked(messages, **kwargs):
            await gate.wait()
            return await original(messages, **kwargs)

        h.fake.chat = blocked
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = bound["thread"]["binding_token"]
        channel_instance = client.hello_ack["channel_instance"]

        env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="这句会被作废")
        thinking = await client.expect(
            lambda env: env.type == "status" and env.payload["state"] == "thinking", timeout=10.0
        )
        assert thinking.payload["state"] == "thinking"

        h.store.void_put(channel_instance, "dm-1", env_id, "rollback")
        gate.set()

        seen = []  # 打断等待期间的其他信封（不该有 reply）
        await client.expect(
            lambda env: env.type == "status" and env.payload["state"] == "interrupted",
            timeout=15.0,
            collect=seen,
        )
        idle = await client.expect(
            lambda env: env.type == "status" and env.payload["state"] == "idle", timeout=15.0
        )
        assert idle.payload["state"] == "idle"
        assert not [env for env in seen if env.type == "reply"], "被打断的轮次不该有回复"
        assert h.store.inbound_find(channel_instance, "dm-1", env_id)["state"] == "cancelled"
