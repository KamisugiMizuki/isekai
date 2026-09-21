"""流式表达（CHANNEL_PLUGIN_SPEC §七 更后置项之一，2026-09-22 落地）：真核心 + 真 WS + 真 SQLite。

判据：只有协商 `streaming` 的通道收得到增量；增量有序、与最终 `message_id` 同号、拼起来等于最终正文；
流到一半坏掉时客户端拿到错误且**没有**最终回复；增量永不作数——正文以固化帧为准。
"""

from __future__ import annotations

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump


async def test_deltas_stream_then_final_reply_matches(tmp_path):
    async with running_core(tmp_path, replies=["今天风大，我把窗关上了。"]) as h:
        h.fake.stream_chunk = 3
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1", streaming=True)
        assert client.hello_ack["negotiated"]["streaming"] is True
        token = bound["thread"]["binding_token"]

        env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="在吗")
        deltas: list[dict] = []
        final = None
        while final is None:
            envelope = await client.expect(
                lambda env: env.type in ("reply_delta", "reply", "error"), timeout=20.0
            )
            if envelope.type == "reply_delta":
                deltas.append(envelope.payload)
            elif envelope.type == "reply":
                final = envelope
            else:
                raise AssertionError(f"不该有错误：{envelope.payload}")

        assert len(deltas) >= 3, deltas
        assert [item["index"] for item in deltas] == list(range(len(deltas))), "增量必须按序编号"
        assert len({item["message_id"] for item in deltas}) == 1, "一轮的增量共用一个 message_id"
        assert final.payload["message_id"] == deltas[0]["message_id"]
        streamed = "".join(str(item["text"]) for item in deltas)
        final_text = "".join(str(part["text"]) for part in final.payload["parts"])
        assert streamed == final_text
        assert final.payload["reply_to"] == env_id

        # 固化之后历史里只有一条回复（增量不是消息，不入库）
        page = h.store.history_page(bound["session"]["id"], limit=10)
        assert [item["role"] for item in page["messages"]] == ["user", "character"], page["messages"]


async def test_non_streaming_channel_gets_no_deltas(tmp_path):
    """没协商流式的通道：拿不到增量，但最终回复照常（能力位只影响预览）。"""
    async with running_core(tmp_path, replies=["嗯，我在。"]) as h:
        h.fake.stream_chunk = 2
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        env_id = await client.send_user_message(
            thread_id="dm-1", binding_token=bound["thread"]["binding_token"], text="在吗"
        )
        seen: list[str] = []
        final = await client.expect(
            lambda env: env.type in ("reply", "reply_delta"), timeout=20.0, collect=[]
        )
        while final.type != "reply":
            seen.append(final.type)
            final = await client.expect(lambda env: env.type in ("reply", "reply_delta"), timeout=20.0)
        assert "reply_delta" not in seen, seen
        assert final.payload["reply_to"] == env_id


async def test_stream_failure_midway_is_reported_without_final_reply(tmp_path):
    """流到一半坏掉：客户端拿到 stream 错误（可重试）+ 没有最终回复；入站行标失败。"""
    async with running_core(tmp_path, replies=["这段话说一半就断。"]) as h:
        h.fake.stream_chunk = 2
        h.fake.stream_fail_after = 1
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1", streaming=True)
        token = bound["thread"]["binding_token"]
        env_id = await client.send_user_message(thread_id="dm-1", binding_token=token, text="说点长的")

        deltas: list[dict] = []
        error = None
        while error is None:
            envelope = await client.expect(
                lambda env: env.type in ("reply_delta", "reply", "error"), timeout=20.0
            )
            if envelope.type == "reply_delta":
                deltas.append(envelope.payload)
            elif envelope.type == "error":
                error = envelope
            else:
                raise AssertionError("流坏掉了就不该有最终回复")
        assert deltas, "坏之前应该已经吐过增量"
        assert error.payload["code"] == "llm_unreachable"
        assert error.payload["retryable"] is True
        row = h.store.inbound_find(client.hello_ack["channel_instance"], "dm-1", env_id)
        assert row["state"] == "failed" and row["error_code"] == "llm_unreachable"


def test_reply_delta_payload_is_validated() -> None:
    """帧校验：增量必须有 message_id / 序号 / 非空文本（客户端按同一套校验丢弃坏帧）。"""
    good = ump.make("reply_delta", {"message_id": "m-1", "index": 0, "text": "哈"}, thread_id="dm-1")
    env = ump.parse(good, direction="s2c")
    assert env.type == "reply_delta" and env.thread_id == "dm-1"

    bad = ump.make("reply_delta", {"message_id": "m-1", "index": -1, "text": "哈"}, thread_id="dm-1")
    try:
        ump.parse(bad, direction="s2c")
    except ump.UmpError as exc:
        assert exc.code == ump.Err.PROTOCOL
    else:  # pragma: no cover - 校验失效才会走到
        raise AssertionError("负序号应被拒")
