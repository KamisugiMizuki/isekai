# -*- coding: utf-8 -*-
"""把《十日终焉》世界观包与角色卡导入 isekai，创建实例并激活。

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_zhongyan_import.py
读数输出到 stdout（建议重定向到文件再读）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Clash 系统代理会拦 ws://127.0.0.1（websockets 读 HTTP_PROXY）——本地回环直连
for key in ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(key, None)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from isekai_core.cli import spawn_core  # noqa: E402
from isekai_core.client import MgmtClient  # noqa: E402

SCRATCH = Path(r"C:\Users\Kamisugi\AppData\Local\hermes\cache\scratch")


def show(title: str, payload) -> None:
    print(f"===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])


async def main() -> int:
    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    try:
        await mgmt.connect()
        # 1) 世界包导入（管理面：外部文件 → 校验 → 创作目录）
        result = await mgmt.call(
            "world.package.import",
            timeout=60,
            source_path=str(SCRATCH / "zhongyan.json"),
            name="zhongyan",
            force=True,
        )
        show("world.package.import", result)

        # 2) 角色卡导入（带包上下文联合校验）
        result = await mgmt.call(
            "world.card.import",
            timeout=60,
            source_path=str(SCRATCH / "chengshuang.json"),
            package_path="zhongyan.json",
            name="chengshuang",
            force=True,
        )
        show("world.card.import", result)

        # 3) 创建实例
        result = await mgmt.call(
            "instance.create",
            timeout=120,
            request_id="zhongyan-import-1",
            package_path="zhongyan.json",
            card_paths=["chengshuang.json"],
            display_name="终焉之地",
        )
        show("instance.create", result)
        instance_id = result["instance"]["id"]

        # 3b) 读实例信息：拿真实时间线标识
        info = await mgmt.call("instance.info", timeout=30, id=instance_id)
        timelines = [t.get("id") for t in info.get("timelines", [])]
        timeline_id = timelines[0] if timelines else "main"

        # 4) 激活时间线
        result = await mgmt.call(
            "runtime.activate",
            timeout=120,
            instance_id=instance_id,
            timeline_id=timeline_id,
        )
        clock = result.get("clock", {})
        show("runtime.activate",
             {"timeline": timeline_id, "state": clock.get("state"), "rate": clock.get("rate"),
              "processed_world": clock.get("processed_world"), "target_world": clock.get("target_world")})

        # 5) 实例信息读数
        show("instance.info", {
            "id": info["instance"].get("id"),
            "display_name": info["instance"].get("name"),
            "state": info["instance"].get("state"),
            "compatibility": info["instance"].get("compatibility"),
            "characters": info.get("characters"),
            "timelines": timelines,
        })
        print("INSTANCE_ID =", instance_id)
        print("TIMELINE_ID =", timeline_id)
    finally:
        await mgmt.close()
        proc.terminate()
        proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
