"""进程内通道（CHANNEL_PLUGIN_SPEC §2.5「安卓内建」）+ 两端解释同一包一致（WORLD_SETTING_SPEC §2.5 / 附录 C2）。

进程内传输与桌面 WS 走**同一段**握手 / 认证 / 分发代码，只是不占回环端口。
"""

from __future__ import annotations

import json

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump
from isekai_core.local_channel import InProcessChannel
from samples import sample_package


async def test_in_process_channel_handshakes_like_ws(tmp_path) -> None:
    async with running_core(tmp_path) as h:
        channel = InProcessChannel(h.runtime.server, channel_id="builtin-local", name="安卓内建")
        ack = await channel.connect(bootstrap=h.bootstrap)
        assert ack.get("type") == "hello_ack", ack
        assert ack["payload"]["state"] == "ready"
        assert int(ack["payload"]["negotiated"]["max_text_len"]) >= 1, "协商限额照旧"
        assert ack["payload"].get("credential"), "引导换持久凭据的语义与桌面一致"
        await channel.close()


async def test_in_process_channel_refuses_forged_credential(tmp_path) -> None:
    async with running_core(tmp_path) as h:
        channel = InProcessChannel(h.runtime.server, channel_id="builtin-local")
        ack = await channel.connect(credential="cr-forged")
        assert ack.get("type") == "error", ack
        assert ack["payload"]["code"] == ump.Err.AUTH_FAILED
        await channel.close()


async def test_extension_fields_refused_on_both_transports(tmp_path) -> None:
    """能力边界与传输无关：进程内也不静默收下附件 / 流式字段。"""
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        ws_client, info = await bind_thread(h, mgmt, channel_id="local-probe", thread_id="dm-1")
        await ws_client.close()  # 只借它的通道与 thread 绑定，收发都走进程内
        channel = InProcessChannel(h.runtime.server, channel_id="local-probe", name="安卓内建")
        ack = await channel.connect(credential=info["credential"])
        assert ack.get("type") == "hello_ack", ack
        bad = ump.make(
            "user_message",
            {"text": "带张图", "attachments": [{"kind": "image"}]},
            thread_id="dm-1",
            binding_token=info["thread"]["binding_token"],
        )
        frames = await channel.send(bad)
        assert frames and frames[0]["type"] == "error", frames
        assert frames[0]["payload"]["code"] == ump.Err.UNSUPPORTED_CAPABILITY, frames
        await channel.close()


async def test_both_transports_interpret_the_same_package_identically(tmp_path) -> None:
    """同一份固化产物，两条传输给出同一结论（WORLD_SETTING_SPEC §2.5 / 附录 C2）。"""
    package_file = tmp_path / "wp-consistent.json"
    package_file.write_text(json.dumps(sample_package(), ensure_ascii=False), encoding="utf-8")

    async with running_core(tmp_path / "core-a") as ws_core:
        ws_mgmt = await open_mgmt(ws_core)
        over_ws = await ws_mgmt.call("world.package.validate", package_path=str(package_file))
        await ws_mgmt.close()

    async with running_core(tmp_path / "core-b") as local_core:
        local = InProcessChannel(local_core.runtime.server, channel_id="builtin-local", name="安卓内建")
        auth = await local.connect_mgmt()
        assert auth.get("ok") is True, auth
        reply = await local.mgmt("world.package.validate", {"package_path": str(package_file)})
        assert reply.get("ok") is True, reply
        over_local = reply["result"]
        await local.close()

    assert over_local == over_ws, "两条传输对同一份包的解释必须逐字段一致"

    # 坏的包也一样：两端都必须拒绝，且理由同一份
    broken = tmp_path / "wp-broken.json"
    broken.write_text(json.dumps({**sample_package(), "calendar": {}}, ensure_ascii=False), encoding="utf-8")
    async with running_core(tmp_path / "core-c") as ws_core:
        ws_mgmt = await open_mgmt(ws_core)
        bad_ws = await ws_mgmt.call("world.package.validate", package_path=str(broken))
        await ws_mgmt.close()
    async with running_core(tmp_path / "core-d") as local_core:
        local = InProcessChannel(local_core.runtime.server, channel_id="builtin-local")
        await local.connect_mgmt()
        reply = await local.mgmt("world.package.validate", {"package_path": str(broken)})
        await local.close()
    assert reply["result"] == bad_ws and bad_ws.get("errors"), (bad_ws, reply)
