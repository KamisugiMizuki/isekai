"""会话核心行为（真实 WebSocket + 真实 SQLite，仅替换 LLM）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump
from isekai_core.client import UmpClient
from isekai_core.llm import LLMError


async def test_turn_is_fixed_and_delivered(tmp_path):
    async with running_core(tmp_path, replies=["刚结束今天的工作。"]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        try:
            env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="今天过得怎么样？")
            accepted = await client.expect(lambda e: e.type == "accepted")
            assert accepted.payload == {"ref": env_id, "state": "queued", "message_id": None}

            reply = await client.expect(lambda e: e.type == "reply")
            assert reply.payload["reply_to"] == env_id
            assert reply.payload["covers"] == [env_id]
            assert reply.payload["batch_index"] == 0 and reply.payload["batch_count"] == 1
            assert reply.payload["parts"] == [{"text": "刚结束今天的工作。"}]

            await client.report_delivery(
                thread_id="dm-1",
                binding_token=token,
                message_id=reply.payload["message_id"],
                batch_index=0,
                state="accepted",
            )
            await asyncio.sleep(0.1)
            stored = h.store.outbound_by_message_id(reply.payload["message_id"])
            assert h.store.delivery_rollup(stored["seq"]) == "delivered"

            inbound = h.store.inbound_find(info["thread"]["channel_id"], "dm-1", env_id)
            assert inbound["state"] == "done"
            assert inbound["reply_message_id"] == reply.payload["message_id"]

            # 上下文：系统提示 + 本轮用户文本
            assert h.fake.calls[0][0]["role"] == "system"
            assert h.fake.calls[0][-1] == {"role": "user", "content": "今天过得怎么样？"}
        finally:
            await client.close()
            await mgmt.close()


async def test_duplicate_input_returns_existing_result_without_regenerating(tmp_path):
    async with running_core(tmp_path, replies=["只有一句。"]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        channel_id = info["thread"]["channel_id"]
        try:
            fixed = ump.make("user_message", {"text": "在吗"}, thread_id="dm-1", binding_token=token, id="e-fixed")
            await client.send(fixed)
            await client.expect(lambda e: e.type == "accepted")
            await client.expect(lambda e: e.type == "reply")

            await client.send(fixed)  # 同键同文重发
            accepted = await client.expect(lambda e: e.type == "accepted")
            assert accepted.payload["state"] == "done"
            assert accepted.payload["ref"] == "e-fixed"
            assert len(h.fake.calls) == 1  # 没有第二次生成
            assert h.store.counts()["messages"] == 2  # 一条入站 + 一条出站

            conflict = ump.make(
                "user_message", {"text": "换了内容"}, thread_id="dm-1", binding_token=token, id="e-fixed"
            )
            await client.send(conflict)
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == ump.Err.CONFLICT
            assert h.store.inbound_find(channel_id, "dm-1", "e-fixed")["text"] == "在吗"
        finally:
            await client.close()
            await mgmt.close()


async def test_failed_generation_keeps_input_and_can_be_retried(tmp_path):
    failure = LLMError("llm_unreachable", "网络不可达", retryable=True)
    async with running_core(tmp_path, fail_with=failure) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        try:
            env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="在忙吗")
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == "llm_unreachable"
            assert error.payload["retryable"] is True
            assert error.payload["ref"] == env_id
            assert h.store.inbound_find(info["thread"]["channel_id"], "dm-1", env_id)["state"] == "failed"

            h.fake.fail_with = None
            h.fake.replies = ["刚才网络断了，现在回你。"]
            await client.request_retry(thread_id="dm-1", binding_token=token, ref=env_id)
            reply = await client.expect(lambda e: e.type == "reply")
            assert reply.payload["reply_to"] == env_id
            assert reply.payload["parts"] == [{"text": "刚才网络断了，现在回你。"}]
            assert len(h.fake.calls) == 2
            assert h.store.inbound_find(info["thread"]["channel_id"], "dm-1", env_id)["state"] == "done"
        finally:
            await client.close()
            await mgmt.close()


async def test_long_reply_is_split_into_batches_without_tail_loss(tmp_path):
    long_text = "\n".join(f"第{index}行：" + "内容" * 20 for index in range(600))
    async with running_core(tmp_path, replies=[long_text]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1", max_parts=2)
        token = info["thread"]["binding_token"]
        try:
            env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="说多点")
            collected: list[str] = []
            batches = 0
            while True:
                reply = await client.expect(lambda e: e.type == "reply")
                assert reply.payload["reply_to"] == env_id
                batches += 1
                assert reply.payload["batch_index"] == batches - 1
                assert len(reply.payload["parts"]) <= 2
                collected.extend(part["text"] for part in reply.payload["parts"])
                if reply.payload["batch_index"] == reply.payload["batch_count"] - 1:
                    assert reply.payload["batch_count"] == batches
                    break
            assert batches > 1  # 确实分了多批
            assert "\n".join(collected) == long_text  # 不裁剪正文尾部
        finally:
            await client.close()
            await mgmt.close()


async def test_late_result_is_dropped_when_thread_rebound(tmp_path):
    async with running_core(tmp_path, replies=["慢回复"]) as h:
        h.fake.delay_s = 0.4
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        try:
            env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="在吗")
            await client.expect(lambda e: e.type == "accepted")
            await mgmt.call(
                "thread.bind", channel="builtin", thread_id="dm-1", session_id=info["session"]["id"]
            )
            with pytest.raises(TimeoutError):
                await client.expect(lambda e: e.type == "reply", timeout=1.5)
            row = h.store.inbound_find(info["thread"]["channel_id"], "dm-1", env_id)
            assert row["state"] == "cancelled"
            page = h.store.history_page(info["session"]["id"], limit=10)
            assert all(item["role"] == "user" for item in page["messages"])
        finally:
            await client.close()
            await mgmt.close()


async def test_interrupted_turn_survives_restart_and_can_be_retried(tmp_path):
    async with running_core(tmp_path, replies=["慢回复"]) as h:
        h.fake.delay_s = 3.0
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        credential = info["credential"]
        channel_id = info["thread"]["channel_id"]
        env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="在吗")
        await client.expect(lambda e: e.type == "accepted")
        await asyncio.sleep(0.2)
        assert h.store.inbound_find(channel_id, "dm-1", env_id)["state"] == "processing"
        await client.close()
        await mgmt.close()
    # 进程关闭时该轮次仍未完成 —— 重启后必须可恢复，而不是永久卡在「处理中」
    async with running_core(tmp_path, replies=["恢复后的回复"]) as h2:
        row = h2.store.inbound_find(channel_id, "dm-1", env_id)
        assert row["state"] == "failed"
        assert row["error_code"] == "interrupted"
        client = UmpClient(endpoint=h2.endpoint, channel_id="builtin", name="builtin", credential=credential)
        await client.connect()
        try:
            await client.request_retry(thread_id="dm-1", binding_token=token, ref=env_id)
            reply = await client.expect(lambda e: e.type == "reply")
            assert reply.payload["reply_to"] == env_id
            assert reply.payload["parts"] == [{"text": "恢复后的回复"}]
            assert h2.store.inbound_find(channel_id, "dm-1", env_id)["state"] == "done"
        finally:
            await client.close()


async def test_resume_reports_incompatibility_when_limits_shrink(tmp_path):
    long_text = "\n".join(f"第{index}行：" + "内容" * 20 for index in range(300))
    async with running_core(tmp_path, replies=[long_text]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1", max_parts=10)
        token = info["thread"]["binding_token"]
        credential = info["credential"]
        try:
            env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="说多点")
            message_id = ""
            while True:
                reply = await client.expect(lambda e: e.type == "reply")
                message_id = reply.payload["message_id"]
                assert reply.payload["batch_count"] == 1
                if reply.payload["batch_index"] == reply.payload["batch_count"] - 1:
                    break
            await client.close()  # 不回执：回复处于「已发出未确认」

            parts_before = json.loads(h.store.outbound_by_message_id(message_id)["parts"])
            shrink = UmpClient(
                endpoint=h.endpoint, channel_id="builtin", name="builtin", credential=credential, max_parts=1
            )
            ack = await shrink.connect()
            assert ack["negotiated"]["max_parts"] == 1
            error = await shrink.expect(lambda e: e.type == "error", timeout=10)
            assert error.payload["code"] == ump.Err.UNSUPPORTED_CAPABILITY
            assert error.payload["ref"] == message_id
            stored = h.store.outbound_by_message_id(message_id)
            assert h.store.delivery_rollup(stored["seq"]) == "incompatible"
            assert json.loads(stored["parts"]) == parts_before  # 不重排、不裁剪、不重新生成
            await shrink.close()
        finally:
            await mgmt.close()


async def test_reply_is_resent_after_reconnect_without_regenerating(tmp_path):
    async with running_core(tmp_path, replies=["离线时说的话。"]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        credential = info["credential"]
        try:
            await client.send_user_message(thread_id="dm-1", binding_token=token, text="在吗")
            first = await client.expect(lambda e: e.type == "reply")
            await client.close()  # 模拟断线：回复已固化但未回执

            again = UmpClient(
                endpoint=h.endpoint, channel_id="builtin", name="builtin", credential=credential
            )
            await again.connect()
            resent = await again.expect(lambda e: e.type == "reply", timeout=10)
            assert resent.payload["message_id"] == first.payload["message_id"]
            assert resent.payload["parts"] == [{"text": "离线时说的话。"}]
            assert len(h.fake.calls) == 1  # 只重发，不重新生成
            await again.close()
        finally:
            await mgmt.close()
