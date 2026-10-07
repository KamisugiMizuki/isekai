# -*- coding: utf-8 -*-
"""终焉之地实例：story 层“追赶中”判定的逐秒 pattern 实测。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_zhongyan_catchup.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

for key in ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core.cli import spawn_core  # noqa: E402
from isekai_core.client import MgmtClient  # noqa: E402

INSTANCE = "in-e72542c8cd3f"
TIMELINE = "tl-0f0aa9b5"
CARD = "cc-程霜"


async def main() -> int:
    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    try:
        await mgmt.connect()
        await asyncio.sleep(10)
        print("t | clock.target | clock.processed | scene.catching | scene.world_time | state")
        for i in range(15):
            clock = (await mgmt.call("runtime.clock", timeout=30, instance_id=INSTANCE, timeline_id=TIMELINE)).get("clock", {})
            scene = await mgmt.call("story.scene", timeout=30, instance_id=INSTANCE, timeline_id=TIMELINE, character_id=CARD)
            print(f"{i:2d} | {clock.get('world_seconds')} | {clock.get('processed_world')} | "
                  f"{scene.get('product_state')} | {scene.get('world_time')} | {clock.get('state')}")
            await asyncio.sleep(1.0)
        return 0
    finally:
        await mgmt.close()
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
