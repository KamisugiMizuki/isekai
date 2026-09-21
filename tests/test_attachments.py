"""附件（CHANNEL_PLUGIN_SPEC §七 更后置项之一，2026-09-22 落地）：真核心 + 真 WS + 真 SQLite。

判据：没协商该能力的通道给附件 = 明确拒绝（不是静默丢掉字段）；协商了就按配额收、随消息落库、
历史读得回来、进模型时图像走 image_url、非图像只留一行文字标注。
"""

from __future__ import annotations

import base64

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 24  # 不是真图，够验字节数与前缀
PNG_B64 = base64.b64encode(PNG).decode("ascii")


def _attachment(name: str, media_type: str, blob: bytes) -> dict:
    return {"name": name, "media_type": media_type, "data": base64.b64encode(blob).decode("ascii")}


def use_core(tmp_path, **values) -> None:
    folder = tmp_path / "config"
    folder.mkdir(parents=True, exist_ok=True)
    body = "core:\n" + "".join(f"  {key}: {value}\n" for key, value in values.items())
    (folder / "config.yaml").write_text(body, encoding="utf-8")


async def test_attachments_without_capability_are_refused(tmp_path):
    """没声明附件能力的通道：带附件的帧被明确拒绝，且不落库。"""
    async with running_core(tmp_path, replies=["收到。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = bound["thread"]["binding_token"]
        await client.send_user_message(
            thread_id="dm-1",
            binding_token=token,
            text="带个图",
            attachments=[_attachment("a.png", "image/png", PNG)],
        )
        error = await client.expect(lambda env: env.type == "error", timeout=10.0)
        assert error.payload["code"] == ump.Err.UNSUPPORTED_CAPABILITY
        assert error.payload["retryable"] is False
        assert h.store.counts()["messages"] == 0, "被拒的帧不该落任何行"


async def test_attachments_roundtrip_into_prompt_and_history(tmp_path):
    """协商之后：随消息落库 → 历史读得回 → 进模型时图像是 image_url、文本件只留标注。"""
    async with running_core(tmp_path, replies=["图我看到了。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(
            h, mgmt, channel_id="builtin", thread_id="dm-1", attachments=True
        )
        assert client.hello_ack["negotiated"]["attachments"] is True
        token = bound["thread"]["binding_token"]

        await client.send_user_message(
            thread_id="dm-1",
            binding_token=token,
            text="看看这个",
            attachments=[
                _attachment("pic.png", "image/png", PNG),
                _attachment("note.txt", "text/plain", b"hello attachment"),
            ],
        )
        await client.expect(lambda env: env.type == "reply", timeout=15.0)

        # 落库 + 历史
        page = h.store.history_page(bound["session"]["id"], limit=10)
        user_rows = [item for item in page["messages"] if item["role"] == "user"]
        assert user_rows, page
        stored = user_rows[-1]
        assert stored["attachments"] and "pic.png" in stored["attachments"]
        assert "note.txt" in stored["attachments"]

        # 进模型：最后一个 user 消息的 content 是块列表
        prompt = h.fake.calls[-1]
        last_user = [item for item in prompt if item["role"] == "user"][-1]
        blocks = last_user["content"]
        assert isinstance(blocks, list), blocks
        kinds = [block["type"] for block in blocks]
        assert kinds[0] == "text" and "image_url" in kinds, kinds
        text_block = blocks[0]["text"]
        assert "看看这个" in text_block
        assert "note.txt" in text_block and "hello attachment" not in text_block, "非图像附件只给标注，不塞正文"
        image_block = next(block for block in blocks if block["type"] == "image_url")
        assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


async def test_attachment_quota_and_bad_base64(tmp_path):
    """配额与格式：条数超限 / 字节超限 → unsupported_capability；坏 base64 → protocol_error。"""
    use_core(tmp_path, max_attachments=1, max_attachment_bytes=16)
    async with running_core(tmp_path, replies=["嗯。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(
            h, mgmt, channel_id="builtin", thread_id="dm-1", attachments=True
        )
        token = bound["thread"]["binding_token"]

        await client.send_user_message(
            thread_id="dm-1",
            binding_token=token,
            text="两件",
            attachments=[
                _attachment("a.bin", "application/octet-stream", b"1234"),
                _attachment("b.bin", "application/octet-stream", b"5678"),
            ],
        )
        too_many = await client.expect(lambda env: env.type == "error", timeout=10.0)
        assert too_many.payload["code"] == ump.Err.UNSUPPORTED_CAPABILITY
        assert "条数" in too_many.payload["message"]

        await client.send_user_message(
            thread_id="dm-1",
            binding_token=token,
            text="太大",
            attachments=[_attachment("big.bin", "application/octet-stream", b"x" * 17)],
        )
        too_big = await client.expect(lambda env: env.type == "error", timeout=10.0)
        assert too_big.payload["code"] == ump.Err.UNSUPPORTED_CAPABILITY
        assert "字节" in too_big.payload["message"]

        await client.send_user_message(
            thread_id="dm-1",
            binding_token=token,
            text="坏编码",
            attachments=[{"name": "x.bin", "media_type": "application/octet-stream", "data": "not base64!!"}],
        )
        bad = await client.expect(lambda env: env.type == "error", timeout=10.0)
        assert bad.payload["code"] == ump.Err.PROTOCOL

        assert h.store.counts()["messages"] == 0, "三种被拒的帧都不该落库"


async def test_same_env_id_with_different_attachment_is_conflict(tmp_path):
    """幂等键是 (通道, thread, env_id)：同键换附件内容 = 冲突，不覆盖既有行。"""
    async with running_core(tmp_path, replies=["嗯。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await bind_thread(
            h, mgmt, channel_id="builtin", thread_id="dm-1", attachments=True
        )
        token = bound["thread"]["binding_token"]
        env_id = "e-dup-attach"
        await client.send(
            ump.make(
                "user_message",
                {"text": "同一键", "attachments": [_attachment("a.bin", "application/octet-stream", b"111")]},
                thread_id="dm-1",
                binding_token=token,
                id=env_id,
            )
        )
        await client.expect(lambda env: env.type == "accepted", timeout=10.0)
        await client.send(
            ump.make(
                "user_message",
                {"text": "同一键", "attachments": [_attachment("a.bin", "application/octet-stream", b"222")]},
                thread_id="dm-1",
                binding_token=token,
                id=env_id,
            )
        )
        error = await client.expect(lambda env: env.type == "error", timeout=10.0)
        assert error.payload["code"] == ump.Err.CONFLICT
