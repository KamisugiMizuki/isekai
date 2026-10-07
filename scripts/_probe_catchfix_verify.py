# -*- coding: utf-8 -*-
"""验收：追赶判定（记账为准）+ tick 解耦（LLM 派生不挡推进）+ 处置遗留巨倍率线。

1) 稳态线不再常显「追赶中」（修复判据：clock.catching_up=False 且 story 场景 available）
2) 推进节拍按 ~5 秒跟进（修复前被 LLM 段拖成分钟级）
3) 遗留的「灰潮纪·制度二卡验收」线（limited=1 巨倍率永远追赶）如实报追赶 →
   冻结它（不再每轮空转推进）
4) 冻结后终焉线照常

用法：cd D:/Hermes_workspace/isekai && .venv/Scripts/python.exe scripts/_probe_catchfix_verify.py
"""
from __future__ import annotations

import asyncio
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

ZY = ("in-e72542c8cd3f", "tl-0f0aa9b5", "cc-程霜")  # 终焉之地（rate=1，用户在用）
GY = ("in-9f3d6fe11d9c", "tl-b02b384b")             # 灰潮纪·制度二卡验收（巨倍率遗留）


async def main() -> int:
    proc, ready = spawn_core(None)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    ok = True
    try:
        await mgmt.connect()
        await asyncio.sleep(8)  # 启动补算 + 至少一轮 tick

        # 1) 稳态线不报追赶
        clock = (await mgmt.call("runtime.clock", timeout=30, instance_id=ZY[0], timeline_id=ZY[1]))["clock"]
        scene = await mgmt.call("story.scene", timeout=30, instance_id=ZY[0], timeline_id=ZY[1],
                                character_id=ZY[2])
        steady = clock.get("catching_up") is False and scene.get("product_state") == "available"
        print(f"[1] 稳态线：clock.catching_up={clock.get('catching_up')} "
              f"story={scene.get('product_state')} → {'PASS' if steady else 'FAIL'}")
        ok &= steady

        # 2) 推进节拍：24 秒内 processed 按拍跟进（修复前被 LLM 段拖到 ~2 分钟一轮）
        samples = []
        for _ in range(24):
            c = (await mgmt.call("runtime.clock", timeout=30, instance_id=ZY[0], timeline_id=ZY[1]))["clock"]
            samples.append(int(c.get("processed_world") or 0))
            await asyncio.sleep(1.0)
        advances = sum(1 for a, b in zip(samples, samples[1:]) if b > a)
        span = samples[-1] - samples[0]
        beat = advances >= 3 and span >= 10
        print(f"[2] 推进节拍：24s 内前进 {advances} 次 / 共 {span} 世界秒 → {'PASS' if beat else 'FAIL'}")
        ok &= beat

        # 3) 遗留线冻结前如实报追赶（limited 记账；不因判定修复被吞掉）
        scope = await mgmt.call("runtime.scope.inspect", timeout=30, instance_id=GY[0], timeline_id=GY[1])
        legacy = scope.get("timeline_state") in ("catching_up", "active")
        print(f"[3] 遗留线冻结前：timeline_state={scope.get('timeline_state')} → {'PASS' if legacy else 'FAIL'}")
        ok &= legacy

        # 4) 冻结遗留线
        await mgmt.call("runtime.freeze", timeout=60, instance_id=GY[0], timeline_id=GY[1])
        scope2 = await mgmt.call("runtime.scope.inspect", timeout=30, instance_id=GY[0], timeline_id=GY[1])
        frozen = scope2.get("timeline_state") == "frozen"
        print(f"[4] 冻结遗留线：timeline_state={scope2.get('timeline_state')} → {'PASS' if frozen else 'FAIL'}")
        ok &= frozen

        # 5) 冻结后终焉线照常
        c2 = (await mgmt.call("runtime.clock", timeout=30, instance_id=ZY[0], timeline_id=ZY[1]))["clock"]
        alive = c2.get("state") == "active"
        print(f"[5] 终焉线仍 active：state={c2.get('state')} rate={c2.get('rate')} → {'PASS' if alive else 'FAIL'}")
        ok &= alive

        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await mgmt.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
