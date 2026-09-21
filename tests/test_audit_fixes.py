"""审计修正项的行为测试：错误阶段 / 握手回带令牌 / 换代通知 / 作废不可重放 / 发送阻塞 / 存储不可用。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump
from isekai_core.client import UmpClient

REPO = Path(__file__).resolve().parent.parent


def test_error_envelope_carries_stage():
    error = ump.UmpError(
        ump.Err.GENERATION_FAILED, "生成失败", retryable=True, ref="e-1", stage=ump.Stage.GENERATE
    )
    env = ump.error_envelope(error, thread_id="dm-1", ref="e-1")
    assert env["payload"]["stage"] == "generate"
    assert ump.parse(env, direction="s2c").payload["stage"] == "generate"


def test_error_stage_must_be_known_value():
    env = ump.error_envelope(ump.UmpError(ump.Err.INTERNAL, "x"))
    env["payload"]["stage"] = "nonsense"
    with pytest.raises(ump.UmpError):
        ump.parse(env, direction="s2c")


async def test_generation_error_reports_generate_stage(tmp_path):
    async with running_core(tmp_path, replies=["不会用到"]) as h:
        h.fake.fail_with = __import__("isekai_core.llm", fromlist=["LLMError"]).LLMError(
            "llm_unreachable", "网络不可达", retryable=True
        )
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        try:
            await client.send_user_message(
                thread_id="dm-1", binding_token=info["thread"]["binding_token"], text="在吗"
            )
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == "llm_unreachable"
            assert error.payload["stage"] == "generate"
        finally:
            await client.close()
            await mgmt.close()


async def test_hello_ack_carries_existing_thread_tokens(tmp_path):
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        credential = info["credential"]
        try:
            await client.close()
            again = UmpClient(
                endpoint=h.endpoint, channel_id="builtin", name="builtin", credential=credential
            )
            ack = await again.connect()
            threads = {item["id"]: item for item in ack["threads"]}
            assert threads["dm-1"]["binding_token"] == info["thread"]["binding_token"]
            assert threads["dm-1"]["binding_version"] == info["thread"]["binding_version"]
            await again.close()
        finally:
            await mgmt.close()


async def test_rebind_notifies_connected_channel(tmp_path):
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        try:
            rebound = await mgmt.call(
                "thread.bind", channel="builtin", thread_id="dm-1", session_id=info["session"]["id"]
            )
            # 换代先通知旧绑定作废，再给新的令牌（2026-09-22：只发 active 会让客户端拿着旧令牌硬撞 binding_expired）
            revoked = await client.expect(lambda e: e.type == "binding", timeout=5)
            assert revoked.payload["thread_id"] == "dm-1"
            assert revoked.payload["state"] == "revoked"
            assert revoked.payload["binding_version"] == info["thread"]["binding_version"]
            notice = await client.expect(lambda e: e.type == "binding", timeout=5)
            assert notice.payload["thread_id"] == "dm-1"
            assert notice.payload["state"] == "active"
            assert notice.payload["binding_token"] == rebound["thread"]["binding_token"]
            assert notice.payload["binding_version"] == rebound["thread"]["binding_version"]
        finally:
            await client.close()
            await mgmt.close()


async def test_revoked_input_cannot_be_replayed(tmp_path):
    async with running_core(tmp_path, replies=["回了一句。"]) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        token = info["thread"]["binding_token"]
        channel_id = info["thread"]["channel_id"]
        try:
            fixed = ump.make("user_message", {"text": "在吗"}, thread_id="dm-1", binding_token=token, id="e-roll")
            await client.send(fixed)
            await client.expect(lambda e: e.type == "reply")

            h.store.void_put(channel_id, "dm-1", "e-roll", "rollback")  # 模拟回滚作废
            await client.send(fixed)
            error = await client.expect(lambda e: e.type == "error")
            assert error.payload["code"] == ump.Err.VOIDED

            await client.request_retry(thread_id="dm-1", binding_token=token, ref="e-roll")
            retry_error = await client.expect(lambda e: e.type == "error")
            assert retry_error.payload["code"] == ump.Err.VOIDED
            assert len(h.fake.calls) == 1  # 作废输入没有被重放
        finally:
            await client.close()
            await mgmt.close()


async def test_stuck_channel_does_not_block_turn(tmp_path, monkeypatch):
    monkeypatch.setattr("isekai_core.session.SEND_TIMEOUT_S", 0.2)
    async with running_core(tmp_path, replies=["一句回复"]) as h:
        original = h.runtime.service.deliver

        async def slow_for_replies(channel_id: str, thread_id: str, envelope: dict) -> bool:
            if envelope.get("type") == "reply":
                await asyncio.sleep(30)  # 通道不读数据
                return True
            return await original(channel_id, thread_id, envelope)

        h.runtime.service.deliver = slow_for_replies
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="builtin", thread_id="dm-1")
        try:
            env_id = await client.send_user_message(
                thread_id="dm-1", binding_token=info["thread"]["binding_token"], text="在吗"
            )
            await asyncio.sleep(1.0)
            row = h.store.inbound_find(info["thread"]["channel_id"], "dm-1", env_id)
            assert row["state"] == "done"  # 轮次已固化完成，没有被卡死
            msg = h.store.outbound_by_message_id(row["reply_message_id"])
            assert h.store.delivery_rollup(msg["seq"]) == "unknown"  # 发送结果未知，不假称成功
        finally:
            await client.close()
            await mgmt.close()


def test_unwritable_data_dir_reports_persistence_blocked(tmp_path):
    (tmp_path / "data").write_text("not a directory", encoding="utf-8")
    core = subprocess.Popen(
        [sys.executable, "-m", "isekai_core", "--root", str(tmp_path)],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        assert core.stdout is not None
        ready = json.loads(core.stdout.readline().decode("utf-8"))
        assert ready["event"] == "ready"
        assert ready["state"] == "persistence_blocked"
        assert ready["endpoint"] is None
        assert ready["error"]
    finally:
        core.kill()
        core.wait(timeout=10)
