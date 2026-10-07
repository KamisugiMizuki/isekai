# -*- coding: utf-8 -*-
"""终焉之地实例：推进到白天后，在同一核心进程里与程霜多轮对话（真实 LLM 链路）。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_zhongyan_talk.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

for key in ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core.cli import (  # noqa: E402
    _load_credential, connect_channel, ensure_binding, run_turn, spawn_core,
)
from isekai_core.client import MgmtClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.version import APP_VERSION  # noqa: E402

INSTANCE = "in-e72542c8cd3f"
TIMELINE = "tl-0f0aa9b5"
CARD = "cc-程霜"
CHANNEL = "cli-dev"
THREAD = "dm-cli"

SCRIPT = [
    "喂——这次听着热闹多了。你醒了？",
    "这几天城里有什么动静没有？说给我听听。",
    "……我问你个事。有人跟我说，凑够三千六百个道就能离开这里。你信吗？",
    "那你打算怎么办？",
]


async def main() -> int:
    cfg = load_config(None)
    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    client = None
    try:
        await mgmt.connect()

        # 1) 推进到白天（10.5 小时）
        result = await mgmt.call(
            "runtime.time.consume", timeout=300,
            instance_id=INSTANCE, timeline_id=TIMELINE,
            seconds=37800, cause="夜里过去了，城里照常开门", time_source="world_process",
        )
        consume = result.get("consume", {})
        print(f"· consume: +{consume.get('consumed_seconds')}s -> {consume.get('processed_world')}")

        # 2) 等 tick 追平（让对话按“现在”作答）
        await asyncio.sleep(6)

        # 3) 绑定并连接
        issued = await mgmt.call("channel.ensure", name=CHANNEL, version=APP_VERSION)
        credential = issued.get("credential") or _load_credential(cfg, CHANNEL)
        if credential is None:
            issued = await mgmt.call("channel.ensure", name=CHANNEL, version=APP_VERSION, rotate=True)
            credential = issued["credential"]
        binding = await ensure_binding(
            cfg, mgmt, CHANNEL, THREAD,
            triple={"instance_id": INSTANCE, "timeline_id": TIMELINE, "character_id": CARD},
        )
        session, thread = binding["session"], binding["thread"]
        client = await connect_channel(
            cfg, ready["endpoint"],
            channel_id=CHANNEL, name="开发 CLI", bootstrap=ready.get("bootstrap"), credential=credential,
        )
        print(f"· 会话 {session['instance_id']}/{session['timeline_id']}/{session['character_id']} 绑定版本 {thread['binding_version']}")

        # 4) 多轮对话
        for text in SCRIPT:
            started = time.time()
            reply = await run_turn(client, thread_id=THREAD, token=thread["binding_token"], text=text, quiet=True, timeout=240.0)
            print(f"\n你> {text}")
            print(f"程霜> {reply}")
            print(f"（{time.time() - started:.1f}s）")
            await asyncio.sleep(1.5)
    finally:
        if client is not None:
            await client.close()
        await mgmt.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
