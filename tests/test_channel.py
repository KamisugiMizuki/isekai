"""通道宿主行为：认证、幂等、隔离、协商、连接卫生（CHANNEL_PLUGIN_SPEC 附录验收）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump
from isekai_core.client import MgmtClient, UmpClient
from isekai_core.ump import UmpError


async def test_bootstrap_token_is_one_time_and_yields_persistent_credential(tmp_path):
    async with running_core(tmp_path) as h:
        first = UmpClient(endpoint=h.endpoint, channel_id="builtin", name="内建聊天窗口", bootstrap=h.bootstrap)
        ack = await first.connect()
        credential = ack.get("credential")
        assert credential and ack["state"] == "ready"
        await first.close()

        # 同一引导凭据再次使用：拒绝
        second = UmpClient(endpoint=h.endpoint, channel_id="builtin", name="内建聊天窗口", bootstrap=h.bootstrap)
        with pytest.raises(UmpError) as excinfo:
            await second.connect()
        assert excinfo.value.code == ump.Err.AUTH_FAILED
        await second.close()

        # 持久凭据：重连继续使用，且不重复签发
        third = UmpClient(endpoint=h.endpoint, channel_id="builtin", name="内建聊天窗口", credential=credential)
        again = await third.connect()
        assert again["channel_instance"] == ack["channel_instance"]
        assert "credential" not in again
        await third.close()


async def test_wrong_credential_is_rejected(tmp_path):
    async with running_core(tmp_path) as h:
        forged = UmpClient(endpoint=h.endpoint, channel_id="builtin", name="builtin", credential="cr-forged")
        with pytest.raises(UmpError) as excinfo:
            await forged.connect()
        assert excinfo.value.code == ump.Err.AUTH_FAILED
        await forged.close()


async def test_two_channels_with_same_thread_and_env_id_do_not_crosstalk(tmp_path):
    async with running_core(tmp_path, replies=["只有一条回复。"]) as h:
        mgmt = await open_mgmt(h)
        client_a, info_a = await bind_thread(h, mgmt, channel_id="plugin-a", thread_id="dm-42")
        client_b, info_b = await bind_thread(h, mgmt, channel_id="plugin-b", thread_id="dm-42")
        try:
            shared = {"text": "同一个 thread 与同一个 id", "id": "e-shared"}
            await client_a.send(
                ump.make("user_message", {"text": shared["text"]}, thread_id="dm-42",
                         binding_token=info_a["thread"]["binding_token"], id=shared["id"])
            )
            await client_b.send(
                ump.make("user_message", {"text": shared["text"]}, thread_id="dm-42",
                         binding_token=info_b["thread"]["binding_token"], id=shared["id"])
            )
            reply_a = await client_a.expect(lambda e: e.type == "reply")
            reply_b = await client_b.expect(lambda e: e.type == "reply")
            assert reply_a.payload["message_id"] != reply_b.payload["message_id"]
            assert reply_a.payload["covers"] == ["e-shared"]
            assert h.store.counts()["messages"] == 4  # 各自一条入站 + 一条出站
        finally:
            await client_a.close()
            await client_b.close()
            await mgmt.close()


async def test_stale_binding_token_is_rejected_before_acceptance(tmp_path):
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        stale = info["thread"]["binding_token"]
        try:
            await mgmt.call(
                "thread.bind", channel="builtin", thread_id="dm-1", session_id=info["session"]["id"]
            )
            await client.send_user_message(thread_id="dm-1", binding_token=stale, text="旧令牌")
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == ump.Err.BINDING_EXPIRED
            assert h.store.counts()["messages"] == 0  # 未被接受、未排队
        finally:
            await client.close()
            await mgmt.close()


async def test_unknown_thread_returns_explicit_error(tmp_path):
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        client, _info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        try:
            await client.send_user_message(thread_id="dm-nothing", binding_token="bt-x", text="有人吗")
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == ump.Err.UNKNOWN_THREAD
        finally:
            await client.close()
            await mgmt.close()


async def test_negotiated_text_limit_is_enforced(tmp_path):
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1", max_text_len=50)
        token = info["thread"]["binding_token"]
        try:
            assert info["ack"]["negotiated"]["max_text_len"] == 50
            await client.send_user_message(thread_id="dm-1", binding_token=token, text="字" * 51)
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == ump.Err.PROTOCOL
            assert h.store.counts()["messages"] == 0
        finally:
            await client.close()
            await mgmt.close()


async def test_delivery_receipt_with_stale_token_is_rejected(tmp_path):
    """回执必须按固化时捕获的绑定令牌核对：换绑后的令牌不能触碰原投递记录。"""
    async with running_core(tmp_path, replies=["回了。"]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        try:
            await client.send_user_message(thread_id="dm-1", binding_token=token, text="在吗")
            reply = await client.expect(lambda e: e.type == "reply")
            message_id = reply.payload["message_id"]
            seq = h.store.outbound_by_message_id(message_id)["seq"]
            assert h.store.delivery_rollup(seq) == "sent"

            # 换代之后拿「新绑定」的令牌给旧回复补回执：不得写进原记录
            rebound = await mgmt.call(
                "thread.bind", channel="builtin", thread_id="dm-1", session_id=info["session"]["id"]
            )
            await client.report_delivery(
                thread_id="dm-1",
                binding_token=rebound["thread"]["binding_token"],
                message_id=message_id,
                batch_index=0,
                state="accepted",
            )
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == ump.Err.BINDING_EXPIRED
            assert h.store.delivery_rollup(seq) == "sent"
        finally:
            await client.close()
            await mgmt.close()


async def test_channel_ensure_does_not_rotate_existing_credential(tmp_path):
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            first = await mgmt.call("channel.ensure", name="builtin")
            assert first["credential"]
            second = await mgmt.call("channel.ensure", name="builtin")
            assert second.get("credential") is None  # 不轮换：既有凭据继续有效
            assert second["channel"]["id"] == first["channel"]["id"]

            client = UmpClient(
                endpoint=h.endpoint, channel_id="builtin", name="builtin",
                credential=first["credential"],
            )
            ack = await client.connect()
            assert ack["channel_instance"] == first["channel"]["id"]
            await client.close()

            rotated = await mgmt.call("channel.ensure", name="builtin", rotate=True)
            assert rotated["credential"] and rotated["credential"] != first["credential"]
            stale = UmpClient(
                endpoint=h.endpoint, channel_id="builtin", name="builtin",
                credential=first["credential"],
            )
            with pytest.raises(UmpError) as excinfo:
                await stale.connect()
            assert excinfo.value.code == ump.Err.AUTH_FAILED
            await stale.close()
        finally:
            await mgmt.close()


async def test_status_only_sent_to_capable_channels(tmp_path):
    async with running_core(tmp_path, replies=["好。"]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1", status=False)
        token = info["thread"]["binding_token"]
        try:
            assert info["ack"]["negotiated"]["status"] is False
            await client.send_user_message(thread_id="dm-1", binding_token=token, text="在吗")
            collected: list = []
            await client.expect(lambda e: e.type == "reply", collect=collected)
            assert [e.type for e in collected] == ["accepted"]
        finally:
            await client.close()
            await mgmt.close()


async def test_protocol_error_limit_closes_connection(tmp_path):
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        try:
            for _ in range(5):
                await client.send({"ump": "1.0", "id": "e-bad", "ts": 0.0})  # 缺 type
            await asyncio.sleep(0.3)
            assert info["thread"]["channel_id"] not in h.runtime.server.connected_channels()
        finally:
            await client.close()
            await mgmt.close()


async def test_management_requires_shell_token(tmp_path):
    async with running_core(tmp_path) as h:
        forged = MgmtClient(h.endpoint, "mg-forged")
        with pytest.raises(UmpError) as excinfo:
            await forged.connect()
        assert excinfo.value.code == ump.Err.AUTH_FAILED
        await forged.close()


async def test_protocol_version_mismatch_is_rejected(tmp_path):
    async with running_core(tmp_path) as h:
        from websockets.asyncio.client import connect

        ws = await connect(h.endpoint)
        await ws.send(json.dumps({"ump": "2.0", "type": "hello", "id": "e-1", "ts": 0.0, "payload": {}}))
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert frame["type"] == "error"
        assert frame["payload"]["code"] == ump.Err.PROTOCOL
        await ws.close()
